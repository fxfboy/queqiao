#!/bin/bash
#
# QueQiao (鹊桥) - 快速启动脚本
# 兼容 Linux, macOS, Windows (Git Bash)
# 用法: ./run.sh encode <输入文件> [选项]
#       ./run.sh decode photo1.jpg [photo2.jpg ...]
#

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

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

# 获取 Python 命令
get_python() {
    if command -v python3 &> /dev/null; then
        echo "python3"
    elif command -v python &> /dev/null; then
        echo "python"
    else
        echo "Error: Python not found" >&2
        exit 1
    fi
}

PYTHON=$(get_python)

# 获取虚拟环境激活脚本路径
get_activate_script() {
    if [ "$OS" = "windows" ]; then
        echo "$VENV_DIR/Scripts/activate"
    else
        echo "$VENV_DIR/bin/activate"
    fi
}

ACTIVATE_SCRIPT=$(get_activate_script)

get_venv_python() {
    if [ "$OS" = "windows" ]; then
        echo "$VENV_DIR/Scripts/python"
    else
        echo "$VENV_DIR/bin/python"
    fi
}

VENV_PYTHON=$(get_venv_python)

# 确保虚拟环境存在
setup_venv() {
    if [ ! -d "$VENV_DIR" ]; then
        echo "Creating virtual environment..."
        $PYTHON -m venv "$VENV_DIR"
        source "$ACTIVATE_SCRIPT"
        
        # 安装依赖
        echo "Installing dependencies..."
        pip install qrcode[pil] Pillow opencv-python-headless
        
        # 安装 pyzbar（需要额外处理）
        install_pyzbar
    else
        source "$ACTIVATE_SCRIPT"
    fi

    PYTHON="$VENV_PYTHON"
}

# 安装 pyzbar 及其依赖
install_pyzbar() {
    case "$OS" in
        macos)
            # macOS 需要 brew 安装 zbar
            if ! command -v brew &> /dev/null; then
                echo "Warning: Homebrew not found. pyzbar may not work."
                echo "Install with: /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
            else
                brew install zbar 2>/dev/null || true
            fi
            pip install pyzbar
            ;;
        linux)
            # Linux 需要 apt 安装 libzbar
            if command -v apt-get &> /dev/null; then
                sudo apt-get update && sudo apt-get install -y libzbar0 2>/dev/null || true
            elif command -v yum &> /dev/null; then
                sudo yum install -y zbar 2>/dev/null || true
            fi
            pip install pyzbar
            ;;
        windows)
            # Windows: pyzbar 需要 Visual C++ Redistributable
            echo "Note: On Windows, pyzbar may need Visual C++ Redistributable"
            echo "Download from: https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist"
            pip install pyzbar
            ;;
    esac
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
    setup_venv
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
            $PYTHON "$SCRIPT_DIR/encoder.py" "$@"
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
            $PYTHON "$SCRIPT_DIR/make_diff.py" "$@"
            ;;
        
        decode|d)
            shift
            if [ $# -lt 1 ]; then
                echo "用法: ./run.sh decode <照片/目录...> [选项]"
                echo ""
                echo "选项:"
                echo "  -o FILE              输出文件 (默认: restored.out)"
                echo "  --backend NAME       识别后端: pyzbar/opencv (默认: pyzbar)"
                echo "  --debug              显示调试信息"
                exit 1
            fi
            $PYTHON "$SCRIPT_DIR/decoder.py" "$@"
            ;;
        
        test|t)
            $PYTHON "$SCRIPT_DIR/test_roundtrip.py"
            ;;
        
        verify|v)
            $PYTHON "$SCRIPT_DIR/verify_full.py"
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
