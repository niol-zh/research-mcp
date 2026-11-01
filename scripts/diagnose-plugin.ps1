# diagnose-plugin.ps1 — check everything the Claude Cowork plugin needs, outside the app.
#
# The plugin starts the server (on Linux, inside the VM Cowork uses on Windows) with
#   uvx --from https://github.com/niol-zh/research-mcp/archive/refs/tags/<ref>.zip research-mcp
# and the server reads its settings from ~/.research-mcp/config.json.
# This script checks uv on the Windows host, the config file, and then runs that
# same uvx command here with a real MCP `initialize` request. If it passes here
# but the plugin still fails in the app, the problem is on the app side (VM,
# network, plugin sync): check the app's logs under %APPDATA%\Claude\logs.
#
# Usage:
#   ./scripts/diagnose-plugin.ps1
#   ./scripts/diagnose-plugin.ps1 -Ref claude/research-mcp-cloud-hosting-wcvruq

param(
    [string]$Ref = "v0.7.0",
    [int]$TimeoutSec = 120
)

$ok = $true

function Step([string]$Label, [bool]$Pass, [string]$Detail) {
    $color = if ($Pass) { "Green" } else { "Red" }
    $mark = if ($Pass) { "OK     " } else { "FEHLER " }
    Write-Host ("  {0} {1}" -f $mark, $Label) -ForegroundColor $color
    if ($Detail) { Write-Host ("         {0}" -f $Detail) -ForegroundColor DarkGray }
}

Write-Host "research-mcp Plugin-Diagnose" -ForegroundColor Yellow
Write-Host "----------------------------"

# ── 1. Tools on PATH ──────────────────────────────────────────────────────────
Write-Host ""
Write-Host "1. Programme im PATH" -ForegroundColor Cyan
$hints = @{
    uv  = "Installieren: winget install astral-sh.uv   (oder: irm https://astral.sh/uv/install.ps1 | iex)"
    uvx = "Kommt mit uv. Fehlt nur uvx: uv neu installieren."
}
foreach ($cmd in @("uv", "uvx")) {
    $found = Get-Command $cmd -ErrorAction SilentlyContinue
    if ($found) {
        $ver = (& $cmd --version 2>&1 | Select-Object -First 1)
        Step $cmd $true ("{0}  ({1})" -f $ver, $found.Source)
    } else {
        Step $cmd $false $hints[$cmd]
        $ok = $false
    }
}
Write-Host "         Nach einer Installation Claude Desktop komplett beenden (auch im Infobereich) und neu starten," -ForegroundColor DarkGray
Write-Host "         sonst sieht die App den neuen PATH nicht." -ForegroundColor DarkGray

# ── 2. Config file ────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "2. Konfigurationsdatei" -ForegroundColor Cyan
$cfgPath = if ($env:RESEARCH_MCP_CONFIG) { $env:RESEARCH_MCP_CONFIG } else { Join-Path $env:USERPROFILE ".research-mcp\config.json" }
if (-not (Test-Path $cfgPath)) {
    Step "Datei vorhanden" $false "$cfgPath fehlt. Vorlage: config.example.json im Repo."
    $ok = $false
} else {
    Step "Datei vorhanden" $true $cfgPath
    try {
        $cfg = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
        Step "Gueltiges JSON" $true ""
        $key = [string]$cfg.scopus_api_key
        $keyOk = ($key -match '^[0-9a-fA-F]{32}$')
        Step "scopus_api_key" $keyOk $(if ($keyOk) { "gesetzt (32 Hex-Zeichen)" } else { "fehlt oder hat nicht das Format eines Scopus-Keys (32 Hex-Zeichen)" })
        $mailOk = ([string]$cfg.unpaywall_email) -match '^[^@\s]+@[^@\s]+\.[^@\s]+$'
        Step "unpaywall_email" $mailOk $(if ($mailOk) { "gesetzt" } else { "fehlt oder ist keine Mailadresse" })
        $dir = [string]$cfg.export_dir
        Step "export_dir" ([bool]$dir) $(if ($dir) { "gesetzt" } else { "fehlt (nur fuer export_scopus_results noetig)" })
        if (-not $keyOk) { $ok = $false }
    } catch {
        Step "Gueltiges JSON" $false "ConvertFrom-Json: $($_.Exception.Message)"
        $ok = $false
    }
}

