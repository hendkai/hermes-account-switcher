"""credential-pool-switcher — switch between pooled provider accounts.

Works on top of Hermes' public CLI surface only:

* ``hermes auth list <provider>``      — read the pool (labels, priorities,
  active marker). Tokens are never printed by that command and never parsed
  here beyond labels/status.
* ``hermes auth priority <p> <t> <n>`` — move an entry to a new priority
  (others are renumbered). Switching = "move to priority 0", i.e. that
  account is tried first on the next model call.

Surfaces:

* tool ``account_switch``  — the agent can list/plan; a real switch is
  escalated to the human-approval gate by a ``pre_tool_call`` hook
* slash ``/switch``        — same thing from any chat
* CLI ``hermes credential-pool-switcher list|switch|plan``

Switching itself never reads tokens and never edits auth.json — it only
re-orders the existing, already-authenticated pool. The optional usage
percentages do read each pooled OAuth token in-process and send it to the
provider's own API (api.anthropic.com) via Hermes' public
``agent.account_usage.fetch_account_usage`` helper; tokens are never
printed, logged, or written anywhere.
"""

from __future__ import annotations

import json
import re

from dataclasses import dataclass, field
from typing import Any, List, Optional

DEFAULT_PROVIDER = "anthropic"
_HEADING = re.compile(r"^(?P<provider>\S+)\s+\((?P<count>\d+)\s+credential", re.I)
_ROW = re.compile(
    r"^(?P<idx>#\d+)\s+(?P<label>\S+)\s+(?P<type>\S+)\s+"
    r"id=(?P<id>\S+)\s+priority=(?P<priority>-?\d+)\s+(?P<source>\S+)"
    r"(?P<rest>.*)$"
)
_REST_BENCH = re.compile(r"rate-limited.*?(?P<left>\([^)]*left\))", re.I)


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
    import time

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
                    current="←" in (m.group("rest") or ""),
                    notes=[b.group("left")] if (b := _REST_BENCH.search(m.group("rest") or "")) else [],
                )
            )
    return provider, accounts


# -- optional usage enrichment -------------------------------------------------

def _usage_for_accounts(accounts: List[Account]) -> Optional[dict]:
    """Best-effort per-account usage (5h window / week) for OAuth entries.

    Uses Hermes' public ``agent.account_usage.fetch_account_usage`` helper in
    read-only mode when importable (source installs).
    Returns {label: {"five_hour": pct, "week": pct, "reset": str}} or None.
    Tokens are used in-process for the API call and never returned.
    """
    try:
        from agent.account_usage import fetch_account_usage  # type: ignore
        from agent.credential_pool import load_pool  # type: ignore
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
                snap = fetch_account_usage("anthropic", api_key=token, read_only=True)
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
                "benched": a.notes[0] if a.notes else None,
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


# -- utilization-based auto rotation ------------------------------------------

AUTO_BENCH_TAG = "auto-rotation"
_PROBE_MODEL_DEFAULT = "claude-haiku-5-5"
_PROBE_TIMEOUT_SECONDS = 20
# A masked 429 body (plain {"error": "Error"}) carries no reset date: bench for
# an hour so the next auto run re-probes instead of hammering the login.
_PROBE_BENCH_FALLBACK_SECONDS = 60 * 60
_RE_GAIN_DATE = re.compile(r"regain access on (\d{4}-\d{2}-\d{2})(?: at (\d{2}:\d{2}))?")


def _iso_utc(epoch: float) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).isoformat()


def _probe_reset_at(status: int, body: str) -> Optional[str]:
    """ISO reset timestamp for a dead-probe bench, or None for non-benching statuses."""
    import datetime
    import time as _time

    if status in (429, 402):
        m = _RE_GAIN_DATE.search(body)
        if m:
            day, clock = m.group(1), m.group(2) or "00:00"
            try:
                dt = datetime.datetime.strptime(f"{day} {clock}", "%Y-%m-%d %H:%M")
                return dt.replace(tzinfo=datetime.timezone.utc).isoformat()
            except ValueError:
                pass
        return _iso_utc(_time.time() + _PROBE_BENCH_FALLBACK_SECONDS)
    return None


def _probe_anthropic_login(label: str, token: str, model: str) -> dict:
    """One 4-token /v1/messages probe: can this login serve requests right now?

    ``alive`` is True/False; ``None`` means transient (network/5xx) — the login
    must NOT be benched on that. The token stays in-process, never returned.
    """
    import json as _json
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=_json.dumps({
            "model": model, "max_tokens": 4,
            "messages": [{"role": "user", "content": "ping"}],
        }).encode(),
        headers={
            "authorization": f"Bearer {token}",
            "anthropic-version": "2023-06-01",
            # Claude Code identity — OAuth requests are rejected without it.
            "anthropic-beta": "claude-code-20250219,oauth-2025-04-20",
            "User-Agent": "claude-code/2.1.74",
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT_SECONDS) as resp:
            return {"label": label, "alive": resp.status == 200, "status": resp.status}
    except urllib.error.HTTPError as ex:
        body = ex.read().decode("utf-8", "replace")
        return {
            "label": label,
            "alive": False if ex.code in (429, 402) else None,
            "status": ex.code,
            "reset_at": _probe_reset_at(ex.code, body),
            "note": body[:160],
        }
    except Exception as exc:  # network/timeout: transient, never bench
        return {"label": label, "alive": None, "status": None, "note": f"{type(exc).__name__}: {exc}"[:160]}


