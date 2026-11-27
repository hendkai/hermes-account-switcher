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
- Switching = moving the target account to **priority 0** (the same `move_entry` call `hermes auth priority` makes; that CLI is the fallback when the pool module is not importable). Everything else is renumbered by Hermes. On the next model call, that account is tried first.
- **Refuses unsafe switches**: dead logins (Hermes never uses them again) and `env:`-fed API-key entries (reordering does not change the env var). Warns when the target is still benched, and when the provider's pool strategy is not `fill_first` (priority then barely matters).
- **Plan mode** (`plan` / `dry_run`) previews the change before touching anything.
- Verifies the switch afterwards by re-reading the pool order.

## Security

- **Agent-initiated switches require human approval.** Reordering the pool changes which account gets billed first, so the plugin registers a `pre_tool_call` hook that escalates every model-issued `switch` and `auto` (not `list`/`plan`/`dry_run`) to Hermes' approval gate. `/switch` and the CLI are unaffected — they are already user actions.
- **Tokens are never printed, logged, returned, or written by the plugin.**
- **Usage percentages** (`/switch list`, the chip's usage bars, `auto`) go through Hermes' public `agent.account_usage.fetch_account_usage(read_only=True)`: each usable pooled OAuth token is sent to the provider's own API (`api.anthropic.com`) by that helper. Dead logins are skipped (benched ones are read so their bars show why). The plugin passes each entry's id as `identity_id`, so Hermes' own usage cache, which may remember the returned snapshot, stays per credential. No token is refreshed or rotated, and no cooldown or bench state is written. The plugin sends no message request of its own.
- **`auth.json`:** the plugin never reads or writes it itself. Switching and auto rotation re-order priorities through the pool object (`move_entry`, the call behind `hermes auth priority`); everything else is read-only. Auto rotation does not bench logins: Hermes' own pool handles 429s.

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
only while the popover is open.

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
- **Fresh check before every move:** usage fetching takes time, so right before an automatic move the pool is read again; a winner that is now dead, benched or of unknown cooldown is declined for that run and the order stays unchanged. (A manual `/switch <label>` still lets you pick a benched login; it only warns.) Usage and eligibility are tracked per credential entry id, so two logins with the same label never share data.
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

## License

MIT
