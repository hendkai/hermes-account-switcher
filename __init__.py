"""credential-pool-switcher — switch between pooled provider accounts.

Surfaces:

* tool ``account_switch``  — the agent can list/plan; a real switch is
  escalated to the human-approval gate by a ``pre_tool_call`` hook
* slash ``/switch``        — same thing from any chat
* CLI ``hermes credential-pool-switcher list|switch|plan|status|setup``
* desktop status bar chip (``desktop/plugin.js``)

How it touches Hermes:

* **Listing / switching** read the pool through ``agent.credential_pool``
  (``load_pool`` -> ``entries()`` / ``peek()`` / ``move_entry()``) — the same
  calls ``hermes auth list`` / ``hermes auth priority`` make. When that
  module is not importable it falls back to running those two CLI commands
  and parsing their output. Switching = "move to priority 0", i.e. that
  account is tried first on the next model call (``fill_first`` strategy).
* **Usage percentages** (list only) go through Hermes' public
  ``agent.account_usage.fetch_account_usage(read_only=True)``: each usable
  pooled OAuth token is sent to the provider's own API (api.anthropic.com) by
  that helper. Dead and still-benched logins are skipped; nothing is
  refreshed, rotated or written.

The plugin never reads or writes auth.json itself and never refreshes tokens.
Tokens are never printed, logged, returned, or written by this plugin.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time

from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

log = logging.getLogger("credential_pool_switcher")

DEFAULT_PROVIDER = "anthropic"
STATUS_EXHAUSTED = "exhausted"
STATUS_DEAD = "dead"
STRATEGY_FILL_FIRST = "fill_first"

_HEADING = re.compile(r"^(?P<provider>\S+)\s+\((?P<count>\d+)\s+credential", re.I)
# The label column is padded (``{label:<20}``) and may itself contain spaces:
# take everything lazily up to "<type> id=".
_ROW = re.compile(
    r"^(?P<idx>#\d+)\s+(?P<label>.+?)\s+(?P<type>\S+)\s+"
    r"id=(?P<id>\S+)\s+priority=(?P<priority>-?\d+)\s+(?P<source>\S+)"
    r"(?P<rest>.*)$"
)
# Any exhaustion class the core prints ("rate-limited", "billing", "exhausted",
# ...) ends with a "(<remaining> left)" group while the bench is still running.
# No reset timestamp stored: the core falls back to a per-error TTL; one hour is its 429 default.
_EXHAUSTED_FALLBACK_SECONDS = 60 * 60
_REST_BENCH = re.compile(r"\((?P<left>[^()]*\bleft)\)", re.I)


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
    status: str = "ok"                  # ok | exhausted | dead
    bench_until: Optional[float] = None  # epoch seconds, only while still benched

    @property
    def safe_label(self) -> str:
        return self.label


# -- small helpers -------------------------------------------------------------

def _iso_utc(epoch: float) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).isoformat()


def _to_epoch(value: Any) -> Optional[float]:
    """Epoch seconds from epoch s/ms (number or numeric string) or ISO-8601 (naive = UTC)."""
    import datetime

    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v <= 0:
            return None
        return v / 1000.0 if v > 1_000_000_000_000 else v
    raw = str(value).strip()
    try:
        return _to_epoch(float(raw))
    except ValueError:
        pass
    try:
        dt = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


def _fmt_left(seconds: float) -> str:
    s = max(0, int(math.ceil(seconds)))
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m, sec = divmod(rem, 60)
    parts = [(d, "d"), (h, "h"), (m, "m"), (sec, "s")]
    first = next((i for i, (v, _) in enumerate(parts) if v), 3)
    return " ".join(f"{v}{u}" for v, u in parts[first:first + 2])


def _display_source(source: str) -> str:
    return source.split(":", 1)[1] if source.startswith("manual:") else source


# -- subprocess fallback ---------------------------------------------------------

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


def _run_subprocess(argv: List[str], timeout: int = 60) -> str:
    """Windows fallback for :func:`_run`: plain ``subprocess.run`` with a hard
    timeout. On ``TimeoutExpired`` the partial output is still returned so a
    hung ``hermes`` cannot hang the tool call."""
    import os
    import subprocess

    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=os.environ,
        )
        raw = (proc.stdout or b"") + b"\n" + (proc.stderr or b"")
    except subprocess.TimeoutExpired as exc:
        raw = (exc.stdout or b"") + b"\n" + (exc.stderr or b"")
    out = raw.decode("utf-8", "replace")
    lines = [
        ln for ln in out.splitlines()
        if "source update" not in ln and ln.strip()
    ]
    return "\n".join(lines)


def _run(args: List[str], timeout: int = 60) -> str:
    """Run `hermes <args>` and return combined output.

    Uses os.posix_spawn + os.waitpid directly: subprocess.run can be
    unavailable inside some host environments, and this path works
    identically on macOS and Linux.
    """
    import os

    binpath = _hermes_bin()
    argv = [binpath, *args]
    if not hasattr(os, "posix_spawn"):
        # Windows: no posix_spawn (and no select on pipes) — use subprocess.
        return _run_subprocess(argv, timeout)
    import select

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
    timed_out = bool(open_fds)  # fds still open => child still running at the deadline
    for fd in open_fds:
        os.close(fd)
    if timed_out:
        # A hung `hermes` must not hang the tool or slash call: kill the child
        # before the blocking waitpid (SIGTERM, then SIGKILL after a short grace).
        import signal
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        else:
            grace = time.time() + 2.0
            reaped = False
            while time.time() < grace:
                try:
                    done, _ = os.waitpid(pid, os.WNOHANG)
                    if done == pid:
                        reaped = True
                        break
                except ChildProcessError:
                    reaped = True
                    break
                time.sleep(0.05)
            if not reaped:
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
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
    """Parse ``hermes auth list <provider>`` output (CLI fallback path)."""
    provider, accounts = "", []
    for line in text.splitlines():
        line = line.rstrip()
        m = _HEADING.search(line)
        if m:
            provider = m.group("provider")
            continue
        m = _ROW.match(line.strip())
        if m:
            rest = m.group("rest") or ""
            bench = _REST_BENCH.search(rest)
            accounts.append(
                Account(
                    index=int(m.group("idx")[1:]),
                    label=m.group("label").strip(),
                    auth_type=m.group("type"),
                    id=m.group("id"),
                    priority=int(m.group("priority")),
                    source=m.group("source"),
                    current="←" in rest,
                    notes=[f"({bench.group('left').strip()})"] if bench else [],
                    status=STATUS_EXHAUSTED if bench else "ok",
                )
            )
    return provider, accounts


# -- direct pool access ------------------------------------------------------------

def _load_pool(provider: str):
    try:
        from agent.credential_pool import load_pool  # type: ignore
    except Exception as exc:
        log.debug("agent.credential_pool not importable (%s); using the CLI fallback", exc)
        return None
    try:
        return load_pool(provider)
    except Exception as exc:
        log.warning("load_pool(%s) failed: %s", provider, exc)
        return None


def _pool_strategy(provider: str) -> str:
    """Configured selection strategy, or "" when it cannot be determined."""
    try:
        from agent.credential_pool import get_pool_strategy  # type: ignore
        return str(get_pool_strategy(provider) or "")
    except Exception as exc:
        log.debug("get_pool_strategy unavailable: %s", exc)
        return ""


def _bench_until(entry) -> Optional[float]:
    if getattr(entry, "last_status", None) != STATUS_EXHAUSTED:
        return None
    reset = _to_epoch(getattr(entry, "last_error_reset_at", None))
    if reset is not None:
        return reset
    at = _to_epoch(getattr(entry, "last_status_at", None))
    return at + _EXHAUSTED_FALLBACK_SECONDS if at else None


def _accounts_from_pool(pool) -> List[Account]:
    try:
        current = pool.peek()
    except Exception as exc:
        log.debug("pool.peek() failed: %s", exc)
        current = None
    now = time.time()
    out: List[Account] = []
    for idx, e in enumerate(pool.entries(), start=1):
        status = str(getattr(e, "last_status", "") or "ok")
        until = _bench_until(e)
        notes: List[str] = []
        if status == STATUS_DEAD:
            notes = ["dead — re-login needed"]
            until = None
        elif until is not None and until > now:
            notes = [f"({_fmt_left(until - now)} left)"]
        else:
            until = None
        out.append(Account(
            index=idx,
            label=str(getattr(e, "label", "") or e.id),
            auth_type=str(getattr(e, "auth_type", "") or ""),
            id=str(e.id),
            priority=int(getattr(e, "priority", idx - 1)),
            source=_display_source(str(getattr(e, "source", "") or "")),
            current=bool(current is not None and current.id == e.id),
            notes=notes,
            status=status if status in (STATUS_EXHAUSTED, STATUS_DEAD) else "ok",
            bench_until=until,
        ))
    return out


def _read_accounts(provider: str) -> Tuple[str, List[Account]]:
    """Pool rows in priority order: direct pool access, CLI parsing as fallback."""
    pool = _load_pool(provider)
    if pool is not None:
        try:
            return provider, _accounts_from_pool(pool)
        except Exception as exc:
            log.warning("reading pool %s directly failed (%s); using the CLI fallback", provider, exc)
    return _parse_list(_run(["auth", "list", provider]))


# -- usage (public helper) ---------------------------------------------------------

def _usage_for_accounts(accounts: List[Account], provider: str = DEFAULT_PROVIDER) -> Optional[dict]:
    """Best-effort per-account usage for the list view.

    Uses Hermes' public ``fetch_account_usage(read_only=True)``: it never
    refreshes, rotates or benches a pool entry. Dead and still-benched logins
    are skipped (their token is useless / the endpoint rate-limits), so an
    expired idle login simply shows no percentages.
    Returns ``{label: {"five_hour": ..., "week": ..., "scoped": {...}}}`` or None.
    """
    wanted = {a.label for a in accounts
              if a.auth_type == "oauth" and a.status != STATUS_DEAD and not _is_benched(a, time.time())}
    if not wanted:
        return None
    try:
        from agent.account_usage import fetch_account_usage
    except Exception as exc:
        log.debug("agent.account_usage not importable: %s", exc)
        return None
    pool = _load_pool(provider)
    if pool is None:
        return None
    out: dict = {}
    for entry in pool.entries():
        label = str(getattr(entry, "label", "") or "")
        token = (getattr(entry, "access_token", "") or "").strip()
        if label not in wanted or getattr(entry, "auth_type", "") != "oauth" or not token:
            continue
        try:
            snap = fetch_account_usage(provider, api_key=token, read_only=True)
        except Exception as exc:
            log.debug("usage fetch for %s failed: %s", label, type(exc).__name__)
            continue
        if snap is None:
            continue
        windows: dict = {}
        for w in snap.windows:
            name = str(w.label or "").lower()
            pct = w.used_percent
            if getattr(w, "scope", "account") == "model":
                windows["scoped"] = {"used_percent": pct, "active": bool(pct is not None and pct >= 100)}
            elif "session" in name:
                windows["five_hour"] = {"used_percent": pct}
            elif "week" in name:
                windows["week"] = {"used_percent": pct}
        if windows:
            out[label] = windows
    return out or None


# -- list / switch -----------------------------------------------------------------

def list_accounts(provider: str = DEFAULT_PROVIDER, with_usage: bool = True) -> dict:
    provider_found, accounts = _read_accounts(provider)
    provider = provider_found or provider
    usage = _usage_for_accounts(accounts, provider) if (with_usage and provider == "anthropic") else None
    return {
        "provider": provider,
        "count": len(accounts),
        "strategy": _pool_strategy(provider),
        "usage_available": bool(usage),
        "accounts": [
            {
                "index": a.index,
                "label": a.safe_label,
                "auth_type": a.auth_type,
                "id": a.id,
                "priority": a.priority,
                "source": a.source,
                "current": a.current,
                "status": a.status,
                "needs_login": a.status == STATUS_DEAD,
                "benched": a.notes[0] if (a.notes and a.status != STATUS_DEAD) else None,
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


def _add_hint(provider: str) -> str:
    return f"hermes auth add {provider} --type oauth --label <name>"


def _relogin_hint(provider: str, label: str, source: str) -> str:
    """How to replace a dead login, by where the credential comes from."""
    if source == "claude_code":
        return "log in again in the Claude Code CLI (`claude` → /login); Hermes re-reads it automatically"
    if source.startswith("env:"):
        return f"put a fresh token into {source[4:]} (e.g. ~/.hermes/.env)"
    return (f"hermes auth remove {provider} '{label}' && "
            f"hermes auth add {provider} --type oauth --label '{label}'")


def _switch_blocker(account: Account, provider: str = DEFAULT_PROVIDER) -> Optional[str]:
    """Refuse switches that would point Hermes at a dead entry."""
    if account.status == STATUS_DEAD:
        return (
            f"Entry {account.safe_label} is dead (its refresh token was rejected); "
            "Hermes never uses it again, whatever its priority. Log in anew: "
            + _relogin_hint(provider, account.safe_label, account.source)
        )
    if account.auth_type == "api_key" and account.source.startswith("env:"):
        env_name = account.source[4:]
        return (
            f"Entry {account.safe_label} is an api_key entry fed by {env_name}; "
            "switching the rotation order to it does not change that env var. "
            "Set the env var instead if you really want this key."
        )
    return None


def _strategy_note(provider: str, strategy: str) -> Optional[str]:
    if strategy and strategy != STRATEGY_FILL_FIRST:
        return (
            f"{provider} uses the {strategy} strategy; priority only orders fill_first "
            "selection, so the switch barely changes which account is used "
            f"(set credential_pool_strategies.{provider}: fill_first in config.yaml)."
        )
    return None


PLUGIN_ID = "credential-pool-switcher"
STATUS_LINE_PREFIX = "CPS_STATUS "  # sentinel the desktop half parses out of `status --json`


def _notify_changed(provider: str) -> None:
    """Tell connected desktop windows the pool order/bench state changed (no-op outside the gateway).

    Only inside a process that already runs the gateway transport: importing it
    from a plain CLI run (cron, ``cli.exec``) would pull in the whole transport
    stack, which takes over stdout — and there is no desktop client to reach anyway.
    """
    import sys

    if "tui_gateway.server" not in sys.modules:
        return
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event
        broadcast_plugin_event(PLUGIN_ID, "pool.changed", {"provider": provider})
    except Exception as exc:
        log.debug("pool.changed broadcast skipped: %s", exc)


def _is_benched(account: Account, now: float) -> bool:
    if account.status != STATUS_EXHAUSTED:
        return False
    if account.bench_until is not None:
        return account.bench_until > now
    return bool(account.notes)  # CLI fallback: only "(… left)" is known


def pool_status(provider: str = DEFAULT_PROVIDER) -> dict:
    """Cheap snapshot for status indicators: no usage fetch, no token refresh, no network.

    ``state``: ``setup`` (no accounts yet), ``ok`` (all usable), ``degraded``
    (some benched/dead, at least one usable), ``exhausted`` (none usable).
    ``active`` is the entry Hermes tries next: the lowest priority that is
    neither dead nor still benched.
    """
    provider_found, accounts = _read_accounts(provider)
    provider = provider_found or provider
    now = time.time()
    usable = [a for a in accounts if a.status != STATUS_DEAD and not _is_benched(a, now)]
    active = min(usable, key=lambda a: a.priority) if usable else None
    benched_until = [a.bench_until for a in accounts if a.bench_until and a.bench_until > now]
    if not accounts:
        state = "setup"
    elif not usable:
        state = "exhausted"
    elif len(usable) < len(accounts):
        state = "degraded"
    else:
        state = "ok"
    rows = []
    for a in accounts:
        status = "dead" if a.status == STATUS_DEAD else ("benched" if _is_benched(a, now) else "ok")
        rows.append({
            "index": a.index,
            "label": a.label,
            "priority": a.priority,
            "auth_type": a.auth_type,
            "source": a.source,
            "status": status,
            "active": active is not None and a.id == active.id,
            "bench_until": _iso_utc(a.bench_until) if a.bench_until else None,
            "bench_left": (a.notes[0].strip("()").removesuffix(" left") if status == "benched" and a.notes else None),
            # Only a usable account that is not already next: switching onto a
            # benched one changes nothing until its reset.
            "switchable": (status == "ok" and _switch_blocker(a, provider) is None
                           and not (active is not None and a.id == active.id)),
            "relogin_hint": _relogin_hint(provider, a.label, a.source) if status == "dead" else None,
        })
    return {
        "provider": provider,
        "state": state,
        "strategy": _pool_strategy(provider),
        "strategy_note": _strategy_note(provider, _pool_strategy(provider)),
        "count": len(accounts),
        "usable": len(usable),
        "active": next((r for r in rows if r["active"]), None),
        "next_ready_at": _iso_utc(min(benched_until)) if benched_until else None,
        "add_hint": _add_hint(provider),
        "accounts": rows,
    }


def render_setup(status: dict, markdown: bool = False) -> str:
    """Onboarding text that adapts to the current pool state."""
    b = "**" if markdown else ""
    tick = "`" if markdown else ""
    provider = status["provider"]
    lines = [f"{b}Account switcher setup ({provider}){b}"]
    if status["state"] == "setup":
        lines.append("No accounts yet. They live in Hermes' credential pool (~/.hermes/auth.json).")
    else:
        lines.append(f"{status['count']} account(s) in the pool, {status['usable']} usable right now"
                     + (f" — Hermes uses {status['active']['label']} next." if status.get("active") else "."))
    lines += [
        f"1. Add each Claude account once (opens the browser login): {tick}{_add_hint(provider)}{tick}",
        f"2. Check: {tick}/switch list{tick} — or the status bar chip in the desktop app.",
        f"3. Pick one by hand: {tick}/switch <label>{tick}.",
    ]
    for row in status["accounts"]:
        if row["status"] == "dead":
            lines.append(f"✖ {row['label']} is dead — {row['relogin_hint']}")
    if status.get("strategy_note"):
        lines.append(f"⚠ {status['strategy_note']}")
    return "\n".join(lines)


def _move_to_front(provider: str, account: Account) -> Optional[str]:
    """Move *account* to priority 0. Returns an error string or None."""
    pool = _load_pool(provider)
    if pool is not None and hasattr(pool, "move_entry"):
        try:
            if pool.move_entry(account.id, 0) is None:
                return f"no pool entry with id {account.id}"
            return None
        except Exception as exc:
            log.warning("pool.move_entry failed (%s); retrying via `hermes auth priority`", exc)
    out = _run(["auth", "priority", provider, account.id, "0"])
    if not re.search(r"^Set \S+ credential ", out, re.M):
        return f"hermes auth priority failed: {out[:200]}"
    return None


def switch(
    target: str,
    provider: str = DEFAULT_PROVIDER,
    dry_run: bool = False,
) -> dict:
    """Move *target* to priority 0 so the pool tries it first.

    Returns a plan when dry_run is true, otherwise performs the switch.
    """
    _, accounts = _read_accounts(provider)
    if not accounts:
        return {"ok": False, "error": f"No credentials found for provider '{provider}'. "
                                      f"Add an account: {_add_hint(provider)}"}
    account = _find(accounts, target)
    if account is None:
        return {
            "ok": False,
            "error": f"No account matches '{target}'. Use /switch list to see accounts.",
            "known": [a.safe_label for a in accounts],
        }
    blocker = _switch_blocker(account, provider)
    if blocker:
        return {"ok": False, "error": blocker}

    strategy = _pool_strategy(provider)
    plan: dict = {
        "ok": True,
        "dry_run": dry_run,
        "provider": provider,
        "account": account.safe_label,
        "was_priority": account.priority,
        "was_current": account.current,
        "new_priority": 0,
        "strategy": strategy,
        "note": "Hermes tries priority 0 first; others are renumbered automatically.",
    }
    if account.bench_until and account.bench_until > time.time():
        plan["warning"] = (
            f"{account.safe_label} is benched until {_iso_utc(account.bench_until)}; "
            "Hermes skips it until then even at priority 0."
        )
    note = _strategy_note(provider, strategy)
    if note:
        plan["strategy_note"] = note
    if dry_run or account.priority == 0:
        plan["already_first"] = account.priority == 0
        return plan

    error = _move_to_front(provider, account)
    if error:
        return {"ok": False, "error": error}
    _notify_changed(provider)

    # Verification only needs the order — no usage fetch (the usage endpoint 429s).
    after = list_accounts(provider, with_usage=False)
    active = next((a for a in after["accounts"] if a["priority"] == 0), None)
    plan["order_after"] = [a["label"] for a in after["accounts"]]
    plan["verified_first"] = bool(active and (active["label"] == account.label or active["id"] == account.id))
    return plan


# -- rendering ---------------------------------------------------------------

def render_list(data: dict, markdown: bool = False) -> str:
    b = "**" if markdown else ""
    lines = [f"{b}Accounts ({data['provider']}){b}"]
    if not data["accounts"]:
        lines.append(f"No accounts in the {data['provider']} pool yet. Add one (opens the provider's "
                     f"login in the browser): {_add_hint(data['provider'])}")
        return "\n".join(lines)
    relogin: List[str] = []
    for a in data["accounts"]:
        if a.get("needs_login") or a.get("status") == STATUS_DEAD:
            relogin.append(f"  {a['label']}: "
                           + _relogin_hint(data["provider"], a["label"], str(a.get("source") or "")))
    for a in data["accounts"]:
        benched = a.get("benched")
        dead = a.get("needs_login") or a.get("status") == STATUS_DEAD
        mark = "✖ " if dead else ("⛔ " if benched else ("● " if a["current"] else "○ "))
        usage = ""
        u = a.get("usage") or {}
        if u:
            parts = []
            for key, short in (("five_hour", "5h"), ("week", "week")):
                pct = (u.get(key) or {}).get("used_percent")
                if pct is not None:
                    parts.append(f"{short} {round(pct)}%")
            scoped = u.get("scoped") or {}
            if scoped.get("used_percent") is not None:
                flag = "⛔" if scoped.get("active") else ""
                parts.append(f"scoped {round(scoped['used_percent'])}%{flag}")
            usage = " · " + " ".join(parts) if parts else ""
        lines.append(
            f"{mark}#{a['index']} {a['label']} — priority {a['priority']}"
            + ("" if dead or benched or a["priority"] != 0 else "  (tried first)")
            + (" · dead — re-login needed" if dead else "")
            + (f" · benched {benched}" if benched and not dead else "")
            + usage
        )
    lines.append("● = current entry marker · priority 0 is tried first · ⛔ = benched (exhausted)"
                 " · ✖ = dead (log in anew)"
                 + ("" if data.get("usage_available") else " · usage % unavailable"))
    if relogin:
        lines.append("To revive dead logins:")
        lines.extend(relogin)
    lines.append(f"More accounts: {_add_hint(data['provider'])} (stored in Hermes' auth.json)")
    note = _strategy_note(data["provider"], data.get("strategy") or "")
    if note:
        lines.append(f"⚠ {note}")
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
    for key in ("warning", "strategy_note"):
        if plan.get(key):
            lines.append(f"⚠ {plan[key]}")
    return "\n".join(lines)


# -- plugin surfaces ----------------------------------------------------------

def _on_pre_tool_call(tool_name: str = "", args: Optional[dict] = None, **_: Any) -> Optional[dict]:
    """Escalate model-initiated real switches to the human-approval gate.

    Reordering the credential pool changes which account gets billed first,
    so a ``switch`` issued by the model must not run silently — a
    prompt-injected turn could otherwise redirect spend. Read-only calls
    (``list``, ``plan``, ``dry_run``) pass through untouched. Never raises:
    a broken gate must not break every other tool.
    """
    try:
        if tool_name != "account_switch" or not isinstance(args, dict):
            return None
        if args.get("dry_run"):
            return None
        if args.get("action") == "switch":
            if not str(args.get("target") or "").strip():
                return None  # switch without target just lists
            return {
                "action": "approve",
                "message": (
                    "account_switch: move "
                    f"'{args.get('target')}' to priority 0 of the "
                    f"{args.get('provider') or DEFAULT_PROVIDER} credential pool "
                    "(changes which account is tried/billed first)."
                ),
            }
        return None
    except Exception:
        return None


def _tool_account_switch(args: dict, **_: Any) -> str:
    """Tool: list pooled accounts or switch the active one.

    Hermes dispatches plugin handlers as ``handler(args, **kwargs)`` — the
    whole argument dict arrives positionally, so take the dict and read the
    fields out of it. Returns a JSON string (the tool result contract).
    """
    if not isinstance(args, dict):
        args = {}
    action = str(args.get("action") or "switch")
    target = str(args.get("target") or "")
    provider = str(args.get("provider") or DEFAULT_PROVIDER) or DEFAULT_PROVIDER
    dry_run = bool(args.get("dry_run", False))
    result: dict
    if action == "list" or (action == "switch" and not target):
        result = list_accounts(provider)
    elif action == "plan":
        if not target.strip():
            result = {"ok": False, "error": "plan needs a target (#index, label, id, or unique prefix)."}
        else:
            result = switch(target, provider, dry_run=True)
    elif action == "switch":
        result = switch(target, provider, dry_run=dry_run)
    else:
        result = {"ok": False, "error": f"Unknown action '{action}'. Use list, switch, or plan."}
    return json.dumps(result, default=str)


def _setup_argparse(p) -> None:
    sub = p.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list", help="show pooled accounts and priorities")
    ls.add_argument("--provider", default=DEFAULT_PROVIDER)
    sw = sub.add_parser("switch", help="move an account to priority 0")
    sw.add_argument("target", help="#index, exact label, entry id, or unique label prefix")
    sw.add_argument("--provider", default=DEFAULT_PROVIDER)
    sw.add_argument("--dry-run", action="store_true")
    pl = sub.add_parser("plan", help="show what a switch would do")
    pl.add_argument("target")
    pl.add_argument("--provider", default=DEFAULT_PROVIDER)
    st = sub.add_parser("status", help="pool snapshot for status indicators (no network)")
    st.add_argument("--provider", default=DEFAULT_PROVIDER)
    st.add_argument("--json", action="store_true", help=f"one '{STATUS_LINE_PREFIX.strip()} <json>' line")
    se = sub.add_parser("setup", help="how to add accounts and what to do next")
    se.add_argument("--provider", default=DEFAULT_PROVIDER)


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
    elif cmd == "status":
        status = pool_status(_arg(args, "provider", DEFAULT_PROVIDER))
        if _arg(args, "json", False):
            print(STATUS_LINE_PREFIX + json.dumps(status, default=str))
        else:
            active = status.get("active")
            print(f"{status['provider']}: {status['state']} · {status['usable']}/{status['count']} usable"
                  + (f" · next: {active['label']}" if active else ""))
    elif cmd == "setup":
        print(render_setup(pool_status(_arg(args, "provider", DEFAULT_PROVIDER))))


_SLASH_USAGE = (
    "Usage: /switch [list] · /switch setup · "
    "/switch <label> · /switch plan <label> · --provider=<id>\n"
    "Accounts live in Hermes' credential pool (~/.hermes/auth.json). Add one: "
    "hermes auth add anthropic --type oauth --label <name>"
)


def _slash(arg: str = "", **_: Any) -> str:
    parts = (arg or "").strip().split()
    provider = DEFAULT_PROVIDER
    rest: List[str] = []
    for p in parts:
        if p.startswith("--provider="):
            provider = p.split("=", 1)[1] or DEFAULT_PROVIDER
        else:
            rest.append(p)
    parts = rest
    if not parts or parts[0] in {"list", "ls"}:
        return render_list(list_accounts(provider), markdown=True)
    if parts[0] in {"help", "-h", "--help"}:
        return _SLASH_USAGE
    if parts[0] in {"setup", "onboarding"}:
        return render_setup(pool_status(provider), markdown=True)
    if parts[0] == "plan":
        if len(parts) < 2:
            return _SLASH_USAGE
        return render_switch(switch(parts[1], provider, dry_run=True), markdown=True)
    return render_switch(switch(parts[0], provider), markdown=True)


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_tool(
        name="account_switch",
        toolset="plugin_credential_pool_switcher",
        schema={
            "name": "account_switch",
            "description": (
                "List or switch the active account of a provider credential pool "
                "(e.g. which Claude login Hermes uses)."
            ),
            "parameters": {
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
        },
        handler=_tool_account_switch,
        description=(
            "List or switch the active account of a provider credential pool "
            "(e.g. which Claude login Hermes uses). action=list shows all "
            "accounts with priorities, bench and dead state; action=switch moves "
            "the target account to the front of the rotation (escalated to human "
            "approval — it changes which account is billed first); action=plan "
            "previews the switch. This tool never creates "
            "accounts: they live in Hermes' credential pool (~/.hermes/auth.json) and "
            "the user adds one with `hermes auth add <provider> --type oauth --label <name>` "
            "(browser login); dead logins are replaced with `hermes auth remove` + `add`."
        ),
    )
    ctx.register_cli_command(
        name="credential-pool-switcher",
        help="Switch between pooled provider accounts (e.g. multiple Claude logins)",
        setup_fn=_setup_argparse,
        handler_fn=_handle_cli,
    )
    ctx.register_command(
        "switch",
        handler=_slash,
        description="Switch pooled accounts (/switch list, /switch setup, /switch <label>)",
        args_hint="[list|setup|<label>|plan <label>] [--provider=<id>]",
    )
