# Vice installer for Windows: dependencies, a private Python environment,
# Start Menu shortcut and (optionally) recording from login.
#
#   powershell -ExecutionPolicy Bypass -File install.ps1
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall
#
# Run it as your normal user. Nothing here needs administrator rights:
# winget installs ffmpeg per user, cloudflared is a single downloaded exe,
# and everything Vice owns lives under %LOCALAPPDATA%\Vice.
#
# This file is plain ASCII on purpose. Windows PowerShell 5.1 reads a script
# without a byte order mark in the ANSI code page, and a stray non-ASCII
# character is enough to break parsing.

[CmdletBinding()]
param(
    # Remove Vice. Clips are never touched.
    [switch]$Uninstall,
    # Answer yes to every question (unattended installs).
    [switch]$Yes,
    # Do not register recording at login.
    [switch]$NoAutostart,
    # Do not offer cloudflared (public share links).
    [switch]$NoCloudflared,
    # Where Vice's environment goes. The default suits everyone; tests move it.
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'Vice'),
    # Where the Start Menu shortcut goes.
    [string]$ShortcutDir = [Environment]::GetFolderPath('Programs')
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvDir = Join-Path $InstallDir 'venv'
$BinDir = Join-Path $InstallDir 'bin'
$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'
$VenvPythonW = Join-Path $VenvDir 'Scripts\pythonw.exe'
$Shortcut = Join-Path $ShortcutDir 'Vice.lnk'
$IconPath = Join-Path $InstallDir 'vice.ico'

function Info([string]$Message) { Write-Host "[vice] $Message" -ForegroundColor Green }
function Warn([string]$Message) { Write-Host "[vice] $Message" -ForegroundColor Yellow }
function Fail([string]$Message) { Write-Host "[vice] $Message" -ForegroundColor Red; exit 1 }

function Confirm-Step([string]$Question, [bool]$Default = $true) {
    if ($Yes) { return $true }
    $hint = if ($Default) { '[Y/n]' } else { '[y/N]' }
    $answer = Read-Host "[vice] $Question $hint"
    if ([string]::IsNullOrWhiteSpace($answer)) { return $Default }
    return $answer.Trim().ToLower().StartsWith('y')
}

function Update-SessionPath {
    # winget edits the user PATH in the registry; this window only sees it
    # once it is read back.
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$machine;$user"
}

function Invoke-Native {
    # Windows PowerShell 5.1 turns anything a native program writes to stderr
    # into a terminating error while ErrorActionPreference is Stop, so a pip
    # warning or "Vice is not running." would abort the script. Exit codes
    # decide success for native programs instead.
    # Arguments come as an array: through a plain function's $args, PowerShell
    # 5.1 eats "--" as its own end-of-parameters marker, so --version broke.
    param([string]$Exe, [string[]]$Arguments = @())
    $ErrorActionPreference = 'Continue'
    & $Exe @Arguments 2>&1 | ForEach-Object { "$_" } | Write-Host
    return $LASTEXITCODE
}

