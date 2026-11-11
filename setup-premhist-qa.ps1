# setup-premhist-qa.ps1 - install PremHist for QA testing (run once from the unzipped package;
# safe to run again: it updates the program files and keeps your data and settings).
#
# Installs into %LOCALAPPDATA%\PremHist-QA (no admin rights needed), data under ...\data:
#   - checks Ollama, at least one pulled model, and that PremHist's ports are free, each in plain
#     words with the exact fix;
#   - copies the program, including its own bundled Python (no Python install needed; any Python
#     already on the PC is never used or changed);
#   - lets you pick the Describe model from what you have pulled into Ollama;
#   - creates Start-menu shortcuts (folder "PremHist QA"): Start PremHist, Stop PremHist,
#     Open PremHist, Switch Describe model, Show logs, Uninstall PremHist QA; and desktop
#     shortcuts Start PremHist / Open PremHist.
# Nothing is downloaded. Setup only ever SUGGESTS commands (e.g. ollama pull); it never runs them.
#
# Usage (Setup.cmd passes its arguments here):
#   Setup.cmd                          install or update
#   Setup.cmd -Model qwen3:8b          pick the model without the menu
#   Setup.cmd -BasePort 5050           move every PremHist port (5050-5055, Describe 5060, OPC UA 5840)
#   Setup.cmd -OllamaUrl http://host:11434   Ollama somewhere other than this computer
#   Setup.cmd -Uninstall               remove PremHist QA (asks before deleting your data)
# More: -InstallRoot <dir>, -OpcUaPort <n>, -ShortcutName <name>,
#       -NoShortcuts, -Pause (wait for Enter at the end).
#
# This file is ASCII only, so it parses in Windows PowerShell 5.1 and PowerShell 7 alike.

param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "PremHist-QA"),
    [int]$BasePort = 0,
    [int]$OpcUaPort = 0,
    [string]$OllamaUrl,
    [string]$Model,
    [string]$ShortcutName = "PremHist QA",
    [switch]$NoShortcuts,
    [switch]$Uninstall,
    [switch]$Pause
)

$ErrorActionPreference = "Stop"
$pkg = $PSScriptRoot
. (Join-Path $pkg "qa-common.ps1")
$InstallRoot = [System.IO.Path]::GetFullPath($InstallRoot)

if ($Uninstall) {
    $u = Join-Path $InstallRoot "uninstall-premhist-qa.ps1"
    if (-not (Test-Path $u)) { Write-QaErr "PremHist QA is not installed in $InstallRoot."; Exit-Qa 1 -Pause:$Pause }
    & $u -Pause:$Pause
    exit $LASTEXITCODE
}

$version = "(unknown)"
if (Test-Path (Join-Path $pkg "VERSION.txt")) {
    $line = Get-Content (Join-Path $pkg "VERSION.txt") | Where-Object { $_ -like "version:*" } | Select-Object -First 1
    if ($line) { $version = $line.Substring(8).Trim() }
}
Write-Host ""
Write-Host "PremHist QA setup  -  version $version" -ForegroundColor Cyan
Write-Host "Install folder: $InstallRoot"
Write-Host ""
Write-Host "Checking what PremHist needs..."

