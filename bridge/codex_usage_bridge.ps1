[CmdletBinding()]
param(
    [string]$Port = "",
    [ValidateRange(1200, 4000000)]
    [int]$Baud = 115200,
    [ValidateRange(5, 86400)]
    [double]$Interval = 30,
    [ValidateRange(1, 3600)]
    [double]$ReconnectInterval = 2,
    [string]$CodexBin = "codex",
    [switch]$Demo,
    [switch]$SelfTest
)

$ErrorActionPreference = "Stop"
$script:SerialPort = $null
$script:LastHeartbeatUtc = [DateTime]::MinValue

function Get-Field {
    param([AllowNull()][object]$Object, [Parameter(Mandatory)][string]$Name)

    if ($null -eq $Object) { return $null }
    if ($Object -is [System.Collections.IDictionary]) {
        if ($Object.Contains($Name)) { return $Object[$Name] }
        return $null
    }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -ne $property) { return $property.Value }
    return $null
}

function Get-ObjectEntries {
    param([AllowNull()][object]$Object)

    if ($null -eq $Object) { return }
    if ($Object -is [System.Collections.IDictionary]) {
        foreach ($key in $Object.Keys) {
            [pscustomobject]@{ Name = [string]$key; Value = $Object[$key] }
        }
        return
    }
    foreach ($property in $Object.PSObject.Properties) {
        [pscustomobject]@{ Name = $property.Name; Value = $property.Value }
    }
}

function ConvertTo-SafeInteger {
    param([AllowNull()][object]$Value, [long]$Default = 0)

    if ($null -eq $Value -or $Value -is [bool]) { return $Default }
    try {
        if ($Value -is [ValueType]) { $number = [Convert]::ToDecimal($Value, [Globalization.CultureInfo]::InvariantCulture) }
        else { $number = [decimal]::Parse([string]$Value, [Globalization.NumberStyles]::Float, [Globalization.CultureInfo]::InvariantCulture) }
        if ($number -gt [long]::MaxValue -or $number -lt [long]::MinValue) { return $Default }
        return [long][decimal]::Truncate($number)
    } catch {
        return $Default
    }
}

function ConvertTo-NullableInteger {
    param([AllowNull()][object]$Value)

    if ($null -eq $Value -or $Value -is [bool]) { return $null }
    try {
        if ($Value -is [ValueType]) { $number = [Convert]::ToDecimal($Value, [Globalization.CultureInfo]::InvariantCulture) }
        else { $number = [decimal]::Parse([string]$Value, [Globalization.NumberStyles]::Float, [Globalization.CultureInfo]::InvariantCulture) }
        if ($number -gt [long]::MaxValue -or $number -lt [long]::MinValue) { return $null }
        return [long][decimal]::Truncate($number)
    } catch {
        return $null
    }
}

function Get-RateLimitSnapshots {
    param([Parameter(Mandatory)][object]$Result)

    $legacy = Get-Field $Result "rateLimits"
    if ($null -ne $legacy) { $legacy }
    $byId = Get-Field $Result "rateLimitsByLimitId"
    foreach ($entry in (Get-ObjectEntries $byId)) {
        if ($null -ne $entry.Value) { $entry.Value }
    }
}

function Find-UsageWindow {
    param([Parameter(Mandatory)][object]$Result, [Parameter(Mandatory)][long]$DurationMinutes)

    $seen = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    foreach ($snapshot in (Get-RateLimitSnapshots $Result)) {
        foreach ($slot in @("primary", "secondary")) {
            $window = Get-Field $snapshot $slot
            if ($null -eq $window) { continue }
            $duration = ConvertTo-NullableInteger (Get-Field $window "windowDurationMins")
            $resetAt = ConvertTo-NullableInteger (Get-Field $window "resetsAt")
            $used = ConvertTo-SafeInteger (Get-Field $window "usedPercent") 0
            $signature = "{0}|{1}|{2}" -f $duration, $resetAt, $used
            if (-not $seen.Add($signature)) { continue }
            if ($duration -eq $DurationMinutes) {
                return [pscustomobject]@{ Available = $true; UsedPercent = $used; ResetAt = $resetAt }
            }
        }
    }
    return [pscustomobject]@{ Available = $false; UsedPercent = 0; ResetAt = $null }
}

