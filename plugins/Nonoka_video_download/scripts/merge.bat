@echo off
rem Merge video + audio fast (-c copy, no re-encode)
rem Usage: merge.bat <ffmpeg> <video> <audio> <output>
"%~1" -y -i "%~2" -i "%~3" -c copy -movflags +faststart "%~4"