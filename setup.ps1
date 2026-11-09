# GrokX setup: dependencies, grokx commands in PATH, self-check.
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir

Write-Host "[1/3] Dependencies (uv sync)..." -ForegroundColor White
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "uv not found in PATH: https://docs.astral.sh/uv/" -ForegroundColor Red
    exit 1
}
& uv sync
if ($LASTEXITCODE -ne 0) { Write-Host "uv sync failed." -ForegroundColor Red; exit 1 }

Write-Host "[2/3] Commands grokx, grokx-cli, grokx-mcp in ~/.local/bin..." -ForegroundColor White
$LocalBin = Join-Path $env:USERPROFILE ".local\bin"
New-Item -ItemType Directory -Force -Path $LocalBin | Out-Null
$Py = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$PyPosix = "/" + ($Py.Substring(0, 1).ToLower()) + ($Py.Substring(2) -replace '\\', '/')
$shims = @{ "grokx" = "grokx_cli"; "grokx-cli" = "grokx_cli"; "grokx-mcp" = "grokx_cli.mcp_server" }
foreach ($name in $shims.Keys) {
    $module = $shims[$name]
    [IO.File]::WriteAllText((Join-Path $LocalBin "$name.cmd"), "@echo off`r`n`"$Py`" -m $module %*`r`n")
    [IO.File]::WriteAllText((Join-Path $LocalBin $name), "#!/usr/bin/env bash`n`"$PyPosix`" -m $module `"`$@`"`n")
}
Remove-Item (Join-Path $LocalBin "start_daemon.vbs"), (Join-Path $LocalBin "start_server.cmd") -ErrorAction SilentlyContinue
if (($env:PATH -split ';') -notcontains $LocalBin) {
    Write-Host "      $LocalBin is not in PATH: add it to use 'grokx' everywhere." -ForegroundColor Yellow
}

Write-Host "[3/3] Self-check..." -ForegroundColor White
& $Py -m grokx_cli doctor

Write-Host ""
Write-Host "Done. Next:" -ForegroundColor Green
Write-Host "  1. chrome://extensions -> Developer mode -> Load unpacked -> $ProjectDir\extension"
Write-Host "  2. Log in to X in that Chrome"
Write-Host "  3. In a terminal: grokx"