function Get-PreferredSnapshot {
    param([Parameter(Mandatory)][object]$Result)

    $byId = Get-Field $Result "rateLimitsByLimitId"
    $codex = Get-Field $byId "codex"
    if ($null -ne $codex) { return $codex }
    foreach ($entry in (Get-ObjectEntries $byId)) {
        if ($null -ne $entry.Value) { return $entry.Value }
    }
    $legacy = Get-Field $Result "rateLimits"
    if ($null -ne $legacy) { return $legacy }
    return [pscustomobject]@{}
}

function Format-ResetTime {
    param([AllowNull()][object]$ResetAt)

    $timestamp = ConvertTo-NullableInteger $ResetAt
    if ($null -eq $timestamp -or $timestamp -eq 0) {
        return [pscustomobject]@{ ResetText = "--"; ResetInText = "--" }
    }
    try {
        $resetDate = [DateTimeOffset]::FromUnixTimeSeconds($timestamp).ToLocalTime()
    } catch {
        return [pscustomobject]@{ ResetText = "--"; ResetInText = "--" }
    }

    $remaining = [Math]::Max([long]0, $timestamp - [DateTimeOffset]::UtcNow.ToUnixTimeSeconds())
    $days = [long][Math]::Floor($remaining / 86400)
    $hours = [long][Math]::Floor(($remaining % 86400) / 3600)
    $minutes = [long][Math]::Floor(($remaining % 3600) / 60)
    if ($days -gt 0) { $relative = "{0}d {1}h" -f $days, $hours }
    elseif ($hours -gt 0) { $relative = "{0}h {1}m" -f $hours, $minutes }
    else { $relative = "{0}m" -f $minutes }

    $now = Get-Date
    if ($resetDate.Date -eq $now.Date) { $absolute = $resetDate.ToString("HH:mm", [Globalization.CultureInfo]::InvariantCulture) }
    else { $absolute = $resetDate.ToString("MM/dd HH:mm", [Globalization.CultureInfo]::InvariantCulture) }
    return [pscustomobject]@{ ResetText = $absolute; ResetInText = $relative }
}

function Format-CreditBalance {
    param([AllowNull()][object]$Value)

    if ($null -eq $Value) { return "--" }
    try {
        if ($Value -is [ValueType]) { $number = [Convert]::ToDecimal($Value, [Globalization.CultureInfo]::InvariantCulture) }
        else {
            $text = ([string]$Value).Trim()
            if (-not $text) { return "--" }
            $number = [decimal]::Parse($text, [Globalization.NumberStyles]::Float, [Globalization.CultureInfo]::InvariantCulture)
        }
        $rounded = [decimal]::Round($number, 0, [MidpointRounding]::AwayFromZero)
        return $rounded.ToString("0", [Globalization.CultureInfo]::InvariantCulture)
    } catch {
        return "--"
    }
}

function New-UsagePayload {
    param([Parameter(Mandatory)][object]$Result)

    $fiveHour = Find-UsageWindow $Result 300
    $weekly = Find-UsageWindow $Result 10080
    $snapshot = Get-PreferredSnapshot $Result
    $credits = Get-Field $snapshot "credits"
    $fiveReset = Format-ResetTime $fiveHour.ResetAt
    $weekReset = Format-ResetTime $weekly.ResetAt
    $plan = Get-Field $snapshot "planType"
    if ($null -eq $plan -or -not [string]$plan) { $plan = "--" }

    return [ordered]@{
        type = "codex_usage"
        ok = $true
        updated = (Get-Date).ToString("HH:mm:ss", [Globalization.CultureInfo]::InvariantCulture)
        plan = [string]$plan
        fiveHour = [ordered]@{
            available = [bool]$fiveHour.Available
            remainingPercent = if ($fiveHour.Available) { [int][Math]::Max(0, [Math]::Min(100, 100 - $fiveHour.UsedPercent)) } else { 0 }
            usedPercent = if ($fiveHour.Available) { $fiveHour.UsedPercent } else { 0 }
            resetAt = $fiveHour.ResetAt
            resetText = $fiveReset.ResetText
            resetInText = $fiveReset.ResetInText
        }
        weekly = [ordered]@{
            available = [bool]$weekly.Available
            remainingPercent = if ($weekly.Available) { [int][Math]::Max(0, [Math]::Min(100, 100 - $weekly.UsedPercent)) } else { 0 }
            usedPercent = if ($weekly.Available) { $weekly.UsedPercent } else { 0 }
            resetAt = $weekly.ResetAt
            resetText = $weekReset.ResetText
            resetInText = $weekReset.ResetInText
        }
        credits = [ordered]@{
            available = [bool](Get-Field $credits "hasCredits")
            unlimited = [bool](Get-Field $credits "unlimited")
            balance = Format-CreditBalance (Get-Field $credits "balance")
        }
    }
}

