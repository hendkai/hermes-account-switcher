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


# -- usage via the public helper -----------------------------------------------------

def test_usage_uses_public_helper_and_skips_dead(mod, monkeypatch):
    W = lambda label, pct, scope="account": SimpleNamespace(label=label, used_percent=pct, scope=scope)
    calls = []

    def fake_fetch(provider, *, api_key=None, read_only=False, **kw):
        calls.append((provider, api_key, read_only))
        return SimpleNamespace(windows=[W("Current session", 12.0), W("Current week", 0.0),
                                        W("Fable weekly", 100.0, "model")])

    _fake_module(monkeypatch, "agent.account_usage", fetch_account_usage=fake_fetch)
    entries = [SimpleNamespace(label=l, auth_type="oauth", access_token=f"tok-{l}") for l in ("ok@x", "dead@x", "bench@x")]
    monkeypatch.setattr(mod, "_load_pool", lambda p: SimpleNamespace(entries=lambda: entries))
    accounts = [_acct(mod, 0, "ok@x"), _acct(mod, 1, "dead@x", status="dead"),
                _acct(mod, 2, "bench@x", status="exhausted", bench_until=time.time() + 600)]
    out = mod._usage_for_accounts(accounts)
    # read_only; dead logins are skipped, benched ones are read so their bars show why
    assert sorted(c[1] for c in calls) == ["tok-bench@x", "tok-ok@x"] and all(c[2] for c in calls)
    assert out["ok@x"] == {"five_hour": {"used_percent": 12.0}, "week": {"used_percent": 0.0},
                           "scoped": {"used_percent": 100.0, "active": True}}
    assert "dead@x" not in out


def test_catalog_build_has_no_auth_store_or_refresh_paths(mod):
    src = PLUGIN_FILE.read_text()
    for forbidden in ("write_credential_pool", "read_credential_pool", "_auth_store_lock",
                      "_refresh_entry", "_get_json", "oauth/usage", "_write_auto_status"):
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

def _usage(five=0.0, week=0.0, scoped=None):
    info = {"five_hour": {"used_percent": five}, "week": {"used_percent": week}}
    if scoped is not None:
        info["scoped"] = {"used_percent": scoped[0], "active": scoped[1]}
    return info


