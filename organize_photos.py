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
    "state_file": "library_state.json",
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


def load_state(state_path):
    p = Path(state_path)
    if not p.exists():
        return {'photos': {}}
    with open(p, 'r', encoding='utf-8') as f:
        data = json.load(f)
    if 'photos' not in data:
        data['photos'] = {}
    return data


def save_state(state_path, state):
    p = Path(state_path)
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


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


def scan_dest_photos(dest_dir, ignore_folders):
    photos = []
    dest_path = Path(dest_dir).resolve()
    duplicate_dir = dest_path / '可能重复'
    extra_ignore = [duplicate_dir] + list(ignore_folders)
    for root, dirs, files in os.walk(dest_path):
        dirs[:] = [d for d in dirs if not is_ignored(Path(root) / d, extra_ignore)]
        if is_ignored(root, extra_ignore):
            continue
        for file in files:
            file_path = Path(root) / file
            if file_path.suffix.lower() in PHOTO_EXTENSIONS:
                photos.append(file_path)
    return photos


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
    extra_ignore = [duplicate_dir] + list(ignore_folders)

    md5_map = defaultdict(list)
    no_time_count = 0
    empty_month_dirs = []

    for root, dirs, files in os.walk(dest_path):
        current_root = Path(root).resolve()
        rel_parts = current_root.relative_to(dest_path).parts
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
                dt, ts = get_photo_datetime(file_path)
                if ts == 'ModifyTime':
                    no_time_count += 1
                md5 = calculate_md5(file_path)
                md5_map[md5].append(str(file_path))
            except Exception:
                no_time_count += 1

    dup_count = sum(1 for v in md5_map.values() if len(v) > 1)
    dup_file_count = sum(len(v) for v in md5_map.values() if len(v) > 1) - dup_count

    return {
        'duplicate_groups': dup_count,
        'duplicate_files': dup_file_count,
        'empty_month_dirs': len(empty_month_dirs),
        'empty_month_dir_list': empty_month_dirs,
        'no_exif_time_count': no_time_count,
    }


def organize_photos(source_dir, dest_dir, ignore_folders, dry_run=False,
                    duplicate_mode='separate', incremental=True, state_file=None,
                    duplicate_csv=None):
    source_dir = Path(source_dir).resolve()
    dest_dir = Path(dest_dir).resolve()
    duplicate_dir = dest_dir / '可能重复'

    state = load_state(state_file) if incremental and state_file else {'photos': {}}
    known_md5s = {entry['md5']: entry for entry in state['photos'].values()}

    allocator = TargetPathAllocator(dest_dir)

    photos = find_photos(source_dir, ignore_folders)

    md5_map = defaultdict(list)
    move_operations = []
    duplicate_operations = []
    skipped = []
    time_source_stats = {'EXIF': 0, 'ModifyTime': 0}
    photo_info_map = {}
    source_path_to_md5 = {}

    print(f"发现照片总数: {len(photos)}")

    for photo_path in photos:
        try:
            photo_dt, time_source = get_photo_datetime(photo_path)
            md5 = calculate_md5(photo_path)
            source_path_to_md5[str(photo_path)] = md5
            md5_map[md5].append(photo_path)
            photo_info_map[photo_path] = {
                'datetime': photo_dt,
                'time_source': time_source,
                'md5': md5,
            }
            time_source_stats[time_source] += 1
        except Exception as e:
            skipped.append((str(photo_path), f"处理失败: {e}"))

    skip_reasons = []
    for md5, file_list in md5_map.items():
        file_list.sort(key=lambda p: photo_info_map[p]['datetime'])
        is_dup_group = len(file_list) > 1

        kept = file_list[0]
        kept_info = photo_info_map[kept]

        if incremental and md5 in known_md5s:
            existing_entry = known_md5s[md5]
            existing_target = existing_entry.get('target_path', '')
            existing_source = existing_entry.get('source_path', '')
            if str(kept) == existing_source:
                skip_reasons.append((str(kept), '增量模式：已整理且内容未变化', md5, existing_target))
            else:
                skip_reasons.append((str(kept), f'增量模式：照片已存在于目标库 (原路径: {existing_source})', md5, existing_target))
            for dup_path in file_list[1:]:
                skip_reasons.append((str(dup_path), '增量模式：与已入库照片重复', md5, existing_target))
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

    for src, reason, md5, target in skip_reasons:
        skipped.append((src, reason))

    all_operations = move_operations + duplicate_operations

    if not dry_run:
        for op in all_operations:
            src, dst, op_type, dt, time_source, md5 = op
            if op_type == 'duplicate_listed':
                continue
            dst_path = Path(dst)
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(src, dst)
            if op_type in ('kept', 'normal') and incremental and state_file:
                state['photos'][md5] = {
                    'md5': md5,
                    'source_path': src,
                    'target_path': dst,
                    'datetime': dt.isoformat() if isinstance(dt, datetime) else '',
                    'time_source': time_source,
                    'added_at': datetime.now().isoformat(),
                }

        if incremental and state_file:
            save_state(state_file, state)

    monthly_stats = _build_monthly_stats(move_operations, duplicate_operations)
    health_stats = check_library_health(dest_dir, ignore_folders)

    report = generate_markdown_report(
        source_dir, dest_dir, move_operations, duplicate_operations,
        skipped, time_source_stats, monthly_stats, health_stats,
        dry_run, duplicate_mode, incremental,
    )

    if duplicate_csv and duplicate_operations:
        export_duplicate_csv(duplicate_csv, duplicate_operations, md5_map)

    return report, all_operations, health_stats