$fromPackage = (Test-Path (Join-Path $pkg "bin\ph-server.exe")) -and (Test-Path (Join-Path $pkg "python-describe\python.exe"))
$samePlace = ([System.IO.Path]::GetFullPath($pkg).TrimEnd("\") -ieq $InstallRoot.TrimEnd("\"))
if (-not $fromPackage -and -not $samePlace) {
    Write-QaErr "run setup from the unzipped PremHist QA package (the folder with bin\ and python-describe\ in it)."
    Exit-Qa 1 -Pause:$Pause
}
$problems = 0

# ---- Windows + PowerShell -----------------------------------------------------------------------
if ([Environment]::OSVersion.Version.Major -lt 10 -or -not [Environment]::Is64BitOperatingSystem) {
    Write-QaErr "PremHist QA needs 64-bit Windows 10 or 11."
    Exit-Qa 1 -Pause:$Pause
}
Write-QaOk ("Windows {0}, PowerShell {1}" -f [Environment]::OSVersion.Version, $PSVersionTable.PSVersion)

# ---- existing install / config ------------------------------------------------------------------
$cfg = Read-QaConfig $InstallRoot
$fresh = (-not $cfg)
if ($fresh) {
    $cfg = [ordered]@{ ollama_url = "http://127.0.0.1:11434"; model = ""; ports = (New-QaPorts 4050 0) }
}
if ($BasePort -gt 0) { $cfg.ports = New-QaPorts $BasePort $OpcUaPort }
elseif ($OpcUaPort -gt 0) { $cfg.ports.opcua = $OpcUaPort }
if ($OllamaUrl) { $cfg.ollama_url = $OllamaUrl.TrimEnd("/") }

# ---- Ollama ---------------------------------------------------------------------------------------
$models = Get-QaOllamaModels $cfg.ollama_url
if ($null -eq $models) {
    Write-QaWarn "Ollama does not answer at $($cfg.ollama_url)."
    Write-Host "        Describe (drafting calcs and displays from plain English) needs it; everything else works without it."
    Write-Host "        Fix: install Ollama from ollama.com, start it (Start menu > Ollama), pull a model (below),"
    Write-Host "        then run 'Switch Describe model' from the Start menu. Setup carries on."
} elseif ($models.Count -eq 0) {
    Write-QaWarn "Ollama is running at $($cfg.ollama_url) but has no chat model pulled."
    Show-QaModelHelp $cfg.ollama_url
} else {
    Write-QaOk ("Ollama at {0}, {1} chat model(s) pulled" -f $cfg.ollama_url, $models.Count)
}

# ---- a running PremHist QA is stopped first (setup replaces its program files) ---------------------
$stoppedIt = $false
$running = @(Read-QaPids $InstallRoot | Where-Object { Get-QaLiveProcess $_ })
if ($running.Count) {
    Write-QaWarn "PremHist QA is running from this folder; stopping it so its files can be updated."
    for ($i = $running.Count - 1; $i -ge 0; $i--) { Stop-QaPart $running[$i] }
    Remove-Item (Get-QaPidFile $InstallRoot) -ErrorAction SilentlyContinue
    $stoppedIt = $true
}

# ---- ports ------------------------------------------------------------------------------------------
$busy = @()
foreach ($k in $cfg.ports.Keys) {
    $owner = Get-QaPortOwner $cfg.ports[$k]
    if ($owner) { $busy += ("port {0} ({1}) is in use by {2}" -f $cfg.ports[$k], $k, $owner) }
}
if ($busy.Count) {
    Write-QaErr "ports PremHist needs are taken:"
    $busy | ForEach-Object { Write-Host "        $_" }
    Write-Host "        Fix: close the program using them, or pick other ports:  Setup.cmd -BasePort 5050"
    $problems++
} else {
    Write-QaOk ("ports free: {0}" -f (($cfg.ports.Values | ForEach-Object { $_ }) -join ", "))
}

if ($problems) {
    Write-Host ""
    # "Nothing was changed" was not true when a running PremHist had just been stopped a few lines
    # above: the tester was told nothing happened while their historian was shut down.
    if ($stoppedIt) {
        Write-QaErr "setup stopped: fix the $problems problem(s) above, then run Setup.cmd again."
        Write-QaWarn "the PremHist that was running here was already stopped; nothing else was changed."
        Write-Host "        Start it again with 'Start PremHist' if you are not reinstalling now."
    } else {
        Write-QaErr "setup stopped: fix the $problems problem(s) above, then run Setup.cmd again. Nothing was changed."
    }
    Exit-Qa 1 -Pause:$Pause
}

# ---- program files ----------------------------------------------------------------------------------
Write-Host ""
Write-Host "Installing..."
New-Item -ItemType Directory -Force $InstallRoot | Out-Null
if (-not $samePlace) {
    foreach ($d in @("bin", "mcp", "describe", "examples", "python-mcp", "python-describe")) {
        $dst = Join-Path $InstallRoot $d
        if (Test-Path $dst) { Remove-Item -Recurse -Force $dst }
        Copy-Item -Recurse (Join-Path $pkg $d) $dst
    }
    foreach ($f in @("start-premhist-qa.ps1", "stop-premhist-qa.ps1", "switch-describe-model.ps1", "uninstall-premhist-qa.ps1",
                     "setup-premhist-qa.ps1", "qa-common.ps1", "seed_examples.py", "README-QA.md", "VERSION.txt", "PremHist_User_Manual.pdf",
                     "requirements-mcp.txt", "requirements-describe.txt")) {
        if (Test-Path (Join-Path $pkg $f)) { Copy-Item -Force (Join-Path $pkg $f) $InstallRoot }
    }
    # Earlier QA builds made venvs from the tester's own Python 3.13; the bundled Python replaces them.
    foreach ($old in @("venv-mcp", "venv-describe")) {
        $o = Join-Path $InstallRoot $old
        if (Test-Path $o) { Remove-Item -Recurse -Force $o }
    }
    # Files from a downloaded zip carry the "from the internet" mark; clear it on our own files.
    Get-ChildItem -Recurse -File $InstallRoot -Exclude "*.log" | Where-Object { $_.FullName -notlike "$InstallRoot\data\*" } | Unblock-File
    Write-QaOk "program files in $InstallRoot"
}
foreach ($d in @("data", "logs", "run")) { New-Item -ItemType Directory -Force (Join-Path $InstallRoot $d) | Out-Null }

# ---- model ----------------------------------------------------------------------------------------
if ($Model) {
    $why = Test-QaModelName $cfg.ollama_url $Model
    if ($why) { Write-QaErr $why; Exit-Qa 1 -Pause:$Pause }
    $cfg.model = $Model
} else {
    $picked = Select-QaModel $cfg.ollama_url $cfg.model
    if ($picked) { $cfg.model = $picked }
}
$modelUnverified = $false
if (-not $cfg.model) {
    # Ollama did not answer, so Select-QaModel could list nothing and returned the current (empty)
    # choice. Falling back to the tuned model is right, but reporting it as a green OK claimed a
    # model that is certainly NOT pulled, since Ollama is not even reachable.
    $cfg.model = $script:QaTunedModel
    $modelUnverified = $true
}
$cfg.shortcut_name = $ShortcutName
Save-QaConfig $InstallRoot $cfg
Write-QaOrchSettings $InstallRoot $cfg
if ($modelUnverified) {
    Write-QaWarn "Describe model set to $($cfg.model) without checking - Ollama did not answer at $($cfg.ollama_url)."
    Write-Host "        Start Ollama, pull a model, then run 'Switch Describe model'. Everything else works."
} else {
    Write-QaOk "Describe model: $($cfg.model)"
}

# ---- shortcuts --------------------------------------------------------------------------------------
if (-not $NoShortcuts) {
    $ps = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    $menu = Join-Path ([Environment]::GetFolderPath("Programs")) $ShortcutName
    New-Item -ItemType Directory -Force $menu | Out-Null
    $shell = New-Object -ComObject WScript.Shell
    function New-QaLink([string]$path, [string]$target, [string]$arguments, [string]$desc) {
        $l = $shell.CreateShortcut($path)
        $l.TargetPath = $target
        $l.Arguments = $arguments
        $l.WorkingDirectory = $InstallRoot
        $l.Description = $desc
        $l.Save()
    }
    function New-QaUrl([string]$path, [string]$url) { Write-Utf8 $path "[InternetShortcut]`r`nURL=$url`r`n" }
    function PsArgs([string]$script, [string]$extra) { "-NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $InstallRoot $script)`" -Pause $extra".Trim() }
    # 127.0.0.1, not "localhost": ph-server binds the IPv4 loopback (start passes --host
    # 127.0.0.1), and on some PCs "localhost" resolves to ::1 first or goes through a system
    # proxy, so the shortcut opened a page that could not connect. Nothing is listening at setup
    # time, so this cannot be probed here - the verified address is used unconditionally.
    $viewer = "http://127.0.0.1:$($cfg.ports.http)"
    New-QaLink (Join-Path $menu "Start PremHist.lnk") $ps (PsArgs "start-premhist-qa.ps1" "") "Start every PremHist part and open the viewer"
    New-QaLink (Join-Path $menu "Stop PremHist.lnk") $ps (PsArgs "stop-premhist-qa.ps1" "") "Stop the PremHist parts started by Start PremHist"
    New-QaLink (Join-Path $menu "Switch Describe model.lnk") $ps (PsArgs "switch-describe-model.ps1" "") "Pick the Ollama model Describe uses"
    New-QaLink (Join-Path $menu "Uninstall $ShortcutName.lnk") $ps (PsArgs "uninstall-premhist-qa.ps1" "") "Remove PremHist QA"
    New-QaLink (Join-Path $menu "Show logs.lnk") (Join-Path $env:SystemRoot "explorer.exe") "`"$(Join-Path $InstallRoot 'logs')`"" "Open the PremHist QA logs folder"
    New-QaUrl (Join-Path $menu "Open PremHist.url") $viewer
    $desk = [Environment]::GetFolderPath("Desktop")
    $suffix = ""
    if ($ShortcutName -ne "PremHist QA") { $suffix = " ($ShortcutName)" }
    New-QaLink (Join-Path $desk "Start PremHist$suffix.lnk") $ps (PsArgs "start-premhist-qa.ps1" "") "Start every PremHist part and open the viewer"
    New-QaUrl (Join-Path $desk "Open PremHist$suffix.url") $viewer
    $cfg.shortcuts = @($menu, (Join-Path $desk "Start PremHist$suffix.lnk"), (Join-Path $desk "Open PremHist$suffix.url"))
    Save-QaConfig $InstallRoot $cfg
    Write-QaOk "shortcuts: Start menu > $ShortcutName, and Start PremHist / Open PremHist on the desktop"
}

Write-Host ""
Write-Host "PremHist QA is installed." -ForegroundColor Green
Write-Host "  Start it:  Start menu > $ShortcutName > Start PremHist   (or the desktop shortcut)"
Write-Host "  Viewer:    http://127.0.0.1:$($cfg.ports.http)"
Write-Host "  Guide:     $(Join-Path $InstallRoot 'README-QA.md')"
Exit-Qa 0 -Pause:$Pause
