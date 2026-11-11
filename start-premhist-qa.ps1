# start-premhist-qa.ps1 - start ALL of PremHist for QA testing, in order, with health checks.
#
#   1. Historian + viewer (ph-server)       http://127.0.0.1:4050  (gRPC ingest 4051)
#   2. OPC UA simulator (PremPlant, 88 tags) opc.tcp://127.0.0.1:4840
#   3. MCP adapter (4052) + buffer agent (4053, health 4054)
#   4. OPC UA collector (health 4055): simulator -> collector -> agent -> historian, dataset PremPlantUA
#   5. Describe service (4060): calc and display Describe, using the model you picked
#   (6. gRPC data pump, only with -Pump: a second live dataset, PremPlant, straight into the historian)
#
# Each part runs hidden; its output goes to logs\<part>.log. Each part must answer its health
# check before the next one starts. The window ends with one status table, then the viewer opens.
# The ports above are the defaults; setup -BasePort moves them all (see qa-config.json).
#
# Usage:  start-premhist-qa.ps1 [-Model <name>] [-NoDescribe] [-Pump] [-NoBrowser]
#   -Model <name>  use this Ollama model for Describe from now on (saved; see also
#                  'Switch Describe model')
#   -NoDescribe    do not start the Describe service (the viewer hides the Describe boxes)
#   -Pump          also start the gRPC data pump
#   -NoBrowser     do not open the viewer at the end
#   -Pause         wait for Enter before closing (the Start-menu shortcut uses this)
#
# This file is ASCII only, so it parses in Windows PowerShell 5.1 and PowerShell 7 alike.