def export_duplicate_csv(csv_path, duplicate_operations, md5_map):
    md5_to_group = {}
    group_id = 1
    for md5, file_list in md5_map.items():
        if len(file_list) > 1:
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
            group_id = md5_to_group.get(md5, '')
            writer.writerow([
                group_id, src, dst, md5,
                dt.isoformat() if isinstance(dt, datetime) else '',
                time_source, op_type,
            ])
    print(f"重复清单已导出: {Path(csv_path).resolve()}")


def _build_monthly_stats(move_operations, duplicate_operations):
    stats = defaultdict(lambda: {
        'moved': 0, 'duplicate': 0, 'exif': 0, 'modify_time': 0,
    })
    for op in move_operations:
        src, dst, op_type, dt, time_source = op[0], op[1], op[2], op[3], op[4]
        key = dt.strftime('%Y-%m')
        stats[key]['moved'] += 1
        if time_source == 'EXIF':
            stats[key]['exif'] += 1
        else:
            stats[key]['modify_time'] += 1
    for op in duplicate_operations:
        src, dst, op_type, dt, time_source = op[0], op[1], op[2], op[3], op[4]
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
        f"- 目标库重复文件数: {health_stats['duplicate_files']}",
        f"- 空月份文件夹: {health_stats['empty_month_dirs']}",
        f"- 无EXIF时间(使用修改时间): {health_stats['no_exif_time_count']}",
        "",
    ]

    if health_stats['empty_month_dir_list']:
        report_lines.extend([
            "### 空月份文件夹列表",
            "",
        ])
        for d in health_stats['empty_month_dir_list']:
            report_lines.append(f"- `{d}`")
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
            short_md5 = md5[:8]
            report_lines.append(f"| {idx} | `{src}` | `{dst}` | {time_source} | `{short_md5}` |")
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
            short_md5 = md5[:8]
            report_lines.append(f"| {idx} | `{src}` | `{dst}` | {time_source} | `{short_md5}` |")
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
                        help='启用增量整理模式，跳过已处理且内容未变的照片')
    parser.add_argument('--no-incremental', action='store_true', default=None,
                        help='关闭增量整理模式')
    parser.add_argument('--report', default=None,
                        help='Markdown报告文件名')
    parser.add_argument('--duplicate-csv', default=None,
                        help='重复照片清单CSV导出路径')
    parser.add_argument('--state-file', default=None,
                        help='增量整理状态文件路径 (JSON)')
    parser.add_argument('--duplicate-mode', choices=['list_only', 'separate', 'move'],
                        default=None,
                        help='重复文件处理策略: list_only=仅列出, separate=移至独立目录保留相对路径, move=移至可能重复文件夹')
    parser.add_argument('--health-check-only', action='store_true',
                        help='仅执行健康检查，不整理照片')

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
    state_file = args.state_file if args.state_file is not None else cfg.get('state_file', 'library_state.json')
    duplicate_mode = args.duplicate_mode if args.duplicate_mode is not None else cfg.get('duplicate_mode', 'separate')

    if not destination:
        print("错误: 未指定目标目录。请通过命令行参数或配置文件提供 destination。")
        sys.exit(1)

    if args.health_check_only:
        print("仅执行健康检查...")
        health_stats = check_library_health(destination, ignore_folders)
        print(f"\n目标库健康检查结果:")
        print(f"  重复文件组: {health_stats['duplicate_groups']}")
        print(f"  重复文件数: {health_stats['duplicate_files']}")
        print(f"  空月份文件夹: {health_stats['empty_month_dirs']}")
        print(f"  无EXIF时间(使用修改时间): {health_stats['no_exif_time_count']}")
        if health_stats['empty_month_dir_list']:
            print(f"\n空月份文件夹列表:")
            for d in health_stats['empty_month_dir_list']:
                print(f"  - {d}")
        sys.exit(0)

    if not source:
        print("错误: 未指定源目录。请通过命令行参数或配置文件提供 source。")
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

    report, operations, health_stats = organize_photos(
        source_dir=source,
        dest_dir=destination,
        ignore_folders=resolved_ignore,
        dry_run=dry_run,
        duplicate_mode=duplicate_mode,
        incremental=incremental,
        state_file=state_file,
        duplicate_csv=duplicate_csv,
    )

    rp = Path(report_path)
    with open(rp, 'w', encoding='utf-8') as f:
        f.write(report)

    print(f"\n整理完成!")
    print(f"报告已保存到: {rp.resolve()}")
    print(f"共处理 {len(operations)} 个文件")
    print(f"目标库健康检查: {health_stats['duplicate_groups']} 组重复, {health_stats['empty_month_dirs']} 个空文件夹")


if __name__ == '__main__':
    main()
