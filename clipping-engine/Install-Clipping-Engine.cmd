@echo off
rem ============================================================================
rem  Clipping Engine - one-click installer / updater for Windows.
rem  Double-click this file. No administrator rights are needed.
rem  It installs (or updates) the engine in %LOCALAPPDATA%\ClippingEngine, keeps your
rem  settings and recordings, creates a Desktop shortcut and starts the setup assistant.
rem  This file also works on its own: saved anywhere, it downloads the project from GitHub.
rem ============================================================================
setlocal EnableExtensions
title Clipping Engine installer
set "PS1=%~dp0scripts\install.ps1"
if exist "%PS1%" goto :run
set "PS1=%TEMP%\clipengine_install.ps1"
echo  Downloading the installer script ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; [Net.ServicePointManager]::SecurityProtocol='Tls12'; Invoke-WebRequest 'https://raw.githubusercontent.com/ce-optimisation/n8n-automation-portfolio/claude/optimistic-noether-k52qjf/clipping-engine/scripts/install.ps1' -OutFile $env:PS1 -UseBasicParsing"
if errorlevel 1 (
  echo.
  echo  Could not download the installer script. Check the internet connection and try again.
  pause
  exit /b 1
)
:run
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" if not "%CLIPENGINE_NONINTERACTIVE%"=="1" pause
exit /b %RC%
