@echo off
rem Read-only storage audit: where the disk space went and what could be recovered. Changes nothing, deletes nothing.
call "%~dp0Start-Clipping-Engine.cmd" --storage-audit
pause
