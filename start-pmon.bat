@echo off
title Pmon - Pokemon Card Monitor
cd /d "%~dp0"

echo ============================================
echo   Pmon - Pokemon Card Stock Monitor
echo   Close this window to stop monitoring
echo ============================================
echo.

python -m pmon.cli run --my-browser

pause
