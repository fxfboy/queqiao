#!/bin/bash
#
# QueQiao (鹊桥) - 一键发版脚本
# 用法: ./release.sh <版本号> [--dry-run] [-y]
#   例: ./release.sh 0.3.0
#
# 做的事情:
#   1. 同步更新三处版本号: pyproject.toml / src/queqiao/__init__.py / uv.lock
#   2. git commit "chore(release): vX.Y.Z"
#   3. git tag vX.Y.Z（先推分支再推 tag）
#   4. tag 推上去后 CI（.github/workflows/publish.yml）自动校验 + 构建 + 发布 PyPI
#
# 注意: 仓库里还有 v2 / v3 两个历史 tag（无 v 前缀格式），本脚本统一用 vX.Y.Z。
#       CI 只认 vX.Y.Z 触发。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYPROJECT="pyproject.toml"
INIT="src/queqiao/__init__.py"
UVLOCK="uv.lock"

DRY_RUN=0
ASSUME_YES=0
for arg in "$@"; do
    case "$arg" in
        --dry-run|-n) DRY_RUN=1 ;;
        -y|--yes)     ASSUME_YES=1 ;;
        -h|--help)    sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)            VERSION_ARG="$arg" ;;
    esac
done

err()  { echo "❌ $*" >&2; exit 1; }
info() { echo "▸ $*"; }

# ---------- 参数解析 ----------
[ -n "${VERSION_ARG:-}" ] || { sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }

VERSION="${VERSION_ARG#v}"   # 容忍 v 前缀
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([-+.][0-9A-Za-z.-]+)?$ ]] \
    || err "版本号格式不合法: $1（期望 X.Y.Z，如 0.3.0）"
TAG="v$VERSION"

# ---------- 前置检查 ----------
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || err "不在 git 仓库内"

BRANCH=$(git rev-parse --abbrev-ref HEAD)
[ "$BRANCH" = "main" ] || echo "⚠️  当前分支是 ${BRANCH}（通常发版应在 main）"

OLD_VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' "$PYPROJECT" | head -1)
[ -n "$OLD_VERSION" ] || err "无法从 $PYPROJECT 读取当前版本号"
[ "$VERSION" != "$OLD_VERSION" ] || err "版本号没变（当前已是 ${OLD_VERSION}）"
[ "$(printf '%s\n' "$OLD_VERSION" "$VERSION" | sort -V | head -1)" = "$OLD_VERSION" ] \
    || err "新版本 $VERSION 不大于当前版本 $OLD_VERSION"

if git rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
    err "本地已存在 tag $TAG"
fi
if git ls-remote --tags origin "refs/tags/$TAG" | grep -q .; then
    err "远端已存在 tag $TAG"
fi

if [ "$DRY_RUN" = 1 ]; then
    info "dry-run：只预览改动，不写文件、不动 git"
else
    # 其余未提交改动不会被卷进发版 commit（只 add 三个版本文件），仅提示
    OTHER=$(git status --porcelain | grep -v '^??' || true)
    [ -z "$OTHER" ] || { echo "⚠️  以下已跟踪文件有未提交改动，不会包含在发版 commit 里："; echo "$OTHER"; }
fi

# ---------- 版本号替换（纯文本 sed，三处必须一致）----------
replace() {  # replace <file> <pattern> <replacement>
    sed -i.bak "$2" "$1" && rm -f "$1.bak"
}
# uv.lock 里自身包的版本块：name = "queqiao" 的下一行（BSD/GNU sed 的花括号
# 单行写法不兼容，改用 awk，三平台行为一致）
update_uvlock() {
    awk -v v="$VERSION" '
        /^name = "queqiao"$/ { print; getline; sub(/^version = ".*"/, "version = \"" v "\""); print; next }
        { print }
    ' "$UVLOCK" > "$UVLOCK.tmp" && mv "$UVLOCK.tmp" "$UVLOCK"
}
check_uvlock() {
    awk '/^name = "queqiao"$/ { getline; if (sub(/^version = "/, "")) { sub(/"$/, ""); print } }' "$UVLOCK" | head -1
}
NEW_PYPROJECT="s/^version = \".*\"/version = \"$VERSION\"/"
NEW_INIT="s/^__version__ = \".*\"/__version__ = \"$VERSION\"/"

info "版本号: $OLD_VERSION → $VERSION"
if [ "$DRY_RUN" = 1 ]; then
    echo "--- 预览 $PYPROJECT ---";    sed "$NEW_PYPROJECT" "$PYPROJECT" | grep '^version'
    echo "--- 预览 $INIT ---";        sed "$NEW_INIT" "$INIT" | grep '^__version__'
    echo "--- 预览 $UVLOCK (queqiao 块) ---"
    awk -v v="$VERSION" '/^name = "queqiao"$/ { print; getline; sub(/^version = ".*"/, "version = \"" v "\""); print; next } { print }' "$UVLOCK" \
        | grep -A 1 '^name = "queqiao"' | head -2
    echo
    info "dry-run 结束，未做任何修改。确认无误后执行: ./release.sh $VERSION"
    exit 0
fi

replace "$PYPROJECT" "$NEW_PYPROJECT"
replace "$INIT" "$NEW_INIT"
[ -f "$UVLOCK" ] && update_uvlock

# 回读校验（CI check-version 也会再查一遍，本地先拦住）
NEW_P=$(sed -n 's/^version = "\(.*\)"/\1/p' "$PYPROJECT" | head -1)
NEW_I=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$INIT" | head -1)
NEW_L=$( [ -f "$UVLOCK" ] && check_uvlock || true )
[ "$NEW_P" = "$VERSION" ] && [ "$NEW_I" = "$VERSION" ] \
    || err "版本号替换失败: pyproject=$NEW_P __init__=$NEW_I"
if [ -n "${NEW_L:-}" ] && [ "$NEW_L" != "$VERSION" ]; then
    echo "⚠️  uv.lock 中 queqiao 版本未更新为 ${VERSION}（当前 ${NEW_L}），可忽略，CI 不校验 uv.lock"
fi

# ---------- commit → push 分支 → tag → push tag ----------
COMMIT_MSG="chore(release): $TAG"
info "git commit: $COMMIT_MSG"
git add "$PYPROJECT" "$INIT"
[ -f "$UVLOCK" ] && git add "$UVLOCK"
git commit -m "$COMMIT_MSG"

info "git push origin $BRANCH"
PUSH_PROXY=""
if ! git push origin "$BRANCH"; then
    echo "⚠️  直连失败，尝试走本机代理 127.0.0.1:7897 重试…"
    PUSH_PROXY="1"
    https_proxy=http://127.0.0.1:7897 git push origin "$BRANCH"
fi

info "git tag -a $TAG"
git tag -a "$TAG" -m "$COMMIT_MSG"

info "git push origin $TAG"
if [ -n "$PUSH_PROXY" ]; then
    https_proxy=http://127.0.0.1:7897 git push origin "$TAG"
else
    git push origin "$TAG"
fi

REPO_SLUG=$(git remote get-url origin | sed -E 's#.*github\.com[:/]##; s#\.git$##')
echo
echo "✅ $TAG 已推送，CI 自动发布中:"
echo "   https://github.com/$REPO_SLUG/actions/workflows/publish.yml"
if command -v gh >/dev/null 2>&1; then
    echo "   观察进度: gh run watch --exit-status \$(gh run list --workflow=publish.yml -L 1 --json databaseId -q '.[0].databaseId')"
fi
