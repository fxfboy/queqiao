#!/usr/bin/env python3
"""Tests for make_diff.compare_directories."""
import os
import sys
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from make_diff import compare_directories


def create_test_repos():
    tmpdir = tempfile.mkdtemp(prefix='qr_diff_test_')
    repo1 = os.path.join(tmpdir, 'repo1')
    repo2 = os.path.join(tmpdir, 'repo2')
    os.makedirs(repo1)
    os.makedirs(repo2)
    for repo in (repo1, repo2):                       # identical file
        with open(os.path.join(repo, 'same.txt'), 'w') as f:
            f.write("identical\n")
    with open(os.path.join(repo1, 'mod.txt'), 'w') as f:   # modified
        f.write("line one\nline two\n")
    with open(os.path.join(repo2, 'mod.txt'), 'w') as f:
        f.write("line one\nline two changed\nline three\n")
    with open(os.path.join(repo2, 'added.txt'), 'w') as f:  # added in repo2
        f.write("brand new\n")
    with open(os.path.join(repo1, 'gone.txt'), 'w') as f:   # deleted (repo1 only)
        f.write("will be removed\n")
    return tmpdir, repo1, repo2


def test_compare():
    tmpdir, repo1, repo2 = create_test_repos()
    try:
        diff_text, stats = compare_directories(repo1, repo2)
        assert stats['added'] >= 1, "expected an added file"
        assert stats['deleted'] >= 1, "expected a deleted file"
        assert stats['modified'] >= 1, "expected a modified file"
        assert len(diff_text) > 0, "diff should not be empty"
        print("  ✅ test_compare PASSED")
    finally:
        shutil.rmtree(tmpdir)


if __name__ == '__main__':
    test_compare()
    print("\n✅ All make_diff tests passed!")
