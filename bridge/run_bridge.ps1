[CmdletBinding()]
param(
    [string]$Port = "COM6",
    [int]$CheckIntervalSeconds = 2,
    [string]$PythonPath = ""
)

$ErrorActionPreference = "Stop"
$bridgePath = Join-Path $PSScriptRoot "codex_usage_bridge.py"

if (-not (Test-Path -LiteralPath $bridgePath -PathType Leaf)) {
    throw "Bridge script not found: $bridgePath"
}

if (-not $PythonPath) {
    $localVenvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
    $platformIoPython = Join-Path $env:USERPROFILE ".platformio\penv\Scripts\python.exe"
    if (Test-Path -LiteralPath $localVenvPython -PathType Leaf) {
        $PythonPath = $localVenvPython
    } elseif (Test-Path -LiteralPath $platformIoPython -PathType Leaf) {
        $PythonPath = $platformIoPython
    } else {
        $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
        if (-not $pythonCommand) {
            throw "Python was not found. Specify -PythonPath PATH"
        }
        $PythonPath = $pythonCommand.Source
    }
}

if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw "Python executable not found: $PythonPath"
}

if ($CheckIntervalSeconds -lt 1) {
    $CheckIntervalSeconds = 1
}

$bridgeProcess = $null

try {
    while ($true) {
        $portPresent = [System.IO.Ports.SerialPort]::GetPortNames() -contains $Port

        if ($portPresent -and ($null -eq $bridgeProcess -or $bridgeProcess.HasExited)) {
            Write-Host "M5Stack detected on $Port. Starting Codex bridge..."
            $bridgeProcess = Start-Process `
                -FilePath $PythonPath `
                -ArgumentList @($bridgePath, "--port", $Port) `
                -PassThru `
                -WindowStyle Hidden
        } elseif (-not $portPresent -and $null -ne $bridgeProcess -and -not $bridgeProcess.HasExited) {
            Write-Host "M5Stack disconnected from $Port. Stopping bridge until it reconnects..."
            Stop-Process -Id $bridgeProcess.Id -Force
            $bridgeProcess.WaitForExit()
            $bridgeProcess = $null
        } elseif ($null -ne $bridgeProcess -and $bridgeProcess.HasExited) {
            Write-Warning "Codex bridge exited with code $($bridgeProcess.ExitCode). It will be restarted if $Port is present."
            $bridgeProcess = $null
        }

        Start-Sleep -Seconds $CheckIntervalSeconds
    }
} finally {
    if ($null -ne $bridgeProcess -and -not $bridgeProcess.HasExited) {
        Stop-Process -Id $bridgeProcess.Id -Force
    }
}