if (-not $ok) {
    Write-Host ""
    Write-Host "Zuerst die roten Punkte beheben, dann das Skript erneut starten." -ForegroundColor Yellow
    exit 1
}

# ── 3. Start the exact plugin command ─────────────────────────────────────────
Write-Host ""
Write-Host "3. Startbefehl des Plugins" -ForegroundColor Cyan
$refPath = if ($Ref -match '^v\d') { "refs/tags/$Ref" } else { "refs/heads/$Ref" }
$source = "https://github.com/niol-zh/research-mcp/archive/$refPath.zip"
Write-Host "   uvx --from $source research-mcp" -ForegroundColor DarkGray
Write-Host "   (erster Start laedt den Server und seine Abhaengigkeiten, bis zu $TimeoutSec s)" -ForegroundColor DarkGray

$log = Join-Path $env:TEMP "research-mcp-plugin.log"
Remove-Item $log -ErrorAction SilentlyContinue

$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = (Get-Command uvx).Source
$psi.Arguments = "--from `"$source`" research-mcp"
$psi.UseShellExecute = $false
$psi.RedirectStandardInput = $true
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$psi.CreateNoWindow = $true

$proc = New-Object System.Diagnostics.Process
$proc.StartInfo = $psi
$stderr = New-Object System.Text.StringBuilder
$proc.add_ErrorDataReceived({ param($s, $e) if ($e.Data) { [void]$stderr.AppendLine($e.Data) } })
[void]$proc.Start()
$proc.BeginErrorReadLine()

$init = @{
    jsonrpc = "2.0"; id = 1; method = "initialize"
    params = @{
        protocolVersion = "2025-06-18"
        capabilities = @{}
        clientInfo = @{ name = "diagnose-plugin"; version = "1.0" }
    }
} | ConvertTo-Json -Depth 10 -Compress
$proc.StandardInput.WriteLine($init)
$proc.StandardInput.Flush()

$readTask = $proc.StandardOutput.ReadLineAsync()
$answered = $readTask.Wait($TimeoutSec * 1000)
$line = if ($answered) { $readTask.Result } else { $null }

try { $proc.Kill() } catch {}
Start-Sleep -Milliseconds 300
$stderr.ToString() | Set-Content $log -Encoding UTF8

if ($answered -and $line -match '"serverInfo"') {
    Step "MCP initialize" $true ($line.Substring(0, [Math]::Min(120, $line.Length)) + " ...")
} elseif ($answered) {
    Step "MCP initialize" $false "Unerwartete Antwort: $line"
    $ok = $false
} else {
    Step "MCP initialize" $false "Keine Antwort innerhalb von $TimeoutSec s."
    $ok = $false
}

$lines = $stderr.ToString() -split "`r?`n" | Where-Object { $_ -match "Starting research-mcp|Config file|error|Error" } | Select-Object -First 6
foreach ($l in $lines) { Write-Host ("         {0}" -f $l) -ForegroundColor DarkGray }
Write-Host ("         Vollstaendiges Server-Log: {0}" -f $log) -ForegroundColor DarkGray

Write-Host ""
if ($ok) {
    Write-Host "Alles gruen: der Plugin-Befehl laeuft auf diesem Rechner." -ForegroundColor Green
    Write-Host "Startet das Plugin in Claude Cowork trotzdem nicht, liegt es an der App:" -ForegroundColor Green
    Write-Host "  %APPDATA%\Claude\logs\mcp-server-research.log  und  main.log (Zeilen mit 'research')" -ForegroundColor Green
    exit 0
} else {
    Write-Host "Der Startbefehl scheitert bereits ausserhalb der App. Log oben und in $log pruefen." -ForegroundColor Red
    exit 1
}
