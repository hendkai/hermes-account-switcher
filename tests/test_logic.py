"""Hermetic logic tests: parsing, sorting, bench handling, safe auth.json writes.

No network, no real ``hermes`` CLI, no real auth.json — Hermes internals are
replaced by fake modules, so these run in a bare plugin clone as well as
inside a hermes-agent checkout.
"""

from __future__ import annotations

import contextlib
import datetime
import importlib
import importlib.util
import json
import os
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

PLUGIN_FILE = Path(__file__).resolve().parents[1] / "__init__.py"


@pytest.fixture()
def mod():
    spec = importlib.util.spec_from_file_location("credential_pool_switcher_logic", PLUGIN_FILE)
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    yield m
    sys.modules.pop(spec.name, None)


def _fake_module(monkeypatch, dotted: str, **attrs):
    """Install a fake ``dotted`` module (parents real when importable, else fake)."""
    parts = dotted.split(".")
    for i in range(1, len(parts)):
        parent = ".".join(parts[:i])
        if parent not in sys.modules:
            try:
                importlib.import_module(parent)
            except Exception:
                monkeypatch.setitem(sys.modules, parent, types.ModuleType(parent))
    fake = types.ModuleType(dotted)
    for k, v in attrs.items():
        setattr(fake, k, v)
    monkeypatch.setitem(sys.modules, dotted, fake)
    parent_mod = sys.modules[".".join(parts[:-1])]
    monkeypatch.setattr(parent_mod, parts[-1], fake, raising=False)
    return fake


def _iso(epoch: float) -> str:
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).isoformat()


# -- parsing ---------------------------------------------------------------------

def test_parse_list_handles_spaced_labels_and_any_bench_class(mod):
    text = "\n".join([
        "anthropic (3 credentials):",
        "  #1  my work acct         oauth   id=aaa111 priority=0 hermes_pkce ←",
        "  #2  b@example.com        oauth   id=bbb222 priority=1 hermes_pkce billing (402) (1h 5m left)",
        "  #3  ANTHROPIC_API_KEY    api_key id=ccc333 priority=2 env:ANTHROPIC_API_KEY",
    ])
    provider, accounts = mod._parse_list(text)
    assert provider == "anthropic"
    assert [a.label for a in accounts] == ["my work acct", "b@example.com", "ANTHROPIC_API_KEY"]
    assert accounts[0].current and accounts[0].auth_type == "oauth"
    assert accounts[1].notes == ["(1h 5m left)"] and accounts[1].status == "exhausted"
    assert accounts[2].source == "env:ANTHROPIC_API_KEY" and not accounts[2].notes


def test_accounts_from_pool_marks_dead_and_benched(mod):
    now = time.time()

    def entry(i, label, status=None, reset=None):
        return SimpleNamespace(
            id=f"id{i}", label=label, auth_type="oauth", priority=i, source="manual:hermes_pkce",
            last_status=status, last_status_at=now if status else None, last_error_code=429 if status else None,
            last_error_reset_at=reset, failure_reason=None,
        )

    entries = [entry(0, "ok@x"), entry(1, "dead@x", "dead"), entry(2, "bench@x", "exhausted", _iso(now + 3700)),
               entry(3, "expired@x", "exhausted", _iso(now - 60))]
    pool = SimpleNamespace(entries=lambda: entries, peek=lambda: entries[0])
    accounts = mod._accounts_from_pool(pool)
    by = {a.label: a for a in accounts}
    assert by["ok@x"].current and by["ok@x"].status == "ok"
    assert by["dead@x"].status == "dead" and by["dead@x"].bench_until is None
    assert by["bench@x"].bench_until and by["bench@x"].notes == ["(1h 1m left)"]
    assert by["expired@x"].bench_until is None and not by["expired@x"].notes
    assert by["ok@x"].source == "hermes_pkce"


# -- switching ---------------------------------------------------------------------

def _acct(mod, i, label, **kw):
    return mod.Account(index=i + 1, label=label, auth_type=kw.pop("auth_type", "oauth"), id=f"id{i}",
                       priority=i, source=kw.pop("source", "hermes_pkce"), **kw)