def _managed_oauth_labels(provider: str) -> dict:
    """{label: access_token} for manual OAuth logins (the pooled real accounts)."""
    try:
        from agent.credential_pool import load_pool
    except Exception:
        return {}
    out: dict = {}
    try:
        for e in load_pool(provider).entries():
            source = str(getattr(e, "source", "") or "")
            if not source.startswith("manual"):
                continue
            if getattr(e, "auth_type", "") != "oauth":
                continue
            token = (getattr(e, "access_token", "") or "").strip()
            if token:
                out[str(getattr(e, "label", "") or e.id)] = token
    except Exception:
        return {}
    return out


def _usage_limits(tokens: dict) -> dict:
    """Per-login verdict from Anthropic's own usage endpoint.

    ``{label: {"dead_until": iso|None, "active_limits": [...], "week_pct": float,
    "session_pct": float}}``. A login is dead when any ``limits[]`` entry is
    active at >=100% / severity critical — the endpoint's own words, no probe
    needed. ``dead_until`` is the LATEST active limit's reset. Tokens stay
    in-process.
    """
    try:
        from agent.account_usage import _get_json
    except Exception:
        return {label: {"error": "agent.account_usage not importable"} for label in tokens}
    out: dict = {}
    for label, token in tokens.items():
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
        active: list = []
        for lim in payload.get("limits") or []:
            if not isinstance(lim, dict) or not lim.get("is_active"):
                continue
            pct = lim.get("percent")
            critical = str(lim.get("severity") or "").lower() == "critical"
            if (isinstance(pct, (int, float)) and pct >= 100) or critical:
                active.append(f"{lim.get('kind')}={pct}%")
                reset = lim.get("resets_at")
                if isinstance(reset, str) and (dead_until is None or reset > dead_until):
                    dead_until = reset
        five = payload.get("five_hour") or {}
        seven = payload.get("seven_day") or {}
        out[label] = {
            "dead_until": dead_until,
            "active_limits": active,
            "week_pct": seven.get("utilization"),
            "session_pct": five.get("utilization"),
        }
    return out


