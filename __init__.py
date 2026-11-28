"""credential-pool-switcher — switch between pooled provider accounts.

Surfaces:

* tool ``account_switch``  — the agent can list/plan; a real switch or an
  auto rotation is escalated to the human-approval gate by a
  ``pre_tool_call`` hook
* slash ``/switch``        — same thing from any chat
* CLI ``hermes credential-pool-switcher list|switch|plan|auto``

How it touches Hermes:

* **Listing / switching** read the pool through ``agent.credential_pool``
  (``load_pool`` → ``entries()`` / ``peek()`` / ``move_entry()``) — the same
  calls ``hermes auth list`` / ``hermes auth priority`` make. When that
  module is not importable it falls back to running those two CLI commands
  and parsing their output. Switching = "move to priority 0", i.e. that
  account is tried first on the next model call (``fill_first`` strategy).
* **Usage percentages** (list + auto) read each pooled OAuth token
  in-process and send it to the provider's own API (api.anthropic.com,
  ``/api/oauth/usage``). Expiring tokens are refreshed through the pool's own
  refresh path first so the rotated single-use refresh token is persisted.
* **Auto rotation** additionally writes bench state (``last_status`` /
  ``last_error_*``) for exhausted logins into auth.json via
  ``hermes_cli.auth.write_credential_pool`` — under the auth-store lock and
  with token-generation bases, so a concurrent token refresh is never
  overwritten. Token fields are never modified.

Tokens are never printed, logged, returned, or written by this plugin.
Some of the helpers used are private Hermes APIs; failures to import or call
them are logged (logger ``credential_pool_switcher``) instead of swallowed.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
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


def _pct(value: Any, default: float = 999.0) -> float:
    """Utilization for sorting: a real 0 stays 0 — only a missing value sorts last."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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
    try:
        from agent.credential_pool import _exhausted_until  # type: ignore  # noqa: SLF001
        return _exhausted_until(entry)
    except Exception as exc:
        log.debug("_exhausted_until unavailable (%s); using last_error_reset_at", exc)
        return _to_epoch(getattr(entry, "last_error_reset_at", None))


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


# -- tokens & usage ----------------------------------------------------------------

def _refresh_if_expiring(pool, entry):
    """Pool-owned OAuth refresh for an expiring/expired entry; the entry itself on any failure."""
    try:
        if pool._entry_needs_refresh(entry):  # noqa: SLF001 - same helper the pool uses on select()
            return pool._refresh_entry(entry, force=False) or entry  # noqa: SLF001
    except AttributeError as exc:
        log.warning("pool refresh API changed (%s); usage may 401 for idle logins", exc)
    except Exception as exc:
        log.debug("refresh for %s failed: %s", getattr(entry, "label", "?"), exc)
    return entry


def _current_bench_resets(provider: str) -> dict:
    """{label: reset_at_iso} for rows currently benched (exhausted) with a future reset.

    Lets list/auto skip the usage endpoint entirely for logins whose bench
    has not expired yet — /api/oauth/usage rate-limits aggressively (429)
    when polled every cycle for every account.
    """
    try:
        from hermes_cli.auth import read_credential_pool
    except Exception as exc:
        log.debug("hermes_cli.auth not importable: %s", exc)
        return {}
    now = time.time()
    out: dict = {}
    try:
        for row in read_credential_pool(provider):
            if not isinstance(row, dict) or row.get("last_status") != STATUS_EXHAUSTED:
                continue
            reset = _to_epoch(row.get("last_error_reset_at"))
            if reset is not None and reset > now:
                out[str(row.get("label") or row.get("id") or "")] = _iso_utc(reset)
    except Exception as exc:
        log.warning("reading bench state for %s failed: %s", provider, exc)
        return {}
    return out


