# ctrlc-broadcast-test.ps1 - settle one question: does Send-QaCtrlC reach ONE process, or every
# process that shares the console?
#
# qa-common.ps1 stops a part with GenerateConsoleCtrlEvent(0, 0). Group 0 means "every process
# attached to the caller's console", not "the process I attached to". Start-QaPart launches each
# part with Start-Process -WindowStyle Hidden and redirected stdio, which does NOT pass
# CREATE_NEW_CONSOLE or CREATE_NEW_PROCESS_GROUP, so every part may share one console. If so,
# stopping any single part stops them all, and stop-premhist-qa.ps1's careful reverse order
# (Describe and the collector first, the historian last) is cosmetic.
#
# This test does NOT touch PremHist. It starts three harmless sleeper processes through the real
# Start-QaPart, sends Ctrl+C to the MIDDLE one through the real Send-QaCtrlC, and reports which
# survived. Everything it creates lives in a temp folder it deletes at the end.
#
#   powershell -ExecutionPolicy Bypass -File .\ctrlc-broadcast-test.ps1
#
# If qa-common.ps1 is not in the default install folder, pass -QaCommon <path to qa-common.ps1>.
# This file is ASCII only, so it parses in Windows PowerShell 5.1 and PowerShell 7 alike.

param(
    [string]$QaCommon = (Join-Path $env:LOCALAPPDATA "PremHist-QA\qa-common.ps1"),
    [int]$Sleep = 600,
    [switch]$Pause
)

$ErrorActionPreference = "Stop"

# ---- protect THIS window, and keep the evidence on disk ---------------------------------------
#
# Start-QaPart launches each part with Start-Process and redirected stdio, which does not pass
# CREATE_NEW_CONSOLE: the children inherit the console of whatever window ran them, which here is
# this one. So if Ctrl+C really does broadcast, it broadcasts to this script too - and the test
# would be interrupted before printing the verdict, in precisely the case it exists to detect.
# The answer would look like "NOT DELIVERED" or like nothing at all.
#
# Two guards. SetConsoleCtrlHandler(NULL, TRUE) makes this process IGNORE Ctrl+C for the duration
# (the helper inside Send-QaCtrlC already does the same for itself), and every observation is
# written to a file as it is made, so the evidence survives even if this window does get cut off.
Add-Type -TypeDefinition @'
using System; using System.Runtime.InteropServices;
public static class QaCtrlSelf {
  [DllImport("kernel32.dll", SetLastError=true)] public static extern bool SetConsoleCtrlHandler(IntPtr h, bool add);
  public static bool Ignore(bool on) { return SetConsoleCtrlHandler(IntPtr.Zero, on); }
}
'@ -ErrorAction SilentlyContinue

$script:Protected = $false
try { $script:Protected = [QaCtrlSelf]::Ignore($true) } catch { }

