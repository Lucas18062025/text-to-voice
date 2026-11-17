#Requires -Version 7.0
<#
.SYNOPSIS
    Despliega el Worker apuntando /api/* a un backend Python y lo verifica.

.DESCRIPTION
    A diferencia de la version anterior, este script no termina en "deploy":
    comprueba el origen antes de publicar y hace un smoke test real despues.
    Un deploy sin verificacion fue exactamente lo que dejo el Worker en 503.

.PARAMETER ApiOrigin
    Origen del backend. Default: el de produccion en Render.

.PARAMETER ProxyToken
    Opcional. Si se pasa (o esta en $env:TTS_PROXY_TOKEN), se carga como
    secreto PROXY_TOKEN en el Worker. Requiere el mismo valor en el backend.

.PARAMETER WorkerUrl
    URL publica del Worker, para el smoke test.

.EXAMPLE
    .\deploy-cloudflare.ps1

.EXAMPLE
    .\deploy-cloudflare.ps1 -ApiOrigin https://algo-random.trycloudflare.com

.EXAMPLE
    $env:TTS_PROXY_TOKEN = (New-Guid).Guid
    .\deploy-cloudflare.ps1
#>
param(
    [string]$ApiOrigin = 'https://text-to-voice-api-ila7.onrender.com',
    [string]$ProxyToken = $env:TTS_PROXY_TOKEN,
    [string]$WorkerUrl = 'https://text-to-voice.lucaslean1806.workers.dev',
    [switch]$SkipSmokeTest
)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$origin = $ApiOrigin.TrimEnd('/')
$WorkerUrl = $WorkerUrl.TrimEnd('/')
if ($origin -notmatch '^https://[^/\s]+$') {
    throw "API_ORIGIN debe ser un origen https:// sin path. Recibi: $ApiOrigin"
}

function Get-Status {
    param([string]$Uri, [string]$Method = 'Get', [string]$Body)
    $params = @{
        Uri                = $Uri
        Method             = $Method
        TimeoutSec         = 90
        SkipHttpErrorCheck = $true
    }
    if ($Body) {
        $params.Body = $Body
        $params.ContentType = 'application/json'
    }
    return Invoke-WebRequest @params
}

Write-Host "1/4  Verificando el origen $origin" -ForegroundColor Cyan
$health = Get-Status -Uri "$origin/api/health"
if ($health.StatusCode -ne 200) {
    throw "El origen no responde 200 en /api/health (status $($health.StatusCode)). Deploy abortado."
}
$healthJson = $health.Content | ConvertFrom-Json
if ($healthJson.status -ne 'ok') {
    throw "El origen respondio 200 pero status='$($healthJson.status)'. Deploy abortado."
}
Write-Host "     OK origen vivo (provider=$($healthJson.provider))" -ForegroundColor Green

Write-Host "2/4  Desplegando el Worker (API_ORIGIN=$origin)" -ForegroundColor Cyan
npx --yes wrangler@latest deploy --var "API_ORIGIN:$origin"
if ($LASTEXITCODE -ne 0) {
    throw "wrangler deploy fallo con exit code $LASTEXITCODE."
}

if ($ProxyToken) {
    Write-Host "     Cargando el secreto PROXY_TOKEN" -ForegroundColor Cyan
    $ProxyToken | npx --yes wrangler@latest secret put PROXY_TOKEN
    if ($LASTEXITCODE -ne 0) {
        throw "wrangler secret put fallo con exit code $LASTEXITCODE."
    }
    Write-Host "     Recorda setear PROXY_TOKEN con el mismo valor en Render." -ForegroundColor Yellow
}
else {
    Write-Host "     Sin PROXY_TOKEN: el backend acepta trafico directo." -ForegroundColor Yellow
}

if ($SkipSmokeTest) {
    Write-Host "3/4  Smoke test omitido (-SkipSmokeTest)" -ForegroundColor Yellow
    exit 0
}

Write-Host "3/4  Smoke test sobre $WorkerUrl" -ForegroundColor Cyan
$workerHealth = Get-Status -Uri "$WorkerUrl/api/health"
if ($workerHealth.StatusCode -ne 200) {
    throw "El Worker devuelve $($workerHealth.StatusCode) en /api/health. Respuesta: $($workerHealth.Content)"
}
if (($workerHealth.Content | ConvertFrom-Json).status -ne 'ok') {
    throw "El Worker responde 200 pero el body no trae status='ok'."
}
Write-Host "     OK /api/health" -ForegroundColor Green

$voices = Get-Status -Uri "$WorkerUrl/api/voices"
if ($voices.StatusCode -ne 200) {
    throw "El Worker devuelve $($voices.StatusCode) en /api/voices."
}
$voiceCount = @(($voices.Content | ConvertFrom-Json).voices).Count
if ($voiceCount -eq 0) {
    throw "El Worker devuelve 0 voces."
}
Write-Host "     OK /api/voices ($voiceCount voces)" -ForegroundColor Green

Write-Host "4/4  Sintesis real de punta a punta" -ForegroundColor Cyan
$payload = @{ text = 'ok'; voice = 'es-AR-ElenaNeural' } | ConvertTo-Json
$audio = Get-Status -Uri "$WorkerUrl/api/tts" -Method Post -Body $payload
if ($audio.StatusCode -ne 200) {
    throw "El Worker devuelve $($audio.StatusCode) en POST /api/tts. Respuesta: $($audio.Content)"
}
$contentType = [string]$audio.Headers['Content-Type']
if ($contentType -notmatch 'audio/mpeg') {
    throw "POST /api/tts devolvio 200 pero Content-Type='$contentType'."
}
Write-Host "     OK audio ($contentType, $($audio.RawContentLength) bytes)" -ForegroundColor Green

Write-Host ""
Write-Host "Deploy verificado: UI, voces y sintesis funcionando." -ForegroundColor Green
