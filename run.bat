@echo off
REM QueQiao (鹊桥) - Windows 批处理启动脚本（基于 uv）
REM 用法: run.bat encode <输入文件> [选项]
REM       run.bat decode photo1.jpg [photo2.jpg ...]
REM 依赖由 pyproject.toml 声明，uv 在首次 `uv sync` 时自动创建 .venv 并同步依赖。

setlocal enabledelayedexpansion

set SCRIPT_DIR=%~dp0
set VENV_DIR=%SCRIPT_DIR%.venv
cd /d "%SCRIPT_DIR%"

REM 确保 uv 已安装
where uv >nul 2>nul
if not %errorlevel%==0 (
    echo Error: 未找到 uv。请先安装 uv：
    echo   powershell -c "irm https://astral.sh/uv/install.ps1 ^| iex"
    echo   或: pipx install uv
    exit /b 1
)

REM 首次创建 .venv：提示原生依赖并用 uv 同步
if not exist "%VENV_DIR%" (
    echo Note: pyzbar 可能需要 Visual C++ Redistributable
    echo Download from: https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist
    echo.
    echo Syncing dependencies with uv...
    uv sync
)

REM 处理命令
if "%1"=="" goto :help
if "%1"=="encode" goto :encode
if "%1"=="e" goto :encode
if "%1"=="diff" goto :diff
if "%1"=="decode" goto :decode
if "%1"=="d" goto :decode
if "%1"=="test" goto :test
if "%1"=="t" goto :test
if "%1"=="verify" goto :verify
if "%1"=="v" goto :verify
goto :help

:encode
shift
if "%1"=="" (
    echo 用法: run.bat encode ^<输入文件^> [选项]
    echo.
    echo 选项:
    echo   -o FILE              输出 HTML 文件 (默认: output/qr-{chunk_size}-{时间戳}.html^)
    echo   --cols N             每行二维码数量 (默认: 6^)
    echo   --qr-size N          二维码尺寸 (默认: 180^)
    echo   --chunk-size N       每片字节数 (默认: 800^)
    echo   --no-open            生成后不自动打开浏览器 (默认: 自动打开^)
    echo.
    echo 示例:
    echo   run.bat encode input.txt -o qr.html
    echo   run.bat encode input.txt -o qr.html --chunk-size 1000 --qr-size 340
    echo   run.bat diff ext-repo int-repo -o changes.patch
    echo   run.bat encode changes.patch -o qr.html
    exit /b 1
)
uv run python "%SCRIPT_DIR%encoder.py" %*
goto :end

:diff
shift
if "%1"=="" (
    echo 用法: run.bat diff ^<基准目录^> ^<目标目录^> [选项]
    echo.
    echo 选项:
    echo   -o FILE              输出 diff 文件 (默认: diff.patch^)
    echo   --ext .py .js ...    只包含指定扩展名的文件
    echo   --no-gitignore       不使用 .gitignore 规则
    echo   --ignore pattern ... 额外的忽略模式
    exit /b 1
)
uv run python "%SCRIPT_DIR%make_diff.py" %*
goto :end

:decode
shift
if "%1"=="" (
    echo 用法: run.bat decode ^<照片/目录...^> [选项]
    echo.
    echo 选项:
    echo   -o FILE              输出文件 (默认: restored.out^)
    echo   --backend NAME       识别后端: zxing/pyzbar (默认: zxing^)
    echo   --debug              显示调试信息
    exit /b 1
)
uv run python "%SCRIPT_DIR%decoder.py" %*
goto :end

:test
uv run python "%SCRIPT_DIR%test_roundtrip.py"
goto :end

:verify
uv run python "%SCRIPT_DIR%verify_full.py"
goto :end

:help
echo QueQiao (鹊桥) - 用二维码跨 air gap 摆渡任意文件
echo.
echo 两个隔离世界，靠屏幕与相机，逐字节完整相会。
echo.
echo 用法:
echo   run.bat encode ^<输入文件^> [--chunk-size N]  # 把文件编码成二维码 HTML
echo   run.bat decode ^<照片/目录...^>         # 从照片还原文件
echo   run.bat diff ^<基准目录^> ^<目标目录^>   # (可选) 生成目录 diff 文件
echo   run.bat test                          # 运行测试
echo   run.bat verify                        # 验证完整往返
echo.
echo 完整流程 (传输任意文件):
echo   1. 内网: run.bat encode input.txt -o qr.html
echo      截图传输可用: run.bat encode input.txt -o qr.html --chunk-size 1000 --qr-size 340
echo   2. 浏览器打开 qr.html，全屏显示
echo   3. 手机拍照，传到外网
echo   4. 外网: run.bat decode photo.jpg -o restored.out
echo.
echo 传输仓库 diff (可选):
echo   run.bat diff ext-repo int-repo -o changes.patch
echo   run.bat encode changes.patch -o qr.html

:end
endlocal
