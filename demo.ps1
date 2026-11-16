<#
    Demo en vivo: backend local + tunel de Cloudflare + deploy del Worker.

    Uso:
        .\demo.ps1          arranca backend (si hace falta), abre el tunel,
                            redeploya el Worker y verifica las 3 capas.
        .\demo.ps1 -Stop    apaga lo que arranco este script.

    La URL del tunel cambia en cada arranque (quick tunnel), por eso el
    script redeploya el Worker con la URL nueva. La PC tiene que quedar
    prendida mientras dure la demo.
#>
param([switch]$Stop)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
Set-Location $root

$apiPort   = 8000
$apiBase   = "http://127.0.0.1:$apiPort"
$publicUrl = 'https://text-to-voice.lucaslean1806.workers.dev'

$py         = Join-Path $root '.venv\Scripts\python.exe'
$cloudflared = Join-Path $env:USERPROFILE '.local\bin\cloudflared.exe'

$demoDir   = Join-Path $root '.demo'
$cfPidFile = Join-Path $demoDir 'cloudflared.pid'
$uvPidFile = Join-Path $demoDir 'uvicorn.pid'
$cfOut     = Join-Path $demoDir 'cloudflared.out'
$cfErr     = Join-Path $demoDir 'cloudflared.err'
$uvOut     = Join-Path $demoDir 'uvicorn.out'
$uvErr     = Join-Path $demoDir 'uvicorn.err'
$urlFile   = Join-Path $demoDir 'tunnel-url.txt'

New-Item -ItemType Directory -Force $demoDir | Out-Null

function Get-AlivePid([string]$file) {
    if (-not (Test-Path $file)) { return $null }
    $raw = Get-Content -LiteralPath $file -Raw -ErrorAction SilentlyContinue
    if (-not $raw) { return $null }
    $procId = 0
    if (-not [int]::TryParse($raw.Trim(), [ref]$procId)) {
        Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue
        return $null
    }
    if (Get-Process -Id $procId -ErrorAction SilentlyContinue) { return $procId }
    Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue
    return $null
}

function Stop-Tree([int]$procId) {
    # /T mata el arbol completo (npx -> node).
    & taskkill /PID $procId /T /F 2>&1 | Out-Null
}

function Test-ApiHealthy {
    try {
        $h = Invoke-RestMethod "$apiBase/api/health" -TimeoutSec 3
        return ($h.status -eq 'ok')
    } catch { return $false }
}

function Test-PortOpen([int]$port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $client.Connect('127.0.0.1', $port)
        return $true
    } catch { return $false }
    finally { $client.Dispose() }
}

function Get-LogTail([string]$file, [int]$lines = 6) {
    if (-not (Test-Path $file)) { return '(sin log)' }
    return (Get-Content -LiteralPath $file -Tail $lines -ErrorAction SilentlyContinue) -join "`n"
}

# ---------------------------------------------------------------- -Stop
if ($Stop) {
    $stopped = @()

    $cf = Get-AlivePid $cfPidFile
    if ($cf) {
        Stop-Tree $cf
        Remove-Item -LiteralPath $cfPidFile -Force -ErrorAction SilentlyContinue
        $stopped += "tunel cloudflared (pid $cf)"
    }

    $uv = Get-AlivePid $uvPidFile
    if ($uv) {
        Stop-Tree $uv
        Remove-Item -LiteralPath $uvPidFile -Force -ErrorAction SilentlyContinue
        $stopped += "backend uvicorn (pid $uv)"
    }

    if ($stopped.Count -eq 0) {
        Write-Host 'No habia nada corriendo (o ya estaba apagado).' -ForegroundColor Yellow
    } else {
        foreach ($s in $stopped) { Write-Host "Detenido: $s" -ForegroundColor Green }
    }

    Write-Host 'El Worker sigue apuntando al tunel viejo: /api/* responde 502 hasta que corras .\demo.ps1 otra vez.' -ForegroundColor DarkGray
    exit 0
}

# ------------------------------------------------------------ preflight
if (-not (Test-Path -LiteralPath $py)) {
    throw "No encontro el venv: $py. Regeneralo con: python -m venv .venv; .\.venv\Scripts\pip install -r requirements.txt"
}
if (-not (Test-Path -LiteralPath $cloudflared)) {
    throw "No encontro cloudflared: $cloudflared"
}

Write-Host ''
Write-Host '=== text-to-voice · demo en vivo ===' -ForegroundColor Cyan