def test_switch_refuses_dead_login(mod, monkeypatch):
    accounts = [_acct(mod, 0, "a@x"), _acct(mod, 1, "dead@x", status="dead")]
    monkeypatch.setattr(mod, "_read_accounts", lambda p: (p, accounts))
    monkeypatch.setattr(mod, "_move_to_front", lambda *a: pytest.fail("must not move a dead login"))
    plan = mod.switch("dead@x")
    assert plan["ok"] is False and "log in anew" in plan["error"].lower()


def test_switch_verifies_without_usage_and_warns(mod, monkeypatch):
    accounts = [_acct(mod, 0, "a@x"), _acct(mod, 1, "b@x", bench_until=time.time() + 600)]
    monkeypatch.setattr(mod, "_read_accounts", lambda p: (p, accounts))
    monkeypatch.setattr(mod, "_pool_strategy", lambda p: "round_robin")
    moved = []
    monkeypatch.setattr(mod, "_move_to_front", lambda p, a: moved.append(a.id))
    seen = {}

    def fake_list(provider="anthropic", with_usage=True):
        seen["with_usage"] = with_usage
        return {"accounts": [{"label": "b@x", "id": "id1", "priority": 0}, {"label": "a@x", "id": "id0", "priority": 1}]}

    monkeypatch.setattr(mod, "list_accounts", fake_list)
    plan = mod.switch("b@x")
    assert moved == ["id1"]
    assert seen["with_usage"] is False
    assert plan["verified_first"] is True
    assert "benched until" in plan["warning"]
    assert "round_robin" in plan["strategy_note"]
    text = mod.render_switch(plan)
    assert "⚠" in text and "round_robin" in text


def test_move_to_front_cli_fallback_checks_success_line(mod, monkeypatch):
    monkeypatch.setattr(mod, "_load_pool", lambda p: None)
    acct = _acct(mod, 1, "b@x")
    monkeypatch.setattr(mod, "_run", lambda args: 'Set anthropic credential "b@x" to priority 0 (#1 in ...)')
    assert mod._move_to_front("anthropic", acct) is None
    monkeypatch.setattr(mod, "_run", lambda args: 'No credential matching "x" for provider anthropic.')
    assert "failed" in mod._move_to_front("anthropic", acct)


# -- auto rotation -------------------------------------------------------------------

def _auto_env(mod, monkeypatch, usage, *, cached=None, needs_login=(), labels=("a@x", "b@x")):
    monkeypatch.setattr(mod, "_current_bench_resets", lambda p: dict(cached or {}))
    monkeypatch.setattr(mod, "_oauth_tokens",
                        lambda p, **kw: ({l: "tok" for l in labels}, list(needs_login)))
    monkeypatch.setattr(mod, "list_accounts", lambda p="anthropic", with_usage=True: {
        "strategy": "fill_first",
        "accounts": [{"label": l, "id": f"id{i}", "auth_type": "oauth", "priority": i}
                     for i, l in enumerate(labels)],
    })
    monkeypatch.setattr(mod, "_usage_limits", lambda tokens, cached_benches=None: usage)
    writes = {}

    def fake_write(provider, benched, alive_labels):
        writes["benched"], writes["alive"] = dict(benched), list(alive_labels)
        return None, []

    monkeypatch.setattr(mod, "_write_auto_status", fake_write)
    switched = []
    monkeypatch.setattr(mod, "switch", lambda label, provider="anthropic", dry_run=False:
                        switched.append(label) or {"ok": True})
    return writes, switched


def test_auto_promotes_zero_percent_login(mod, monkeypatch):
    usage = {
        "a@x": {"dead_until": None, "active_limits": [], "week_pct": 50, "session_pct": 10, "cached": False},
        "b@x": {"dead_until": None, "active_limits": [], "week_pct": 0, "session_pct": 0, "cached": False},
    }
    _, switched = _auto_env(mod, monkeypatch, usage)
    report = mod.auto_rotate(probe=False)
    assert switched == ["b@x"]
    assert report["promoted"] == "b@x"