function New-ErrorPayload {
    param([Parameter(Mandatory)][string]$Message)

    if ($Message.Length -gt 180) { $Message = $Message.Substring(0, 180) }
    return [ordered]@{
        type = "codex_usage"
        ok = $false
        updated = (Get-Date).ToString("HH:mm:ss", [Globalization.CultureInfo]::InvariantCulture)
        error = $Message
    }
}

function New-DemoResult {
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    return @{
        rateLimits = @{
            limitId = "codex"
            primary = @{ usedPercent = 18; windowDurationMins = 300; resetsAt = $now + 4 * 3600 + 6 * 60 }
            secondary = @{ usedPercent = 37; windowDurationMins = 10080; resetsAt = $now + 2 * 86400 + 14 * 3600 }
            credits = @{ hasCredits = $true; unlimited = $false; balance = "1240" }
            planType = "plus"
        }
    }
}

function Resolve-CodexExecutable {
    param([Parameter(Mandatory)][string]$Name)

    $command = Get-Command -Name $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($command) { return $command.Source }

    if ([IO.Path]::GetFileName($Name) -notin @("codex", "codex.exe")) { return $Name }
    if (-not $env:LOCALAPPDATA) { return $Name }
    $installRoot = Join-Path $env:LOCALAPPDATA "OpenAI\Codex\bin"
    if (-not (Test-Path -LiteralPath $installRoot -PathType Container)) { return $Name }
    $candidates = @()
    $direct = Join-Path $installRoot "codex.exe"
    if (Test-Path -LiteralPath $direct -PathType Leaf) { $candidates += Get-Item -LiteralPath $direct }
    $candidates += Get-ChildItem -LiteralPath $installRoot -Directory -ErrorAction SilentlyContinue |
        ForEach-Object { Get-Item -LiteralPath (Join-Path $_.FullName "codex.exe") -ErrorAction SilentlyContinue }
    $latest = $candidates | Sort-Object -Property LastWriteTimeUtc -Descending | Select-Object -First 1
    if ($latest) { return $latest.FullName }
    return $Name
}

function Test-SerialPortPresent {
    param([Parameter(Mandatory)][string]$PortName)

    try {
        return @([IO.Ports.SerialPort]::GetPortNames()) -contains $PortName
    } catch {
        return $false
    }
}

function Set-SerialModemState {
    param(
        [Parameter(Mandatory)][object]$Connection,
        [Parameter(Mandatory)][bool]$Dtr,
        [Parameter(Mandatory)][bool]$Rts
    )

    $Connection.DtrEnable = $Dtr
    $Connection.RtsEnable = $Rts
    # Some Windows USB serial drivers only propagate RTS after the current
    # DTR state is written again.
    $Connection.DtrEnable = $Dtr
}

function Reset-Core2SerialTarget {
    param([Parameter(Mandatory)][object]$Connection)

    # Core2 exposes the ESP32 reset control through the USB-UART modem
    # control lines.  A USB unplug/replug can leave the ESP32 serial driver
    # wedged while the COM port itself remains available.  Pulse RTS after
    # opening the port so the application starts receiving again without a
    # physical reset-button press.
    try {
        Set-SerialModemState -Connection $Connection -Dtr $false -Rts $false
        Set-SerialModemState -Connection $Connection -Dtr $false -Rts $true
        Start-Sleep -Milliseconds 120
        Set-SerialModemState -Connection $Connection -Dtr $false -Rts $false
        Start-Sleep -Milliseconds 1200
    } catch {
        throw [IO.IOException]::new("M5Stack reset pulse failed: $($_.Exception.Message)", $_.Exception)
    }
}

