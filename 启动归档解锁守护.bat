@echo off
rem Start the lock watchdog in the background (minimized window).
setlocal
where python >nul 2>nul
if %errorlevel%==0 (
    start "" /min python "%~dp0lock_guard.py" --watch
    goto :eof
)
where py >nul 2>nul
if %errorlevel%==0 (
    start "" /min py -3 "%~dp0lock_guard.py" --watch
    goto :eof
)
echo Python 3.8+ not found in PATH. Install it first.
pause