#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import csv
import json
import os
import re
import sys
import hashlib
import shutil
from pathlib import Path
from datetime import datetime
from collections import defaultdict

try:
    from PIL import Image
    from PIL.ExifTags import TAGS
    EXIF_AVAILABLE = True
except ImportError:
    EXIF_AVAILABLE = False

PHOTO_EXTENSIONS = {
    '.jpg', '.jpeg', '.png', '.gif', '.bmp',
    '.tiff', '.tif', '.heic', '.heif', '.webp',
    '.raw', '.cr2', '.nef', '.arw', '.dng',
    '.orf', '.rw2',
}

DEFAULT_CONFIG = {
    "source": "",
    "destination": "",
    "ignore_folders": [],
    "report": "photo_organize_report.md",
    "duplicate_csv": "duplicates.csv",
    "plan_file": "plan.json",
    "state_file": "library_state.json",
    "isolation_dir": "隔离重复",
    "dry_run": False,
    "incremental": True,
    "duplicate_mode": "separate",
}

YEAR_RE = re.compile(r'^\d{4}$')
MONTH_RE = re.compile(r'^\d{2}$')


def load_config(config_path):
    p = Path(config_path)
    if not p.exists():
        return None
    with open(p, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_default_config(config_path):
    with open(config_path, 'w', encoding='utf-8') as f:
        json.dump(DEFAULT_CONFIG, f, indent=2, ensure_ascii=False)


def load_json(path):
    p = Path(path)
    if not p.exists():
        return None
    with open(p, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_json(path, data):
    p = Path(path)
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_exif_datetime(file_path):
    if not EXIF_AVAILABLE:
        return None
    try:
        image = Image.open(file_path)
        exif_data = image._getexif()
        if not exif_data:
            return None
        for tag_id, value in exif_data.items():
            tag = TAGS.get(tag_id, tag_id)
            if tag in ('DateTimeOriginal', 'DateTimeDigitized', 'DateTime'):
                try:
                    return datetime.strptime(value, '%Y:%m:%d %H:%M:%S')
                except (ValueError, TypeError):
                    continue
    except Exception:
        return None
    return None


def get_file_modify_datetime(file_path):
    mtime = os.path.getmtime(file_path)
    return datetime.fromtimestamp(mtime)


def get_photo_datetime(file_path):
    dt = get_exif_datetime(file_path)
    if dt:
        return dt, 'EXIF'
    dt = get_file_modify_datetime(file_path)
    return dt, 'ModifyTime'


def calculate_md5(file_path, chunk_size=8192):
    md5 = hashlib.md5()
    with open(file_path, 'rb') as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            md5.update(chunk)
    return md5.hexdigest()


def is_ignored(path, ignore_folders):
    path = Path(path).resolve()
    for ignore in ignore_folders:
        ignore_path = Path(ignore).resolve()
        try:
            if path == ignore_path or ignore_path in path.parents:
                return True
        except Exception:
            continue
    return False


def find_photos(source_dir, ignore_folders):
    photos = []
    source_path = Path(source_dir).resolve()
    for root, dirs, files in os.walk(source_path):
        dirs[:] = [d for d in dirs if not is_ignored(Path(root) / d, ignore_folders)]
        if is_ignored(root, ignore_folders):
            continue
        for file in files:
            file_path = Path(root) / file
            if file_path.suffix.lower() in PHOTO_EXTENSIONS:
                photos.append(file_path)
    return photos


def scan_dest_library(dest_dir, ignore_folders):
    dest_path = Path(dest_dir).resolve()
    duplicate_dir = dest_path / '可能重复'
    isolation_dir = dest_path / '隔离重复'
    extra_ignore = [duplicate_dir, isolation_dir] + list(ignore_folders)

    md5_to_paths = defaultdict(list)
    for root, dirs, files in os.walk(dest_path):
        dirs[:] = [d for d in dirs if not is_ignored(Path(root) / d, extra_ignore)]
        if is_ignored(root, extra_ignore):
            continue
        for file in files:
            file_path = Path(root) / file
            if file_path.suffix.lower() not in PHOTO_EXTENSIONS:
                continue
            try:
                md5 = calculate_md5(file_path)
                md5_to_paths[md5].append(str(file_path.resolve()))
            except Exception:
                pass
    return md5_to_paths


class TargetPathAllocator:
    def __init__(self, dest_dir=None):
        self._assigned = set()
        if dest_dir is not None:
            self._scan_existing(dest_dir)

    def _scan_existing(self, dest_dir):
        dest_path = Path(dest_dir).resolve()
        if not dest_path.exists():
            return
        for root, _, files in os.walk(dest_path):
            for file in files:
                file_path = Path(root) / file
                self._assigned.add(str(file_path.resolve()))

    def allocate(self, target_path):
        target_path = Path(target_path).resolve()
        if str(target_path) not in self._assigned:
            self._assigned.add(str(target_path))
            return target_path
        stem = target_path.stem
        suffix = target_path.suffix
        parent = target_path.parent
        counter = 1
        while True:
            candidate = parent / f"{stem}_{counter}{suffix}"
            if str(candidate) not in self._assigned:
                self._assigned.add(str(candidate))
                return candidate
            counter += 1


def check_library_health(dest_dir, ignore_folders):
    dest_path = Path(dest_dir).resolve()
    duplicate_dir = dest_path / '可能重复'
    isolation_dir = dest_path / '隔离重复'
    extra_ignore = [duplicate_dir, isolation_dir] + list(ignore_folders)

    md5_map = defaultdict(list)
    no_exif_has_mtime = 0
    no_time_at_all = 0
    read_failed = 0
    read_failed_list = []
    empty_month_dirs = []

    for root, dirs, files in os.walk(dest_path):
        current_root = Path(root).resolve()
        try:
            rel_parts = current_root.relative_to(dest_path).parts
        except ValueError:
            continue
        if is_ignored(root, extra_ignore):
            dirs[:] = []
            continue
        if len(rel_parts) == 2:
            if YEAR_RE.match(rel_parts[0]) and MONTH_RE.match(rel_parts[1]):
                if not files:
                    empty_month_dirs.append(str(current_root))
        for file in files:
            file_path = current_root / file
            if file_path.suffix.lower() not in PHOTO_EXTENSIONS:
                continue
            try:
                exif_dt = get_exif_datetime(file_path)
                if exif_dt is not None:
                    pass
                else:
                    try:
                        mtime = os.path.getmtime(file_path)
                        datetime.fromtimestamp(mtime)
                        no_exif_has_mtime += 1
                    except Exception:
                        no_time_at_all += 1
                md5 = calculate_md5(file_path)
                md5_map[md5].append(str(file_path))
            except Exception as e:
                read_failed += 1
                read_failed_list.append((str(file_path), str(e)))

    dup_count = sum(1 for v in md5_map.values() if len(v) > 1)
    dup_file_count = sum(len(v) for v in md5_map.values() if len(v) > 1) - dup_count

    return {
        'duplicate_groups': dup_count,
        'duplicate_files': dup_file_count,
        'empty_month_dirs': len(empty_month_dirs),
        'empty_month_dir_list': empty_month_dirs,
        'no_exif_has_mtime': no_exif_has_mtime,
        'no_time_at_all': no_time_at_all,
        'read_failed': read_failed,
        'read_failed_list': read_failed_list,
    }


def organize_photos(source_dir, dest_dir, ignore_folders, dry_run=False,
                    duplicate_mode='separate', incremental=True, state_file=None,
                    duplicate_csv=None, plan_file=None):
    source_dir = Path(source_dir).resolve()
    dest_dir = Path(dest_dir).resolve()
    duplicate_dir = dest_dir / '可能重复'

    state = {'photos': {}}
    if incremental and state_file:
        loaded = load_json(state_file)
        if loaded and 'photos' in loaded:
            state = loaded
            print(f"已加载状态文件: {state_file} ({len(state['photos'])} 条记录)")
        else:
            print(f"状态文件不存在或为空，将从空状态开始 (后续将自动创建: {state_file})")
    known_md5s = {entry['md5']: entry for entry in state.get('photos', {}).values()}

    dest_md5_map = scan_dest_library(dest_dir, ignore_folders)
    dest_md5_lookup = {}
    for md5, paths in dest_md5_map.items():
        for p in paths:
            dest_md5_lookup[md5] = p

    allocator = TargetPathAllocator(dest_dir)

    photos = find_photos(source_dir, ignore_folders)

    md5_map = defaultdict(list)
    move_operations = []
    duplicate_operations = []
    skipped = []
    time_source_stats = {'EXIF': 0, 'ModifyTime': 0}
    photo_info_map = {}

    print(f"发现照片总数: {len(photos)}")

    for photo_path in photos:
        try:
            photo_dt, time_source = get_photo_datetime(photo_path)
            md5 = calculate_md5(photo_path)
            md5_map[md5].append(photo_path)
            photo_info_map[photo_path] = {
                'datetime': photo_dt,
                'time_source': time_source,
                'md5': md5,
            }
            time_source_stats[time_source] += 1
        except Exception as e:
            skipped.append((str(photo_path), f'处理失败: {e}'))

    for md5, file_list in md5_map.items():
        file_list.sort(key=lambda p: photo_info_map[p]['datetime'])
        is_dup_group = len(file_list) > 1

        kept = file_list[0]
        kept_info = photo_info_map[kept]

        in_state = incremental and md5 in known_md5s
        in_dest = md5 in dest_md5_lookup

        if in_state or in_dest:
            if in_state:
                existing = known_md5s[md5]
                existing_target = existing.get('target_path', '')
                existing_source = existing.get('source_path', '')
                if str(kept) == existing_source:
                    skip_reason = f'增量模式：已整理且内容未变化 (目标: {existing_target})'
                else:
                    skip_reason = f'增量模式：照片已存在于目标库 (原路径: {existing_source}, 目标: {existing_target})'
            else:
                dest_path_found = dest_md5_lookup[md5]
                skip_reason = f'目标库扫描：内容相同的照片已存在 (目标位置: {dest_path_found})'

            skipped.append((str(kept), skip_reason))
            for dup_path in file_list[1:]:
                dup_info = photo_info_map[dup_path]
                skipped.append((str(dup_path), f'与已入库照片重复 (MD5: {md5[:8]}...)'))
            continue

        year = kept_info['datetime'].strftime('%Y')
        month = kept_info['datetime'].strftime('%m')
        target_dir = dest_dir / year / month
        target_path = allocator.allocate(target_dir / kept.name)
        move_operations.append((
            str(kept), str(target_path), 'kept',
            kept_info['datetime'], kept_info['time_source'], kept_info['md5'],
        ))

        if is_dup_group:
            for dup_path in file_list[1:]:
                dup_info = photo_info_map[dup_path]
                dup_dt = dup_info['datetime']

                if duplicate_mode == 'list_only':
                    duplicate_operations.append((
                        str(dup_path), str(dup_path), 'duplicate_listed',
                        dup_dt, dup_info['time_source'], dup_info['md5'],
                    ))
                elif duplicate_mode == 'separate':
                    rel = dup_path.relative_to(source_dir)
                    sep_target = allocator.allocate(duplicate_dir / rel)
                    duplicate_operations.append((
                        str(dup_path), str(sep_target), 'duplicate_separate',
                        dup_dt, dup_info['time_source'], dup_info['md5'],
                    ))
                elif duplicate_mode == 'move':
                    dup_year = dup_dt.strftime('%Y')
                    dup_month = dup_dt.strftime('%m')
                    dup_target_dir = duplicate_dir / dup_year / dup_month
                    dup_target = allocator.allocate(dup_target_dir / dup_path.name)
                    duplicate_operations.append((
                        str(dup_path), str(dup_target), 'duplicate_move',
                        dup_dt, dup_info['time_source'], dup_info['md5'],
                    ))

    all_operations = move_operations + duplicate_operations

    plan_data = None
    if plan_file and dry_run:
        plan_data = {
            'created_at': datetime.now().isoformat(),
            'source_dir': str(source_dir),
            'dest_dir': str(dest_dir),
            'duplicate_mode': duplicate_mode,
            'move_operations': [
                {'source': op[0], 'target': op[1], 'type': op[2],
                 'datetime': op[3].isoformat() if isinstance(op[3], datetime) else '',
                 'time_source': op[4], 'md5': op[5]}
                for op in move_operations
            ],
            'duplicate_operations': [
                {'source': op[0], 'target': op[1], 'type': op[2],
                 'datetime': op[3].isoformat() if isinstance(op[3], datetime) else '',
                 'time_source': op[4], 'md5': op[5]}
                for op in duplicate_operations
            ],
            'skipped': [{'path': s[0], 'reason': s[1]} for s in skipped],
        }
        save_json(plan_file, plan_data)
        print(f"计划文件已保存: {Path(plan_file).resolve()}")

    if not dry_run:
        for op in all_operations:
            src, dst, op_type, dt, time_source, md5 = op
            if op_type == 'duplicate_listed':
                continue
            dst_path = Path(dst)
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(src, dst)
            if op_type in ('kept', 'normal') and incremental and state_file:
                state.setdefault('photos', {})[md5] = {
                    'md5': md5,
                    'source_path': src,
                    'target_path': dst,
                    'datetime': dt.isoformat() if isinstance(dt, datetime) else '',
                    'time_source': time_source,
                    'added_at': datetime.now().isoformat(),
                }

        if incremental and state_file:
            save_json(state_file, state)
            print(f"状态文件已保存: {state_file} ({len(state.get('photos', {}))} 条记录)")

    monthly_stats = _build_monthly_stats(move_operations, duplicate_operations)
    health_stats = check_library_health(dest_dir, ignore_folders)

    report = generate_markdown_report(
        source_dir, dest_dir, move_operations, duplicate_operations,
        skipped, time_source_stats, monthly_stats, health_stats,
        dry_run, duplicate_mode, incremental,
    )

    if duplicate_csv and duplicate_operations:
        export_duplicate_csv(duplicate_csv, duplicate_operations, md5_map)

    return report, all_operations, health_stats, skipped


def execute_plan(plan_path, incremental=True, state_file=None):
    plan = load_json(plan_path)
    if not plan:
        print(f"错误: 计划文件不存在或格式错误: {plan_path}")
        return

    move_ops = plan.get('move_operations', [])
    dup_ops = plan.get('duplicate_operations', [])

    all_ops = move_ops + dup_ops

    print(f"执行计划文件: {plan_path}")
    print(f"计划移动: {len(move_ops)} 个文件")
    print(f"计划重复处理: {len(dup_ops)} 个文件")

    state = {'photos': {}}
    if incremental and state_file:
        loaded = load_json(state_file)
        if loaded and 'photos' in loaded:
            state = loaded
            print(f"已加载状态文件: {state_file} ({len(state['photos'])} 条记录)")
        else:
            print(f"状态文件不存在或为空，将从空状态开始 (执行完成后将自动创建)")

    executed_count = 0
    skipped_count = 0

    for op in all_ops:
        src = op['source']
        dst = op['target']
        op_type = op['type']
        md5 = op.get('md5', '')

        if op_type == 'duplicate_listed':
            continue

        if not Path(src).exists():
            print(f"  跳过 (源文件不存在): {src}")
            skipped_count += 1
            continue

        dst_path = Path(dst)
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(src, dst)
        executed_count += 1

        if op_type in ('kept', 'normal') and incremental and state_file:
            state.setdefault('photos', {})[md5] = {
                'md5': md5,
                'source_path': src,
                'target_path': dst,
                'datetime': op.get('datetime', ''),
                'time_source': op.get('time_source', ''),
                'added_at': datetime.now().isoformat(),
            }

    if incremental and state_file:
        save_json(state_file, state)
        print(f"状态文件已保存: {state_file} ({len(state.get('photos', {}))} 条记录)")

    print(f"\n计划执行完成!")
    print(f"  成功执行: {executed_count} 个操作")
    print(f"  跳过: {skipped_count} 个 (源文件不存在)")
    print(f"  重复(仅列出): {sum(1 for o in dup_ops if o['type'] == 'duplicate_listed')} 个")


def clean_duplicates(dest_dir, ignore_folders, plan_only=True, isolation_dir_name='隔离重复', duplicate_csv=None):
    dest_path = Path(dest_dir).resolve()
    isolation_path = dest_path / isolation_dir_name
    duplicate_dir = dest_path / '可能重复'
    extra_ignore = [duplicate_dir, isolation_path] + list(ignore_folders)

    md5_map = defaultdict(list)
    for root, dirs, files in os.walk(dest_path):
        dirs[:] = [d for d in dirs if not is_ignored(Path(root) / d, extra_ignore)]
        if is_ignored(root, extra_ignore):
            continue
        for file in files:
            file_path = Path(root) / file
            if file_path.suffix.lower() not in PHOTO_EXTENSIONS:
                continue
            try:
                md5 = calculate_md5(file_path)
                md5_map[md5].append(str(file_path.resolve()))
            except Exception:
                pass

    clean_groups = []
    for md5, paths in md5_map.items():
        if len(paths) <= 1:
            continue
        paths.sort()
        kept = paths[0]
        to_isolate = paths[1:]
        clean_groups.append({
            'group_id': len(clean_groups) + 1,
            'md5': md5,
            'kept': kept,
            'isolated': to_isolate,
        })

    if duplicate_csv:
        with open(duplicate_csv, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f)
            writer.writerow([
                '重复组编号', 'MD5', '操作', '文件路径',
                '隔离目标路径',
            ])
            for group in clean_groups:
                writer.writerow([
                    group['group_id'], group['md5'], '保留', group['kept'], '',
                ])
                for iso_path in group['isolated']:
                    rel = Path(iso_path).relative_to(dest_path)
                    iso_target = str(isolation_path / rel)
                    writer.writerow([
                        group['group_id'], group['md5'], '隔离', iso_path, iso_target,
                    ])
        print(f"清理清单已导出: {Path(duplicate_csv).resolve()}")

    if not plan_only:
        for group in clean_groups:
            for iso_path_str in group['isolated']:
                src = Path(iso_path_str)
                if not src.exists():
                    continue
                rel = src.relative_to(dest_path)
                target = isolation_path / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(target))
        print(f"清理完成! 共隔离 {sum(len(g['isolated']) for g in clean_groups)} 个重复文件到 {isolation_path}")

    return clean_groups


def export_duplicate_csv(csv_path, duplicate_operations, md5_map):
    md5_to_group = {}
    group_id = 1
    for md5 in md5_map:
        if len(md5_map[md5]) > 1:
            md5_to_group[md5] = group_id
            group_id += 1

    with open(csv_path, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        writer.writerow([
            '重复组编号', '原路径', '目标路径', 'MD5', '拍摄时间',
            '时间来源', '操作类型',
        ])
        for op in duplicate_operations:
            src, dst, op_type, dt, time_source, md5 = op
            gid = md5_to_group.get(md5, '')
            writer.writerow([
                gid, src, dst, md5,
                dt.isoformat() if isinstance(dt, datetime) else '',
                time_source, op_type,
            ])
    print(f"重复清单已导出: {Path(csv_path).resolve()}")


def _build_monthly_stats(move_operations, duplicate_operations):
    stats = defaultdict(lambda: {
        'moved': 0, 'duplicate': 0, 'exif': 0, 'modify_time': 0,
    })
    for op in move_operations:
        dt, time_source = op[3], op[4]
        key = dt.strftime('%Y-%m')
        stats[key]['moved'] += 1
        if time_source == 'EXIF':
            stats[key]['exif'] += 1
        else:
            stats[key]['modify_time'] += 1
    for op in duplicate_operations:
        dt, time_source = op[3], op[4]
        key = dt.strftime('%Y-%m')
        stats[key]['duplicate'] += 1
        if time_source == 'EXIF':
            stats[key]['exif'] += 1
        else:
            stats[key]['modify_time'] += 1
    return dict(stats)


def generate_markdown_report(source_dir, dest_dir, move_operations, duplicate_operations,
                             skipped, time_source_stats, monthly_stats, health_stats,
                             dry_run, duplicate_mode, incremental):
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    mode = "试运行" if dry_run else "实际执行"
    dup_mode_labels = {
        'list_only': '仅列出',
        'separate': '移至独立目录(保留相对路径)',
        'move': '移至"可能重复"文件夹(按年月)',
    }
    inc_mode = "启用" if incremental else "关闭"

    report_lines = [
        "# 照片整理报告",
        "",
        "## 基本信息",
        "",
        f"- 整理时间: {now}",
        f"- 运行模式: {mode}",
        f"- 增量模式: {inc_mode}",
        f"- 重复处理策略: {dup_mode_labels.get(duplicate_mode, duplicate_mode)}",
        f"- 源目录: `{source_dir}`",
        f"- 目标目录: `{dest_dir}`",
        "",
        "## 相册库健康检查",
        "",
        f"- 目标库重复文件组: {health_stats['duplicate_groups']}",
        f"- 目标库重复文件数(多余): {health_stats['duplicate_files']}",
        f"- 空月份文件夹: {health_stats['empty_month_dirs']}",
        f"- 无EXIF但可用修改时间: {health_stats['no_exif_has_mtime']}",
        f"- 完全无法获取时间: {health_stats['no_time_at_all']}",
        f"- 读取文件失败: {health_stats['read_failed']}",
        "",
    ]

    if health_stats['empty_month_dir_list']:
        report_lines.extend(["### 空月份文件夹列表", ""])
        for d in health_stats['empty_month_dir_list']:
            report_lines.append(f"- `{d}`")
        report_lines.append("")

    if health_stats['read_failed_list']:
        report_lines.extend(["### 读取失败文件列表", ""])
        for fp, err in health_stats['read_failed_list']:
            report_lines.append(f"- `{fp}`: {err}")
        report_lines.append("")

    report_lines.extend([
        "## 本次运行统计",
        "",
        f"- 正常移动文件数: {len(move_operations)}",
        f"- 重复文件数: {len(duplicate_operations)}",
        f"- 跳过文件数: {len(skipped)}",
        f"- EXIF时间来源: {time_source_stats.get('EXIF', 0)}",
        f"- 修改时间来源: {time_source_stats.get('ModifyTime', 0)}",
        "",
    ])

    if monthly_stats:
        report_lines.extend([
            "## 按月份汇总",
            "",
            "| 年月 | 处理数 | 重复数 | EXIF时间 | 修改时间 |",
            "|------|--------|--------|----------|----------|",
        ])
        for ym in sorted(monthly_stats.keys()):
            s = monthly_stats[ym]
            total = s['moved'] + s['duplicate']
            report_lines.append(
                f"| {ym} | {total} | {s['duplicate']} | {s['exif']} | {s['modify_time']} |"
            )
        report_lines.append("")

    if move_operations:
        report_lines.extend([
            "## 正常移动文件",
            "",
            "| 序号 | 原路径 | 目标路径 | 时间来源 | MD5 |",
            "|------|--------|----------|----------|-----|",
        ])
        for idx, op in enumerate(move_operations, 1):
            src, dst, op_type, dt, time_source, md5 = op
            report_lines.append(f"| {idx} | `{src}` | `{dst}` | {time_source} | `{md5[:8]}` |")
        report_lines.append("")

    if duplicate_operations:
        section_title = "重复文件"
        if duplicate_mode == 'list_only':
            section_title += "（仅列出，未移动）"
        elif duplicate_mode == 'separate':
            section_title += "（移至独立目录，保留相对路径）"
        elif duplicate_mode == 'move':
            section_title += "（移至\"可能重复\"文件夹）"

        report_lines.extend([
            f"## {section_title}",
            "",
            "| 序号 | 原路径 | 目标路径 | 时间来源 | MD5 |",
            "|------|--------|----------|----------|-----|",
        ])
        for idx, op in enumerate(duplicate_operations, 1):
            src, dst, op_type, dt, time_source, md5 = op
            report_lines.append(f"| {idx} | `{src}` | `{dst}` | {time_source} | `{md5[:8]}` |")
        report_lines.append("")

    if skipped:
        report_lines.extend([
            "## 跳过文件",
            "",
            "| 序号 | 路径 | 原因 |",
            "|------|------|------|",
        ])
        for idx, (path, reason) in enumerate(skipped, 1):
            report_lines.append(f"| {idx} | `{path}` | {reason} |")
        report_lines.append("")

    return "\n".join(report_lines)


def generate_clean_report(clean_groups, dest_dir, isolation_dir_name):
    dest_path = Path(dest_dir).resolve()
    isolation_path = dest_path / isolation_dir_name

    lines = [
        "# 重复照片清理报告",
        "",
        f"- 清理时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 目标目录: `{dest_path}`",
        f"- 隔离目录: `{isolation_path}`",
        f"- 重复组数: {len(clean_groups)}",
        f"- 保留文件数: {len(clean_groups)}",
        f"- 隔离文件数: {sum(len(g['isolated']) for g in clean_groups)}",
        "",
    ]

    for group in clean_groups:
        lines.extend([
            f"### 重复组 {group['group_id']}",
            "",
            f"- **保留**: `{group['kept']}`",
            f"- MD5: `{group['md5']}`",
            f"- 隔离文件:",
            "",
        ])
        for iso_path in group['isolated']:
            rel = Path(iso_path).relative_to(dest_path)
            target = isolation_path / rel
            lines.append(f"  - `{iso_path}` → `{target}`")
        lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description='按照拍摄时间整理照片到按年份/月份自动分类',
    )
    parser.add_argument('source', nargs='?', default=None, help='源照片目录')
    parser.add_argument('destination', nargs='?', default=None, help='目标整理目录')
    parser.add_argument('--config', default=None, help='配置文件路径 (JSON)')
    parser.add_argument('--init-config', default=None,
                        help='生成默认配置文件到指定路径后退出')
    parser.add_argument('--ignore', nargs='*', default=None,
                        help='忽略的文件夹列表')
    parser.add_argument('--dry-run', action='store_true', default=None,
                        help='试运行模式，不实际移动文件')
    parser.add_argument('--no-dry-run', action='store_true', default=None,
                        help='取消试运行，实际执行移动')
    parser.add_argument('--incremental', action='store_true', default=None,
                        help='启用增量整理模式')
    parser.add_argument('--no-incremental', action='store_true', default=None,
                        help='关闭增量整理模式')
    parser.add_argument('--report', default=None, help='Markdown报告文件名')
    parser.add_argument('--duplicate-csv', default=None,
                        help='重复照片清单CSV导出路径')
    parser.add_argument('--plan-file', default=None,
                        help='计划文件路径 (试运行时保存，正式执行时可用 --execute-plan)')
    parser.add_argument('--execute-plan', default=None,
                        help='按计划文件执行移动操作')
    parser.add_argument('--state-file', default=None,
                        help='增量整理状态文件路径 (JSON)')
    parser.add_argument('--duplicate-mode', choices=['list_only', 'separate', 'move'],
                        default=None,
                        help='重复文件处理策略')
    parser.add_argument('--health-check-only', action='store_true',
                        help='仅执行健康检查，不整理照片')
    parser.add_argument('--clean-duplicates', action='store_true',
                        help='清理目标库重复照片(默认仅生成清单)')
    parser.add_argument('--clean-execute', action='store_true',
                        help='配合 --clean-duplicates 使用，实际执行隔离(否则仅生成清单)')

    args = parser.parse_args()

    if args.init_config:
        save_default_config(args.init_config)
        print(f"默认配置已保存到: {Path(args.init_config).resolve()}")
        sys.exit(0)

    cfg = dict(DEFAULT_CONFIG)
    if args.config:
        loaded = load_config(args.config)
        if loaded:
            cfg.update(loaded)
        else:
            print(f"警告: 配置文件不存在: {args.config}")

    source = args.source if args.source is not None else cfg.get('source', '')
    destination = args.destination if args.destination is not None else cfg.get('destination', '')
    ignore_folders = args.ignore if args.ignore is not None else cfg.get('ignore_folders', [])

    if not destination and source:
        destination = source
        source = ''

    if args.dry_run is True:
        dry_run = True
    elif args.no_dry_run is True:
        dry_run = False
    else:
        dry_run = cfg.get('dry_run', False)

    if args.incremental is True:
        incremental = True
    elif args.no_incremental is True:
        incremental = False
    else:
        incremental = cfg.get('incremental', True)

    report_path = args.report if args.report is not None else cfg.get('report', 'photo_organize_report.md')
    duplicate_csv = args.duplicate_csv if args.duplicate_csv is not None else cfg.get('duplicate_csv', 'duplicates.csv')
    plan_file = args.plan_file if args.plan_file is not None else cfg.get('plan_file', 'plan.json')
    state_file = args.state_file if args.state_file is not None else cfg.get('state_file', 'library_state.json')
    duplicate_mode = args.duplicate_mode if args.duplicate_mode is not None else cfg.get('duplicate_mode', 'separate')
    isolation_dir_name = cfg.get('isolation_dir', '隔离重复')

    if args.execute_plan:
        plan = load_json(args.execute_plan)
        if not plan:
            print(f"错误: 计划文件不存在或格式错误: {args.execute_plan}")
            sys.exit(1)
        dest_dir = plan.get('dest_dir', '')
        if dest_dir:
            state_file_path = Path(dest_dir) / state_file if not Path(state_file).is_absolute() else Path(state_file)
        else:
            state_file_path = Path(state_file)
        if not state_file_path.parent.exists():
            state_file_path.parent.mkdir(parents=True, exist_ok=True)
        print(f"执行计划文件: {args.execute_plan}")
        print(f"目标目录: {dest_dir}")
        print(f"状态文件: {state_file_path}")
        execute_plan(args.execute_plan, incremental=incremental, state_file=str(state_file_path))
        sys.exit(0)

    if not destination:
        print("错误: 未指定目标目录。")
        print("用法: python organize_photos.py [源目录] 目标目录 [选项]")
        print("  整理照片: python organize_photos.py 源目录 目标目录")
        print("  健康检查: python organize_photos.py 目标目录 --health-check-only")
        print("  清理重复: python organize_photos.py 目标目录 --clean-duplicates")
        sys.exit(1)

    if not Path(destination).exists():
        print(f"目标目录不存在: {destination} (将自动创建)")

    if args.health_check_only:
        print(f"相册库健康检查: {destination}")
        print()
        health_stats = check_library_health(destination, ignore_folders)
        print(f"目标库健康检查结果:")
        print(f"  重复文件组: {health_stats['duplicate_groups']}")
        print(f"  重复文件数(多余): {health_stats['duplicate_files']}")
        print(f"  空月份文件夹: {health_stats['empty_month_dirs']}")
        print(f"  无EXIF但可用修改时间: {health_stats['no_exif_has_mtime']}")
        print(f"  完全无法获取时间: {health_stats['no_time_at_all']}")
        print(f"  读取文件失败: {health_stats['read_failed']}")
        if health_stats['empty_month_dir_list']:
            print(f"\n空月份文件夹列表:")
            for d in health_stats['empty_month_dir_list']:
                print(f"  - {d}")
        if health_stats['read_failed_list']:
            print(f"\n读取失败文件:")
            for fp, err in health_stats['read_failed_list']:
                print(f"  - {fp}: {err}")
        sys.exit(0)

    if args.clean_duplicates:
        print(f"清理目标库重复照片: {destination}")
        plan_only = not args.clean_execute
        if plan_only:
            print("模式: 仅生成清理清单 (使用 --clean-execute 实际执行隔离)")
        else:
            print("模式: 实际执行隔离")

        clean_groups = clean_duplicates(
            destination, ignore_folders,
            plan_only=plan_only,
            isolation_dir_name=isolation_dir_name,
            duplicate_csv=duplicate_csv,
        )

        clean_report = generate_clean_report(clean_groups, destination, isolation_dir_name)
        rp = Path(report_path)
        with open(rp, 'w', encoding='utf-8') as f:
            f.write(clean_report)
        print(f"清理报告已保存到: {rp.resolve()}")
        if clean_groups:
            print(f"发现 {len(clean_groups)} 组重复, "
                  f"{sum(len(g['isolated']) for g in clean_groups)} 个文件待隔离")
        else:
            print("未发现重复照片")
        sys.exit(0)

    if not source:
        print("错误: 未指定源目录。")
        print("用法: python organize_photos.py 源目录 目标目录 [选项]")
        print("示例: python organize_photos.py D:\\新照片 E:\\相册库 --dry-run")
        sys.exit(1)

    if not EXIF_AVAILABLE:
        print("警告: 未安装Pillow库，将仅使用文件修改时间。\n可通过 `pip install Pillow` 安装以支持EXIF读取。\n")

    source_dir = Path(source)
    if not source_dir.exists() or not source_dir.is_dir():
        print(f"错误: 源目录不存在或不是目录: {source}")
        sys.exit(1)

    resolved_ignore = [Path(p).resolve() for p in ignore_folders]

    print("开始整理照片...")
    print(f"源目录: {source}")
    print(f"目标目录: {destination}")
    print(f"增量模式: {'启用' if incremental else '关闭'}")
    print(f"重复处理策略: {duplicate_mode}")
    if dry_run:
        print("模式: 试运行 (不会实际移动文件)")
    if resolved_ignore:
        print(f"忽略文件夹: {[str(p) for p in resolved_ignore]}")
    print()

    report, operations, health_stats, skipped = organize_photos(
        source_dir=source,
        dest_dir=destination,
        ignore_folders=resolved_ignore,
        dry_run=dry_run,
        duplicate_mode=duplicate_mode,
        incremental=incremental,
        state_file=state_file,
        duplicate_csv=duplicate_csv,
        plan_file=plan_file,
    )

    rp = Path(report_path)
    with open(rp, 'w', encoding='utf-8') as f:
        f.write(report)

    print(f"\n整理完成!")
    print(f"报告已保存到: {rp.resolve()}")
    moved = sum(1 for o in operations if o[2] in ('kept', 'normal'))
    dups = sum(1 for o in operations if o[2].startswith('duplicate'))
    print(f"移动: {moved} 个 | 重复处理: {dups} 个 | 跳过: {len(skipped)} 个")
    if health_stats['duplicate_groups'] > 0 or health_stats['read_failed'] > 0:
        print(f"健康检查: {health_stats['duplicate_groups']} 组重复, "
              f"{health_stats['no_exif_has_mtime']} 张无EXIF, "
              f"{health_stats['no_time_at_all']} 张无时间, "
              f"{health_stats['read_failed']} 张读取失败")
    else:
        print(f"健康检查: 目标库状态良好")


if __name__ == '__main__':
    main()
