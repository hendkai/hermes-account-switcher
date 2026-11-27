"""Hermetic logic tests: parsing, sorting, bench handling, safe auth.json writes.

No network, no real ``hermes`` CLI, no real auth.json — Hermes internals are
replaced by fake modules, so these run in a bare plugin clone as well as
inside a hermes-agent checkout.
"""

from __future__ import annotations

import datetime
import importlib
import importlib.util
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

PLUGIN_FILE = Path(__file__).resolve().parents[1] / "__init__.py"


@pytest.fixture()
def mod(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("credential_pool_switcher_logic", PLUGIN_FILE)
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    m._state_path = lambda: tmp_path / "state.json"  # never touch the real Hermes home
    monkeypatch.delenv("HERMES_CPS_AUTO_ROTATE", raising=False)
    m._test_config = {}  # what the fake load_config() returns; default = nothing configured
    _fake_module(monkeypatch, "hermes_cli.config", load_config=lambda: m._test_config)
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


def _opt_in(mod, value=True):
    mod._test_config = {"plugins": {"credential-pool-switcher": {"auto_rotate": value}}}


def _iso(epoch: float) -> str:
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).isoformat()


# -- pool rows ---------------------------------------------------------------------

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
    pool = SimpleNamespace(entries=lambda: entries)  # no peek(): reads never call it
    accounts = mod._accounts_from_pool(pool)
    by = {a.label: a for a in accounts}
    assert by["ok@x"].current and by["ok@x"].status == "ok"
    assert by["dead@x"].status == "dead" and by["dead@x"].bench_until is None
    assert by["bench@x"].bench_until and by["bench@x"].notes == ["(1h 1m left)"]
    assert by["expired@x"].bench_until is None and not by["expired@x"].notes
    assert by["ok@x"].source == "hermes_pkce"


def test_exhausted_row_without_reset_has_unknown_cooldown_not_a_fabricated_hour(mod):
    now = time.time()
    e = SimpleNamespace(
        id="id9", label="noreset@x", auth_type="oauth", priority=0, source="manual:hermes_pkce",
        last_status="exhausted", last_status_at=now - 10, last_error_code=429,
        last_error_reset_at=None, failure_reason=None,
    )
    old = SimpleNamespace(**{**e.__dict__, "id": "id8", "label": "old@x", "last_status_at": now - 7200})
    pool = SimpleNamespace(entries=lambda: [e, old])
    by = {a.label: a for a in mod._accounts_from_pool(pool)}
    for a in by.values():
        assert a.bench_until is None and a.bench_unknown is True
        assert a.notes == ["(cooldown unknown)"] and mod._is_benched(a, now) is True
    assert mod._bench_until(e) is None


def test_status_shows_unknown_cooldown_without_left_time(mod, monkeypatch):
    accounts = [_acct(mod, 0, "u@x", status="exhausted", bench_unknown=True, notes=["(cooldown unknown)"]),
                _acct(mod, 1, "ok@x")]
    monkeypatch.setattr(mod, "_read_accounts", lambda p: (p, accounts))
    monkeypatch.setattr(mod, "_pool_strategy", lambda p: "fill_first")
    rows = {r["label"]: r for r in mod.pool_status()["accounts"]}
    assert rows["u@x"]["status"] == "benched" and rows["u@x"]["cooldown_unknown"] is True
    assert rows["u@x"]["bench_left"] is None and rows["u@x"]["bench_until"] is None
    assert rows["ok@x"]["cooldown_unknown"] is False
    text = mod.render_list({
        "provider": "anthropic", "accounts": [{"index": 1, "label": "u@x", "priority": 0, "current": False,
                                              "status": "exhausted", "benched": "(cooldown unknown)"}]})
    assert "cooldown unknown" in text and "1h" not in text


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
    monkeypatch.setattr(mod, "_move_to_front", lambda p, a, **k: moved.append(a.id))
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
    monkeypatch.setattr(mod, "_load_writable_pool", lambda p: None)
    acct = _acct(mod, 1, "b@x")
    monkeypatch.setattr(mod, "_run", lambda args: 'Set anthropic credential "b@x" to priority 0 (#1 in ...)')
    assert mod._move_to_front("anthropic", acct) is None
    monkeypatch.setattr(mod, "_run", lambda args: 'No credential matching "x" for provider anthropic.')
    assert "failed" in mod._move_to_front("anthropic", acct)


# -- usage via the public helper -----------------------------------------------------

def test_usage_uses_public_helper_and_skips_dead(mod, monkeypatch):
    W = lambda label, pct, scope="account": SimpleNamespace(label=label, used_percent=pct, scope=scope)
    calls = []

    def fake_fetch(provider, *, api_key=None, read_only=False, identity_id=None, **kw):
        calls.append((provider, api_key, read_only, identity_id))
        return SimpleNamespace(windows=[W("Current session", 12.0), W("Current week", 0.0),
                                        W("Fable weekly", 100.0, "model")])

    _fake_module(monkeypatch, "agent.account_usage", fetch_account_usage=fake_fetch)
    entries = [SimpleNamespace(id=f"id{i}", label=l, auth_type="oauth", source="manual:x", access_token=f"tok-{l}")
               for i, l in enumerate(("ok@x", "dead@x", "bench@x"))]
    monkeypatch.setattr(mod, "_load_pool", lambda p: SimpleNamespace(entries=lambda: entries))
    accounts = [_acct(mod, 0, "ok@x"), _acct(mod, 1, "dead@x", status="dead"),
                _acct(mod, 2, "bench@x", status="exhausted", bench_until=time.time() + 600)]
    out = mod._usage_for_accounts(accounts)
    # read_only; dead logins are skipped, benched ones are read so their bars show why;
    # every call carries the entry id as identity_id so Hermes' usage cache stays per credential
    assert sorted(c[1] for c in calls) == ["tok-bench@x", "tok-ok@x"] and all(c[2] for c in calls)
    assert sorted(c[3] for c in calls) == ["id0", "id2"]
    assert out["id0"] == {"five_hour": {"used_percent": 12.0}, "week": {"used_percent": 0.0},
                          "scoped": {"label": "Fable weekly", "used_percent": 100.0, "active": True}}
    assert "id1" not in out and set(out) == {"id0", "id2"}


def test_catalog_build_has_no_auth_store_or_refresh_paths(mod):
    src = PLUGIN_FILE.read_text()
    for forbidden in ("write_credential_pool", "_load_auth_store", "_auth_store_lock",
                      "_refresh_entry", "_get_json", "oauth/usage", "_write_auto_status"):
        assert forbidden not in src, forbidden


def test_catalog_build_sends_no_message_probe_and_writes_no_cooldown(mod):
    """No /v1/messages request, no direct HTTP client, no cooldown/bench/auth-store write."""
    src = PLUGIN_FILE.read_text()
    for forbidden in ("v1/messages", "urllib", "requests.", "httpx", "http.client", "aiohttp",
                      "mark_exhausted", "mark_used", "_mark_", "last_error_reset_at =",
                      "last_status =", "last_status_at =", "auth.json\"", "auth_store",
                      "_exhausted_until", "_exhausted_ttl",
                      "refresh_token", "_EXHAUSTED_FALLBACK_SECONDS"):
        assert forbidden not in src, forbidden


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
    monkeypatch.setattr(mod, "pool_status", lambda p="anthropic", **kw: {"provider": p, "state": "setup"})
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


# -- auto rotation (read-only, catalog build) ---------------------------------------

def _by_id(accounts, usage):
    """Label-keyed usage -> entry-id-keyed usage, the shape ``_usage_for_accounts`` returns."""
    return {a.id: usage[a.label] for a in accounts if a.label in usage}


def _usage(five=0.0, week=0.0, scoped=None):
    info = {"five_hour": {"used_percent": five}, "week": {"used_percent": week}}
    if scoped is not None:
        info["scoped"] = {"used_percent": scoped[0], "active": scoped[1]}
    return info


def test_auto_promotes_login_with_most_weekly_headroom(mod, monkeypatch):
    accounts = [_acct(mod, 0, "busy@x"), _acct(mod, 1, "fresh@x"), _acct(mod, 2, "dead@x", status="dead"),
                _acct(mod, 3, "bench@x", status="exhausted", bench_until=time.time() + 600)]
    monkeypatch.setattr(mod, "_read_accounts", lambda p: (p, accounts))
    monkeypatch.setattr(mod, "_usage_for_accounts", lambda accs, p: _by_id(accounts, {
        "busy@x": _usage(five=10, week=60), "fresh@x": _usage(five=40, week=0)}))
    promoted = []
    monkeypatch.setattr(mod, "switch", lambda target, provider="anthropic", dry_run=False, **kw:
                        promoted.append(target) or {"ok": True})
    monkeypatch.setattr(mod, "list_accounts", lambda p="anthropic", with_usage=True: {
        "accounts": [{"label": "fresh@x"}, {"label": "busy@x"}]})
    report = mod.auto_rotate()
    assert promoted == ["id1"]  # switch() gets the entry id of the winner
    assert report["promoted"] == "fresh@x"
    assert report["alive"] == ["busy@x", "fresh@x"]
    assert report["dead"] == ["dead@x"] and report["benched"] == ["bench@x"]
    assert report["order_after"] == ["fresh@x", "busy@x"]


def test_auto_skips_exhausted_and_dry_run_moves_nothing(mod, monkeypatch):
    accounts = [_acct(mod, 0, "full@x"), _acct(mod, 1, "spare@x"), _acct(mod, 2, "unknown@x")]
    monkeypatch.setattr(mod, "_read_accounts", lambda p: (p, accounts))
    monkeypatch.setattr(mod, "_usage_for_accounts", lambda accs, p: _by_id(accounts, {
        "full@x": _usage(five=100, week=20), "spare@x": _usage(five=5, week=30)}))
    monkeypatch.setattr(mod, "switch", lambda *a, **k: pytest.fail("dry run must not switch"))
    report = mod.auto_rotate(dry_run=True)
    assert report["exhausted"] == ["full@x"] and report["unknown"] == ["unknown@x"]
    assert report["would_promote"] == "spare@x" and report["promoted"] is None


def _run_auto(mod, monkeypatch, accounts, usage, dry_run=True):
    monkeypatch.setattr(mod, "_read_accounts", lambda p: (p, accounts))
    by_id = _by_id(accounts, usage)
    monkeypatch.setattr(mod, "_usage_for_accounts", lambda accs, p: by_id)
    promoted = []
    monkeypatch.setattr(mod, "switch", lambda target, provider="anthropic", dry_run=False, **kw:
                        promoted.append(target) or {"ok": True})
    monkeypatch.setattr(mod, "list_accounts", lambda p="anthropic", with_usage=True: {"accounts": []})
    return mod.auto_rotate(dry_run=dry_run), promoted


def test_auto_model_cap_with_account_headroom_stays_eligible(mod, monkeypatch):
    accounts = [_acct(mod, 0, "capped@x"), _acct(mod, 1, "ok@x")]
    usage = {"capped@x": _usage(week=10, scoped=(100.0, True)), "ok@x": _usage(week=50)}
    report, promoted = _run_auto(mod, monkeypatch, accounts, usage, dry_run=False)
    assert report["exhausted"] == [] and report["alive"] == ["capped@x", "ok@x"]
    assert report["scoped_caps"] == {"capped@x": "model cap"}
    assert report["promoted"] == "capped@x" and promoted == []  # already first and best (week 10 vs 50)
    text = mod.render_auto(report)
    assert "capped@x usage-eligible" in text and "model-scoped cap reached" in text
    assert "exhausted" not in text and "not verified by a message" in text


