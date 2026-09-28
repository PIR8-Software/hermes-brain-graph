#Requires -Version 5.1
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$HermesHome = if ($env:HERMES_HOME) { $env:HERMES_HOME } else { Join-Path $env:USERPROFILE ".hermes" }
$DesktopRoot = Join-Path $env:LOCALAPPDATA "hermes"

function Copy-File {
    param([string]$Src, [string]$Dest)
    $dir = Split-Path -Parent $Dest
    if (-not (Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    Copy-Item -Force $Src $Dest
}

# Desktop app loads plugins from %LOCALAPPDATA%\hermes on Windows.
Copy-File `
    (Join-Path $Root "src\desktop-plugin\plugin.js") `
    (Join-Path $DesktopRoot "desktop-plugins\brain-graph\plugin.js")

# Gateway/API: only if this machine also has a Hermes home.
if (Test-Path $HermesHome) {
    Copy-File `
        (Join-Path $Root "src\backend-api\plugin.yaml") `
        (Join-Path $HermesHome "plugins\brain-graph\plugin.yaml")
    Copy-File `
        (Join-Path $Root "src\backend-api\__init__.py") `
        (Join-Path $HermesHome "plugins\brain-graph\__init__.py")
    Copy-File `
        (Join-Path $Root "src\backend-api\manifest.json") `
        (Join-Path $HermesHome "plugins\brain-graph\dashboard\manifest.json")
    Copy-File `
        (Join-Path $Root "src\backend-api\plugin_api.py") `
        (Join-Path $HermesHome "plugins\brain-graph\dashboard\plugin_api.py")
    $dashInit = Join-Path $HermesHome "plugins\brain-graph\dashboard\__init__.py"
    if (-not (Test-Path (Split-Path $dashInit))) {
        New-Item -ItemType Directory -Path (Split-Path $dashInit) -Force | Out-Null
    }
    if (-not (Test-Path $dashInit)) {
        New-Item -ItemType File -Path $dashInit -Force | Out-Null
    }
    Write-Host "Installed API into $HermesHome"
} else {
    Write-Host "No $HermesHome — installed desktop plugin only."
    Write-Host "Run install.sh on the gateway host for the API."
}

Write-Host "Desktop plugin: $DesktopRoot\desktop-plugins\brain-graph\plugin.js"
Write-Host "Enable on the gateway: hermes plugins enable brain-graph --no-allow-tool-override"
Write-Host "Then: Ctrl+K → Reload desktop plugins"
