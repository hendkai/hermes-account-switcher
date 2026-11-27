"""Dispatch tests through Hermes' real tools.registry — the plugin contract regression suite.

Hermes calls plugin tool handlers as ``handler(args, **kwargs)`` and expects
``schema={"name": ..., "description": ..., "parameters": {...}}`` plus a str
(JSON) result. These tests register the plugin against the *real* registry and
go through ``registry.dispatch()`` so a calling-convention drift fails here
instead of producing "Unknown action" for every model call.

Run inside a hermes-agent checkout: ``PYTHONPATH=/path/to/hermes-agent pytest tests/``.
Skips (not fails) when hermes-agent is not importable, e.g. in a bare plugin clone.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

PLUGIN_FILE = Path(__file__).resolve().parents[1] / "__init__.py"

try:
    from tools.registry import registry  # noqa: E402

    _HAVE_HERMES = True
except Exception:  # pragma: no cover - bare clone without hermes-agent on sys.path
    registry = None
    _HAVE_HERMES = False

pytestmark = pytest.mark.skipif(not _HAVE_HERMES, reason="hermes-agent not importable")


def _load_plugin():
    spec = importlib.util.spec_from_file_location(
        "credential_pool_switcher_under_test", PLUGIN_FILE
    )
    mod = importlib.util.module_from_spec(spec)
    # dataclasses resolves annotations via sys.modules[cls.__module__]:
    # register before exec or @dataclass fails under non-package loading.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class _Ctx:
    """Minimal register facade: tools land in the REAL global registry."""

    def __init__(self):
        self.hooks: dict = {}
        self.cli: dict = {}
        self.tool_calls: list = []

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def register_tool(self, name, toolset, schema, handler, description="", **_):
        registry.register(
            name=name, toolset=toolset, schema=schema,
            handler=handler, description=description,
        )
        self.tool_calls.append(name)

    def register_cli_command(self, **kw):
        self.cli = kw

    def register_command(self, name, **kw):
        self.slash = {"name": name, **kw}


@pytest.fixture()
def plugin(monkeypatch):
    mod = _load_plugin()
    # Hermetic: never shell out to the real `hermes` CLI from a unit test.
    monkeypatch.setattr(
        mod, "list_accounts",
        lambda provider="anthropic": {
            "ok": True, "provider": provider,
            "accounts": [{"index": 0, "label": "acct-a", "priority": 0, "current": True}],
        },
    )
    monkeypatch.setattr(
        mod, "switch",
        lambda target, provider="anthropic", dry_run=False: {
            "ok": True, "moved": target, "provider": provider, "dry_run": dry_run,
        },
    )
    monkeypatch.setattr(
        mod, "auto_rotate",
        lambda provider="anthropic", dry_run=False: {"ok": True, "auto": True, "dry_run": dry_run},
    )
    ctx = _Ctx()
    mod.register(ctx)
    return mod, ctx


def test_dispatch_list(plugin):
    _, ctx = plugin
    assert "account_switch" in ctx.tool_calls
    out = registry.dispatch("account_switch", {"action": "list"})
    assert isinstance(out, str)
    data = json.loads(out)
    assert data["ok"] is True
    assert data["provider"] == "anthropic"
    assert data["accounts"][0]["label"] == "acct-a"


def test_dispatch_unknown_action(plugin):
    out = registry.dispatch("account_switch", {"action": "bogus"})
    assert isinstance(out, str)
    assert "Unknown action 'bogus'" in out


def test_dispatch_plan_routes_dry_run(plugin):
    out = registry.dispatch("account_switch", {"action": "plan", "target": "acct-a"})
    assert isinstance(out, str)
    data = json.loads(out)
    assert data["ok"] is True
    assert data["dry_run"] is True
    assert data["moved"] == "acct-a"


def test_dispatch_switch_without_target_lists(plugin):
    out = registry.dispatch("account_switch", {"action": "switch"})
    assert isinstance(out, str)
    data = json.loads(out)
    assert data["ok"] is True
    assert "accounts" in data  # fell back to list, not an error


def test_registered_schema_exposes_parameters(plugin):
    schema = registry.get_schema("account_switch")
    assert isinstance(schema, dict)
    params = schema.get("parameters")
    assert isinstance(params, dict)
    props = params.get("properties")
    assert isinstance(props, dict)
    assert set(props) >= {"action", "target", "provider", "dry_run"}
    assert props["action"]["enum"] == ["list", "switch", "plan", "auto"]


def test_gate_escalates_switch_and_auto_but_not_plan_or_list(plugin):
    mod, ctx = plugin
    gate = ctx.hooks["pre_tool_call"]
    real = gate(tool_name="account_switch", args={"action": "switch", "target": "acct-a"})
    assert real and real["action"] == "approve"
    assert gate(tool_name="account_switch", args={"action": "switch", "target": "acct-a", "dry_run": True}) is None
    assert gate(tool_name="account_switch", args={"action": "plan", "target": "acct-a"}) is None
    assert gate(tool_name="account_switch", args={"action": "list"}) is None
    auto = gate(tool_name="account_switch", args={"action": "auto"})
    assert auto and auto["action"] == "approve" and "auto-rotate" in auto["message"]
    assert gate(tool_name="account_switch", args={"action": "auto", "dry_run": True}) is None
    out = json.loads(registry.dispatch("account_switch", {"action": "auto", "dry_run": True}))
    assert out["auto"] is True and out["dry_run"] is True


# -- approval gate and handler read every call the same way -----------------------------

_GATE_CASES = [
    # (args, mutates)
    ({"target": "acct-b"}, True),                                   # omitted action + target = switch
    ({"action": None, "target": "acct-b"}, True),
    ({"action": "", "target": "acct-b"}, True),
    ({"action": " Switch ", "target": "acct-b"}, True),             # case / whitespace
    ({"action": "switch", "target": "acct-b", "dry_run": "false"}, True),  # not a real dry run
    ({"action": "switch", "target": "acct-b", "dry_run": 0}, True),
    ({"action": "auto"}, True),
    ({"action": "AUTO", "dry_run": "no"}, True),
    ({"target": "acct-b", "dry_run": True}, False),
    ({"action": "switch", "target": "acct-b", "dry_run": "true"}, False),
    ({"action": "auto", "dry_run": True}, False),
    ({"action": "plan", "target": "acct-b"}, False),
    ({"action": "list"}, False),
    ({"action": "list", "target": "acct-b"}, False),
    ({}, False),                                                    # list
    ({"action": "switch"}, False),                                  # no target: lists
    ({"target": "   "}, False),                                     # blank target: lists, never a match-all prefix
    ({"action": "bogus", "target": "acct-b"}, False),               # rejected by the handler
]


@pytest.mark.parametrize("args,mutates", _GATE_CASES)
def test_gate_matches_what_the_handler_actually_does(plugin, monkeypatch, args, mutates):
    mod, ctx = plugin
    gate = ctx.hooks["pre_tool_call"]
    ran = []
    monkeypatch.setattr(mod, "switch", lambda target, provider="anthropic", dry_run=False:
                        ran.append(("switch", target, dry_run)) or {"ok": True})
    monkeypatch.setattr(mod, "auto_rotate", lambda provider="anthropic", dry_run=False:
                        ran.append(("auto", dry_run)) or {"ok": True})
    verdict = gate(tool_name="account_switch", args=args)
    registry.dispatch("account_switch", dict(args))
    really_mutated = any((r[0] == "switch" and not r[2]) or (r[0] == "auto" and not r[1]) for r in ran)
    assert really_mutated is mutates
    assert bool(verdict and verdict["action"] == "approve") is mutates


def test_gate_omitted_action_message_names_target_and_provider(plugin):
    _, ctx = plugin
    v = ctx.hooks["pre_tool_call"](tool_name="account_switch", args={"target": " acct-b ", "provider": "anthropic"})
    assert v["action"] == "approve" and "'acct-b'" in v["message"] and "anthropic" in v["message"]


def test_gate_ignores_other_tools_and_fails_closed_for_account_switch(plugin, monkeypatch):
    mod, ctx = plugin
    gate = ctx.hooks["pre_tool_call"]
    assert gate(tool_name="terminal", args={"target": "x"}) is None
    monkeypatch.setattr(mod, "_interpret_tool_args", lambda a: (_ for _ in ()).throw(RuntimeError("boom")))
    assert gate(tool_name="account_switch", args={"action": "list"})["action"] == "approve"
    assert gate(tool_name="terminal", args={}) is None
