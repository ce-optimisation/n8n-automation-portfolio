@echo off
rem Remove the files of ONE failed live capture (default: item 20 - America First Ep. 1780, seg_001.ts and recording.mp4).
rem It verifies identity, that no recorder is writing, and that no recording was registered from it; you must type DISCARD 20.
rem Usage:  Recover-FailedRecording.cmd [ITEM_ID] [file1,file2]
set ITEM=%1
if "%ITEM%"=="" set ITEM=20
set NAMES=%2
if "%NAMES%"=="" set NAMES=seg_001.ts,recording.mp4
call "%~dp0Start-Clipping-Engine.cmd" --discard-live %ITEM% --names %NAMES%
pause
