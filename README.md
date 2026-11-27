# credential-pool-switcher

A [Hermes Agent](https://hermes-agent.nousresearch.com) plugin that lets you switch between pooled provider accounts — for example between several Claude (Anthropic) logins — from chat, the CLI, or by the agent itself, .

## Why

Hermes rotates a *credential pool* per provider: add several OAuth logins (`hermes auth add anthropic --type oauth`) and it walks them in priority order, skipping exhausted ones automatically. What the core does not give you is a quick, safe way to say **"use THIS account next"** — e.g. the account whose weekly quota just reset — without reading docs about priorities.

This plugin adds exactly that:

- **`/switch`** in any chat — `/switch list`, `/switch hendrik@…`, `/switch plan work`, `/switch setup`, `/switch auto`
- **Tool `account_switch`** — the agent can list, plan, and auto-rotate; a real switch or auto rotation is escalated to the human-approval gate (see Security)
- **CLI `hermes credential-pool-switcher list|switch|plan|auto|status|setup`**

## What it does

- Lists every pooled account: index, label, auth type, priority, current marker, bench state (⛔ with remaining cooldown, any exhaustion class) and dead logins (✖ — refresh token rejected, log in anew), plus 5h / weekly (account-level) and model-scoped (Opus / Sonnet week) usage for Anthropic OAuth logins. A benched row Hermes stored without a reset time shows "cooldown unknown": the plugin does not guess a duration.
- **Switching** = moving the target account to **priority 0** (the same `move_entry` call `hermes auth priority` makes; that CLI is the fallback for a *manual* label switch when the pool module is not importable **or its `move_entry` call fails** — exact-id moves never use it, and when the fallback itself fails the plugin reports a fixed failure message and never echoes the CLI's output). Everything else is renumbered by Hermes. On the next model call, that account is tried first.
- **Refuses unsafe switches**: dead logins (Hermes never uses them again) and `env:`-fed API-key entries (reordering does not change the env var). Warns when the target is still benched, and when the provider's pool strategy is not `fill_first` (priority then barely matters).
- **Plan mode** (`plan` / `dry_run`) previews the change before touching anything.
- Verifies the switch afterwards by re-reading the pool order and comparing the resolved **entry id** (labels can repeat, so a label match never counts). A move whose read-back fails or shows another entry first is reported as `unverified` — never as success, never as "done" — and nothing is rolled back; the order may or may not have changed.

## Security

- **Agent-initiated switches require human approval.** Reordering the pool changes which account gets billed first, so the plugin registers a `pre_tool_call` hook that escalates every model-issued `switch` and `auto` (not `list`/`plan`/`dry_run`) to Hermes' approval gate. `/switch` and the CLI are unaffected — they are already user actions.
- **Tokens are never printed, logged, returned, or written by the plugin.**
- **Usage percentages** (`/switch list`, the chip's usage bars, `auto`) go through Hermes' public `agent.account_usage.fetch_account_usage(read_only=True)`: each usable pooled OAuth token is sent to the provider's own API (`api.anthropic.com`) by that helper. Dead logins are skipped (benched ones are read so their bars show why). Where the helper supports `identity_id` (the plugin checks its signature), each entry's id is passed as `identity_id`, so Hermes' own usage cache, which may remember the returned snapshot, is keyed per credential; on a core whose helper lacks it, the plugin calls it without `identity_id` and that cache may not be per credential (the plugin's own results are still keyed by entry id). No token is refreshed or rotated, and no cooldown or bench state is written. The plugin sends no message request of its own.
- **`auth.json`:** the plugin has no path of its own to it: reads (list, status, usage, auto ranking) go through core's `read_credential_pool` accessor, wrapped in a read-only snapshot. It never writes auth.json and never refreshes tokens. The plugin does not call `load_pool()` or `peek()` for reads, because those can seed or prune entries, clear exhaustion and persist. Consequences: the "current" marker is derived (first login in priority order that is neither dead nor benched), and a login borrowed from another tool (for example Claude Code) carries no token in that snapshot, so it shows no usage bars, auto rotation never promotes it, and a token from such a borrowed/foreign source (claude_code, env, unknown) is never sent to the usage helper even on an older pool row that still has one; only logins from Hermes' own auth flows (`manual:*`, `hermes_pkce`) are probed. There is no `hermes auth list` fallback: that command loads and peeks the pool and can persist core maintenance, so when the read accessor is missing (older core) or the pool cannot be read, every read path fails closed with a clear error (the CLI exits nonzero) instead of showing possibly stale text — a pool that cannot be re-read for usage tokens is an explicit pool-read error too, never plain "no usage" with a silent success. Only the move itself loads the real pool (`load_pool()` then `move_entry`, the call behind `hermes auth priority`); core may persist its own load-time maintenance at that point. Auto rotation re-validates the exact entry id (unique, no label collision, still an OAuth login from Hermes' own auth flows, alive, not benched, known cooldown) on that same pool object right before the move and declines if anything changed. Desktop row clicks switch by exact entry id (`switch --exact-id`), and a row without an id is not clickable at all; typed `/switch <label>` keeps label, `#n` and prefix matching. Auto rotation does not bench logins: Hermes' own pool handles 429s.

## Install

```bash
hermes plugins install hendkai/hermes-account-switcher
```

or clone into `~/.hermes/plugins/credential-pool-switcher` and `hermes plugins enable credential-pool-switcher`.

## Adding your accounts

The plugin never creates or stores accounts itself. It works on Hermes' own
**credential pool**, which lives in `~/.hermes/auth.json` (per profile:
`~/.hermes/profiles/<name>/auth.json`).

Add each Claude account once — this opens Anthropic's login in the browser:

```bash
hermes auth add anthropic --type oauth --label work@example.com
hermes auth add anthropic --type oauth --label private@example.com --priority 0   # tried first
hermes auth list anthropic                                                       # check
```

Use a recognisable `--label` (e.g. the account's e-mail): it is what `/switch <label>` matches.

Hermes also seeds the pool automatically from:

| Source | Shows up as |
| --- | --- |
| `hermes auth add … --type oauth` | your label |
| `ANTHROPIC_API_KEY` / `CLAUDE_CODE_OAUTH_TOKEN` (env or `~/.hermes/.env`) | the variable name |
| Claude Code CLI login (`~/.claude/.credentials.json` / Keychain) | `claude_code` |

**Dead logins** (✖ in `/switch list`, refresh token rejected — e.g. after a
long idle period) are never used again. Replace them:

```bash
hermes auth remove anthropic 'work@example.com'
hermes auth add anthropic --type oauth --label 'work@example.com'
```

For `claude_code` log in again in the Claude Code CLI; for env entries put a
fresh token into the variable. `/switch list` prints the right command for
each dead entry.

`/switch setup` (or `hermes credential-pool-switcher setup`) prints these steps
adapted to your current pool.

## Status bar indicator (desktop app)

The package ships a desktop half (`desktop/plugin.js`). Hermes copies it into
the app automatically; enable it once under **Capabilities → Plugins**
(desktop halves are opt-in). It adds a chip at the bottom right:

- **Set up Claude accounts** (amber) while the pool is empty — click for the
  onboarding steps with copyable commands.
- Afterwards the **account Hermes uses next**, coloured by pool health:
  green = all usable, amber = some benched or dead, red = none usable (with the
  time the next one is free).
- Click it for every account's state with its 5h and weekly usage bars (loaded while the popover is open, benched logins included), plus the auto-switch status; click a usable account to put it first; copy the command to revive a dead login or add another. **Auto-rotate now** promotes the login with the most headroom.

It reads `hermes credential-pool-switcher status --json` — a local snapshot
without network calls or token refreshes — every 90 s, and refreshes at once
when a switch happens inside the gateway. The usage bars call `status --json --usage`
only while the popover is open; a usage read that fails outright (for example the
pool could not be re-read) shows its own message, distinct from "no usage data".
`switch` / `plan` / `auto` end with a
`CPS_RESULT ok|failed|unverified` line and exit nonzero on failure: the desktop
shows a success toast only for a confirmed `ok`, never for an unverified move.

## Usage

```text
/switch list                    → all accounts, priorities, usage (⛔ = benched, ✖ = dead)

/switch snailberg@              → move that account to the front
/switch plan snailberg@         → dry-run preview
/switch auto [--dry-run]        → promote the login with the most usage headroom
/switch list --provider=openai  → other pooled providers (flag works in any position)
```

Agent tool:

```json
{"action": "switch", "target": "hendrik@kaiser-mail.de", "provider": "anthropic"}
```

(real switches and `auto` are escalated to human approval; `action: "list"` / `"plan"` run directly)

CLI:

```bash
hermes credential-pool-switcher list
hermes credential-pool-switcher switch '#2'
hermes credential-pool-switcher plan work
hermes credential-pool-switcher auto --dry-run
hermes credential-pool-switcher status
```

## Auto rotation

**Opt-in, off by default.** Background rotation changes which account is billed, so it only runs after you enable it in `config.yaml` (not via `.env`):

```yaml
plugins:
  credential-pool-switcher:
    auto_rotate: true
```

Once enabled, while Hermes is in use, a `pre_llm_call` hook starts a background run at most every 10 minutes (shared across the gateway, CLI and desktop app through a small state file, `credential-pool-switcher-state.json` in the Hermes home: timestamps and the last result, no credentials). A run reads each login's usage and ranks by the **account-level** windows only (weekly first, then 5h), moving the best one to priority 0. It keeps the current login unless the winner is at least 5 points better, so two similar logins do not swap back and forth.

- **Account quota vs. model cap:** a login with its 5h or weekly window at 100% is exhausted. A model-family cap (Opus / Sonnet week) is shown as a scope-specific warning and does **not** disqualify the login, because the ranking is model-agnostic.
- **Usage-eligible is not message-verified:** "has headroom" means the usage API shows room in the account-level windows. The plugin sends no message request and writes no cooldown, so a login can still be rejected by the provider; Hermes' own pool handles that.
- **Fresh check before every move:** usage fetching takes time, so right before an automatic move the pool is read again; a winner that is now dead, benched, borrowed (its source is no longer one of Hermes' own auth flows, e.g. it turned `claude_code`) or of unknown cooldown is declined for that run and the order stays unchanged. (A manual `/switch <label>` still lets you pick a benched login; it only warns.) Usage and eligibility are tracked per credential entry id, so two logins with the same label never share data. The automatic move addresses the winner by its exact entry id only (no label or prefix matching, no CLI fallback) and re-checks it on the very pool object used for the move: still an OAuth login from Hermes' own auth flows, still unique, still not dead, benched or of unknown cooldown. If the pool cannot be read directly or the entry changed or vanished, the run declines without changing anything. If the pool cannot even be re-read for the usage tokens, the whole run fails with an explicit pool-read error instead of reporting success without usage. A move that ran but could not be verified by entry id is reported as `unverified` (nonzero CLI exit), never as a promotion. The token sent to the usage helper is re-checked on the snapshot that supplies it (exact id, OAuth, not dead, pool-owned source), so a login that turned borrowed or foreign between the two reads is never probed.
- **Left alone, never promoted:** logins benched by Hermes (explicit reset time), exhausted rows with no stored reset ("cooldown unknown"; no replacement timeout is invented), dead logins, logins without usage data, and logins whose snapshot has only model-scoped windows (account headroom unproven).

- Turn it off again: remove the setting or set `auto_rotate: false` (the default).
- Run it by hand: `/switch auto [--dry-run]`, `hermes credential-pool-switcher auto`, or **Auto-rotate now** in the chip popover.
- A cron job is optional: it only helps if Hermes sits idle for long stretches.

## Targets

A target can be `#index`, an exact label, an entry id, or a unique label prefix (`/switch work` matches `work@example.com` if only one label starts with `work`).

## Development

```bash
PYTHONPATH=/path/to/hermes-agent pytest tests/
```

`tests/test_logic.py` is hermetic (fake Hermes modules, no network); `tests/test_dispatch.py` exercises the real tool registry and skips without hermes-agent.

Catalog admission note: the read-only snapshot reads through the
`read_credential_pool` accessor as re-exported by the documented public
`agent.credential_pool` module (on Hermes' published plugin API), so
`hermes plugins validate` reports no private core imports. Reading the pool
this way is an explicit operator decision (2026-11-28): the mutating
alternatives (`load_pool()`, `peek()`, `hermes auth list`) all persist pool
state, which the plugin refuses for read paths. The plugin still opens no auth
store itself, imports nothing from `hermes_cli.auth`, and a source-policy test
pins `read_credential_pool` to exactly one call site (`_load_pool`) and to the
public import route.

## License

MIT