# Somewhere to write it. $env:TEMP is always set on Windows, but if it ever is not, falling back
# keeps the transcript real - and $script:Saving records whether a line ever actually landed, so
# the end of the run cannot claim a file that was never written.
$tmpDir = $env:TEMP
if (-not $tmpDir) { $tmpDir = $env:TMP }
if (-not $tmpDir) { $tmpDir = $PSScriptRoot }
if (-not $tmpDir) { $tmpDir = "." }
$transcript = Join-Path $tmpDir ("qa-ctrlc-result-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".txt")
$script:Saving = $false
function Say([string]$text, [string]$colour = "Gray") {
    Write-Host $text -ForegroundColor $colour
    try {
        Add-Content -LiteralPath $transcript -Value $text -ErrorAction Stop
        $script:Saving = $true
    } catch { }
}

Say ""
Say "Ctrl+C delivery test (no PremHist state is touched)" "Cyan"
if (-not (Test-Path $QaCommon)) {
    Say "  ERROR cannot find qa-common.ps1 at $QaCommon" "Red"
    Say "        pass -QaCommon <path>"
    if ($Pause) { [void](Read-Host "Press Enter to close") }
    exit 1
}
. $QaCommon
Say "  using $QaCommon"
Say "  PowerShell $($PSVersionTable.PSVersion), host '$($Host.Name)'"
# A private root, so nothing lands in the install's logs or run folder.
$root = Join-Path $env:TEMP ("qa-ctrlc-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Force $root | Out-Null
New-Item -ItemType Directory -Force (Join-Path $root "logs") | Out-Null
$ps = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$names = @("sim", "mcp", "agent")      # real part names, so Stop-QaPart's titles resolve

$recs = @()
try {
    Say ""
    Say "  starting three sleepers through the real Start-QaPart ..."
    foreach ($n in $names) {
        $rec = Start-QaPart $root $n $ps @("-NoProfile", "-NonInteractive", "-Command",
                                           "Start-Sleep -Seconds $Sleep") $null
        $recs += $rec
        Say ("    {0,-6} pid {1}" -f $n, $rec.pid)
    }
    Start-Sleep -Seconds 2

    $before = @{}
    foreach ($r in $recs) { $before[[string]$r.name] = [bool](Get-QaLiveProcess $r) }
    Say ""
    Say ("  alive before: " + (($names | ForEach-Object { "$_=$($before[$_])" }) -join "  "))
    if (($before.Values | Where-Object { -not $_ }).Count -gt 0) {
        Say "  ERROR a sleeper did not start; the test cannot say anything." "Red"
        throw "setup failed"
    }

    $target = $recs[1]
    Say ""
    Say ("  sending Ctrl+C to '{0}' (pid {1}) ONLY ..." -f $target.name, $target.pid)
    $delivered = Send-QaCtrlC ([int]$target.pid)
    Say "  Send-QaCtrlC reported delivered = $delivered"
    Start-Sleep -Seconds 3

    $after = @{}
    foreach ($r in $recs) { $after[[string]$r.name] = [bool](Get-QaLiveProcess $r) }
    Say ""
    Say ("  {0,-8} {1,-8} {2,-8} {3}" -f "PART", "PID", "BEFORE", "AFTER")
    foreach ($r in $recs) {
        $n = [string]$r.name
        $mark = ""
        if ($n -eq [string]$target.name) { $mark = "   <- the one Ctrl+C was sent to" }
        $color = "Gray"
        if ($after[$n]) { $color = "Green" } else { $color = "Yellow" }
        Say ("  {0,-8} {1,-8} {2,-8} {3}{4}" -f $n, $r.pid, "alive",
                    $(if ($after[$n]) { "alive" } else { "GONE" }), $mark) -ForegroundColor $color
    }

    $targetGone = -not $after[[string]$target.name]
    $others = @($names | Where-Object { $_ -ne [string]$target.name })
    $othersGone = @($others | Where-Object { -not $after[$_] })

    Say ""
    Say "  VERDICT" "Cyan"
    if ($targetGone -and $othersGone.Count -eq 0) {
        Say "    SCOPED. Ctrl+C reached only its target. The finding was wrong; stop order is real." "Green"
    } elseif ($targetGone -and $othersGone.Count -gt 0) {
        Say ("    BROADCAST. Ctrl+C also took out: " + ($othersGone -join ", ")) "Red"
        Say "    Every part shares one console, so stopping any part stops them all and" "Red"
        Say "    stop-premhist-qa.ps1's reverse order has no effect. Fix: start each part with" "Red"
        Say "    CREATE_NEW_PROCESS_GROUP and send CTRL_BREAK_EVENT to that group, or give each" "Red"
        Say "    part its own console and attach per PID." "Red"
    } elseif (-not $targetGone -and $othersGone.Count -eq 0) {
        Say "    NOT DELIVERED. Nothing stopped, so this run says nothing about scope." "Yellow"
        Say "    Likely no console to attach to (try from a normal PowerShell window, not the ISE)." "Yellow"
    } else {
        Say ("    ODD. The target survived but these died: " + ($othersGone -join ", ")) "Red"
    }
    Say ""
    Say "  Paste this whole window back."
}
finally {
    foreach ($r in $recs) {
        $p = Get-QaLiveProcess $r
        if ($p) { & taskkill.exe /T /F /PID $p.Id 2>&1 | Out-Null }
    }
    Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
    Say "  cleaned up (sleepers ended, $root removed)"
    # Hand the console back, whatever happened above: leaving Ctrl+C disabled in the tester's own
    # window would be a nasty thing to walk away from.
    if ($script:Protected) { try { [void][QaCtrlSelf]::Ignore($false) } catch { } }
    Write-Host ""
    if ($script:Saving) {
        Write-Host "  The whole result is also saved at:" -ForegroundColor Cyan
        Write-Host "    $transcript" -ForegroundColor Cyan
        Write-Host "  If this window was cut off before the VERDICT, paste that file instead." -ForegroundColor Cyan
    } else {
        Write-Host "  NOTE nothing could be written to $transcript, so this window is the only" -ForegroundColor Yellow
        Write-Host "       copy of the result - paste it before closing." -ForegroundColor Yellow
    }
}
if ($Pause) { Write-Host ""; [void](Read-Host "Press Enter to close this window") }
