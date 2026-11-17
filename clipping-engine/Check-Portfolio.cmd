@echo off
rem Portfolio setup check: versions, schema, credentials (yes/no only), identity verification, discovery health.
rem Safe to run repeatedly. Never prints secrets, never edits .env, needs no administrator rights.
call "%~dp0Start-Clipping-Engine.cmd" --portfolio-check
pause
