@echo off
setlocal DisableDelayedExpansion
set "AI_MULTI_INSTANCE_INSTALLER=%~f0"
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -Command "$parts = [IO.File]::ReadAllText($env:AI_MULTI_INSTANCE_INSTALLER) -split '(?m)^# Installer\r?$', 2; & ([scriptblock]::Create($parts[1]))"
set "INSTALL_EXIT=%ERRORLEVEL%"
echo(
pause
exit /b %INSTALL_EXIT%

# Installer
param([string]$DesktopDirectory = [Environment]::GetFolderPath('DesktopDirectory'))

$ErrorActionPreference = 'Stop'

function Find-Git {
    $git = Get-Command git.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($git) { return $git.Source }

    foreach ($key in 'HKCU:\SOFTWARE\GitForWindows', 'HKLM:\SOFTWARE\GitForWindows') {
        $root = (Get-ItemProperty -LiteralPath $key -Name InstallPath -ErrorAction SilentlyContinue).InstallPath
        if ($root) {
            $gitPath = Join-Path $root 'cmd\git.exe'
            if (Test-Path -LiteralPath $gitPath -PathType Leaf) { return $gitPath }
        }
    }
}

function Find-Python {
    $ErrorActionPreference = 'Continue'
    $env:PYTHON_MANAGER_AUTOMATIC_INSTALL = 'false'
    foreach ($tool in Get-Command python.exe, py.exe -CommandType Application -All -ErrorAction SilentlyContinue) {
        $result = & $tool.Source -I -c 'import json, sys; assert (3, 9) <= sys.version_info < (4,); print(json.dumps(dict(path=sys.executable)))' 2>$null
        if ($LASTEXITCODE -eq 0 -and $result) {
            return (ConvertFrom-Json -InputObject $result).path
        }
    }
}

function Install-Tool($name, $package) {
    $winget = Get-Command winget.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $winget) {
        throw "WinGet is needed to install $name. Install or update 'App Installer' in the Microsoft Store, then run this setup again."
    }

    Write-Host "Installing $name with WinGet..."
    & $winget.Source install --id $package --exact --source winget --scope user --no-upgrade --silent --disable-interactivity --accept-source-agreements --accept-package-agreements
    if ($LASTEXITCODE -ne 0) {
        throw "WinGet could not install $name (code $LASTEXITCODE). Check the message above, or install $name manually and run setup again."
    }

    # This window keeps its original PATH until you refresh it.
    $env:Path = $env:Path + ';' + [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
}

Write-Host 'AI Multi-Instance setup'
Write-Host ''

try {
    if (-not $DesktopDirectory -or -not [IO.Directory]::Exists($DesktopDirectory)) {
        throw 'Windows could not find your Desktop folder. Check that it is available and try again.'
    }
    $desktop = [IO.Path]::GetFullPath($DesktopDirectory)
    $project = Join-Path $desktop 'ai-multi-instance'
    $installed = Get-Item -LiteralPath $project -Force -ErrorAction SilentlyContinue
    if ($installed) {
        foreach ($file in 'main.py', 'engine.py', 'usage.py', 'launcher.pyw') {
            if (-not (Test-Path -LiteralPath (Join-Path $project $file) -PathType Leaf)) {
                throw "This location is already in use: $project. Move that item to another location and run setup again."
            }
        }
        Write-Host 'AI Multi-Instance is already installed on your desktop.'
    }

    $git = Find-Git
    if ($git) {
        Write-Host 'Git is already available.'
    } else {
        Install-Tool 'Git' 'Git.Git'
        $git = Find-Git
        if (-not $git) { throw 'Git is still unavailable. Reopen setup or check that Git is on your PATH.' }
    }

    $python = Find-Python
    if ($python) {
        Write-Host 'Python 3.9 or newer is already available.'
    } else {
        Install-Tool 'Python' 'Python.Python.3.14'
        $python = Find-Python
        if (-not $python) { throw 'Python is still unavailable. Reopen setup or check that Python 3.9+ is on your PATH.' }
    }

    if (-not $installed) {
        Write-Host 'Downloading AI Multi-Instance...'
        $stage = Join-Path $desktop ('.ai-multi-instance-' + [guid]::NewGuid().ToString('N'))
        try {
            $env:GIT_TERMINAL_PROMPT = '0'
            & $git clone --depth 1 --branch main -- https://github.com/Zoltak-Dev/ai-multi-instance.git $stage
            if ($LASTEXITCODE -ne 0) { throw 'The download failed. Check your internet connection and run setup again.' }
            [IO.Directory]::Move($stage, $project)
        } finally {
            if ([IO.Directory]::Exists($stage) -and [IO.Path]::GetDirectoryName($stage) -eq $desktop) {
                Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
    }

    Write-Host ''
    Write-Host "Ready. You'll find the project folder on your desktop: $project"

    $linkPath = Join-Path $desktop 'AI Multi-Instance.lnk'
    $entryPoint = Join-Path $project 'main.py'
    try {
        $shell = New-Object -ComObject WScript.Shell
        $link = $shell.CreateShortcut($linkPath)
        if (-not (Get-Item -LiteralPath $linkPath -Force -ErrorAction SilentlyContinue)) {
            $link.TargetPath = $python
            $link.Arguments = '"' + $entryPoint + '"'
            $link.WorkingDirectory = $project
            $link.Save()
        }
        if ($link.TargetPath -eq $python -and $link.Arguments -eq ('"' + $entryPoint + '"')) {
            Write-Host 'Open the AI Multi-Instance shortcut on your desktop to get started.'
        } else {
            throw 'A different desktop shortcut already uses that name.'
        }
    } catch {
        Write-Host $_.Exception.Message
        Write-Host ("To start the manager, run this in PowerShell: & '{0}' '{1}'" -f $python.Replace("'", "''"), $entryPoint.Replace("'", "''"))
    }
} catch {
    Write-Host "Setup stopped. $($_.Exception.Message)"
    exit 1
}
