# switch-describe-model.ps1 - pick the Ollama model Describe uses, then restart ONLY Describe.
#
# Shows the models you have pulled into Ollama as a numbered menu (with a fits-your-GPU hint),
# saves the choice, and, when PremHist is running, restarts only the Describe service so the
# next Describe uses the new model. The historian, simulator, agent, collector and MCP adapter
# keep running untouched.
#
# Usage:  switch-describe-model.ps1 [-Model <name>] [-Pause]
#   -Model <name>   skip the menu and use this pulled model
#
# This file is ASCII only, so it parses in Windows PowerShell 5.1 and PowerShell 7 alike.

param([string]$Model, [switch]$Pause)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
. (Join-Path $root "qa-common.ps1")

$cfg = Read-QaConfig $root
if (-not $cfg) {
    Write-QaErr "PremHist QA is not set up here (no qa-config.json). Run Setup.cmd from the package first."
    Exit-Qa 1 -Pause:$Pause
}
Write-Host ""
Write-Host "Switch Describe model  (Ollama $($cfg.ollama_url))" -ForegroundColor Cyan

if ($Model) {
    $why = Test-QaModelName $cfg.ollama_url $Model
    if ($why) { Write-QaErr $why; Exit-Qa 1 -Pause:$Pause }
    $chosen = $Model
} else {
    $chosen = Select-QaModel $cfg.ollama_url $cfg.model
}
if (-not $chosen) {
    Write-QaWarn "no model picked; nothing changed."
    Exit-Qa 1 -Pause:$Pause
}
$changed = ($chosen -ne $cfg.model)
$cfg.model = $chosen
Save-QaConfig $root $cfg
Write-QaOrchSettings $root $cfg
if ($changed) { Write-QaOk "Describe model: $chosen (saved)" } else { Write-QaOk "Describe model stays $chosen" }

$recs = @(Read-QaPids $root)
$desc = $recs | Where-Object { $_.name -eq "describe" } | Select-Object -First 1
$server = $recs | Where-Object { $_.name -eq "server" } | Select-Object -First 1
if (-not $server -or -not (Get-QaLiveProcess $server)) {
    Write-Host "  PremHist is not running; the model is used the next time you start it."
    Exit-Qa 0 -Pause:$Pause
}
if (-not $desc) {
    Write-Host "  PremHist is running without Describe (-NoDescribe); the model is used the next time Describe starts."
    Exit-Qa 0 -Pause:$Pause
}

Write-Host "  Restarting the Describe service only..."
Stop-QaPart $desc
$rec = Start-QaDescribe $root $cfg
$recs = @($recs | Where-Object { $_.name -ne "describe" }) + $rec
Save-QaPids $root $recs
$proc = Get-QaLiveProcess $rec
$r = Wait-QaReady { Test-QaHttp "http://127.0.0.1:$($cfg.ports.describe)/health" } $proc 90
if ($r -ne "ok") {
    Write-QaErr "the Describe service did not come back."
    Show-QaLogTail $root "describe"
    Exit-Qa 1 -Pause:$Pause
}
$h = Get-QaDescribeHealth $cfg
# /health now reports whether each authoring engine imports, so a restart can be reported honestly.
# This used to announce "Describe is running with model X" purely because the port answered, which
# is the same overstatement start-premhist-qa.ps1 used to make: the service comes up cleanly with a
# broken engine, and the only sign was a 503 on the first real Describe. If the reply is missing or
# says a model that is not the one just chosen, say that rather than echoing the request back.
if (-not $h) {
    Write-QaWarn "Describe restarted (pid $($rec.pid)) but /health did not answer, so the model in"
    Write-Host  "          use could not be confirmed. Check with: tools\diagnose-describe.ps1"
    Exit-Qa 0 -Pause:$Pause
}
$broken = @()
if ($h.PSObject.Properties.Name -contains "engines" -and $h.engines) {
    foreach ($n in @("calcs", "displays")) {
        $v = $h.engines.$n
        if ($null -ne $v -and $v -ne $true) { $broken += "$n ($v)" }
    }
}
if ($broken.Count) {
    Write-QaErr "Describe restarted (pid $($rec.pid)) with model $($h.model), but an engine does not import:"
    foreach ($b in $broken) { Write-Host "    $b" -ForegroundColor Red }
    Write-Host "  Every Describe will fail until that is fixed. Run: tools\why-describe-wont-start.ps1"
    Exit-Qa 1 -Pause:$Pause
}
if ($h.model -and $h.model -ne $chosen) {
    Write-QaWarn "Describe is running with model $($h.model), not the $chosen just chosen -"
    Write-Host  "          it may be reading a different settings file: $($h.settings_file)"
    Exit-Qa 1 -Pause:$Pause
}
Write-QaOk "Describe is running with model $($h.model) (pid $($rec.pid)). Nothing else was restarted."
Exit-Qa 0 -Pause:$Pause
