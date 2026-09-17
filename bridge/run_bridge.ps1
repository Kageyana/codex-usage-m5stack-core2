[CmdletBinding()]
param(
    [string]$Port = "",
    [int]$Baud = 115200,
    [double]$Interval = 30,
    [double]$ReconnectInterval = 2,
    [string]$CodexBin = "codex",
    [switch]$Demo
)

$bridgePath = Join-Path $PSScriptRoot "codex_usage_bridge.ps1"
if (-not (Test-Path -LiteralPath $bridgePath -PathType Leaf)) {
    throw "Bridge script not found: $bridgePath"
}

& $bridgePath @PSBoundParameters
