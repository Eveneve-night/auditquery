param(
    [Parameter(Mandatory=$true)][string]$InstanceDir,
    [ValidateSet('offline','online')][string]$Mode = 'offline',
    [string]$PythonExe,
    [switch]$UseExistingEnvironment,
    [int]$Port = 8000
)

$ErrorActionPreference = 'Stop'
# Keep this file ASCII so Windows PowerShell 5.1 parses it without a UTF-8 BOM.
$repo = Split-Path -Parent $PSScriptRoot
$resolvedInstance = [System.IO.Path]::GetFullPath($InstanceDir)
$resolvedRepo = [System.IO.Path]::GetFullPath($repo)
if ($resolvedInstance.StartsWith($resolvedRepo + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase) -or $resolvedInstance -eq $resolvedRepo) {
    throw 'The instance directory must be outside the repository.'
}
if (-not $env:ICT_SECRET_MASTER_KEY_B64 -and -not $env:ICT_MASTER_KEY_FILE) {
    throw 'Provide a master key through an external environment variable or file path.'
}
$pythonCommand = $PythonExe
if ($UseExistingEnvironment -and -not $PythonExe) {
    throw '-UseExistingEnvironment requires -PythonExe.'
}
$pythonArgs = @()
if (-not $pythonCommand) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $pythonCommand = 'py'
        $pythonArgs = @('-3.12')
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        $pythonCommand = 'python'
    } else {
        throw 'Python was not found; install 3.12 or specify -PythonExe.'
    }
}
$version = & $pythonCommand @pythonArgs -c 'import sys; print(sys.version_info[0] * 100 + sys.version_info[1])'
if ($LASTEXITCODE -ne 0 -or $version -ne '312') { throw 'Python 3.12 is required.' }
$python = $pythonCommand
if (-not $UseExistingEnvironment) {
    $venv = Join-Path $resolvedRepo '.venv-product'
    if (-not (Test-Path (Join-Path $venv 'Scripts\python.exe'))) {
        & $pythonCommand @pythonArgs -m venv $venv
        if ($LASTEXITCODE -ne 0) { throw 'Failed to create the virtual environment.' }
    }
    $python = Join-Path $venv 'Scripts\python.exe'
}
if ($UseExistingEnvironment) {
    & $python -m pip install --disable-pip-version-check --no-index -r (Join-Path $resolvedRepo 'requirements-product.txt')
} else {
    & $python -m pip install --disable-pip-version-check -r (Join-Path $resolvedRepo 'requirements-product.txt')
}
if ($LASTEXITCODE -ne 0) { throw 'Failed to install product dependencies.' }
New-Item -ItemType Directory -Force -Path $resolvedInstance | Out-Null
Push-Location $resolvedRepo
try {
    $env:PYTHONPATH = Join-Path $resolvedRepo 'src'
    & $python -m backend.product.server admin-exists --instance $resolvedInstance --mode offline
    if ($LASTEXITCODE -ne 0) { throw 'Administrator initialization or instance check failed.' }
    & $python -m backend.product.server serve --instance $resolvedInstance --mode $Mode --port $Port
} finally { Pop-Location }
