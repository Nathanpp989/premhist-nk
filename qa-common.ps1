# qa-common.ps1 - shared helpers for the PremHist QA scripts (dot-sourced; does nothing on its own).
#
# Install layout (everything under one folder, %LOCALAPPDATA%\PremHist-QA by default):
#   bin\                 ph-server, ph-agent, ph-collect-opcua, ph-sim (release, static C runtime)
#   mcp\  describe\      the MCP adapter and the Describe service (Python sources)
#   python-mcp\  python-describe\   their bundled Python (embeddable, packages preinstalled)
#   data\                historian store, agent queue, collector state, simulator status
#   logs\                one log per part per run (+ the previous run's), and one outcome log per
#                        Describe engine: calc_describe_log.jsonl, display_describe_log.jsonl
#   run\pids.json        the processes start-premhist-qa.ps1 started (stop uses ONLY these PIDs)
#   qa-config.json       ports, Ollama URL, Describe model
#   orchestrator_settings.json   what the Describe service reads (ORCH_SETTINGS_FILE)
#
# This file is ASCII only, so it parses in Windows PowerShell 5.1 and PowerShell 7 alike.

$script:QaParts = @("server", "sim", "mcp", "agent", "collector", "describe", "pump")
$script:QaTitles = @{
    server = "Historian + viewer (ph-server)"; sim = "OPC UA simulator (PremPlant)"; mcp = "MCP adapter"
    agent = "Buffer agent"; collector = "OPC UA collector"; describe = "Describe service"; pump = "gRPC data pump"
    seedlate = "Examples (late tags)"
}
$script:QaTunedModel = "gpt-oss:20b"

# Plain UTF-8 without a BOM, in both PowerShell 5.1 and 7.
function Write-Utf8([string]$path, [string]$text) {
    [System.IO.File]::WriteAllText($path, $text, (New-Object System.Text.UTF8Encoding($false)))
}

function Write-QaOk([string]$msg)   { Write-Host "  OK    $msg" -ForegroundColor Green }
function Write-QaWarn([string]$msg) { Write-Host "  NOTE  $msg" -ForegroundColor Yellow }
function Write-QaErr([string]$msg)  { Write-Host "  ERROR $msg" -ForegroundColor Red }

# ---- config -----------------------------------------------------------------------------------

function New-QaPorts([int]$BasePort = 4050, [int]$OpcUaPort = 0) {
    if ($OpcUaPort -le 0) { $OpcUaPort = 4840 + ($BasePort - 4050) }
    [ordered]@{
        http = $BasePort; grpc = $BasePort + 1; mcp = $BasePort + 2; agent = $BasePort + 3
        agent_health = $BasePort + 4; collector_health = $BasePort + 5; describe = $BasePort + 10
        opcua = $OpcUaPort
    }
}

function Get-QaConfigPath([string]$root) { Join-Path $root "qa-config.json" }

function Read-QaConfig([string]$root) {
    $path = Get-QaConfigPath $root
    if (-not (Test-Path $path)) { return $null }
    $raw = Get-Content -Raw -Path $path | ConvertFrom-Json
    $cfg = [ordered]@{}
    foreach ($p in $raw.PSObject.Properties) { $cfg[$p.Name] = $p.Value }
    $ports = [ordered]@{}
    foreach ($p in $raw.ports.PSObject.Properties) { $ports[$p.Name] = [int]$p.Value }
    $cfg.ports = $ports
    $cfg.ollama_url = [string]$raw.ollama_url
    $cfg.model = [string]$raw.model
    return $cfg
}

function Save-QaConfig([string]$root, $cfg) {
    Write-Utf8 (Get-QaConfigPath $root) (([pscustomobject]$cfg | ConvertTo-Json -Depth 4) + "`n")
}

# The Describe service reads ONLY this file (ORCH_SETTINGS_FILE): in orchestrator/settings.py a
# settings file beats ORCH_OLLAMA_MODEL, so a QA-owned file is the one place the choice lives.
function Write-QaOrchSettings([string]$root, $cfg) {
    $o = [ordered]@{ ollama_host = $cfg.ollama_url; ollama_model = $cfg.model }
    Write-Utf8 (Join-Path $root "orchestrator_settings.json") (([pscustomobject]$o | ConvertTo-Json) + "`n")
}