def _oauth_tokens(
    provider: str,
    *,
    manual_only: bool,
    labels: Optional[set] = None,
    cached_benches: Optional[dict] = None,
) -> Tuple[dict, List[str]]:
    """``({label: access_token}, [dead labels])`` for the pool's OAuth rows.

    Dead rows (refresh token already rejected) are reported, never refreshed —
    re-POSTing a rejected refresh token only produces another invalid_grant.
    Rows that are not benched get an expiring token refreshed through the pool
    first: a benched login is never used, so Hermes never refreshes it, and by
    the time its bench expires the access token is long dead (usage 401s).
    """
    pool = _load_pool(provider)
    if pool is None:
        return {}, []
    benched = set(cached_benches if cached_benches is not None else _current_bench_resets(provider))
    tokens: dict = {}
    dead: List[str] = []
    try:
        entries = pool.entries()
    except Exception as exc:
        log.warning("pool.entries() failed: %s", exc)
        return {}, []
    for e in entries:
        if getattr(e, "auth_type", "") != "oauth":
            continue
        source = str(getattr(e, "source", "") or "")
        if manual_only and not source.startswith("manual"):
            continue
        label = str(getattr(e, "label", "") or e.id)
        if labels is not None and label not in labels:
            continue
        if getattr(e, "last_status", None) == STATUS_DEAD:
            dead.append(label)
            continue
        if label not in benched:
            e = _refresh_if_expiring(pool, e)
            if getattr(e, "last_status", None) == STATUS_DEAD:
                dead.append(label)
                continue
        token = (getattr(e, "access_token", "") or "").strip()
        if token:
            tokens[label] = token
    return tokens, dead


def _usage_limits(tokens: dict, cached_benches: Optional[dict] = None) -> dict:
    """Per-login verdict from Anthropic's own usage endpoint.

    ``{label: {"dead_until": iso|None, "active_limits": [...], "week_pct": float,
    "session_pct": float, "scoped_pct": float, "scoped_active": bool,
    "cached": bool}}``. A login is dead when any ``limits[]`` entry is active
    at >=100% / severity critical — the endpoint's own words, no probe needed.
    ``dead_until`` is the LATEST active limit's reset. Tokens stay in-process.
    """
    try:
        from agent.account_usage import _get_json  # noqa: SLF001
    except Exception as exc:
        log.warning("agent.account_usage._get_json not importable: %s", exc)
        return {label: {"error": "agent.account_usage not importable"} for label in tokens}
    cached = cached_benches or {}
    out: dict = {}
    for label, token in tokens.items():
        if label in cached:
            # Still benched with a future reset: no network call.
            out[label] = {
                "dead_until": cached[label],
                "active_limits": ["cached bench"],
                "week_pct": None,
                "session_pct": None,
                "scoped_pct": None,
                "scoped_active": True,
                "cached": True,
            }
            continue
        try:
            payload = _get_json(
                "https://api.anthropic.com/api/oauth/usage",
                {"Authorization": f"Bearer {token}",
                 "anthropic-beta": "oauth-2025-04-20", "User-Agent": "claude-code/2.1.0"},
                timeout=15.0,
            )
        except Exception as exc:
            out[label] = {"error": f"usage fetch failed: {type(exc).__name__}"}
            continue
        dead_until: Optional[str] = None
        dead_epoch: Optional[float] = None
        active: list = []
        scoped_pct = None
        scoped_active = False
        for lim in payload.get("limits") or []:
            if not isinstance(lim, dict) or not lim.get("is_active"):
                if isinstance(lim, dict) and str(lim.get("kind") or "") == "weekly_scoped":
                    scoped_pct = lim.get("percent")
                    scoped_active = False
                continue
            pct = lim.get("percent")
            critical = str(lim.get("severity") or "").lower() == "critical"
            if str(lim.get("kind") or "") == "weekly_scoped":
                scoped_pct = pct
                scoped_active = True
            if (isinstance(pct, (int, float)) and pct >= 100) or critical:
                active.append(f"{lim.get('kind')}={pct}%")
                reset = lim.get("resets_at")
                reset_epoch = _to_epoch(reset)
                if reset_epoch is not None and (dead_epoch is None or reset_epoch > dead_epoch):
                    dead_epoch, dead_until = reset_epoch, str(reset)
        five = payload.get("five_hour") or {}
        seven = payload.get("seven_day") or {}
        out[label] = {
            "dead_until": dead_until,
            "active_limits": active,
            "week_pct": seven.get("utilization"),
            "session_pct": five.get("utilization"),
            "scoped_pct": scoped_pct,
            "scoped_active": scoped_active,
            "cached": False,
        }
    return out


