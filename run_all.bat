@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo [%date% %time%] === 뉴스 수집 시작 ===

REM 1. 뉴스 수집 + 스코어링
call venv\Scripts\python.exe fetch.py

REM 2. 언론사 관리 비활성화 (DB 비어있을 때 전체 삭제 방지)
REM for /f %%d in ('powershell -command "(Get-Date).DayOfWeek"') do set DOW=%%d
REM if "%DOW%"=="Monday" (
REM     echo [%date% %time%] === 언론사 자동 관리 실행 ===
REM     call venv\Scripts\python.exe source_manager.py
REM )

echo [%date% %time%] === 완료 ===
