$ErrorActionPreference = "Stop"
$bridgePath = Join-Path (Split-Path -Parent $PSScriptRoot) "bridge\codex_usage_bridge.ps1"
& $bridgePath -SelfTest
