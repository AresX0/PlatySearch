#!/usr/bin/env pwsh
# deploy.ps1 — Build, tag, push, and deploy to Azure App Service.
#
# IMPORTANT: Azure App Service caches the :latest tag digest. A plain
# `az webapp restart` does NOT re-pull the image. We must either:
#   (a) push a unique tag and update the container config, or
#   (b) call `az webapp config container set` to force a re-pull.
#
# This script uses approach (a) — a timestamp tag — because it is
# deterministic, auditable, and avoids any caching ambiguity.

param(
    [string]$Registry   = "platysearchacr",
    [string]$Image      = "platysearch",
    [string]$AppName    = "platysearch-app",
    [string]$ResGroup   = "platysearch-rg",
    [string]$Tag        = (Get-Date -Format "yyyyMMddHHmmss")
)

$ErrorActionPreference = "Stop"

$fqRegistry = "$Registry.azurecr.io"
$fqImage    = "$fqRegistry/${Image}:$Tag"

Write-Host "==> Building image  $fqImage" -ForegroundColor Cyan
az acr build --registry $Registry `
    --image "${Image}:${Tag}" `
    --image "${Image}:latest" `
    --no-logs . 2>&1 | Out-Null

Write-Host "==> Updating App Service container to  $fqImage" -ForegroundColor Cyan
az webapp config container set `
    --name $AppName `
    --resource-group $ResGroup `
    --container-image-name $fqImage 2>&1 | Out-Null

Write-Host "==> Restarting App Service" -ForegroundColor Cyan
az webapp restart --name $AppName --resource-group $ResGroup 2>&1 | Out-Null

Write-Host "==> Waiting for container startup..." -ForegroundColor Yellow
$maxAttempts = 12
$attempt = 0
do {
    Start-Sleep -Seconds 10
    $attempt++
    try {
        $resp = Invoke-WebRequest -Uri "https://platysearch.platysoft.com/health" -UseBasicParsing -TimeoutSec 10
        if ($resp.StatusCode -eq 200) {
            Write-Host "==> Healthy!  (attempt $attempt)" -ForegroundColor Green
            exit 0
        }
    } catch {
        Write-Host "    attempt $attempt/$maxAttempts — not ready yet..." -ForegroundColor DarkGray
    }
} while ($attempt -lt $maxAttempts)

Write-Host "==> WARNING: App did not become healthy after $maxAttempts attempts." -ForegroundColor Red
Write-Host "    Check logs:  az webapp log tail --name $AppName --resource-group $ResGroup"
exit 1
