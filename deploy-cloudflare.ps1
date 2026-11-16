# Configura API_ORIGIN en wrangler.toml y despliega el Worker.
# Uso: .\deploy-cloudflare.ps1 -ApiOrigin https://text-to-voice-api.onrender.com
param(
    [Parameter(Mandatory = $true)]
    [string]$ApiOrigin
)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$origin = $ApiOrigin.TrimEnd('/')
if (-not $origin.StartsWith('https://')) {
    throw 'API_ORIGIN debe empezar con https://'
}

$content = Get-Content -LiteralPath 'wrangler.toml' -Raw
$updated = [regex]::Replace(
    $content,
    'API_ORIGIN\s*=\s*"[^"]*"',
    'API_ORIGIN = "' + $origin + '"'
)
if ($updated -ne $content) {
    Set-Content -LiteralPath 'wrangler.toml' -Value $updated -NoNewline
    Write-Host "API_ORIGIN = $origin" -ForegroundColor Cyan
}

npx --yes wrangler@latest deploy
