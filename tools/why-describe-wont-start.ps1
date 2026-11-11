# why-describe-wont-start.ps1 - the Describe service shows FAILED. Find out why, in one run.
#
#     powershell -ExecutionPolicy Bypass -File .\tools\why-describe-wont-start.ps1
#
# start-premhist-qa.ps1 launches Describe hidden with its output redirected to logs\describe.log,
# so a Python traceback at startup never reaches the screen - all the tester sees is
# "did not become healthy in time". This walks the launch one layer at a time, in the same order
# Python does, and stops at the first thing that is actually broken:
#
#   1. the bundled interpreter runs at all
#   2. fastapi and uvicorn import in THAT interpreter (not whatever is on PATH)
#   3. describe_app.py imports (this is where a bad copy or a syntax error shows up)
#   4. the two authoring engines import
#   5. the port is free
#   6. uvicorn actually serves /health, run in the FOREGROUND so errors are visible
#
# Read-only apart from step 6, which starts the service for a few seconds and then stops it.
# Run 'Stop PremHist' first if PremHist is running, or step 5 will report the port busy.
#
# This file is ASCII only, so it parses in Windows PowerShell 5.1 and PowerShell 7 alike.

param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "PremHist-QA"),
    [int]$Seconds = 12
)

$ErrorActionPreference = "Continue"
$fail = $null

function Step([string]$n) { Write-Host ""; Write-Host $n -ForegroundColor Cyan }
function Ok([string]$m)   { Write-Host "   OK    $m" -ForegroundColor Green }
function Bad([string]$m)  { Write-Host "   BAD   $m" -ForegroundColor Red }
function Note([string]$m) { Write-Host "         $m" -ForegroundColor DarkGray }

Write-Host ""
Write-Host "Why will the Describe service not start?" -ForegroundColor Cyan
Write-Host "  install: $InstallRoot"

$py = Join-Path $InstallRoot "python-describe\python.exe"
$describe = Join-Path $InstallRoot "describe"
$cfgPath = Join-Path $InstallRoot "qa-config.json"
$port = 4060
if (Test-Path $cfgPath) {
    try { $port = [int]((Get-Content -Raw $cfgPath | ConvertFrom-Json).ports.describe) } catch { }
}
Write-Host "  python : $py"
Write-Host "  app dir: $describe"
Write-Host "  port   : $port"

