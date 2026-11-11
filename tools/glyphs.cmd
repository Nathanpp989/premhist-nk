@echo off
rem glyphs.cmd - run gen_glyph_catalog.py with the bundled Python, from anywhere.
rem
rem PowerShell will not execute a .py file directly (no shebang on Windows), so this finds the
rem interpreter that ships with PremHist QA and forwards whatever arguments you pass:
rem
rem     tools\glyphs.cmd --summary
rem     tools\glyphs.cmd --out build
rem     tools\glyphs.cmd --check build
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
  echo Run it yourself with any Python 3:  python tools\gen_glyph_catalog.py %*
  exit /b 1
)
"%PY%" -X utf8 -B "%HERE%gen_glyph_catalog.py" %*
exit /b %ERRORLEVEL%
