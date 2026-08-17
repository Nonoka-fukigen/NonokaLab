@echo off
rem Remux to container (no re-encode)
rem Usage: remux.bat <ffmpeg> <src> <output>
"%~1" -y -i "%~2" -c copy "%~3"