# apply-patch.ps1 - copy the patched PremHist QA files into an installed PremHist QA.
#
# Run it from the folder this file is in. No paths to edit, nothing to substitute:
#
#     powershell -ExecutionPolicy Bypass -File .\apply-patch.ps1
#
# It finds the install, shows you exactly what it will do, asks once, backs up every file it
# replaces, copies, unblocks, and then runs the verifier. Nothing is deleted.
#
# Options:
#   -InstallRoot <dir>   the install to patch      (default %LOCALAPPDATA%\PremHist-QA)
#   -AlsoPackage <dir>   ALSO patch the original unzipped package, so that running Setup.cmd
#                        again later does not quietly put the old files back
#   -Yes  (or -Force)    copy without asking (use this when pasting one-line commands)
#   -WhatIfOnly          show the plan and stop
#
# This file is ASCII only, so it parses in Windows PowerShell 5.1 and PowerShell 7 alike.

param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "PremHist-QA"),
    [string]$AlsoPackage,
    [Alias("Force")]
    [switch]$Yes,
    [switch]$WhatIfOnly
)

$ErrorActionPreference = "Stop"
$src = $PSScriptRoot

Write-Host ""
Write-Host "PremHist QA - apply patched files" -ForegroundColor Cyan
Write-Host "  source : $src"
Write-Host "  install: $InstallRoot"

if (-not (Test-Path (Join-Path $src "qa-common.ps1"))) {
    Write-Host "  ERROR this script must sit in the unzipped premhist-qa-patched folder." -ForegroundColor Red
    Write-Host "        It expects qa-common.ps1 beside it."
    exit 1
}
if (-not (Test-Path $InstallRoot)) {
    Write-Host "  ERROR no PremHist QA install at $InstallRoot" -ForegroundColor Red
    Write-Host "        Pass the right one:  .\apply-patch.ps1 -InstallRoot 'D:\somewhere\PremHist-QA'"
    exit 1
}

# Is it running? Copying is safe, but a running Describe service keeps the modules it already
# imported, so the Python fixes do not take effect until it restarts.
$running = $false
$pidFile = Join-Path $InstallRoot "run\pids.json"
if (Test-Path $pidFile) {
    try {
        foreach ($rec in @((Get-Content -Raw $pidFile | ConvertFrom-Json).parts)) {
            if ($rec -and $rec.pid -and (Get-Process -Id ([int]$rec.pid) -ErrorAction SilentlyContinue)) {
                $running = $true
            }
        }
    } catch { }
}

# Where does the orchestrator package actually live? Found, never assumed.
#
# It used to take the FIRST calc_scope.py the recursive search returned and say nothing about the
# rest. If an install has two copies - a backup folder from an earlier patch, a nested unzip, a
# __pycache__ sibling - "first" is whatever order the filesystem happened to return, so the patch
# could land in a folder the service does not import, report success, and leave the running
# install untouched. Now: backups and caches are excluded, describe\orchestrator wins when it is
# among the candidates, and any other copy is named out loud rather than silently ignored.
$orchDst = $null
$hits = @(Get-ChildItem -Path $InstallRoot -Recurse -Filter "calc_scope.py" -ErrorAction SilentlyContinue |
          Where-Object { $_.DirectoryName -notlike "*__pycache__*" -and
                         $_.DirectoryName -notlike "*\patch-backup*" -and
                         $_.DirectoryName -notlike "*\backup*" })
$preferred = Join-Path $InstallRoot "describe\orchestrator"
if ($hits.Count -gt 0) {
    $exact = $hits | Where-Object { $_.DirectoryName -eq $preferred } | Select-Object -First 1
    if ($exact) {
        $orchDst = $exact.DirectoryName
    } else {
        $orchDst = ($hits | Sort-Object { $_.DirectoryName.Length } | Select-Object -First 1).DirectoryName
    }
    if ($hits.Count -gt 1) {
        Write-Host ""
        Write-Host "  NOTE $($hits.Count) orchestrator packages are under this install." -ForegroundColor Yellow
        foreach ($h in $hits) {
            $mark = if ($h.DirectoryName -eq $orchDst) { "  <- patching this one" } else { "" }
            Write-Host "       $($h.DirectoryName)$mark" -ForegroundColor Yellow
        }
        Write-Host "       If the Describe service imports a different one, pass -InstallRoot." -ForegroundColor Yellow
    }
} elseif (Test-Path $preferred) {
    $orchDst = $preferred
}

