<#
  Maestro installer for Windows. Native: no WSL, no Linux, no admin, no restart.

  What it sets up, skipping whatever is already there:
    1. Git for Windows (per user). Claude Code needs its Bash; Maestro its git.
    2. uv, which brings its own Python.
    3. Claude Code (Anthropic's native installer).
    4. Maestro itself, then `maestro init`: profiles, the `maestro` skill and
       the MCP server that lets Claude Code (VS Code or terminal) drive it.
    5. The Claude sign-in: a window opens and the browser signs you in.
    6. Maestro starts and its panel opens in a window of its own; a Start menu
       entry opens it again later.

  Usage:  install.ps1 [-Uninstall] [-Yes] [-NoOpen] [-Version <x.y.z>] [-Source <spec>]
  -Version installs that release (MaestroSetup.exe passes its own); without it,
  the latest code on GitHub. -Source overrides both (a local checkout, a wheel).
  Runs again safely: every step checks before it acts.
#>
param(
    [switch]$Uninstall,
    [switch]$Yes,                 # answer every question with yes (silent installs)
    [switch]$NoOpen,
    [string]$Version = "",
    [string]$Source = ""
)
if (-not $Source) {
    $Source = "git+https://github.com/daniel-madrid-07/Maestro"
    if ($Version) { $Source += "@v$Version" }
}

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppDir = Join-Path $env:LOCALAPPDATA "Maestro"
$Tool = "maestro-orchestrator"
New-Item -ItemType Directory -Force $AppDir | Out-Null
$Log = Join-Path $AppDir "install.log"
Start-Transcript -Path $Log -Append | Out-Null

function Step($text) { Write-Host ""; Write-Host "==> $text" -ForegroundColor Cyan }
function Ok($text)   { Write-Host "    ok  $text" -ForegroundColor Green }
function Info($text) { Write-Host "    $text" }
function Fail($text) {
    Write-Host ""; Write-Host "    error: $text" -ForegroundColor Red
    Write-Host "    The full log is at $Log"
    Stop-Transcript | Out-Null
    if (-not $Yes) { [void](Read-Host "    Press Enter to close") }
    exit 1
}
function Ask($title, $text) {
    if ($Yes) { return $true }
    Add-Type -AssemblyName PresentationFramework
    return [System.Windows.MessageBox]::Show($text, $title, "YesNo", "Question") -eq "Yes"
}
function Native([scriptblock]$Block) {
    # Windows PowerShell turns a native program's stderr into an error record,
    # and under "Stop" the first progress line uv prints would end the script.
    # Exit codes are what count; callers check $LASTEXITCODE.
    $saved = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $Block 2>&1 | ForEach-Object { "$_" } } finally { $ErrorActionPreference = $saved }
}
function Refresh-Path {
    # What an installer added to the user or machine PATH, visible to this
    # process without opening a new window. ~\.local\bin first: uv and Claude
    # Code both install there, and it may not be on the stored PATH yet.
    $stored = @([Environment]::GetEnvironmentVariable("Path", "User"),
                [Environment]::GetEnvironmentVariable("Path", "Machine")) -join ";"
    $env:Path = (Join-Path $env:USERPROFILE ".local\bin") + ";" + $stored + ";" + $env:Path
}
function Find($name) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cmd) { return $cmd.Source } else { return $null }
}
function Run-Remote-Script($url) {
    # The official one-line installers (`irm ... | iex`), each in a child
    # PowerShell: a script that calls `exit` must not end this one.
    $code = "`$ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol='Tls12'; irm '$url' | iex"
    Native { & powershell.exe -NoProfile -ExecutionPolicy Bypass -Command $code } | Out-Host
    return $LASTEXITCODE
}
function Tool-Python {
    # The interpreter of Maestro's own uv environment. pythonw.exe runs it
    # with no console window (the Start menu entry uses it).
    $dir = (Native { & uv tool dir } | Select-Object -Last 1)
    if ($dir) { $dir = $dir.Trim() }
    if ($dir) { return Join-Path $dir "$Tool\Scripts" } else { return $null }
}
function Signed-In {
    $claude = Find "claude"
    if (-not $claude) { return $false }
    $out = (Native { & $claude auth status --json } | Out-String)
    return $out -match '"loggedIn"\s*:\s*true'
}

