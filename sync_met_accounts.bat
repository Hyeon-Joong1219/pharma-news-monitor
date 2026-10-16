@echo off
cd /d "%~dp0"
REM Sync Obsidian meeting notes -> DB (met_accounts). Run daily by Task Scheduler.
set PYTHONIOENCODING=utf-8
call venv\Scripts\python.exe scripts\sync_met_accounts.py >> sync_met_accounts.log 2>&1
