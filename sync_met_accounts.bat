@echo off
chcp 65001 >nul
cd /d "%~dp0"
REM Obsidian 미팅록 → DB(met_accounts) 동기화. 작업 스케줄러에서 매일 실행.
call venv\Scripts\python.exe scripts\sync_met_accounts.py >> sync_met_accounts.log 2>&1