def _usage_for_accounts(accounts: List[Account], provider: str = DEFAULT_PROVIDER) -> Optional[dict]:
    """Best-effort per-account usage for the list view.

    Same token path as auto rotation: dead rows skipped, benched rows answered
    from their stored bench (no fetch), expiring tokens refreshed first.
    Returns ``{label: {"five_hour": ..., "week": ..., "scoped": {...}}}`` or None.
    """
    labels = {a.label for a in accounts if a.auth_type == "oauth" and a.status != STATUS_DEAD}
    if not labels:
        return None
    cached = _current_bench_resets(provider)
    tokens, _ = _oauth_tokens(provider, manual_only=False, labels=labels, cached_benches=cached)
    tokens = {k: v for k, v in tokens.items() if k not in cached}
    if not tokens:
        return None
    out: dict = {}
    for label, info in _usage_limits(tokens).items():
        if info.get("error"):
            continue
        out[label] = {
            "five_hour": {"used_percent": info.get("session_pct")},
            "week": {"used_percent": info.get("week_pct")},
            "scoped": {"used_percent": info.get("scoped_pct"), "active": info.get("scoped_active")},
        }
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
        f"3. Pick one by hand: {tick}/switch <label>{tick}; or rotate by real quota: {tick}/switch auto{tick}"
        f" (unattended: run {tick}hermes {PLUGIN_ID} auto{tick} from cron every 30 min).",
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


# -- utilization-based auto rotation ------------------------------------------

AUTO_BENCH_TAG = "auto-rotation"
_PROBE_MODEL_DEFAULT = "claude-haiku-5-5"
# Tried in order when the API does not know a probe model (404 / model error),
# so a renamed model id degrades to the next one instead of a silent no-op probe.
_PROBE_MODEL_FALLBACKS = ("claude-haiku-4-5",)
_PROBE_TIMEOUT_SECONDS = 20
# A masked 429 body (plain {"error": "Error"}) carries no reset date: bench for
# an hour so the next auto run re-probes instead of hammering the login.
_PROBE_BENCH_FALLBACK_SECONDS = 60 * 60
_RE_GAIN_DATE = re.compile(
    r"regain access on (\d{4}-\d{2}-\d{2})(?: at (\d{1,2}:\d{2}))?"
    r"(?:\s*\(?\s*(UTC|GMT|Z|[+-]\d{2}:?\d{2})\b\)?)?",
    re.I,
)


def _probe_reset_at(status: int, body: str) -> Optional[str]:
    """ISO reset timestamp for a dead-probe bench, or None for non-benching statuses.

    "regain access on <date> at <HH:MM>" with an explicit zone is taken as is.
    Without one the zone is ambiguous: take the EARLIER of the UTC and the
    local reading — unbenching too early costs one re-probe (which re-benches),
    too late keeps a working login idle for hours.
    """
    import datetime

    if status not in (429, 402):
        return None
    now = time.time()
    m = _RE_GAIN_DATE.search(body or "")
    if m:
        day, clock, zone = m.group(1), m.group(2) or "00:00", (m.group(3) or "").upper()
        try:
            naive = datetime.datetime.strptime(f"{day} {clock}", "%Y-%m-%d %H:%M")
        except ValueError:
            naive = None
        if naive is not None:
            if zone in ("UTC", "GMT", "Z"):
                epoch = naive.replace(tzinfo=datetime.timezone.utc).timestamp()
            elif zone:
                sign = 1 if zone[0] == "+" else -1
                digits = zone[1:].replace(":", "")
                offset = datetime.timedelta(hours=int(digits[:2]), minutes=int(digits[2:4]))
                epoch = naive.replace(tzinfo=datetime.timezone(sign * offset)).timestamp()
            else:
                as_utc = naive.replace(tzinfo=datetime.timezone.utc).timestamp()
                as_local = naive.timestamp()  # naive → local time
                epoch = min(as_utc, as_local)
            if epoch > now:
                return _iso_utc(epoch)
    return _iso_utc(now + _PROBE_BENCH_FALLBACK_SECONDS)


# Claude Code identity the Hermes core itself sends for OAuth logins
# (agent/anthropic_adapter.py). Anthropic answers an OAuth request that lacks
# it with a MASKED 429 {"type": "rate_limit_error", "message": "Error"} for
# every 5.x model, even on a completely free login — so the probe has to look
# exactly like a real Hermes request: system prefix, "(external, cli)" UA with
# the current Claude Code version, x-app: cli. Duplicated here on purpose (no
# private core imports); keep in sync with the core.
_CLAUDE_CODE_SYSTEM_PREFIX = "You are Claude Code, Anthropic's official CLI for Claude."
_CLAUDE_CODE_VERSION_FALLBACK = "2.1.74"
_PROBE_BETAS = "claude-code-20250219,oauth-2025-04-20"
_claude_code_version_cache: Optional[str] = None


