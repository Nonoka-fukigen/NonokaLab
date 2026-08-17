@echo off
rem Re-encode (for non copy-safe containers)
rem Usage: reencode.bat <ffmpeg> <kind:audio|video> <src> <output>
if /I "%~2"=="audio" (
  "%~1" -y -i "%~3" -c:a libmp3lame -b:a 320k "%~4"
) else (
  "%~1" -y -i "%~3" -c:v libx264 -c:a aac -preset fast "%~4"
)