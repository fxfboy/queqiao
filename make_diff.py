#!/usr/bin/env python3
"""
make_diff - optional helper for QueQiao (鹊桥).
Compare two directories and write a unified diff text file that can then be
fed to encoder.py. This is NOT part of the QR transfer itself.

用法:
    python make_diff.py /path/to/external-repo /path/to/internal-repo -o changes.patch
    python encoder.py changes.patch -o qr.html
"""

import os
import difflib
import argparse
import fnmatch
from pathlib import Path


def parse_gitignore(gitignore_path):
    """解析 .gitignore 文件，返回忽略模式列表"""
    patterns = []
    if not os.path.exists(gitignore_path):
        return patterns

    with open(gitignore_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            # 跳过空行和注释
            if not line or line.startswith('#'):
                continue
            # 处理否定模式（以 ! 开头）
            if line.startswith('!'):
                continue  # 暂不支持否定模式
            patterns.append(line)

    return patterns


def match_gitignore_pattern(relpath, pattern):
    """匹配单个 gitignore 模式"""
    # 移除开头的 /
    pattern = pattern.lstrip('/')

    # 如果模式以 / 结尾，只匹配目录
    if pattern.endswith('/'):
        pattern = pattern[:-1]
        # 检查 relpath 是否是这个目录或在其下
        if relpath == pattern or relpath.startswith(pattern + '/'):
            return True
        return False

    # 使用 fnmatch 进行匹配
    # 匹配文件名
    if fnmatch.fnmatch(os.path.basename(relpath), pattern):
        return True

    # 匹配完整路径
    if fnmatch.fnmatch(relpath, pattern):
        return True

    # 匹配路径中的任意部分
    parts = Path(relpath).parts
    for i in range(len(parts)):
        subpath = '/'.join(parts[i:])
        if fnmatch.fnmatch(subpath, pattern):
            return True

    return False


def should_ignore(relpath, ignore_patterns=None, gitignore_patterns=None, include_extensions=None):
    """
    判断文件是否应该忽略

    Args:
        relpath: 相对路径
        ignore_patterns: 自定义忽略模式列表
        gitignore_patterns: .gitignore 解析出的模式
        include_extensions: 只包含这些扩展名（如 ['.py', '.js']），为空则包含所有
    """
    # 默认忽略
    default_ignores = {
        '.git', '.svn', '.hg', '__pycache__', 'node_modules',
        '.DS_Store', '*.pyc', '*.pyo', '*.so', '*.dylib',
        '.venv', 'venv', 'env',
    }

    patterns = ignore_patterns or set()
    all_patterns = default_ignores | patterns

    # 检查扩展名白名单
    if include_extensions:
        ext = os.path.splitext(relpath)[1].lower()
        if ext not in include_extensions:
            return True

    # 检查默认忽略和自定义忽略
    parts = Path(relpath).parts
    for part in parts:
        if part in all_patterns:
            return True
        for pat in all_patterns:
            if pat.startswith('*') and part.endswith(pat[1:]):
                return True

    # 检查 .gitignore 规则
    if gitignore_patterns:
        for pattern in gitignore_patterns:
            if match_gitignore_pattern(relpath, pattern):
                return True

    return False


def read_file_lines(filepath):
    """安全读取文件行"""
    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            return f.readlines()
    except Exception:
        return None


def compare_directories(dir1, dir2, ignore_binary=True, use_gitignore=True, include_extensions=None, ignore_patterns=None):
    """
    比较两个目录，生成 unified diff
    dir1: 基准目录（外网仓库）
    dir2: 目标目录（内网仓库）

    Args:
        dir1: 基准目录
        dir2: 目标目录
        ignore_binary: 是否忽略二进制文件
        use_gitignore: 是否使用 .gitignore 规则
        include_extensions: 只包含这些扩展名（如 ['.py', '.js']）
        ignore_patterns: 额外的忽略模式
    """
    dir1 = Path(dir1).resolve()
    dir2 = Path(dir2).resolve()

    # 解析 .gitignore
    gitignore_patterns = []
    if use_gitignore:
        for d in [dir1, dir2]:
            gitignore_path = d / '.gitignore'
            patterns = parse_gitignore(gitignore_path)
            gitignore_patterns.extend(patterns)
        gitignore_patterns = list(set(gitignore_patterns))  # 去重

    # 收集所有文件
    def collect_files(base_dir):
        files = set()
        for root, dirs, filenames in os.walk(base_dir):
            # 过滤隐藏目录
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            for f in filenames:
                if f.startswith('.'):
                    continue
                full = os.path.join(root, f)
                rel = os.path.relpath(full, base_dir)
                if not should_ignore(rel,
                                    ignore_patterns=ignore_patterns,
                                    gitignore_patterns=gitignore_patterns,
                                    include_extensions=include_extensions):
                    files.add(rel)
        return files

    files1 = collect_files(dir1)
    files2 = collect_files(dir2)

    all_files = sorted(files1 | files2)

    diffs = []
    stats = {'added': 0, 'deleted': 0, 'modified': 0, 'unchanged': 0, 'binary': 0}

    for f in all_files:
        path1 = dir1 / f
        path2 = dir2 / f

        in1 = f in files1
        in2 = f in files2

        if in1 and not in2:
            # 文件被删除
            lines = read_file_lines(path1)
            if lines is not None:
                diff = difflib.unified_diff(
                    lines, [],
                    fromfile=f'a/{f}',
                    tofile=f'/dev/null',
                    lineterm='\n'
                )
                diff_str = '\n'.join(diff)
                if diff_str:
                    diffs.append(diff_str)
                    stats['deleted'] += 1

        elif not in1 and in2:
            # 新增文件
            lines = read_file_lines(path2)
            if lines is not None:
                diff = difflib.unified_diff(
                    [], lines,
                    fromfile='/dev/null',
                    tofile=f'b/{f}',
                    lineterm='\n'
                )
                diff_str = '\n'.join(diff)
                if diff_str:
                    diffs.append(diff_str)
                    stats['added'] += 1

        else:
            # 两边都有，比较内容
            lines1 = read_file_lines(path1)
            lines2 = read_file_lines(path2)

            if lines1 is None or lines2 is None:
                stats['binary'] += 1
                continue

            if lines1 != lines2:
                diff = difflib.unified_diff(
                    lines1, lines2,
                    fromfile=f'a/{f}',
                    tofile=f'b/{f}',
                    lineterm='\n'
                )
                diff_str = '\n'.join(diff)
                if diff_str:
                    diffs.append(diff_str)
                    stats['modified'] += 1
            else:
                stats['unchanged'] += 1

    result = '\n'.join(diffs)
    return result, stats


def main():
    parser = argparse.ArgumentParser(
        description='Compare two directories and write a unified diff (optional QueQiao helper for encoder.py)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python make_diff.py ~/external-repo ~/internal-repo -o changes.patch
  python make_diff.py /ext /int --ext .py .ts --ignore "test_*" -o changes.patch
  # 然后传输:
  python encoder.py changes.patch -o qr.html
"""
    )
    parser.add_argument('dir1', help='基准目录 (外网仓库)')
    parser.add_argument('dir2', help='目标目录 (内网仓库)')
    parser.add_argument('-o', '--output', default='diff.patch',
                        help='输出 diff 文件 (默认: diff.patch)')
    filter_group = parser.add_argument_group('文件过滤')
    filter_group.add_argument('--ext', nargs='+', metavar='.py',
                              help='只包含指定扩展名的文件 (如: --ext .py .js .ts)')
    filter_group.add_argument('--no-gitignore', action='store_true',
                              help='不使用 .gitignore 规则')
    filter_group.add_argument('--ignore', nargs='+', metavar='pattern',
                              help='额外的忽略模式 (如: --ignore "*.log" "test_*")')
    args = parser.parse_args()

    include_extensions = None
    if args.ext:
        include_extensions = [e if e.startswith('.') else f'.{e}' for e in args.ext]
        include_extensions = [e.lower() for e in include_extensions]
    ignore_patterns = set(args.ignore) if args.ignore else None

    print("=" * 60)
    print("  make_diff - directory comparison")
    print("=" * 60)
    print(f"  Base (dir1):   {args.dir1}")
    print(f"  Target (dir2): {args.dir2}")
    print()

    diff_text, stats = compare_directories(
        args.dir1, args.dir2,
        use_gitignore=not args.no_gitignore,
        include_extensions=include_extensions,
        ignore_patterns=ignore_patterns,
    )

    print(f"  Added:    {stats['added']} files")
    print(f"  Deleted:  {stats['deleted']} files")
    print(f"  Modified: {stats['modified']} files")
    print(f"  Binary:   {stats['binary']} files (skipped)")
    print(f"  Same:     {stats['unchanged']} files")
    print()

    if not diff_text:
        print("✅ No differences found!")
        return

    with open(args.output, 'w', encoding='utf-8') as f:
        f.write(diff_text)
    print(f"  Diff size: {len(diff_text):,} bytes")
    print(f"  Saved:     {args.output}")


if __name__ == '__main__':
    main()
