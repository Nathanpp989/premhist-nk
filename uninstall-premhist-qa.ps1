# uninstall-premhist-qa.ps1 - remove PremHist QA from this computer.
#
# Stops PremHist if it is running (only the processes it started), removes the shortcuts setup
# made, asks whether to delete your PremHist data (historian store, agent queue, collector state),
# then deletes the install folder (keeping data\ if you said no).
#
# Usage:  uninstall-premhist-qa.ps1 [-DeleteData | -KeepData] [-Pause]
#   -DeleteData / -KeepData   answer the data question up front (no prompt)
#
# This file is ASCII only, so it parses in Windows PowerShell 5.1 and PowerShell 7 alike.

param([switch]$DeleteData, [switch]$KeepData, [switch]$Pause)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
. (Join-Path $root "qa-common.ps1")
Set-Location $env:TEMP   # never sit inside the folder being deleted

Write-Host ""
Write-Host "Uninstall PremHist QA  -  $root" -ForegroundColor Cyan

$recs = @(Read-QaPids $root)
if (@($recs | Where-Object { Get-QaLiveProcess $_ }).Count) {
    Write-Host "  Stopping PremHist first..."
    for ($i = $recs.Count - 1; $i -ge 0; $i--) { Stop-QaPart $recs[$i] }
}

$cfg = Read-QaConfig $root
if ($cfg -and $cfg.shortcuts) {
    foreach ($s in @($cfg.shortcuts)) {
        if ($s -and (Test-Path -LiteralPath $s)) { Remove-Item -LiteralPath $s -Recurse -Force }
    }
    Write-QaOk "shortcuts removed"
}

$data = Join-Path $root "data"
$removeData = $false
if (Test-Path $data) {
    if ($DeleteData) { $removeData = $true }
    elseif ($KeepData) { $removeData = $false }
    else {
        $answer = Read-Host "  Delete your PremHist data too (historian archive, agent queue)? Type YES to delete, Enter to keep it"
        $removeData = ($answer -ceq "YES")
    }
}

# Delete what can be deleted, and NAME what cannot.
#
# $ErrorActionPreference is "Stop" for this script, so a single locked file - a part that survived
# the stop above still holding its .exe, a log open in an editor - used to throw here and end the
# uninstall halfway through, with no message saying which file or that the folder was now a broken
# half-install. Each item is removed on its own terms now, and anything left is listed.
$failed = @()
foreach ($item in @(Get-ChildItem -Force $root)) {
    if ($item.Name -eq "data" -and -not $removeData) { continue }
    try {
        Remove-Item -LiteralPath $item.FullName -Recurse -Force -ErrorAction Stop
    } catch {
        $failed += [pscustomobject]@{ Path = $item.FullName; Why = $_.Exception.Message }
    }
}
if ($failed.Count) {
    Write-Host ""
    Write-QaErr "$($failed.Count) item(s) could not be removed:"
    foreach ($f in $failed) {
        Write-Host "    $($f.Path)" -ForegroundColor Red
        Write-Host "      $($f.Why)" -ForegroundColor DarkGray
    }
    Write-Host "  Usually something is still running or has a file open. Close it, then run this"
    Write-Host "  again - what was already removed stays removed."
    Write-Host ""
    Write-QaWarn "PremHist QA is PARTLY uninstalled; $root still exists."
    Exit-Qa 1 -Pause:$Pause
}
if ($removeData -or -not (Test-Path $data)) {
    Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
    Write-QaOk "removed $root"
} else {
    Write-QaOk "removed PremHist QA; your data is kept in $data (delete that folder yourself when done)"
}
Write-Host ""
Write-Host "PremHist QA is uninstalled." -ForegroundColor Green
Exit-Qa 0 -Pause:$Pause
