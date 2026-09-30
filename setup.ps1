<#
.SYNOPSIS
  One-step, idempotent setup of the UEFN MCP server on a Windows machine: THE onboarding step for a teammate.

.DESCRIPTION
  Run it from the clone once per machine, and again after moving the clone or after a Fortnite update. Every step
  checks first and changes only what is missing; -DryRun reports every step and changes nothing.

    1. Finds a real host Python 3.10+ (never the Microsoft Store alias) and pip-installs requirements.txt (the mcp
       SDK). -WithMss adds mss (only the old screenshot_desktop tool needs it; the desktop_* tools do not).
    2. -WithTray: installs pystray + Pillow into .\vendor with UEFN's embedded Python (listener tray icon).
    3. Sets the user environment variable UEFN_MCP_PATH to this folder.
    4. Runs ensure_mcp_hook.ps1: the listener autostart hook in Epic's EditorToolset init_unreal.py.
       -ScheduleHook also registers the hourly task "UEFN-MCP-hook" that re-adds the hook after Fortnite updates.
    5. Reports the UEFN side: install and Epic launcher entry, "Load on Startup", whether Python is enabled for
       -Project (or the last opened project), the hook.
    6. -EnableLoadLastProject: explicit opt-in, sets UEFN's "Load on Startup" to Most Recent Project so a relaunch
       reopens the project by itself (UEFN must be closed). Never changed otherwise.
    7. -RegisterClaude: registers the server for Claude Code at user scope (claude mcp add uefn -s user ...).
    8. Prints what is left to do by hand.

  Nothing machine-specific is written into the repo; paths come from the Epic launcher manifests, environment
  variables and the current user's folders.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1 -DryRun
.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1 -ScheduleHook -Project "<path>\MyIsland.uefnproject"
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [string]$Python,
    [string]$Project,
    [switch]$WithMss,
    [switch]$WithTray,
    [switch]$ScheduleHook,
    [switch]$EnableLoadLastProject,
    [switch]$RegisterClaude,
    [switch]$SkipPip
)

# Native tools write to stderr on normal "not found" answers; errors are checked through exit codes.
$ErrorActionPreference = 'Continue'
$repo = $PSScriptRoot
$todo = New-Object System.Collections.Generic.List[string]
$failed = $false

function Step([string]$text) { Write-Output ''; Write-Output "== $text" }
function Info([string]$text) { Write-Output "   $text" }
function Would([string]$text) { Write-Output "   [dry-run] would: $text" }

function Test-Python([string]$exe) {
    if (-not $exe -or -not (Test-Path -LiteralPath $exe)) { return $null }
    if ($exe -like '*\WindowsApps\*') { return $null }   # Microsoft Store alias: Claude Code cannot start it
    $out = & $exe -c "import sys; print('%d.%d' % sys.version_info[:2]); print(sys.executable)" 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $out -or @($out).Count -lt 2) { return $null }
    try { if ([version]$out[0] -lt [version]'3.10') { return $null } } catch { return $null }
    return @{ Exe = $out[1].Trim(); Version = $out[0].Trim() }
}

function Find-Python {
    $candidates = New-Object System.Collections.Generic.List[string]
    if ($Python) { $candidates.Add($Python) }
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        $p = & $launcher.Source -3 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $p) { $candidates.Add(([string]$p).Trim()) }
    }
    foreach ($c in @(Get-Command python, python3 -All -ErrorAction SilentlyContinue)) { $candidates.Add($c.Source) }
    foreach ($root in 'HKCU:\Software\Python\PythonCore', 'HKLM:\Software\Python\PythonCore') {
        foreach ($ver in @(Get-ChildItem -Path $root -ErrorAction SilentlyContinue | Sort-Object PSChildName -Descending)) {
            $ip = Get-ItemProperty -Path (Join-Path $ver.PSPath 'InstallPath') -ErrorAction SilentlyContinue
            if (-not $ip) { continue }
            if ($ip.ExecutablePath) { $candidates.Add($ip.ExecutablePath) }
            elseif ($ip.'(default)') { $candidates.Add((Join-Path $ip.'(default)' 'python.exe')) }
        }
    }
    foreach ($c in $candidates) {
        $r = Test-Python $c
        if ($r) { return $r }
    }
    return $null
}

