#Requires -Version 7.0
<#
.SYNOPSIS
    Despliega el Worker apuntando /api/* a un backend Python y lo verifica.

.DESCRIPTION
    A diferencia de la version anterior, este script no termina en "deploy":
    comprueba el origen antes de publicar y hace un smoke test real despues.
    Un deploy sin verificacion fue exactamente lo que dejo el Worker sin origen.

    Usa curl.exe y no Invoke-WebRequest: el edge de Cloudflare de Render
    responde 403 con un managed challenge a la huella TLS de .NET
    (cf-mitigated: challenge), aunque el mismo request con curl da 200.

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
if (-not (Get-Command curl.exe -ErrorAction SilentlyContinue)) {
    throw 'Se necesita curl.exe (viene incluido en Windows 10 1803+).'
}

$script:tempFiles = @()

function Invoke-Probe {
    <#
        Request con curl.exe. Devuelve Status, Content y Headers.
        curl se usa porque Invoke-WebRequest recibe el managed challenge
        de Cloudflare en el edge de Render.
    #>
    param(
        [Parameter(Mandatory)][string]$Uri,
        [string]$Method = 'Get',
        [string]$BodyFile
    )

    $stamp = [guid]::NewGuid().ToString('N')
    $outFile = Join-Path ([IO.Path]::GetTempPath()) "ttv-$stamp.out"
    $hdrFile = Join-Path ([IO.Path]::GetTempPath()) "ttv-$stamp.hdr"
    $script:tempFiles += $outFile, $hdrFile

    $curlArgs = @('-sS', '--max-time', '90', '-o', $outFile, '-D', $hdrFile, '-w', '%{http_code}')
    if ($Method -ne 'Get') {
        # curl manda el metodo tal cual: en minuscula seria "Post" y el edge
        # responde 400 por metodo invalido.
        $curlArgs += @('-X', $Method.ToUpperInvariant())
    }
    if ($BodyFile) {
        $curlArgs += @('-H', 'Content-Type: application/json', '--data-binary', "@$BodyFile")
    }

    $code = (& curl.exe @curlArgs $Uri | Out-String).Trim()

    return [pscustomobject]@{
        Status  = [int]$code
        Content = if (Test-Path $outFile) { Get-Content $outFile -Raw } else { '' }
        Headers = if (Test-Path $hdrFile) { Get-Content $hdrFile } else { @() }
    }
}

function New-JsonBody {
    param([hashtable]$Data)
    $file = Join-Path ([IO.Path]::GetTempPath()) ("ttv-body-" + [guid]::NewGuid().ToString('N') + '.json')
    $script:tempFiles += $file
    ($Data | ConvertTo-Json -Compress) | Set-Content -Path $file -Encoding utf8 -NoNewline
    return $file
}

try {
    Write-Host "1/4  Verificando el origen $origin" -ForegroundColor Cyan
    $health = Invoke-Probe -Uri "$origin/api/health"
    if ($health.Status -ne 200) {
        throw "El origen no responde 200 en /api/health (status $($health.Status)). Deploy abortado."
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
        Write-Host '     Cargando el secreto PROXY_TOKEN' -ForegroundColor Cyan
        $ProxyToken | npx --yes wrangler@latest secret put PROXY_TOKEN
        if ($LASTEXITCODE -ne 0) {
            throw "wrangler secret put fallo con exit code $LASTEXITCODE."
        }
        Write-Host '     Recorda setear PROXY_TOKEN con el mismo valor en Render.' -ForegroundColor Yellow
    }
    else {
        Write-Host '     Sin PROXY_TOKEN: el backend acepta trafico directo.' -ForegroundColor Yellow
    }

    if ($SkipSmokeTest) {
        Write-Host '3/4  Smoke test omitido (-SkipSmokeTest)' -ForegroundColor Yellow
        exit 0
    }

    Write-Host "3/4  Smoke test sobre $WorkerUrl" -ForegroundColor Cyan

    $workerHealth = Invoke-Probe -Uri "$WorkerUrl/api/health"
    if ($workerHealth.Status -ne 200) {
        throw "El Worker devuelve $($workerHealth.Status) en /api/health. Respuesta: $($workerHealth.Content)"
    }
    if (($workerHealth.Content | ConvertFrom-Json).status -ne 'ok') {
        throw "El Worker responde 200 pero el body no trae status='ok'."
    }
    Write-Host '     OK /api/health' -ForegroundColor Green

    $voices = Invoke-Probe -Uri "$WorkerUrl/api/voices"
    if ($voices.Status -ne 200) {
        throw "El Worker devuelve $($voices.Status) en /api/voices."
    }
    $voiceCount = @(($voices.Content | ConvertFrom-Json).voices).Count
    if ($voiceCount -eq 0) {
        throw 'El Worker devuelve 0 voces.'
    }
    Write-Host "     OK /api/voices ($voiceCount voces)" -ForegroundColor Green

    Write-Host '4/4  Sintesis real de punta a punta' -ForegroundColor Cyan
    $bodyFile = New-JsonBody -Data @{ text = 'ok'; voice = 'es-AR-ElenaNeural' }
    $audio = Invoke-Probe -Uri "$WorkerUrl/api/tts" -Method Post -BodyFile $bodyFile
    if ($audio.Status -ne 200) {
        throw "El Worker devuelve $($audio.Status) en POST /api/tts. Respuesta: $($audio.Content)"
    }
    $contentType = ($audio.Headers | Select-String -Pattern '^content-type:' | Select-Object -First 1) -replace '(?i)^content-type:\s*', ''
    if ($contentType -notmatch 'audio/mpeg') {
        throw "POST /api/tts devolvio 200 pero Content-Type='$contentType'."
    }
    Write-Host "     OK audio ($($contentType.Trim()))" -ForegroundColor Green

    Write-Host ''
    Write-Host 'Deploy verificado: UI, voces y sintesis funcionando.' -ForegroundColor Green
}
finally {
    foreach ($file in $script:tempFiles) {
        Remove-Item -Path $file -ErrorAction SilentlyContinue
    }
}
