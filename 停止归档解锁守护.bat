@echo off
rem Stop the lock watchdog.
setlocal
where python >nul 2>nul
if %errorlevel%==0 (
    python "%~dp0lock_guard.py" --stop
    goto :end
)
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 "%~dp0lock_guard.py" --stop
    goto :end
)
echo Python 3.8+ not found in PATH.
:end
pause