function Write-SerialMessage {
    param([Parameter(Mandatory)][object]$Message)

    if ($null -eq $script:SerialPort -or -not $script:SerialPort.IsOpen) {
        throw [IO.IOException]::new("serial port is not open")
    }
    $portProperty = $script:SerialPort.PSObject.Properties["PortName"]
    if ($null -ne $portProperty -and $portProperty.Value -and -not (Test-SerialPortPresent -PortName ([string]$portProperty.Value))) {
        throw [IO.IOException]::new("serial port disappeared: $($portProperty.Value)")
    }
    $json = ConvertTo-Json -InputObject $Message -Compress -Depth 20
    try {
        $script:SerialPort.WriteLine($json)
    } catch {
        throw [IO.IOException]::new("serial write failed: $($_.Exception.Message)", $_.Exception)
    }
}

function Send-HeartbeatIfDue {
    $now = [DateTime]::UtcNow
    if (($now - $script:LastHeartbeatUtc).TotalSeconds -ge 1) {
        Write-SerialMessage ([ordered]@{ type = "codex_heartbeat" })
        $script:LastHeartbeatUtc = $now
    }
}

function Write-AppServerDiagnostics {
    param([Parameter(Mandatory)][object]$App)

    $line = $null
    while ($App.Stderr.TryDequeue([ref]$line)) {
        if ($line) { [Console]::Error.WriteLine("[codex] {0}" -f $line.TrimEnd()) }
        $line = $null
    }
}

function Close-CodexAppServer {
    param([AllowNull()][object]$App)

    if ($null -eq $App) { return }
    try { $App.Process.StandardInput.Close() } catch { }
    try {
        if (-not $App.Process.WaitForExit(2000)) { $App.Process.Kill(); $App.Process.WaitForExit() }
    } catch { }
    foreach ($source in @($App.StdoutEvent, $App.StderrEvent)) {
        if ($source) {
            $subscriber = Get-EventSubscriber -SourceIdentifier $source -ErrorAction SilentlyContinue
            Unregister-Event -SourceIdentifier $source -ErrorAction SilentlyContinue
            if ($subscriber -and $subscriber.Action) {
                Stop-Job -Job $subscriber.Action -ErrorAction SilentlyContinue
                Remove-Job -Job $subscriber.Action -Force -ErrorAction SilentlyContinue
            }
        }
    }
    $App.Process.Dispose()
}

function Start-CodexAppServer {
    param([Parameter(Mandatory)][string]$Executable)

    $resolved = Resolve-CodexExecutable $Executable
    $process = [Diagnostics.Process]::new()
    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    if ([IO.Path]::GetExtension($resolved) -in @(".cmd", ".bat")) {
        $comspec = if ($env:ComSpec) { $env:ComSpec } else { "cmd.exe" }
        $quoted = '"' + $resolved.Replace('"', '""') + '" app-server --listen stdio://'
        $startInfo.FileName = $comspec
        $startInfo.Arguments = '/d /s /c "' + $quoted + '"'
    } else {
        $startInfo.FileName = $resolved
        $startInfo.Arguments = "app-server --listen stdio://"
    }
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardInput = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.StandardOutputEncoding = [Text.UTF8Encoding]::new($false)
    $startInfo.StandardErrorEncoding = [Text.UTF8Encoding]::new($false)
    $process.StartInfo = $startInfo

    try {
        if (-not $process.Start()) { throw "Process.Start returned false" }
    } catch {
        $process.Dispose()
        throw [InvalidOperationException]::new("Codex app-server failed to start: $($_.Exception.Message)", $_.Exception)
    }

    $stdout = [System.Collections.Concurrent.ConcurrentQueue[string]]::new()
    $stderr = [System.Collections.Concurrent.ConcurrentQueue[string]]::new()
    $eventPrefix = "CodexUsageBridge-{0}" -f [guid]::NewGuid().ToString("N")
    $stdoutEvent = "$eventPrefix-stdout"
    $stderrEvent = "$eventPrefix-stderr"
    Register-ObjectEvent -InputObject $process -EventName OutputDataReceived -SourceIdentifier $stdoutEvent -MessageData $stdout -Action {
        $line = $Event.SourceEventArgs.Data
        if ($null -ne $line) { [void]$Event.MessageData.Enqueue([string]$line) }
    } | Out-Null
    Register-ObjectEvent -InputObject $process -EventName ErrorDataReceived -SourceIdentifier $stderrEvent -MessageData $stderr -Action {
        $line = $Event.SourceEventArgs.Data
        if ($null -ne $line) { [void]$Event.MessageData.Enqueue([string]$line) }
    } | Out-Null
    $process.BeginOutputReadLine()
    $process.BeginErrorReadLine()
    $app = [pscustomobject]@{
        Process = $process
        Stdout = $stdout
        Stderr = $stderr
        StdoutEvent = $stdoutEvent
        StderrEvent = $stderrEvent
    }

    try {
        $null = Invoke-CodexRequest -App $app -Method "initialize" -Params @{
            clientInfo = @{ name = "codex_usage_m5stack_core2"; title = "Codex Usage M5Stack Core2"; version = "0.1.0" }
            capabilities = @{ experimentalApi = $true }
        } -TimeoutSeconds 15
        Send-CodexNotification -App $app -Method "initialized"
        return $app
    } catch {
        Close-CodexAppServer $app
        if ($_.Exception -is [IO.IOException]) { throw }
        throw [InvalidOperationException]::new($_.Exception.Message, $_.Exception)
    }
}

