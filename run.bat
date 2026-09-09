@echo off
REM QueQiao (鹊桥) - Windows 批处理启动脚本（基于 uv）
REM 用法: run.bat <子命令> [参数...]
REM   各子命令的完整选项用 `run.bat <子命令> --help` 查看。
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

REM 首次创建 .venv：提示原生依赖并用 uv 同步（含 pyzbar extra）
if not exist "%VENV_DIR%" (
    echo Note: pyzbar 可能需要 Visual C++ Redistributable
    echo Download from: https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist
    echo.
    echo Syncing dependencies with uv...
    uv sync --extra pyzbar
)

REM 处理命令
if "%1"=="" goto :help
if "%1"=="encode" goto :run
if "%1"=="e" goto :run
if "%1"=="diff" goto :run
if "%1"=="decode" goto :run
if "%1"=="d" goto :run
if "%1"=="stream" goto :run
if "%1"=="s" goto :run
if "%1"=="receive" goto :run
if "%1"=="r" goto :run
if "%1"=="test" goto :test
if "%1"=="t" goto :test
if "%1"=="verify" goto :verify
if "%1"=="v" goto :verify
goto :help

:run
uv run queqiao %*
goto :end

:test
uv run python tests\test_roundtrip.py
goto :end

:verify
uv run python tests\verify_full.py
goto :end

:help
echo QueQiao (鹊桥) - 用二维码跨 air gap 摆渡任意文件
echo.
echo 两个隔离世界，靠屏幕与相机，逐字节完整相会。
echo.
echo 用法:
echo   run.bat encode ^<输入文件^> [--chunk-size N]  # 把文件编码成二维码 HTML
echo   run.bat decode ^<照片/目录...^>         # 从照片还原文件
echo   run.bat stream ^<输入文件^>             # 循环播放喷泉码窗口
echo   run.bat receive [--screen] [-o FILE]  # 圈选屏幕区域接收
echo   run.bat diff ^<基准目录^> ^<目标目录^>   # (可选) 生成目录 diff 文件
echo   run.bat test                          # 运行测试
echo   run.bat verify                        # 验证完整往返
echo.
echo 各子命令的完整选项: run.bat ^<子命令^> --help
echo 安装为独立命令 (不依赖本仓库): pipx install queqiao[pyzbar]

:end
endlocal
