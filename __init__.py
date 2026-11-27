"""credential-pool-switcher — switch between pooled provider accounts.

Surfaces:

* tool ``account_switch``  — the agent can list/plan; a real switch or an
  auto rotation is escalated to the human-approval gate by a
  ``pre_tool_call`` hook
* slash ``/switch``        — same thing from any chat
* CLI ``hermes credential-pool-switcher list|switch|plan|auto|status|setup``
* desktop status bar chip (``desktop/plugin.js``)

How it touches Hermes:

* **Reading** (list, status, usage, auto ranking) uses a non-mutating snapshot:
  core's ``read_credential_pool`` rows wrapped in ``PooledCredential`` objects.
  ``load_pool()`` and ``peek()`` are NOT used for reads: they can seed or prune
  entries, clear exhaustion and persist. Rows borrowed from another tool (e.g.
  Claude Code) carry no token in that snapshot, so they show no usage and are never
  promoted by auto. When the read accessor is missing (older core) a display-only
  fallback parses ``hermes auth list``; such rows are non-authoritative.
* **Switching** = "move to priority 0" through ``load_pool().move_entry()`` (the call
  behind ``hermes auth priority``): the only place the real pool is loaded. Auto
  rotation re-validates the exact id on that same pool object right before the move.
  The account is tried first on the next model call (``fill_first`` strategy).
* **Usage percentages** (list only) go through Hermes' public
  ``fetch_account_usage(read_only=True, identity_id=<entry id>)``: each usable
  pooled OAuth token is sent to the provider's own API (api.anthropic.com) by
  that helper. Dead logins are skipped; no token is refreshed or rotated and no
  cooldown or bench state is written. Hermes' own usage cache may remember the
  returned snapshot, per credential.
* **Auto rotation** (``auto``) uses the same usage data and ``switch`` (the
  pool's ``move_entry``) to promote the login with the most headroom. It runs
  only when you opt in (``plugins.credential-pool-switcher.auto_rotate: true``
  in config.yaml; off by default): the ``pre_llm_call`` hook then starts it in
  the background at most every 10 minutes. It does
  not bench logins: Hermes' own pool handles 429s. It ranks by account-level
  windows (5h, week) only: a model-family cap (Opus/Sonnet week) is shown as a
  warning but does not disqualify a login, and a login with only model-scoped
  data, or exhausted without a stored reset, is never promoted. "Usage-eligible"
  is not message-verified: the plugin sends no message request and writes no
  cooldown. Besides the pool, the plugin
  keeps one small state file (``credential-pool-switcher-state.json`` in the
  Hermes home: timestamps and the last result, no credentials).

The plugin never reads or writes auth.json itself and never refreshes tokens.
Tokens are never printed, logged, returned, or written by this plugin.
"""

from __future__ import annotations

import inspect
import json
import logging
import math
import os
import re
import threading
import time

from dataclasses import dataclass, field
from pathlib import Path
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
# A pool row that is exhausted but stores no reset timestamp has a cooldown the
# core derives from a per-error TTL; this plugin cannot know it and never guesses
# one: such a row is "cooldown unknown" and is never promoted by auto rotation.
COOLDOWN_UNKNOWN_NOTE = "(cooldown unknown)"
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
    bench_until: Optional[float] = None  # epoch seconds, only while still benched (explicit reset)
    bench_unknown: bool = False          # exhausted, but no stored reset: end of the cooldown unknown
    # True when read straight from the pool. The CLI-text fallback cannot show dead
    # logins or an exhausted row without a stored reset, so it sets this to False and
    # such a row is never treated as proven eligible for an automatic move.
    authoritative: bool = True

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


