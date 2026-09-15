import pathlib
import sys
import time

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
