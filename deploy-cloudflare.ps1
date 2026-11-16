# Despliega el Worker apuntando /api/* a un backend Python.
#
# Uso:
#   .\deploy-cloudflare.ps1 -ApiOrigin https://text-to-voice-api.onrender.com
#   .\deploy-cloudflare.ps1 -ApiOrigin https://algo-random.trycloudflare.com
#
# No modifica wrangler.toml: la URL se pasa por CLI, asi el repo queda
# limpio (la URL del tunel cambia en cada arranque).

param(
    [Parameter(Mandatory = $true)]
    [string]$ApiOrigin
)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$origin = $ApiOrigin.TrimEnd('/')
if ($origin -notmatch '^https://[^/\s]+$') {
    throw "API_ORIGIN debe ser un origen https:// sin path. Recibi: $ApiOrigin"
}

Write-Host "API_ORIGIN = $origin" -ForegroundColor Cyan
npx --yes wrangler@latest deploy --var "API_ORIGIN:$origin"