function Test-Command([string]$Name) {
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Install-Winget([string]$Id, [string]$What) {
    if (-not (Test-Command 'winget')) {
        Fail "$What is missing and winget is not available to install it. Install $What yourself, then run this again."
    }
    Info "Installing $What with winget ($Id)"
    $code = Invoke-Native winget @('install', '--id', $Id, '-e', '--accept-source-agreements', '--accept-package-agreements', '--silent')
    if ($code -ne 0) {
        # winget exits non-zero for "already installed" too, so check the result instead.
        Warn "winget exited with $code while installing $What"
    }
    Update-SessionPath
}

function Find-Python {
    # Prefer the py launcher, which knows every installed version.
    $candidates = @()
    if (Test-Command 'py') { $candidates += ,@('py', '-3') }
    if (Test-Command 'python') { $candidates += ,@('python') }
    foreach ($cmd in $candidates) {
        $exe = $cmd[0]
        $rest = @()
        if ($cmd.Length -gt 1) { $rest = $cmd[1..($cmd.Length - 1)] }
        $out = $null
        try {
            $ErrorActionPreference = 'Continue'
            $out = & $exe @rest -c "import sys; print('%d.%d|%s' % (sys.version_info[0], sys.version_info[1], sys.executable))" 2>$null
        } catch { continue } finally { $ErrorActionPreference = 'Stop' }
        if (-not $out) { continue }
        $version, $path = "$out".Trim().Split('|', 2)
        $major, $minor = $version.Split('.')
        # The Microsoft Store stub prints nothing useful and is skipped above.
        if ([int]$major -eq 3 -and [int]$minor -ge 10) { return $path }
    }
    return $null
}

function Test-FfmpegDdagrab {
    if (-not (Test-Command 'ffmpeg')) { return $false }
    $ErrorActionPreference = 'Continue'
    $filters = & ffmpeg -hide_banner -filters 2>$null | Out-String
    return $filters -match '\bddagrab\b'
}

function Remove-FromUserPath([string]$Dir) {
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    if (-not $user) { return }
    $kept = ($user.Split(';') | Where-Object { $_ -and ($_.TrimEnd('\') -ne $Dir.TrimEnd('\')) }) -join ';'
    if ($kept -ne $user) { [Environment]::SetEnvironmentVariable('Path', $kept, 'User') }
}

function Add-ToUserPath([string]$Dir) {
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $parts = @()
    # @() matters: with a single entry the pipeline yields a plain string, and
    # string + string glued the new folder onto it with no separator, which
    # broke the whole user PATH on a fresh Windows install.
    if ($user) { $parts = @($user.Split(';') | Where-Object { $_ }) }
    if ($parts | Where-Object { $_.TrimEnd('\') -eq $Dir.TrimEnd('\') }) { return }
    [Environment]::SetEnvironmentVariable('Path', (($parts + $Dir) -join ';'), 'User')
    $env:Path = "$env:Path;$Dir"
}

# --- Uninstall --------------------------------------------------------------

if ($Uninstall) {
    Info 'Removing Vice'
    if (Test-Path $VenvPython) {
        Invoke-Native $VenvPython @('-m', 'vice.main', 'stop') | Out-Null
        Invoke-Native $VenvPython @('-m', 'vice.main', 'autostart', '--disable') | Out-Null
    }
    # The window and anything else still running from the venv hold its files
    # open, and Windows will not delete an open file.
    $venvFull = [IO.Path]::GetFullPath($VenvDir)
    Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($venvFull, [StringComparison]::OrdinalIgnoreCase) } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    if (Test-Path $Shortcut) { Remove-Item $Shortcut -Force; Info "Removed $Shortcut" }
    Remove-FromUserPath $BinDir
    foreach ($dir in @($VenvDir, $BinDir)) {
        # The daemon can take a few seconds to let go of its files after it
        # has been asked to stop, so retry instead of aborting half-way.
        for ($try = 0; (Test-Path $dir) -and $try -lt 10; $try++) {
            Remove-Item $dir -Recurse -Force -ErrorAction SilentlyContinue
            if (Test-Path $dir) { Start-Sleep -Seconds 1 }
        }
        if (Test-Path $dir) { Warn "Could not remove $dir; delete it once Vice has exited." }
        else { Info "Removed $dir" }
    }
    if ((Test-Path $InstallDir) -and (Confirm-Step "Also remove logs, playlists and other data in $InstallDir?" $false)) {
        Remove-Item $InstallDir -Recurse -Force
        Info "Removed $InstallDir"
    }
    $config = Join-Path $env:APPDATA 'Vice'
    if ((Test-Path $config) -and (Confirm-Step "Also remove your settings in $config?" $false)) {
        Remove-Item $config -Recurse -Force
        Info "Removed $config"
    }
    Info 'Vice has been removed. Your clips were left where they are.'
    exit 0
}

# --- Install ----------------------------------------------------------------

Info 'Installing Vice for Windows'

if (-not (Test-Path (Join-Path $RepoRoot 'pyproject.toml'))) {
    Fail "Run install.ps1 from a Vice checkout; $RepoRoot has no pyproject.toml."
}

# 1. Python 3.10 or newer.
$python = Find-Python
if (-not $python) {
    if (-not (Confirm-Step 'Python 3.10 or newer is required. Install Python 3.12 with winget?')) {
        Fail 'Python is required. Install it from python.org and run this again.'
    }
    Install-Winget 'Python.Python.3.12' 'Python 3.12'
    $python = Find-Python
    if (-not $python) { Fail 'Python was installed but cannot be found yet. Open a new terminal and run this again.' }
}
Info "Using Python at $python"

# 2. ffmpeg with ddagrab (6.0 or newer) does the capturing and encoding.
if (-not (Test-FfmpegDdagrab)) {
    if (-not (Test-Command 'ffmpeg')) {
        Install-Winget 'Gyan.FFmpeg' 'ffmpeg'
    } else {
        Warn 'The ffmpeg on PATH has no ddagrab filter (it needs 6.0 or newer). Installing a current build.'
        Install-Winget 'Gyan.FFmpeg' 'ffmpeg'
    }
    if (-not (Test-FfmpegDdagrab)) {
        Warn 'ffmpeg with ddagrab is still not on PATH in this window. Open a new terminal before starting Vice.'
    }
} else {
    Info 'ffmpeg with ddagrab is installed'
}

# 3. cloudflared, for share links that work outside your network. Optional.
if ($NoCloudflared) {
    Info 'Skipping cloudflared as asked.'
} elseif (-not (Test-Command 'cloudflared')) {
    if (Confirm-Step 'Install cloudflared for public share links? (Vice works without it)') {
        # Not through winget: its cloudflared package is a machine-wide MSI
        # that asks for administrator rights. Cloudflare also publishes the
        # program as a single exe, which goes in Vice's own bin folder.
        New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
        $arch = if ([Environment]::Is64BitOperatingSystem) { 'amd64' } else { '386' }
        $url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-$arch.exe"
        $target = Join-Path $BinDir 'cloudflared.exe'
        Info "Downloading cloudflared from $url"
        try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            Invoke-WebRequest -Uri $url -OutFile $target -UseBasicParsing
            Info "Installed cloudflared to $target"
        } catch {
            Remove-Item $target -Force -ErrorAction SilentlyContinue
            Warn "Could not download cloudflared ($($_.Exception.Message)). Share links will work on your LAN only."
        }
    } else {
        Warn 'Skipping cloudflared. Turn the public tunnel off in Settings, Sharing, or install it later.'
    }
} else {
    Info 'cloudflared is installed'
}

# 4. A private environment, so Vice's packages never clash with anything else.
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
if (-not (Test-Path $VenvPython)) {
    Info "Creating the Python environment in $VenvDir"
    if ((Invoke-Native $python @('-m', 'venv', $VenvDir)) -ne 0) { Fail 'Could not create the Python environment.' }
}
Info 'Installing Vice and its Python packages (this takes a minute)'
Invoke-Native $VenvPython @('-m', 'pip', 'install', '--disable-pip-version-check', '--quiet', '--upgrade', 'pip') | Out-Null
if ((Invoke-Native $VenvPython @('-m', 'pip', 'install', '--disable-pip-version-check', '--quiet', '--upgrade', $RepoRoot)) -ne 0) {
    Fail 'pip could not install Vice. The output above says why.'
}

# 5. A `vice` command for terminals, and the Start Menu shortcut.
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
# The entry-point exe, not `python -m`: that would put the current directory
# on sys.path, and inside a checkout it would run the checkout, not the install.
$ViceExe = Join-Path $VenvDir 'Scripts\vice.exe'
Set-Content -Path (Join-Path $BinDir 'vice.cmd') -Encoding ASCII -Value "@echo off`r`n`"$ViceExe`" %*"
Add-ToUserPath $BinDir

Copy-Item (Join-Path $RepoRoot 'assets\vice.ico') $IconPath -Force
New-Item -ItemType Directory -Force -Path $ShortcutDir | Out-Null
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($Shortcut)
$link.TargetPath = $VenvPythonW
$link.Arguments = '-m vice.app'
$link.WorkingDirectory = $InstallDir
$link.IconLocation = $IconPath
$link.Description = 'Vice game clip recorder'
$link.Save()
Info "Added the Start Menu shortcut: $Shortcut"

# 6. Record from login, like the systemd service does on Linux.
if (-not $NoAutostart) {
    if (Confirm-Step 'Start recording automatically when you log in?') {
        Invoke-Native $VenvPython @('-m', 'vice.main', 'autostart', '--enable') | Out-Null
    }
}

# 7. Check the install actually imports and sees the machine.
if ((Invoke-Native $ViceExe @('--version')) -ne 0) { Fail 'Vice installed but will not start. Run: vice doctor' }

Write-Host ''
Info 'Done. Open Vice from the Start Menu and press F9 in a game to save a clip.'
Info 'The first time it runs, Windows may ask whether Python can use the network.'
Info 'Allow private networks for share links on your LAN; tunnel links work either way.'
Info "Diagnostics: vice doctor    Uninstall: powershell -ExecutionPolicy Bypass -File `"$($MyInvocation.MyCommand.Path)`" -Uninstall"
