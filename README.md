# Codex Usage Display for M5Stack Core2

Display Codex account usage on an M5Stack Core2 (320x240) connected to a Windows PC by USB-C.

The PC keeps Codex authentication local. A small Python bridge launches `codex app-server`, reads `account/rateLimits/read`, converts the result into a compact JSON message, and sends it over the Core2 USB serial port.

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
bridge/                     Windows-side Python bridge
  codex_usage_bridge.py
  requirements.txt
tests/
  test_bridge.py
```

## Requirements

### PC

- Windows 11
- Codex CLI installed and logged in (`codex` command available)
- Python 3.11+ recommended
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

The USB serial baud rate is 115200.

## 2. Install the PC bridge

```powershell
cd bridge
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Check that Codex is available:

```powershell
codex --version
```

If `codex` is not on PATH, the Windows bridge also searches the standard Codex Desktop installation directory. Use `--codex-bin PATH` to override it.

## 3. Test the display without Codex

The bridge can auto-detect common Core2 USB serial chips. If auto-detection fails, specify `--port COM7` (replace with your port).

```powershell
python codex_usage_bridge.py --demo
```

or:

```powershell
python codex_usage_bridge.py --demo --port COM7
```

You should see example 5-hour, weekly and credit values on the Core2.

## 4. Run with real Codex data

```powershell
python codex_usage_bridge.py --port COM7
```

Default refresh interval is 30 seconds. To use 60 seconds:

```powershell
python codex_usage_bridge.py --port COM7 --interval 60
```

The bridge keeps running when the M5Stack is unplugged and retries the serial connection automatically. To run it from a PowerShell watchdog that also restarts the bridge process after a failure:

```powershell
.\run_bridge.ps1 -Port COM6
```

The watchdog checks the COM port every two seconds. Stop it with `Ctrl+C`.

The bridge performs the app-server handshake and calls:

```text
initialize
initialized
account/rateLimits/read
```

No Codex token is stored on the Core2.

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

The bridge sends a one-second heartbeat independently of the Codex refresh interval. If the Core2 receives no data or heartbeat from the PC for 30 seconds after a connection has been established, it displays a disconnect message and powers itself off.

This project intentionally talks to the local Codex app-server instead of reading OAuth files directly.