function Invoke-Session([string[]]$arguments) {
    $raw = & $script:py.Exe (Join-Path $repo 'uefn_session.py') @arguments 2>$null | Out-String
    try { return $raw | ConvertFrom-Json } catch { return [pscustomobject]@{ error = "unexpected output: $raw" } }
}

Write-Output "UEFN MCP server setup$(if ($DryRun) { ' (dry run: nothing is changed)' })"
Info "repo: $repo"

# 1. Python ---------------------------------------------------------------------------------------------------
Step 'Host Python (runs mcp_server.py for Claude Code)'
$script:py = Find-Python
if (-not $script:py) {
    Info 'No Python 3.10+ found (the Microsoft Store alias does not count).'
    Info 'Install one from python.org (tick "Add to PATH"), or pass -Python <path to python.exe>, then run again.'
    exit 1
}
Info "Python $($script:py.Version): $($script:py.Exe)"

Step 'Python packages'
if ($SkipPip) {
    Info 'skipped (-SkipPip)'
} else {
    $pipArgs = @('-m', 'pip', 'install', '--disable-pip-version-check', '-q', '-r', (Join-Path $repo 'requirements.txt'))
    if ($WithMss) { $pipArgs += 'mss>=10' }
    & $script:py.Exe -c "import mcp.server.fastmcp" 2>$null
    $hasMcp = ($LASTEXITCODE -eq 0)
    if ($DryRun) {
        Would "$($script:py.Exe) $($pipArgs -join ' ')$(if ($hasMcp) { '  (mcp is already installed)' })"
    } else {
        & $script:py.Exe @pipArgs
        if ($LASTEXITCODE -ne 0) { & $script:py.Exe @($pipArgs[0..2] + '--user' + $pipArgs[3..($pipArgs.Count - 1)]) }
        & $script:py.Exe -c "import mcp.server.fastmcp" 2>$null
        if ($LASTEXITCODE -eq 0) { Info 'mcp SDK: OK' } else { Info 'mcp SDK: pip install FAILED'; $failed = $true }
    }
}

# UEFN install (from the Epic launcher manifests; used by the next steps) ------------------------------------------
$install = Invoke-Session @('find-install')
$fortniteDir = $install.fortnite_dir