function Send-CodexNotification {
    param([Parameter(Mandatory)][object]$App, [Parameter(Mandatory)][string]$Method)

    try {
        $json = ConvertTo-Json -InputObject @{ method = $Method } -Compress -Depth 20
        $App.Process.StandardInput.WriteLine($json)
        $App.Process.StandardInput.Flush()
    } catch {
        throw [InvalidOperationException]::new("Codex app-server write failed: $($_.Exception.Message)", $_.Exception)
    }
}

function Invoke-CodexRequest {
    param(
        [Parameter(Mandatory)][object]$App,
        [Parameter(Mandatory)][string]$Method,
        [AllowNull()][object]$Params,
        [ValidateRange(1, 300)][int]$TimeoutSeconds = 15
    )

    $requestId = [guid]::NewGuid().ToString()
    $request = [ordered]@{ id = $requestId; method = $Method }
    if ($null -ne $Params) { $request.params = $Params }
    try {
        $json = ConvertTo-Json -InputObject $request -Compress -Depth 20
        $App.Process.StandardInput.WriteLine($json)
        $App.Process.StandardInput.Flush()
    } catch {
        throw [InvalidOperationException]::new("Codex app-server write failed: $($_.Exception.Message)", $_.Exception)
    }

    $timer = [Diagnostics.Stopwatch]::StartNew()
    while ($timer.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
        Write-AppServerDiagnostics $App
        if ($App.Process.HasExited) {
            throw [InvalidOperationException]::new("Codex app-server exited with code $($App.Process.ExitCode)")
        }
        $line = $null
        while ($App.Stdout.TryDequeue([ref]$line)) {
            try { $response = ConvertFrom-Json -InputObject $line -ErrorAction Stop } catch { $line = $null; continue }
            $responseId = Get-Field $response "id"
            if ([string]$responseId -ne $requestId) { $line = $null; continue }
            $remoteError = Get-Field $response "error"
            if ($null -ne $remoteError) {
                throw [InvalidOperationException]::new("${Method}: $($remoteError | ConvertTo-Json -Compress -Depth 10)")
            }
            $result = Get-Field $response "result"
            if ($null -eq $result -or $result -is [string] -or $result -is [ValueType]) {
                throw [InvalidOperationException]::new("${Method}: invalid result")
            }
            return $result
        }
        Send-HeartbeatIfDue
        Start-Sleep -Milliseconds 100
        $line = $null
    }
    throw [InvalidOperationException]::new("Timeout waiting for $Method")
}

