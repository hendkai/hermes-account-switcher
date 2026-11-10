"""account-switcher — switch between pooled provider accounts.

Works on top of Hermes' public CLI surface only:

* ``hermes auth list <provider>``      — read the pool (labels, priorities,
  active marker). Tokens are never printed by that command and never parsed
  here beyond labels/status.
* ``hermes auth priority <p> <t> <n>`` — move an entry to a new priority
  (others are renumbered). Switching = "move to priority 0", i.e. that
  account is tried first on the next model call.

Surfaces:

* tool ``account_switch``  — the agent can switch accounts on request
* slash ``/switch``        — same thing from any chat
* CLI ``hermes account-switcher list|switch|plan``

The plugin never touches tokens, never re-authenticates, and never edits
auth.json. It only re-orders the existing, already-authenticated pool.
"""

from __future__ import annotations

import re

from dataclasses import dataclass, field
from typing import Any, List, Optional

DEFAULT_PROVIDER = "anthropic"
_HEADING = re.compile(r"^(?P<provider>\S+)\s+\((?P<count>\d+)\s+credential", re.I)
_ROW = re.compile(
    r"^(?P<idx>#\d+)\s+(?P<label>\S+)\s+(?P<type>\S+)\s+"
    r"id=(?P<id>\S+)\s+priority=(?P<priority>-?\d+)\s+(?P<source>\S+)"
    r"(?P<current>\s*←\s*)?$"
)


@dataclass
class Account:
    index: int
    label: str
    auth_type: str
    id: str
    priority: int
    source: str
    current: bool = False
    notes: List[str] = field(default_factory=list)

    @property
    def safe_label(self) -> str:
        return self.label


def _hermes_bin() -> str:
    import os
    import shutil
    import sys

    cand = shutil.which("hermes")
    if cand:
        return cand
    argv0 = sys.argv[0] if sys.argv and sys.argv[0] else ""
    if argv0 and os.path.isfile(argv0):
        return argv0
    return "hermes"


def _run(args: List[str], timeout: int = 60) -> str:
    """Run `hermes <args>` and return combined output.

    Uses os.posix_spawn + os.waitpid directly: subprocess.run can be
    unavailable inside some host environments, and this path works
    identically on macOS and Linux.
    """
    import os
    import select
    import time

    binpath = _hermes_bin()
    argv = [binpath, *args]
    r_out, w_out = os.pipe()
    r_err, w_err = os.pipe()
    devnull = os.open(os.devnull, os.O_RDONLY)
    try:
        pid = os.posix_spawn(
            binpath, argv, os.environ,
            file_actions=[
                (os.POSIX_SPAWN_DUP2, devnull, 0),
                (os.POSIX_SPAWN_DUP2, w_out, 1),
                (os.POSIX_SPAWN_DUP2, w_err, 2),
            ],
        )
    finally:
        os.close(w_out)
        os.close(w_err)
        os.close(devnull)

    chunks: List[str] = []
    deadline = time.time() + timeout
    open_fds = {r_out, r_err}
    while open_fds and time.time() < deadline:
        ready, _, _ = select.select(list(open_fds), [], [], 1.0)
        for fd in ready:
            try:
                data = os.read(fd, 65536)
            except OSError:
                data = b""
            if data:
                chunks.append(data.decode("utf-8", "replace"))
            else:
                os.close(fd)
                open_fds.discard(fd)
    for fd in open_fds:
        os.close(fd)
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass
    out = "".join(chunks)
    # filter known noise lines
    lines = [
        ln for ln in out.splitlines()
        if "source update" not in ln and ln.strip()
    ]
    return "\n".join(lines)


def _parse_list(text: str) -> tuple[str, List[Account]]:
    provider, accounts = "", []
    for line in text.splitlines():
        line = line.rstrip()
        m = _HEADING.search(line)
        if m:
            provider = m.group("provider")
            continue
        m = _ROW.match(line.strip())
        if m:
            accounts.append(
                Account(
                    index=int(m.group("idx")[1:]),
                    label=m.group("label"),
                    auth_type=m.group("type"),
                    id=m.group("id"),
                    priority=int(m.group("priority")),
                    source=m.group("source"),
                    current=bool(m.group("current")),
                )
            )
    return provider, accounts


# -- optional usage enrichment -------------------------------------------------