def test_auto_model_cap_does_not_block_promotion(mod, monkeypatch):
    accounts = [_acct(mod, 0, "busy@x"), _acct(mod, 1, "capped@x")]
    usage = {"busy@x": _usage(week=60), "capped@x": _usage(week=5, scoped=(100.0, True))}
    report, promoted = _run_auto(mod, monkeypatch, accounts, usage, dry_run=False)
    assert promoted == ["id1"] and report["promoted"] == "capped@x" and report["exhausted"] == []


def test_auto_full_account_window_is_exhausted(mod, monkeypatch):
    accounts = [_acct(mod, 0, "week@x"), _acct(mod, 1, "five@x"), _acct(mod, 2, "ok@x")]
    usage = {"week@x": _usage(five=0, week=100), "five@x": _usage(five=100, week=1), "ok@x": _usage(week=40)}
    report, _ = _run_auto(mod, monkeypatch, accounts, usage)
    assert report["exhausted"] == ["week@x", "five@x"] and report["alive"] == ["ok@x"]
    assert report["would_promote"] == "ok@x"
    text = mod.render_auto(report)
    assert "week@x account quota exhausted" in text and "five@x account quota exhausted" in text


def test_auto_scoped_only_snapshot_is_unknown_and_not_promoted(mod, monkeypatch):
    accounts = [_acct(mod, 0, "busy@x"), _acct(mod, 1, "scopedonly@x")]
    usage = {"busy@x": _usage(week=90), "scopedonly@x": {"scoped": {"label": "Opus week", "used_percent": 3.0,
                                                                     "active": False}}}
    report, promoted = _run_auto(mod, monkeypatch, accounts, usage, dry_run=False)
    assert report["unknown"] == ["scopedonly@x"] and report["unknown_reason"] == {"scopedonly@x": "scoped_only"}
    assert report["alive"] == ["busy@x"] and promoted == [] and report["would_promote"] is None
    assert "only model-scoped usage" in mod.render_auto(report)


def test_auto_never_promotes_unknown_cooldown_row(mod, monkeypatch):
    accounts = [_acct(mod, 0, "busy@x"),
                _acct(mod, 1, "noreset@x", status="exhausted", bench_unknown=True, notes=["(cooldown unknown)"])]
    usage = {"busy@x": _usage(week=90), "noreset@x": _usage(week=0)}  # great usage must not override it
    report, promoted = _run_auto(mod, monkeypatch, accounts, usage, dry_run=False)
    assert report["cooldown_unknown"] == ["noreset@x"] and report["benched"] == []
    assert "noreset@x" not in report["alive"] and promoted == [] and report["would_promote"] is None
    assert "cooldown unknown" in mod.render_auto(report)


def test_auto_explicit_reset_bench_is_still_respected_and_expired_is_not(mod, monkeypatch):
    now = time.time()
    accounts = [_acct(mod, 0, "busy@x"), _acct(mod, 1, "bench@x", status="exhausted", bench_until=now + 600),
                _acct(mod, 2, "expired@x", status="exhausted")]  # reset passed: no bench_until, no note
    usage = {"busy@x": _usage(week=60), "bench@x": _usage(week=0), "expired@x": _usage(week=10)}
    report, _ = _run_auto(mod, monkeypatch, accounts, usage)
    assert report["benched"] == ["bench@x"] and report["cooldown_unknown"] == []
    assert report["would_promote"] == "expired@x"


def test_auto_already_first_is_not_moved(mod, monkeypatch):
    accounts = [_acct(mod, 0, "best@x"), _acct(mod, 1, "other@x")]
    monkeypatch.setattr(mod, "_read_accounts", lambda p: (p, accounts))
    monkeypatch.setattr(mod, "_usage_for_accounts", lambda accs, p: _by_id(accounts, {
        "best@x": _usage(week=5), "other@x": _usage(week=50)}))
    monkeypatch.setattr(mod, "switch", lambda *a, **k: pytest.fail("already first: nothing to move"))
    report = mod.auto_rotate()
    assert report["promoted"] == "best@x"


def test_auto_without_oauth_logins_reports_no_headroom(mod, monkeypatch):
    accounts = [_acct(mod, 0, "KEY", auth_type="api_key")]
    monkeypatch.setattr(mod, "_read_accounts", lambda p: (p, accounts))
    monkeypatch.setattr(mod, "_usage_for_accounts", lambda accs, p: None)
    report = mod.auto_rotate()
    assert report["ok"] is True and report["alive"] == [] and report["promoted"] is None
    assert "No login is usage-eligible" in mod.render_auto(report)


def test_pool_status_usage_only_when_asked(mod, monkeypatch):
    monkeypatch.setattr(mod, "_pool_strategy", lambda p: "fill_first")
    monkeypatch.setattr(mod, "_read_accounts", lambda p: (p, [_acct(mod, 0, "a@x")]))
    calls = []
    monkeypatch.setattr(mod, "_usage_for_accounts", lambda accs, p: calls.append(p) or {"id0": _usage(week=7)})
    plain = mod.pool_status()
    assert calls == [] and plain["accounts"][0]["usage"] is None and plain["usage_available"] is False
    rich = mod.pool_status(with_usage=True)
    assert calls == ["anthropic"]
    assert rich["accounts"][0]["usage"]["week"]["used_percent"] == 7 and rich["usage_available"] is True



# -- automatic mode (hook-driven, throttled) ----------------------------------------

def test_auto_keeps_current_login_within_margin(mod, monkeypatch):
    accounts = [_acct(mod, 0, "cur@x"), _acct(mod, 1, "alt@x")]
    monkeypatch.setattr(mod, "_read_accounts", lambda p: (p, accounts))
    monkeypatch.setattr(mod, "_usage_for_accounts", lambda accs, p: _by_id(accounts, {
        "cur@x": _usage(week=30), "alt@x": _usage(week=27)}))  # 3 points better: not worth a swap
    monkeypatch.setattr(mod, "switch", lambda *a, **k: pytest.fail("within margin: no swap"))
    report = mod.auto_rotate()
    assert report["promoted"] == "cur@x" and not report.get("changed")


