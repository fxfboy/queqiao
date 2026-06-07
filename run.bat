@echo off
REM QueQiao (鹊桥) - Windows 批处理启动脚本
REM 用法: run.bat encode ext-repo int-repo [选项]
REM       run.bat decode photo1.jpg [photo2.jpg ...]

setlocal enabledelayedexpansion

set SCRIPT_DIR=%~dp0
set VENV_DIR=%SCRIPT_DIR%.venv

REM 获取 Python 命令
where python3 >nul 2>nul
if %errorlevel%==0 (
    set PYTHON=python3
) else (
    where python >nul 2>nul
    if %errorlevel%==0 (
        set PYTHON=python
    ) else (
        echo Error: Python not found
        exit /b 1
    )
)

REM 确保虚拟环境存在
if not exist "%VENV_DIR%\Scripts\activate" (
    echo Creating virtual environment...
    %PYTHON% -m venv "%VENV_DIR%"
    call "%VENV_DIR%\Scripts\activate.bat"
    
    echo Installing dependencies...
    pip install qrcode[pil] Pillow opencv-python-headless pyzbar
    
    REM 提示安装 Visual C++ Redistributable
    echo.
    echo Note: pyzbar may need Visual C++ Redistributable
    echo Download from: https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist
    echo.
) else (
    call "%VENV_DIR%\Scripts\activate.bat"
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
    echo   -o FILE              输出 HTML 文件 (默认: qr_diff.html^)
    echo   --cols N             每行二维码数量 (默认: 6^)
    echo   --qr-size N          二维码尺寸 (默认: 180^)
    echo   --chunk-size N       每片字节数 (默认: 400^)
    echo.
    echo 示例:
    echo   run.bat encode input.txt -o qr.html
    echo   run.bat diff ext-repo int-repo -o changes.patch
    echo   run.bat encode changes.patch -o qr.html
    exit /b 1
)
%PYTHON% "%SCRIPT_DIR%encoder.py" %*
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
%PYTHON% "%SCRIPT_DIR%make_diff.py" %*
goto :end

:decode
shift
if "%1"=="" (
    echo 用法: run.bat decode ^<照片1^> [照片2 ...] [选项]
    echo.
    echo 选项:
    echo   -o FILE     输出文件 (默认: restored.out^)
    echo   --debug     显示调试信息
    exit /b 1
)
%PYTHON% "%SCRIPT_DIR%decoder.py" %*
goto :end

:test
%PYTHON% "%SCRIPT_DIR%test_roundtrip.py"
goto :end

:verify
%PYTHON% "%SCRIPT_DIR%verify_full.py"
goto :end

:help
echo QueQiao (鹊桥) - 用二维码跨 air gap 摆渡任意文件
echo.
echo 两个隔离世界，靠屏幕与相机，逐字节完整相会。
echo.
echo 用法:
echo   run.bat encode ^<输入文件^>             # 把文件编码成二维码 HTML
echo   run.bat decode ^<照片...^>              # 从照片还原文件
echo   run.bat diff ^<基准目录^> ^<目标目录^>   # (可选) 生成目录 diff 文件
echo   run.bat test                          # 运行测试
echo   run.bat verify                        # 验证完整往返
echo.
echo 完整流程 (传输任意文件):
echo   1. 内网: run.bat encode input.txt -o qr.html
echo   2. 浏览器打开 qr.html，全屏显示
echo   3. 手机拍照，传到外网
echo   4. 外网: run.bat decode photo.jpg -o restored.out
echo.
echo 传输仓库 diff (可选):
echo   run.bat diff ext-repo int-repo -o changes.patch
echo   run.bat encode changes.patch -o qr.html

:end
endlocal
