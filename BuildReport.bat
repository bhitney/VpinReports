@echo off
cd /d "%~dp0"
REM Builds VpinStatsReport.html (Play History, Performance Stats, and AI Recommendations tabs).
REM Settings (database path, emulator IDs, history days, logs folder) live in report_config.py.
REM Any arguments are passed through, e.g.: BuildReport.bat --emus 4,6,10 --days 180
if not exist .venv\Scripts\activate.bat (
  echo Creating virtual environment...
  py -m venv .venv || python -m venv .venv
)
call .venv\Scripts\activate.bat
python -c "import pandas, PIL" 2>nul || pip install -q -r requirements.txt
python build_report.py %*
if errorlevel 1 exit /b %errorlevel%
if "%CI%"=="" if exist VpinStatsReport.html start "" VpinStatsReport.html
