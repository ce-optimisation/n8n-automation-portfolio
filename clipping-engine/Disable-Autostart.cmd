@echo off
rem Stop starting the Clipping Engine automatically when you sign in to Windows.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (echo Run Start-Clipping-Engine.cmd once first. & pause & exit /b 1)
".venv\Scripts\python.exe" -m clipengine --autostart off
pause