def test_auto_does_not_rewrite_cached_bench(mod, monkeypatch):
    reset = _iso(time.time() + 3600)
    usage = {
        "a@x": {"dead_until": reset, "active_limits": ["cached bench"], "cached": True},
        "b@x": {"dead_until": None, "active_limits": [], "week_pct": 20, "session_pct": 5, "cached": False},
    }
    writes, _ = _auto_env(mod, monkeypatch, usage, cached={"a@x": reset})
    report = mod.auto_rotate(probe=False)
    assert writes["benched"] == {}
    assert report["dead"] == {"a@x": reset}
    assert "a@x" not in report["benched"]


def test_auto_reports_dead_logins_needing_login(mod, monkeypatch):
    usage = {"a@x": {"dead_until": None, "active_limits": [], "week_pct": 1, "session_pct": 1, "cached": False}}
    _auto_env(mod, monkeypatch, usage, needs_login=["dead@x"], labels=("a@x",))
    report = mod.auto_rotate(probe=False)
    assert report["needs_login"] == ["dead@x"]
    assert "dead@x" in mod.render_auto(report) and "log in anew" in mod.render_auto(report)


def test_auto_flags_inconclusive_probe(mod, monkeypatch):
    usage = {"a@x": {"dead_until": None, "active_limits": [], "week_pct": 1, "session_pct": 1, "cached": False}}
    _auto_env(mod, monkeypatch, usage, labels=("a@x",))
    monkeypatch.setattr(mod, "_probe_anthropic_login",
                        lambda label, token, model: {"label": label, "alive": None, "status": 503})
    report = mod.auto_rotate(probe=True)
    assert report["alive"] == ["a@x"]
    assert report["probe_inconclusive"] == {"a@x": "HTTP 503"}
    assert "probe inconclusive" in mod.render_auto(report)


# -- probe identity + masked 429 ------------------------------------------------------

class _FakeResp:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code, body):
    import io
    import urllib.error

    return urllib.error.HTTPError("https://api.anthropic.com/v1/messages", code, "x", {},
                                  io.BytesIO(body.encode()))


def _capture_urlopen(monkeypatch, outcome):
    import urllib.request

    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        seen["body"] = json.loads(req.data.decode())
        result = outcome() if callable(outcome) else outcome  # callable: fresh error per call
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return seen


def test_probe_request_carries_claude_code_identity(mod, monkeypatch):
    monkeypatch.setattr(mod, "_claude_code_version", lambda: "9.8.7")
    seen = _capture_urlopen(monkeypatch, _FakeResp(200))
    res = mod._probe_once("a@x", "SECRET-TOKEN", "claude-haiku-5-5")
    assert res["alive"] is True and res["status"] == 200
    assert "SECRET-TOKEN" not in json.dumps(res)
    h = seen["headers"]
    assert seen["url"] == "https://api.anthropic.com/v1/messages"
    assert h["authorization"] == "Bearer SECRET-TOKEN"
    assert h["user-agent"] == "claude-code/9.8.7 (external, cli)"
    assert h["x-app"] == "cli"
    assert h["anthropic-beta"] == "claude-code-20250219,oauth-2025-04-20"
    assert h["anthropic-version"] == "2023-06-01"
    assert seen["body"]["system"] == [
        {"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude."}]
    assert seen["body"]["model"] == "claude-haiku-5-5" and seen["body"]["max_tokens"] == 4


def test_claude_code_version_prefers_installed_cli_then_fallback(mod, monkeypatch):
    import shutil
    import subprocess

    monkeypatch.setattr(shutil, "which", lambda name: "/fake/claude" if name == "claude" else None)
    monkeypatch.setattr(os.path, "isfile", lambda p: False)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="2.1.325 (Claude Code)\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert mod._claude_code_version() == "2.1.325"
    assert mod._claude_code_version() == "2.1.325" and len(calls) == 1  # cached

    mod._claude_code_version_cache = None
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert mod._claude_code_version() == mod._CLAUDE_CODE_VERSION_FALLBACK