def _usage_for_accounts(accounts: List[Account]) -> Optional[dict]:
    """Best-effort per-account usage (5h window / week) for OAuth entries.

    Uses Hermes' account_usage helper when importable (source installs).
    Returns {label: {"five_hour": pct, "week": pct, "reset": str}} or None.
    Tokens are used in-process for the API call and never returned.
    """
    try:
        from agent.credential_pool import load_pool  # type: ignore
        from agent.account_usage import _fetch_anthropic_account_usage  # type: ignore
    except Exception:
        return None
    out: dict = {}
    try:
        pool = load_pool("anthropic")
        by_label = {a.label: a for a in accounts}
        for entry in pool.entries():
            label = str(getattr(entry, "label", "") or "")
            if label not in by_label or getattr(entry, "auth_type", "") != "oauth":
                continue
            try:
                token = (getattr(entry, "access_token", "") or "").strip()
                if not token:
                    continue
                snap = _fetch_anthropic_account_usage(api_key=token)
                if snap is None:
                    continue
                windows = {}
                for w in snap.windows:
                    if w.label in ("Current session", "Current week"):
                        windows["five_hour" if "session" in w.label.lower() else "week"] = {
                            "used_percent": w.used_percent,
                        }
                out[label] = windows
            except Exception:
                continue
    except Exception:
        return out or None
    return out or None


def list_accounts(provider: str = DEFAULT_PROVIDER, with_usage: bool = True) -> dict:
    provider, accounts = _parse_list(_run(["auth", "list", provider]))
    usage = _usage_for_accounts(accounts) if (with_usage and provider == "anthropic") else None
    return {
        "provider": provider,
        "count": len(accounts),
        "usage_available": bool(usage),
        "accounts": [
            {
                "index": a.index,
                "label": a.safe_label,
                "auth_type": a.auth_type,
                "id": a.id,
                "priority": a.priority,
                "current": a.current,
                **({"usage": (usage or {}).get(a.label)} if usage else {}),
            }
            for a in accounts
        ],
    }


def _find(accounts: List[Account], target: str) -> Optional[Account]:
    """Match '#2', an exact label, an entry id, or a unique label prefix."""
    t = target.strip()
    if t.startswith("#"):
        try:
            return next(a for a in accounts if a.index == int(t[1:]))
        except (StopIteration, ValueError):
            return None
    exact = [a for a in accounts if a.label == t or a.id == t]
    if exact:
        return exact[0]
    prefix = [a for a in accounts if a.label.lower().startswith(t.lower())]
    return prefix[0] if len(prefix) == 1 else None


def _switch_blocker(account: Account) -> Optional[str]:
    """Refuse switches that would point Hermes at a dead entry."""
    if account.auth_type == "api_key" and account.source.startswith("env:"):
        env_name = account.source[4:]
        return (
            f"Entry {account.safe_label} is an api_key entry fed by {env_name}; "
            "switching the rotation order to it does not change that env var. "
            "Set the env var instead if you really want this key."
        )
    return None


def switch(
    target: str,
    provider: str = DEFAULT_PROVIDER,
    dry_run: bool = False,
) -> dict:
    """Move *target* to priority 0 so the pool tries it first.

    Returns a plan when dry_run is true, otherwise performs the switch.
    """
    _, accounts = _parse_list(_run(["auth", "list", provider]))
    if not accounts:
        return {"ok": False, "error": f"No credentials found for provider '{provider}'."}
    account = _find(accounts, target)
    if account is None:
        return {
            "ok": False,
            "error": f"No account matches '{target}'. Use /switch list to see accounts.",
            "known": [a.safe_label for a in accounts],
        }
    blocker = _switch_blocker(account)
    if blocker:
        return {"ok": False, "error": blocker}

    plan = {
        "ok": True,
        "dry_run": dry_run,
        "provider": provider,
        "account": account.safe_label,
        "was_priority": account.priority,
        "was_current": account.current,
        "new_priority": 0,
        "note": "Hermes tries priority 0 first; others are renumbered automatically.",
    }
    if dry_run or account.priority == 0:
        plan["already_first"] = account.priority == 0
        return plan

    out = _run(["auth", "priority", provider, str(account.index), "0"])
    if "error" in out.lower() and "updated" not in out.lower():
        return {"ok": False, "error": f"hermes auth priority failed: {out[:200]}"}

    after = list_accounts(provider)
    active = next((a for a in after["accounts"] if a["priority"] == 0), None)
    plan["order_after"] = [a["label"] for a in after["accounts"]]
    plan["verified_first"] = bool(active and (active["label"] == account.label or active["id"] == account.id))
    return plan


# -- rendering ---------------------------------------------------------------

def render_list(data: dict, markdown: bool = False) -> str:
    b = "**" if markdown else ""
    lines = [f"{b}Accounts ({data['provider']}){b}"]
    for a in data["accounts"]:
        mark = "● " if a["current"] else "○ "
        usage = ""
        u = a.get("usage") or {}
        if u:
            parts = []
            for key, short in (("five_hour", "5h"), ("week", "week")):
                pct = (u.get(key) or {}).get("used_percent")
                if pct is not None:
                    parts.append(f"{short} {round(pct)}%")
            usage = " · " + " ".join(parts) if parts else ""
        lines.append(
            f"{mark}#{a['index']} {a['label']} — priority {a['priority']}"
            + ("  (tried first)" if a["priority"] == 0 else "")
            + usage
        )
    lines.append("● = current entry marker · priority 0 is tried first"
                 + ("" if data.get("usage_available") else " · usage % unavailable on this install"))
    return "\n".join(lines)


