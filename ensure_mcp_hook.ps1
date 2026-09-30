<#
.SYNOPSIS
  Appends (or re-appends) the UEFN-MCP autostart hook to Epic's whitelisted init_unreal.py.

.DESCRIPTION
  UEFN runs init_unreal.py only from a fixed whitelist of engine plugin folders, so the listener cannot
  autostart from this repo by itself. This script appends a marked hook block to
  <Fortnite>\Engine\Plugins\Experimental\Toolsets\EditorToolset\Content\Python\init_unreal.py; the hook runs
  -Target with runpy whenever UEFN brings Python up (right after a project with "Enable Python Locally" opens).

  Fortnite updates overwrite that file and remove the hook. The script is idempotent: run it after every
  update, or register it as an hourly scheduled task (README, "Auto-start"). It never touches a running editor;
  the hook takes effect at the next project open. Nothing machine-specific is hard-coded: every path is a
  parameter, an environment variable or derived from the current user's folders.

.PARAMETER FortniteDir
  Fortnite install root. Default: $env:UEFN_FORTNITE_DIR, else the Epic launcher manifest
  (%ProgramData%\Epic\UnrealEngineLauncher\LauncherInstalled.dat), else "%ProgramFiles%\Epic Games\Fortnite".

.PARAMETER Target
  Python file the hook runs. Default: $env:UEFN_MCP_HOOK_TARGET, else the per-user shim
  <Documents>\UnrealEngine\Python\init_unreal.py when it exists (installs made before 0.4.0), else the
  init_unreal.py next to this script (self-locating; the recommended target).

.PARAMETER EpicInit
  Full path of the init_unreal.py to patch. Overrides the path derived from -FortniteDir (used by tests).

.PARAMETER DryRun
  Report what would change and write nothing.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File "$env:UEFN_MCP_PATH\ensure_mcp_hook.ps1" -DryRun
#>
[CmdletBinding()]
param(
    [string]$FortniteDir = $env:UEFN_FORTNITE_DIR,
    [string]$Target = $env:UEFN_MCP_HOOK_TARGET,
    [string]$EpicInit,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$marker = 'UEFN-MCP autostart hook'
$log = Join-Path $PSScriptRoot 'ensure_mcp_hook.log'

function Write-Status([string]$message) {
    if ($DryRun) {
        Write-Output "[dry-run] $message"
        return
    }
    Write-Output $message
    try { Add-Content -Path $log -Value "$(Get-Date -Format s) $message" } catch { }
}

function Resolve-FortniteDir {
    if ($FortniteDir) { return $FortniteDir }
    $manifest = Join-Path $env:ProgramData 'Epic\UnrealEngineLauncher\LauncherInstalled.dat'
    if (Test-Path -LiteralPath $manifest) {
        try {
            $entry = (Get-Content -LiteralPath $manifest -Raw | ConvertFrom-Json).InstallationList |
                Where-Object { $_.AppName -eq 'Fortnite' } | Select-Object -First 1
            if ($entry -and $entry.InstallLocation) { return $entry.InstallLocation }
        } catch { }
    }
    return Join-Path $env:ProgramFiles 'Epic Games\Fortnite'
}

function Resolve-Target {
    if ($Target) { return $Target }
    $shim = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'UnrealEngine\Python\init_unreal.py'
    if (Test-Path -LiteralPath $shim) { return $shim }
    return Join-Path $PSScriptRoot 'init_unreal.py'
}

if (-not $EpicInit) {
    $EpicInit = Join-Path (Resolve-FortniteDir) 'Engine\Plugins\Experimental\Toolsets\EditorToolset\Content\Python\init_unreal.py'
}
$hookTarget = Resolve-Target

if (-not (Test-Path -LiteralPath $EpicInit)) {
    Write-Status "Epic init_unreal.py not found: $EpicInit (set -FortniteDir or UEFN_FORTNITE_DIR)"
    exit 0
}
if (-not (Test-Path -LiteralPath $hookTarget)) {
    Write-Status "hook target not found: $hookTarget (set -Target or UEFN_MCP_HOOK_TARGET)"
    exit 1
}

$content = [IO.File]::ReadAllText($EpicInit)
if ($content.Contains($marker) -and $content.Contains($hookTarget)) {
    if ($DryRun) { Write-Status "hook present in $EpicInit (target $hookTarget); nothing to do" }
    exit 0
}

$stale = $content.Contains($marker)
if ($stale) {
    # A hook from an older install points at another file: strip that block, then append the current one.
    $content = [regex]::Replace(
        $content,
        '(?s)\r?\n# --- UEFN-MCP autostart hook.*?# --- end UEFN-MCP autostart hook ---[^\r\n]*(\r?\n)?',
        '')
}

$hook = @"

# --- UEFN-MCP autostart hook (added by ensure_mcp_hook.ps1; re-added after Fortnite updates) ---
try:
    import runpy
    runpy.run_path(r"$hookTarget")
except Exception as _mcp_e:
    unreal.log_error(f"[MCP] Autostart hook failed: {_mcp_e}")
# --- end UEFN-MCP autostart hook ---
"@

$action = if ($stale) { 'hook replaced' } else { 'hook appended' }
if ($DryRun) {
    Write-Status "$action in $EpicInit (target $hookTarget) - not written"
    exit 0
}
try {
    Copy-Item -LiteralPath $EpicInit -Destination "$EpicInit.bak-$(Get-Date -Format yyyyMMdd_HHmmss)"
    [IO.File]::WriteAllText($EpicInit, $content + $hook)
} catch {
    Write-Status "write failed for ${EpicInit}: $($_.Exception.Message) (run once from an elevated shell)"
    exit 1
}
Write-Status "$action in $EpicInit (target $hookTarget)"