def _claude_code_version() -> str:
    """Installed Claude Code version (``claude --version``), else a static fallback.

    Anthropic rejects OAuth requests whose UA version lags the real release too
    far, so the installed CLI's version is preferred. Best effort, cached for
    the process; the fallback is the version the core ships as its own fallback.
    """
    global _claude_code_version_cache
    if _claude_code_version_cache is not None:
        return _claude_code_version_cache
    import shutil
    import subprocess

    version = _CLAUDE_CODE_VERSION_FALLBACK
    candidates: list = []
    for name in ("claude", "claude-code"):
        hit = shutil.which(name)
        if hit:
            candidates.append(hit)
    # GUI/LaunchAgent PATHs lack the usual install prefixes (same list the core probes).
    for prefix in ("~/.local/bin", "~/.claude/local", "~/bin", "~/.npm-global/bin", "~/.bun/bin",
                   "~/.volta/bin", "/opt/homebrew/bin", "/usr/local/bin"):
        for name in ("claude", "claude-code"):
            path = os.path.join(os.path.expanduser(prefix), name)
            if os.path.isfile(path) and path not in candidates:
                candidates.append(path)
    for cmd in candidates:
        try:
            out = subprocess.run([cmd, "--version"], stdin=subprocess.DEVNULL, capture_output=True,
                                 text=True, encoding="utf-8", errors="replace", timeout=5, check=False)
        except Exception:
            continue
        first = (out.stdout or "").strip().split()[:1]
        if out.returncode == 0 and first and first[0][:1].isdigit():
            version = first[0]
            break
    _claude_code_version_cache = version
    return version


def _probe_request_parts(token: str, model: str) -> tuple:
    """``(headers, body)`` of the probe — the same identity the core uses."""
    headers = {
        "authorization": f"Bearer {token}",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": _PROBE_BETAS,
        "user-agent": f"claude-code/{_claude_code_version()} (external, cli)",
        "x-app": "cli",
        "content-type": "application/json",
    }
    body = {
        "model": model, "max_tokens": 4,
        "system": [{"type": "text", "text": _CLAUDE_CODE_SYSTEM_PREFIX}],
        "messages": [{"role": "user", "content": "ping"}],
    }
    return headers, body


def _is_masked_429(status: int, body: str) -> bool:
    """A 429 whose body only says "Error": no limit name, no reset date.

    That is what Anthropic returns for a request it does not accept as a
    Claude Code request — not evidence that the login is exhausted.
    """
    if status != 429 or _RE_GAIN_DATE.search(body or ""):
        return False
    text = (body or "").strip()
    if not text:
        return True
    try:
        data = json.loads(text)
    except ValueError:
        return text.strip(" \"'.").lower() == "error"
    if isinstance(data, dict):
        err = data.get("error")
        msg = data.get("message")
        if isinstance(err, dict):
            msg = err.get("message", msg)
        elif isinstance(err, str) and msg is None:
            msg = err
        return isinstance(msg, str) and msg.strip().lower() in ("error", "")
    return False


def _probe_once(label: str, token: str, model: str) -> dict:
    import urllib.error
    import urllib.request

    headers, payload = _probe_request_parts(token, model)
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode(),
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT_SECONDS) as resp:
            return {"label": label, "alive": resp.status == 200, "status": resp.status, "model": model}
    except urllib.error.HTTPError as ex:
        body = ex.read().decode("utf-8", "replace")
        return {
            "label": label,
            "alive": False if ex.code in (429, 402) else None,
            "status": ex.code,
            "model": model,
            "masked": _is_masked_429(ex.code, body),
            "reset_at": _probe_reset_at(ex.code, body),
            "note": body[:160],
        }
    except Exception as exc:  # network/timeout: transient, never bench
        return {"label": label, "alive": None, "status": None, "model": model,
                "note": f"{type(exc).__name__}: {exc}"[:160]}


def _is_model_error(result: dict) -> bool:
    status = result.get("status")
    note = str(result.get("note") or "").lower()
    return status == 404 or (status == 400 and "model" in note)


def _probe_anthropic_login(label: str, token: str, model: str) -> dict:
    """One 4-token /v1/messages probe: can this login serve requests right now?

    ``alive`` is True/False; ``None`` means inconclusive (network/5xx/unknown
    model) — the login must NOT be benched on that. The token stays
    in-process, never returned.
    """
    models = [model, *[m for m in _PROBE_MODEL_FALLBACKS if m != model]]
    result: dict = {}
    for m in models:
        result = _probe_once(label, token, m)
        if not _is_model_error(result):
            return result
        log.warning("probe model %s rejected (HTTP %s); trying the next one", m, result.get("status"))
    result["alive"] = None
    return result


