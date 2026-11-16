# Levanta la interfaz local en http://127.0.0.1:8000
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    throw "No existe .venv. Creá el entorno con: python -m venv .venv"
}

Write-Host 'Iniciando Texto a audio en http://127.0.0.1:8000 ...' -ForegroundColor Cyan
Start-Job -ScriptBlock {
    Start-Sleep -Seconds 2
    Start-Process 'http://127.0.0.1:8000'
} | Out-Null

& $python -m uvicorn app:app --host 127.0.0.1 --port 8000