# ---- Ollama + the model menu ------------------------------------------------------------------

# The pulled chat models, or $null when Ollama does not answer. Embedding-only models are left
# out: Describe needs a chat model.
function Get-QaOllamaModels([string]$url) {
    try {
        $tags = Invoke-RestMethod -Uri ($url.TrimEnd("/") + "/api/tags") -TimeoutSec 5 -UseBasicParsing
    } catch { return $null }
    $out = @()
    foreach ($m in @($tags.models)) {
        if (-not $m) { continue }
        $fam = ""
        if ($m.details -and $m.details.family) { $fam = [string]$m.details.family }
        if ($m.name -match "embed" -or $fam -match "bert") { continue }
        $out += [pscustomobject]@{ Name = [string]$m.name; Size = [double]$m.size }
    }
    return ,@($out | Sort-Object Name)
}

# Largest dedicated GPU memory in bytes (the display-adapter class key; readable without admin),
# else nvidia-smi, else 0 = unknown (no fit hints are shown).
function Get-QaGpuMemoryBytes {
    [uint64]$max = 0
    $class = "HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    foreach ($k in @(Get-ChildItem $class -ErrorAction SilentlyContinue)) {
        try {
            $v = (Get-ItemProperty -Path $k.PSPath -Name "HardwareInformation.qwMemorySize" -ErrorAction Stop)."HardwareInformation.qwMemorySize"
            if ($v -is [byte[]]) { $v = [BitConverter]::ToUInt64($v, 0) }
            if ([uint64]$v -gt $max) { $max = [uint64]$v }
        } catch { }
    }
    if ($max -eq 0) {
        $smi = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue
        if ($smi) {
            try {
                foreach ($line in (& $smi.Source --query-gpu=memory.total --format=csv,noheader,nounits 2>$null)) {
                    $mb = [uint64]("$line".Trim())
                    if ($mb * 1MB -gt $max) { $max = $mb * 1MB }
                }
            } catch { }
        }
    }
    return $max
}

function Format-QaGB([double]$bytes) { "{0:N1} GB" -f ($bytes / 1GB) }

function Show-QaModelHelp([string]$url) {
    Write-Host "  To get a model, open a Command Prompt and run, for example:"
    Write-Host "      ollama pull $($script:QaTunedModel)"
    Write-Host "  ($($script:QaTunedModel) is what Describe was tuned on; it needs about 16 GB of GPU memory."
    Write-Host "   Smaller models work but draft noticeably worse - that is expected, not a bug.)"
    Write-Host "  Then run 'Switch Describe model' from the Start menu."
}

