@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" app.py
) else (
  python app.py
)

echo.
echo Server stopped. Press any key to close.
pause >nul
