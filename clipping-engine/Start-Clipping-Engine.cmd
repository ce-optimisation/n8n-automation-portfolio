@echo off
rem ============================================================================
rem  Local AI Video Clipping & Editorial Review Engine - one-click launcher
rem  Double-click to start. Options:
rem    --setup       run the guided setup assistant again, then start
rem    --check       validate only            --doctor   diagnostic report
rem    --no-browser  do not open the dashboard
rem  Set CLIPENGINE_NONINTERACTIVE=1 to never prompt (used by automated tests).
rem ============================================================================
setlocal EnableExtensions EnableDelayedExpansion
title Local Clipping Engine
cd /d "%~dp0"

echo.
echo  Local Clipping Engine - checking requirements...
echo.

set "WANT_SETUP=0"
set "PASS_ARGS="
for %%A in (%*) do (
  if /i "%%~A"=="--setup" (set "WANT_SETUP=1") else (set "PASS_ARGS=!PASS_ARGS! %%A")
)

rem ---- 1. Python 3.10 - 3.13 ---------------------------------------------------
call :find_python
if defined PY goto :python_ok
echo  [!] Python 3.10 - 3.13 was not found on this computer.
if "%CLIPENGINE_NONINTERACTIVE%"=="1" goto :fail
choice /c YN /m "  Install Python 3.12 now for your user account (no administrator rights needed)"
if errorlevel 2 (
  echo      Install Python 3.12 yourself from https://www.python.org/downloads/windows/
  echo      ^(tick "Add python.exe to PATH"^) and start this launcher again.
  goto :fail
)
where winget >nul 2>nul
if not errorlevel 1 (
  winget install --id Python.Python.3.12 -e --scope user --accept-source-agreements --accept-package-agreements
) else (
  echo  winget is not available; downloading the official installer from python.org ...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; [Net.ServicePointManager]::SecurityProtocol='Tls12'; $f=Join-Path $env:TEMP 'python-3.12.10-amd64.exe'; Invoke-WebRequest 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe' -OutFile $f; Start-Process $f -ArgumentList '/quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_test=0' -Wait"
)
call :find_python
if defined PY goto :python_ok
echo  Python was installed but is not visible in this window yet.
echo  Close this window and double-click the launcher again.
if not "%CLIPENGINE_NONINTERACTIVE%"=="1" pause
exit /b 0

:python_ok
echo  [ok] Python: %PY%

rem ---- 2. Virtual environment and packages --------------------------------------
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import sys; sys.exit(0 if (3,10)<=sys.version_info[:2]<=(3,13) else 1)" >nul 2>nul || (
    echo  Existing .venv uses an unsupported Python version; recreating it ...
    rmdir /s /q ".venv"
  )
)
if not exist ".venv\Scripts\python.exe" (
  echo  Creating private Python environment in .venv ...
  %PY% -m venv .venv || goto :fail
)
set "VPY=%~dp0.venv\Scripts\python.exe"
"%VPY%" scripts\ensure_deps.py || goto :fail
echo  [ok] Python packages

rem ---- 3. FFmpeg ---------------------------------------------------------------
call :find_ffmpeg
if not errorlevel 1 goto :ffmpeg_ok
echo  [!] FFmpeg was not found.
if "%CLIPENGINE_NONINTERACTIVE%"=="1" (
  if not "%CLIPENGINE_AUTO_FFMPEG%"=="1" goto :fail
  goto :ffmpeg_portable
)
choice /c YN /m "  Download FFmpeg now (free, no administrator rights needed)"
if errorlevel 2 (
  echo  Alternatively extract an FFmpeg build so that tools\ffmpeg\bin\ffmpeg.exe exists inside this
  echo  folder ^(https://www.gyan.dev/ffmpeg/builds/^), then run the launcher again.
  goto :fail
)
where winget >nul 2>nul
if not errorlevel 1 (
  winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
  call :find_ffmpeg
  if not errorlevel 1 goto :ffmpeg_ok
  echo  winget did not provide a usable FFmpeg; downloading a portable copy instead ...
)
:ffmpeg_portable
"%VPY%" scripts\get_ffmpeg.py || goto :fail
call :find_ffmpeg
if errorlevel 1 goto :fail
:ffmpeg_ok
echo  [ok] FFmpeg

rem ---- 4. Configuration ---------------------------------------------------------
if not exist ".env" (
  copy /y ".env.example" ".env" >nul
  echo  [ok] Created .env from .env.example ^(your local settings; never uploaded^)
)

rem ---- 5. First-time setup assistant ----------------------------------------------
set "DATA_DIR=%~dp0data"
if defined CLIPENGINE_DATA_DIR set "DATA_DIR=%CLIPENGINE_DATA_DIR%"
if "%WANT_SETUP%"=="1" goto :run_setup
if "%PASS_ARGS%"=="" if not exist "%DATA_DIR%\setup_state.json" if not "%CLIPENGINE_NONINTERACTIVE%"=="1" goto :run_setup
goto :start

:run_setup
"%VPY%" -m clipengine --setup
if "%CLIPENGINE_NONINTERACTIVE%"=="1" exit /b %ERRORLEVEL%
echo.
echo  Starting the engine ...

rem ---- 6. Start --------------------------------------------------------------------
:start
echo.
rem --supervise restarts the engine automatically if it ever crashes.
"%VPY%" -m clipengine --supervise %PASS_ARGS%
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :fail_rc
exit /b 0

:find_python
set "PY="
rem Prefer specific versions: the speech-recognition libraries publish wheels for 3.10-3.13.
for %%V in (3.12 3.13 3.11 3.10) do (
  if not defined PY py -%%V -c "import sys" >nul 2>nul && set "PY=py -%%V"
)
if not defined PY (
  python -c "import sys; sys.exit(0 if (3,10)<=sys.version_info[:2]<=(3,13) else 1)" >nul 2>nul && set "PY=python"
)
rem A per-user install made a moment ago is not on PATH in this window yet.
for %%V in (312 313 311 310) do (
  if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" set "PY="%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe""
)
exit /b 0

:find_ffmpeg
if exist "%~dp0tools\ffmpeg\bin\ffmpeg.exe" set "PATH=%~dp0tools\ffmpeg\bin;%PATH%"
if exist "%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe" set "PATH=%LOCALAPPDATA%\Microsoft\WinGet\Links;%PATH%"
where ffmpeg >nul 2>nul || exit /b 1
where ffprobe >nul 2>nul || exit /b 1
exit /b 0

:fail
set "RC=1"
:fail_rc
echo.
echo  The Clipping Engine did not start (code %RC%). Read the messages above.
echo  Diagnose:     Start-Clipping-Engine.cmd --doctor
echo  Detailed log: %~dp0data\logs\clipengine.jsonl
echo  Help: README.md, section "Troubleshooting".
if not "%CLIPENGINE_NONINTERACTIVE%"=="1" pause
exit /b %RC%