# The numbered menu. Returns the chosen model name, or the current one (Enter / nothing to pick).
# Writes nothing; the caller saves.
function Select-QaModel([string]$url, [string]$current) {
    $models = Get-QaOllamaModels $url
    if ($null -eq $models) {
        Write-QaWarn "Ollama does not answer at $url, so the models cannot be listed."
        Write-Host "  Fix: start Ollama (Start menu > Ollama, or 'ollama serve' in a Command Prompt), then run"
        Write-Host "  'Switch Describe model'. Everything except Describe works without it."
        return $current
    }
    if ($models.Count -eq 0) {
        Write-QaWarn "Ollama is running but has no chat model pulled."
        Show-QaModelHelp $url
        return $current
    }
    $gpu = Get-QaGpuMemoryBytes
    Write-Host ""
    Write-Host "  Describe model (what drafts calcs and displays from plain English):"
    if ($gpu -gt 0) { Write-Host ("  Your GPU memory: {0}" -f (Format-QaGB $gpu)) }
    for ($i = 0; $i -lt $models.Count; $i++) {
        $m = $models[$i]
        $hint = ""
        if ($gpu -gt 0) {
            if ($m.Size * 1.2 -le $gpu) { $hint = "likely fits your GPU" } else { $hint = "may be slow (bigger than GPU memory)" }
        }
        $tags = @()
        if ($m.Name -eq $current) { $tags += "CURRENT" }
        if ($m.Name -eq $script:QaTunedModel) { $tags += "tuned for Describe" }
        $tagText = ""
        if ($tags.Count) { $tagText = "  [" + ($tags -join ", ") + "]" }
        Write-Host ("  {0,3}) {1,-34} {2,9}  {3}{4}" -f ($i + 1), $m.Name, (Format-QaGB $m.Size), $hint, $tagText)
    }
    if (-not ($models | Where-Object { $_.Name -eq $script:QaTunedModel })) {
        Write-Host "  (Describe was tuned on $($script:QaTunedModel); smaller models draft worse. To add it: ollama pull $($script:QaTunedModel))"
    }
    $keep = $current
    if (-not $keep -or -not ($models | Where-Object { $_.Name -eq $keep })) { $keep = $null }
    while ($true) {
        if ($keep) { $prompt = "  Pick a number, or press Enter to keep $keep" } else { $prompt = "  Pick a number" }
        $answer = Read-Host $prompt
        if ([string]::IsNullOrWhiteSpace($answer)) {
            if ($keep) { return $keep }
            continue
        }
        $n = 0
        if ([int]::TryParse($answer.Trim(), [ref]$n) -and $n -ge 1 -and $n -le $models.Count) { return $models[$n - 1].Name }
        Write-Host "  Type a number from 1 to $($models.Count)."
    }
}

# For -Model: $null when fine, else the plain-words reason it is refused.
function Test-QaModelName([string]$url, [string]$name) {
    $models = Get-QaOllamaModels $url
    if ($null -eq $models) { return $null }   # cannot check; accepted (Describe will say so if wrong)
    if ($models | Where-Object { $_.Name -eq $name }) { return $null }
    $have = ($models | ForEach-Object { $_.Name }) -join ", "
    if (-not $have) { $have = "none" }
    return "model '$name' is not pulled in Ollama. Pulled: $have. Pull it with: ollama pull $name"
}

# ---- ports ------------------------------------------------------------------------------------

function Get-QaPortOwner([int]$port) {
    $c = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $c) { return $null }
    $name = "?"
    try { $name = (Get-Process -Id $c.OwningProcess -ErrorAction Stop).ProcessName } catch { }
    return "$name (pid $($c.OwningProcess))"
}

function Test-QaTcp([int]$port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $ar = $client.BeginConnect("127.0.0.1", $port, $null, $null)
        if (-not $ar.AsyncWaitHandle.WaitOne(1000)) { return $false }
        $client.EndConnect($ar)
        return $true
    } catch { return $false } finally { $client.Close() }
}

function Test-QaHttp([string]$url) {
    try {
        $r = Invoke-WebRequest -Uri $url -TimeoutSec 3 -UseBasicParsing
        return ($r.StatusCode -eq 200)
    } catch { return $false }
}

# The URL to SHOW and OPEN for the viewer.
#
# ph-server is started with --host 127.0.0.1, so it listens on the IPv4 loopback only, and every
# health check in these scripts confirms 127.0.0.1. The URL handed to the tester, however, has
# always been "localhost" - the one address nothing ever verified. On a PC where "localhost"
# resolves to ::1 first, or where a system proxy has no loopback bypass, that name does not reach
# the historian: the status table said "healthy" and the browser said it could not connect.
#
# So the name is used only when it answers, and otherwise the address that was actually verified
# is used, with the reason said out loud. Returns @{ url = <what to open>; fellback = $true/$false }.
function Get-QaViewerUrl([int]$port) {
    if (Test-QaHttp "http://localhost:$port/api/v1/health") {
        return @{ url = "http://localhost:$port"; fellback = $false }
    }
    return @{ url = "http://127.0.0.1:$port"; fellback = $true }
}

# ---- processes --------------------------------------------------------------------------------

