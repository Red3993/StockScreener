@echo off
REM One-time setup on Windows: installs the one Python package needed and schedules a daily run at 6:15pm.
set DIR=%~dp0
py -m pip install --user --quiet requests
schtasks /Create /F /SC DAILY /ST 18:15 /TN "StockScreener Senate trades" /TR "py \"%DIR%senate_trades.py\""
echo Scheduled daily at 6:15pm (Task Scheduler: "StockScreener Senate trades").
echo Run it once now with:  py "%DIR%senate_trades.py"
