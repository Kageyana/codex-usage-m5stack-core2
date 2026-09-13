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
    assert payload["credits"]["balance"] == "12.5"


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