def render_switch(plan: dict, markdown: bool = False) -> str:
    b = "**" if markdown else ""
    if not plan.get("ok"):
        return f"{b}Switch failed{b}: {plan.get('error')}"
    if plan.get("dry_run"):
        lines = [
            f"{b}Plan{b}: move {plan['account']} to priority 0 "
            f"(currently {plan['was_priority']})."
        ]
    else:
        lines = [
            f"{b}Switched{b}: {plan['account']} is now priority 0 "
            f"(was {plan['was_priority']})."
        ]
        if plan.get("verified_first"):
            lines.append("Verified: it is first in the new rotation order.")
        elif "order_after" in plan:
            lines.append("New order: " + " → ".join(plan["order_after"]))
    return "\n".join(lines)


# -- plugin surfaces ----------------------------------------------------------

def _tool_account_switch(
    action: str = "switch",
    target: str = "",
    provider: str = DEFAULT_PROVIDER,
    dry_run: bool = False,
    **_: Any,
) -> dict:
    """Tool: list pooled accounts or switch the active one.

    action: 'list' | 'switch' | 'plan' (plan = dry-run switch)
    """
    if action == "list" or (action == "switch" and not target):
        return list_accounts(provider)
    if action == "plan":
        return switch(target, provider, dry_run=True)
    if action == "switch":
        return switch(target, provider, dry_run=dry_run)
    return {"ok": False, "error": f"Unknown action '{action}'. Use list, switch, or plan."}


def _setup_argparse(p) -> None:
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="show pooled accounts and priorities")
    sw = sub.add_parser("switch", help="move an account to priority 0")
    sw.add_argument("target", help="#index, exact label, entry id, or unique label prefix")
    sw.add_argument("--provider", default=DEFAULT_PROVIDER)
    sw.add_argument("--dry-run", action="store_true")
    pl = sub.add_parser("plan", help="show what a switch would do")
    pl.add_argument("target")
    pl.add_argument("--provider", default=DEFAULT_PROVIDER)


def _arg(args, name: str, default):
    val = getattr(args, name, None)
    return default if val is None else val


def _handle_cli(args) -> None:
    cmd = _arg(args, "cmd", "list")
    if cmd == "list":
        print(render_list(list_accounts(_arg(args, "provider", DEFAULT_PROVIDER))))
    elif cmd == "plan":
        print(render_switch(switch(args.target, _arg(args, "provider", DEFAULT_PROVIDER), dry_run=True)))
    elif cmd == "switch":
        print(render_switch(switch(args.target, _arg(args, "provider", DEFAULT_PROVIDER), dry_run=_arg(args, "dry_run", False))))


def _slash(arg: str = "", **_: Any) -> str:
    parts = (arg or "").strip().split()
    if not parts or parts[0] in {"list", "ls"}:
        return render_list(list_accounts(), markdown=True)
    provider = DEFAULT_PROVIDER
    if parts[0].startswith("--provider="):
        provider = parts[0].split("=", 1)[1]
        parts = parts[1:]
    if not parts:
        return render_list(list_accounts(provider), markdown=True)
    target = parts[0]
    if parts[:1] == ["plan"] and len(parts) > 1:
        return render_switch(switch(parts[1], provider, dry_run=True), markdown=True)
    return render_switch(switch(target, provider), markdown=True)


def register(ctx) -> None:
    ctx.register_tool(
        name="account_switch",
        toolset="plugin_account_switcher",
        schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "switch", "plan"],
                    "description": "list = show all pooled accounts; switch = move target to priority 0; plan = dry-run preview",
                    "default": "switch",
                },
                "target": {
                    "type": "string",
                    "description": "#index, exact label, entry id, or unique label prefix (required for switch/plan)",
                },
                "provider": {
                    "type": "string",
                    "description": "provider id of the credential pool",
                    "default": "anthropic",
                },
                "dry_run": {"type": "boolean", "default": False},
            },
        },
        handler=_tool_account_switch,
        description=(
            "List or switch the active account of a provider credential pool "
            "(e.g. which Claude login Hermes uses). action=list shows all "
            "accounts with priorities; action=switch moves the target account "
            "to the front of the rotation; action=plan previews the switch."
        ),
    )
    ctx.register_cli_command(
        name="account-switcher",
        help="Switch between pooled provider accounts (e.g. multiple Claude logins)",
        setup_fn=_setup_argparse,
        handler_fn=_handle_cli,
    )
    ctx.register_command(
        "switch",
        handler=_slash,
        description="Switch pooled accounts (/switch list, /switch <label>)",
        args_hint="[list|<label>|plan <label>]",
    )
