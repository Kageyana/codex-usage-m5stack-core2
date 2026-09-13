#!/usr/bin/env python3
"""Read Codex account limits from `codex app-server` and send them to M5Stack Core2 over USB serial."""

from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

FIVE_HOUR_MINUTES = 300
WEEKLY_MINUTES = 10080


class AppServerError(RuntimeError):
    pass


class CodexAppServer:
    def __init__(self, codex_bin: str = "codex") -> None:
        self.codex_bin = codex_bin
        self.proc: subprocess.Popen[str] | None = None
        self.responses: queue.Queue[dict[str, Any]] = queue.Queue()
        self.pending: dict[str, queue.Queue[dict[str, Any]]] = {}
        self.pending_lock = threading.Lock()
        self.stderr_thread: threading.Thread | None = None
        self.stdout_thread: threading.Thread | None = None

    def start(self) -> None:
        exe = shutil.which(self.codex_bin) or self.codex_bin
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.proc = subprocess.Popen(
            [exe, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            creationflags=creationflags,
        )
        self.stdout_thread = threading.Thread(target=self._stdout_loop, daemon=True)
        self.stderr_thread = threading.Thread(target=self._stderr_loop, daemon=True)
        self.stdout_thread.start()
        self.stderr_thread.start()

        self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "codex_usage_m5stack_core2",
                    "title": "Codex Usage M5Stack Core2",
                    "version": "0.1.0",
                },
                "capabilities": {"experimentalApi": True},
            },
            timeout=15,
        )
        self.notify("initialized")

    def close(self) -> None:
        if self.proc is None:
            return
        proc = self.proc
        self.proc = None
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            proc.kill()

    def _stdout_loop(self) -> None:
        assert self.proc and self.proc.stdout
        for raw in self.proc.stdout:
            raw = raw.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            msg_id = msg.get("id")
            if msg_id is None:
                continue
            with self.pending_lock:
                waiter = self.pending.get(str(msg_id))
            if waiter is not None:
                waiter.put(msg)

    def _stderr_loop(self) -> None:
        assert self.proc and self.proc.stderr
        for raw in self.proc.stderr:
            text = raw.rstrip()
            if text:
                print(f"[codex] {text}", file=sys.stderr)

    def _write(self, message: dict[str, Any]) -> None:
        if self.proc is None or self.proc.stdin is None:
            raise AppServerError("Codex app-server is not running")
        self.proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.proc.stdin.flush()

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        msg: dict[str, Any] = {"method": method}
        if params is not None:
            msg["params"] = params
        self._write(msg)

    def request(self, method: str, params: dict[str, Any] | None = None, timeout: float = 15) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        waiter: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self.pending_lock:
            self.pending[request_id] = waiter
        try:
            msg: dict[str, Any] = {"id": request_id, "method": method}
            if params is not None:
                msg["params"] = params
            self._write(msg)
            try:
                response = waiter.get(timeout=timeout)
            except queue.Empty as exc:
                raise AppServerError(f"Timeout waiting for {method}") from exc
            if "error" in response:
                raise AppServerError(f"{method}: {response['error']}")
            result = response.get("result")
            if not isinstance(result, dict):
                raise AppServerError(f"{method}: invalid result")
            return result
        finally:
            with self.pending_lock:
                self.pending.pop(request_id, None)

    def read_rate_limits(self) -> dict[str, Any]:
        return self.request("account/rateLimits/read", timeout=20)


@dataclass
class LimitWindow:
    available: bool = False
    used_percent: int = 0
    reset_at: int | None = None

    @property
    def remaining_percent(self) -> int:
        return max(0, min(100, 100 - self.used_percent))


def iter_windows(result: dict[str, Any]):
    snapshots: list[dict[str, Any]] = []
    legacy = result.get("rateLimits")
    if isinstance(legacy, dict):
        snapshots.append(legacy)
    by_id = result.get("rateLimitsByLimitId")
    if isinstance(by_id, dict):
        snapshots.extend(v for v in by_id.values() if isinstance(v, dict))

    seen: set[tuple[int | None, int | None, int]] = set()
    for snapshot in snapshots:
        for key in ("primary", "secondary"):
            window = snapshot.get(key)
            if not isinstance(window, dict):
                continue
            duration = window.get("windowDurationMins")
            reset_at = window.get("resetsAt")
            used = int(window.get("usedPercent", 0))
            signature = (duration, reset_at, used)
            if signature in seen:
                continue
            seen.add(signature)
            yield duration, window, snapshot


def find_window(result: dict[str, Any], duration_minutes: int) -> LimitWindow:
    for duration, window, _snapshot in iter_windows(result):
        if duration == duration_minutes:
            return LimitWindow(True, int(window.get("usedPercent", 0)), window.get("resetsAt"))
    return LimitWindow()


