@echo off
rem Enter YouTube / Twitch / Kick monitoring credentials with HIDDEN prompts (never typed into chat), then test them live.
rem Only the named keys in %LOCALAPPDATA%\ClippingEngine\.env are written; values are never displayed or logged.
call "%~dp0Start-Clipping-Engine.cmd" --set-credentials
pause