def test_probe_masked_429_is_flagged(mod, monkeypatch):
    monkeypatch.setattr(mod, "_claude_code_version", lambda: "9.8.7")
    for body in ('{"type":"rate_limit_error","message":"Error"}',
                 '{"type":"error","error":{"type":"rate_limit_error","message":"Error"}}',
                 '{"error":"Error"}', ""):
        _capture_urlopen(monkeypatch, _http_error(429, body))
        res = mod._probe_once("a@x", "tok", "claude-haiku-5-5")
        assert res["status"] == 429 and res["masked"] is True, body


def test_probe_real_limit_429_is_not_masked(mod, monkeypatch):
    monkeypatch.setattr(mod, "_claude_code_version", lambda: "9.8.7")
    future = datetime.date.today() + datetime.timedelta(days=2)
    for body in (f'{{"error":{{"message":"You will regain access on {future} at 14:00 UTC"}}}}',
                 '{"error":{"type":"rate_limit_error","message":"Number of request tokens has exceeded your limit"}}'):
        _capture_urlopen(monkeypatch, _http_error(429, body))
        res = mod._probe_once("a@x", "tok", "claude-haiku-5-5")
        assert res["masked"] is False and res["alive"] is False and res["reset_at"]
    _capture_urlopen(monkeypatch, _http_error(402, ""))
    assert mod._probe_once("a@x", "tok", "claude-haiku-5-5")["masked"] is False


def test_auto_masked_probe_429_with_free_usage_never_benches(mod, monkeypatch):
    usage = {
        "a@x": {"dead_until": None, "active_limits": [], "week_pct": 0, "session_pct": 0, "cached": False},
        "b@x": {"dead_until": None, "active_limits": [], "week_pct": 89, "session_pct": 3, "cached": False},
    }
    writes, switched = _auto_env(mod, monkeypatch, usage)
    monkeypatch.setattr(mod, "_claude_code_version", lambda: "9.8.7")
    _capture_urlopen(monkeypatch, lambda: _http_error(429, '{"type":"rate_limit_error","message":"Error"}'))
    report = mod.auto_rotate(probe=True)
    assert writes["benched"] == {}
    assert report["dead"] == {} and report["benched"] == {}
    assert report["alive"] == ["a@x", "b@x"]
    assert set(report["probe_inconclusive"]) == {"a@x", "b@x"}
    assert "masked" in report["probe_inconclusive"]["a@x"]
    assert report["all_dead"] is False


def test_auto_real_limit_probe_429_still_benches(mod, monkeypatch):
    usage = {
        "a@x": {"dead_until": None, "active_limits": [], "week_pct": 10, "session_pct": 10, "cached": False},
        "b@x": {"dead_until": None, "active_limits": [], "week_pct": 20, "session_pct": 5, "cached": False},
    }
    writes, switched = _auto_env(mod, monkeypatch, usage)
    monkeypatch.setattr(mod, "_claude_code_version", lambda: "9.8.7")
    future = datetime.date.today() + datetime.timedelta(days=2)
    monkeypatch.setattr(mod, "_probe_anthropic_login", lambda label, token, model: (
        mod._probe_once(label, token, model)))
    _capture_urlopen(monkeypatch, lambda: _http_error(
        429, f'{{"error":{{"message":"You will regain access on {future} at 14:00 UTC"}}}}'))
    report = mod.auto_rotate(probe=True)
    assert set(writes["benched"]) == {"a@x", "b@x"}
    assert report["alive"] == [] and report["probe_inconclusive"] == {}
    assert str(future) in report["dead"]["a@x"]


def test_auto_masked_probe_429_at_full_usage_keeps_old_bench_path(mod, monkeypatch):
    # Usage under the threshold but the endpoint reports 100 % on the session
    # window without an active limit entry: masked probe results are then NOT
    # excused, the old one-hour fallback bench applies.
    usage = {"a@x": {"dead_until": None, "active_limits": [], "week_pct": 10, "session_pct": 100, "cached": False}}
    writes, _ = _auto_env(mod, monkeypatch, usage, labels=("a@x",))
    monkeypatch.setattr(mod, "_probe_anthropic_login", lambda label, token, model: {
        "label": label, "alive": False, "status": 429, "masked": True, "reset_at": _iso(time.time() + 3600)})
    report = mod.auto_rotate(probe=True)
    assert set(writes["benched"]) == {"a@x"} and report["probe_inconclusive"] == {}