# (source relative path, destination folder)
#
# EVERY folder is enumerated. It used to be a fixed list of file names per folder, with only
# tools\ enumerated, and that was a real defect rather than a style choice: display_glyphs.py,
# routes_common.py, routes_calcs.py and routes_displays.py were all in the bundle and all absent
# from the list, so the script reported success having silently left them behind. What the tester
# then saw was "no attribute PORTLESS_GLYPHS" in the middle of a Describe, and a verifier run that
# failed the checks for files that were sitting in the folder they had just unzipped. A fixed list
# has to be edited every time a file is added, and nothing fails when it is forgotten - so there
# is no fixed list anywhere in here now.
$plan = @()
$skipRoot = @("apply-patch.ps1", "VERIFY.txt")              # this script, and the manifest
foreach ($f in Get-ChildItem $src -File) {
    if ($skipRoot -contains $f.Name) { continue }
    $plan += , @($f.Name, $InstallRoot)
}
if ($orchDst) {
    $srcOrch = Join-Path $src "describe\orchestrator"
    if (Test-Path $srcOrch) {
        foreach ($f in Get-ChildItem $srcOrch -File -Filter *.py) {
            $plan += , @("describe\orchestrator\$($f.Name)", $orchDst)
        }
    }
    # describe_app.py and the routes_*.py sit one level up, beside the orchestrator package.
    $describeDst = Split-Path -Parent $orchDst
    $srcDescribe = Join-Path $src "describe"
    if (Test-Path $srcDescribe) {
        foreach ($f in Get-ChildItem $srcDescribe -File -Filter *.py) {
            $plan += , @("describe\$($f.Name)", $describeDst)
        }
    }
}
if (Test-Path (Join-Path $src "tools")) {
    foreach ($t in Get-ChildItem (Join-Path $src "tools") -File) {
        $plan += , @("tools\$($t.Name)", (Join-Path $InstallRoot "tools"))
    }
}
# This script and the manifest are copied into tools\ as a record of what was applied: VERIFY.txt
# names every file and its hash, so "which bundle is actually on this machine?" is answerable from
# the install alone, and verify-fixes.py can check the applier itself - the fixed-list defect above
# was invisible precisely because nothing ever verified the thing doing the copying. (Re-applying
# still needs the unzipped bundle: this copy has no files beside it to copy from.)
foreach ($self in @("apply-patch.ps1", "VERIFY.txt")) {
    if (Test-Path (Join-Path $src $self)) { $plan += , @($self, (Join-Path $InstallRoot "tools")) }
}
# Every file in the bundle is either planned or deliberately skipped - never quietly dropped.
$planned = @{}
foreach ($p in $plan) { $planned[$p[0]] = $true }
$unplanned = @()
foreach ($f in Get-ChildItem $src -Recurse -File) {
    $rel = $f.FullName.Substring($src.Length).TrimStart("\")
    if ($rel -like "*__pycache__*") { continue }
    if (-not $planned.ContainsKey($rel) -and -not ($skipRoot -contains $rel)) { $unplanned += $rel }
}
if ($unplanned.Count -gt 0) {
    Write-Host ""
    Write-Host "  NOTE these bundled files have no destination and will NOT be copied:" -ForegroundColor Yellow
    foreach ($u in $unplanned) { Write-Host "       $u" -ForegroundColor Yellow }
    if (-not $orchDst) {
        Write-Host "       (the orchestrator package was not found, which is why)" -ForegroundColor Yellow
    }
}

Write-Host ""
if ($orchDst) {
    Write-Host "  orchestrator found at: $orchDst" -ForegroundColor Green
} else {
    Write-Host "  NOTE the orchestrator package was not found under the install." -ForegroundColor Yellow
    Write-Host "       Every Describe module will be SKIPPED (they are listed above). Find it with:"
    Write-Host "         Get-ChildItem -Recurse -Filter calc_scope.py `"$InstallRoot`" | Select FullName"
}
Write-Host ""
Write-Host "  will copy $($plan.Count) file(s):"
foreach ($p in $plan) { Write-Host ("    {0,-44} -> {1}" -f $p[0], $p[1]) }
if ($running) {
    Write-Host ""
    Write-Host "  NOTE PremHist is running. Copying is safe, but the Describe service keeps the" -ForegroundColor Yellow
    Write-Host "       modules it already loaded: run 'Stop PremHist' then 'Start PremHist' after this."
}
if ($WhatIfOnly) { Write-Host ""; Write-Host "  -WhatIfOnly: nothing was changed."; exit 0 }

# The confirmation. This used to be a bare `Read-Host` whose answer was compared case-sensitively
# against one fixed word, which was wrong in three ways at once: "yes" and "Yes" cancelled,
# pressing Enter cancelled, and all three cases said so in one quiet line that scrolls away under
# the plan - so the script looked like it had simply done nothing. Now: any yes answers, no answer
# is asked again rather than treated as no, a cancel is unmissable, and -Yes skips the prompt.
Write-Host ""
if ($Yes) {
    Write-Host "  -Yes: copying without asking." -ForegroundColor Cyan
} else {
    $interactive = $true
    try { if ([Console]::IsInputRedirected) { $interactive = $false } } catch { }
    if (-not $interactive) {
        Write-Host "  This window cannot prompt (its input is redirected), so nothing was copied." -ForegroundColor Red
        Write-Host "  Re-run with -Yes to skip the question:" -ForegroundColor Yellow
        Write-Host "      powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Yes" -ForegroundColor Yellow
        exit 1
    }
    $answer = ""
    for ($i = 0; $i -lt 3 -and -not $answer; $i++) {
        $answer = (Read-Host "  Copy these $($plan.Count) file(s)? [yes/no]").Trim()
        if (-not $answer) { Write-Host "  (nothing typed - type yes or no, on this same line)" -ForegroundColor DarkGray }
    }
    if ($answer -notmatch '^(y|yes)$') {
        Write-Host ""
        Write-Host "  CANCELLED - nothing was copied, and the install is unchanged." -ForegroundColor Red
        Write-Host "  To apply it, run this again and type  yes  at the question:" -ForegroundColor Yellow
        Write-Host "      powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Yes" -ForegroundColor Yellow
        Write-Host "  (-Yes copies straight away, with no question.)" -ForegroundColor Yellow
        exit 1
    }
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backup = Join-Path $InstallRoot "backup-$stamp"
$copied = 0
$backedUp = 0
foreach ($p in $plan) {
    $from = Join-Path $src $p[0]
    $toDir = $p[1]
    $to = Join-Path $toDir (Split-Path -Leaf $p[0])
    New-Item -ItemType Directory -Force $toDir | Out-Null
    if (Test-Path $to) {
        $bdir = Join-Path $backup (Split-Path -Parent $p[0])
        if (-not (Split-Path -Parent $p[0])) { $bdir = $backup }
        New-Item -ItemType Directory -Force $bdir | Out-Null
        Copy-Item -Force $to (Join-Path $bdir (Split-Path -Leaf $p[0]))
        $backedUp++
    }
    Copy-Item -Force $from $to
    Unblock-File -LiteralPath $to -ErrorAction SilentlyContinue
    $copied++
}
Write-Host ""
Write-Host "  copied $copied file(s); replaced $backedUp, backed up to $backup" -ForegroundColor Green

if ($AlsoPackage) {
    if (-not (Test-Path (Join-Path $AlsoPackage "Setup.cmd"))) {
        Write-Host "  NOTE -AlsoPackage '$AlsoPackage' has no Setup.cmd; skipped." -ForegroundColor Yellow
    } else {
        # The same enumeration as above, and for the same reason: this path used to carry its own
        # fixed list, so describe_app.py, the routes_*.py and the whole of tools\ were left out of
        # the package even when they had just been copied into the install - and the next
        # Setup.cmd put the old ones back, undoing the patch without saying anything.
        $n = 0
        foreach ($f in Get-ChildItem $src -File) {
            if ($skipRoot -contains $f.Name) { continue }
            Copy-Item -Force $f.FullName (Join-Path $AlsoPackage $f.Name); $n++
        }
        foreach ($pair in @(@("describe\orchestrator", "*.py"), @("describe", "*.py"), @("tools", "*"))) {
            $from = Join-Path $src $pair[0]
            $to = Join-Path $AlsoPackage $pair[0]
            if (-not (Test-Path $from)) { continue }
            if (-not (Test-Path $to)) { New-Item -ItemType Directory -Force -Path $to | Out-Null }
            foreach ($f in Get-ChildItem $from -File -Filter $pair[1]) {
                Copy-Item -Force $f.FullName (Join-Path $to $f.Name); $n++
            }
        }
        Get-ChildItem -Recurse -File $AlsoPackage | Unblock-File -ErrorAction SilentlyContinue
        Write-Host "  package at $AlsoPackage patched too: $n file(s) (Setup.cmd will keep these)" -ForegroundColor Green
    }
}

# Verify, with the bundled Python so nothing on PATH matters.
$py = Join-Path $InstallRoot "python-describe\python.exe"
$ver = Join-Path $InstallRoot "tools\verify-fixes.py"
if ((Test-Path $py) -and (Test-Path $ver)) {
    Write-Host ""
    Write-Host "  running the verifier ..." -ForegroundColor Cyan
    & $py $ver $InstallRoot
    $rc = $LASTEXITCODE
    Write-Host ""
    if ($rc -eq 0) {
        Write-Host "  All checks passed." -ForegroundColor Green
        if ($running) { Write-Host "  Now: Stop PremHist, then Start PremHist." -ForegroundColor Yellow }
    } else {
        Write-Host "  Some checks did not pass - see the list above." -ForegroundColor Yellow
        Write-Host "  To undo everything: copy the files in $backup back over the install."
    }
} else {
    Write-Host ""
    Write-Host "  (bundled Python or tools\verify-fixes.py not found; skipped the verifier)"
}
Write-Host ""
