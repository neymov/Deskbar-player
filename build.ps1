param([string]$PythonPath = '', [string]$BuildPath = '')
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot
$CoverPath = Join-Path $ProjectRoot 'assets\reference_cover.png'
$IconPath = Join-Path $ProjectRoot 'assets\deskbar.ico'
$ManifestPath = Join-Path $ProjectRoot 'app.manifest'
$WorkspaceRoot = Split-Path -Parent (Split-Path -Parent $ProjectRoot)
$BuildRoot = if ($BuildPath) {
    if ([IO.Path]::IsPathRooted($BuildPath)) { $BuildPath } else { Join-Path $ProjectRoot $BuildPath }
} else { Join-Path $WorkspaceRoot 'work\DeskBar-build' }
New-Item -ItemType Directory -Force -Path $BuildRoot | Out-Null

$Python = if ($PythonPath) { $PythonPath } else { Join-Path $ProjectRoot '.venv\Scripts\python.exe' }
if (-not (Test-Path -LiteralPath $Python)) {
    $EnvPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    $PyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($PyLauncher) {
        & $PyLauncher.Source -3 -m venv (Join-Path $ProjectRoot '.venv')
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the project virtual environment.' }
        $Python = $EnvPython
    } else {
        $PythonCommand = Get-Command python -ErrorAction Stop
        & $PythonCommand.Source -m venv (Join-Path $ProjectRoot '.venv')
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the project virtual environment.' }
        $Python = $EnvPython
    }
}

& $Python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }

& $Python -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name DeskBar --distpath (Join-Path $ProjectRoot 'publish') `
    --workpath (Join-Path $BuildRoot 'work') --specpath (Join-Path $BuildRoot 'spec') `
    --icon $IconPath `
    --manifest $ManifestPath `
    --add-data "$CoverPath;assets" `
    --hidden-import winrt.windows.media.control `
    --hidden-import winrt.windows.storage `
    --hidden-import winrt.windows.storage.streams `
    --hidden-import winrt.windows.foundation `
    deskbar.py
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }
Write-Host "Created $ProjectRoot\publish\DeskBar.exe"