# ---- 0. the files exist --------------------------------------------------------------------
Step "0. Files"
foreach ($p in @($py, (Join-Path $describe "describe_app.py"),
                 (Join-Path $describe "routes_calcs.py"), (Join-Path $describe "routes_displays.py"))) {
    if (Test-Path $p) {
        $len = (Get-Item $p).Length
        if ($len -eq 0) { Bad "$p is ZERO BYTES - a failed copy"; if (-not $fail) { $fail = "empty file" } }
        else { Ok ("{0}  ({1:N0} bytes)" -f (Split-Path -Leaf $p), $len) }
    } else {
        Bad "missing: $p"
        if (-not $fail) { $fail = "missing file" }
    }
}
$orch = Join-Path $describe "orchestrator"
if (Test-Path $orch) {
    $zero = @(Get-ChildItem (Join-Path $orch "*.py") | Where-Object { $_.Length -eq 0 })
    if ($zero) {
        foreach ($z in $zero) { Bad "$($z.Name) is ZERO BYTES" }
        if (-not $fail) { $fail = "empty module" }
    } else {
        Ok ("orchestrator: {0} modules, smallest {1:N0} bytes" -f `
            (@(Get-ChildItem (Join-Path $orch "*.py")).Count),
            ((Get-ChildItem (Join-Path $orch "*.py") | Measure-Object Length -Minimum).Minimum))
    }
} else {
    Bad "no orchestrator package at $orch"
}

# ---- 1. the interpreter --------------------------------------------------------------------
Step "1. The bundled interpreter"
if (-not (Test-Path $py)) {
    Bad "not found - the install is incomplete; re-run Setup.cmd from the package"
    Write-Host ""; exit 1
}
$v = & $py -c "import sys; print(sys.version.replace(chr(10),' '))" 2>&1
if ($LASTEXITCODE -eq 0) { Ok "runs: $v" } else { Bad "will not run: $v"; if (-not $fail) { $fail = "interpreter" } }

# ---- 2. fastapi / uvicorn in THAT interpreter ------------------------------------------------
Step "2. fastapi and uvicorn in that interpreter"
$out = & $py -c "import fastapi, uvicorn; print('fastapi', fastapi.__version__, 'uvicorn', uvicorn.__version__)" 2>&1
if ($LASTEXITCODE -eq 0) { Ok "$out" } else {
    Bad "they do not import:"
    $out | ForEach-Object { Note $_ }
    if (-not $fail) { $fail = "fastapi/uvicorn" }
}

# ---- 3. describe_app imports -----------------------------------------------------------------
Step "3. describe_app.py imports (where a bad copy shows up)"
$code = "import sys; sys.path.insert(0, r'$describe'); import describe_app; print('describe_app OK, routes:', len(describe_app.app.routes))"
$out = & $py -X utf8 -B -c $code 2>&1
if ($LASTEXITCODE -eq 0) { Ok "$out" } else {
    Bad "it does not import - THIS is why the service dies at launch:"
    $out | ForEach-Object { Note $_ }
    if (-not $fail) { $fail = "describe_app" }
}

# ---- 4. the authoring engines ------------------------------------------------------------------
Step "4. The two authoring engines"
foreach ($pair in @(@("calcs", "orchestrator.calc_authoring"), @("displays", "orchestrator.display_authoring"))) {
    $code = "import sys; sys.path.insert(0, r'$describe'); import importlib; importlib.import_module('$($pair[1])'); print('ok')"
    $out = & $py -X utf8 -B -c $code 2>&1
    if ($LASTEXITCODE -eq 0) {
        Ok "$($pair[0]) engine imports"
    } else {
        Bad "$($pair[0]) engine does NOT import (Describe for it would 503, but this alone does"
        Note "not stop the service starting):"
        $out | Select-Object -Last 6 | ForEach-Object { Note $_ }
    }
}

# ---- 5. the port ----------------------------------------------------------------------------
Step "5. Is port $port free?"
$owner = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue | Select-Object -First 1
if ($owner) {
    $who = "?"
    try { $who = (Get-Process -Id $owner.OwningProcess -ErrorAction Stop).ProcessName } catch { }
    Bad "in use by $who (pid $($owner.OwningProcess)) on $($owner.LocalAddress)"
    Note "If that is an old Describe, run Stop PremHist first, or end that pid."
    if (-not $fail) { $fail = "port busy" }
} else {
    Ok "free"
}

# ---- 6. run it in the foreground ---------------------------------------------------------------
Step "6. Starting uvicorn in the FOREGROUND for $Seconds s (the exact command Start uses)"
if ($fail -and $fail -ne "port busy") {
    Write-Host "   skipped: fix '$fail' above first." -ForegroundColor Yellow
} else {
    $o = Join-Path $env:TEMP "qa-describe-fg.out.log"
    $e = Join-Path $env:TEMP "qa-describe-fg.err.log"
    Remove-Item $o, $e -ErrorAction SilentlyContinue
    $env:ORCH_SETTINGS_FILE = Join-Path $InstallRoot "orchestrator_settings.json"
    $env:PREMALYTICS_CALC_DESCRIBE_LOG = Join-Path $InstallRoot "logs\calc_describe_log.jsonl"
    $args = @("-X", "utf8", "-B", "-m", "uvicorn", "describe_app:app", "--app-dir", $describe,
              "--host", "127.0.0.1", "--port", [string]$port)
    $p = Start-Process -FilePath $py -ArgumentList $args -PassThru -WindowStyle Hidden `
                       -RedirectStandardOutput $o -RedirectStandardError $e
    $deadline = (Get-Date).AddSeconds($Seconds)
    $answered = $false
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 700
        try {
            $r = Invoke-RestMethod -Uri "http://127.0.0.1:$port/health" -TimeoutSec 3 -UseBasicParsing
            $answered = $true
            Ok "/health answered:"
            Write-Host ("         " + ($r | ConvertTo-Json -Compress))
            break
        } catch { }
        $p.Refresh()
        if ($p.HasExited) { break }
    }
    $p.Refresh()
    if (-not $answered) {
        if ($p.HasExited) { Bad "the process exited after $([int]((Get-Date) - $p.StartTime).TotalSeconds) s (exit $($p.ExitCode))" }
        else { Bad "still no answer on /health after $Seconds s" }
    }
    if (-not $p.HasExited) { & taskkill.exe /T /F /PID $p.Id 2>&1 | Out-Null }
    foreach ($f in @($e, $o)) {
        if ((Test-Path $f) -and (Get-Item $f).Length -gt 0) {
            Write-Host "   --- $(Split-Path -Leaf $f)" -ForegroundColor DarkGray
            Get-Content $f -Tail 25 | ForEach-Object { Write-Host "   | $_" -ForegroundColor DarkGray }
        }
    }
}

# ---- 7. what the last real start left behind ----------------------------------------------------
Step "7. What the last Start PremHist left in the logs"
foreach ($n in @("describe.err.log", "describe.log", "describe.prev.err.log")) {
    $f = Join-Path $InstallRoot "logs\$n"
    if ((Test-Path $f) -and (Get-Item $f).Length -gt 0) {
        Write-Host "   --- last 20 of $n" -ForegroundColor DarkGray
        Get-Content $f -Tail 20 | ForEach-Object { Write-Host "   | $_" -ForegroundColor DarkGray }
    } else {
        Write-Host "   $n : empty or missing"
    }
}

Write-Host ""
Write-Host "Paste this whole window back." -ForegroundColor Cyan
Write-Host ""