Refresh-Path

# ============================================================== uninstall
if ($Uninstall) {
    Step "Removing Maestro"
    $maestro = Find "maestro"
    if ($maestro) { Native { & $maestro down } | Out-Null }
    $claude = Find "claude"
    if ($claude) {
        Native { & $claude mcp remove maestro -s user } | Out-Null
        Ok "MCP server unregistered from Claude Code"
    }
    # The skill and the scout subagent only when they are still exactly what
    # `maestro init` wrote; anything the user edited stays.
    $scripts = Tool-Python
    $pkg = if ($scripts) { Join-Path (Split-Path $scripts) "Lib\site-packages\maestro" } else { $null }
    $claudeDir = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $env:USERPROFILE ".claude" }
    foreach ($pair in @(@("skill\SKILL.md", "skills\maestro\SKILL.md"), @("agents\scout.md", "agents\scout.md"))) {
        $ours = if ($pkg) { Join-Path $pkg $pair[0] } else { $null }
        $theirs = Join-Path $claudeDir $pair[1]
        if ($ours -and (Test-Path $ours) -and (Test-Path $theirs) -and
            ((Get-FileHash $ours).Hash -eq (Get-FileHash $theirs).Hash)) {
            Remove-Item -Force $theirs
            Ok "removed $theirs"
        }
    }
    $skillDir = Join-Path $claudeDir "skills\maestro"
    if ((Test-Path $skillDir) -and -not (Get-ChildItem $skillDir)) { Remove-Item $skillDir }
    if (Find "uv") {
        Native { & uv tool uninstall $Tool } | Out-Null
        Ok "Maestro uninstalled"
    }
    Remove-Item -Force (Join-Path ([Environment]::GetFolderPath("Programs")) "Maestro.lnk") -ErrorAction SilentlyContinue
    Info "Kept: Git, uv, Claude Code (other tools may use them) and ~\.maestro (settings and logs)."
    Stop-Transcript | Out-Null
    exit 0
}

Write-Host ""
Write-Host "  Maestro installer" -ForegroundColor White
Write-Host "  Runs many Claude Code sessions at once. Nothing to type: answer the windows that appear."

# ============================================================== 1. Git
Step "Git for Windows"
$git = Find "git"
if (-not $git) {
    foreach ($p in @("$env:ProgramFiles\Git\cmd\git.exe", "$env:LOCALAPPDATA\Programs\Git\cmd\git.exe")) {
        if (Test-Path $p) { $git = $p; $env:Path = (Split-Path $p) + ";" + $env:Path; break }
    }
}
if ($git) {
    Ok ((Native { & $git --version }) -join "")
} else {
    Info "Downloading the latest Git for Windows..."
    $rel = Invoke-RestMethod "https://api.github.com/repos/git-for-windows/git/releases/latest" -Headers @{ "User-Agent" = "maestro-installer" }
    $asset = $rel.assets | Where-Object { $_.name -match '^Git-[\d.]+-64-bit\.exe$' } | Select-Object -First 1
    if (-not $asset) { Fail "could not find the Git for Windows installer on GitHub" }
    $exe = Join-Path $AppDir $asset.name
    Invoke-WebRequest $asset.browser_download_url -OutFile $exe -UseBasicParsing
    if ($asset.digest -match '^sha256:(.+)$') {
        if ((Get-FileHash $exe -Algorithm SHA256).Hash -ne $Matches[1].ToUpper()) {
            Remove-Item $exe; Fail "the Git download is corrupt (checksum mismatch); run the installer again"
        }
        Ok "checksum verified"
    }
    Info "Installing Git (per user, a minute or two)..."
    $p = Start-Process $exe -ArgumentList "/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES", "/SP-", "/CURRENTUSER", "/NOCANCEL" -Wait -PassThru
    Remove-Item $exe -ErrorAction SilentlyContinue
    if ($p.ExitCode -ne 0) { Fail "the Git installer exited with code $($p.ExitCode)" }
    Refresh-Path
    $git = Find "git"
    if (-not $git) {
        $p = "$env:LOCALAPPDATA\Programs\Git\cmd\git.exe"
        if (Test-Path $p) { $git = $p; $env:Path = (Split-Path $p) + ";" + $env:Path }
    }
    if (-not $git) { Fail "Git was installed but cannot be found; open a new window and run the installer again" }
    Ok ((Native { & $git --version }) -join "")
}

