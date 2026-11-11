@echo off
rem describe-report.cmd - run describe-report.py with the bundled Python, from anywhere.
rem
rem PowerShell will not execute a .py file directly on Windows, so this finds the interpreter that
rem ships with PremHist QA and forwards whatever arguments you pass:
rem
rem     tools\describe-report.cmd
rem     tools\describe-report.cmd --since 2026-11-01
rem     tools\describe-report.cmd --csv report
rem     tools\describe-report.cmd --engine displays --full
rem
rem %~dp0 is this file's own folder, so it works from the install root or from inside tools\.

setlocal
set "HERE=%~dp0"
set "PY=%HERE%..\python-describe\python.exe"
if not exist "%PY%" set "PY=%HERE%..\python-mcp\python.exe"
if not exist "%PY%" (
  echo Could not find the bundled Python next to this package.
  echo Looked for ..\python-describe\python.exe and ..\python-mcp\python.exe relative to
  echo   %HERE%
  echo Run it yourself with any Python 3:  python tools\describe-report.py %*
  exit /b 1
)
"%PY%" -X utf8 -B "%HERE%describe-report.py" %*
exit /b %ERRORLEVEL%