# Two things this process passes down to every part it starts, both fixed here first:
# - "ignore Ctrl+C": a shortcut or a tool can start this script with it set, and then the parts
#   could never be stopped gracefully. Cleared, so every part gets normal Ctrl+C handling.
# - this window's own stdin/stdout/stderr: Start-Process lets the parts inherit them, and a
#   long-running part holding them keeps a pipe (or the window) open after this script ends.
#   Made non-inheritable; each part gets its own log files and an empty stdin instead.
function Enable-QaCtrlC {
    if (-not ("QaCtrlInherit" -as [type])) {
        Add-Type -TypeDefinition @"
using System; using System.Runtime.InteropServices;
public static class QaCtrlInherit {
  [DllImport("kernel32.dll")] public static extern bool SetConsoleCtrlHandler(IntPtr h, bool add);
  [DllImport("kernel32.dll")] static extern IntPtr GetStdHandle(int n);
  [DllImport("kernel32.dll")] static extern bool SetHandleInformation(IntPtr h, uint mask, uint flags);
  public static void NoStdInherit() {
    foreach (int n in new int[] { -10, -11, -12 }) {
      IntPtr h = GetStdHandle(n);
      if (h != IntPtr.Zero && h != new IntPtr(-1)) SetHandleInformation(h, 1, 0);
    }
  }
}
"@
    }
    [void][QaCtrlInherit]::SetConsoleCtrlHandler([IntPtr]::Zero, $false)
    [QaCtrlInherit]::NoStdInherit()
}

# One command-line argument, quoted the way the C runtime parses it (paths with spaces, e.g.
# C:\Users\First Last\..., must survive Windows PowerShell 5.1's plain join).
function ConvertTo-QaArg([string]$a) {
    if ($a -ne "" -and $a -notmatch '[\s"]') { return $a }
    $q = $a -replace '(\\*)"', '$1$1\"'
    $q = $q -replace '(\\+)$', '$1$1'
    return '"' + $q + '"'
}

function Get-QaPidFile([string]$root) { Join-Path $root "run\pids.json" }

function Read-QaPids([string]$root) {
    $f = Get-QaPidFile $root
    if (-not (Test-Path $f)) { return @() }
    try { return @((Get-Content -Raw $f | ConvertFrom-Json).parts) } catch { return @() }
}

function Save-QaPids([string]$root, $parts) {
    $f = Get-QaPidFile $root
    New-Item -ItemType Directory -Force (Split-Path -Parent $f) | Out-Null
    $doc = [pscustomobject]@{ saved = (Get-Date).ToString("o"); parts = @($parts) }
    Write-Utf8 $f (($doc | ConvertTo-Json -Depth 4) + "`n")
}

# The live process for a recorded part, or $null. Matches the PID AND its start time, so a PID
# Windows has since reused for something else is never touched.
function Get-QaLiveProcess($rec) {
    if (-not $rec -or -not $rec.pid) { return $null }
    try { $p = Get-Process -Id ([int]$rec.pid) -ErrorAction Stop } catch { return $null }
    try {
        # PowerShell 7's ConvertFrom-Json already turns the ISO text into a DateTime; 5.1 leaves text.
        if ($rec.started -is [datetime]) { $started = $rec.started }
        else { $started = [datetime]::Parse([string]$rec.started, $null, [System.Globalization.DateTimeStyles]::RoundtripKind) }
        if ([math]::Abs(($p.StartTime.ToUniversalTime() - $started.ToUniversalTime()).TotalSeconds) -gt 2) { return $null }
    } catch { return $null }
    return $p
}

