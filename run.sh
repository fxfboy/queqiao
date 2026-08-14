#!/bin/bash
#
# QueQiao (鹊桥) - 快速启动脚本（基于 uv）
# 兼容 Linux, macOS, Windows (Git Bash)
# 用法: ./run.sh encode <输入文件> [选项]
#       ./run.sh decode photo1.jpg [photo2.jpg ...]
#
# 依赖由 pyproject.toml 声明，uv 在首次 `uv sync` 时自动创建 .venv 并同步依赖。

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

# 首次运行前的准备：装好原生 zbar，并用 uv 同步依赖
setup() {
    ensure_uv
    if [ ! -d "$VENV_DIR" ]; then
        install_zbar
        echo "Syncing dependencies with uv..."
        uv sync
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

# 用项目环境运行某个脚本（uv 会自动确保依赖已同步）
run_py() {
    uv run python "$@"
}

# 主流程
main() {
    setup
    setup_env

    case "$1" in
        encode|e)
            shift
            if [ $# -lt 1 ]; then
                echo "用法: ./run.sh encode <输入文件> [选项]"
                echo ""
                echo "选项:"
                echo "  -o FILE              输出 HTML 文件 (默认: output/qr-{chunk_size}-{时间戳}.html)"
                echo "  --cols N             每行二维码数量 (默认: 6)"
                echo "  --qr-size N          二维码尺寸 (默认: 180)"
                echo "  --chunk-size N       每片字节数 (默认: 800)"
                echo "  --backend qr|jab     码制后端 (默认: qr)"
                echo "  --jab-colors 4|8     JAB Code 颜色数 (默认: 8)"
                echo "  --no-open            生成后不自动打开浏览器 (默认: 自动打开)"
                echo ""
                echo "示例:"
                echo "  ./run.sh encode input.txt -o qr.html"
                echo "  ./run.sh encode input.txt -o qr.html --chunk-size 1000 --qr-size 340"
                echo "  # 传输仓库 diff（先生成再编码）:"
                echo "  ./run.sh diff /ext/repo /int/repo -o changes.patch"
                echo "  ./run.sh encode changes.patch -o qr.html"
                exit 1
            fi
            run_py "$SCRIPT_DIR/encoder.py" "$@"
            ;;

        diff)
            shift
            if [ $# -lt 2 ]; then
                echo "用法: ./run.sh diff <基准目录> <目标目录> [选项]"
                echo ""
                echo "选项:"
                echo "  -o FILE              输出 diff 文件 (默认: diff.patch)"
                echo "  --ext .py .js ...    只包含指定扩展名的文件"
                echo "  --no-gitignore       不使用 .gitignore 规则"
                echo "  --ignore pattern ... 额外的忽略模式"
                echo ""
                echo "示例:"
                echo "  ./run.sh diff /ext/repo /int/repo -o changes.patch"
                exit 1
            fi
            run_py "$SCRIPT_DIR/make_diff.py" "$@"
            ;;

        decode|d)
            shift
            if [ $# -lt 1 ]; then
                echo "用法: ./run.sh decode <照片/目录...> [选项]"
                echo ""
                echo "选项:"
                echo "  -o FILE              输出文件 (默认: restored.out)"
                echo "  --backend NAME       识别后端: zxing/pyzbar/jab (默认: zxing)"
                echo "  --debug              显示调试信息"
                exit 1
            fi
            run_py "$SCRIPT_DIR/decoder.py" "$@"
            ;;

        test|t)
            run_py "$SCRIPT_DIR/test_roundtrip.py"
            ;;

        verify|v)
            run_py "$SCRIPT_DIR/verify_full.py"
            ;;

        *)
            echo "QueQiao (鹊桥) - 用二维码跨 air gap 摆渡任意文件"
            echo ""
            echo "两个隔离世界，靠屏幕与相机，逐字节完整相会。"
            echo ""
            echo "用法:"
            echo "  ./run.sh encode <输入文件> [--chunk-size N]  # 把文件编码成二维码 HTML"
            echo "  ./run.sh decode <照片/目录...>         # 从照片还原文件"
            echo "  ./run.sh diff <基准目录> <目标目录>    # (可选) 生成目录 diff 文件"
            echo "  ./run.sh test                          # 运行测试"
            echo "  ./run.sh verify                        # 验证完整往返"
            echo ""
            echo "完整流程 (传输任意文件):"
            echo "  1. 内网: ./run.sh encode input.txt -o qr.html"
            echo "     截图传输可用: ./run.sh encode input.txt -o qr.html --chunk-size 1000 --qr-size 340"
            echo "  2. 浏览器打开 qr.html，全屏显示"
            echo "  3. 手机拍照，传到外网"
            echo "  4. 外网: ./run.sh decode photo.jpg -o restored.out"
            echo ""
            echo "传输仓库 diff (可选):"
            echo "  ./run.sh diff /ext/repo /int/repo -o changes.patch"
            echo "  ./run.sh encode changes.patch -o qr.html"
            ;;
    esac
}

main "$@"