# -- auth.json write safety ----------------------------------------------------------

def test_write_auto_status_locks_passes_bases_and_cleared_ids(mod, monkeypatch):
    state = {"locked": False, "calls": []}
    rows = [
        {"id": "id-a", "label": "a@x", "access_token": "A1", "refresh_token": "R1",
         "last_status": "exhausted", "last_error_reset_at": "x"},
        {"id": "id-b", "label": "b@x", "access_token": "A2", "refresh_token": "R2", "last_status": "ok"},
        {"id": "id-c", "label": "c@x", "access_token": "A3", "refresh_token": "R3", "last_status": "dead"},
    ]

    @contextlib.contextmanager
    def fake_lock(timeout_seconds=15):
        state["locked"] = True
        try:
            yield
        finally:
            state["locked"] = False

    def fake_read(provider):
        assert state["locked"], "read must happen under the auth-store lock"
        return [dict(r) for r in rows]

    def fake_write(provider, entries, *, status_cleared_ids=None, token_bases=None, removed_ids=None):
        assert state["locked"], "write must happen under the auth-store lock"
        state["calls"].append({"entries": entries, "cleared": status_cleared_ids, "bases": token_bases})
        return entries

    _fake_module(monkeypatch, "hermes_cli.auth", read_credential_pool=fake_read,
                 write_credential_pool=fake_write, _auth_store_lock=fake_lock)
    reset = _iso(time.time() + 3600)
    benched = {"b@x": {"reset_at": reset, "code": 429, "note": "probe HTTP 429"},
               "c@x": {"reset_at": reset, "code": 429, "note": "x"}}
    error, cleared = mod._write_auto_status("anthropic", benched, alive_labels=["a@x"])
    assert error is None and cleared == ["a@x"]
    call = state["calls"][0]
    assert call["cleared"] == ["id-a"]  # ids, not labels
    assert call["bases"] == {"id-a": ("A1", "R1"), "id-b": ("A2", "R2"), "id-c": ("A3", "R3")}
    out = {e["id"]: e for e in call["entries"]}
    assert out["id-a"]["last_status"] == "ok" and out["id-a"]["last_error_reset_at"] is None
    assert out["id-b"]["last_status"] == "exhausted" and out["id-b"]["last_error_reset_at"] == reset
    assert out["id-c"]["last_status"] == "dead"  # terminal verdict untouched
    assert all(out[i]["access_token"] == rows[n]["access_token"] for n, i in enumerate(("id-a", "id-b", "id-c")))


# -- usage / probe helpers -----------------------------------------------------------

def test_usage_limits_picks_latest_reset_across_formats(mod, monkeypatch):
    payload = {
        "limits": [
            {"kind": "five_hour", "is_active": True, "percent": 100, "resets_at": "2030-01-01T10:00:00Z"},
            {"kind": "seven_day", "is_active": True, "percent": 100, "resets_at": "2030-01-01T09:30:00+00:00"},
            {"kind": "weekly_scoped", "is_active": True, "percent": 100, "resets_at": "2030-01-02T00:00:00.5+00:00"},
        ],
        "five_hour": {"utilization": 100}, "seven_day": {"utilization": 0},
    }
    _fake_module(monkeypatch, "agent.account_usage", _get_json=lambda url, headers, timeout: payload)
    out = mod._usage_limits({"a@x": "tok"})["a@x"]
    assert out["dead_until"] == "2030-01-02T00:00:00.5+00:00"
    assert out["week_pct"] == 0 and out["scoped_active"] is True