def find_snapshot(result: dict[str, Any]) -> dict[str, Any]:
    by_id = result.get("rateLimitsByLimitId")
    if isinstance(by_id, dict):
        codex = by_id.get("codex")
        if isinstance(codex, dict):
            return codex
        for value in by_id.values():
            if isinstance(value, dict):
                return value
    legacy = result.get("rateLimits")
    return legacy if isinstance(legacy, dict) else {}


def format_reset(reset_at: int | None) -> tuple[str, str]:
    if not reset_at:
        return "--", "--"
    now = time.time()
    remaining = max(0, int(reset_at - now))
    days, rem = divmod(remaining, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        reset_in = f"{days}d {hours}h"
    elif hours:
        reset_in = f"{hours}h {minutes}m"
    else:
        reset_in = f"{minutes}m"
    dt = datetime.fromtimestamp(reset_at)
    if dt.date() == datetime.now().date():
        absolute = dt.strftime("%H:%M")
    else:
        absolute = dt.strftime("%m/%d %H:%M")
    return absolute, reset_in


def window_payload(window: LimitWindow) -> dict[str, Any]:
    reset_text, reset_in_text = format_reset(window.reset_at)
    return {
        "available": window.available,
        "remainingPercent": window.remaining_percent if window.available else 0,
        "usedPercent": window.used_percent if window.available else 0,
        "resetAt": window.reset_at,
        "resetText": reset_text,
        "resetInText": reset_in_text,
    }


def build_payload(result: dict[str, Any]) -> dict[str, Any]:
    five = find_window(result, FIVE_HOUR_MINUTES)
    weekly = find_window(result, WEEKLY_MINUTES)
    snapshot = find_snapshot(result)
    credits = snapshot.get("credits") if isinstance(snapshot.get("credits"), dict) else {}
    return {
        "type": "codex_usage",
        "ok": True,
        "updated": datetime.now().strftime("%H:%M:%S"),
        "plan": str(snapshot.get("planType") or "--"),
        "fiveHour": window_payload(five),
        "weekly": window_payload(weekly),
        "credits": {
            "available": bool(credits.get("hasCredits", False)),
            "unlimited": bool(credits.get("unlimited", False)),
            "balance": str(credits.get("balance", "--")),
        },
    }


def error_payload(message: str) -> dict[str, Any]:
    return {
        "type": "codex_usage",
        "ok": False,
        "updated": datetime.now().strftime("%H:%M:%S"),
        "error": message[:180],
    }


def find_core2_port(explicit: str | None) -> str:
    if explicit:
        return explicit
    from serial.tools import list_ports

    candidates = []
    for p in list_ports.comports():
        text = f"{p.description} {p.manufacturer or ''} {p.hwid}".lower()
        if any(token in text for token in ("cp210", "silicon labs", "ch910", "m5stack")):
            candidates.append(p.device)
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise RuntimeError("M5Stack serial port not found. Specify --port COMx")
    raise RuntimeError(f"Multiple candidate serial ports: {', '.join(candidates)}. Specify --port")


def demo_result() -> dict[str, Any]:
    now = int(time.time())
    return {
        "rateLimits": {
            "limitId": "codex",
            "primary": {"usedPercent": 18, "windowDurationMins": 300, "resetsAt": now + 4 * 3600 + 6 * 60},
            "secondary": {"usedPercent": 37, "windowDurationMins": 10080, "resetsAt": now + 2 * 86400 + 14 * 3600},
            "credits": {"hasCredits": True, "unlimited": False, "balance": "1240"},
            "planType": "plus",
        }
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", help="M5Stack COM port, e.g. COM7")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--interval", type=float, default=30.0, help="Codex refresh interval in seconds")
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--demo", action="store_true", help="Send sample values without starting Codex")
    args = parser.parse_args()

    try:
        import serial
    except ImportError as exc:
        raise SystemExit("pyserial is required: pip install -r requirements.txt") from exc

    port = find_core2_port(args.port)
    print(f"M5Stack: {port} @ {args.baud}")

    app: CodexAppServer | None = None
    try:
        with serial.Serial(port, args.baud, timeout=1, write_timeout=2) as ser:
            time.sleep(1.5)
            if not args.demo:
                app = CodexAppServer(args.codex_bin)
                app.start()
                print("Codex app-server initialized")

            while True:
                try:
                    result = demo_result() if args.demo else app.read_rate_limits()  # type: ignore[union-attr]
                    payload = build_payload(result)
                    print(json.dumps(payload, ensure_ascii=False))
                except Exception as exc:
                    payload = error_payload(str(exc))
                    print(f"ERROR: {exc}", file=sys.stderr)

                ser.write((json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8"))
                ser.flush()
                time.sleep(max(5.0, args.interval))
    except KeyboardInterrupt:
        return 0
    finally:
        if app:
            app.close()


if __name__ == "__main__":
    raise SystemExit(main())
