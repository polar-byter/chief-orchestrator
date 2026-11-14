@echo off
setlocal
pushd "%~dp0.." || exit /b 1
call kit\python.cmd -B -m kit.project_docs init
set "docs_exit=%errorlevel%"
if not "%docs_exit%"=="0" pause
popd
exit /b %docs_exit%
