# diagnose-describe.ps1 - why is the Describe service misbehaving?
#
#     powershell -ExecutionPolicy Bypass -File .\tools\diagnose-describe.ps1
#
# Read-only. Starts nothing, changes nothing, and - importantly - never invokes the model, so it
# answers in seconds rather than minutes.
#
# The trick it uses: routes_calcs.py and routes_displays.py import the orchestrator package
# INSIDE the request handler, before they validate the body. So POSTing an empty object {} tells
# the two apart with no LLM call at all:
#
#     400 bad_request      -> the orchestrator imported fine; the body was simply rejected
#     503 llm_unavailable  -> the orchestrator could NOT be imported, and the message says why
#
# That is the single most useful fact after patching the Describe modules, and nothing in the
# status table or /health will tell you.
#
# This file is ASCII only, so it parses in Windows PowerShell 5.1 and PowerShell 7 alike.

param([string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "PremHist-QA"))

$ErrorActionPreference = "Continue"

function Post-Json([string]$url, [string]$body) {
    # Returns @{ code = <int>; body = <string> }, for success AND for an error status.
    try {
        $r = Invoke-WebRequest -Uri $url -Method POST -Body $body -ContentType "application/json" `
                               -TimeoutSec 20 -UseBasicParsing
        return @{ code = [int]$r.StatusCode; body = [string]$r.Content }
    } catch {
        $code = 0
        $text = $_.Exception.Message
        $resp = $_.Exception.Response
        if ($resp) {
            try { $code = [int]$resp.StatusCode } catch { }
            try {
                $sr = New-Object System.IO.StreamReader($resp.GetResponseStream())
                $text = $sr.ReadToEnd()
                $sr.Close()
            } catch { }
        }
        if ($_.ErrorDetails -and $_.ErrorDetails.Message) { $text = $_.ErrorDetails.Message }
        return @{ code = $code; body = [string]$text }
    }
}

function Get-Json([string]$url, [int]$sec = 10) {
    try { return Invoke-RestMethod -Uri $url -TimeoutSec $sec -UseBasicParsing } catch { return $null }
}

Write-Host ""
Write-Host "Describe service diagnosis" -ForegroundColor Cyan
Write-Host "  install: $InstallRoot"

$cfgPath = Join-Path $InstallRoot "qa-config.json"
if (-not (Test-Path $cfgPath)) { Write-Host "  ERROR no qa-config.json at $cfgPath" -ForegroundColor Red; exit 1 }
$cfg = Get-Content -Raw $cfgPath | ConvertFrom-Json
$port = [int]$cfg.ports.describe
# The historian's own port, for the selftest call: it needs the premhist_url a real
# Describe would carry, and ph-server is where that points.
$serverPort = if ($cfg.ports.server) { [int]$cfg.ports.server } else { $port - 10 }
$ollama = [string]$cfg.ollama_url
$model = [string]$cfg.model
Write-Host "  port   : $port"
Write-Host "  ollama : $ollama"
Write-Host "  model  : $model"

# ---- 1. the process, and whether it predates the files it imported --------------------------
Write-Host ""
Write-Host "1. The describe process" -ForegroundColor Cyan
$proc = $null
$pidFile = Join-Path $InstallRoot "run\pids.json"
if (Test-Path $pidFile) {
    try {
        foreach ($rec in @((Get-Content -Raw $pidFile | ConvertFrom-Json).parts)) {
            if ([string]$rec.name -eq "describe" -and $rec.pid) {
                $proc = Get-Process -Id ([int]$rec.pid) -ErrorAction SilentlyContinue
            }
        }
    } catch { }
}
if (-not $proc) {
    Write-Host "   not running (no live describe pid in run\pids.json)" -ForegroundColor Yellow
} else {
    Write-Host "   pid $($proc.Id), started $($proc.StartTime)"
    # STALE CODE CHECK: Python caches modules at import. Files patched after the process started
    # are not in use until it restarts - the commonest reason a fix "did not work".
    $orchDir = (Get-ChildItem -Recurse -Filter calc_scope.py $InstallRoot -ErrorAction SilentlyContinue |
                Select-Object -First 1)
    if ($orchDir) {
        $newer = Get-ChildItem (Join-Path $orchDir.Directory.FullName "*.py") |
                 Where-Object { $_.LastWriteTime -gt $proc.StartTime }
        if ($newer) {
            Write-Host "   STALE: these modules changed AFTER the service started -" -ForegroundColor Red
            foreach ($f in $newer) { Write-Host ("     {0}  ({1})" -f $f.Name, $f.LastWriteTime) -ForegroundColor Red }
            Write-Host "   The running service is still using the OLD code." -ForegroundColor Red
            Write-Host "   Fix: .\stop-premhist-qa.ps1 then .\start-premhist-qa.ps1" -ForegroundColor Red
        } else {
            Write-Host "   the orchestrator modules on disk all predate the process (not stale)" -ForegroundColor Green
        }
    }
}

# ---- 2. /health -------------------------------------------------------------------------------
Write-Host ""
Write-Host "2. GET /health" -ForegroundColor Cyan
$h = Get-Json "http://127.0.0.1:$port/health"
if (-not $h) {
    Write-Host "   no answer on port $port" -ForegroundColor Red
} else {
    Write-Host "   ok=$($h.ok)  model=$($h.model)  ollama=$($h.ollama)"
    Write-Host "   settings_file=$($h.settings_file)"
    if ($h.version) { Write-Host "   version=$($h.version)  up $($h.uptime_s)s (since $($h.started))" }
    if ($null -ne $h.engines) {
        Write-Host "   engines: calcs=$($h.engines.calcs) displays=$($h.engines.displays)"
    }
    if ($h.index) {
        $w = $h.index.warm
        $cache = "entries=$($h.index.entries) hits=$($h.index.hits) misses=$($h.index.misses)"
        Write-Host "   index: $cache  warm=$($w.state)$(if ($w.user) { " (user $($w.user))" })"
        if ($w.state -eq "off") {
            Write-Host "     the index is not warmed at startup, so the first calc Describe after a" -ForegroundColor Yellow
            Write-Host "     restart reads the whole plant before it begins." -ForegroundColor Yellow
        } elseif ($w.why) {
            Write-Host "     $($w.why)" -ForegroundColor Yellow
        }
    } else {
        # Only true of an install from before /health learned to report these.
        Write-Host "   NOTE this /health does not report the engines, the version or the index" -ForegroundColor Yellow
        Write-Host "        cache, so ok:true here only means the web server is up - step 3 and" -ForegroundColor Yellow
        Write-Host "        step 4 are what actually prove Describe can work." -ForegroundColor Yellow
    }
    $osf = Join-Path $InstallRoot "orchestrator_settings.json"
    if ($h.settings_file -and ($h.settings_file -ne $osf)) {
        Write-Host "   WARNING it is reading $($h.settings_file)," -ForegroundColor Red
        Write-Host "           not the QA-owned $osf - your model choice is NOT the one in use." -ForegroundColor Red
    }
}

# ---- 3. the selftest route, when this install has it ------------------------------------------
# One call that answers everything the {} probe below could only hint at: whether this process can
# reach PremHist, what each engine's grounding found, the index cache state, and whether the
# configured model is actually pulled. No model call, nothing written. The {} probe is kept after
# it, because an install that predates the selftest route still needs the old answer.
Write-Host ""
Write-Host "3. Describe selftest (one call, no model call)" -ForegroundColor Cyan
$stBody = "{""premhist_url"":""http://127.0.0.1:$serverPort"",""user"":""$($env:USERNAME)""}"
$st = Post-Json "http://127.0.0.1:$port/api/describe/selftest" $stBody
if ($st.code -eq 404) {
    Write-Host "   not in this install (it arrived with the selftest patch) - skipping to the probe" -ForegroundColor DarkGray
} elseif ($st.code -eq 0) {
    Write-Host "   no answer from the service: $($st.body)" -ForegroundColor Red
} else {
    try {
        $j = $st.body | ConvertFrom-Json
        $colour = if ($j.ok) { "Green" } else { "Red" }
        Write-Host "   ok = $($j.ok)   (HTTP $($st.code), $($j.route_seconds)s, no model call)" -ForegroundColor $colour
        foreach ($name in @("engines", "premhist", "calcs", "displays", "index", "ollama")) {
            $s = $j.steps.$name
            if (-not $s) { continue }
            $mark = if ($s.ok) { "ok  " } else { "FAIL" }
            $c = if ($s.ok) { "Gray" } else { "Red" }
            $detail = switch ($name) {
                "engines"  { "calcs=$($s.calcs) displays=$($s.displays)" }
                "premhist" { "$($s.templates) template(s), site '$($s.site)'" }
                "calcs"    { "scope '$($s.scope)', $($s.elements) element(s), $($s.est_tokens) tokens, from_cache=$($s.from_cache)" }
                "displays" { "$($s.tags) tag(s), $($s.templates) template(s), $($s.est_tokens) tokens" }
                "index"    { "entries=$($s.entries) hits=$($s.hits) misses=$($s.misses) ttl=$($s.ttl_s)s" }
                "ollama"   { "$($s.models) model(s), '$($s.model)' pulled=$($s.model_pulled)" }
                default    { "" }
            }
            if (-not $s.ok) { $detail = ($s.error, $s.skipped | Where-Object { $_ })[0] }
            Write-Host ("   {0} {1,-9} {2}" -f $mark, $name, $detail) -ForegroundColor $c
        }
        foreach ($w in @($j.warnings)) { if ($w) { Write-Host "   WARNING $w" -ForegroundColor Yellow } }
    } catch {
        Write-Host "   could not read the answer: $($st.body)" -ForegroundColor Yellow
    }
}

# ---- 4. can the service import the orchestrator? (no model call) ------------------------------
Write-Host ""
Write-Host "4. Can the running service import the authoring engines?" -ForegroundColor Cyan
foreach ($ep in @("calcs", "displays")) {
    $r = Post-Json "http://127.0.0.1:$port/api/$ep/draft" "{}"
    $short = $r.body
    if ($short.Length -gt 300) { $short = $short.Substring(0, 300) + " ..." }
    if ($r.code -eq 400) {
        Write-Host "   /api/$ep/draft -> 400  the engine imports fine (body rejected, as expected)" -ForegroundColor Green
    } elseif ($r.code -eq 503) {
        Write-Host "   /api/$ep/draft -> 503  THE ENGINE DID NOT IMPORT" -ForegroundColor Red
        Write-Host "     $short" -ForegroundColor Red
    } elseif ($r.code -eq 0) {
        Write-Host "   /api/$ep/draft -> no answer: $short" -ForegroundColor Red
    } else {
        Write-Host "   /api/$ep/draft -> $($r.code)" -ForegroundColor Yellow
        Write-Host "     $short"
    }
}

# ---- 5. Ollama --------------------------------------------------------------------------------
Write-Host ""
Write-Host "5. Ollama" -ForegroundColor Cyan
$tags = Get-Json ($ollama.TrimEnd("/") + "/api/tags") 5
if (-not $tags) {
    Write-Host "   no answer at $ollama - every Describe will fail with llm_unavailable" -ForegroundColor Red
} else {
    $names = @($tags.models | ForEach-Object { [string]$_.name })
    Write-Host "   answering; $($names.Count) model(s) pulled"
    if ($model -and ($names -notcontains $model)) {
        Write-Host "   '$model' is NOT among them: $($names -join ', ')" -ForegroundColor Red
        Write-Host "   Fix: .\switch-describe-model.ps1" -ForegroundColor Red
    } elseif ($model) {
        Write-Host "   '$model' is pulled" -ForegroundColor Green
    }
}

# ---- 6. the logs ------------------------------------------------------------------------------
Write-Host ""
Write-Host "6. Recent log lines" -ForegroundColor Cyan
foreach ($n in @("describe.err.log", "describe.log")) {
    $f = Join-Path $InstallRoot "logs\$n"
    if ((Test-Path $f) -and (Get-Item $f).Length -gt 0) {
        Write-Host "   --- last 15 of $n" -ForegroundColor DarkGray
        Get-Content $f -Tail 15 | ForEach-Object { Write-Host "   | $_" -ForegroundColor DarkGray }
    } else {
        Write-Host "   $n is empty or missing"
    }
}
# One outcome log per engine. Both are read: a tester who says "Describe is broken" has usually
# only tried one of the two tabs, and which one is the first thing worth knowing.
foreach ($pair in @(@("calc_describe_log.jsonl", "calc"), @("display_describe_log.jsonl", "display"))) {
    $jl = Join-Path $InstallRoot ("logs\" + $pair[0])
    if (Test-Path $jl) {
        $n = (Get-Content $jl | Measure-Object -Line).Lines
        Write-Host ("   {0}: {1} line(s); last outcome(s):" -f $pair[0], $n)
        Get-Content $jl -Tail 3 | ForEach-Object {
            try {
                $o = $_ | ConvertFrom-Json
                Write-Host ("     {0}  outcome={1}  model={2}  {3}s  {4}" -f $o.ts, $o.outcome, $o.model, $o.seconds, $o.headline)
            } catch { Write-Host "     $_" }
        }
    } else {
        Write-Host ("   {0}: not created yet (no {1} Describe has run)" -f $pair[0], $pair[1])
    }
}

Write-Host ""
Write-Host "Paste this whole window back." -ForegroundColor Cyan
Write-Host ""
