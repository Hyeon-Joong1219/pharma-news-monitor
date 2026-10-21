@echo off
REM Weekly local DB backup (Task Scheduler "PharmaNewsMonitor").
REM News fetch runs on GitHub Actions (fetch.yml) every 3h - not here, to avoid doubling Groq usage.
REM Keep this file ASCII + CRLF: UTF-8 Korean/LF made cmd misparse it (exit 255, nothing ran since 2026-07).
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo [%date% %time%] backup start >> run_all.log
call venv\Scripts\python.exe scripts\backup_db.py >> run_all.log 2>&1
echo [%date% %time%] done (exit %errorlevel%) >> run_all.log