# 2. Tray icon deps --------------------------------------------------------------------------------------------------
if ($WithTray) {
    Step 'Listener tray icon (pystray + Pillow for UEFN''s embedded Python)'
    $uefnPy = if ($fortniteDir) { Join-Path $fortniteDir 'Engine\Binaries\ThirdParty\Python3\Win64\python.exe' } else { $null }
    if (-not $uefnPy -or -not (Test-Path -LiteralPath $uefnPy)) {
        $todo.Add('Tray icon: UEFN''s embedded python.exe was not found (Fortnite not installed?).')
    } elseif (Test-Path -LiteralPath (Join-Path $repo 'vendor\pystray')) {
        Info 'already installed in vendor\'
    } elseif ($DryRun) {
        Would "$uefnPy -m pip install --target `"$repo\vendor`" pystray Pillow"
    } else {
        & $uefnPy -m pip install --disable-pip-version-check -q --target (Join-Path $repo 'vendor') pystray Pillow
        if ($LASTEXITCODE -ne 0) { $todo.Add('Tray icon: pip install into vendor\ failed (optional).') }
    }
}

# 3. UEFN_MCP_PATH ---------------------------------------------------------------------------------------------------
Step 'User environment variable UEFN_MCP_PATH'
$current = [Environment]::GetEnvironmentVariable('UEFN_MCP_PATH', 'User')
$same = $false
if ($current) {
    $resolved = Resolve-Path -LiteralPath $current -ErrorAction SilentlyContinue
    $same = $resolved -and ($resolved.Path.TrimEnd('\') -ieq $repo.TrimEnd('\'))
}
if ($same) {
    Info "already set to $repo"
} elseif ($DryRun) {
    Would "set UEFN_MCP_PATH=$repo (currently: $(if ($current) { $current } else { 'not set' }))"
} else {
    [Environment]::SetEnvironmentVariable('UEFN_MCP_PATH', $repo, 'User')
    $env:UEFN_MCP_PATH = $repo
    Info "set to $repo"
    $todo.Add('Restart terminals, IDEs and Claude Code so they see UEFN_MCP_PATH.')
}

# 4. Autostart hook --------------------------------------------------------------------------------------------------
Step 'Listener autostart hook (ensure_mcp_hook.ps1)'
$hookScript = Join-Path $repo 'ensure_mcp_hook.ps1'
$hookArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $hookScript)
if ($fortniteDir) { $hookArgs += @('-FortniteDir', $fortniteDir) }
if ($DryRun) { $hookArgs += '-DryRun' }
$hookOut = & powershell.exe @hookArgs 2>&1 | Out-String
$hookCode = $LASTEXITCODE
foreach ($line in ($hookOut -split "`r?`n")) { if ($line.Trim()) { Info $line.Trim() } }
if ($hookCode -ne 0) {
    $todo.Add('Autostart hook: ensure_mcp_hook.ps1 failed (Program Files is write-protected?): run setup.ps1 once from an elevated PowerShell.')
}
$taskExists = $false
& schtasks.exe /Query /TN 'UEFN-MCP-hook' *> $null
if ($LASTEXITCODE -eq 0) { $taskExists = $true }
if ($ScheduleHook) {
    $tr = "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$hookScript`""
    if ($DryRun) {
        Would "schtasks /Create /F /SC HOURLY /TN UEFN-MCP-hook /TR '$tr'$(if ($taskExists) { '  (task exists; refreshed)' })"
    } else {
        & schtasks.exe /Create /F /SC HOURLY /TN 'UEFN-MCP-hook' /TR $tr *> $null
        if ($LASTEXITCODE -eq 0) { Info 'hourly task UEFN-MCP-hook registered' } else { $todo.Add('Could not register the hourly task UEFN-MCP-hook.') }
    }
} elseif ($taskExists) {
    Info 'hourly task UEFN-MCP-hook: present'
} else {
    $todo.Add('Optional: run again with -ScheduleHook so an hourly task re-adds the hook after Fortnite updates.')
}

# 5. UEFN report ------------------------------------------------------------------------------------------------------
Step 'UEFN'
if ($install.exe) {
    Info "editor: $($install.exe) (found via $($install.exe_source))"
} else {
    Info 'editor: NOT FOUND (install UEFN from the Epic Games Launcher, or set UEFN_EDITOR_EXE / UEFN_FORTNITE_DIR)'
    $todo.Add('Install Unreal Editor for Fortnite (Epic Games Launcher), then run setup.ps1 again.')
}
Info "Epic launcher entry: $(if ($install.launcher_uri -and $install.launcher_registered) { 'yes (launches with sign-in)' } else { 'no (launch falls back to the editor exe)' })"
$statusArgs = @('status', '--no-ports')
if ($Project) { $statusArgs += @('--project', $Project) }
$st = Invoke-Session $statusArgs
if ($st.error) {
    Info "status: $($st.error)"
} else {
    Info "editor state: $($st.state)"
    $load = $st.settings.load_on_startup
    Info "Load on Startup: $(if ($load) { $load } else { 'default' }) ($($st.settings.load_on_startup_meaning))"
    if ($st.hook.installed) { Info "autostart hook: installed ($($st.hook.path))" } else { Info "autostart hook: $($st.hook.reason)" }
    if ($st.python_readiness) {
        $pr = $st.python_readiness
        Info "project: $($pr.title) ($($pr.project))"
        Info "  Enable Python Locally: $(if ($pr.enable_python_locally) { 'on' } else { 'OFF' }); project Python flag: $($pr.project_python_flag); Toolsets: $($pr.project_toolsets_flag)"
        if (-not $pr.ok) {
            $todo.Add("Enable Python for '$($pr.title)': in UEFN, Project Settings > search 'Python' > Python Editor Script Plugin (Enable Python Locally); or remotely with unreal-mcp ValkyriePythonToolset.EnablePythonInUEFN. Without it the listener cannot start.")
        }
        if ($pr.project_toolsets_flag -ne $true) {
            $todo.Add("unreal-mcp (port 8000) needs Toolsets enabled for '$($pr.title)' (Project Settings, search 'Toolsets').")
        }
    } else {
        $todo.Add('Pass -Project <path>\<Island>.uefnproject to check that Python is enabled for your project.')
    }
}

# 6. Load on Startup (explicit opt-in) --------------------------------------------------------------------------------
if ($EnableLoadLastProject) {
    Step "Load on Startup = Most Recent Project (opt-in)"
    $setArgs = @('set-load-on-startup', 'LastProject')
    if ($DryRun) { $setArgs += '--dry-run' }
    $r = Invoke-Session $setArgs
    if ($r.error) {
        Info $r.error
        $todo.Add('Load on Startup was not changed: close UEFN and run setup.ps1 -EnableLoadLastProject again, or set it in UEFN (Editor Preferences > Loading & Saving > Load on Startup).')
    } elseif (-not $r.changed) {
        Info 'already Most Recent Project'
    } else {
        Info "$(if ($DryRun) { '[dry-run] would change' } else { 'changed' }): $($r.changes.ValkyrieLoadAtStartupMostRecentProject.old) -> LastProject$(if ($r.backup) { " (backup $($r.backup))" })"
        if ($r.warning) { Info $r.warning }
    }
} elseif (-not $st.error -and $st.settings.load_on_startup -ne 'LastProject') {
    $todo.Add('Optional: UEFN starts on the HUB screen here; agents then pick the project from a screenshot (uefn_launch_project). To reopen the last project automatically, close UEFN and run setup.ps1 -EnableLoadLastProject, or set Editor Preferences > Loading & Saving > Load on Startup = Most Recent Project.')
}

# 7. Claude Code registration -----------------------------------------------------------------------------------------
$serverPy = Join-Path $repo 'mcp_server.py'
if ($RegisterClaude) {
    Step 'Claude Code (user scope)'
    $claude = Get-Command claude -ErrorAction SilentlyContinue
    if (-not $claude) {
        $todo.Add("claude CLI not found; register later: claude mcp add uefn -s user -- `"$($script:py.Exe)`" `"$serverPy`"")
    } elseif ($DryRun) {
        Would "claude mcp add uefn -s user -- `"$($script:py.Exe)`" `"$serverPy`""
    } else {
        & $claude.Source mcp add uefn -s user -- $script:py.Exe $serverPy *> $null
        if ($LASTEXITCODE -eq 0) { Info 'registered "uefn" at user scope' } else { Info '"uefn" is already registered (see: claude mcp list)' }
    }
}

# 8. What is left -----------------------------------------------------------------------------------------------------
Step 'Left for you'
$todo.Add('Projects created by the hub already have .mcp.json (uefn + unreal-mcp). Elsewhere: see README "Configure Claude Code". In Claude Code run /mcp and approve / reconnect "uefn"; check with the ping tool.')
$todo.Add('Open your project in UEFN once: the Output Log should show "[MCP] Auto-started on port 8765". Manual fallback: Tools > Execute Python Script > uefn_listener.py from this folder.')
$todo.Add('Desktop control (desktop_* / uefn_* tools) needs nothing else. Safety: park the mouse in a monitor''s top-left corner to stop any agent input (docs/desktop_control.md).')
$i = 1
foreach ($item in $todo) { Info "$i. $item"; $i++ }
if ($failed) { exit 1 }
exit 0
