@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
set "PYTHONUTF8=1"
pushd "%~dp0"
if errorlevel 1 (
    echo [错误] 无法访问项目目录。
    exit /b 1
)
title mud.ren 社区知识库 API

if exist ".venv\Scripts\python.exe" goto existing_venv
py -3.12 -c "import sys" >nul 2>&1
if not errorlevel 1 goto python312
py -3 -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if not errorlevel 1 goto python_launcher
python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if not errorlevel 1 goto python_path

echo [错误] 需要 Python 3.10 及以上版本，推荐 Python 3.12。
echo 请安装 Python 并启用 Windows 启动器或添加到 PATH，然后重试。
set "MUDREN_EXIT_CODE=1"
goto finished

:existing_venv
".venv\Scripts\python.exe" "scripts\start_server.py" %*
set "MUDREN_EXIT_CODE=%ERRORLEVEL%"
goto finished

:python312
py -3.12 "scripts\start_server.py" %*
set "MUDREN_EXIT_CODE=%ERRORLEVEL%"
goto finished

:python_launcher
py -3 "scripts\start_server.py" %*
set "MUDREN_EXIT_CODE=%ERRORLEVEL%"
goto finished

:python_path
python "scripts\start_server.py" %*
set "MUDREN_EXIT_CODE=%ERRORLEVEL%"

:finished
popd
if /i "%~1"=="--no-pause" goto return
if /i "%~1"=="--help" goto return
if /i "%~1"=="-h" goto return
pause

:return
exit /b %MUDREN_EXIT_CODE%