def _pct(value: Any, default: float = 999.0) -> float:
    """Utilization for sorting: a real 0 stays 0 — only a missing value sorts last."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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
                    authoritative=False,
                )
            )
    return provider, accounts


# -- direct pool access ------------------------------------------------------------

class PoolReadError(RuntimeError):
    """The pool is reachable but could not be read safely; callers fail closed."""


class _PoolSnapshot:
    """Read-only view of the persisted pool rows: ``entries()`` only.

    Built from core's ``read_credential_pool`` accessor. It is deliberately NOT a
    ``CredentialPool``: no ``load_pool`` seeding, pruning of dead manual logins,
    exhaustion clearing, priority normalisation or ``_persist`` can happen, and
    there is no ``peek``/``select`` (those run ``_available_entries``, which can
    clear cooldowns and prune entries and then persist).
    """

    def __init__(self, entries) -> None:
        self._entries = list(entries)

    def entries(self) -> list:
        return list(self._entries)


def _load_pool(provider: str):
    """Non-mutating snapshot of the pool, or None when core has no read accessor.

    None means "use the CLI-text fallback" (older core). A pool that exists but
    cannot be read raises :class:`PoolReadError`: nothing is retried through a
    helper that could write.
    """
    try:
        from agent.credential_pool import PooledCredential  # type: ignore
        from hermes_cli.auth import read_credential_pool  # type: ignore
    except Exception as exc:
        log.debug("read-only pool accessors not importable (%s); using the CLI fallback", exc)
        return None
    try:
        rows = read_credential_pool(provider)
        entries = [PooledCredential.from_dict(provider, row) for row in rows if isinstance(row, dict)]
        entries.sort(key=lambda e: int(getattr(e, "priority", 0) or 0))
    except Exception as exc:
        log.warning("reading the %s pool snapshot failed: %s", provider, type(exc).__name__)
        raise PoolReadError(f"could not read the {provider} credential pool ({type(exc).__name__})") from exc
    return _PoolSnapshot(entries)


def _load_writable_pool(provider: str):
    """The real ``CredentialPool`` — used ONLY by :func:`_move_to_front`.

    ``load_pool`` may normalise priorities, seed or prune entries and persist
    them: acceptable for a move (which persists anyway, like ``hermes auth
    priority``), never for a read path.
    """
    try:
        from agent.credential_pool import load_pool  # type: ignore
    except Exception as exc:
        log.debug("agent.credential_pool not importable (%s)", exc)
        return None
    try:
        return load_pool(provider)
    except Exception as exc:
        log.warning("load_pool(%s) failed: %s", provider, type(exc).__name__)
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
    # Only an explicit stored reset counts; no replacement TTL is invented.
    return _to_epoch(getattr(entry, "last_error_reset_at", None))


def _accounts_from_pool(pool) -> List[Account]:
    """Rows from ``pool.entries()`` only (never ``peek``: it can write).

    ``current`` is derived: the first row, in priority order, that is neither dead
    nor benched: what ``peek`` would return on a freshly loaded pool.
    """
    now = time.time()
    out: List[Account] = []
    for idx, e in enumerate(pool.entries(), start=1):
        status = str(getattr(e, "last_status", "") or "ok")
        until = _bench_until(e)
        notes: List[str] = []
        unknown = False
        if status == STATUS_DEAD:
            notes = ["dead — re-login needed"]
            until = None
        elif status == STATUS_EXHAUSTED and until is None:
            unknown = True
            notes = [COOLDOWN_UNKNOWN_NOTE]
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
            notes=notes,
            status=status if status in (STATUS_EXHAUSTED, STATUS_DEAD) else "ok",
            bench_until=until,
            bench_unknown=unknown,
        ))
    first_usable = next((a for a in out if a.status != STATUS_DEAD and not _is_benched(a, now)), None)
    if first_usable is not None:
        first_usable.current = True
    return out


def _read_accounts(provider: str) -> Tuple[str, List[Account]]:
    """Pool rows in priority order: direct pool access, CLI parsing as fallback."""
    pool = _load_pool(provider)  # raises PoolReadError when the pool exists but is unreadable
    if pool is not None:
        try:
            return provider, _accounts_from_pool(pool)
        except Exception as exc:
            raise PoolReadError(f"could not interpret the {provider} credential pool ({type(exc).__name__})") from exc
    # Old core without the read accessor: display-only text fallback (rows are
    # marked non-authoritative; nothing automatic ever acts on them).
    return _parse_list(_run(["auth", "list", provider]))


# -- usage (public helper) ---------------------------------------------------------

def _fetch_supports_identity(fetch) -> bool:
    """True when the public helper accepts ``identity_id`` (older cores do not)."""
    try:
        params = inspect.signature(fetch).parameters
    except (TypeError, ValueError):
        return False
    return "identity_id" in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _usage_for_accounts(accounts: List[Account], provider: str = DEFAULT_PROVIDER) -> Optional[dict]:
    """Best-effort per-account usage for the list view.

    Uses Hermes' public ``fetch_account_usage(read_only=True)``: it never
    refreshes, rotates or benches a pool entry (its only side effect is Hermes'
    own usage cache remembering the snapshot, which is why the entry id is passed
    as ``identity_id`` where supported: the cache then stays per credential).
    Dead logins are skipped (their token is useless); benched ones are still
    read, so their bars show why. An expired idle login simply shows no percentages.
    ``five_hour`` / ``week`` are account-level windows; ``scoped`` is the tightest
    model-family cap and is informational for model-agnostic decisions.
    Returns ``{entry_id: {"five_hour": ..., "week": ..., "scoped": {...}}}`` or None.
    Keys are stable credential entry ids, never display labels: labels can repeat.
    """
    wanted = {a.id for a in accounts
              if a.auth_type == "oauth" and a.status != STATUS_DEAD}
    if not wanted:
        return None
    try:
        from agent.account_usage import fetch_account_usage
    except Exception as exc:
        log.debug("agent.account_usage not importable: %s", exc)
        return None
    try:
        pool = _load_pool(provider)
    except PoolReadError:
        return None
    if pool is None:
        return None
    with_identity = _fetch_supports_identity(fetch_account_usage)
    out: dict = {}
    for entry in pool.entries():
        entry_id = str(getattr(entry, "id", "") or "")
        token = (getattr(entry, "access_token", "") or "").strip()
        if not entry_id or entry_id not in wanted or getattr(entry, "auth_type", "") != "oauth" or not token:
            continue
        try:
            if with_identity:
                snap = fetch_account_usage(provider, api_key=token, read_only=True, identity_id=entry_id)
            else:
                snap = fetch_account_usage(provider, api_key=token, read_only=True)
        except Exception as exc:
            log.debug("usage fetch for entry %s failed: %s", entry_id, type(exc).__name__)
            continue
        if snap is None:
            continue
        windows: dict = {}
        for w in snap.windows:
            name = str(w.label or "").lower()
            pct = w.used_percent
            if getattr(w, "scope", "account") == "model":
                # Model-family caps (Opus / Sonnet week) are scope-specific: shown,
                # never mixed into the account-level five_hour / week windows.
                prev = (windows.get("scoped") or {}).get("used_percent")
                if prev is None or (pct is not None and pct > prev):  # keep the tightest model cap
                    windows["scoped"] = {"label": str(w.label or "model cap"), "used_percent": pct,
                                         "active": bool(pct is not None and pct >= 100)}
            elif "session" in name:
                windows["five_hour"] = {"used_percent": pct}
            elif "week" in name:
                windows["week"] = {"used_percent": pct}
        if windows:
            out[entry_id] = windows
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
                **({"usage": (usage or {}).get(a.id)} if usage else {}),
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


def _find_exact_id(accounts: List[Account], entry_id: str) -> Tuple[Optional[Account], Optional[str]]:
    """Resolve an internal entry id: exact id only, no label, '#n' or prefix matching.

    Fails closed: no match, a duplicated id, or another entry whose *label* equals
    the id (a label-based lookup could then reach the wrong credential) gives
    ``(None, reason)``.
    """
    eid = str(entry_id or "")
    if not eid:
        return None, "no entry id given"
    hits = [a for a in accounts if a.id == eid]
    if not hits:
        return None, f"no pool entry with id {eid} (it may have been removed)"
    if len(hits) > 1:
        return None, f"entry id {eid} is not unique in the pool"
    if any(a.label == eid and a.id != eid for a in accounts):
        return None, f"entry id {eid} collides with another entry's label"
    return hits[0], None


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
    if account.bench_unknown:
        return True  # cooldown not knowable: treat as unavailable, never as expired
    return bool(account.notes)  # CLI fallback: only "(… left)" is known


def pool_status(provider: str = DEFAULT_PROVIDER, with_usage: bool = False) -> dict:
    """Snapshot for status indicators.

    Default: no usage fetch, no token refresh, no network. ``with_usage`` adds
    each login's 5h/week windows (one read-only usage call per usable login).

    ``state``: ``setup`` (no accounts yet), ``ok`` (all usable), ``degraded``
    (some benched/dead, at least one usable), ``exhausted`` (none usable).
    ``active`` is the entry Hermes tries next: the lowest priority that is
    neither dead nor still benched.
    """
    provider_found, accounts = _read_accounts(provider)
    provider = provider_found or provider
    now = time.time()
    usage = _usage_for_accounts(accounts, provider) if (with_usage and provider == DEFAULT_PROVIDER) else None
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
            "id": a.id,
            "priority": a.priority,
            "auth_type": a.auth_type,
            "source": a.source,
            "status": status,
            "active": active is not None and a.id == active.id,
            "bench_until": _iso_utc(a.bench_until) if a.bench_until else None,
            "bench_left": (a.notes[0].strip("()").removesuffix(" left")
                           if status == "benched" and a.notes and not a.bench_unknown else None),
            "cooldown_unknown": bool(status == "benched" and a.bench_unknown),
            # Only a usable account that is not already next: switching onto a
            # benched one changes nothing until its reset.
            "switchable": (status == "ok" and _switch_blocker(a, provider) is None
                           and not (active is not None and a.id == active.id)),
            "relogin_hint": _relogin_hint(provider, a.label, a.source) if status == "dead" else None,
            "usage": (usage or {}).get(a.id) if usage else None,
        })
    return {
        "provider": provider,
        "state": state,
        "strategy": _pool_strategy(provider),
        "strategy_note": _strategy_note(provider, _pool_strategy(provider)),
        "count": len(accounts),
        "usable": len(usable),
        "usage_available": bool(usage),
        "auto": auto_info(),
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
        f"3. Switch by hand: {tick}/switch <label>{tick} or {tick}/switch auto{tick}. Optional automatic rotation by "
        f"real quota is OFF by default; to enable it set {tick}plugins.credential-pool-switcher.auto_rotate: true{tick} "
        f"in config.yaml (then checked at most every {AUTO_INTERVAL_SECONDS // 60} min while Hermes is in use).",
    ]
    for row in status["accounts"]:
        if row["status"] == "dead":
            lines.append(f"✖ {row['label']} is dead — {row['relogin_hint']}")
    if status.get("strategy_note"):
        lines.append(f"⚠ {status['strategy_note']}")
    return "\n".join(lines)


class _MoveDeclined(str):
    """Returned by :func:`_move_to_front` when the pool state used for the move no
    longer allows it: a decline (nothing moved), not an error. The text is the reason."""


def _move_to_front(provider: str, account: Account, *, exact_id: bool = False,
                   require_eligible: bool = False) -> Optional[str]:
    """Move *account* to priority 0. Returns an error string, a :class:`_MoveDeclined`, or None.

    With ``exact_id`` / ``require_eligible`` (auto rotation) only the pool's id-keyed
    ``move_entry`` is used (the CLI fallback resolves by index/label/id and could hit
    another entry), and the very pool object that ``move_entry`` then runs on is
    re-checked first: the id must still be unique, must not collide with a label,
    and (``require_eligible``) must still be alive, not benched and not of unknown
    cooldown. Otherwise nothing is moved.
    """
    strict = exact_id or require_eligible
    pool = _load_writable_pool(provider)
    if pool is not None and hasattr(pool, "move_entry"):
        if strict:
            try:
                fresh, why = _find_exact_id(_accounts_from_pool(pool), account.id)
            except Exception as exc:
                return f"could not re-read the pool state for the move ({type(exc).__name__}); order left unchanged"
            if fresh is None:
                return f"exact entry id lookup failed: {why}"
            if require_eligible:
                reason = _ineligible_reason(fresh, time.time())
                if reason:
                    return _MoveDeclined(reason)
                blocker = _switch_blocker(fresh, provider)
                if blocker:
                    return blocker
        try:
            if pool.move_entry(account.id, 0) is None:
                return f"no pool entry with id {account.id}"
            return None
        except Exception as exc:
            if strict:
                return f"pool.move_entry failed ({type(exc).__name__}); not retrying by label"
            log.warning("pool.move_entry failed (%s); retrying via `hermes auth priority`", exc)
    if strict:
        return "the pool cannot be accessed directly; an exact-id, re-validated move is not possible"
    out = _run(["auth", "priority", provider, account.id, "0"])
    if not re.search(r"^Set \S+ credential ", out, re.M):
        return f"hermes auth priority failed: {out[:200]}"
    return None


def _ineligible_reason(account: Account, now: float) -> Optional[str]:
    """Why Hermes would not use *account* right now (dead / benched), or None."""
    if account.status == STATUS_DEAD:
        return "dead (re-login needed)"
    if account.bench_unknown:
        return "benched with an unknown cooldown"
    if _is_benched(account, now):
        return "benched by Hermes until its reset"
    return None


def switch(
    target: str,
    provider: str = DEFAULT_PROVIDER,
    dry_run: bool = False,
    *,
    require_eligible: bool = False,
    exact_id: bool = False,
) -> dict:
    """Move *target* to priority 0 so the pool tries it first.

    Returns a plan when dry_run is true, otherwise performs the switch.
    A manual switch may still pick a benched login (it only warns). With
    ``require_eligible`` (used by auto rotation) the pool is re-read and a login
    that is dead, benched or of unknown cooldown *now* is declined, not moved:
    ``{"ok": False, "declined": True, "reason": ...}``. A login whose state was
    not read straight from the pool (CLI-text fallback) cannot be proven eligible
    and is declined too.

    With ``exact_id`` (auto rotation and desktop row clicks) *target* is an entry id
    and nothing else: no label, '#n' or prefix matching, no CLI move fallback, and the
    post-move check compares ids only. A vanished or ambiguous id fails closed.
    With ``require_eligible`` the pool object used for the move itself is re-validated
    (see :func:`_move_to_front`), so a state change after the first read declines.
    """
    _, accounts = _read_accounts(provider)
    if not accounts:
        return {"ok": False, "error": f"No credentials found for provider '{provider}'. "
                                      f"Add an account: {_add_hint(provider)}"}
    if exact_id:
        account, why = _find_exact_id(accounts, target)
        if account is None:
            return {"ok": False, "error": f"exact entry id lookup failed: {why}"}
    else:
        account = _find(accounts, target)
    if account is None:
        return {
            "ok": False,
            "error": f"No account matches '{target}'. Use /switch list to see accounts.",
            "known": [a.safe_label for a in accounts],
        }
    if require_eligible:
        reason = (_ineligible_reason(account, time.time()) if account.authoritative
                  else "unverifiable (pool state not read directly; dead or cooldown state unknown)")
        if reason:
            return {"ok": False, "declined": True, "account": account.safe_label, "reason": reason,
                    "error": f"{account.safe_label} is now {reason}; order left unchanged"}
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
    elif account.bench_unknown:
        plan["warning"] = (
            f"{account.safe_label} is benched by Hermes (exhausted) and no reset time is stored, "
            "so its cooldown is unknown; Hermes skips it until the cooldown ends."
        )
    note = _strategy_note(provider, strategy)
    if note:
        plan["strategy_note"] = note
    if dry_run or account.priority == 0:
        plan["already_first"] = account.priority == 0
        return plan

    flags = {k: True for k, v in (("exact_id", exact_id), ("require_eligible", require_eligible)) if v}
    error = _move_to_front(provider, account, **flags)
    if isinstance(error, _MoveDeclined):
        # The pool state that the move would have used is no longer eligible.
        return {"ok": False, "declined": True, "account": account.safe_label, "reason": str(error),
                "error": f"{account.safe_label} is now {error}; order left unchanged"}
    if error:
        return {"ok": False, "error": error}
    _notify_changed(provider)

    # Verification only needs the order — no usage fetch (the usage endpoint 429s).
    try:
        after = list_accounts(provider, with_usage=False)
    except PoolReadError:
        plan["order_after"] = []
        plan["verified_first"] = False
        return plan
    active = next((a for a in after["accounts"] if a["priority"] == 0), None)
    plan["order_after"] = [a["label"] for a in after["accounts"]]
    if exact_id:
        plan["verified_first"] = bool(active and active["id"] == account.id)
    else:
        plan["verified_first"] = bool(active and (active["label"] == account.label or active["id"] == account.id))
    return plan


# -- usage-based auto rotation (read-only) -----------------------------------------

def _exhausted(info: dict) -> bool:
    """Account quota is out when its 5h or week (account-level) window is at 100%.

    A model-family cap (``scoped``) is deliberately not considered: it limits
    one model family, not the account, and auto ranking is model-agnostic.
    """
    for key in ("five_hour", "week"):
        pct = (info.get(key) or {}).get("used_percent")
        if isinstance(pct, (int, float)) and pct >= 100:
            return True
    return False


def _has_account_window(info: dict) -> bool:
    """True when the snapshot carries at least one account-level percentage."""
    return any(isinstance((info.get(k) or {}).get("used_percent"), (int, float))
               for k in ("five_hour", "week"))


def _window_pct(info: dict, key: str) -> float:
    return _pct((info.get(key) or {}).get("used_percent"))


def auto_rotate(provider: str = DEFAULT_PROVIDER, *, dry_run: bool = False) -> dict:
    """Usage-based rotation: move the login with the most headroom to priority 0.

    Reads usage through the public helper and reorders with :func:`switch`
    (the pool's ``move_entry``) with ``require_eligible``: the pool is re-read right
    before the move and a winner that is now dead, benched or of unknown cooldown is
    declined (``report["declined"]``), not moved. It never benches, refreshes or writes tokens,
    and sends no message request: a login Hermes' own pool benched after a 429
    is only skipped here. Ranking uses account-level windows only (5h, week); a
    model-family cap is reported (``scoped_caps``) but does not exclude a login.
    "Has headroom" means usage-eligible, not verified by a message. Logins with
    scoped-only usage data, or exhausted without a stored reset, are never promoted.
    """
    if provider != DEFAULT_PROVIDER:
        return {"ok": False, "error": f"auto rotation supports 'anthropic' (got '{provider}')."}
    _, accounts = _read_accounts(provider)
    if not accounts:
        return {"ok": False, "error": f"No credentials found for provider '{provider}'. "
                                      f"Add an account: {_add_hint(provider)}"}
    usage = _usage_for_accounts(accounts, provider) or {}  # keyed by entry id, never by label
    now = time.time()
    # Display names: the label, plus the pool position when two entries share a label.
    label_counts: dict = {}
    for a in accounts:
        label_counts[a.label] = label_counts.get(a.label, 0) + 1
    name_of = {a.id: (a.label if label_counts[a.label] == 1 else f"{a.label} (#{a.index})") for a in accounts}
    report: dict = {
        "ok": True, "dry_run": dry_run, "provider": provider,
        "alive": [], "exhausted": [], "benched": [], "cooldown_unknown": [], "dead": [],
        "unknown": [], "unknown_reason": {}, "scoped_caps": {}, "relogin": {}, "measured": {},
        "promoted": None, "would_promote": None, "declined": None, "order_after": None,
    }
    candidates: List[Account] = []
    for a in accounts:
        if a.auth_type != "oauth":
            continue  # api keys are not part of rotation
        name = name_of[a.id]
        info = usage.get(a.id)
        if a.status == STATUS_DEAD:
            report["dead"].append(name)
            report["relogin"][name] = _relogin_hint(provider, a.label, a.source)
        elif not a.authoritative:
            # CLI-text fallback: dead / unknown-reset state may have been lost, so
            # eligibility cannot be established. Never a candidate (fail closed).
            report["unknown"].append(name)
            report["unknown_reason"][name] = "pool_unverified"
        elif a.bench_unknown:
            report["cooldown_unknown"].append(name)  # exhausted, no stored reset: never promoted
        elif _is_benched(a, now):
            report["benched"].append(name)
        elif info is None:
            report["unknown"].append(name)
            report["unknown_reason"][name] = "no_usage"
        elif _exhausted(info):
            report["exhausted"].append(name)
        elif not _has_account_window(info):
            # scoped-only (or empty) snapshot: no proof of account headroom
            report["unknown"].append(name)
            report["unknown_reason"][name] = "scoped_only"
        else:
            report["alive"].append(name)
            report["measured"][name] = [short for key, short in (("five_hour", "5h"), ("week", "week"))
                                        if isinstance((info.get(key) or {}).get("used_percent"), (int, float))]
            candidates.append(a)
        scoped = (info or {}).get("scoped")
        if scoped and scoped.get("active") and a.status != STATUS_DEAD:
            report["scoped_caps"][name] = scoped.get("label") or "model cap"

    if candidates:
        def _rank(a: Account):
            return (_window_pct(usage[a.id], "week"), _window_pct(usage[a.id], "five_hour"), a.priority)

        best = min(candidates, key=_rank)
        current = min(candidates, key=lambda a: a.priority)  # what Hermes would use right now
        # Hysteresis: keep the current login unless the winner is clearly better,
        # so two similar logins do not swap places every run (each swap drops the prompt cache).
        if (current is not best and _window_pct(usage[current.id], "week")
                - _window_pct(usage[best.id], "week") < AUTO_SWITCH_MARGIN_PCT):
            best = current
        first_priority = min(a.priority for a in accounts if a.status != STATUS_DEAD)
        if best.priority == first_priority:
            report["promoted"] = name_of[best.id]  # already first
        elif dry_run:
            report["would_promote"] = name_of[best.id]
        else:
            # The usage fetch took time and the pool may have changed since the first
            # read: switch() re-reads it and declines a login that is now dead,
            # benched or of unknown cooldown (a manual switch would only warn).
            # best.id is an entry id: exact-id path (no label/prefix match, no CLI move).
            plan = switch(best.id, provider, require_eligible=True, exact_id=True)
            if plan.get("ok") and plan.get("verified_first") is False:
                report["changed"] = True  # the move ran, but this entry is not first by id
                report["ok"] = False
                report["error"] = "promotion could not be verified by entry id"
            elif plan.get("ok"):
                report["promoted"] = name_of[best.id]
                report["changed"] = True
                try:
                    after = list_accounts(provider, with_usage=False)
                    report["order_after"] = [a["label"] for a in after["accounts"]]
                except PoolReadError:
                    report["order_after"] = None
            elif plan.get("declined"):
                report["declined"] = {"account": name_of[best.id], "reason": plan.get("reason")}
            else:
                report["ok"] = False
                report["error"] = f"promotion failed: {plan.get('error')}"
    return report


# -- automatic rotation (no cron, no click) ------------------------------------------
#
# Every model call passes the ``pre_llm_call`` hook. It starts a background
# auto run at most once per AUTO_INTERVAL_SECONDS — across all Hermes processes
# (gateway, CLI, desktop) through a small state file, so the usage endpoint is
# not polled by several processes at once. OPT-IN: off unless config.yaml has
#   plugins:
#     credential-pool-switcher:
#       auto_rotate: true

AUTO_INTERVAL_SECONDS = 10 * 60
AUTO_SWITCH_MARGIN_PCT = 5.0
_auto_lock = threading.Lock()
_auto_running = False


def _hermes_home() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return value is True


def auto_enabled() -> bool:
    """Automatic rotation is opt-in: ``plugins.credential-pool-switcher.auto_rotate`` in config.yaml."""
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
    except Exception:
        try:
            import yaml
            cfg = yaml.safe_load((_hermes_home() / "config.yaml").read_text()) or {}
        except Exception:
            return False
    try:
        section = (cfg.get("plugins") or {}).get("credential-pool-switcher") or {}
        return _truthy(section.get("auto_rotate"))
    except Exception:
        return False


def _state_path() -> Path:
    return _hermes_home() / "credential-pool-switcher-state.json"


def _read_state() -> dict:
    try:
        data = json.loads(_state_path().read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_state(update: dict) -> None:
    """Merge ``update`` into the state file (timestamps and the last result; never credentials)."""
    try:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({**_read_state(), **update}))
        os.replace(tmp, path)
    except Exception as exc:
        log.debug("state file not written: %s", exc)


def auto_info() -> dict:
    """Cheap description of the automatic mode for status views."""
    state = _read_state()
    last = state.get("last_run")
    return {
        "enabled": auto_enabled(),
        "interval_minutes": AUTO_INTERVAL_SECONDS // 60,
        "last_run": _iso_utc(last) if isinstance(last, (int, float)) else None,
        "last_promoted": state.get("last_promoted"),
        "last_error": state.get("last_error"),
    }


def _auto_worker(provider: str) -> None:
    global _auto_running
    try:
        report = auto_rotate(provider)
        _write_state({
            "last_finished": time.time(),
            "last_promoted": report.get("promoted"),
            "last_error": None if report.get("ok") else report.get("error"),
        })
        if report.get("changed"):
            log.info("auto rotation: %s is now tried first", report.get("promoted"))
    except Exception as exc:  # a failed run must never reach the model call
        log.debug("auto rotation failed: %s", exc)
        _write_state({"last_finished": time.time(), "last_error": f"{type(exc).__name__}"})
    finally:
        with _auto_lock:
            _auto_running = False


def maybe_auto_rotate(provider: str = DEFAULT_PROVIDER, *, background: bool = True,
                      now: Optional[float] = None) -> bool:
    """Start an auto run when one is due. Returns True when a run was started."""
    global _auto_running
    if not auto_enabled():
        return False
    now = time.time() if now is None else now
    with _auto_lock:
        if _auto_running:
            return False
        last = _read_state().get("last_run")
        if isinstance(last, (int, float)) and 0 <= now - last < AUTO_INTERVAL_SECONDS:
            return False
        _write_state({"last_run": now})  # claim the slot for all processes
        _auto_running = True
    if background:
        threading.Thread(target=_auto_worker, args=(provider,), name="cps-auto-rotate", daemon=True).start()
    else:
        _auto_worker(provider)
    return True


def _on_pre_llm_call(**_: Any) -> None:
    try:
        maybe_auto_rotate()
    except Exception:
        pass
    return None


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
                name = scoped.get("label") or "scoped"
                flag = " ⚠ model cap reached (this model family only)" if scoped.get("active") else ""
                parts.append(f"{name} {round(scoped['used_percent'])}%{flag}")
            usage = " · " + " ".join(parts) if parts else ""
        lines.append(
            f"{mark}#{a['index']} {a['label']} — priority {a['priority']}"
            + ("" if dead or benched or a["priority"] != 0 else "  (tried first)")
            + (" · dead — re-login needed" if dead else "")
            + (f" · benched {benched}" if benched and not dead else "")
            + usage
        )
    lines.append("● = current entry marker · priority 0 is tried first · ⛔ = benched (exhausted; "
                 "\"cooldown unknown\" = Hermes stored no reset time) · ✖ = dead (log in anew)"
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


def render_auto(data: dict, markdown: bool = False) -> str:
    b = "**" if markdown else ""
    if not data.get("ok") and not data.get("provider"):
        return f"{b}Auto rotation failed{b}: {data.get('error')}"
    lines = [f"{b}Auto rotation ({data['provider']}){b}" + (" — dry run" if data.get("dry_run") else "")]
    caps = data.get("scoped_caps") or {}
    measured = data.get("measured") or {}
    for label in data["alive"]:
        extra = f" — ⚠ model-scoped cap reached: {caps[label]} (that model family only)" if label in caps else ""
        seen = measured.get(label) or []
        missing = [w for w in ("5h", "week") if w not in seen]
        scope = "/".join(seen) if seen else "account window"
        gap = f"; {'/'.join(missing)} not measured" if missing and seen else ""
        lines.append(f"✓ {label} usage-eligible ({scope} below 100%{gap}; not verified by a message){extra}")
    for label in data["exhausted"]:
        lines.append(f"⛔ {label} account quota exhausted (5h or week at 100%)")
    for label in data["benched"]:
        lines.append(f"⛔ {label} benched by Hermes until its reset")
    for label in data.get("cooldown_unknown", []):
        lines.append(f"? {label} exhausted, cooldown unknown (no reset stored; left untouched)")
    reasons = data.get("unknown_reason") or {}
    for label in data["unknown"]:
        if reasons.get(label) == "scoped_only":
            lines.append(f"? {label} only model-scoped usage seen, no account-level window "
                         "(headroom unproven; left untouched)")
        elif reasons.get(label) == "pool_unverified":
            lines.append(f"? {label} state not read directly from the pool (dead / cooldown state "
                         "unverifiable; left untouched)")
        else:
            lines.append(f"? {label} usage unavailable this round (left untouched)")
    for label, cap in caps.items():
        if label not in data["alive"]:
            lines.append(f"⚠ {label}: model-scoped cap reached: {cap} (that model family only)")
    for label in data["dead"]:
        lines.append(f"✖ {label} dead — log in anew: " + data.get("relogin", {}).get(label, ""))
    declined = data.get("declined")
    if declined:
        lines.append(f"⚠ {declined['account']} looked eligible in the usage snapshot but is now "
                     f"{declined['reason']}; order unchanged this run")
    if data.get("promoted"):
        lines.append(f"● priority 0 → {data['promoted']}")
        if data.get("order_after"):
            lines.append("New order: " + " → ".join(data["order_after"]))
    elif data.get("would_promote"):
        lines.append(f"Would move {data['would_promote']} to priority 0")
    elif data.get("ok") and not data["alive"] and not declined:
        lines.append("No login is usage-eligible right now — order unchanged.")
    if data.get("error"):
        lines.append(f"⚠ {data['error']}")
    return "\n".join(lines)


# -- plugin surfaces ----------------------------------------------------------

@dataclass(frozen=True)
class _ToolCall:
    """One ``account_switch`` call, interpreted once for the approval hook and the handler."""
    action: str
    target: str
    provider: str
    dry_run: bool

    @property
    def mutating(self) -> bool:
        """True when this call reorders the pool (and so needs human approval)."""
        if self.dry_run:
            return False
        if self.action == "auto":
            return True
        return self.action == "switch" and bool(self.target)


def _interpret_tool_args(args: Any) -> _ToolCall:
    """Single source of truth for what an ``account_switch`` call means.

    The schema does not require ``action``: a missing one means ``switch``, which
    mutates as soon as a target is given. ``dry_run`` counts only when it is
    really true (``True`` or a truthy word); any other value, e.g. the string
    ``"false"``, is not a dry run, so an odd value can never skip approval.
    """
    a = args if isinstance(args, dict) else {}
    action = str(a.get("action") or "switch").strip().lower() or "switch"
    target = str(a.get("target") or "").strip()
    provider = str(a.get("provider") or "").strip() or DEFAULT_PROVIDER
    return _ToolCall(action=action, target=target, provider=provider, dry_run=_truthy(a.get("dry_run")))


def _on_pre_tool_call(tool_name: str = "", args: Optional[dict] = None, **_: Any) -> Optional[dict]:
    """Escalate model-initiated real switches to the human-approval gate.

    Reordering the credential pool changes which account gets billed first,
    so a ``switch`` or ``auto`` issued by the model must not run silently — a
    prompt-injected turn could otherwise redirect spend. The call is read with the
    same :func:`_interpret_tool_args` as the handler, so an omitted action with a
    target (which the handler treats as ``switch``) is gated too. Read-only calls
    (``list``, no-target ``switch``, ``plan``, ``dry_run``) pass through untouched.
    Never raises: a broken gate must not break other tools (for ``account_switch``
    itself it fails closed and asks for approval).
    """
    if tool_name != "account_switch":
        return None
    try:
        call = _interpret_tool_args(args)
        if not call.mutating:
            return None
        if call.action == "auto":
            return {
                "action": "approve",
                "message": (
                    "account_switch: auto-rotate the "
                    f"{call.provider} credential pool "
                    "(promotes the login with the most quota headroom to priority 0 — "
                    "changes which account is tried/billed first)."
                ),
            }
        return {
            "action": "approve",
            "message": (
                "account_switch: move "
                f"'{call.target}' to priority 0 of the "
                f"{call.provider} credential pool "
                "(changes which account is tried/billed first)."
            ),
        }
    except Exception:
        return {"action": "approve",
                "message": "account_switch: could not classify this call as read-only; approve to run it."}


def _tool_account_switch(args: dict, **_: Any) -> str:
    """Tool: list pooled accounts, switch the active one, plan, or auto-rotate.

    Hermes dispatches plugin handlers as ``handler(args, **kwargs)`` — the
    whole argument dict arrives positionally, so take the dict and read the
    fields out of it. Returns a JSON string (the tool result contract). The call
    is interpreted by :func:`_interpret_tool_args`, shared with the approval hook.
    """
    call = _interpret_tool_args(args)
    try:
        return json.dumps(_dispatch_tool_call(call), default=str)
    except PoolReadError as exc:
        return json.dumps({"ok": False, "error": f"{exc}; nothing was changed"})


def _dispatch_tool_call(call: _ToolCall) -> dict:
    action, target, provider, dry_run = call.action, call.target, call.provider, call.dry_run
    result: dict
    if action == "list" or (action == "switch" and not target):
        result = list_accounts(provider)
    elif action == "plan":
        if not target:
            result = {"ok": False, "error": "plan needs a target (#index, label, id, or unique prefix)."}
        else:
            result = switch(target, provider, dry_run=True)
    elif action == "switch":
        result = switch(target, provider, dry_run=dry_run)
    elif action == "auto":
        result = auto_rotate(provider, dry_run=dry_run)
    else:
        result = {"ok": False, "error": f"Unknown action '{action}'. Use list, switch, plan, or auto."}
    return result


def _setup_argparse(p) -> None:
    sub = p.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list", help="show pooled accounts and priorities")
    ls.add_argument("--provider", default=DEFAULT_PROVIDER)
    sw = sub.add_parser("switch", help="move an account to priority 0")
    sw.add_argument("target", help="#index, exact label, entry id, or unique label prefix")
    sw.add_argument("--provider", default=DEFAULT_PROVIDER)
    sw.add_argument("--dry-run", action="store_true")
    sw.add_argument("--exact-id", action="store_true",
                    help="treat target as an entry id only (no label / #n / prefix matching); used by the desktop rows")
    pl = sub.add_parser("plan", help="show what a switch would do")
    pl.add_argument("target")
    pl.add_argument("--provider", default=DEFAULT_PROVIDER)
    pl.add_argument("--exact-id", action="store_true", help="treat target as an entry id only")
    st = sub.add_parser("status", help="pool snapshot for status indicators (no network unless --usage)")
    st.add_argument("--provider", default=DEFAULT_PROVIDER)
    st.add_argument("--json", action="store_true", help=f"one '{STATUS_LINE_PREFIX.strip()} <json>' line")
    st.add_argument("--usage", action="store_true", help="add each login's 5h/week usage (one read-only call per login)")
    au = sub.add_parser("auto", help="move the login with the most usage headroom to priority 0")
    au.add_argument("--provider", default=DEFAULT_PROVIDER)
    au.add_argument("--dry-run", action="store_true")
    se = sub.add_parser("setup", help="how to add accounts and what to do next")
    se.add_argument("--provider", default=DEFAULT_PROVIDER)


def _arg(args, name: str, default):
    val = getattr(args, name, None)
    return default if val is None else val


def _handle_cli(args) -> None:
    try:
        _handle_cli_cmd(args)
    except PoolReadError as exc:
        print(f"Credential pool not read: {exc}; nothing was changed")


def _handle_cli_cmd(args) -> None:
    cmd = _arg(args, "cmd", "list")
    if cmd == "list":
        print(render_list(list_accounts(_arg(args, "provider", DEFAULT_PROVIDER))))
    elif cmd == "plan":
        print(render_switch(switch(args.target, _arg(args, "provider", DEFAULT_PROVIDER), dry_run=True,
                                   exact_id=bool(_arg(args, "exact_id", False)))))
    elif cmd == "switch":
        print(render_switch(switch(args.target, _arg(args, "provider", DEFAULT_PROVIDER),
                                   dry_run=_arg(args, "dry_run", False),
                                   exact_id=bool(_arg(args, "exact_id", False)))))
    elif cmd == "auto":
        print(render_auto(auto_rotate(_arg(args, "provider", DEFAULT_PROVIDER),
                                      dry_run=_arg(args, "dry_run", False))))
    elif cmd == "status":
        status = pool_status(_arg(args, "provider", DEFAULT_PROVIDER), with_usage=_arg(args, "usage", False))
        if _arg(args, "json", False):
            print(STATUS_LINE_PREFIX + json.dumps(status, default=str))
        else:
            active = status.get("active")
            print(f"{status['provider']}: {status['state']} · {status['usable']}/{status['count']} usable"
                  + (f" · next: {active['label']}" if active else ""))
    elif cmd == "setup":
        print(render_setup(pool_status(_arg(args, "provider", DEFAULT_PROVIDER))))


_SLASH_USAGE = (
    "Usage: /switch [list] · /switch setup · /switch auto [--dry-run] · "
    "/switch <label> · /switch plan <label> · --provider=<id>\n"
    "Accounts live in Hermes' credential pool (~/.hermes/auth.json). Add one: "
    "hermes auth add anthropic --type oauth --label <name>"
)


def _slash(arg: str = "", **_: Any) -> str:
    try:
        return _slash_impl(arg)
    except PoolReadError as exc:
        return f"Credential pool not read: {exc}; nothing was changed"


def _slash_impl(arg: str = "") -> str:
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
    if parts[0] == "auto":
        return render_auto(auto_rotate(provider, dry_run="--dry-run" in parts), markdown=True)
    if parts[0] == "plan":
        if len(parts) < 2:
            return _SLASH_USAGE
        return render_switch(switch(parts[1], provider, dry_run=True), markdown=True)
    return render_switch(switch(parts[0], provider), markdown=True)


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
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
                        "enum": ["list", "switch", "plan", "auto"],
                        "description": "list = show all pooled accounts; switch = move target to priority 0; plan = dry-run preview; auto = promote the login with the most usage headroom",
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
            "previews the switch; action=auto promotes the login with the most quota "
            "headroom (also escalated; the CLI runs it unattended). This tool never creates "
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
        description="Switch pooled accounts (/switch list, /switch setup, /switch auto, /switch <label>)",
        args_hint="[list|setup|auto [--dry-run]|<label>|plan <label>] [--provider=<id>]",
    )