# Starts one part hidden, stdout/stderr to logs\<name>.log / <name>.err.log (the previous run's
# kept as *.prev.*). $envVars is set for the child only. Returns the pid record.
function Start-QaPart([string]$root, [string]$name, [string]$exe, [string[]]$argList, [hashtable]$envVars) {
    Enable-QaCtrlC
    $logs = Join-Path $root "logs"
    New-Item -ItemType Directory -Force $logs | Out-Null
    $out = Join-Path $logs "$name.log"
    $err = Join-Path $logs "$name.err.log"
    foreach ($f in @($out, $err)) {
        if (Test-Path $f) { Move-Item -Force $f ($f -replace '\.log$', '.prev.log') }
    }
    $stdin = Join-Path $root "run\stdin.empty"
    if (-not (Test-Path $stdin)) {
        New-Item -ItemType Directory -Force (Split-Path -Parent $stdin) | Out-Null
        Write-Utf8 $stdin ""
    }
    $saved = @{}
    if ($envVars) {
        foreach ($k in $envVars.Keys) { $saved[$k] = [Environment]::GetEnvironmentVariable($k, "Process"); [Environment]::SetEnvironmentVariable($k, [string]$envVars[$k], "Process") }
    }
    try {
        $argText = (@($argList) | ForEach-Object { ConvertTo-QaArg $_ }) -join " "
        $p = Start-Process -FilePath $exe -ArgumentList $argText -WorkingDirectory $root -WindowStyle Hidden `
            -RedirectStandardInput $stdin -RedirectStandardOutput $out -RedirectStandardError $err -PassThru
    } finally {
        foreach ($k in $saved.Keys) { [Environment]::SetEnvironmentVariable($k, $saved[$k], "Process") }
    }
    try { $started = $p.StartTime.ToUniversalTime().ToString("o") } catch { $started = (Get-Date).ToUniversalTime().ToString("o") }
    return [pscustomobject]@{ name = $name; pid = $p.Id; started = $started; exe = $exe }
}

# Sends Ctrl+C to the console of one process (a separate helper attaches to it, so this
# process's own console is untouched). $true when the event was delivered.
function Send-QaCtrlC([int]$procId) {
    $sig = @'
using System; using System.Runtime.InteropServices;
public static class QaCtrl {
  [DllImport("kernel32.dll", SetLastError=true)] static extern bool FreeConsole();
  [DllImport("kernel32.dll", SetLastError=true)] static extern bool AttachConsole(uint p);
  [DllImport("kernel32.dll", SetLastError=true)] static extern bool SetConsoleCtrlHandler(IntPtr h, bool add);
  [DllImport("kernel32.dll", SetLastError=true)] static extern bool GenerateConsoleCtrlEvent(uint e, uint g);
  public static int Send(uint pid) {
    FreeConsole();
    if (!AttachConsole(pid)) return 3;
    SetConsoleCtrlHandler(IntPtr.Zero, true);
    bool ok = GenerateConsoleCtrlEvent(0, 0);
    System.Threading.Thread.Sleep(500);
    FreeConsole();
    return ok ? 0 : 2;
  }
}
'@
    $helper = "Add-Type -TypeDefinition @'`n$sig`n'@; exit [QaCtrl]::Send($procId)"
    $enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($helper))
    $h = Start-Process powershell.exe -ArgumentList "-NoProfile -NonInteractive -EncodedCommand $enc" -WindowStyle Hidden -Wait -PassThru
    return ($h.ExitCode -eq 0)
}

# Stops one recorded part: Ctrl+C first (ph-server closes its store cleanly), then, if it is
# still running after $graceS seconds, ends that PID and its children. Never by image name.
function Stop-QaPart($rec, [int]$graceS = 20) {
    $p = Get-QaLiveProcess $rec
    $title = $script:QaTitles[[string]$rec.name]
    if (-not $p) {
        if ([string]$rec.name -ne "seedlate") { Write-Host ("  {0,-34} was not running" -f $title) }   # it ends by itself
        return
    }
    [void](Send-QaCtrlC $p.Id)
    $deadline = (Get-Date).AddSeconds($graceS)
    while (-not $p.HasExited -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 250; $p.Refresh() }
    if ($p.HasExited) {
        Write-Host ("  {0,-34} stopped (pid {1})" -f $title, $rec.pid) -ForegroundColor Green
    } else {
        $eap = $ErrorActionPreference; $ErrorActionPreference = "Continue"
        & taskkill.exe /T /F /PID $p.Id 2>&1 | Out-Null
        $ErrorActionPreference = $eap
        Write-Host ("  {0,-34} did not stop in {1} s; ended (pid {2})" -f $title, $graceS, $rec.pid) -ForegroundColor Yellow
    }
}

function Show-QaLogTail([string]$root, [string]$name, [int]$lines = 12) {
    foreach ($f in @((Join-Path $root "logs\$name.err.log"), (Join-Path $root "logs\$name.log"))) {
        if ((Test-Path $f) -and (Get-Item $f).Length -gt 0) {
            Write-Host "  --- last lines of $f" -ForegroundColor DarkGray
            Get-Content $f -Tail $lines | ForEach-Object { Write-Host "  | $_" -ForegroundColor DarkGray }
        }
    }
}

# Waits until $test returns true, the process exits, or the timeout passes. Returns "ok",
# "exited" or "timeout".
#
# $procOrRec may be a System.Diagnostics.Process OR a pid record from Start-QaPart. The record
# matters: Get-QaLiveProcess returns $null for a part that has ALREADY exited, so a caller that
# passed its result had nothing to poll, and a part that died the instant it launched waited out
# its entire timeout (120 s for the historian) and was then reported as "did not become healthy
# in time" rather than "stopped right after starting". A record can be re-checked either way.
function Wait-QaReady([scriptblock]$test, $procOrRec, [int]$timeoutS) {
    $deadline = (Get-Date).AddSeconds($timeoutS)
    while ((Get-Date) -lt $deadline) {
        if (& $test) { return "ok" }
        if ($procOrRec -is [System.Diagnostics.Process]) {
            $procOrRec.Refresh()
            if ($procOrRec.HasExited) { return "exited" }
        } elseif ($procOrRec -and $procOrRec.pid) {
            if (-not (Get-QaLiveProcess $procOrRec)) { return "exited" }
        }
        Start-Sleep -Milliseconds 500
    }
    if (& $test) { return "ok" }
    return "timeout"
}

# ---- the Describe service (shared by start and switch) -------------------------------------------

function Start-QaDescribe([string]$root, $cfg) {
    $py = Join-Path $root "python-describe\python.exe"
    $envVars = @{
        ORCH_SETTINGS_FILE = (Join-Path $root "orchestrator_settings.json")
        PREMALYTICS_CALC_DESCRIBE_LOG = (Join-Path $root "logs\calc_describe_log.jsonl")
        # Displays are logged the same way as calcs. Both default to logs\ on their own, so this
        # is only here to keep the two logs with the rest of the run's logs when the install root
        # is not where the orchestrator package thinks the product root is.
        PREMALYTICS_DISPLAY_DESCRIBE_LOG = (Join-Path $root "logs\display_describe_log.jsonl")
        # Where to warm the plant index at startup. The Describe service is otherwise never told
        # which PremHist to read: premhist_url arrives in each request body, so without this the
        # cache is cold after every restart and the FIRST calc Describe pays the full round trips.
        # The cache is keyed by user as well, so this user should be the one testing: /health
        # reports which user was warmed, so a mismatch shows up rather than just feeling slow.
        PREMALYTICS_PREMHIST_URL = "http://127.0.0.1:$($cfg.ports.server)"
        PREMALYTICS_PREMHIST_USER = $(if ($env:USERNAME) { $env:USERNAME } else { "qa" })
    }
    # The bundled Python runs isolated (its ._pth file), so PYTHON* variables are ignored: -X utf8
    # and -B are its UTF-8 mode and no-bytecode switches.
    Start-QaPart $root "describe" $py @("-X", "utf8", "-B", "-m", "uvicorn", "describe_app:app", "--app-dir", (Join-Path $root "describe"),
        "--host", "127.0.0.1", "--port", [string]$cfg.ports.describe) $envVars
}

function Get-QaDescribeHealth($cfg) {
    try { return Invoke-RestMethod -Uri "http://127.0.0.1:$($cfg.ports.describe)/health" -TimeoutSec 3 -UseBasicParsing } catch { return $null }
}

# Ends the script, waiting for Enter first when started from a shortcut (-Pause), so the window
# does not vanish before the tester has read it.
function Exit-Qa([int]$code, [switch]$Pause) {
    if ($Pause) { Write-Host ""; [void](Read-Host "Press Enter to close this window") }
    exit $code
}
