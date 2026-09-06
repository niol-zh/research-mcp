# diagnose.ps1 — isoliert, ob ein Fehler vom Server oder vom Tunnel kommt.
#
# Usage:
#   ./scripts/diagnose.ps1
#   ./scripts/diagnose.ps1 -TunnelUrl "https://<dein-tunnel>.trycloudflare.com/mcp"

param(
    [string]$LocalUrl  = "http://127.0.0.1:8000/mcp",
    [string]$TunnelUrl = ""
)

function Test-McpEndpoint {
    param([string]$Url, [string]$Label)

    Write-Host ""
    Write-Host "=== $Label ===" -ForegroundColor Cyan
    Write-Host "    $Url" -ForegroundColor DarkGray

    $body = @{
        jsonrpc = "2.0"; id = 1; method = "initialize"
        params  = @{
            protocolVersion = "2025-06-18"
            capabilities    = @{}
            clientInfo      = @{ name = "diagnose"; version = "1.0" }
        }
    } | ConvertTo-Json -Depth 10

    try {
        $r = Invoke-WebRequest -Uri $Url -Method Post -Body $body `
                -ContentType "application/json" `
                -Headers @{ "Accept" = "application/json, text/event-stream" } `
                -TimeoutSec 25 -SkipHttpErrorCheck

        Write-Host ("  HTTP {0}" -f $r.StatusCode) -ForegroundColor $(if ($r.StatusCode -eq 200) { "Green" } else { "Red" })
        $ct = $r.Headers["Content-Type"]
        if ($ct) { Write-Host "  Content-Type: $ct" -ForegroundColor DarkGray }
        $sid = $r.Headers["Mcp-Session-Id"]
        if ($sid) { Write-Host "  Mcp-Session-Id: $sid" -ForegroundColor DarkGray }

        $text = $r.Content
        if ($text -is [byte[]]) { $text = [Text.Encoding]::UTF8.GetString($text) }
        $snippet = ($text -replace "`r?`n", " ").Trim()
        if ($snippet.Length -gt 400) { $snippet = $snippet.Substring(0, 400) + " ..." }
        Write-Host "  Body: $snippet"

        if ($text -match '"serverInfo"') {
            Write-Host "  => OK: gueltige MCP-Antwort" -ForegroundColor Green
            return $true
        } else {
            Write-Host "  => FEHLER: keine gueltige initialize-Antwort" -ForegroundColor Red
            return $false
        }
    } catch {
        Write-Host "  => EXCEPTION: $($_.Exception.Message)" -ForegroundColor Red
        return $false
    }
}

Write-Host "research-mcp Diagnose" -ForegroundColor Yellow
Write-Host "---------------------"

# Laeuft ueberhaupt etwas auf dem Port?
$port = ([uri]$LocalUrl).Port
$listening = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($listening) {
    Write-Host "Port $port : LAUSCHT" -ForegroundColor Green
    foreach ($c in $listening) {
        $p = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue
        if ($p) { Write-Host "  -> PID $($p.Id)  $($p.ProcessName)" -ForegroundColor DarkGray }
    }
} else {
    Write-Host "Port $port : NICHTS LAUSCHT — der Server laeuft nicht." -ForegroundColor Red
}

# Relevante Umgebungsvariablen
Write-Host ""
Write-Host "Environment:" -ForegroundColor Cyan
foreach ($v in @("SCOPUS_API_KEY", "UNPAYWALL_EMAIL")) {
    $val = [Environment]::GetEnvironmentVariable($v)
    if ($val) {
        $shown = if ($v -eq "SCOPUS_API_KEY") { "gesetzt (Laenge $($val.Length))" } else { $val }
        Write-Host "  $v : $shown" -ForegroundColor Green
    } else {
        Write-Host "  $v : NICHT gesetzt" -ForegroundColor Red
    }
}

$localOk = Test-McpEndpoint -Url $LocalUrl -Label "1) Server direkt (ohne Tunnel)"

$tunnelOk = $null
if ($TunnelUrl) {
    $tunnelOk = Test-McpEndpoint -Url $TunnelUrl -Label "2) Ueber den Cloudflare-Tunnel"
}

Write-Host ""
Write-Host "=== Befund ===" -ForegroundColor Yellow
if (-not $localOk -and $tunnelOk -eq $null) {
    Write-Host "Der lokale Server antwortet nicht korrekt. Sieh dir die Konsole an," -ForegroundColor Red
    Write-Host "in der start-tunnel.ps1 laeuft — dort steht der Traceback." -ForegroundColor Red
} elseif (-not $localOk) {
    Write-Host "Problem liegt beim SERVER (lokal schon fehlerhaft), nicht am Tunnel." -ForegroundColor Red
} elseif ($tunnelOk -eq $false) {
    Write-Host "Server ist lokal GESUND, aber ueber den Tunnel fehlerhaft." -ForegroundColor Yellow
    Write-Host "=> Cloudflare Quick Tunnel bricht vermutlich den Streaming-Response." -ForegroundColor Yellow
} elseif ($tunnelOk) {
    Write-Host "Server und Tunnel antworten beide korrekt." -ForegroundColor Green
    Write-Host "=> Der Fehler entsteht dann erst beim Tool-Aufruf, nicht beim Verbinden." -ForegroundColor Green
} else {
    Write-Host "Server lokal gesund. Tunnel nicht geprueft (-TunnelUrl angeben)." -ForegroundColor Green
}
Write-Host ""