def _write_auto_status(provider: str, benched: dict, alive_labels: list) -> tuple:
    """Bench/clear pool rows in auth.json. Returns ``(error, cleared_labels)``.

    Mirrors what the core's own exhaustion path would persist: for each dead
    login ``last_status=exhausted`` with the reset timestamp. An ALIVE login
    still carrying an exhaustion bench is cleared. Read and write happen under
    the auth-store lock and pass the read token generation as ``token_bases``,
    so a token refreshed concurrently by another Hermes process is kept, never
    reverted to the stale one (Anthropic refresh tokens are single-use).
    """
    try:
        from hermes_cli.auth import read_credential_pool, write_credential_pool
    except Exception as exc:
        return f"cannot import auth store helpers: {exc}", []
    try:
        from hermes_cli.auth import _auth_store_lock  # noqa: SLF001
        lock = _auth_store_lock(timeout_seconds=45)
    except Exception as exc:
        log.warning("auth-store lock unavailable (%s); relying on token_bases only", exc)
        lock = contextlib.nullcontext()

    cleared: list = []
    try:
        with lock:
            # Annotated as dict upstream, but a provider slice is a list of rows.
            rows: List[Any] = list(read_credential_pool(provider) or [])  # type: ignore[arg-type]
            bases = {
                str(row["id"]): (row.get("access_token"), row.get("refresh_token"))
                for row in rows if isinstance(row, dict) and row.get("id")
            }
            now = time.time()
            alive = set(alive_labels)
            cleared_ids: list = []
            changed = False
            for row in rows:
                if not isinstance(row, dict):
                    continue
                label = str(row.get("label") or "")
                if row.get("last_status") == STATUS_DEAD:
                    continue  # terminal verdict owned by the core; never overwrite
                if label in benched:
                    row["last_status"] = STATUS_EXHAUSTED
                    row["last_status_at"] = now
                    row["last_error_code"] = benched[label].get("code", 429)
                    row["last_error_reason"] = "rate_limit_error"
                    row["last_error_message"] = f"[{AUTO_BENCH_TAG}] {benched[label].get('note', '')}"[:400]
                    row["last_error_reset_at"] = benched[label]["reset_at"]
                    changed = True
                elif label in alive and row.get("last_status") == STATUS_EXHAUSTED:
                    for key in ("last_status_at", "last_error_code", "last_error_reason",
                                "last_error_message", "last_error_reset_at"):
                        row[key] = None
                    row["last_status"] = "ok"
                    cleared.append(label)
                    if row.get("id"):
                        cleared_ids.append(str(row["id"]))
                    changed = True
            if not changed:
                return None, cleared
            try:
                write_credential_pool(provider, rows, status_cleared_ids=cleared_ids or None,
                                      token_bases=bases)
            except TypeError:
                # Older core without token_bases: still safe under the lock above.
                write_credential_pool(provider, rows, status_cleared_ids=cleared_ids or None)
    except Exception as exc:
        return f"write_credential_pool failed: {exc}", cleared
    return None, cleared