# ============================================================== 2. uv
Step "uv (Python tooling)"
if (Find "uv") {
    Ok ((Native { & uv --version }) -join "")
} else {
    Info "Installing uv..."
    if ((Run-Remote-Script "https://astral.sh/uv/install.ps1") -ne 0) { Fail "installing uv failed" }
    Refresh-Path
    if (-not (Find "uv")) { Fail "uv was installed but cannot be found" }
    Ok ((Native { & uv --version }) -join "")
}

# ============================================================== 3. Claude Code
Step "Claude Code"
if (Find "claude") {
    Ok ((Native { & (Find "claude") --version }) -join "")
} else {
    Info "Installing Claude Code..."
    if ((Run-Remote-Script "https://claude.ai/install.ps1") -ne 0) { Fail "installing Claude Code failed" }
    Refresh-Path
    if (-not (Find "claude")) { Fail "Claude Code was installed but cannot be found" }
    Ok ((Native { & (Find "claude") --version }) -join "")
}

# ============================================================== 4. Maestro
Step "Maestro"
Info "Installing from $Source ..."
# --force: a re-run upgrades in place. uv fetches a Python of its own if needed.
Native { & uv tool install --force --python 3.13 $Source } | Out-Host
if ($LASTEXITCODE -ne 0) { Fail "installing Maestro failed" }
Native { & uv tool update-shell } | Out-Null
Refresh-Path
$maestro = Find "maestro"
if (-not $maestro) { Fail "Maestro was installed but the 'maestro' command cannot be found" }
Ok ((Native { & $maestro --version }) -join "")
Native { & $maestro init } | Out-Host

# ============================================================== 5. sign-in
Step "Claude account"
if (Signed-In) {
    Ok "already signed in"
} else {
    Info "A window opens to sign in: your browser shows the Claude sign-in page."
    Info "If it asks for a code, paste it into that window."
    Start-Process (Find "claude") -ArgumentList "auth", "login"
    $deadline = (Get-Date).AddMinutes(15)
    while (-not (Signed-In)) {
        if ((Get-Date) -gt $deadline) { Fail "still not signed in after 15 minutes; run the installer again" }
        Start-Sleep -Seconds 3
    }
    Ok "signed in"
}

# ============================================================== 6. start
Step "Starting Maestro"
Native { & $maestro doctor } | Out-Host
Native { & $maestro up --no-open } | Out-Host
if ($LASTEXITCODE -ne 0) { Fail "Maestro did not start; the lines above say why" }

$scripts = Tool-Python
$pythonw = if ($scripts) { Join-Path $scripts "pythonw.exe" } else { $null }
$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath("Programs")) "Maestro.lnk"))
if ($pythonw -and (Test-Path $pythonw)) {
    # `maestro up` starts the server when it is down and opens the panel either way.
    $lnk.TargetPath = $pythonw
    $lnk.Arguments = "-m maestro.cli up"
} else {
    $lnk.TargetPath = $maestro
    $lnk.Arguments = "up"
    $lnk.WindowStyle = 7  # minimised
}
$icon = Join-Path $Here "maestro.ico"
if (Test-Path $icon) { Copy-Item $icon $AppDir -Force; $lnk.IconLocation = (Join-Path $AppDir "maestro.ico") }
$lnk.WorkingDirectory = $env:USERPROFILE
$lnk.Description = "Open the Maestro panel"
$lnk.Save()
Ok "Start menu entry 'Maestro' created"

if (-not $NoOpen) { Native { & $maestro open } | Out-Null }

Write-Host ""
Write-Host "  Maestro is ready." -ForegroundColor Green
Write-Host "  In Claude Code (VS Code or terminal), say: use Maestro to build <what you want>."
Write-Host "  The panel is open; the Start menu entry 'Maestro' reopens it."
Stop-Transcript | Out-Null
if (-not $Yes) { Start-Sleep -Seconds 4 }
