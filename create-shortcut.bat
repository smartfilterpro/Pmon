@echo off
:: Creates a desktop shortcut for Pmon
set SCRIPT_DIR=%~dp0
set SHORTCUT_PATH=%USERPROFILE%\Desktop\Pmon.lnk

powershell -Command "$ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut('%SHORTCUT_PATH%'); $s.TargetPath = '%SCRIPT_DIR%start-pmon.bat'; $s.WorkingDirectory = '%SCRIPT_DIR%'; $s.Description = 'Pmon - Pokemon Card Stock Monitor'; $s.Save()"

echo.
echo Desktop shortcut created: %SHORTCUT_PATH%
echo Double-click "Pmon" on your desktop to start!
echo.
pause
