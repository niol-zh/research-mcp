# start-tunnel.ps1 — expose research-mcp over HTTPS for Claude Cowork.
#
# Starts the server (via mcp-proxy) and a Cloudflare Quick Tunnel, then copies
# the connector URL to the clipboard. Portable: no hard-coded paths.
#
# Requirements on PATH:
#   - uvx        (https://docs.astral.sh/uv/)
#   - cloudflared (https://github.com/cloudflare/cloudflared/releases)
#
# Usage:
#   $env:SCOPUS_API_KEY = "your-key"
#   $env:UNPAYWALL_EMAIL = "you@example.com"   # required for get_pdf_link
#   ./scripts/start-tunnel.ps1

$ErrorActionPreference = "Stop"
$Port = 8000
$RepoRoot = Split-Path -Parent $PSScriptRoot

if (-not $env:SCOPUS_API_KEY) {
    Write-Host "SCOPUS_API_KEY is not set. Run: `$env:SCOPUS_API_KEY = 'your-key'" -ForegroundColor Red
    exit 1
}
foreach ($cmd in @("uvx", "cloudflared")) {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        Write-Host "'$cmd' not found on PATH. See the README requirements." -ForegroundColor Red
        exit 1
    }
}

Write-Host "Starting research-mcp on port $Port ..." -ForegroundColor Cyan
$server = Start-Process uvx -PassThru -ArgumentList @(
    "mcp-proxy", "--port", "$Port", "--transport", "streamablehttp",
    "-e", "SCOPUS_API_KEY", "$env:SCOPUS_API_KEY",
    "-e", "UNPAYWALL_EMAIL", "$env:UNPAYWALL_EMAIL",
    "--", "uvx", "--from", "$RepoRoot", "research-mcp"
)

# Wait for the port to come up
$ready = $false
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Seconds 1
    if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) { $ready = $true; break }
}
if (-not $ready) { Write-Host "Server did not start." -ForegroundColor Red; exit 1 }
Write-Host "Server is up." -ForegroundColor Green

Write-Host "Opening Cloudflare tunnel ..." -ForegroundColor Cyan
$log = Join-Path $env:TEMP "research-mcp-tunnel.log"
Remove-Item $log -ErrorAction SilentlyContinue
Start-Process cloudflared -ArgumentList @("tunnel", "--url", "http://localhost:$Port") `
    -RedirectStandardError $log -RedirectStandardOutput "$log.out"

$url = $null
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    if (Test-Path $log) {
        $m = Select-String -Path $log -Pattern "https://[a-z0-9-]+\.trycloudflare\.com" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($m) { $url = $m.Matches[0].Value; break }
    }
}
if (-not $url) { Write-Host "Could not obtain tunnel URL; check $log" -ForegroundColor Yellow; exit 1 }

$mcpUrl = "$url/mcp"
$mcpUrl | Set-Clipboard
Write-Host ""
Write-Host "Connector URL (copied to clipboard):" -ForegroundColor Green
Write-Host "  $mcpUrl" -ForegroundColor Yellow
Write-Host ""
Write-Host "In Cowork: Customize -> Connectors -> Add custom connector -> paste the URL." -ForegroundColor Gray
Write-Host "Keep this window open. Press Ctrl+C to stop." -ForegroundColor Gray

try { Wait-Process -Id $server.Id } finally { Write-Host "Stopped." }
