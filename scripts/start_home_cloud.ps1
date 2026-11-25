<#
.SYNOPSIS
  Zero-Cost 24/7 Home Cloud Launcher for X4 Cloud Terminal (Windows)
  Replaces Azure Windows VM using your own PC/laptop + optional free Cloudflare Quick Tunnel.
#>
param(
    [int]$Port = 8501,
    [switch]$WithCloudflareTunnel
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

Write-Host "==================================================================" -ForegroundColor Cyan
Write-Host "  X4 CLOUD TERMINAL - ZERO-CARD 24/7 SELF-HOSTED WATCHDOG" -ForegroundColor Cyan
Write-Host "==================================================================" -ForegroundColor Cyan

# Keep AC-powered Windows machine awake for 24/7 operation (best-effort)
powercfg /change standby-timeout-ac 0 2>$null
powercfg /change hibernate-timeout-ac 0 2>$null

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "[INFO] Created .env from .env.example — edit X4_API_TOKEN and X4_BRIDGE_SECRET before exposing online!" -ForegroundColor Yellow
}

if ($WithCloudflareTunnel) {
    $cf = Get-Command cloudflared -ErrorAction SilentlyContinue
    if ($null -eq $cf) {
        Write-Host "[WARN] cloudflared not found. Install free with: winget install --id Cloudflare.cloudflared" -ForegroundColor Yellow
    } else {
        Write-Host "[INFO] Starting free Cloudflare HTTPS Quick Tunnel -> http://127.0.0.1:$Port" -ForegroundColor Green
        Start-Process -FilePath $cf.Source -ArgumentList @("tunnel", "--url", "http://127.0.0.1:$Port")
    }
}

Write-Host "[INFO] Starting auto-restarting X4 Cloud Terminal on http://127.0.0.1:$Port ..." -ForegroundColor Green
while ($true) {
    python run.py --host 0.0.0.0 --port $Port
    Write-Host "[WARN] Process exited ($LASTEXITCODE). Restarting in 3 seconds..." -ForegroundColor Yellow
    Start-Sleep -Seconds 3
}
