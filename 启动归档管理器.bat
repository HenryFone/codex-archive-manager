@echo off
rem Launch the archive manager in the background (no console window).
rem Prefers pythonw; falls back to py/python.
setlocal
set "HERE=%~dp0"
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "%HERE%codex_archive_manager_gui.py"
    goto :eof
)
where py >nul 2>nul
if %errorlevel%==0 (
    start "" py -3w "%HERE%codex_archive_manager_gui.py"
    goto :eof
)
where python >nul 2>nul
if %errorlevel%==0 (
    start "" /b python "%HERE%codex_archive_manager_gui.py"
    goto :eof
)
echo Python 3.8+ not found in PATH. Install it first.
pause