function Get-RegistrySerialPortCandidates {
    param([Parameter(Mandatory)][string[]]$Ports)

    # Some Windows installations cannot query Win32_SerialPort without
    # elevated WMI access, or expose the adapter with a generic description.
    # CH9102 is commonly registered as VID_1A86&PID_55D4, so use the USB
    # registry as a read-only fallback for the known USB-serial vendors.
    $knownVendors = @("VID_1A86", "VID_10C4", "VID_0403")
    $result = [System.Collections.Generic.List[string]]::new()
    try {
        $usbRoot = "Registry::HKEY_LOCAL_MACHINE\SYSTEM\CurrentControlSet\Enum\USB"
        foreach ($deviceKey in (Get-ChildItem -LiteralPath $usbRoot -ErrorAction Stop)) {
            $vendor = (($deviceKey.PSChildName -split "&")[0]).ToUpperInvariant()
            if ($knownVendors -notcontains $vendor) { continue }
            foreach ($parametersKey in (Get-ChildItem -LiteralPath $deviceKey.PSPath -Recurse -ErrorAction SilentlyContinue | Where-Object { $_.PSChildName -eq "Device Parameters" })) {
                $portName = [string](Get-ItemProperty -LiteralPath $parametersKey.PSPath -Name PortName -ErrorAction SilentlyContinue).PortName
                if ($portName -and ($Ports -contains $portName) -and -not $result.Contains($portName)) {
                    $result.Add($portName)
                }
            }
        }
    } catch {
        Write-Verbose "Could not read USB serial registry entries: $($_.Exception.Message)"
    }
    return @($result)
}

function Get-Core2Port {
    param([AllowEmptyString()][string]$ExplicitPort)

    $ports = @([IO.Ports.SerialPort]::GetPortNames())
    if ($ExplicitPort) {
        if ($ports -contains $ExplicitPort) { return $ExplicitPort }
        return $null
    }
    if ($ports.Count -eq 0) { return $null }

    $candidateNames = @()
    try {
        foreach ($device in (Get-CimInstance -ClassName Win32_SerialPort -ErrorAction Stop)) {
            $description = "{0} {1} {2}" -f $device.Name, $device.Manufacturer, $device.PNPDeviceID
            if ($description -match "(?i)cp210|silicon labs|ch910|m5stack") {
                $candidateNames += [string]$device.DeviceID
            }
        }
    } catch {
        Write-Verbose "Could not read serial device descriptions: $($_.Exception.Message)"
    }
    $registryCandidates = @(Get-RegistrySerialPortCandidates -Ports $ports)
    $candidates = @($candidateNames + $registryCandidates | Where-Object { $ports -contains $_ } | Select-Object -Unique)
    if ($candidates.Count -eq 1) { return $candidates[0] }
    if ($candidates.Count -gt 1) {
        [Console]::Error.WriteLine("[SERIAL] multiple M5Stack candidate ports: {0}; specify -Port", ($candidates -join ", "))
        return $null
    }
    if ($ports.Count -eq 1) { return $ports[0] }
    [Console]::Error.WriteLine("[SERIAL] no M5Stack candidate port among {0}; specify -Port", ($ports -join ", "))
    return $null
}

function Wait-WithHeartbeat {
    param([Parameter(Mandatory)][double]$Seconds, [AllowNull()][object]$App)

    $timer = [Diagnostics.Stopwatch]::StartNew()
    while ($timer.Elapsed.TotalSeconds -lt $Seconds) {
        Send-HeartbeatIfDue
        if ($null -ne $App) { Write-AppServerDiagnostics $App }
        Start-Sleep -Milliseconds 200
    }
}

