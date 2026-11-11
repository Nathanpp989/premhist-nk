# stop-premhist-qa.ps1 - stop the PremHist parts that start-premhist-qa.ps1 started.
#
# Stops ONLY the processes recorded in run\pids.json, matched by PID and start time, in reverse
# start order (Describe and the collector first, the historian last). Each one is asked to stop
# with Ctrl+C first, so the historian closes its store cleanly. Nothing is ever stopped by program
# name: another PremHist on this machine (same exe names) is never touched. Data stays on disk.
#
# Usage:  stop-premhist-qa.ps1 [-Pause]
#
# This file is ASCII only, so it parses in Windows PowerShell 5.1 and PowerShell 7 alike.

param([switch]$Pause)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
. (Join-Path $root "qa-common.ps1")

Write-Host ""
Write-Host "Stopping PremHist (QA)  -  $root" -ForegroundColor Cyan
$recs = @(Read-QaPids $root)
if ($recs.Count -eq 0) {
    Write-Host "  PremHist is not running (nothing was started by start-premhist-qa.ps1)."
    Exit-Qa 0 -Pause:$Pause
}
for ($i = $recs.Count - 1; $i -ge 0; $i--) { Stop-QaPart $recs[$i] }

# Only forget the parts that are actually gone.
#
# This used to delete run\pids.json unconditionally and report "PremHist is stopped". Stop-QaPart
# force-ends a part that ignores Ctrl+C, but taskkill can fail - a denied handle, a process wedged
# in a driver call - and it prints "ended" either way. So a part could still be holding its port
# with its only record deleted: the next stop could not see it, the next start failed on a port
# that "nothing" was using, and the message had said it was stopped.
$alive = @($recs | Where-Object { Get-QaLiveProcess $_ })
if ($alive.Count -eq 0) {
    Remove-Item (Get-QaPidFile $root) -ErrorAction SilentlyContinue
    Write-Host ""
    Write-Host "PremHist is stopped. Your data is kept in $(Join-Path $root 'data')." -ForegroundColor Green
    Exit-Qa 0 -Pause:$Pause
}

# Keep the survivors on record, so another stop can try again and a start can say what is in the way.
Save-QaPids $root $alive
Write-Host ""
Write-QaErr "$($alive.Count) part(s) did NOT stop and are still running:"
foreach ($r in $alive) {
    $p = Get-QaLiveProcess $r
    Write-Host ("    {0,-12} pid {1}  {2}" -f $r.name, $r.pid, $(if ($p) { $p.ProcessName } else { "" })) -ForegroundColor Red
}
Write-Host "  They are still recorded in run\pids.json, so running this again will try them again."
Write-Host "  If one will not end, close it in Task Manager by the pid above - never by name, or"
Write-Host "  you may stop another PremHist on this machine."
Exit-Qa 1 -Pause:$Pause
