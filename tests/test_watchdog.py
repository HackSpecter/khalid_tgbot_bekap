import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import watchdog
from watchdog import State, evaluate, transitions


def iso(seconds_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


def heartbeat(**overrides):
    base = {"ts": iso(10), "status": "ok", "started_at": iso(600), "disconnected_since": None,
            "bot_connected": True, "last_anchor_at": None, "last_anchor_tsa": None}
    base.update(overrides)
    return base


def keys(problems):
    return {p.key for p in problems}


def test_healthy(tmp_path):
    assert evaluate(heartbeat(), tmp_path, State(), time.time()) == []


def test_missing_and_stale(tmp_path):
    assert keys(evaluate(None, tmp_path, State(), time.time())) == {"stale"}
    assert keys(evaluate(heartbeat(ts=iso(600)), tmp_path, State(), time.time())) == {"stale"}


def test_unauthorized_and_offline(tmp_path):
    assert keys(evaluate(heartbeat(status="unauthorized"), tmp_path, State(), time.time())) == {"auth"}
    assert keys(evaluate(heartbeat(disconnected_since=iso(900)), tmp_path, State(), time.time())) == {"offline"}
    assert evaluate(heartbeat(disconnected_since=iso(30)), tmp_path, State(), time.time()) == []


def test_anchor_overdue(tmp_path):
    hb = heartbeat(started_at=iso(40 * 3600), last_anchor_at=iso(37 * 3600))
    assert keys(evaluate(hb, tmp_path, State(), time.time())) == {"anchor"}


def test_restart_loop(tmp_path):
    state = State()
    now = time.time()
    for i in range(5):
        problems = evaluate(heartbeat(started_at=iso(100 - i)), tmp_path, state, now + i)
    assert "restarts" in keys(problems)


def test_alert_once_remind_and_recover(tmp_path):
    state = State()
    now = time.time()
    problems = evaluate(None, tmp_path, state, now)
    assert len(transitions(problems, state, now)) == 1
    assert transitions(problems, state, now + 60) == []
    reminder = transitions(problems, state, now + watchdog.REMIND_EVERY + 1)
    assert reminder and reminder[0].startswith("🔁")
    assert transitions([], state, now + watchdog.REMIND_EVERY + 61) == ["✅ Бэкап снова работает"]