def auto_rotate(
    provider: str = DEFAULT_PROVIDER,
    *,
    probe: bool = True,
    dry_run: bool = False,
    model: str = _PROBE_MODEL_DEFAULT,
) -> dict:
    """Usage-aware rotation: bench dead logins until their reset, promote the
    account with the most weekly headroom to priority 0.

    This is the reactive complement to Hermes' pool rotation: when the core
    cannot attribute a 429 to a pool entry (rotated OAuth token), accounts are
    never benched and every call burns retries on a dead login. The probe is
    the ground truth; usage windows pick the healthiest survivor.
    """
    if provider != DEFAULT_PROVIDER:
        return {"ok": False, "error": f"auto rotation supports 'anthropic' (got '{provider}')."}
    cached = _current_bench_resets(provider)
    tokens, needs_login = _oauth_tokens(provider, manual_only=True, cached_benches=cached)
    if not tokens and not needs_login:
        return {"ok": False, "error": (
            "no OAuth logins added via `hermes auth add` found — auto rotation only manages those. "
            f"Add each Claude account: {_add_hint(provider)}")}
    accounts = list_accounts(provider, with_usage=False)
    managed = [a for a in accounts["accounts"] if a["label"] in tokens and a["auth_type"] == "oauth"]
    if not managed and not needs_login:
        return {"ok": False, "error": "pool rows and auth list disagree; nothing to manage."}

    # Ground truth #1: Anthropic's usage endpoint (limits[] with is_active).
    # Logins with a future-dated bench are answered from the stored bench.
    usage = _usage_limits(tokens, cached_benches=cached)
    dead: list = []          # (account, {"reset_at", "why", "cached"})
    alive: list = []         # usage says alive
    unknown: list = []       # usage fetch failed → untouched this round
    for a in managed:
        info = usage.get(a["label"]) or {}
        if info.get("error"):
            unknown.append(a)
            continue
        if info.get("dead_until"):
            dead.append((a, {"reset_at": info["dead_until"],
                             "why": ", ".join(info["active_limits"]) or "active limit",
                             "cached": bool(info.get("cached"))}))
        else:
            alive.append(a)

    # Ground truth #2 (probe, optional): usage can look healthy while an org
    # cap still blocks requests — confirm usage-alive logins with one 4-token
    # call before trusting them with priority 0.
    probes: dict = {}
    inconclusive: dict = {}
    if probe:
        for a in alive:
            p = _probe_anthropic_login(a["label"], tokens[a["label"]], model)
            probes[a["label"]] = p
            info = usage.get(a["label"]) or {}
            usage_free = (
                not info.get("dead_until")
                and not info.get("error")
                and _pct(info.get("week_pct"), 0.0) < 100
                and _pct(info.get("session_pct"), 0.0) < 100
            )
            if p.get("masked") and usage_free:
                # A bare 429 "Error" with the usage endpoint under 100 % is
                # Anthropic's masked reject, not a limit: never bench on it.
                inconclusive[a["label"]] = "masked HTTP 429 (usage endpoint shows headroom)"
            elif p.get("alive") is False and p.get("reset_at"):
                dead.append((a, {"reset_at": p["reset_at"], "why": f"probe HTTP {p.get('status')}",
                                 "cached": False}))
            elif p.get("alive") is None:
                inconclusive[a["label"]] = f"HTTP {p.get('status')}" if p.get("status") else str(p.get("note") or "")[:80]
    alive = [a for a in alive if a["label"] not in {d[0]["label"] for d in dead}]

    def _week_pct(a: dict) -> float:
        return _pct((usage.get(a["label"]) or {}).get("week_pct"))

    def _session_pct(a: dict) -> float:
        return _pct((usage.get(a["label"]) or {}).get("session_pct"))

    # Cached benches are already on disk: re-writing them every cycle would
    # only replace the original reason with "cached bench".
    to_bench = {a["label"]: {"reset_at": w["reset_at"], "code": 429, "note": w["why"]}
                for a, w in dead if not w["cached"]}
    report = {
        "ok": True,
        "dry_run": dry_run,
        "provider": provider,
        "strategy": accounts.get("strategy", ""),
        "probed": len(probes),
        "alive": [a["label"] for a in alive],
        "unknown": [a["label"] for a in unknown],
        "needs_login": list(needs_login),
        "probe_inconclusive": inconclusive,
        "dead": {a["label"]: w["reset_at"] for a, w in dead},
        "dead_why": {a["label"]: w["why"] for a, w in dead},
        "benched": {k: v["reset_at"] for k, v in to_bench.items()},
        "cleared": [],
        "promoted": None,
        "all_dead": not alive and not unknown,
    }
    if dry_run:
        return report

    error, cleared = _write_auto_status(provider, to_bench, alive_labels=[a["label"] for a in alive])
    report["cleared"] = cleared
    if error:
        report["ok"] = False
        report["error"] = error
        return report
    if to_bench or cleared:
        _notify_changed(provider)

    if alive:
        best = sorted(alive, key=lambda a: (_week_pct(a), _session_pct(a), a["priority"]))[0]
        if best["priority"] != 0:
            plan = switch(best["label"], provider)
            report["promoted"] = best["label"] if plan.get("ok") else None
            if not plan.get("ok"):
                report["error"] = f"promotion failed: {plan.get('error')}"
        else:
            report["promoted"] = best["label"]
        after = list_accounts(provider, with_usage=False)
        report["order_after"] = [a["label"] for a in after["accounts"]]
    return report


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