function Invoke-SerialSession {
    param([Parameter(Mandatory)][object]$Connection)

    $script:SerialPort = $Connection
    $script:LastHeartbeatUtc = [DateTime]::MinValue
    $clock = [Diagnostics.Stopwatch]::StartNew()
    $app = $null
    $nextAppRetryAt = 0.0
    $nextRefreshAt = 0.0
    $retryDelay = [Math]::Max(1.0, $ReconnectInterval)
    $refreshDelay = [Math]::Max(5.0, $Interval)

    try {
        Send-HeartbeatIfDue
        Write-Host "[SERIAL] connected $($Connection.PortName)"
        Wait-WithHeartbeat -Seconds 1.5 -App $null
        while ($true) {
            Send-HeartbeatIfDue
            if (-not $Demo -and $null -eq $app) {
                if ($clock.Elapsed.TotalSeconds -lt $nextAppRetryAt) {
                    Wait-WithHeartbeat -Seconds ([Math]::Min(0.25, $nextAppRetryAt - $clock.Elapsed.TotalSeconds)) -App $null
                    continue
                }
                Write-Host "[CODEX] app-server connecting"
                try {
                    $app = Start-CodexAppServer -Executable $CodexBin
                    Write-Host "[CODEX] app-server started"
                    $nextAppRetryAt = 0.0
                } catch [IO.IOException] {
                    throw
                } catch {
                    [Console]::Error.WriteLine("[CODEX] app-server reconnecting: {0}" -f $_.Exception.Message)
                    $nextAppRetryAt = $clock.Elapsed.TotalSeconds + $retryDelay
                    continue
                }
            }

            if ($clock.Elapsed.TotalSeconds -lt $nextRefreshAt) {
                Wait-WithHeartbeat -Seconds ([Math]::Min(0.25, $nextRefreshAt - $clock.Elapsed.TotalSeconds)) -App $app
                continue
            }

            if ($Demo) {
                $result = New-DemoResult
            } else {
                try {
                    $result = Invoke-CodexRequest -App $app -Method "account/rateLimits/read" -Params $null -TimeoutSeconds 20
                } catch [IO.IOException] {
                    throw
                } catch {
                    [Console]::Error.WriteLine("[CODEX] app-server error: {0}" -f $_.Exception.Message)
                    Close-CodexAppServer $app
                    $app = $null
                    $nextAppRetryAt = $clock.Elapsed.TotalSeconds + $retryDelay
                    Write-SerialMessage (New-ErrorPayload "Codex app-server unavailable: $($_.Exception.Message)")
                    continue
                }
            }

            try {
                $payload = New-UsagePayload $result
            } catch {
                [Console]::Error.WriteLine("[CODEX] rate-limit data conversion failed: {0}" -f $_.Exception.Message)
                $payload = New-ErrorPayload "Invalid rate-limit data: $($_.Exception.Message)"
            }
            Write-SerialMessage $payload
            if ($payload.ok) { Write-Host "[CODEX] rate limits updated" }
            $nextRefreshAt = $clock.Elapsed.TotalSeconds + $refreshDelay
        }
    } finally {
        Close-CodexAppServer $app
        $script:SerialPort = $null
        try { $Connection.Close() } catch { }
    }
}