# ------------------------------------------------------------- backend
if (Test-ApiHealthy) {
    Write-Host "Backend ya estaba arriba en $apiBase (reuso, no lo apago despues)." -ForegroundColor Green
} elseif (Test-PortOpen $apiPort) {
    throw "El puerto $apiPort esta ocupado por otro proceso y no responde /api/health. Cerralo y reintenta."
} else {
    Remove-Item -LiteralPath $uvOut, $uvErr -Force -ErrorAction SilentlyContinue
    $uv = Start-Process -FilePath $py `
        -ArgumentList '-m', 'uvicorn', 'app:app', '--host', '127.0.0.1', '--port', "$apiPort" `
        -WorkingDirectory $root -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput $uvOut -RedirectStandardError $uvErr
    Set-Content -LiteralPath $uvPidFile -Value $uv.Id -NoNewline

    $ready = $false
    for ($i = 0; $i -lt 60; $i++) {
        if (Test-ApiHealthy) { $ready = $true; break }
        if ($uv.HasExited) { break }
        Start-Sleep -Milliseconds 500
    }
    if (-not $ready) {
        throw "El backend no arranco.`n$(Get-LogTail $uvErr 12)"
    }
    Write-Host "Backend arriba en $apiBase (pid $($uv.Id))." -ForegroundColor Green
}

# -------------------------------------------------------------- tunel
$old = Get-AlivePid $cfPidFile
if ($old) {
    Stop-Tree $old
    Remove-Item -LiteralPath $cfPidFile -Force -ErrorAction SilentlyContinue
}

Remove-Item -LiteralPath $cfOut, $cfErr, $urlFile -Force -ErrorAction SilentlyContinue
$cf = Start-Process -FilePath $cloudflared `
    -ArgumentList 'tunnel', '--no-autoupdate', '--url', $apiBase `
    -WorkingDirectory $root -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput $cfOut -RedirectStandardError $cfErr
Set-Content -LiteralPath $cfPidFile -Value $cf.Id -NoNewline

$tunnel = $null
for ($i = 0; $i -lt 120; $i++) {
    Start-Sleep -Milliseconds 500
    $txt = ''
    foreach ($f in @($cfErr, $cfOut)) {
        if (Test-Path -LiteralPath $f) {
            $chunk = Get-Content -LiteralPath $f -Raw -ErrorAction SilentlyContinue
            if ($chunk) { $txt += $chunk + "`n" }
        }
    }
    $m = [regex]::Match($txt, 'https://[a-z0-9-]+\.trycloudflare\.com')
    if ($m.Success) { $tunnel = $m.Value; break }
    if ($cf.HasExited) { break }
}
if (-not $tunnel) {
    throw "cloudflared no entrego URL.`n$(Get-LogTail $cfErr 15)"
}
Set-Content -LiteralPath $urlFile -Value $tunnel -NoNewline
Write-Host "Tunel abierto: $tunnel" -ForegroundColor Green

$tunnelOk = $false
for ($i = 0; $i -lt 40; $i++) {
    try {
        $h = Invoke-RestMethod "$tunnel/api/health" -TimeoutSec 8
        if ($h.status -eq 'ok') { $tunnelOk = $true; break }
    } catch { }
    Start-Sleep -Seconds 1
}
if (-not $tunnelOk) {
    throw "El tunel existe pero /api/health no responde a traves de el.`n$(Get-LogTail $cfErr 15)"
}
Write-Host 'Tunel -> /api/health OK' -ForegroundColor Green

# ------------------------------------------------------------- deploy
Write-Host 'Deployando el Worker con la URL nueva...' -ForegroundColor Cyan
& (Join-Path $root 'deploy-cloudflare.ps1') -ApiOrigin $tunnel
if ($LASTEXITCODE -ne 0) { throw "wrangler deploy fallo (exit $LASTEXITCODE)" }

# -------------------------------------------------------- verificacion
$publicOk = $false
$voices = 0
for ($i = 0; $i -lt 30; $i++) {
    try {
        $h = Invoke-RestMethod "$publicUrl/api/health" -TimeoutSec 15
        if ($h.status -eq 'ok') {
            $publicOk = $true
            $v = Invoke-RestMethod "$publicUrl/api/voices" -TimeoutSec 20
            $voices = @($v.voices).Count
            break
        }
    } catch { }
    Start-Sleep -Seconds 2
}

Write-Host ''
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host '  text-to-voice · listo para demo' -ForegroundColor Cyan
Write-Host '============================================================' -ForegroundColor Cyan
Write-Host "  Local    http://127.0.0.1:$apiPort"
Write-Host "  Tunel    $tunnel"
Write-Host "  Publico  $publicUrl"
if ($publicOk) {
    Write-Host "  Estado   OK · /api/health a traves de Cloudflare · $voices voces" -ForegroundColor Green
} else {
    Write-Host '  Estado   el deploy salio pero /api/health publico todavia no responde (espera ~10 s y recarga)' -ForegroundColor Yellow
}
Write-Host ''
Write-Host '  Apagar:  .\demo.ps1 -Stop' -ForegroundColor DarkGray
Write-Host '  La PC tiene que quedar prendida mientras dure la demo.' -ForegroundColor DarkGray
