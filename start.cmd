@echo off
setlocal
cd /d "%~dp0"
call "%~dp0kit\python.cmd" -m kit.run_agent --project-default %*
exit /b %errorlevel%