def test_probe_reset_at_timezones(mod):
    future = datetime.date.today() + datetime.timedelta(days=3)
    explicit = mod._probe_reset_at(429, f"regain access on {future} at 14:00 UTC")
    assert explicit.startswith(f"{future}T14:00:00") and explicit.endswith("+00:00")
    naive = mod._probe_reset_at(429, f"You will regain access on {future} at 14:00")
    naive_dt = datetime.datetime.strptime(f"{future} 14:00", "%Y-%m-%d %H:%M")
    expected = min(naive_dt.replace(tzinfo=datetime.timezone.utc).timestamp(), naive_dt.timestamp())
    assert abs(mod._to_epoch(naive) - expected) < 1
    past = mod._probe_reset_at(429, "regain access on 2000-01-01 at 00:00 UTC")
    assert 3500 < mod._to_epoch(past) - time.time() <= 3600
    assert mod._probe_reset_at(500, "boom") is None


def test_probe_falls_back_on_unknown_model(mod, monkeypatch):
    tried = []

    def fake_once(label, token, model):
        tried.append(model)
        if model == "bogus-model":
            return {"label": label, "alive": None, "status": 404, "note": "not_found_error: model"}
        return {"label": label, "alive": True, "status": 200}

    monkeypatch.setattr(mod, "_probe_once", fake_once)
    res = mod._probe_anthropic_login("a@x", "tok", "bogus-model")
    assert tried == ["bogus-model", "claude-haiku-4-5"]
    assert res["alive"] is True


def test_oauth_tokens_skips_dead_and_refreshes_only_unbenched(mod, monkeypatch):
    refreshed = []
    entries = [
        SimpleNamespace(id="1", label="a@x", auth_type="oauth", source="manual:hermes_pkce",
                        last_status=None, access_token="A"),
        SimpleNamespace(id="2", label="bench@x", auth_type="oauth", source="manual:hermes_pkce",
                        last_status="exhausted", access_token="B"),
        SimpleNamespace(id="3", label="dead@x", auth_type="oauth", source="manual:hermes_pkce",
                        last_status="dead", access_token="C"),
        SimpleNamespace(id="4", label="env", auth_type="oauth", source="env:X", last_status=None, access_token="D"),
    ]
    pool = SimpleNamespace(
        entries=lambda: entries,
        _entry_needs_refresh=lambda e: True,
        _refresh_entry=lambda e, force: refreshed.append(e.label) or e,
    )
    monkeypatch.setattr(mod, "_load_pool", lambda p: pool)
    tokens, dead = mod._oauth_tokens("anthropic", manual_only=True, cached_benches={"bench@x": "x"})
    assert tokens == {"a@x": "A", "bench@x": "B"}
    assert dead == ["dead@x"]
    assert refreshed == ["a@x"]


# -- onboarding hints --------------------------------------------------------------------

def test_empty_pool_explains_how_to_add_accounts(mod, monkeypatch):
    text = mod.render_list({"provider": "anthropic", "accounts": []})
    assert "hermes auth add anthropic --type oauth" in text
    monkeypatch.setattr(mod, "_read_accounts", lambda p: (p, []))
    assert "hermes auth add anthropic" in mod.switch("x")["error"]


def test_dead_logins_get_source_specific_relogin_hint(mod):
    data = {"provider": "anthropic", "accounts": [
        {"index": 1, "label": "a@x", "priority": 0, "current": False, "status": "dead",
         "needs_login": True, "source": "hermes_pkce"},
        {"index": 2, "label": "claude_code", "priority": 1, "current": False, "status": "dead",
         "needs_login": True, "source": "claude_code"},
    ]}
    text = mod.render_list(data)
    assert "hermes auth remove anthropic 'a@x' && hermes auth add anthropic --type oauth --label 'a@x'" in text
    assert "Claude Code CLI" in text
    assert "auth.json" in text


# -- status indicator --------------------------------------------------------------------

