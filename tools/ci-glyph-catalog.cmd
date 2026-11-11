@echo off
rem ci-glyph-catalog.cmd - the Windows twin of ci-glyph-catalog.sh. Same two checks, same exit
rem codes, for a Windows build agent or for running it by hand before you commit:
rem
rem     tools\ci-glyph-catalog.cmd
rem     tools\ci-glyph-catalog.cmd ph-types
rem
rem Uses the bundled Python when it is there (an install), else whatever `python` is on PATH (a
rem checkout). Exit 0 = clean, 1 = drift.

setlocal
set "HERE=%~dp0"
set "GEN=%HERE%gen_glyph_catalog.py"
set "PY=%HERE%..\python-describe\python.exe"
if not exist "%PY%" set "PY=python"
set "RC=0"

echo == glyph catalog: internal audit
"%PY%" -I "%GEN%" --audit || set "RC=1"

set "DIR=%~1"
if "%DIR%"=="" if exist "ph-types\" set "DIR=ph-types"
if "%DIR%"=="" (
  echo == glyph catalog: no generated files to compare ^(skipped^)
) else (
  echo == glyph catalog: generated files in %DIR%
  "%PY%" -I "%GEN%" --check "%DIR%" || set "RC=1"
)

if not "%RC%"=="0" (
  echo.
  echo The glyph catalog has drifted. Regenerate with:
  echo     python tools\gen_glyph_catalog.py --out %DIR%
  echo and commit the result - do not hand-edit the generated files.
)
exit /b %RC%
