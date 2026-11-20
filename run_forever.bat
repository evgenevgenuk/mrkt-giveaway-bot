@echo off
cd /d "%~dp0"
:loop
python bot.py >> console.log 2>&1
timeout /t 30 /nobreak >nul
goto loop