def test_pool_status_states_and_active(mod, monkeypatch):
    monkeypatch.setattr(mod, "_pool_strategy", lambda p: "fill_first")
    monkeypatch.setattr(mod, "_read_accounts", lambda p: (p, []))
    empty = mod.pool_status()
    assert empty["state"] == "setup" and empty["active"] is None
    assert "hermes auth add anthropic" in empty["add_hint"]

    now = time.time()
    accounts = [
        _acct(mod, 0, "dead@x", status="dead"),
        _acct(mod, 1, "bench@x", status="exhausted", bench_until=now + 600, notes=["(10m left)"]),
        _acct(mod, 2, "ok@x"),
        _acct(mod, 3, "spare@x"),
    ]
    monkeypatch.setattr(mod, "_read_accounts", lambda p: (p, accounts))
    st = mod.pool_status()
    assert st["state"] == "degraded" and st["usable"] == 2 and st["count"] == 4
    assert st["active"]["label"] == "ok@x"
    rows = {r["label"]: r for r in st["accounts"]}
    assert rows["dead@x"]["status"] == "dead" and "hermes auth remove" in rows["dead@x"]["relogin_hint"]
    assert rows["bench@x"]["status"] == "benched" and rows["bench@x"]["bench_left"] == "10m"
    assert [l for l, r in rows.items() if r["switchable"]] == ["spare@x"]

    monkeypatch.setattr(mod, "_read_accounts", lambda p: (p, accounts[:2]))
    st = mod.pool_status()
    assert st["state"] == "exhausted" and st["active"] is None and st["next_ready_at"]


def test_status_cli_prints_sentinel_json_line(mod, monkeypatch, capsys):
    import json as _json
    monkeypatch.setattr(mod, "pool_status", lambda p="anthropic": {"provider": p, "state": "setup"})
    mod._handle_cli(SimpleNamespace(cmd="status", provider="anthropic", json=True))
    line = capsys.readouterr().out.strip()
    assert line.startswith(mod.STATUS_LINE_PREFIX)
    assert _json.loads(line[len(mod.STATUS_LINE_PREFIX):])["state"] == "setup"


def test_setup_text_adapts_to_pool(mod, monkeypatch):
    monkeypatch.setattr(mod, "_pool_strategy", lambda p: "fill_first")
    monkeypatch.setattr(mod, "_read_accounts", lambda p: (p, []))
    assert "No accounts yet" in mod._slash("setup")
    monkeypatch.setattr(mod, "_read_accounts", lambda p: (p, [_acct(mod, 0, "a@x"), _acct(mod, 1, "d@x", status="dead")]))
    text = mod._slash("setup")
    assert "Hermes uses a@x next" in text and "d@x is dead" in text


def test_switch_broadcasts_pool_changed(mod, monkeypatch):
    sent = []
    monkeypatch.setitem(sys.modules, "tui_gateway.server", types.ModuleType("tui_gateway.server"))
    _fake_module(monkeypatch, "hermes_cli.plugin_events",
                 broadcast_plugin_event=lambda pid, ev, payload=None: sent.append((pid, ev, payload)))
    monkeypatch.setattr(mod, "_read_accounts", lambda p: (p, [_acct(mod, 0, "a@x"), _acct(mod, 1, "b@x")]))
    monkeypatch.setattr(mod, "_pool_strategy", lambda p: "fill_first")
    monkeypatch.setattr(mod, "_move_to_front", lambda p, a: None)
    monkeypatch.setattr(mod, "list_accounts", lambda p="anthropic", with_usage=True: {"accounts": []})
    mod.switch("b@x")
    assert sent == [("credential-pool-switcher", "pool.changed", {"provider": "anthropic"})]


def test_broadcast_skipped_outside_gateway(mod, monkeypatch):
    monkeypatch.delitem(sys.modules, "tui_gateway.server", raising=False)
    _fake_module(monkeypatch, "hermes_cli.plugin_events",
                 broadcast_plugin_event=lambda *a, **k: pytest.fail("must not broadcast from a CLI process"))
    mod._notify_changed("anthropic")


# -- slash -------------------------------------------------------------------------------

def test_slash_plan_without_target_shows_usage(mod, monkeypatch):
    monkeypatch.setattr(mod, "switch", lambda *a, **k: pytest.fail("plan without target must not switch"))
    assert mod._slash("plan").startswith("Usage:")


def test_slash_provider_flag_anywhere(mod, monkeypatch):
    seen = {}
    monkeypatch.setattr(mod, "switch", lambda t, p="anthropic", dry_run=False:
                        seen.update(t=t, p=p, dry=dry_run) or {"ok": False, "error": "x"})
    mod._slash("plan work --provider=openai")
    assert seen == {"t": "work", "p": "openai", "dry": True}
