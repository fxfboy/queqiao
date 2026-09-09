#!/bin/bash
#
# QueQiao (鹊桥) - 快速启动脚本（基于 uv）
# 兼容 Linux, macOS, Windows (Git Bash)
# 用法: ./run.sh <子命令> [参数...]
#   各子命令的完整选项用 `./run.sh <子命令> --help` 查看。
#
# 依赖由 pyproject.toml 声明，uv 在首次 `uv sync` 时自动创建 .venv 并同步依赖；
# 本地运行的是仓库源码（uv 以 editable 方式安装 src/queqiao）。

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
cd "$SCRIPT_DIR"

# 检测操作系统
detect_os() {
    case "$(uname -s)" in
        Linux*)     echo "linux";;
        Darwin*)    echo "macos";;
        MINGW*|MSYS*|CYGWIN*)  echo "windows";;
        *)          echo "unknown";;
    esac
}

OS=$(detect_os)

# 确保 uv 已安装
ensure_uv() {
    if ! command -v uv &> /dev/null; then
        echo "Error: 未找到 uv。请先安装 uv：" >&2
        echo "  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
        echo "  或: brew install uv / pipx install uv" >&2
        exit 1
    fi
}

# 安装 pyzbar 运行时所需的原生 zbar 库（仅在首次创建 .venv 时尝试）
install_zbar() {
    case "$OS" in
        macos)
            if ! command -v brew &> /dev/null; then
                echo "Warning: 未找到 Homebrew，pyzbar 可能无法工作。"
                echo "安装: /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
            else
                brew install zbar 2>/dev/null || true
            fi
            ;;
        linux)
            if command -v apt-get &> /dev/null; then
                sudo apt-get update && sudo apt-get install -y libzbar0 2>/dev/null || true
            elif command -v yum &> /dev/null; then
                sudo yum install -y zbar 2>/dev/null || true
            fi
            ;;
        windows)
            echo "Note: Windows 上 pyzbar 可能需要 Visual C++ Redistributable"
            echo "下载: https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist"
            ;;
    esac
}

# 首次运行前的准备：装好原生 zbar，并用 uv 同步依赖（含 pyzbar extra）
setup() {
    ensure_uv
    if [ ! -d "$VENV_DIR" ]; then
        install_zbar
        echo "Syncing dependencies with uv..."
        uv sync --extra pyzbar
    fi
}

# 设置环境变量
setup_env() {
    case "$OS" in
        macos)
            # macOS Homebrew zbar 路径
            if [ -d "/opt/homebrew/lib" ]; then
                export DYLD_LIBRARY_PATH="/opt/homebrew/lib:$DYLD_LIBRARY_PATH"
            fi
            ;;
        linux)
            # 通常不需要额外设置
            ;;
        windows)
            # Windows 通常不需要设置
            ;;
    esac
}

# 主流程
main() {
    setup
    setup_env

    case "$1" in
        encode|e|decode|d|diff|stream|s|receive|r)
            uv run queqiao "$@"
            ;;

        test|t)
            uv run python tests/test_roundtrip.py
            ;;

        verify|v)
            uv run python tests/verify_full.py
            ;;

        *)
            echo "QueQiao (鹊桥) - 用二维码跨 air gap 摆渡任意文件"
            echo ""
            echo "两个隔离世界，靠屏幕与相机，逐字节完整相会。"
            echo ""
            echo "用法:"
            echo "  ./run.sh encode <输入文件> [--chunk-size N]  # 把文件编码成二维码 HTML"
            echo "  ./run.sh decode <照片/目录...>         # 从照片还原文件"
            echo "  ./run.sh stream <输入文件>             # 循环播放喷泉码窗口"
            echo "  ./run.sh receive [--screen] [-o FILE]  # 圈选屏幕区域接收"
            echo "  ./run.sh diff <基准目录> <目标目录>    # (可选) 生成目录 diff 文件"
            echo "  ./run.sh test                          # 运行字节往返测试"
            echo "  ./run.sh verify                        # 验证完整往返 (真实二维码)"
            echo ""
            echo "各子命令的完整选项: ./run.sh <子命令> --help"
            echo "安装为独立命令 (不依赖本仓库): pipx install queqiao[pyzbar]"
            ;;
    esac
}

main "$@"
