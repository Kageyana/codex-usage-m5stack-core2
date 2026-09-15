#!/usr/bin/env python3
"""Read Codex account limits from `codex app-server` and send them to M5Stack Core2 over USB serial."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

FIVE_HOUR_MINUTES = 300
WEEKLY_MINUTES = 10080


class AppServerError(RuntimeError):
    pass


class RequestCancelled(RuntimeError):
    pass


class SerialConnectionLost(RuntimeError):
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

    def start(self, abort_event: threading.Event | None = None) -> None:
        exe = resolve_codex_executable(self.codex_bin)
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
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
        except FileNotFoundError as exc:
            raise AppServerError(
                f"Codex CLI not found: {exe}. Install Codex or specify --codex-bin PATH"
            ) from exc
        except OSError as exc:
            raise AppServerError(f"Codex app-server failed to start: {exc}") from exc
        proc = self.proc
        assert proc is not None
        self.stdout_thread = threading.Thread(target=self._stdout_loop, args=(proc,), daemon=True)
        self.stderr_thread = threading.Thread(target=self._stderr_loop, args=(proc,), daemon=True)
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
            abort_event=abort_event,
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
            try:
                proc.kill()
            except Exception:
                pass

    def _stdout_loop(self, proc: subprocess.Popen[str]) -> None:
        if proc.stdout is None:
            return
        for raw in proc.stdout:
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

    def _stderr_loop(self, proc: subprocess.Popen[str]) -> None:
        if proc.stderr is None:
            return
        for raw in proc.stderr:
            text = raw.rstrip()
            if text:
                print(f"[codex] {text}", file=sys.stderr)

    def _write(self, message: dict[str, Any]) -> None:
        if self.proc is None or self.proc.stdin is None:
            raise AppServerError("Codex app-server is not running")
        try:
            self.proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            self.proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise AppServerError("Codex app-server write failed") from exc

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        msg: dict[str, Any] = {"method": method}
        if params is not None:
            msg["params"] = params
        self._write(msg)

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 15,
        abort_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        waiter: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self.pending_lock:
            self.pending[request_id] = waiter
        try:
            msg: dict[str, Any] = {"id": request_id, "method": method}
            if params is not None:
                msg["params"] = params
            self._write(msg)
            deadline = time.monotonic() + timeout
            while True:
                if abort_event is not None and abort_event.is_set():
                    raise RequestCancelled(f"Request cancelled while waiting for {method}")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AppServerError(f"Timeout waiting for {method}")
                try:
                    response = waiter.get(timeout=min(0.2, remaining))
                    break
                except queue.Empty:
                    continue
            if "error" in response:
                raise AppServerError(f"{method}: {response['error']}")
            result = response.get("result")
            if not isinstance(result, dict):
                raise AppServerError(f"{method}: invalid result")
            return result
        finally:
            with self.pending_lock:
                self.pending.pop(request_id, None)

    def read_rate_limits(self, abort_event: threading.Event | None = None) -> dict[str, Any]:
        return self.request("account/rateLimits/read", timeout=20, abort_event=abort_event)


def resolve_codex_executable(codex_bin: str) -> str:
    resolved = shutil.which(codex_bin)
    if resolved:
        return resolved

    if os.name != "nt" or Path(codex_bin).name.lower() not in {"codex", "codex.exe"}:
        return codex_bin

    local_appdata = os.environ.get("LOCALAPPDATA")
    if not local_appdata:
        return codex_bin

    install_root = Path(local_appdata) / "OpenAI" / "Codex" / "bin"
    candidates = [path for path in install_root.glob("*/codex.exe") if path.is_file()]
    direct = install_root / "codex.exe"
    if direct.is_file():
        candidates.append(direct)
    if not candidates:
        return codex_bin

    return str(max(candidates, key=lambda path: path.stat().st_mtime_ns))


class SerialSession:
    """Own a serial connection and keep its heartbeat independent of Codex work."""

    HEARTBEAT_INTERVAL = 1.0

    def __init__(self, ser: Any, serial_exception: type[BaseException]) -> None:
        self.ser = ser
        self.serial_exception = serial_exception
        self.write_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.disconnected_event = threading.Event()
        self.errors: queue.Queue[BaseException] = queue.Queue(maxsize=1)
        self.heartbeat_thread: threading.Thread | None = None

    def start(self) -> None:
        self.heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self.heartbeat_thread.start()

    def close(self) -> None:
        self.stop_event.set()
        self.wake_event.set()
        try:
            self.ser.close()
        except Exception:
            pass
        if self.heartbeat_thread is not None:
            self.heartbeat_thread.join(timeout=2)

    def write(self, message: dict[str, Any]) -> None:
        self.raise_if_disconnected()
        encoded = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            with self.write_lock:
                self.ser.write(encoded)
                self.ser.flush()
        except (self.serial_exception, OSError) as exc:
            self._record_disconnect(exc)
            raise SerialConnectionLost(f"serial write failed: {exc}") from exc

    def wait(self, timeout: float) -> bool:
        return self.wake_event.wait(max(0.0, timeout))

    def raise_if_disconnected(self) -> None:
        try:
            error = self.errors.get_nowait()
        except queue.Empty:
            if self.disconnected_event.is_set():
                raise SerialConnectionLost("serial connection lost")
            return
        raise SerialConnectionLost(f"serial connection lost: {error}") from error

    def _heartbeat_loop(self) -> None:
        while not self.stop_event.wait(self.HEARTBEAT_INTERVAL):
            try:
                self.write({"type": "codex_heartbeat"})
            except SerialConnectionLost:
                return

    def _record_disconnect(self, error: BaseException) -> None:
        self.disconnected_event.set()
        self.wake_event.set()
        try:
            self.errors.put_nowait(error)
        except queue.Full:
            pass
        try:
            self.ser.close()
        except Exception:
            pass


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
            duration = safe_int(window.get("windowDurationMins"))
            reset_at = safe_int(window.get("resetsAt"))
            used = safe_int(window.get("usedPercent"), 0)
            signature = (duration, reset_at, used)
            if signature in seen:
                continue
            seen.add(signature)
            yield duration, window, snapshot


def find_window(result: dict[str, Any], duration_minutes: int) -> LimitWindow:
    for duration, window, _snapshot in iter_windows(result):
        if duration == duration_minutes:
            return LimitWindow(True, safe_int(window.get("usedPercent"), 0), safe_int(window.get("resetsAt")))
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
    reset_at = safe_int(reset_at)
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
    try:
        dt = datetime.fromtimestamp(reset_at)
    except (OverflowError, OSError, ValueError):
        return "--", "--"
    if dt.date() == datetime.now().date():
        absolute = dt.strftime("%H:%M")
    else:
        absolute = dt.strftime("%m/%d %H:%M")
    return absolute, reset_in


def safe_int(value: Any, default: int | None = None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


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
            "balance": format_credit_balance(credits.get("balance")),
        },
    }


def error_payload(message: str) -> dict[str, Any]:
    return {
        "type": "codex_usage",
        "ok": False,
        "updated": datetime.now().strftime("%H:%M:%S"),
        "error": message[:180],
    }


def format_credit_balance(value: Any) -> str:
    if value is None:
        return "--"
    text = str(value).strip()
    if not text:
        return "--"
    try:
        return str(Decimal(text).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        return "--"


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


def run_serial_session(
    ser: Any,
    args: argparse.Namespace,
    serial_exception: type[BaseException],
    app_factory: Any = CodexAppServer,
    session_factory: Any = SerialSession,
) -> None:
    """Run one serial session while allowing Codex to be recreated in place."""
    session = session_factory(ser, serial_exception)
    session.start()
    app: CodexAppServer | None = None
    app_retry_at = 0.0
    next_refresh_at = 0.0
    refresh_interval = max(5.0, float(args.interval))
    retry_delay = max(1.0, float(args.reconnect_interval))

    try:
        while True:
            session.raise_if_disconnected()
            now = time.monotonic()

            if not args.demo and app is None:
                if now < app_retry_at:
                    if session.wait(app_retry_at - now):
                        session.raise_if_disconnected()
                    continue

                print("[CODEX] app-server reconnecting")
                try:
                    candidate = app_factory(args.codex_bin)
                except AppServerError as exc:
                    app_retry_at = time.monotonic() + retry_delay
                    print(f"[CODEX] app-server reconnecting: {exc}", file=sys.stderr)
                    continue
                try:
                    candidate.start(abort_event=session.disconnected_event)
                except RequestCancelled as exc:
                    candidate.close()
                    raise SerialConnectionLost("serial connection lost during app-server start") from exc
                except AppServerError as exc:
                    candidate.close()
                    app_retry_at = time.monotonic() + retry_delay
                    print(f"[CODEX] app-server reconnecting: {exc}", file=sys.stderr)
                    continue
                app = candidate
                app_retry_at = 0.0
                print("[CODEX] app-server started")

            now = time.monotonic()
            if now < next_refresh_at:
                if session.wait(next_refresh_at - now):
                    session.raise_if_disconnected()
                continue

            if args.demo:
                result = demo_result()
            else:
                assert app is not None
                try:
                    result = app.read_rate_limits(abort_event=session.disconnected_event)
                except RequestCancelled as exc:
                    raise SerialConnectionLost("serial connection lost during rate-limit request") from exc
                except AppServerError as exc:
                    print(f"[CODEX] app-server error: {exc}", file=sys.stderr)
                    app.close()
                    app = None
                    app_retry_at = time.monotonic() + retry_delay
                    print("[CODEX] app-server reconnecting", file=sys.stderr)
                    session.write(error_payload(f"Codex app-server unavailable: {exc}"))
                    continue

            try:
                payload = build_payload(result)
            except Exception as exc:
                print(f"[CODEX] rate-limit data conversion failed: {exc}", file=sys.stderr)
                payload = error_payload(f"Invalid rate-limit data: {exc}")
            else:
                print("[CODEX] rate limits updated")

            session.write(payload)
            next_refresh_at = time.monotonic() + refresh_interval
    finally:
        session.close()
        if app is not None:
            app.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", help="M5Stack COM port, e.g. COM7")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--interval", type=float, default=30.0, help="Codex refresh interval in seconds")
    parser.add_argument(
        "--reconnect-interval",
        type=float,
        default=2.0,
        help="Seconds to wait before retrying after a serial/app-server disconnect",
    )
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--demo", action="store_true", help="Send sample values without starting Codex")
    args = parser.parse_args()

    try:
        import serial
    except ImportError as exc:
        raise SystemExit("pyserial is required: pip install -r requirements.txt") from exc

    try:
        while True:
            try:
                port = find_core2_port(args.port)
            except RuntimeError as exc:
                print(f"[SERIAL] unavailable: {exc}", file=sys.stderr)
            else:
                print(f"[SERIAL] connecting {port} @ {args.baud}")
                try:
                    with serial.Serial(port, args.baud, timeout=1, write_timeout=2) as ser:
                        print(f"[SERIAL] connected {port}")
                        time.sleep(1.5)
                        run_serial_session(ser, args, serial.SerialException)
                except SerialConnectionLost as exc:
                    print(f"[SERIAL] disconnected: {exc}", file=sys.stderr)
                except (serial.SerialException, OSError) as exc:
                    print(f"[SERIAL] disconnected: {exc}", file=sys.stderr)

            try:
                time.sleep(max(1.0, args.reconnect_interval))
            except KeyboardInterrupt:
                return 0
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