def test_auto_promotes_login_with_most_weekly_headroom(mod, monkeypatch):
    accounts = [_acct(mod, 0, "busy@x"), _acct(mod, 1, "fresh@x"), _acct(mod, 2, "dead@x", status="dead"),
                _acct(mod, 3, "bench@x", status="exhausted", bench_until=time.time() + 600)]
    monkeypatch.setattr(mod, "_read_accounts", lambda p: (p, accounts))
    monkeypatch.setattr(mod, "_usage_for_accounts", lambda accs, p: {
        "busy@x": _usage(five=10, week=60), "fresh@x": _usage(five=40, week=0)})
    promoted = []
    monkeypatch.setattr(mod, "switch", lambda target, provider="anthropic", dry_run=False:
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
    monkeypatch.setattr(mod, "_usage_for_accounts", lambda accs, p: {
        "full@x": _usage(five=100, week=20), "spare@x": _usage(five=5, week=30)})
    monkeypatch.setattr(mod, "switch", lambda *a, **k: pytest.fail("dry run must not switch"))
    report = mod.auto_rotate(dry_run=True)
    assert report["exhausted"] == ["full@x"] and report["unknown"] == ["unknown@x"]
    assert report["would_promote"] == "spare@x" and report["promoted"] is None


def test_auto_treats_active_scoped_limit_as_exhausted(mod, monkeypatch):
    accounts = [_acct(mod, 0, "scoped@x"), _acct(mod, 1, "ok@x")]
    monkeypatch.setattr(mod, "_read_accounts", lambda p: (p, accounts))
    monkeypatch.setattr(mod, "_usage_for_accounts", lambda accs, p: {
        "scoped@x": _usage(week=10, scoped=(100.0, True)), "ok@x": _usage(week=50)})
    monkeypatch.setattr(mod, "switch", lambda target, provider="anthropic", dry_run=False: {"ok": True})
    monkeypatch.setattr(mod, "list_accounts", lambda p="anthropic", with_usage=True: {"accounts": []})
    report = mod.auto_rotate()
    assert report["exhausted"] == ["scoped@x"] and report["promoted"] == "ok@x"


def test_auto_already_first_is_not_moved(mod, monkeypatch):
    accounts = [_acct(mod, 0, "best@x"), _acct(mod, 1, "other@x")]
    monkeypatch.setattr(mod, "_read_accounts", lambda p: (p, accounts))
    monkeypatch.setattr(mod, "_usage_for_accounts", lambda accs, p: {
        "best@x": _usage(week=5), "other@x": _usage(week=50)})
    monkeypatch.setattr(mod, "switch", lambda *a, **k: pytest.fail("already first: nothing to move"))
    report = mod.auto_rotate()
    assert report["promoted"] == "best@x"


def test_auto_without_oauth_logins_reports_no_headroom(mod, monkeypatch):
    accounts = [_acct(mod, 0, "KEY", auth_type="api_key")]
    monkeypatch.setattr(mod, "_read_accounts", lambda p: (p, accounts))
    monkeypatch.setattr(mod, "_usage_for_accounts", lambda accs, p: None)
    report = mod.auto_rotate()
    assert report["ok"] is True and report["alive"] == [] and report["promoted"] is None
    assert "No login has headroom" in mod.render_auto(report)


def test_pool_status_usage_only_when_asked(mod, monkeypatch):
    monkeypatch.setattr(mod, "_pool_strategy", lambda p: "fill_first")
    monkeypatch.setattr(mod, "_read_accounts", lambda p: (p, [_acct(mod, 0, "a@x")]))
    calls = []
    monkeypatch.setattr(mod, "_usage_for_accounts", lambda accs, p: calls.append(p) or {"a@x": _usage(week=7)})
    plain = mod.pool_status()
    assert calls == [] and plain["accounts"][0]["usage"] is None and plain["usage_available"] is False
    rich = mod.pool_status(with_usage=True)
    assert calls == ["anthropic"]
    assert rich["accounts"][0]["usage"]["week"]["used_percent"] == 7 and rich["usage_available"] is True



# -- automatic mode (hook-driven, throttled) ----------------------------------------

def test_auto_keeps_current_login_within_margin(mod, monkeypatch):
    accounts = [_acct(mod, 0, "cur@x"), _acct(mod, 1, "alt@x")]
    monkeypatch.setattr(mod, "_read_accounts", lambda p: (p, accounts))
    monkeypatch.setattr(mod, "_usage_for_accounts", lambda accs, p: {
        "cur@x": _usage(week=30), "alt@x": _usage(week=27)})  # 3 points better: not worth a swap
    monkeypatch.setattr(mod, "switch", lambda *a, **k: pytest.fail("within margin: no swap"))
    report = mod.auto_rotate()
    assert report["promoted"] == "cur@x" and not report.get("changed")


def test_auto_swaps_when_clearly_better_and_reports_changed(mod, monkeypatch):
    accounts = [_acct(mod, 0, "cur@x"), _acct(mod, 1, "alt@x")]
    monkeypatch.setattr(mod, "_read_accounts", lambda p: (p, accounts))
    monkeypatch.setattr(mod, "_usage_for_accounts", lambda accs, p: {
        "cur@x": _usage(week=40), "alt@x": _usage(week=10)})
    monkeypatch.setattr(mod, "switch", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(mod, "list_accounts", lambda p="anthropic", with_usage=True: {"accounts": []})
    report = mod.auto_rotate()
    assert report["promoted"] == "alt@x" and report["changed"] is True


def test_maybe_auto_rotate_is_throttled_across_calls(mod, monkeypatch):
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


def test_maybe_auto_rotate_respects_opt_out(mod, monkeypatch):
    monkeypatch.setenv("HERMES_CPS_AUTO_ROTATE", "0")
    monkeypatch.setattr(mod, "auto_rotate", lambda *a, **k: pytest.fail("opted out"))
    assert mod.maybe_auto_rotate(background=False) is False
    assert mod.auto_info()["enabled"] is False


def test_auto_worker_failure_is_recorded_not_raised(mod, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("usage down")
    monkeypatch.setattr(mod, "auto_rotate", boom)
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
    entries = [SimpleNamespace(label=a.label, access_token="tok-" + a.label, auth_type="oauth") for a in accounts]
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
    assert set(out) == {"ok@x", "bench@x"} and sorted(seen) == ["tok-bench@x", "tok-ok@x"]
    assert out["ok@x"]["five_hour"]["used_percent"] == 12.0
    assert out["ok@x"]["week"]["used_percent"] == 34.0
    assert out["ok@x"]["scoped"]["used_percent"] == 80.0  # tightest model cap wins