param(
    [string]$Model,
    [switch]$NoDescribe,
    [switch]$Pump,
    [switch]$NoBrowser,
    [switch]$Pause
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
. (Join-Path $root "qa-common.ps1")

Write-Host ""
Write-Host "Starting PremHist (QA)  -  $root" -ForegroundColor Cyan

$cfg = Read-QaConfig $root
if (-not $cfg) {
    Write-QaErr "PremHist QA is not set up here (no qa-config.json). Run Setup.cmd from the package first."
    Exit-Qa 1 -Pause:$Pause
}
$P = $cfg.ports
$bin = Join-Path $root "bin"
foreach ($need in @("bin\ph-server.exe", "bin\ph-sim.exe", "bin\ph-agent.exe", "bin\ph-collect-opcua.exe", "python-mcp\python.exe", "python-describe\python.exe")) {
    if (-not (Test-Path (Join-Path $root $need))) {
        Write-QaErr "missing $root\$need - the install is incomplete. Run Setup.cmd from the package again."
        Exit-Qa 1 -Pause:$Pause
    }
}

# ---- already running? -------------------------------------------------------------------------
# seedlate is left out on purpose: it binds late-arriving tags and then ends by itself, so it can
# still be waiting (up to 15 min) long after the parts it was seeding for have gone. Counted as a
# running part, it made this guard refuse to start anything and then open the browser on a
# historian that was not running - which looks exactly like "the viewer will not open".
$running = @(Read-QaPids $root | Where-Object { [string]$_.name -ne "seedlate" -and (Get-QaLiveProcess $_) })
if ($running.Count -gt 0) {
    $names = ($running | ForEach-Object { $script:QaTitles[[string]$_.name] }) -join ", "
    Write-QaWarn ("PremHist is already running ({0}). Use 'Stop PremHist' first to restart it." -f $names)
    if (-not $NoBrowser) {
        $v = Get-QaViewerUrl $P.http
        if ($v.fellback) { Write-QaWarn "localhost does not reach the historian on this PC; opening $($v.url) instead." }
        Start-Process $v.url
    }
    Exit-Qa 0 -Pause:$Pause
}

# ---- model --------------------------------------------------------------------------------------
if ($Model) {
    $why = Test-QaModelName $cfg.ollama_url $Model
    if ($why) { Write-QaErr $why; Exit-Qa 1 -Pause:$Pause }
    $cfg.model = $Model
    Save-QaConfig $root $cfg
    Write-QaOk "Describe model set to $Model (saved)"
}
Write-QaOrchSettings $root $cfg

# ---- ports free? ----------------------------------------------------------------------------------
$want = @("http", "grpc", "opcua", "mcp", "agent", "agent_health", "collector_health")
if (-not $NoDescribe) { $want += "describe" }
$busy = @()
foreach ($k in $want) {
    $owner = Get-QaPortOwner $P[$k]
    if ($owner) { $busy += ("port {0} ({1}) is already in use by {2}" -f $P[$k], $k, $owner) }
}
if ($busy.Count) {
    Write-QaErr "PremHist cannot start: its ports are taken."
    $busy | ForEach-Object { Write-Host "        $_" }
    Write-Host "  Fix: close the program using them (another PremHist, a Command Center...), or re-run setup with"
    Write-Host "  a different base port, e.g.  Setup.cmd -BasePort 5050"
    Exit-Qa 1 -Pause:$Pause
}

$data = Join-Path $root "data"
foreach ($d in @("historian", "agent", "collector", "sim")) { New-Item -ItemType Directory -Force (Join-Path $data $d) | Out-Null }

$records = @()
$state = [ordered]@{}
foreach ($n in $script:QaParts) { $state[$n] = "off" }

function Add-Rec($rec) {
    $script:records += $rec
    Save-QaPids $root $script:records
    # The RECORD, not a Process object. Get-QaLiveProcess returns $null for a part that has
    # already exited, and Wait-QaReady then had nothing to poll: a part that died on launch sat
    # out its whole timeout and was reported as a health-check timeout instead of a crash.
    # Wait-QaReady takes either shape (see qa-common.ps1).
    return $rec
}

function Fail-Part([string]$name, [string]$result) {
    $script:state[$name] = "FAILED"
    if ($result -eq "exited") { Write-QaErr "$($script:QaTitles[$name]) stopped right after starting." }
    else { Write-QaErr "$($script:QaTitles[$name]) did not become healthy in time." }
    Show-QaLogTail $root $name
}

function Stop-Started {
    Write-Host "  Stopping what was started..."
    for ($i = $script:records.Count - 1; $i -ge 0; $i--) { Stop-QaPart $script:records[$i] }
    Remove-Item (Get-QaPidFile $root) -ErrorAction SilentlyContinue
}

# ---- 1. historian -------------------------------------------------------------------------------
Write-Host "  [1/5] $($QaTitles.server) ..."
$args1 = @("--data", (Join-Path $data "historian"), "--host", "127.0.0.1", "--http-port", [string]$P.http, "--grpc-port", [string]$P.grpc)
if (-not $NoDescribe) {
    $args1 += @("--authoring-url", "http://127.0.0.1:$($P.describe)/api/displays/draft",
                "--calc-authoring-url", "http://127.0.0.1:$($P.describe)/api/calcs/draft")
}
$proc = Add-Rec (Start-QaPart $root "server" (Join-Path $bin "ph-server.exe") $args1 $null)
$r = Wait-QaReady { Test-QaHttp "http://127.0.0.1:$($P.http)/api/v1/health" } $proc 120
if ($r -ne "ok") {
    Fail-Part "server" $r
    Write-QaErr "Nothing else can work without the historian."
    Stop-Started
    Exit-Qa 1 -Pause:$Pause
}
$state.server = "healthy"

# ---- 2. OPC UA simulator ------------------------------------------------------------------------
Write-Host "  [2/5] $($QaTitles.sim) ..."
$simStatus = Join-Path $data "sim\sim-status.json"
$proc = Add-Rec (Start-QaPart $root "sim" (Join-Path $bin "ph-sim.exe") @("opcua", "--listen", "127.0.0.1:$($P.opcua)", "--status-file", $simStatus) $null)
$r = Wait-QaReady { Test-QaTcp $P.opcua } $proc 60
if ($r -eq "ok") { $state.sim = "healthy" } else { Fail-Part "sim" $r }

# ---- 3. MCP adapter + buffer agent ----------------------------------------------------------------
Write-Host "  [3/5] $($QaTitles.mcp) + $($QaTitles.agent) ..."
$mcpEnv = @{ PH_BASE_URL = "http://127.0.0.1:$($P.http)"; PH_MCP_HOST = "127.0.0.1"; PH_MCP_PORT = [string]$P.mcp
             PH_MCP_USER = "premalytics" }
$mcpProc = Add-Rec (Start-QaPart $root "mcp" (Join-Path $root "python-mcp\python.exe") @("-X", "utf8", "-B", (Join-Path $root "mcp\ph_mcp_server.py")) $mcpEnv)
$agentProc = Add-Rec (Start-QaPart $root "agent" (Join-Path $bin "ph-agent.exe") @(
    "run", "--data", (Join-Path $data "agent"), "--name", "qa-agent", "--listen", "127.0.0.1:$($P.agent)",
    "--health", "127.0.0.1:$($P.agent_health)", "--dest", "live=http://127.0.0.1:$($P.grpc)") $null)
$r = Wait-QaReady { Test-QaHttp "http://127.0.0.1:$($P.agent_health)/" } $agentProc 60
if ($r -eq "ok") { $state.agent = "healthy" } else { Fail-Part "agent" $r }
$r = Wait-QaReady { Test-QaTcp $P.mcp } $mcpProc 90
if ($r -eq "ok") { $state.mcp = "healthy" } else { Fail-Part "mcp" $r }

# ---- 4. OPC UA collector -------------------------------------------------------------------------
Write-Host "  [4/5] $($QaTitles.collector) ..."
$colDir = Join-Path $data "collector"
$colConfig = Join-Path $colDir "collector.toml"
$mapping = Join-Path $colDir "mapping.toml"
$endpoint = "opc.tcp://127.0.0.1:$($P.opcua)"
# Rewritten every start, so it always matches qa-config.json's ports.
Write-Utf8 $colConfig @"
[collector]
data = "$((Join-Path $colDir 'data') -replace '\\', '/')"
agent = "http://127.0.0.1:$($P.agent)"
health = "127.0.0.1:$($P.collector_health)"
agent_health = "127.0.0.1:$($P.agent_health)"

[[connection]]
name = "PremPlantUA"
endpoint = "$endpoint"
security = "none"
mapping = "mapping.toml"
"@
if ($state.sim -ne "healthy" -or $state.agent -ne "healthy") {
    $state.collector = "FAILED"
    Write-QaErr "the collector needs the simulator and the agent; not started."
} else {
    $ok = $true
    if (-not (Test-Path $mapping)) {
        # First run: browse the simulator's 88 tags into mapping.toml (the simulator is up: step 2).
        # (Continue: Windows PowerShell 5.1 turns a native program's stderr into errors under Stop.)
        $ErrorActionPreference = "Continue"
        $out = & (Join-Path $bin "ph-collect-opcua.exe") browse $endpoint --all --connection PremPlantUA 2> (Join-Path $root "logs\collector-browse.err.log")
        $ErrorActionPreference = "Stop"
        if ($LASTEXITCODE -ne 0 -or -not $out) {
            $ok = $false
            $state.collector = "FAILED"
            Write-QaErr "could not read the tag list from the simulator at $endpoint."
            Show-QaLogTail $root "collector-browse"
        } else {
            Write-Utf8 $mapping (($out -join "`n") + "`n")
        }
    }
    if ($ok) {
        $proc = Add-Rec (Start-QaPart $root "collector" (Join-Path $bin "ph-collect-opcua.exe") @("run", "--config", $colConfig) $null)
        $r = Wait-QaReady { Test-QaHttp "http://127.0.0.1:$($P.collector_health)/" } $proc 60
        if ($r -eq "ok") { $state.collector = "healthy" } else { Fail-Part "collector" $r }
    }
}

# ---- the QA examples (asset hierarchy, displays, calcs): only what is missing ------------------------
$seedNote = $null
if ($state.collector -eq "healthy") {
    Write-Host "        adding the example asset hierarchy, displays and calcs (only what is missing) ..."
    $seedLog = Join-Path $root "logs\seed.log"
    $seedErr = Join-Path $root "logs\seed.err.log"
    # Rotate as Start-QaPart does for every other part: these two were overwritten on each start,
    # so the previous run's seeding was gone the moment the next one began.
    foreach ($f in @($seedLog, $seedErr)) {
        if (Test-Path $f) { Move-Item -Force $f ($f -replace '\.log$', '.prev.log') }
    }
    $seedArgs = @("-X", "utf8", "-B", (ConvertTo-QaArg (Join-Path $root "seed_examples.py")), "--url", "http://127.0.0.1:$($P.http)")
    $sp = Start-Process -FilePath (Join-Path $root "python-mcp\python.exe") -ArgumentList $seedArgs -WindowStyle Hidden `
        -RedirectStandardOutput $seedLog -RedirectStandardError $seedErr -PassThru -Wait
    # What the seed actually did, in its own words (it prints one EXAMPLES line). The table used to
    # print a fixed "4 displays ... 3 approved, 1 draft" regardless of the outcome.
    $seedLine = $null
    if (Test-Path $seedLog) {
        $seedLine = (Get-Content $seedLog | Where-Object { $_ -like "EXAMPLES *" } | Select-Object -Last 1)
        if ($seedLine) { $seedLine = $seedLine.Substring("EXAMPLES ".Length).Trim() }
    }
    if ($sp.ExitCode -eq 0) {
        $seedNote = "ok"
    } elseif ($sp.ExitCode -eq 2) {
        # Seeded; some simulator tags have not sent a first value yet (first start). A hidden pass
        # binds them as they appear (up to 15 min) and then exits; Stop stops it if it is still waiting.
        $seedNote = "late"
        [void](Add-Rec (Start-QaPart $root "seedlate" (Join-Path $root "python-mcp\python.exe") @("-X", "utf8", "-B",
            (Join-Path $root "seed_examples.py"), "--url", "http://127.0.0.1:$($P.http)", "--late", "900") $null))
    } else {
        $seedNote = "failed"
    }
}

# ---- 5. Describe -----------------------------------------------------------------------------------
if ($NoDescribe) {
    Write-Host "  [5/5] $($QaTitles.describe): off (-NoDescribe)"
} else {
    Write-Host "  [5/5] $($QaTitles.describe) ..."
    $proc = Add-Rec (Start-QaDescribe $root $cfg)
    $r = Wait-QaReady { Test-QaHttp "http://127.0.0.1:$($P.describe)/health" } $proc 90
    if ($r -eq "ok") { $state.describe = "healthy" } else { Fail-Part "describe" $r }
}

# ---- (6) pump ----------------------------------------------------------------------------------------
if ($Pump) {
    Write-Host "  [+]   $($QaTitles.pump) ..."
    $proc = Add-Rec (Start-QaPart $root "pump" (Join-Path $bin "ph-sim.exe") @("--server", "http://127.0.0.1:$($P.grpc)",
        "--status-file", (Join-Path $data "sim\pump-status.json")) $null)
    $r = Wait-QaReady { $false } $proc 4
    if ($r -eq "timeout") { $state.pump = "running" } else { Fail-Part "pump" $r }
}

# ---- status -----------------------------------------------------------------------------------------
$addr = @{
    server = "http://127.0.0.1:$($P.http)  (gRPC $($P.grpc))"; sim = "opc.tcp://127.0.0.1:$($P.opcua)"
    mcp = "http://127.0.0.1:$($P.mcp)/mcp"; agent = "127.0.0.1:$($P.agent)  (health $($P.agent_health))"
    collector = "health http://127.0.0.1:$($P.collector_health)/"; describe = "http://127.0.0.1:$($P.describe)"
    pump = "-> gRPC $($P.grpc), dataset PremPlant"
}
$pids = @{}
foreach ($rec in $records) { $pids[[string]$rec.name] = $rec.pid }
Write-Host ""
Write-Host ("  {0,-32} {1,-9} {2,-8} {3}" -f "PART", "STATE", "PID", "ADDRESS")
foreach ($n in $script:QaParts) {
    if ($n -eq "pump" -and -not $Pump) { continue }
    $color = "Gray"
    if ($state[$n] -eq "healthy" -or $state[$n] -eq "running") { $color = "Green" } elseif ($state[$n] -eq "FAILED") { $color = "Red" }
    Write-Host ("  {0,-32} {1,-9} {2,-8} {3}" -f $QaTitles[$n], $state[$n], $pids[$n], $addr[$n]) -ForegroundColor $color
}
Write-Host ""
if (-not $NoDescribe) {
    $h = Get-QaDescribeHealth $cfg
    $model = $cfg.model
    if ($h) { $model = $h.model }
    if (-not $model) { $model = "(none picked - run 'Switch Describe model')" }
    $models = Get-QaOllamaModels $cfg.ollama_url
    if ($null -eq $models) {
        Write-Host "  Describe model:  $model   - Ollama does NOT answer at $($cfg.ollama_url); Describe will say so." -ForegroundColor Yellow
        Write-Host "                   Start Ollama; no restart of PremHist is needed."
    } elseif ($model -and -not ($models | Where-Object { $_.Name -eq $model })) {
        Write-Host "  Describe model:  $model   - NOT pulled in Ollama. Run 'Switch Describe model'." -ForegroundColor Yellow
    } else {
        Write-Host "  Describe model:  $model   (Ollama $($cfg.ollama_url))"
    }
    # An authoring engine that does not import is invisible otherwise: the service starts, the
    # health check passes, describe.err.log stays empty, and the only sign is a 503 on the first
    # Describe a tester types. /health reports the import now, so say it here.
    if ($h -and $h.engines) {
        foreach ($eng in $h.engines.PSObject.Properties) {
            if ($eng.Value -ne $true) {
                Write-Host "  Describe engine: '$($eng.Name)' did NOT load - that Describe box will fail:" -ForegroundColor Red
                Write-Host "                   $($eng.Value)" -ForegroundColor Red
            }
        }
    }
}
if ($seedNote -eq "ok" -or $seedNote -eq "late") {
    if ($seedLine) {
        Write-Host "  Examples:        $seedLine"
    } else {
        Write-Host "  Examples:        added; see $(Join-Path $root 'logs\seed.log') for what" -ForegroundColor Yellow
    }
}
if ($seedNote -eq "late") {
    Write-Host "                   A few simulator tags have not reported yet; they are added to the hierarchy as they"
    Write-Host "                   appear (usually within a few minutes)."
} elseif ($seedNote -eq "failed") {
    Write-Host "  Examples:        could NOT be added; see $(Join-Path $root 'logs\seed.err.log'). The rest works." -ForegroundColor Yellow
}
# The name "localhost" only when it actually reaches the historian, else the 127.0.0.1 address
# every health check above confirmed (Get-QaViewerUrl says why).
$viewer = Get-QaViewerUrl $P.http
Write-Host "  Viewer:          $($viewer.url)"
if ($viewer.fellback) {
    Write-Host "                   NOTE localhost does not reach the historian on this PC (IPv6 or a" -ForegroundColor Yellow
    Write-Host "                        system proxy). The historian itself is healthy on 127.0.0.1 -" -ForegroundColor Yellow
    Write-Host "                        use the address above; the Start-menu shortcut uses it too." -ForegroundColor Yellow
}
Write-Host "  Data:            $data"
Write-Host "  Logs:            $(Join-Path $root 'logs')"
Write-Host "  Stop with 'Stop PremHist' (Start menu) or $(Join-Path $root 'stop-premhist-qa.ps1')."

$failed = @($state.Keys | Where-Object { $state[$_] -eq "FAILED" })
if (-not $NoBrowser) { Start-Process $viewer.url }
if ($failed.Count) {
    Write-Host ""
    Write-QaErr ("{0} part(s) failed: {1}. The logs above say why; the rest is running." -f $failed.Count, ($failed -join ", "))
    Exit-Qa 1 -Pause:$Pause
}
Write-Host ""
Write-Host "PremHist is running." -ForegroundColor Green
Exit-Qa 0 -Pause:$Pause
