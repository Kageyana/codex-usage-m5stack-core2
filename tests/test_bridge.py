import pathlib
import sys
import threading
import time
from types import SimpleNamespace

import pytest

BRIDGE_DIR = pathlib.Path(__file__).parents[1] / "bridge"
sys.path.insert(0, str(BRIDGE_DIR))
import codex_usage_bridge as bridge


def test_classifies_windows_by_duration_not_slot():
    now = int(time.time())
    result = {
        "rateLimits": {
            "primary": {"usedPercent": 35, "windowDurationMins": 10080, "resetsAt": now + 500},
            "secondary": {"usedPercent": 10, "windowDurationMins": 300, "resetsAt": now + 100},
            "credits": {"hasCredits": True, "unlimited": False, "balance": "12.5"},
            "planType": "plus",
        }
    }
    payload = bridge.build_payload(result)
    assert payload["fiveHour"]["remainingPercent"] == 90
    assert payload["weekly"]["remainingPercent"] == 65
    assert payload["credits"]["balance"] == "13"


def test_missing_five_hour_is_supported():
    result = {
        "rateLimits": {
            "primary": {"usedPercent": 31, "windowDurationMins": 10080, "resetsAt": None},
            "secondary": None,
            "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
            "planType": "plus",
        }
    }
    payload = bridge.build_payload(result)
    assert payload["fiveHour"]["available"] is False
    assert payload["weekly"]["remainingPercent"] == 69


def test_missing_weekly_is_supported():
    result = {
        "rateLimits": {
            "primary": {"usedPercent": 31, "windowDurationMins": 300, "resetsAt": None},
            "secondary": None,
            "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
            "planType": "plus",
        }
    }
    payload = bridge.build_payload(result)
    assert payload["fiveHour"]["remainingPercent"] == 69
    assert payload["weekly"]["available"] is False


def test_rate_limits_by_limit_id_is_supported():
    result = {
        "rateLimitsByLimitId": {
            "codex": {
                "primary": {"usedPercent": 22, "windowDurationMins": 10080, "resetsAt": None},
                "secondary": {"usedPercent": 7, "windowDurationMins": 300, "resetsAt": None},
                "credits": {"hasCredits": True, "unlimited": False, "balance": 3.2},
                "planType": "pro",
            }
        }
    }
    payload = bridge.build_payload(result)
    assert payload["fiveHour"]["remainingPercent"] == 93
    assert payload["weekly"]["remainingPercent"] == 78
    assert payload["plan"] == "pro"
    assert payload["credits"]["balance"] == "3"


def test_null_credit_balance_is_normalized():
    result = {
        "rateLimits": {
            "primary": {"usedPercent": 0, "windowDurationMins": 300, "resetsAt": None},
            "secondary": {"usedPercent": 0, "windowDurationMins": 10080, "resetsAt": None},
            "credits": {"hasCredits": True, "unlimited": False, "balance": None},
            "planType": "plus",
        }
    }
    payload = bridge.build_payload(result)
    assert payload["credits"]["balance"] == "--"


def test_credit_balance_is_rounded_to_integer():
    result = {
        "rateLimits": {
            "credits": {"hasCredits": True, "unlimited": False, "balance": "1179.5332000000"},
            "planType": "plus",
        }
    }
    payload = bridge.build_payload(result)
    assert payload["credits"]["balance"] == "1180"


def test_codex_executable_on_path_is_preferred():
    executable = bridge.resolve_codex_executable(sys.executable)
    assert executable.lower() == sys.executable.lower()


def test_invalid_values_do_not_abort_payload_conversion():
    result = {
        "rateLimits": {
            "primary": {"usedPercent": [], "windowDurationMins": {}, "resetsAt": 10**1000},
            "secondary": {"usedPercent": "not-a-number", "windowDurationMins": "300", "resetsAt": "bad"},
            "credits": {"hasCredits": True, "unlimited": False, "balance": {"unexpected": "object"}},
            "planType": None,
        }
    }
    payload = bridge.build_payload(result)
    assert payload["fiveHour"]["available"] is True
    assert payload["fiveHour"]["remainingPercent"] == 100
    assert payload["weekly"]["available"] is False
    assert payload["credits"]["balance"] == "--"


def test_app_server_write_failure_is_reported_separately():
    class BrokenStdin:
        def write(self, _text):
            raise BrokenPipeError("closed")

        def flush(self):
            raise AssertionError("flush must not be reached")

    app = bridge.CodexAppServer()
    app.proc = SimpleNamespace(stdin=BrokenStdin())
    with pytest.raises(bridge.AppServerError):
        app._write({"method": "account/rateLimits/read"})


def test_app_server_start_os_error_is_reported_separately(monkeypatch):
    def fail_start(*_args, **_kwargs):
        raise PermissionError("process creation denied")

    monkeypatch.setattr(bridge, "resolve_codex_executable", lambda _name: "codex.exe")
    monkeypatch.setattr(bridge.subprocess, "Popen", fail_start)
    with pytest.raises(bridge.AppServerError, match="failed to start"):
        bridge.CodexAppServer().start()


def test_app_server_failure_is_recreated_and_recovers():
    class FakeSerialSession:
        def __init__(self, _ser, _serial_exception):
            self.disconnected_event = threading.Event()
            self.writes = []
            self.wait_count = 0
            self.__class__.last_instance = self

        def start(self):
            pass

        def close(self):
            pass

        def raise_if_disconnected(self):
            pass

        def wait(self, _timeout):
            self.wait_count += 1
            if self.wait_count >= 2:
                raise KeyboardInterrupt
            time.sleep(_timeout)
            return False

        def write(self, message):
            self.writes.append(message)

    class FakeApp:
        instances = []

        def __init__(self, _codex_bin):
            self.closed = False
            self.started = False
            self.read_count = 0
            self.__class__.instances.append(self)

        def start(self, abort_event=None):
            assert abort_event is not None
            self.started = True

        def close(self):
            self.closed = True

        def read_rate_limits(self, abort_event=None):
            assert abort_event is not None
            self.read_count += 1
            if len(self.__class__.instances) == 1:
                raise bridge.AppServerError("temporary app-server failure")
            return {"rateLimits": {"primary": {"windowDurationMins": 300, "usedPercent": 1}}}

    args = SimpleNamespace(demo=False, codex_bin="codex", interval=30, reconnect_interval=1)
    with pytest.raises(KeyboardInterrupt):
        bridge.run_serial_session(
            object(),
            args,
            RuntimeError,
            app_factory=FakeApp,
            session_factory=FakeSerialSession,
        )

    assert len(FakeApp.instances) == 2
    assert FakeApp.instances[0].started is True
    assert FakeApp.instances[0].closed is True
    assert FakeApp.instances[1].started is True
    assert any(message.get("ok") is False for message in FakeSerialSession.last_instance.writes)


def test_heartbeat_disconnect_wakes_session_and_closes_serial():
    class FakeSerialError(OSError):
        pass

    class BrokenSerial:
        def __init__(self):
            self.closed = False
            self.writes = []

        def write(self, _data):
            raise FakeSerialError("USB removed")

        def flush(self):
            pass

        def close(self):
            self.closed = True

    serial = BrokenSerial()
    session = bridge.SerialSession(serial, FakeSerialError)
    session.HEARTBEAT_INTERVAL = 0.01
    session.start()
    try:
        assert session.wait(0.5) is True
        with pytest.raises(bridge.SerialConnectionLost):
            session.raise_if_disconnected()
        assert serial.closed is True
    finally:
        session.close()
