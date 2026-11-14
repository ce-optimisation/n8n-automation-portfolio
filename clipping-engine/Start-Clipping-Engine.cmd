@echo off
rem ============================================================================
rem  Local AI Video Clipping & Editorial Review Engine - one-click launcher
rem  Double-click to start. Options:  --check (validate only)  --no-browser
rem  Set CLIPENGINE_NONINTERACTIVE=1 to never prompt (used by automated tests).
rem ============================================================================
setlocal EnableExtensions EnableDelayedExpansion
title Local Clipping Engine
cd /d "%~dp0"

echo.
echo  Local Clipping Engine - checking requirements...
echo.

rem ---- 1. Python 3.10+ ---------------------------------------------------------
set "PY="
rem Prefer specific versions: the speech-recognition libraries publish wheels for 3.10-3.13.
for %%V in (3.12 3.13 3.11 3.10) do (
  if not defined PY py -%%V -c "import sys" >nul 2>nul && set "PY=py -%%V"
)
if not defined PY (
  python -c "import sys; sys.exit(0 if (3,10)<=sys.version_info[:2]<=(3,13) else 1)" >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo  [!] Python 3.10 - 3.13 was not found.
  echo      Install Python 3.12 from https://www.python.org/downloads/windows/
  echo      ^(tick "Add python.exe to PATH"^), or let this launcher install it with winget.
  if "%CLIPENGINE_NONINTERACTIVE%"=="1" goto :fail
  choice /c YN /m "  Install Python 3.12 now using winget (no administrator rights needed)"
  if errorlevel 2 goto :fail
  winget install --id Python.Python.3.12 -e --scope user --accept-source-agreements --accept-package-agreements
  if errorlevel 1 goto :fail
  echo  Python installed. Close this window and double-click the launcher again.
  pause
  exit /b 0
)
echo  [ok] Python: %PY%

rem ---- 2. Virtual environment & packages ----------------------------------------
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
if exist "%~dp0tools\ffmpeg\bin\ffmpeg.exe" set "PATH=%~dp0tools\ffmpeg\bin;%PATH%"
if exist "%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe" set "PATH=%LOCALAPPDATA%\Microsoft\WinGet\Links;%PATH%"
where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo  [!] FFmpeg was not found.
  if "%CLIPENGINE_NONINTERACTIVE%"=="1" goto :fail
  choice /c YN /m "  Install FFmpeg now using winget (Gyan.FFmpeg, no administrator rights needed)"
  if errorlevel 2 (
    echo  Alternatively download a build from https://www.gyan.dev/ffmpeg/builds/ and extract it so that
    echo  tools\ffmpeg\bin\ffmpeg.exe exists inside this folder, then run the launcher again.
    goto :fail
  )
  winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
  set "PATH=%LOCALAPPDATA%\Microsoft\WinGet\Links;%PATH%"
  where ffmpeg >nul 2>nul || (echo  FFmpeg installed - close this window and start the launcher again. & pause & exit /b 0)
)
echo  [ok] FFmpeg

rem ---- 4. Configuration ------------------------------------------------------------
if not exist ".env" (
  copy /y ".env.example" ".env" >nul
  echo  [ok] Created .env from .env.example ^(edit it to change settings^)
)

rem ---- 5. Start ----------------------------------------------------------------------
echo.
rem --supervise restarts the engine automatically if it ever crashes.
"%VPY%" -m clipengine --supervise %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" goto :fail_rc
exit /b 0

:fail
set "RC=1"
:fail_rc
echo.
echo  The Clipping Engine did not start (code %RC%). Read the messages above.
echo  Detailed log: %~dp0data\logs\clipengine.jsonl
echo  Help: README.md, section "Troubleshooting".
if not "%CLIPENGINE_NONINTERACTIVE%"=="1" pause
exit /b %RC%
