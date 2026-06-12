#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import os
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


PHOTO_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.tif', '.heic', '.heif', '.webp', '.raw', '.cr2', '.nef', '.arw', '.dng', '.orf', '.rw2'}


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


def generate_unique_path(dest_dir, photo_dt, source_file):
    year = photo_dt.strftime('%Y')
    month = photo_dt.strftime('%m')
    target_dir = Path(dest_dir) / year / month
    target_path = target_dir / source_file.name
    if target_path.exists():
        stem = source_file.stem
        suffix = source_file.suffix
        counter = 1
        while True:
            new_name = f"{stem}_{counter}{suffix}"
            target_path = target_dir / new_name
            if not target_path.exists():
                break
            counter += 1
    return target_path


def organize_photos(source_dir, dest_dir, ignore_folders, dry_run=False):
    source_dir = Path(source_dir).resolve()
    dest_dir = Path(dest_dir).resolve()
    duplicate_dir = dest_dir / '可能重复'

    photos = find_photos(source_dir, ignore_folders)
    
    md5_map = defaultdict(list)
    move_operations = []
    duplicate_operations = []
    skipped = []
    time_source_stats = {'EXIF': 0, 'ModifyTime': 0}

    print(f"发现照片总数: {len(photos)}")

    for photo_path in photos:
        try:
            photo_dt, time_source = get_photo_datetime(photo_path)
            time_source_stats[time_source] += 1
            md5 = calculate_md5(photo_path)
            md5_map[md5].append((photo_path, photo_dt))
        except Exception as e:
            skipped.append((str(photo_path), f"处理失败: {e}"))

    for md5, file_list in md5_map.items():
        if len(file_list) > 1:
            file_list.sort(key=lambda x: x[1])
            kept = file_list[0][0]
            target_path = generate_unique_path(dest_dir, file_list[0][1], kept)
            move_operations.append((str(kept), str(target_path), 'kept'))
            for dup_path, dup_dt in file_list[1:]:
                dup_target = generate_unique_path(duplicate_dir, dup_dt, dup_path)
                duplicate_operations.append((str(dup_path), str(dup_target), 'duplicate'))
        else:
            photo_path, photo_dt = file_list[0]
            target_path = generate_unique_path(dest_dir, photo_dt, photo_path)
            move_operations.append((str(photo_path), str(target_path), 'normal'))

    all_operations = move_operations + duplicate_operations

    if not dry_run:
        for src, dst, op_type in all_operations:
            dst_path = Path(dst)
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(src, dst)

    report = generate_markdown_report(source_dir, dest_dir, move_operations, duplicate_operations, skipped, time_source_stats, dry_run)

    return report, all_operations


def generate_markdown_report(source_dir, dest_dir, move_operations, duplicate_operations, skipped, time_source_stats, dry_run):
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    mode = "试运行" if dry_run else "实际执行"
    report_lines = [
        f"# 照片整理报告",
        f"",
        f"## 基本信息",
        f"",
        f"- 整理时间: {now}",
        f"- 运行模式: {mode}",
        f"- 源目录: `{source_dir}`",
        f"- 目标目录: `{dest_dir}`",
        f"",
        f"## 统计信息",
        f"",
        f"- 正常移动文件数: {len(move_operations)}",
        f"- 重复文件数: {len(duplicate_operations)}",
        f"- 跳过文件数: {len(skipped)}",
        f"- EXIF时间来源: {time_source_stats.get('EXIF', 0)}",
        f"- 修改时间来源: {time_source_stats.get('ModifyTime', 0)}",
        f"",
    ]

    if move_operations:
        report_lines.extend([
            f"## 正常移动文件",
            f"",
            f"| 序号 | 原路径 | 目标路径 |",
            f"|------|--------|----------|",
        ])
        for idx, (src, dst, op_type) in enumerate(move_operations, 1):
            report_lines.append(f"| {idx} | `{src}` | `{dst}` |")
        report_lines.append("")

    if duplicate_operations:
        report_lines.extend([
            f"## 重复文件（移动到\"可能重复\"文件夹)",
            f"",
            f"| 序号 | 原路径 | 目标路径 |",
            f"|------|--------|----------|",
        ])
        for idx, (src, dst, op_type) in enumerate(duplicate_operations, 1):
            report_lines.append(f"| {idx} | `{src}` | `{dst}` |")
        report_lines.append("")

    if skipped:
        report_lines.extend([
            f"## 跳过文件",
            f"",
            f"| 序号 | 路径 | 原因 |",
            f"|------|------|------|",
        ])
        for idx, (path, reason) in enumerate(skipped, 1):
            report_lines.append(f"| {idx} | `{path}` | {reason} |")
        report_lines.append("")

    return "\n".join(report_lines)


def main():
    parser = argparse.ArgumentParser(description='按照拍摄时间整理照片到按年份/月份自动分类')
    parser.add_argument('source', help='源照片目录')
    parser.add_argument('destination', help='目标整理目录')
    parser.add_argument('--ignore', nargs='*', default=[], help='忽略的文件夹列表')
    parser.add_argument('--dry-run', action='store_true', help='试运行模式，不实际移动文件')
    parser.add_argument('--report', default='photo_organize_report.md', help='Markdown报告文件名')

    args = parser.parse_args()

    if not EXIF_AVAILABLE:
        print("警告: 未安装Pillow库，将仅使用文件修改时间。\n可通过 `pip install Pillow` 安装以支持EXIF读取。")

    source_dir = Path(args.source)
    if not source_dir.exists() or not source_dir.is_dir():
        print(f"错误: 源目录不存在或不是目录: {args.source}")
        sys.exit(1)

    ignore_folders = [Path(p).resolve() for p in args.ignore]

    print(f"开始整理照片...")
    print(f"源目录: {args.source}")
    print(f"目标目录: {args.destination}")
    if args.dry_run:
        print("模式: 试运行 (不会实际移动文件)")
    if ignore_folders:
        print(f"忽略文件夹: {[str(p) for p in ignore_folders]}")
    print()

    report, operations = organize_photos(
        source_dir=args.source,
        dest_dir=args.destination,
        ignore_folders=ignore_folders,
        dry_run=args.dry_run
    )

    report_path = Path(args.report)
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report)

    print(f"\n整理完成!")
    print(f"报告已保存到: {report_path.resolve()}")
    print(f"共处理 {len(operations)} 个文件")


if __name__ == '__main__':
    main()
