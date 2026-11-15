<#
  Clipping Engine - Windows installer / updater (no administrator rights needed).

  What it does, in order:
    1. Gets the application: from the folder this script sits in (an extracted ZIP), or, when run
       on its own, by downloading the project ZIP from GitHub.
    2. Copies it to a predictable local folder:  %LOCALAPPDATA%\ClippingEngine
       (not inside OneDrive). Your .env, data\, .venv\ and tools\ are never overwritten, so
       running the installer again is also how you UPDATE the engine.
    3. Creates "Clipping Engine" shortcuts on the Desktop and in the Start menu.
    4. Starts the normal launcher with --setup, which installs Python/FFmpeg if missing,
       prepares the speech model, proves the workflow with a real self-test and walks you
       through phone access, notifications and start-at-sign-in.

  Parameters (all optional):
    -InstallDir <path>   where to install            -Branch <name>   which GitHub branch
    -Source <path>       use this local copy instead of downloading
    -NonInteractive      never prompt (automated tests)   -NoStart   install only
    -NoShortcut          do not create shortcuts
#>
param(
  [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'ClippingEngine'),
  [string]$Branch = 'claude/optimistic-noether-k52qjf',
  [string]$Source = '',
  [switch]$NonInteractive,
  [switch]$NoStart,
  [switch]$NoShortcut
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Repo = 'ce-optimisation/n8n-automation-portfolio'

function Say($m) { Write-Host "  $m" }
function Fail($m) {
  Write-Host ""
  Write-Host "  INSTALL FAILED: $m" -ForegroundColor Red
  if (-not $NonInteractive) { Read-Host "  Press Enter to close" | Out-Null }
  exit 1
}

Write-Host ""
Say "Clipping Engine installer"
Say "Install folder: $InstallDir"
if ($InstallDir -match 'OneDrive') {
  Say "WARNING: that folder is inside OneDrive, which would upload every recording. Choose a local folder."
}

# ---- 1. locate or download the application ------------------------------------------------
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$src = $null
$candidates = @()
if ($Source) { $candidates += $Source; $candidates += (Join-Path $Source 'clipping-engine') }
$candidates += (Split-Path -Parent $here)          # scripts\ sits inside clipping-engine\
foreach ($c in $candidates) {
  if ($c -and (Test-Path (Join-Path $c 'Start-Clipping-Engine.cmd')) -and (Test-Path (Join-Path $c 'clipengine'))) {
    $src = (Resolve-Path $c).Path; break
  }
}
$tmp = $null
if (-not $src) {
  $zipUrl = "https://github.com/$Repo/archive/refs/heads/$Branch.zip"
  Say "Downloading the application from GitHub ($Branch) ..."
  $tmp = Join-Path $env:TEMP ("clipengine_install_" + [guid]::NewGuid().ToString('N'))
  New-Item -ItemType Directory -Path $tmp | Out-Null
  $zip = Join-Path $tmp 'project.zip'
  try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $zipUrl -OutFile $zip -UseBasicParsing
    Expand-Archive -Path $zip -DestinationPath $tmp -Force
  } catch {
    Fail "Could not download $zipUrl ($($_.Exception.Message)). Check the internet connection, or download the ZIP in your browser (GitHub -> Code -> Download ZIP), extract it and run Install-Clipping-Engine.cmd from the clipping-engine folder."
  }
  $found = Get-ChildItem -Path $tmp -Directory -Recurse -Filter 'clipping-engine' | Select-Object -First 1
  if (-not $found) { Fail "The downloaded archive does not contain the clipping-engine folder (branch '$Branch')." }
  $src = $found.FullName
}
Say "Source: $src"

# ---- 2. copy (keeps .env, data, .venv, tools) ---------------------------------------------
$dst = $InstallDir
New-Item -ItemType Directory -Force -Path $dst | Out-Null
$dstFull = (Resolve-Path $dst).Path
if ($dstFull.TrimEnd('\') -ieq $src.TrimEnd('\')) {
  Say "Already running from the install folder; nothing to copy."
} else {
  $existing = Test-Path (Join-Path $dstFull 'clipengine')
  Say ($(if ($existing) { "Updating the existing installation (your .env, recordings and settings are kept) ..." } else { "Copying files ..." }))
  & robocopy $src $dstFull /E /NFL /NDL /NJH /NJS /NP /R:2 /W:2 /XD .venv data tools __pycache__ .pytest_cache .review /XF .env | Out-Null
  if ($LASTEXITCODE -ge 8) { Fail "Copying files failed (robocopy code $LASTEXITCODE). Close the Clipping Engine window if it is open and try again." }
  $global:LASTEXITCODE = 0
}
if (-not (Test-Path (Join-Path $dstFull 'Start-Clipping-Engine.cmd'))) { Fail "The launcher is missing from $dstFull after copying." }
"Installed from branch '$Branch' on $(Get-Date -Format 'yyyy-MM-dd HH:mm') (source: $src)" |
  Set-Content -Path (Join-Path $dstFull 'INSTALLED.txt') -Encoding UTF8
if ($tmp) { Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue }

# ---- 3. shortcuts ---------------------------------------------------------------------------
if (-not $NoShortcut) {
  try {
    $shell = New-Object -ComObject WScript.Shell
    $places = @([Environment]::GetFolderPath('Desktop'), (Join-Path ([Environment]::GetFolderPath('Programs')) ''))
    foreach ($p in $places) {
      if (-not $p -or -not (Test-Path $p)) { continue }
      $lnk = $shell.CreateShortcut((Join-Path $p 'Clipping Engine.lnk'))
      $lnk.TargetPath = Join-Path $env:SystemRoot 'System32\cmd.exe'
      $lnk.Arguments = '/c "' + (Join-Path $dstFull 'Start-Clipping-Engine.cmd') + '"'
      $lnk.WorkingDirectory = $dstFull
      $lnk.Description = 'Start the Clipping Engine and open its dashboard'
      $lnk.Save()
    }
    Say "Shortcuts created: Desktop and Start menu -> 'Clipping Engine'"
  } catch { Say "Shortcuts could not be created ($($_.Exception.Message)); start it from $dstFull\Start-Clipping-Engine.cmd" }
}

Say "Installed in $dstFull"
if ($NoStart) { exit 0 }

# ---- 4. first start with the setup assistant -------------------------------------------------
if ($NonInteractive) { $env:CLIPENGINE_NONINTERACTIVE = '1'; $env:CLIPENGINE_AUTO_FFMPEG = '1' }
Say "Starting the setup assistant ..."
& (Join-Path $dstFull 'Start-Clipping-Engine.cmd') --setup
exit $LASTEXITCODE