def test_auto_swaps_when_clearly_better_and_reports_changed(mod, monkeypatch):
    accounts = [_acct(mod, 0, "cur@x"), _acct(mod, 1, "alt@x")]
    monkeypatch.setattr(mod, "_read_accounts", lambda p: (p, accounts))
    monkeypatch.setattr(mod, "_usage_for_accounts", lambda accs, p: _by_id(accounts, {
        "cur@x": _usage(week=40), "alt@x": _usage(week=10)}))
    monkeypatch.setattr(mod, "switch", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(mod, "list_accounts", lambda p="anthropic", with_usage=True: {"accounts": []})
    report = mod.auto_rotate()
    assert report["promoted"] == "alt@x" and report["changed"] is True


def test_maybe_auto_rotate_is_throttled_across_calls(mod, monkeypatch):
    _opt_in(mod)
    runs = []
    monkeypatch.setattr(mod, "auto_rotate", lambda provider="anthropic", dry_run=False:
                        runs.append(provider) or {"ok": True, "promoted": "a@x"})
    t0 = 1_000_000.0
    assert mod.maybe_auto_rotate(background=False, now=t0) is True
    assert mod.maybe_auto_rotate(background=False, now=t0 + 60) is False          # inside the interval
    assert mod.maybe_auto_rotate(background=False, now=t0 + mod.AUTO_INTERVAL_SECONDS + 1) is True
    assert len(runs) == 2
    info = mod.auto_info()
    assert info["enabled"] is True and info["last_promoted"] == "a@x" and info["last_error"] is None


def test_auto_rotation_is_off_by_default(mod, monkeypatch):
    """Reviewer requirement: background rotation must be opt-in."""
    monkeypatch.setattr(mod, "auto_rotate", lambda *a, **k: pytest.fail("not opted in"))
    assert mod.auto_enabled() is False
    assert mod.maybe_auto_rotate(background=False) is False
    assert mod.auto_info()["enabled"] is False


def test_auto_rotation_ignores_env_var(mod, monkeypatch):
    """The old default-on env switch is gone: .env is for secrets only."""
    monkeypatch.setenv("HERMES_CPS_AUTO_ROTATE", "1")
    assert mod.auto_enabled() is False


def test_auto_rotation_opt_in_via_config_yaml(mod, monkeypatch):
    calls = []
    monkeypatch.setattr(mod, "auto_rotate", lambda *a, **k: calls.append(1) or {"promoted": None})
    _opt_in(mod, True)
    assert mod.auto_enabled() is True
    assert mod.maybe_auto_rotate(background=False, now=7_000_000.0) is True
    assert calls == [1]
    _opt_in(mod, "true")
    assert mod.auto_enabled() is True
    for off in (False, "false", "0", None):
        _opt_in(mod, off)
        assert mod.auto_enabled() is False


def test_auto_enabled_survives_broken_config(mod, monkeypatch):
    def boom():
        raise RuntimeError("config unreadable")
    _fake_module(monkeypatch, "hermes_cli.config", load_config=boom)
    monkeypatch.setattr(mod, "_hermes_home", lambda: Path("/nonexistent-dir-cps"))
    assert mod.auto_enabled() is False


def test_auto_worker_failure_is_recorded_not_raised(mod, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("usage down")
    monkeypatch.setattr(mod, "auto_rotate", boom)
    _opt_in(mod)
    assert mod.maybe_auto_rotate(background=False, now=5_000_000.0) is True
    assert mod.auto_info()["last_error"] == "RuntimeError"
    assert mod._auto_running is False  # slot released, next interval can run


def test_pre_llm_call_hook_never_raises(mod, monkeypatch):
    monkeypatch.setattr(mod, "maybe_auto_rotate", lambda *a, **k: 1 / 0)
    assert mod._on_pre_llm_call(session_id="s") is None


def test_register_adds_pre_llm_call_hook(mod):
    hooks = {}
    ctx = SimpleNamespace(register_hook=lambda n, f: hooks.update({n: f}),
                          register_tool=lambda **k: None, register_cli_command=lambda **k: None,
                          register_command=lambda *a, **k: None)
    mod.register(ctx)
    assert set(hooks) == {"pre_tool_call", "pre_llm_call"}


def test_usage_includes_benched_logins_but_not_dead(mod, monkeypatch):
    accounts = [_acct(mod, 0, "ok@x"), _acct(mod, 1, "bench@x", status="exhausted", bench_until=time.time() + 600),
                _acct(mod, 2, "dead@x", status="dead")]
    seen = []
    entries = [SimpleNamespace(id=a.id, label=a.label, access_token="tok-" + a.label, auth_type="oauth", source="manual:x") for a in accounts]
    monkeypatch.setattr(mod, "_load_pool", lambda p: SimpleNamespace(entries=lambda: entries))
    win = lambda label, pct, scope="account": SimpleNamespace(label=label, used_percent=pct, scope=scope)
    fake = types.ModuleType("agent.account_usage")
    fake.fetch_account_usage = lambda provider, api_key=None, read_only=False: (
        seen.append(api_key) or SimpleNamespace(windows=[
            win("Current session", 12.0), win("Current week", 34.0),
            win("Opus week", 50.0, "model"), win("Sonnet week", 80.0, "model")]))
    monkeypatch.setitem(sys.modules, "agent.account_usage", fake)
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    out = mod._usage_for_accounts(accounts)
    assert set(out) == {"id0", "id1"} and sorted(seen) == ["tok-bench@x", "tok-ok@x"]
    assert out["id0"]["five_hour"]["used_percent"] == 12.0
    assert out["id0"]["week"]["used_percent"] == 34.0
    assert out["id0"]["scoped"]["used_percent"] == 80.0  # tightest model cap wins
    assert out["id0"]["scoped"]["label"] == "Sonnet week"  # family label kept, not a generic "model"


# -- review follow-up: fresh eligibility, entry-id keys, display ------------------------

def _seq_reads(monkeypatch, mod, first, later):
    """_read_accounts: the first call sees *first*, every later call *later*."""
    state = {"n": 0}

    def reader(p):
        state["n"] += 1
        return p, (first if state["n"] == 1 else later)

    monkeypatch.setattr(mod, "_read_accounts", reader)
    return state


@pytest.mark.parametrize("kind", ["cooldown_unknown", "benched", "dead"])
def test_auto_declines_winner_that_became_ineligible_after_the_usage_fetch(mod, monkeypatch, kind):
    busy, fresh = _acct(mod, 0, "busy@x"), _acct(mod, 1, "fresh@x")
    first = [busy, fresh]
    changed = {
        "cooldown_unknown": _acct(mod, 1, "fresh@x", status="exhausted", bench_unknown=True,
                                  notes=["(cooldown unknown)"]),
        "benched": _acct(mod, 1, "fresh@x", status="exhausted", bench_until=time.time() + 600,
                         notes=["(10m left)"]),
        "dead": _acct(mod, 1, "fresh@x", status="dead"),
    }[kind]
    state = _seq_reads(monkeypatch, mod, first, [busy, changed])
    monkeypatch.setattr(mod, "_usage_for_accounts", lambda accs, p: _by_id(first, {
        "busy@x": _usage(week=90), "fresh@x": _usage(week=0)}))
    monkeypatch.setattr(mod, "_pool_strategy", lambda p: "fill_first")
    monkeypatch.setattr(mod, "_move_to_front", lambda *a: pytest.fail("a declined winner must not be moved"))
    monkeypatch.setattr(mod, "list_accounts", lambda *a, **k: pytest.fail("nothing moved: no re-list"))
    report = mod.auto_rotate(dry_run=False)
    assert state["n"] >= 2  # the pool was re-read before the move
    assert report["ok"] is True and report["promoted"] is None and not report.get("changed")
    assert report["declined"]["account"] == "fresh@x" and report["declined"]["reason"]
    text = mod.render_auto(report)
    assert "fresh@x looked eligible" in text and "order unchanged this run" in text
    assert "priority 0 →" not in text and "No login is usage-eligible" not in text


def test_manual_switch_can_still_override_a_benched_or_unknown_cooldown_login(mod, monkeypatch):
    accounts = [_acct(mod, 0, "a@x"),
                _acct(mod, 1, "u@x", status="exhausted", bench_unknown=True, notes=["(cooldown unknown)"])]
    monkeypatch.setattr(mod, "_read_accounts", lambda p: (p, accounts))
    monkeypatch.setattr(mod, "_pool_strategy", lambda p: "fill_first")
    moved = []
    monkeypatch.setattr(mod, "_move_to_front", lambda p, a, **k: moved.append(a.id))
    monkeypatch.setattr(mod, "list_accounts", lambda *a, **k: {"accounts": [
        {"label": "u@x", "id": "id1", "priority": 0}, {"label": "a@x", "id": "id0", "priority": 1}]})
    plan = mod.switch("u@x")
    assert moved == ["id1"] and plan["ok"] is True and "cooldown is unknown" in plan["warning"]
    # the same login is declined only when the caller asks for the eligibility check
    moved.clear()
    declined = mod.switch("u@x", require_eligible=True)
    assert declined["ok"] is False and declined["declined"] is True and moved == []


def test_auto_moves_winner_that_is_still_eligible_on_the_fresh_read(mod, monkeypatch):
    accounts = [_acct(mod, 0, "busy@x"), _acct(mod, 1, "fresh@x")]
    monkeypatch.setattr(mod, "_read_accounts", lambda p: (p, accounts))
    monkeypatch.setattr(mod, "_usage_for_accounts", lambda accs, p: _by_id(accounts, {
        "busy@x": _usage(week=90), "fresh@x": _usage(week=0)}))
    monkeypatch.setattr(mod, "_pool_strategy", lambda p: "fill_first")
    moved = []
    monkeypatch.setattr(mod, "_move_to_front", lambda p, a, **k: moved.append(a.id))
    monkeypatch.setattr(mod, "list_accounts", lambda *a, **k: {"accounts": [
        {"label": "fresh@x", "id": "id1", "priority": 0}, {"label": "busy@x", "id": "id0", "priority": 1}]})
    report = mod.auto_rotate()
    assert moved == ["id1"] and report["promoted"] == "fresh@x" and report["changed"] is True
    assert report["declined"] is None


def _dup_entries():
    return [SimpleNamespace(id="idA", label="same@x", auth_type="oauth", source="manual:x", access_token="tok-A"),
            SimpleNamespace(id="idB", label="same@x", auth_type="oauth", source="manual:x", access_token="tok-B")]


def test_duplicate_labels_keep_separate_usage_and_pass_identity_id(mod, monkeypatch):
    win = lambda label, pct, scope="account": SimpleNamespace(label=label, used_percent=pct, scope=scope)
    calls = []

    def fake_fetch(provider, *, api_key=None, read_only=False, identity_id=None, **kw):
        calls.append((api_key, identity_id))
        if api_key == "tok-A":
            return SimpleNamespace(windows=[win("Current session", 10.0), win("Current week", 70.0)])
        return SimpleNamespace(windows=[win("Opus week", 3.0, "model")])  # scoped-only

    _fake_module(monkeypatch, "agent.account_usage", fetch_account_usage=fake_fetch)
    entries = _dup_entries()
    monkeypatch.setattr(mod, "_load_pool", lambda p: SimpleNamespace(entries=lambda: entries))
    accounts = [mod.Account(index=1, label="same@x", auth_type="oauth", id="idA", priority=0, source="hermes_pkce"),
                mod.Account(index=2, label="same@x", auth_type="oauth", id="idB", priority=1, source="hermes_pkce")]
    out = mod._usage_for_accounts(accounts)
    assert sorted(calls) == [("tok-A", "idA"), ("tok-B", "idB")]
    assert set(out) == {"idA", "idB"}  # nothing overwritten by the shared label
    assert out["idA"]["week"]["used_percent"] == 70.0 and "week" not in out["idB"]
    assert out["idB"] == {"scoped": {"label": "Opus week", "used_percent": 3.0, "active": False}}


def test_duplicate_labels_scoped_only_row_never_borrows_headroom_or_gets_promoted(mod, monkeypatch):
    accounts = [mod.Account(index=1, label="same@x", auth_type="oauth", id="idA", priority=0, source="hermes_pkce"),
                mod.Account(index=2, label="same@x", auth_type="oauth", id="idB", priority=1, source="hermes_pkce")]
    usage = {"idA": _usage(week=70), "idB": {"scoped": {"label": "Opus week", "used_percent": 3.0, "active": False}}}
    monkeypatch.setattr(mod, "_read_accounts", lambda p: (p, accounts))
    monkeypatch.setattr(mod, "_usage_for_accounts", lambda accs, p: usage)
    monkeypatch.setattr(mod, "switch", lambda *a, **k: pytest.fail("the scoped-only twin must not be promoted"))
    report = mod.auto_rotate(dry_run=False)
    assert report["alive"] == ["same@x (#1)"] and report["unknown"] == ["same@x (#2)"]
    assert report["unknown_reason"] == {"same@x (#2)": "scoped_only"}
    assert report["promoted"] == "same@x (#1)" and report["would_promote"] is None
    text = mod.render_auto(report)
    assert "same@x (#2) only model-scoped usage" in text


def test_duplicate_labels_promote_the_right_entry_id(mod, monkeypatch):
    accounts = [mod.Account(index=1, label="same@x", auth_type="oauth", id="idA", priority=0, source="hermes_pkce"),
                mod.Account(index=2, label="same@x", auth_type="oauth", id="idB", priority=1, source="hermes_pkce")]
    usage = {"idA": _usage(week=80), "idB": _usage(week=5)}
    monkeypatch.setattr(mod, "_read_accounts", lambda p: (p, accounts))
    monkeypatch.setattr(mod, "_usage_for_accounts", lambda accs, p: usage)
    monkeypatch.setattr(mod, "_pool_strategy", lambda p: "fill_first")
    moved = []
    monkeypatch.setattr(mod, "_move_to_front", lambda p, a, **k: moved.append(a.id))
    monkeypatch.setattr(mod, "list_accounts", lambda *a, **k: {"accounts": [
        {"label": "same@x", "id": "idB", "priority": 0}, {"label": "same@x", "id": "idA", "priority": 1}]})
    report = mod.auto_rotate()
    assert moved == ["idB"] and report["promoted"] == "same@x (#2)"


def test_list_and_status_attach_usage_by_entry_id_not_label(mod, monkeypatch):
    accounts = [mod.Account(index=1, label="same@x", auth_type="oauth", id="idA", priority=0, source="hermes_pkce"),
                mod.Account(index=2, label="same@x", auth_type="oauth", id="idB", priority=1, source="hermes_pkce")]
    usage = {"idA": _usage(week=11), "idB": _usage(week=22)}
    monkeypatch.setattr(mod, "_read_accounts", lambda p: (p, accounts))
    monkeypatch.setattr(mod, "_pool_strategy", lambda p: "fill_first")
    monkeypatch.setattr(mod, "_usage_for_accounts", lambda accs, p: usage)
    listed = mod.list_accounts("anthropic")
    assert [a["usage"]["week"]["used_percent"] for a in listed["accounts"]] == [11, 22]
    rows = mod.pool_status(with_usage=True)["accounts"]
    assert [r["id"] for r in rows] == ["idA", "idB"]
    assert [r["usage"]["week"]["used_percent"] for r in rows] == [11, 22]


def test_auto_output_names_only_measured_windows(mod, monkeypatch):
    accounts = [_acct(mod, 0, "both@x"), _acct(mod, 1, "weekonly@x"), _acct(mod, 2, "fiveonly@x")]
    usage = {"both@x": _usage(five=5, week=50),
             "weekonly@x": {"week": {"used_percent": 20.0}},
             "fiveonly@x": {"five_hour": {"used_percent": 20.0}}}
    report, _ = _run_auto(mod, monkeypatch, accounts, usage)
    assert report["measured"] == {"both@x": ["5h", "week"], "weekonly@x": ["week"], "fiveonly@x": ["5h"]}
    lines = {l.split()[1]: l for l in mod.render_auto(report).splitlines() if l.startswith("✓")}
    assert "(5h/week below 100%;" in lines["both@x"]
    assert "(week below 100%; 5h not measured;" in lines["weekonly@x"] and "5h/week" not in lines["weekonly@x"]
    assert "(5h below 100%; week not measured;" in lines["fiveonly@x"]


def test_render_keeps_model_family_label_and_unknown_cooldown(mod):
    text = mod.render_list({"provider": "anthropic", "usage_available": True, "accounts": [
        {"index": 1, "label": "a@x", "priority": 0, "current": True, "status": "ok",
         "usage": {"week": {"used_percent": 10}, "scoped": {"label": "Opus week", "used_percent": 100,
                                                          "active": True}}},
        {"index": 2, "label": "u@x", "priority": 1, "current": False, "status": "exhausted",
         "benched": "(cooldown unknown)"}]})
    assert "Opus week 100%" in text and "model cap reached" in text
    assert "benched (cooldown unknown)" in text
    auto = mod.render_auto({"ok": True, "provider": "anthropic", "alive": ["a@x"], "exhausted": [], "benched": [],
                            "cooldown_unknown": ["u@x"], "dead": [], "unknown": [], "measured": {"a@x": ["week"]},
                            "scoped_caps": {"a@x": "Opus week"}, "promoted": "a@x"})
    assert "Opus week" in auto and "u@x exhausted, cooldown unknown" in auto


# -- desktop half: pure helpers run in node with stubbed SDK imports ----------------

# -- review follow-up 2: fail-closed eligibility, exact entry ids ---------------------

def test_there_is_no_cli_text_parser_left(mod):
    # `hermes auth list` is never a read path (it loads and peeks the pool); only the snapshot is.
    assert not hasattr(mod, "_parse_list")
    assert _acct(mod, 0, "a@x").authoritative is True


def test_switch_require_eligible_declines_cli_text_rows_without_moving(mod, monkeypatch):
    # The CLI text cannot show dead logins or an exhausted row without a reset.
    rows = [_acct(mod, 0, "busy@x", authoritative=False), _acct(mod, 1, "fresh@x", authoritative=False)]
    monkeypatch.setattr(mod, "_read_accounts", lambda p: (p, rows))
    monkeypatch.setattr(mod, "_pool_strategy", lambda p: "fill_first")
    monkeypatch.setattr(mod, "_move_to_front", lambda *a, **k: pytest.fail("unverifiable state must not move"))
    out = mod.switch("id1", require_eligible=True, exact_id=True)
    assert out["ok"] is False and out["declined"] is True and "unverifiable" in out["reason"]
    # a manual switch is unchanged
    moved = []
    monkeypatch.setattr(mod, "_move_to_front", lambda p, a, **k: moved.append(a.id))
    monkeypatch.setattr(mod, "list_accounts", lambda *a, **k: {"accounts": [
        {"label": "fresh@x", "id": "id1", "priority": 0}, {"label": "busy@x", "id": "id0", "priority": 1}]})
    assert mod.switch("fresh@x")["ok"] is True and moved == ["id1"]


def test_auto_declines_when_the_fresh_read_falls_back_to_cli_text(mod, monkeypatch):
    busy, fresh = _acct(mod, 0, "busy@x"), _acct(mod, 1, "fresh@x")
    # the direct read failed on the second call; the CLI text shows fresh@x as plain "ok"
    _seq_reads(monkeypatch, mod, [busy, fresh],
               [_acct(mod, 0, "busy@x", authoritative=False), _acct(mod, 1, "fresh@x", authoritative=False)])
    monkeypatch.setattr(mod, "_usage_for_accounts", lambda accs, p: _by_id([busy, fresh], {
        "busy@x": _usage(week=90), "fresh@x": _usage(week=0)}))
    monkeypatch.setattr(mod, "_pool_strategy", lambda p: "fill_first")
    monkeypatch.setattr(mod, "_move_to_front", lambda *a, **k: pytest.fail("must not move"))
    report = mod.auto_rotate()
    assert report["ok"] is True and report["promoted"] is None and not report.get("changed")
    assert report["declined"]["account"] == "fresh@x" and "unverifiable" in report["declined"]["reason"]


def test_auto_never_ranks_cli_text_rows(mod, monkeypatch):
    rows = [_acct(mod, 0, "busy@x", authoritative=False), _acct(mod, 1, "fresh@x", authoritative=False)]
    monkeypatch.setattr(mod, "_read_accounts", lambda p: (p, rows))
    monkeypatch.setattr(mod, "_usage_for_accounts", lambda accs, p: _by_id(rows, {
        "busy@x": _usage(week=90), "fresh@x": _usage(week=0)}))
    monkeypatch.setattr(mod, "switch", lambda *a, **k: pytest.fail("no candidate, no move"))
    report = mod.auto_rotate()
    assert report["alive"] == [] and report["promoted"] is None and report["would_promote"] is None
    assert report["unknown_reason"] == {"busy@x": "pool_unverified", "fresh@x": "pool_unverified"}
    assert "not read directly from the pool" in mod.render_auto(report)


def _three(mod):
    return [_acct(mod, 0, "busy@x"), _acct(mod, 1, "fresh@x"), _acct(mod, 2, "other@x")]


def test_exact_id_switch_does_not_resolve_labels_numbers_or_prefixes(mod, monkeypatch):
    accts = _three(mod)
    monkeypatch.setattr(mod, "_read_accounts", lambda p: (p, accts))
    monkeypatch.setattr(mod, "_move_to_front", lambda *a, **k: pytest.fail("must not move"))
    for target in ("fresh@x", "#2", "fre", "FRESH@X", "id", ""):
        out = mod.switch(target, exact_id=True)
        assert out["ok"] is False and "exact entry id lookup failed" in out["error"], target


def test_exact_id_fails_closed_when_the_entry_disappeared(mod, monkeypatch):
    accts = [_acct(mod, 0, "busy@x")]
    monkeypatch.setattr(mod, "_read_accounts", lambda p: (p, accts))
    monkeypatch.setattr(mod, "_move_to_front", lambda *a, **k: pytest.fail("must not move"))
    out = mod.switch("id1", require_eligible=True, exact_id=True)
    assert out["ok"] is False and not out.get("declined") and "may have been removed" in out["error"]


def test_exact_id_fails_closed_when_the_id_collides_with_a_label(mod, monkeypatch):
    victim = mod.Account(index=1, label="id1", auth_type="oauth", id="idZ", priority=0, source="hermes_pkce")
    target = mod.Account(index=2, label="fresh@x", auth_type="oauth", id="id1", priority=1, source="hermes_pkce")
    monkeypatch.setattr(mod, "_read_accounts", lambda p: (p, [victim, target]))
    monkeypatch.setattr(mod, "_move_to_front", lambda *a, **k: pytest.fail("must not move"))
    out = mod.switch("id1", exact_id=True)
    assert out["ok"] is False and "collides with another entry's label" in out["error"]
    # the label-capable manual path is untouched by the flag (still first exact match)
    assert mod._find([victim, target], "id1").id == "idZ"


def test_exact_id_duplicate_ids_fail_closed(mod):
    a = mod.Account(index=1, label="a", auth_type="oauth", id="dup", priority=0, source="s")
    b = mod.Account(index=2, label="b", auth_type="oauth", id="dup", priority=1, source="s")
    assert mod._find_exact_id([a, b], "dup")[0] is None


def _pool_entry(i, label, *, status=None, reset=None, token="", eid=None, source="manual:hermes_pkce",
                auth_type="oauth"):
    return SimpleNamespace(
        id=eid or f"id{i}", label=label, auth_type=auth_type, priority=i, source=source,
        last_status=status, last_status_at=time.time() if status else None, last_error_code=None,
        last_error_reset_at=reset, failure_reason=None, access_token=token)


def test_exact_id_move_never_falls_back_to_the_label_resolving_cli(mod, monkeypatch):
    acct = _acct(mod, 1, "fresh@x")
    monkeypatch.setattr(mod, "_run", lambda *a, **k: pytest.fail("CLI fallback must not run for exact ids"))
    monkeypatch.setattr(mod, "_load_writable_pool", lambda p: None)
    assert "not possible" in mod._move_to_front("anthropic", acct, exact_id=True)

    def boom(_id, _pos):
        raise RuntimeError("x")

    entries = [_pool_entry(0, "busy@x"), _pool_entry(1, "fresh@x")]
    monkeypatch.setattr(mod, "_load_writable_pool", lambda p: SimpleNamespace(move_entry=boom, entries=lambda: entries))
    assert "not retrying by label" in mod._move_to_front("anthropic", acct, exact_id=True)
    # non-exact keeps its fallback
    monkeypatch.setattr(mod, "_run", lambda *a, **k: "Set anthropic credential #1 priority")
    assert mod._move_to_front("anthropic", acct) is None


def test_switch_verification_compares_the_resolved_entry_id_only(mod, monkeypatch):
    """Astra round 5: two accounts with the same label must not yield a false
    verified_first / CPS_RESULT ok when a *different* entry id is priority 0 — on the
    exact-id path AND on the friendly label path (the old test expected that false
    positive here)."""
    accts = [_acct(mod, 0, "busy@x"), _acct(mod, 1, "fresh@x")]
    monkeypatch.setattr(mod, "_read_accounts", lambda p: (p, accts))
    monkeypatch.setattr(mod, "_pool_strategy", lambda p: "fill_first")
    moved = []
    monkeypatch.setattr(mod, "_move_to_front", lambda p, a, **k: moved.append(a.id))
    # a different entry that merely carries the same label sits at priority 0
    monkeypatch.setattr(mod, "list_accounts", lambda *a, **k: {"accounts": [
        {"label": "fresh@x", "id": "idOTHER", "priority": 0}, {"label": "fresh@x", "id": "id1", "priority": 1}]})
    assert mod.switch("id1", exact_id=True)["verified_first"] is False
    manual = mod.switch("fresh@x")  # friendly path resolves id1 — must NOT verify on the label twin
    assert manual["ok"] is False and manual["unverified"] is True and manual["verified_first"] is False
    assert "may or may not have changed" in manual["error"]
    assert "Switch unverified" in mod.render_switch(manual) and "Switched" not in mod.render_switch(manual)
    assert mod._switch_outcome(manual) == "unverified"
    # the same resolved id first again → verified, and ok stays true
    moved.clear()
    monkeypatch.setattr(mod, "list_accounts", lambda *a, **k: {"accounts": [
        {"label": "fresh@x", "id": "id1", "priority": 0}, {"label": "busy@x", "id": "id0", "priority": 1}]})
    ok = mod.switch("fresh@x")
    assert ok["ok"] is True and ok["verified_first"] is True and moved == ["id1"]


def test_auto_uses_the_exact_id_path_and_reports_unverified_moves(mod, monkeypatch):
    accts = [_acct(mod, 0, "busy@x"), _acct(mod, 1, "fresh@x")]
    monkeypatch.setattr(mod, "_read_accounts", lambda p: (p, accts))
    monkeypatch.setattr(mod, "_usage_for_accounts", lambda accs, p: _by_id(accts, {
        "busy@x": _usage(week=90), "fresh@x": _usage(week=0)}))
    monkeypatch.setattr(mod, "_pool_strategy", lambda p: "fill_first")
    seen = {}
    monkeypatch.setattr(mod, "_move_to_front", lambda p, a, **k: seen.update(id=a.id, kw=k))
    monkeypatch.setattr(mod, "list_accounts", lambda *a, **k: {"accounts": [
        {"label": "fresh@x", "id": "idOTHER", "priority": 0}, {"label": "fresh@x", "id": "id1", "priority": 1}]})
    report = mod.auto_rotate()
    assert seen == {"id": "id1", "kw": {"exact_id": True, "require_eligible": True}}
    assert report["ok"] is False and report["promoted"] is None and report["changed"] is True
    assert "could not be verified by entry id" in report["error"]


import json as _json
import shutil as _shutil
import subprocess as _subprocess

PLUGIN_JS = Path(__file__).resolve().parents[1] / "desktop" / "plugin.js"


@pytest.mark.skipif(_shutil.which("node") is None, reason="node not installed")
def test_desktop_chip_shows_unknown_cooldown_and_model_family_label(tmp_path):
    src = PLUGIN_JS.read_text()
    src = src.replace("import * as sdk from '@hermes/plugin-sdk'", "const sdk = new Proxy({}, {get: () => () => null})")
    src = src.replace("import { useState } from 'react'", "const useState = () => [false, () => {}]")
    src = src.replace("import { jsx, jsxs } from 'react/jsx-runtime'",
                      "const jsx = (type, props, key) => ({ type, props, key }); const jsxs = jsx")
    src = src.replace("export default {", "globalThis.__plugin = {")
    probe = """
const en = MESSAGES.en
const t = (k, ...a) => (typeof en[k] === 'function' ? en[k](...a) : en[k])
const flat = (n) => (n == null || typeof n !== 'object') ? String(n ?? '') :
  Array.isArray(n) ? n.map(flat).join('|') :
  typeof n.type === 'function' ? flat(n.type(n.props)) :
  [flat(n.props?.children), n.props?.title ?? ''].join('|')
const out = {
  unknown: rowState({ status: 'benched', cooldown_unknown: true, bench_left: null }, t),
  timed: rowState({ status: 'benched', cooldown_unknown: false, bench_left: '10m' }, t),
  plain: rowState({ status: 'benched' }, t),
  bars: flat(UsageBars({ usage: { week: { used_percent: 10 },
        scoped: { label: 'Opus week', used_percent: 100 } }, t })),
  fallback: flat(UsageBars({ usage: { scoped: { used_percent: 5 } }, t })),
  de: MESSAGES.de.cooldownUnknown
}
console.log(JSON.stringify(out))
"""
    f = tmp_path / "probe.mjs"
    f.write_text(src + "\n" + probe)
    res = _subprocess.run(["node", str(f)], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    out = _json.loads(res.stdout.strip().splitlines()[-1])
    assert out["unknown"] == "benched \u00b7 cooldown unknown"
    assert out["timed"] == "benched 10m" and out["plain"] == "benched"
    assert "Opus week" in out["bars"] and "model" not in out["bars"].replace("Opus week", "")
    assert "model" in out["fallback"]  # generic label only when the backend sent none
    assert "Cooldown" in out["de"]


# -- Astra round 3: read-only snapshot, same-pool revalidation, desktop exact ids -----------

import ast as _ast
import json
import argparse as _argparse


def _snapshot_env(monkeypatch, mod, rows, *, load_pool=None, reader=None):
    """Fake core: read_credential_pool returns *rows*; load_pool must never run for reads."""
    class Cred(SimpleNamespace):
        @classmethod
        def from_dict(cls, provider, row):
            return cls(**{"label": row.get("id"), "auth_type": "oauth", "priority": 0, "source": "manual:x",
                          "last_status": None, "last_error_reset_at": None, "access_token": "", **row})

    def no_load(provider):
        pytest.fail("load_pool() must not be used for a read: it can seed, prune and persist")

    _fake_module(monkeypatch, "agent.credential_pool", PooledCredential=Cred, load_pool=load_pool or no_load,
                 read_credential_pool=reader or (lambda provider: rows),
                 get_pool_strategy=lambda p: "fill_first")
    # No private-route comeback: hermes_cli.auth has no read accessor here, so any
    # regression to importing it fails closed (and these tests fail with it).
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", types.ModuleType("hermes_cli.auth"))
    monkeypatch.setattr(mod, "_run", lambda *a, **k: pytest.fail("no CLI fallback while the snapshot works"))


def test_reads_use_the_snapshot_never_load_pool_or_peek(mod, monkeypatch):
    now = time.time()
    rows = [
        {"id": "idB", "label": "b@x", "priority": 1, "access_token": "tok-b"},
        {"id": "idD", "label": "dead@x", "priority": 0, "last_status": "dead"},
        {"id": "idA", "label": "a@x", "priority": 2, "access_token": "tok-a",
         "last_status": "exhausted", "last_error_reset_at": _iso(now + 600)},
    ]
    _snapshot_env(monkeypatch, mod, rows)
    calls = []
    _fake_module(monkeypatch, "agent.account_usage",
                 fetch_account_usage=lambda provider, api_key=None, read_only=False, identity_id=None: (
                     calls.append((api_key, identity_id)) or SimpleNamespace(windows=[
                         SimpleNamespace(label="Current week", used_percent=10.0, scope="account")])))
    data = mod.list_accounts()
    assert [a["id"] for a in data["accounts"]] == ["idD", "idB", "idA"]  # priority order
    by = {a["id"]: a for a in data["accounts"]}
    assert by["idB"]["current"] is True and by["idD"]["current"] is False and by["idA"]["current"] is False
    assert by["idD"]["needs_login"] and by["idA"]["benched"]
    assert sorted(calls) == [("tok-a", "idA"), ("tok-b", "idB")]  # tokens come from the snapshot rows
    status = mod.pool_status()
    assert status["active"]["id"] == "idB"


def test_borrowed_row_without_token_gets_no_usage_and_is_never_promoted(mod, monkeypatch):
    rows = [{"id": "idA", "label": "a@x", "priority": 0, "access_token": "tok-a"},
            {"id": "idC", "label": "cc@x", "priority": 1, "source": "claude_code"}]  # secrets stripped on disk
    _snapshot_env(monkeypatch, mod, rows)
    _fake_module(monkeypatch, "agent.account_usage",
                 fetch_account_usage=lambda provider, api_key=None, read_only=False, identity_id=None: SimpleNamespace(
                     windows=[SimpleNamespace(label="Current week", used_percent=5.0, scope="account")]))
    _, accounts = mod._read_accounts("anthropic")
    assert set(mod._usage_for_accounts(accounts)) == {"idA"}
    report = mod.auto_rotate(dry_run=True)
    assert report["unknown_reason"]["cc@x"] == "no_usage" and "cc@x" not in report["alive"]


def test_unreadable_pool_fails_closed_without_cli_fallback_or_load_pool(mod, monkeypatch):
    def boom(provider):
        raise OSError("EIO")

    _snapshot_env(monkeypatch, mod, [], reader=boom)
    with pytest.raises(mod.PoolReadError):
        mod.list_accounts()
    out = json.loads(mod._tool_account_switch({"action": "list"}))
    assert out["ok"] is False and "nothing was changed" in out["error"]
    assert "not read" in mod._slash("list") and "nothing was changed" in mod._slash("list")
    moved = []
    monkeypatch.setattr(mod, "_load_writable_pool", lambda p: moved.append(p))
    assert json.loads(mod._tool_account_switch({"action": "switch", "target": "a"}))["ok"] is False
    assert moved == []  # no write path was even opened


def test_missing_read_accessor_fails_closed_on_every_read_path(mod, monkeypatch, capsys):
    """Older core whose public agent.credential_pool lacks the read_credential_pool
    re-export: no `hermes auth list` fallback, no load_pool, no private import."""
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", types.ModuleType("hermes_cli.auth"))  # no private route either
    _fake_module(monkeypatch, "agent.credential_pool", PooledCredential=SimpleNamespace,
                 load_pool=lambda p: pytest.fail("load_pool must not be a read fallback"),
                 get_pool_strategy=lambda p: "fill_first")
    monkeypatch.setattr(mod, "_run", lambda *a, **k: pytest.fail("no CLI fallback (auth list loads and peeks the pool)"))
    for fn in (mod._read_accounts, mod.list_accounts, mod.pool_status, mod.auto_rotate,
               lambda p: mod.switch("a", p, dry_run=True), lambda p: mod.switch("a", p, dry_run=True, exact_id=True)):
        with pytest.raises(mod.PoolReadError, match="read-only"):
            fn("anthropic")
    # tool / slash / CLI report it and change nothing; the CLI exits nonzero
    assert json.loads(mod._tool_account_switch({"action": "list"}))["ok"] is False
    assert json.loads(mod._tool_account_switch({"action": "plan", "target": "a"}))["ok"] is False
    assert "nothing was changed" in mod._slash("list")
    for cmd in ("list", "status", "setup", "plan", "switch", "auto"):
        with pytest.raises(SystemExit) as ei:
            mod._handle_cli(SimpleNamespace(cmd=cmd, target="a", provider="anthropic", dry_run=True, exact_id=False))
        assert ei.value.code == 1
    out = capsys.readouterr().out
    assert "nothing was changed" in out and "failed" in out


def _functions_calling(tree, predicate):
    hits = {}
    for fn in [n for n in _ast.walk(tree) if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))]:
        for node in _ast.walk(fn):
            if isinstance(node, _ast.Call) and predicate(node.func):
                hits.setdefault(fn.name, 0)
                hits[fn.name] += 1
    return set(hits)


def test_source_calls_load_pool_and_peek_only_where_a_write_is_allowed(mod):
    tree = _ast.parse(PLUGIN_FILE.read_text())
    name = lambda f: f.id if isinstance(f, _ast.Name) else (f.attr if isinstance(f, _ast.Attribute) else "")
    assert _functions_calling(tree, lambda f: name(f) == "load_pool") == {"_load_writable_pool"}
    assert _functions_calling(tree, lambda f: name(f) in {"peek", "choose", "reclaim", "has_available",
                                                         "lift_reopened_cooldowns", "next_available_at"}) == set()
    assert _functions_calling(tree, lambda f: name(f) == "_load_writable_pool") == {"_move_to_front"}
    assert _functions_calling(tree, lambda f: name(f) == "move_entry") == {"_move_to_front"}
    # `hermes auth list` is never invoked anywhere: it loads and peeks the pool and can persist.
    assert '"auth", "list"' not in PLUGIN_FILE.read_text()
    assert not hasattr(mod, "_parse_list")


# -- same-pool revalidation of the exact id right before the move ---------------------------

def _writable(entries, moved):
    return SimpleNamespace(entries=lambda: entries, move_entry=lambda i, pos: moved.append((i, pos)) or object())


def test_move_declines_when_the_move_pool_shows_the_winner_exhausted_without_reset(mod, monkeypatch):
    moved = []
    entries = [_pool_entry(0, "busy@x"), _pool_entry(1, "fresh@x", status="exhausted")]
    monkeypatch.setattr(mod, "_load_writable_pool", lambda p: _writable(entries, moved))
    out = mod._move_to_front("anthropic", _acct(mod, 1, "fresh@x"), exact_id=True, require_eligible=True)
    assert isinstance(out, mod._MoveDeclined) and "unknown cooldown" in out and moved == []


@pytest.mark.parametrize("status,reset", [("dead", None), ("exhausted", "FUTURE")])
def test_move_declines_dead_or_benched_winner_on_the_move_pool(mod, monkeypatch, status, reset):
    moved = []
    reset = _iso(time.time() + 900) if reset else None
    entries = [_pool_entry(0, "busy@x"), _pool_entry(1, "fresh@x", status=status, reset=reset)]
    monkeypatch.setattr(mod, "_load_writable_pool", lambda p: _writable(entries, moved))
    out = mod._move_to_front("anthropic", _acct(mod, 1, "fresh@x"), exact_id=True, require_eligible=True)
    assert isinstance(out, mod._MoveDeclined) and moved == []


def test_move_fails_closed_when_the_id_vanished_or_collides_with_a_label_on_the_move_pool(mod, monkeypatch):
    moved = []
    gone = [_pool_entry(0, "busy@x")]
    monkeypatch.setattr(mod, "_load_writable_pool", lambda p: _writable(gone, moved))
    out = mod._move_to_front("anthropic", _acct(mod, 1, "fresh@x"), exact_id=True, require_eligible=True)
    assert out and not isinstance(out, mod._MoveDeclined) and "no pool entry" in out
    clash = [_pool_entry(0, "id1"), _pool_entry(1, "fresh@x")]  # entry 0's LABEL equals entry 1's id
    monkeypatch.setattr(mod, "_load_writable_pool", lambda p: _writable(clash, moved))
    out = mod._move_to_front("anthropic", _acct(mod, 1, "fresh@x"), exact_id=True, require_eligible=True)
    assert "collides" in out and moved == []
    dup = [_pool_entry(0, "a", eid="id1"), _pool_entry(1, "fresh@x")]
    monkeypatch.setattr(mod, "_load_writable_pool", lambda p: _writable(dup, moved))
    assert "not unique" in mod._move_to_front("anthropic", _acct(mod, 1, "fresh@x"), exact_id=True, require_eligible=True)
    assert moved == []


def test_move_runs_on_the_same_pool_object_it_validated(mod, monkeypatch):
    moved = []
    entries = [_pool_entry(0, "busy@x"), _pool_entry(1, "fresh@x")]
    loads = []
    monkeypatch.setattr(mod, "_load_writable_pool", lambda p: loads.append(p) or _writable(entries, moved))
    assert mod._move_to_front("anthropic", _acct(mod, 1, "fresh@x"), exact_id=True, require_eligible=True) is None
    assert moved == [("id1", 0)] and loads == ["anthropic"]  # one load serves check and move


def test_state_change_between_precheck_and_move_reload_declines_end_to_end(mod, monkeypatch):
    """First read: winner eligible. The reload used for the move: winner got benched/dead meanwhile."""
    accts = [_acct(mod, 0, "busy@x"), _acct(mod, 1, "fresh@x")]
    monkeypatch.setattr(mod, "_read_accounts", lambda p: (p, accts))  # precheck snapshot: all fine
    monkeypatch.setattr(mod, "_usage_for_accounts", lambda accs, p: _by_id(accts, {
        "busy@x": _usage(week=90), "fresh@x": _usage(week=0)}))
    monkeypatch.setattr(mod, "_pool_strategy", lambda p: "fill_first")
    moved = []
    changed = [_pool_entry(0, "busy@x"), _pool_entry(1, "fresh@x", status="dead")]
    monkeypatch.setattr(mod, "_load_writable_pool", lambda p: _writable(changed, moved))
    monkeypatch.setattr(mod, "list_accounts", lambda *a, **k: pytest.fail("no verification read after a decline"))
    report = mod.auto_rotate()
    assert moved == []
    assert report["declined"] and report["declined"]["account"] == "fresh@x" and "dead" in report["declined"]["reason"]
    assert report["promoted"] is None and not report.get("changed") and report["ok"] is True
    assert "order unchanged" in mod.render_auto(report)


def test_manual_switch_keeps_label_resolution_and_does_not_require_eligibility(mod, monkeypatch):
    moved = []
    entries = [_pool_entry(0, "busy@x"), _pool_entry(1, "fresh@x", status="exhausted")]
    monkeypatch.setattr(mod, "_load_writable_pool", lambda p: _writable(entries, moved))
    assert mod._move_to_front("anthropic", _acct(mod, 1, "fresh@x")) is None
    assert moved == [("id1", 0)]


# -- desktop rows: exact entry id, typed commands keep friendly matching ------------------------

def _collision_accounts(mod):
    # row 1's LABEL is "id1", which is row 2's entry ID
    a = mod.Account(index=1, label="id1", auth_type="oauth", id="idX", priority=0, source="hermes_pkce")
    b = mod.Account(index=2, label="real@x", auth_type="oauth", id="id1", priority=1, source="hermes_pkce")
    return [a, b]


def test_typed_switch_by_label_or_id_still_uses_the_friendly_matcher(mod, monkeypatch):
    accts = _collision_accounts(mod)
    monkeypatch.setattr(mod, "_read_accounts", lambda p: (p, accts))
    monkeypatch.setattr(mod, "_pool_strategy", lambda p: "fill_first")
    assert mod.switch("id1", dry_run=True)["account"] == "id1"  # ordinary matcher: label match wins (typed command)
    assert mod.switch("real", dry_run=True)["account"] == "real@x"  # unique prefix still works
    assert mod.switch("#2", dry_run=True)["account"] == "real@x"


def test_desktop_row_click_never_switches_the_label_twin(mod, monkeypatch, capsys):
    accts = _collision_accounts(mod)
    monkeypatch.setattr(mod, "_read_accounts", lambda p: (p, accts))
    monkeypatch.setattr(mod, "_pool_strategy", lambda p: "fill_first")
    monkeypatch.setattr(mod, "_move_to_front", lambda *a, **k: pytest.fail("ambiguous exact id must not move anything"))
    args = SimpleNamespace(cmd="switch", target="id1", provider="anthropic", dry_run=False, exact_id=True)
    with pytest.raises(SystemExit) as ei:
        mod._handle_cli(args)
    out = capsys.readouterr().out
    assert ei.value.code == 1 and "Switch failed" in out and "collides" in out
    assert out.strip().splitlines()[-1] == "CPS_RESULT failed"


def test_desktop_row_click_moves_exactly_the_clicked_entry(mod, monkeypatch, capsys):
    accts = [mod.Account(index=1, label="a@x", auth_type="oauth", id="idA", priority=0, source="hermes_pkce"),
             mod.Account(index=2, label="b@x", auth_type="oauth", id="idB", priority=1, source="hermes_pkce")]
    monkeypatch.setattr(mod, "_read_accounts", lambda p: (p, accts))
    monkeypatch.setattr(mod, "_pool_strategy", lambda p: "fill_first")
    moved = []
    monkeypatch.setattr(mod, "_move_to_front", lambda p, a, **k: moved.append((a.id, k)))
    monkeypatch.setattr(mod, "list_accounts", lambda *a, **k: {"accounts": [
        {"label": "b@x", "id": "idB", "priority": 0}, {"label": "a@x", "id": "idA", "priority": 1}]})
    mod._handle_cli(SimpleNamespace(cmd="switch", target="idB", provider="anthropic", dry_run=False, exact_id=True))
    assert moved == [("idB", {"exact_id": True})]
    assert "Switched" in capsys.readouterr().out


def test_cli_parser_accepts_exact_id_and_the_desktop_passes_it(mod):
    p = _argparse.ArgumentParser()
    mod._setup_argparse(p)
    ns = p.parse_args(["switch", "--exact-id", "id1"])
    assert ns.exact_id is True and ns.target == "id1"
    assert p.parse_args(["switch", "id1"]).exact_id is False
    js = PLUGIN_JS.read_text()
    assert "rowSwitchArgv" in js and "['switch', '--exact-id', row.id]" in js  # row clicks: exact id, one helper


# -- Astra round 3, second batch: fail-closed reads, oauth revalidation, safe usage tokens, outcome reporting ------------------


def _snap(entries):
    return SimpleNamespace(entries=lambda: list(entries))


def test_usage_token_entries_filters_on_the_second_snapshot(mod):
    now = time.time()
    token_rows = [
        SimpleNamespace(id="idA", label="a@x", auth_type="oauth", source="manual:x",
                        last_status=None, access_token="tok-A"),
        # legacy borrowed row that still carries a token: never probed
        SimpleNamespace(id="idB", label="cc@x", auth_type="oauth", source="claude_code",
                        last_status=None, access_token="tok-B"),
        # turned into an API key since the first snapshot
        SimpleNamespace(id="idC", label="c@x", auth_type="api_key", source="manual:x",
                        last_status=None, access_token="tok-C"),
        # died between the snapshots
        SimpleNamespace(id="idD", label="d@x", auth_type="oauth", source="manual:x",
                        last_status="dead", access_token="tok-D"),
        # source is gone entirely: not provably pool-owned
        SimpleNamespace(id="idE", label="e@x", auth_type="oauth", source="",
                        last_status=None, access_token="tok-E"),
        # Hermes' own root-file login (hermes auth login): an explicit Hermes flow
        SimpleNamespace(id="idF", label="f@x", auth_type="oauth", source="hermes_pkce",
                        last_status=None, access_token="tok-F"),
    ]
    got = mod._usage_token_entries(token_rows, {"idA", "idB", "idC", "idD", "idE", "idF"})
    assert got == [("idA", "tok-A"), ("idF", "tok-F")]
    # a duplicated id keeps BOTH tokens at home (ambiguous which entry owns the token)
    dup = token_rows + [SimpleNamespace(id="idA", label="a2@x", auth_type="oauth", source="manual:x",
                                        last_status=None, access_token="tok-A2")]
    assert mod._usage_token_entries(dup, {"idA"}) == []
    # nothing wanted -> nothing probed
    assert mod._usage_token_entries(token_rows, set()) == []


def test_usage_fetch_rechecks_the_actual_second_snapshot_entry(mod, monkeypatch):
    """State/source changed between the first read (accounts) and the token snapshot: no probe."""
    calls = []
    _fake_module(monkeypatch, "agent.account_usage",
                 fetch_account_usage=lambda provider, api_key=None, read_only=False, identity_id=None: (
                     calls.append((api_key, identity_id)) or SimpleNamespace(windows=[
                         SimpleNamespace(label="Current week", used_percent=10.0, scope="account")])))
    first = [_acct(mod, 0, "a@x"), _acct(mod, 1, "b@x"), _acct(mod, 2, "cc@x", source="manual:hermes_pkce")]
    monkeypatch.setattr(mod, "_read_accounts", lambda p: (p, first))  # first read: cc still pool-owned
    second = [  # second snapshot: a is fine, b died, cc became a borrowed claude_code row with a token
        SimpleNamespace(id="id0", label="a@x", auth_type="oauth", source="manual:x", last_status=None, access_token="tok-a"),
        SimpleNamespace(id="id1", label="b@x", auth_type="oauth", source="manual:x", last_status="dead", access_token="tok-b"),
        SimpleNamespace(id="id2", label="cc@x", auth_type="oauth", source="claude_code", last_status=None, access_token="tok-cc"),
    ]
    monkeypatch.setattr(mod, "_load_pool", lambda p: _snap(second))
    out = mod._usage_for_accounts(first)
    assert [c[0] for c in calls] == ["tok-a"] and [c[1] for c in calls] == ["id0"]
    assert set(out) == {"id0"}  # no dead token, no borrowed token probed or exposed


def test_legacy_borrowed_row_with_token_is_never_probed(mod, monkeypatch):
    calls = []
    _fake_module(monkeypatch, "agent.account_usage",
                 fetch_account_usage=lambda provider, api_key=None, read_only=False, identity_id=None: (
                     calls.append(api_key) or SimpleNamespace(windows=[
                         SimpleNamespace(label="Current week", used_percent=5.0, scope="account")])))
    rows = [{"id": "idA", "label": "a@x", "priority": 0, "access_token": "tok-a", "source": "manual:hermes_pkce"},
            {"id": "idC", "label": "cc@x", "priority": 1, "access_token": "tok-cc", "source": "claude_code"}]
    _snapshot_env(monkeypatch, mod, rows)
    assert json.loads(json.dumps(mod.list_accounts()))["usage_available"] is True
    assert calls == ["tok-a"]  # only the pool-owned token ever reaches the helper


def test_auto_move_declines_when_the_winner_became_an_api_key(mod, monkeypatch):
    """Ranked as OAuth on the first read; the pool used for the move now shows an api_key row."""
    accts = [_acct(mod, 0, "busy@x"), _acct(mod, 1, "fresh@x")]
    monkeypatch.setattr(mod, "_read_accounts", lambda p: (p, accts))
    monkeypatch.setattr(mod, "_usage_for_accounts", lambda accs, p: _by_id(accts, {
        "busy@x": _usage(week=90), "fresh@x": _usage(week=0)}))
    monkeypatch.setattr(mod, "_pool_strategy", lambda p: "fill_first")
    moved = []
    changed = [_pool_entry(0, "busy@x"),
               _pool_entry(1, "fresh@x", auth_type="api_key")]
    monkeypatch.setattr(mod, "_load_writable_pool", lambda p: _writable(changed, moved))
    out = mod._move_to_front("anthropic", _acct(mod, 1, "fresh@x"), exact_id=True, require_eligible=True)
    assert isinstance(out, mod._MoveDeclined) and "OAuth" in out and moved == []


def test_switch_require_eligible_declines_a_non_oauth_winner_before_moving(mod, monkeypatch):
    rows = [_acct(mod, 0, "busy@x"), _acct(mod, 1, "key@x", auth_type="api_key")]
    monkeypatch.setattr(mod, "_read_accounts", lambda p: (p, rows))
    monkeypatch.setattr(mod, "_pool_strategy", lambda p: "fill_first")
    monkeypatch.setattr(mod, "_move_to_front", lambda *a, **k: pytest.fail("a non-oauth winner must not move"))
    out = mod.switch("id1", require_eligible=True, exact_id=True)
    assert out["ok"] is False and out["declined"] is True and "OAuth" in out["reason"]
    # manual switch is unchanged
    monkeypatch.setattr(mod, "_move_to_front", lambda *a, **k: None)
    monkeypatch.setattr(mod, "list_accounts", lambda *a, **k: {"accounts": [
        {"label": "key@x", "id": "id1", "priority": 0}, {"label": "busy@x", "id": "id0", "priority": 1}]})
    assert mod.switch("key@x")["ok"] is True


# -- outcome reporting the desktop relies on -------------------------------------------


def test_cli_outcomes_for_switch_paths(mod, monkeypatch, capsys):
    accts = [mod.Account(index=1, label="a@x", auth_type="oauth", id="idA", priority=0, source="manual:hermes_pkce"),
             mod.Account(index=2, label="b@x", auth_type="oauth", id="idB", priority=1, source="manual:hermes_pkce")]
    monkeypatch.setattr(mod, "_read_accounts", lambda p: (p, accts))
    monkeypatch.setattr(mod, "_pool_strategy", lambda p: "fill_first")
    moved = []
    monkeypatch.setattr(mod, "_move_to_front", lambda p, a, **k: moved.append(a.id))

    def order(first_id):
        return lambda *a, **k: {"accounts": [
            {"label": "b@x", "id": "idB", "priority": 0}, {"label": "a@x", "id": first_id, "priority": 1}]}

    # a move that verifies by exact id: ok, exit 0
    monkeypatch.setattr(mod, "list_accounts", order("idA"))
    mod._handle_cli(SimpleNamespace(cmd="switch", target="idB", provider="anthropic", dry_run=False, exact_id=True))
    assert capsys.readouterr().out.strip().splitlines()[-1] == "CPS_RESULT ok"
    # a move whose read-back does not confirm the id: unverified, nonzero exit, never "Switched"
    monkeypatch.setattr(mod, "list_accounts", lambda *a, **k: {"accounts": [
        {"label": "a@x", "id": "idA", "priority": 0}, {"label": "b@x", "id": "idB", "priority": 1}]})
    with pytest.raises(SystemExit) as ei:
        mod._handle_cli(SimpleNamespace(cmd="switch", target="idB", provider="anthropic", dry_run=False, exact_id=True))
    out = capsys.readouterr().out
    assert ei.value.code == 1 and out.strip().splitlines()[-1] == "CPS_RESULT unverified"
    assert "Switch unverified" in out and "Switched" not in out
    # a failed lookup: failed, nonzero exit
    with pytest.raises(SystemExit) as ei:
        mod._handle_cli(SimpleNamespace(cmd="switch", target="nope", provider="anthropic", dry_run=False, exact_id=True))
    out = capsys.readouterr().out
    assert ei.value.code == 1 and out.strip().splitlines()[-1] == "CPS_RESULT failed" and "Switch failed" in out
    # a plan / an already-first no-op: ok, exit 0
    mod._handle_cli(SimpleNamespace(cmd="plan", target="idB", provider="anthropic", exact_id=True))
    assert capsys.readouterr().out.strip().splitlines()[-1] == "CPS_RESULT ok"
    moved.clear()
    monkeypatch.setattr(mod, "list_accounts", order("idA"))
    mod._handle_cli(SimpleNamespace(cmd="switch", target="idA", provider="anthropic", dry_run=False, exact_id=True))
    out = capsys.readouterr().out
    assert out.strip().splitlines()[-1] == "CPS_RESULT ok" and "No change" in out and moved == []


def test_cli_auto_outcomes(mod, monkeypatch, capsys):
    # declined: still ok=True overall (order unchanged by design) → exit 0
    _report = {"ok": True, "provider": "anthropic", "alive": [], "exhausted": [], "benched": [],
               "cooldown_unknown": [], "dead": [], "unknown": [], "promoted": None, "dry_run": False,
               "declined": {"account": "b@x", "reason": "dead"}}
    monkeypatch.setattr(mod, "auto_rotate", lambda p, dry_run=False: _report)
    mod._handle_cli(SimpleNamespace(cmd="auto", provider="anthropic", dry_run=False))
    assert capsys.readouterr().out.strip().splitlines()[-1] == "CPS_RESULT ok"
    # report ok=False without a change: failed, nonzero exit
    _report2 = {"ok": False, "provider": "anthropic", "alive": [], "exhausted": [], "benched": [],
                "cooldown_unknown": [], "dead": [], "unknown": [], "dry_run": False,
                "error": "promotion failed: boom"}
    monkeypatch.setattr(mod, "auto_rotate", lambda p, dry_run=False: _report2)
    with pytest.raises(SystemExit) as ei:
        mod._handle_cli(SimpleNamespace(cmd="auto", provider="anthropic", dry_run=False))
    out = capsys.readouterr().out
    assert ei.value.code == 1 and out.strip().splitlines()[-1] == "CPS_RESULT failed"
    # moved but unverifiable: unverified, nonzero exit
    _report3 = {"ok": False, "provider": "anthropic", "alive": [], "exhausted": [], "benched": [],
                "cooldown_unknown": [], "dead": [], "unknown": [], "dry_run": False, "changed": True,
                "error": "promotion could not be verified by entry id"}
    monkeypatch.setattr(mod, "auto_rotate", lambda p, dry_run=False: _report3)
    with pytest.raises(SystemExit) as ei:
        mod._handle_cli(SimpleNamespace(cmd="auto", provider="anthropic", dry_run=False))
    out = capsys.readouterr().out
    assert ei.value.code == 1 and out.strip().splitlines()[-1] == "CPS_RESULT unverified"


def test_render_switch_never_claims_success_without_verification(mod):
    assert "Switched" not in mod.render_switch({"ok": True, "dry_run": False, "account": "b@x", "was_priority": 1,
                                                "verified_first": False, "order_after": ["a@x", "b@x"]})
    assert "Switch unverified" in mod.render_switch({"ok": True, "dry_run": False, "account": "b@x", "was_priority": 1,
                                                     "verified_first": False, "order_after": ["a@x", "b@x"]})
    assert "Switched" in mod.render_switch({"ok": True, "dry_run": False, "account": "b@x", "was_priority": 1,
                                            "verified_first": True})
    assert "No change" in mod.render_switch({"ok": True, "dry_run": False, "account": "a@x", "was_priority": 0,
                                             "already_first": True})


def test_node_outcome_contract_and_idless_rows(tmp_path):
    """The desktop decides success from CPS_RESULT + exit code only; id-less rows are inert."""
    src = PLUGIN_JS.read_text()
    src = src.replace("import * as sdk from '@hermes/plugin-sdk'", "const sdk = new Proxy({}, {get: () => () => null})")
    src = src.replace("import { useState } from 'react'", "const useState = () => [false, () => {}]")
    src = src.replace("import { jsx, jsxs } from 'react/jsx-runtime'",
                      "const jsx = (type, props, key) => ({ type, props, key }); const jsxs = jsx")
    src = src.replace("export default {", "globalThis.__plugin = {")
    probe = """
const en = MESSAGES.en
const t = (k, ...a) => (typeof en[k] === 'function' ? en[k](...a) : en[k])
const out = {
  ok: (() => { const r = cliOutcome(0, 'Switched: b@x is now priority 0.\\nCPS_RESULT ok'); return {ok: r.ok, kind: r.kind, message: r.message ?? null} })(),
  unverified: cliOutcome(0, 'Switch unverified: b@x …\\nCPS_RESULT unverified'),
  failed: cliOutcome(0, 'Switch failed: boom\\nCPS_RESULT failed'),
  zeroExitNoLine: cliOutcome(0, 'Switched: b@x is now priority 0.'),
  nonzeroExit: cliOutcome(1, 'Switch failed: pool not read\\nCPS_RESULT failed'),
  statusOnly: cliOutcome(0, 'CPS_STATUS {…}'),
  rowWithId: rowSwitchArgv({ id: 'idB', label: 'b@x' }),
  unverifiedText: switchFailText({ label: 'b@x' }, 'Switch unverified: b@x was asked to move', t),
  failedText: switchFailText({ label: 'b@x' }, 'boom', t),
  rowWithoutId: rowSwitchArgv({ label: 'b@x' }),
  rowEmptyId: rowSwitchArgv({ id: '', label: 'b@x' }),
  step3en: MESSAGES.en.step3,
  step3de: MESSAGES.de.step3,
  usageErrorEn: MESSAGES.en.usageError,
  usageErrorDe: MESSAGES.de.usageError,
  usageUnavailableEn: MESSAGES.en.usageUnavailable
}
console.log(JSON.stringify(out))
"""
    f = tmp_path / "probe.mjs"
    f.write_text(src + "\n" + probe)
    res = _subprocess.run(["node", str(f)], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    out = _json.loads(res.stdout.strip().splitlines()[-1])
    assert out["ok"]["ok"] is True and out["ok"]["kind"] == "ok" and out["ok"]["message"] is None
    assert out["unverified"]["ok"] is False and out["unverified"]["kind"] == "unverified"
    assert out["failed"]["ok"] is False and out["failed"]["kind"] == "failed" and "boom" in out["failed"]["message"]
    # the old contract (zero exit + no 'failed' word) must no longer pass as success
    assert out["zeroExitNoLine"]["ok"] is False
    assert out["nonzeroExit"]["ok"] is False
    assert out["statusOnly"]["ok"] is False
    assert out["rowWithId"] == ["switch", "--exact-id", "idB"]
    assert "could not confirm" in out["unverifiedText"] and out["unverifiedText"].startswith("b@x")
    assert out["failedText"] == "Switch failed: boom"
    assert out["rowWithoutId"] is None and out["rowEmptyId"] is None
    assert "off by default" in out["step3en"] and "standardm" in out["step3de"]
    assert "by itself" not in out["step3en"] and "selbst" not in out["step3de"]
    # a failed --usage read has its own wording, distinct from ordinary no-usage
    assert "could not be read" in out["usageErrorEn"] and "nicht lesbar" in out["usageErrorDe"]
    assert out["usageErrorEn"] != out["usageUnavailableEn"]
    assert "usage.isError" in PLUGIN_JS.read_text() and "t('usageError')" in PLUGIN_JS.read_text()


# -- Astra round 5: source revalidation, id-only verification, unverified
#    non-success results, explicit pool-read errors on the usage snapshot,
#    fixed diagnostics, and the operator-approved read accessor --------------------


def test_move_declines_when_the_winner_became_a_borrowed_source(mod, monkeypatch):
    """Source-only change: still OAuth, alive, not benched — but the row turned claude_code."""
    moved = []
    entries = [_pool_entry(0, "busy@x"), _pool_entry(1, "fresh@x", source="claude_code")]
    monkeypatch.setattr(mod, "_load_writable_pool", lambda p: _writable(entries, moved))
    out = mod._move_to_front("anthropic", _acct(mod, 1, "fresh@x"), exact_id=True, require_eligible=True)
    assert isinstance(out, mod._MoveDeclined) and "own auth flows" in out and "claude_code" in out
    assert moved == []


def test_auto_declines_a_winner_that_became_borrowed_between_reads(mod, monkeypatch):
    """Ranked as pool-owned on the first read; the pool used for the move now shows claude_code."""
    accts = [_acct(mod, 0, "busy@x"), _acct(mod, 1, "fresh@x")]
    monkeypatch.setattr(mod, "_read_accounts", lambda p: (p, accts))
    monkeypatch.setattr(mod, "_usage_for_accounts", lambda accs, p: _by_id(accts, {
        "busy@x": _usage(week=90), "fresh@x": _usage(week=0)}))
    monkeypatch.setattr(mod, "_pool_strategy", lambda p: "fill_first")
    moved = []
    changed = [_pool_entry(0, "busy@x"), _pool_entry(1, "fresh@x", source="claude_code")]
    monkeypatch.setattr(mod, "_load_writable_pool", lambda p: _writable(changed, moved))
    monkeypatch.setattr(mod, "list_accounts", lambda *a, **k: pytest.fail("no verification read after a decline"))
    report = mod.auto_rotate()
    assert moved == []
    assert report["declined"]["account"] == "fresh@x" and "claude_code" in report["declined"]["reason"]
    assert report["promoted"] is None and not report.get("changed") and report["ok"] is True
    assert "order unchanged" in mod.render_auto(report)


def test_switch_require_eligible_declines_a_borrowed_winner_before_moving(mod, monkeypatch):
    rows = [_acct(mod, 0, "busy@x"), _acct(mod, 1, "cc@x", source="claude_code")]
    monkeypatch.setattr(mod, "_read_accounts", lambda p: (p, rows))
    monkeypatch.setattr(mod, "_pool_strategy", lambda p: "fill_first")
    monkeypatch.setattr(mod, "_move_to_front", lambda *a, **k: pytest.fail("a borrowed winner must not move"))
    out = mod.switch("id1", require_eligible=True, exact_id=True)
    assert out["ok"] is False and out["declined"] is True and "claude_code" in out["reason"]


def test_switch_read_back_failure_is_unverified_not_success(mod, monkeypatch, capsys):
    """A missing read-back after a real move: attempted, unverified, never ok / never
    claimed as done — and the wording does not imply the move was rolled back."""
    accts = [_acct(mod, 0, "busy@x"), _acct(mod, 1, "fresh@x")]
    monkeypatch.setattr(mod, "_read_accounts", lambda p: (p, accts))
    monkeypatch.setattr(mod, "_pool_strategy", lambda p: "fill_first")
    moved = []
    monkeypatch.setattr(mod, "_move_to_front", lambda p, a, **k: moved.append(a.id))

    def boom(*a, **k):
        raise mod.PoolReadError("could not read the anthropic credential pool (OSError)")

    monkeypatch.setattr(mod, "list_accounts", boom)
    plan = mod.switch("fresh@x")
    assert moved == ["id1"]                      # the move was attempted
    assert plan["ok"] is False and plan["unverified"] is True and plan["verified_first"] is False
    assert "could not be read back" in plan["error"] and "may or may not have changed" in plan["error"]
    assert mod._switch_outcome(plan) == "unverified"
    text = mod.render_switch(plan)
    assert "Switch unverified" in text and "Switched" not in text
    # the tool result carries the non-success too
    out = json.loads(mod._tool_account_switch({"action": "switch", "target": "fresh@x"}))
    assert out["ok"] is False and out["unverified"] is True
    # and the CLI exits nonzero on CPS_RESULT unverified
    with pytest.raises(SystemExit) as ei:
        mod._handle_cli(SimpleNamespace(cmd="switch", target="fresh@x", provider="anthropic",
                                        dry_run=False, exact_id=False))
    lines = capsys.readouterr().out.strip().splitlines()
    assert ei.value.code == 1 and lines[-1] == "CPS_RESULT unverified"


def test_auto_worker_claims_first_only_when_verified(mod, monkeypatch, caplog):
    """changed without a promoted name (unverified) must not log 'now tried first'."""
    monkeypatch.setattr(mod, "auto_rotate", lambda p="anthropic", dry_run=False: {
        "ok": False, "changed": True, "promoted": None, "unverified": True,
        "error": "promotion could not be verified by entry id"})
    with caplog.at_level("INFO", logger="credential_pool_switcher"):
        mod._auto_worker("anthropic")
    assert "now tried first" not in caplog.text
    info = mod.auto_info()
    assert info["last_promoted"] is None and "could not be verified" in info["last_error"]
    # a verified promotion still claims it
    monkeypatch.setattr(mod, "auto_rotate", lambda p="anthropic", dry_run=False: {
        "ok": True, "changed": True, "promoted": "a@x"})
    caplog.clear()
    with caplog.at_level("INFO", logger="credential_pool_switcher"):
        mod._auto_worker("anthropic")
    assert "a@x is now tried first" in caplog.text


def test_usage_snapshot_pool_read_error_propagates_and_stays_distinct(mod, monkeypatch, capsys):
    """A PoolReadError on the second (token) snapshot surfaces as an explicit error
    through auto/tool/CLI — never as silent no-usage success. Ordinary no-usage
    (helper missing) stays None and keeps auto's ok/no_usage reporting."""
    accounts = [_acct(mod, 0, "ok@x")]
    monkeypatch.setattr(mod, "_read_accounts", lambda p: (p, accounts))
    monkeypatch.setattr(mod, "_pool_strategy", lambda p: "fill_first")
    _fake_module(monkeypatch, "agent.account_usage",
                 fetch_account_usage=lambda *a, **k: pytest.fail("no token may be probed when the snapshot is unreadable"))

    def unreadable(p):
        raise mod.PoolReadError("could not read the anthropic credential pool (OSError)")

    monkeypatch.setattr(mod, "_load_pool", unreadable)
    with pytest.raises(mod.PoolReadError):
        mod._usage_for_accounts(accounts)     # the token snapshot read failed
    with pytest.raises(mod.PoolReadError):
        mod.list_accounts("anthropic")        # first read ok, second fails → command fails closed
    with pytest.raises(mod.PoolReadError):
        mod.pool_status(with_usage=True)
    assert mod.pool_status()["usage_available"] is False  # without --usage nothing is fetched
    # auto fails closed before any move
    monkeypatch.setattr(mod, "switch", lambda *a, **k: pytest.fail("no usage: nothing may move"))
    with pytest.raises(mod.PoolReadError):
        mod.auto_rotate()
    out = json.loads(mod._tool_account_switch({"action": "auto"}))
    assert out["ok"] is False and "nothing was changed" in out["error"]
    for cmd, ns in (("auto", SimpleNamespace(cmd="auto", provider="anthropic", dry_run=False)),
                    ("list", SimpleNamespace(cmd="list", provider="anthropic")),
                    ("status", SimpleNamespace(cmd="status", provider="anthropic", json=True, usage=True))):
        with pytest.raises(SystemExit) as ei:
            mod._handle_cli(ns)
        assert ei.value.code == 1, cmd
    printed = capsys.readouterr().out
    assert "Auto rotation failed" in printed and "Command failed" in printed
    assert "nothing was changed" in printed
    # ordinary no-usage (helper not importable) is still just "no usage": None, no raise
    _fake_module(monkeypatch, "agent.account_usage")  # module without fetch_account_usage
    monkeypatch.setattr(mod, "_load_pool", lambda p: _snap([_pool_entry(0, "ok@x", token="t")]))
    assert mod._usage_for_accounts(accounts) is None
    report = mod.auto_rotate()
    assert report["ok"] is True and report["unknown_reason"] == {"ok@x": "no_usage"}


def test_cli_fallback_failure_returns_a_fixed_message_not_subprocess_output(mod, monkeypatch):
    monkeypatch.setattr(mod, "_load_writable_pool", lambda p: None)
    secret = "***"
    monkeypatch.setattr(mod, "_run", lambda args: f"Error: invalid key {secret}")
    out = mod._move_to_front("anthropic", _acct(mod, 1, "b@x"))
    assert secret not in out
    assert "failed" in out and "may or may not have changed" in out


def test_move_failure_diagnostics_carry_the_exception_class_only(mod, monkeypatch, caplog):
    def boom(_id, _pos):
        raise RuntimeError("boom ***")

    entries = [_pool_entry(0, "busy@x"), _pool_entry(1, "fresh@x")]
    monkeypatch.setattr(mod, "_load_writable_pool",
                        lambda p: SimpleNamespace(move_entry=boom, entries=lambda: entries))
    # strict: the returned message names the exception class, never the exception text
    out = mod._move_to_front("anthropic", _acct(mod, 1, "fresh@x"), exact_id=True)
    assert "RuntimeError" in out and "***" not in out
    # non-strict: the retry warning logs the class only, then the CLI fallback runs
    caplog.clear()
    monkeypatch.setattr(mod, "_run", lambda args: "Set anthropic credential x to priority 0")
    with caplog.at_level("DEBUG", logger="credential_pool_switcher"):
        assert mod._move_to_front("anthropic", _acct(mod, 1, "fresh@x")) is None
    assert "RuntimeError" in caplog.text and "***" not in caplog.text


def test_no_raw_exception_or_subprocess_output_in_diagnostics(mod):
    """Every log/return of a failure carries a fixed message or the exception class."""
    src = PLUGIN_FILE.read_text()
    assert "out[:200]" not in src               # the old truncated CLI-output echo
    assert "hermes auth priority failed: {" not in src
    # no log call passes the exception object itself (class name only)
    import re as _re
    assert _re.search(r"log\.(debug|info|warning|error|exception)\(\s*[^)]*,\s*exc\s*[,)]", src) is None


def test_reads_use_only_the_operator_approved_core_read_accessor(mod):
    """Operator decision 2026-11-28: snapshot reads may go through core's
    read_credential_pool accessor — via the documented public agent.credential_pool
    module (on the published plugin API), so catalog rule 9 stays clean — and
    through nothing else that touches the auth store. The accessor is called from
    exactly one place; direct auth-store helpers stay forbidden in the source."""
    tree = _ast.parse(PLUGIN_FILE.read_text())
    name = lambda f: f.id if isinstance(f, _ast.Name) else (f.attr if isinstance(f, _ast.Attribute) else "")
    assert _functions_calling(tree, lambda f: name(f) == "read_credential_pool") == {"_load_pool"}
    # Public route only: the accessor must be imported from agent.credential_pool,
    # never from the private hermes_cli.auth module (catalog rule 9).
    imports = [n for n in _ast.walk(tree) if isinstance(n, _ast.ImportFrom)]
    rcp = [n for n in imports if any(a.name == "read_credential_pool" for a in n.names)]
    assert rcp, "read_credential_pool must be imported"
    assert all(n.module == "agent.credential_pool" for n in rcp)
    assert all(n.module != "hermes_cli.auth" for n in imports), "private core import (catalog rule 9): hermes_cli.auth"
    src = PLUGIN_FILE.read_text()
    for forbidden in ("write_credential_pool", "_load_auth_store", "_auth_store_lock", "_get_json"):
        assert forbidden not in src, forbidden