function Invoke-BridgeSelfTest {
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $result = @{
        rateLimits = @{
            primary = @{ usedPercent = 35; windowDurationMins = 10080; resetsAt = $now + 500 }
            secondary = @{ usedPercent = 10; windowDurationMins = 300; resetsAt = $now + 100 }
            credits = @{ hasCredits = $true; unlimited = $false; balance = "12.5" }
            planType = "plus"
        }
    }
    $payload = New-UsagePayload $result
    if ($payload.fiveHour.remainingPercent -ne 90) { throw "Self-test failed: 5-hour classification" }
    if ($payload.weekly.remainingPercent -ne 65) { throw "Self-test failed: weekly classification" }
    if ($payload.credits.balance -ne "13") { throw "Self-test failed: credit rounding" }

    $numericCredits = New-UsagePayload @{ rateLimits = @{ primary = @{ usedPercent = [double]20.5; windowDurationMins = 300 }; credits = @{ balance = [double]2.5 } } }
    if ($numericCredits.fiveHour.remainingPercent -ne 80) { throw "Self-test failed: numeric percentage conversion" }
    if ($numericCredits.credits.balance -ne "3") { throw "Self-test failed: numeric credit rounding" }

    $missing = New-UsagePayload @{ rateLimits = @{ primary = @{ usedPercent = 31; windowDurationMins = 10080 } } }
    if ($missing.fiveHour.available) { throw "Self-test failed: missing window handling" }
    if ($missing.weekly.remainingPercent -ne 69) { throw "Self-test failed: remaining percentage" }

    $badValues = New-UsagePayload @{ rateLimits = @{ primary = @{ usedPercent = @(); windowDurationMins = @(); resetsAt = "invalid" }; credits = @{ balance = @{ unexpected = "object" } } } }
    if ($badValues.fiveHour.available) { throw "Self-test failed: invalid duration handling" }
    if ($badValues.credits.balance -ne "--") { throw "Self-test failed: invalid credit handling" }

    $testRoot = Join-Path ([IO.Path]::GetTempPath()) ("codex-usage-bridge-test-" + [guid]::NewGuid().ToString("N"))
    $fakeServerPath = Join-Path $testRoot "fake-app-server.ps1"
    $fakeCommandPath = Join-Path $testRoot "fake-codex.cmd"
    $null = New-Item -Path $testRoot -ItemType Directory
    try {
        @'
while ($null -ne ($line = [Console]::In.ReadLine())) {
    try { $request = ConvertFrom-Json -InputObject $line -ErrorAction Stop } catch { continue }
    if ($null -eq $request.id) { continue }
    if ($request.method -eq "account/rateLimits/read") {
        $answer = @{ rateLimits = @{ primary = @{ usedPercent = 20; windowDurationMins = 300; resetsAt = $null } } }
    } else {
        $answer = @{ ready = $true }
    }
    [Console]::Out.WriteLine((ConvertTo-Json -InputObject @{ id = $request.id; result = $answer } -Compress -Depth 20))
    [Console]::Out.Flush()
}
'@ | Set-Content -LiteralPath $fakeServerPath -Encoding UTF8
        $fakeCommand = "@echo off`r`n`"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe`" -NoProfile -ExecutionPolicy Bypass -File `"$fakeServerPath`"`r`n"
        Set-Content -LiteralPath $fakeCommandPath -Value $fakeCommand -Encoding ASCII

        $messageList = [System.Collections.Generic.List[string]]::new()
        $fakeSerial = [pscustomobject]@{ IsOpen = $true; Messages = $messageList }
        $fakeSerial | Add-Member -MemberType ScriptMethod -Name WriteLine -Value { param($line) $this.Messages.Add([string]$line) }
        $script:SerialPort = $fakeSerial
        $script:LastHeartbeatUtc = [DateTime]::MinValue
        $fakeApp = Start-CodexAppServer -Executable $fakeCommandPath
        try {
            $fakeResult = Invoke-CodexRequest -App $fakeApp -Method "account/rateLimits/read" -Params $null -TimeoutSeconds 10
            $fakePayload = New-UsagePayload $fakeResult
            if ($fakePayload.fiveHour.remainingPercent -ne 80) { throw "Self-test failed: app-server JSON-RPC response" }
            if ($messageList.Count -eq 0) { throw "Self-test failed: heartbeat during app-server startup" }
        } finally {
            Close-CodexAppServer $fakeApp
            $script:SerialPort = $null
        }
    } finally {
        if (Test-Path -LiteralPath $testRoot) { Remove-Item -LiteralPath $testRoot -Recurse -Force }
    }
    Write-Host "PowerShell bridge self-test passed."
}

if ($SelfTest) {
    Invoke-BridgeSelfTest
    return
}

if (-not $IsWindows -and $PSVersionTable.PSEdition -eq "Core") {
    throw "This bridge requires Windows because it uses a local COM serial port."
}

while ($true) {
    $portName = Get-Core2Port -ExplicitPort $Port
    if (-not $portName) {
        if ($Port) { [Console]::Error.WriteLine("[SERIAL] $Port is unavailable") }
        Start-Sleep -Seconds ([Math]::Max(1.0, $ReconnectInterval))
        continue
    }

    $connection = $null
    try {
        Write-Host "[SERIAL] connecting $portName @ $Baud"
        $connection = [IO.Ports.SerialPort]::new($portName, $Baud)
        $connection.Encoding = [Text.UTF8Encoding]::new($false)
        $connection.NewLine = "`n"
        $connection.WriteTimeout = 2000
        # Do not hold the Core2's USB-UART reset control line while the
        # application is running.  The board can remain powered from its
        # battery when USB is unplugged, so an asserted modem-control line
        # can leave the ESP32 unable to receive after reconnect.
        $connection.DtrEnable = $false
        $connection.RtsEnable = $false
        $connection.Open()
        Write-Host "[SERIAL] resetting Core2 target"
        Reset-Core2SerialTarget -Connection $connection
        Invoke-SerialSession -Connection $connection
    } catch {
        [Console]::Error.WriteLine("[SERIAL] disconnected: {0}" -f $_.Exception.Message)
        if ($null -ne $connection) {
            try { $connection.Close() } catch { }
            $connection.Dispose()
        }
    }
    Start-Sleep -Seconds ([Math]::Max(1.0, $ReconnectInterval))
}
