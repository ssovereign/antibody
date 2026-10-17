# Windows launcher (double-click start-antibody.cmd). Needs nothing preinstalled:
# uv (a small Python manager) is installed if missing, and it downloads Python 3.12 for ANTIBODY.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

function Find-Uv {
    $c = Get-Command uv -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    foreach ($p in @("$env:USERPROFILE\.local\bin\uv.exe", "$env:USERPROFILE\.cargo\bin\uv.exe")) {
        if (Test-Path $p) { return $p }
    }
    return $null
}

$uv = Find-Uv
if (-not $uv) {
    Write-Host "Installing uv (Python manager, from astral.sh)..."
    powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    $uv = Find-Uv
    if (-not $uv) { throw "uv could not be installed. Install it from https://docs.astral.sh/uv/ and run this again." }
}

$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "Setting up Python 3.12 for ANTIBODY..."
    & $uv venv --python 3.12 .venv
    if ($LASTEXITCODE -ne 0) { throw "Could not create the Python environment." }
}
$stamp = Join-Path $root ".venv\.antibody-deps"
$hash = (Get-FileHash (Join-Path $root "requirements.lock")).Hash
if (-not (Test-Path $stamp) -or ((Get-Content $stamp -Raw).Trim() -ne $hash)) {
    Write-Host "Installing ANTIBODY's packages..."
    & $uv pip install --quiet --python $py -r requirements.lock
    if ($LASTEXITCODE -ne 0) { throw "Package installation failed." }
    Set-Content -Path $stamp -Value $hash
}

& $py (Join-Path $root "scripts\launch.py")
exit $LASTEXITCODE