def _write_auto_status(provider: str, benched: dict, alive_labels: list) -> tuple:
    """Bench/clear pool rows directly in auth.json. Returns ``(error, cleared)``.

    Mirrors what the core's own exhaustion path would persist: for each dead
    login ``last_status=exhausted`` with the probe's reset timestamp. An ALIVE
    login still carrying an exhaustion bench is cleared — the probe is the
    ground truth, not the stale bench. Tokens on the rows are never touched.
    """
    import time as _time

    try:
        from hermes_cli.auth import read_credential_pool, write_credential_pool
    except Exception as exc:
        return f"cannot import auth store helpers: {exc}", []
    try:
        rows = read_credential_pool(provider)
    except Exception as exc:
        return f"cannot read credential pool: {exc}", []
    changed = False
    now = _time.time()
    alive = set(alive_labels)
    cleared: list = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        label = str(row.get("label") or "")
        if label in benched:
            row["last_status"] = "exhausted"
            row["last_status_at"] = now
            row["last_error_code"] = benched[label].get("code", 429)
            row["last_error_reason"] = "rate_limit_error"
            row["last_error_message"] = f"[{AUTO_BENCH_TAG}] probe failed: {benched[label].get('note', '')}"[:400]
            row["last_error_reset_at"] = benched[label]["reset_at"]
            changed = True
        elif label in alive and row.get("last_status") == "exhausted":
            for key in ("last_status_at", "last_error_code", "last_error_reason",
                        "last_error_message", "last_error_reset_at"):
                row[key] = None
            row["last_status"] = "ok"
            cleared.append(label)
            changed = True
    if not changed:
        return None, cleared
    try:
        write_credential_pool(provider, rows, status_cleared_ids=cleared or None)
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
    tokens = _managed_oauth_labels(provider)
    if not tokens:
        return {"ok": False, "error": "no manual OAuth logins with tokens found (source install required)."}
    accounts = list_accounts(provider, with_usage=False)
    managed = [a for a in accounts["accounts"] if a["label"] in tokens and a["auth_type"] == "oauth"]
    if not managed:
        return {"ok": False, "error": "pool rows and auth list disagree; nothing to manage."}

    # Ground truth #1: Anthropic's usage endpoint (limits[] with is_active).
    usage = _usage_limits(tokens)
    dead: list = []          # (account, {"reset_at", "why"})
    alive: list = []         # usage says alive
    unknown: list = []       # usage fetch failed → untouched this round
    for a in managed:
        info = usage.get(a["label"]) or {}
        if info.get("error"):
            unknown.append(a)
            continue
        if info.get("dead_until"):
            dead.append((a, {"reset_at": info["dead_until"], "why": ", ".join(info["active_limits"]) or "active limit"}))
        else:
            alive.append(a)

    # Ground truth #2 (probe, optional): usage can look healthy while an org
    # cap still blocks requests — confirm usage-alive logins with one 4-token
    # call before trusting them with priority 0.
    probes: dict = {}
    if probe:
        for a in alive:
            p = _probe_anthropic_login(a["label"], tokens[a["label"]], model)
            probes[a["label"]] = p
            if p.get("alive") is False and p.get("reset_at"):
                dead.append((a, {"reset_at": p["reset_at"], "why": f"probe HTTP {p.get('status')}"}))
    alive = [a for a in alive if a["label"] not in {d[0]["label"] for d in dead}]

    def _week_pct(a: dict) -> float:
        return float((usage.get(a["label"]) or {}).get("week_pct") or 999.0)

    def _session_pct(a: dict) -> float:
        return float((usage.get(a["label"]) or {}).get("session_pct") or 999.0)

    to_bench = {a["label"]: {"reset_at": w["reset_at"], "code": 429, "note": w["why"]} for a, w in dead}
    report = {
        "ok": True,
        "dry_run": dry_run,
        "provider": provider,
        "probed": len(probes),
        "alive": [a["label"] for a in alive],
        "unknown": [a["label"] for a in unknown],
        "dead": {a["label"]: w["reset_at"] for a, w in dead},
        "dead_why": {a["label"]: w["why"] for a, w in dead},
        "benched": {k: v["reset_at"] for k, v in to_bench.items()},
        "cleared": [],
        "promoted": None,
        "all_dead": not alive,
    }
    if dry_run:
        return report

    error, cleared = _write_auto_status(provider, to_bench, alive_labels=[a["label"] for a in alive])
    report["cleared"] = cleared
    if error:
        report["ok"] = False
        report["error"] = error
        return report

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
    for a in data["accounts"]:
        benched = a.get("benched")
        mark = "⛔ " if benched else ("● " if a["current"] else "○ ")
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
            + ("" if benched or a["priority"] != 0 else "  (tried first)")
            + (f" · benched {benched}" if benched else "")
            + usage
        )
    lines.append("● = current entry marker · priority 0 is tried first · ⛔ = benched (exhausted)"
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


def render_auto(data: dict, markdown: bool = False) -> str:
    b = "**" if markdown else ""
    if not data.get("ok"):
        return f"{b}Auto rotation failed{b}: {data.get('error')}"
    lines = [f"{b}Auto rotation ({data['provider']}){b}" + (" — dry run" if data.get("dry_run") else "")]
    for label in data.get("alive", []):
        lines.append(f"✓ {label} alive")
    for label, reset in (data.get("dead") or {}).items():
        until = reset if isinstance(reset, str) else "unknown reset"
        why = (data.get("dead_why") or {}).get(label, "")
        benched = " (benched)" if label in (data.get("benched") or {}) else ""
        suffix = f" [{why}]" if why else ""
        lines.append(f"✗ {label} dead until {until}{benched}{suffix}")
    for label in data.get("cleared", []):
        lines.append(f"↺ {label} bench lifted (probe alive)")
    if data.get("all_dead"):
        lines.append("All managed logins exhausted — provider fails over elsewhere.")
    elif data.get("promoted"):
        lines.append(f"● priority 0 → {data['promoted']}")
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
    sub.add_parser("list", help="show pooled accounts and priorities")
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


def _slash(arg: str = "", **_: Any) -> str:
    parts = (arg or "").strip().split()
    if not parts or parts[0] in {"list", "ls"}:
        return render_list(list_accounts(), markdown=True)
    if parts[0] == "auto":
        return render_auto(auto_rotate(probe="--no-probe" not in parts, dry_run="--dry-run" in parts), markdown=True)
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
            "accounts with priorities; action=switch moves the target account "
            "to the front of the rotation (escalated to human approval — "
            "it changes which account is billed first); action=plan previews "
            "the switch; action=auto benches exhausted logins until their "
            "reset and promotes the account with the most quota headroom "
            "(also escalated; the CLI runs it unattended)."
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
        description="Switch pooled accounts (/switch list, /switch <label>)",
        args_hint="[list|auto|<label>|plan <label>]",
    )
