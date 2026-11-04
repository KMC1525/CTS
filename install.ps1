# CTS (Comic To Story) installer for Windows (PowerShell 5.1+).
#
#   irm https://raw.githubusercontent.com/KMC1525/CTS/main/install.ps1 | iex
#
# What it does: installs `uv` (a tiny Python manager) if needed, creates a private Python 3.12
# environment, installs CTS and every library it needs, downloads the llama.cpp engine for local
# models, and adds CTS to the Start Menu.
#
# Options when running the file:  .\install.ps1 -NoRuntime -NoDesktop -Dev -Uninstall -Purge
# Environment: CTS_REF (branch/tag, default main)  CTS_INSTALL_HOME (default %LOCALAPPDATA%\Programs\CTS)

function Install-CTS {
    param(
        [switch]$NoRuntime,
        [switch]$NoDesktop,
        [switch]$Dev,
        [switch]$Uninstall,
        [switch]$Purge
    )
    # 'Continue' on purpose: in Windows PowerShell 5.1, 'Stop' turns anything a native tool (uv, pip)
    # writes to stderr into a terminating error. Failures are detected with $LASTEXITCODE instead.
    $ErrorActionPreference = 'Continue'
    $ProgressPreference = 'SilentlyContinue'
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

    $Repo = 'KMC1525/CTS'
    $Ref = if ($env:CTS_REF) { $env:CTS_REF } else { 'main' }
    $PyVersion = if ($env:CTS_PYTHON) { $env:CTS_PYTHON } else { '3.12' }
    $InstallHome = if ($env:CTS_INSTALL_HOME) { $env:CTS_INSTALL_HOME } else { Join-Path $env:LOCALAPPDATA 'Programs\CTS' }
    $Venv = Join-Path $InstallHome 'venv'
    $BinDir = Join-Path $InstallHome 'bin'
    $VenvPython = Join-Path $Venv 'Scripts\python.exe'
    $CtsExe = Join-Path $Venv 'Scripts\cts.exe'
    $CtsGui = Join-Path $Venv 'Scripts\cts-gui.exe'

    function Say($m)  { Write-Host "==> $m" -ForegroundColor White }
    function Warn($m) { Write-Host "!!  $m" -ForegroundColor Yellow }
    function Fail($m) { Write-Host "xx  $m" -ForegroundColor Red; throw $m }
    function Run-Native([scriptblock]$Block, [string]$Message) {
        & $Block
        if ($LASTEXITCODE -ne 0) { Fail $Message }
    }
    # Everything after the first element (PowerShell's 1..0 range would wrap around on 1-item arrays).
    function Tail($items) { if ($items.Count -gt 1) { return @($items[1..($items.Count - 1)]) } else { return @() } }

    # ---------------------------------------------------------------- uninstall
    if ($Uninstall) {
        Say 'Removing CTS'
        if (Test-Path $CtsExe) {
            try { & $CtsExe stop | Out-Null } catch { }
            & $CtsExe uninstall-desktop
            if ($Purge) { & $CtsExe purge --yes }
        }
        # drop our bin folder from the user PATH
        $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
        if ($userPath) {
            $kept = ($userPath -split ';' | Where-Object { $_ -and ($_ -ne $BinDir) }) -join ';'
            [Environment]::SetEnvironmentVariable('Path', $kept, 'User')
        }
        if (Test-Path $InstallHome) { Remove-Item -Recurse -Force $InstallHome }
        Say 'CTS was removed.'
        if (-not $Purge) { Write-Host 'Your settings, models and novels were kept. Use -Uninstall -Purge to delete them too.' }
        return
    }

    # ------------------------------------------------------------------- source
    $Src = $null
    if ($PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot 'pyproject.toml')) -and (Test-Path (Join-Path $PSScriptRoot 'cts'))) {
        $Src = $PSScriptRoot
        Say "Installing CTS from this folder ($Src)"
    } else {
        $Src = "https://github.com/$Repo/archive/$Ref.zip"
        Say "Installing CTS ($Ref) from GitHub"
    }

    # ----------------------------------------------------------------------- uv
    $uvDirs = @("$env:USERPROFILE\.local\bin", "$env:USERPROFILE\.cargo\bin")
    foreach ($d in $uvDirs) { if (Test-Path $d) { $env:Path = "$d;$env:Path" } }
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Say 'Installing uv (Python manager)'
        try {
            Invoke-RestMethod -Uri https://astral.sh/uv/install.ps1 -ErrorAction Stop | Invoke-Expression
        } catch {
            Warn "Could not install uv automatically: $($_.Exception.Message)"
        }
        foreach ($d in $uvDirs) { if (Test-Path $d) { $env:Path = "$d;$env:Path" } }
    }
    $hasUv = [bool](Get-Command uv -ErrorAction SilentlyContinue)

    New-Item -ItemType Directory -Force -Path $InstallHome, $BinDir -ErrorAction Stop | Out-Null
    $PipArgs = @($Src)
    if ($Dev -and (Test-Path $Src -PathType Container)) { $PipArgs = @('-e', $Src) }

    if ($hasUv) {
        Say "Creating a private Python $PyVersion environment"
        Run-Native { uv venv --python $PyVersion --allow-existing $Venv } 'uv could not create the Python environment.'
        Say 'Installing CTS and its libraries (CrewAI, FastAPI, Pillow...). This can take a few minutes.'
        Run-Native { uv pip install --python $VenvPython --upgrade @PipArgs } 'Installing the Python packages failed.'
    } else {
        Warn 'uv is unavailable - falling back to your system Python.'
        $py = $null
        foreach ($cmd in @('py -3.12', 'py -3.13', 'py -3.11', 'py -3.10', 'python')) {
            $parts = @($cmd -split ' ')
            if (Get-Command $parts[0] -ErrorAction SilentlyContinue) {
                $argsList = @(Tail $parts) + @('-c', 'import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] < (3,14) else 1)')
                & $parts[0] @argsList | Out-Null
                if ($LASTEXITCODE -eq 0) { $py = $parts; break }
            }
        }
        if (-not $py) { Fail 'Python 3.10-3.13 is required. Install it from python.org (tick "Add to PATH") and run this again.' }
        $pyArgs = @(Tail $py) + @('-m', 'venv', $Venv)
        Run-Native { & $py[0] @pyArgs } 'Could not create the virtual environment.'
        Run-Native { & $VenvPython -m pip install --upgrade pip } 'Could not upgrade pip.'
        Run-Native { & $VenvPython -m pip install --upgrade @PipArgs } 'Installing the Python packages failed.'
    }

    # `cts` command on PATH (a tiny wrapper so the venv's python.exe does not shadow yours)
    Set-Content -Path (Join-Path $BinDir 'cts.cmd') -Encoding ASCII -ErrorAction Stop -Value "@echo off`r`n`"$CtsExe`" %*"
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if (-not (($userPath -split ';') -contains $BinDir)) {
        [Environment]::SetEnvironmentVariable('Path', (($userPath, $BinDir | Where-Object { $_ }) -join ';'), 'User')
        $env:Path = "$BinDir;$env:Path"
    }

    # -------------------------------------------------------- llama.cpp (local AI)
    if (-not $NoRuntime) {
        Say 'Installing the llama.cpp engine (runs local GGUF models)'
        & $CtsExe setup-runtime
        if ($LASTEXITCODE -ne 0) { Warn 'Skipped. You can install it later from Settings > Local models.' }
    }

    # --------------------------------------------------------------- Start Menu
    if (-not $NoDesktop) {
        Say 'Adding CTS to the Start Menu'
        $exec = if (Test-Path $CtsGui) { $CtsGui } else { $CtsExe }
        & $CtsExe install-desktop --exec $exec
        if ($LASTEXITCODE -ne 0) { Warn 'Could not create the Start Menu shortcut.' }
    }

    Say 'CTS is installed.'
    Write-Host 'Open it from the Start Menu (search for CTS), or open a new terminal and run:  cts'
}

# Run with the script's own arguments when invoked as a file (iex passes none).
$cliArgs = @{}
foreach ($a in $args) {
    switch -Regex ($a) {
        '^-NoRuntime$'  { $cliArgs.NoRuntime = $true }
        '^-NoDesktop$'  { $cliArgs.NoDesktop = $true }
        '^-Dev$'        { $cliArgs.Dev = $true }
        '^-Uninstall$'  { $cliArgs.Uninstall = $true }
        '^-Purge$'      { $cliArgs.Purge = $true }
    }
}
Install-CTS @cliArgs
