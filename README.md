# Codex Usage Display for M5Stack Core2

Display Codex account usage on an M5Stack Core2 (320x240) connected to a Windows PC by USB-C.

The PC keeps Codex authentication local. A PowerShell bridge launches `codex app-server`, reads `account/rateLimits/read`, converts the result into a compact JSON message, and sends it over the Core2 USB serial port.

## Displayed values

- 5-hour limit: remaining percent and reset time
- Weekly limit: remaining percent and reset time
- Credits: balance or unlimited status when reported by Codex
- Plan type
- USB / stale-data status

The bridge identifies windows by `windowDurationMins` rather than assuming that `primary` always means 5 hours and `secondary` always means weekly. Current known values are 300 minutes for 5 hours and 10080 minutes for 7 days.

## Repository layout

```text
firmware/                   PlatformIO project for M5Stack Core2
  platformio.ini
  src/main.cpp
bridge/                     Windows-side PowerShell bridge
  codex_usage_bridge.ps1
  run_bridge.ps1
tests/
  test_bridge.ps1
```

## Requirements

### PC

- Windows 11
- Codex CLI installed and logged in (`codex` command available)
- Windows PowerShell 5.1 or PowerShell 7
- USB data cable

### M5Stack Core2

- PlatformIO / VS Code
- M5Unified
- ArduinoJson

Dependencies are installed automatically by PlatformIO from `firmware/platformio.ini`.

## 1. Flash the Core2

Open the `firmware` directory in PlatformIO and run Upload.

CLI example:

```powershell
cd firmware
pio run -t upload
```

After flashing and rebooting, the Core2 should show the `CODEX USAGE` screen with `OFFLINE` status even when the PC bridge is stopped. The USB serial baud rate is 115200.

## 2. Check the PC bridge

The bridge uses Windows PowerShell and .NET's built-in serial support; it has no Python package dependencies. Check that Codex is available:

```powershell
codex --version
```

If `codex` is not on PATH, the Windows bridge also searches the standard Codex Desktop installation directory. Use `-CodexBin PATH` to override it.

## 3. Test the display without Codex

The bridge can auto-detect common Core2 USB serial chips. Select the USB serial device in Device Manager if you need to specify a port. On the current PC, the Core2's CH9102 adapter is COM6; COM7 is a Bluetooth port.

```powershell
.\bridge\run_bridge.ps1 -Demo
```

If port detection does not select the Core2, specify its COM port (COM numbers vary by PC):

```powershell
.\bridge\run_bridge.ps1 -Demo -Port COM6
```

You should see example 5-hour, weekly and credit values on the Core2.

## 4. Run with real Codex data

```powershell
.\bridge\run_bridge.ps1 -Port COM6
```

Default refresh interval is 30 seconds. To use 60 seconds:

```powershell
.\bridge\run_bridge.ps1 -Port COM6 -Interval 60
```

The bridge keeps running when the M5Stack is unplugged and retries the serial connection automatically. USB disconnects are detected by the one-second heartbeat, the serial port is closed, and the bridge waits for the port to return.

Stop it with `Ctrl+C`.

The bridge performs the app-server handshake and calls:

```text
initialize
initialized
account/rateLimits/read
```

No Codex token is stored on the Core2.

If Codex App Server stops or a rate-limit request fails, the bridge closes that process, resolves the current Codex executable again, and repeats the `initialize` / `initialized` handshake after a short retry delay. The serial connection and heartbeat continue while App Server recovery is in progress.

## Serial JSON format

One JSON object is sent per line. Example:

```json
{
  "type":"codex_usage",
  "ok":true,
  "updated":"14:27:12",
  "plan":"plus",
  "fiveHour":{
    "available":true,
    "remainingPercent":82,
    "usedPercent":18,
    "resetAt":1789288920,
    "resetText":"18:34",
    "resetInText":"4h 6m"
  },
  "weekly":{
    "available":true,
    "remainingPercent":63,
    "usedPercent":37,
    "resetAt":1789728000,
    "resetText":"09/21 05:00",
    "resetInText":"2d 14h"
  },
  "credits":{
    "available":true,
    "unlimited":false,
    "balance":"1240"
  }
}
```

## Notes

Codex may not always report both rolling windows. When the 5-hour window is absent, the Core2 displays `Not reported by Codex` instead of guessing a value.

Credits balance is rounded to the nearest integer for display. A missing or invalid balance is shown as `--`.

The Core2 renders the fixed layout only once. On later updates it redraws only the changed status, usage window, credits, footer, or error region to reduce display flicker.

The bridge sends a one-second heartbeat independently of the Codex refresh interval. Codex usage is read every 30 seconds by default. If the Core2 receives no data or heartbeat from the PC for 30 seconds after a connection has been established, the LCD is put to sleep while the Core2 remains powered. The next heartbeat or usage message wakes the LCD and redraws the display. `POWER_OFF_ON_DISCONNECT` in `firmware/src/main.cpp` defaults to `false`; set it to `true` if the previous disconnect behavior (show a short message and power off the Core2) is desired.

## Verification

Run the bridge payload checks locally with:

```powershell
powershell -NoProfile -File tests\test_bridge.ps1
```

Firmware compilation can be checked locally with `pio run -d firmware`.

This project intentionally talks to the local Codex app-server instead of reading OAuth files directly.