def render_auto(data: dict, markdown: bool = False) -> str:
    b = "**" if markdown else ""
    if not data.get("ok") and not data.get("provider"):
        return f"{b}Auto rotation failed{b}: {data.get('error')}"
    lines = [f"{b}Auto rotation ({data['provider']}){b}" + (" — dry run" if data.get("dry_run") else "")]
    inconclusive = data.get("probe_inconclusive") or {}
    for label in data.get("alive", []):
        extra = f" (probe inconclusive: {inconclusive[label]})" if label in inconclusive else ""
        lines.append(f"✓ {label} alive{extra}")
    for label, reset in (data.get("dead") or {}).items():
        until = reset if isinstance(reset, str) else "unknown reset"
        why = (data.get("dead_why") or {}).get(label, "")
        benched = " (benched)" if label in (data.get("benched") or {}) else ""
        suffix = f" [{why}]" if why else ""
        lines.append(f"✗ {label} dead until {until}{benched}{suffix}")
    for label in data.get("cleared", []):
        lines.append(f"↺ {label} bench lifted (probe alive)")
    for label in data.get("unknown", []):
        lines.append(f"? {label} usage unavailable this round (left untouched)")
    for label in data.get("needs_login", []):
        lines.append(f"✖ {label} dead — refresh token rejected, log in anew: "
                     + _relogin_hint(data["provider"], label, "manual"))
    if data.get("all_dead"):
        lines.append("All managed logins exhausted — provider fails over elsewhere.")
    elif data.get("promoted"):
        lines.append(f"● priority 0 → {data['promoted']}")
    note = _strategy_note(data["provider"], data.get("strategy") or "")
    if note:
        lines.append(f"⚠ {note}")
    if data.get("error"):
        lines.append(f"⚠ {data['error']}")
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
        if args.get("action") == "auto":
            return {
                "action": "approve",
                "message": (
                    "account_switch: auto-rotate the "
                    f"{args.get('provider') or DEFAULT_PROVIDER} credential pool "
                    "(benches exhausted logins, promotes the healthiest to "
                    "priority 0 — changes which account is tried/billed first)."
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
    probe = bool(args.get("probe", True))
    result: dict
    if action == "list" or (action == "switch" and not target):
        result = list_accounts(provider)
    elif action == "plan":
        if not target.strip():
            result = {"ok": False, "error": "plan needs a target (#index, label, id, or unique prefix)."}
        else:
            result = switch(target, provider, dry_run=True)
    elif action == "auto":
        result = auto_rotate(provider, probe=probe, dry_run=dry_run)
    elif action == "switch":
        result = switch(target, provider, dry_run=dry_run)
    else:
        result = {"ok": False, "error": f"Unknown action '{action}'. Use list, switch, plan, or auto."}
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
    au = sub.add_parser("auto", help="probe logins, bench dead ones, promote the healthiest")
    au.add_argument("--provider", default=DEFAULT_PROVIDER)
    au.add_argument("--no-probe", action="store_true", help="reorder by usage windows only, no test call")
    au.add_argument("--dry-run", action="store_true")
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
    elif cmd == "auto":
        print(render_auto(auto_rotate(
            _arg(args, "provider", DEFAULT_PROVIDER),
            probe=not _arg(args, "no_probe", False),
            dry_run=_arg(args, "dry_run", False),
        )))
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
    "Usage: /switch [list] · /switch setup · /switch auto [--dry-run] [--no-probe] · "
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
    if parts[0] == "auto":
        return render_auto(auto_rotate(provider, probe="--no-probe" not in parts,
                                       dry_run="--dry-run" in parts), markdown=True)
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
                        "enum": ["list", "switch", "plan", "auto"],
                        "description": "list = show all pooled accounts; switch = move target to priority 0; plan = dry-run preview; auto = probe logins and rotate by real utilization",
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
                    "probe": {
                        "type": "boolean",
                        "default": True,
                        "description": "for action=auto: send a 4-token test request per login (ground truth) instead of trusting usage windows only",
                    },
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
            "previews the switch; action=auto benches exhausted logins until "
            "their reset and promotes the account with the most quota headroom "
            "(also escalated; the CLI runs it unattended). This tool never creates "
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
        description="Switch pooled accounts (/switch list, /switch setup, /switch <label>, /switch auto)",
        args_hint="[list|setup|auto|<label>|plan <label>] [--provider=<id>]",
    )
