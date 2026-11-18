# credential-pool-switcher

A [Hermes Agent](https://hermes-agent.nousresearch.com) plugin that lets you switch between pooled provider accounts — for example between several Claude (Anthropic) logins — from chat, the CLI, or by the agent itself, .

## Why

Hermes rotates a *credential pool* per provider: add several OAuth logins (`hermes auth add anthropic --type oauth`) and it walks them in priority order, skipping exhausted ones automatically. What the core does not give you is a quick, safe way to say **"use THIS account next"** — e.g. the account whose weekly quota just reset — without reading docs about priorities.

This plugin adds exactly that:

- **`/switch`** in any chat — `/switch list`, `/switch hendrik@…`, `/switch plan work`, `/switch setup`
- **Tool `account_switch`** — the agent can list and plan; a real switch is escalated to the human-approval gate (see Security)
- **CLI `hermes credential-pool-switcher list|switch|plan|status|setup`**

## What it does

- Lists every pooled account: index, label, auth type, priority, current marker, bench state (⛔ with remaining cooldown, any exhaustion class) and dead logins (✖ — refresh token rejected, log in anew), plus 5h / weekly / model-scoped usage for Anthropic OAuth logins.
- Switching = moving the target account to **priority 0** (the same `move_entry` call `hermes auth priority` makes; that CLI is the fallback when the pool module is not importable). Everything else is renumbered by Hermes. On the next model call, that account is tried first.
- **Refuses unsafe switches**: dead logins (Hermes never uses them again) and `env:`-fed API-key entries (reordering does not change the env var). Warns when the target is still benched, and when the provider's pool strategy is not `fill_first` (priority then barely matters).
- **Plan mode** (`plan` / `dry_run`) previews the change before touching anything.
- Verifies the switch afterwards by re-reading the pool order.

## Security

- **Agent-initiated switches require human approval.** Reordering the pool changes which account gets billed first, so the plugin registers a `pre_tool_call` hook that escalates every model-issued `switch` (not `list`/`plan`/`dry_run`) to Hermes' approval gate. `/switch` and the CLI are unaffected — they are already user actions.
- **Tokens are never printed, logged, returned, or written by the plugin.**
- **Usage percentages** (`/switch list`) go through Hermes' public `agent.account_usage.fetch_account_usage(read_only=True)`: each usable pooled OAuth token is sent to the provider's own API (`api.anthropic.com`) by that helper. Dead and still-benched logins are skipped; nothing is refreshed, rotated or written.
- **`auth.json`:** the plugin never reads or writes it itself. Switching re-orders priorities through the pool object (`move_entry`, the call behind `hermes auth priority`); everything else is read-only.
- This catalog build has no automatic rotation. The full build with `auto` (usage-based benching and promotion, unattended cron mode) lives on the repository's `main` branch.

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
- Click it for every account's state; click a usable account to put it first;
  copy the command to revive a dead login or add another.

It reads `hermes credential-pool-switcher status --json` — a local snapshot
without network calls or token refreshes — every 90 s, and refreshes at once
when a switch happens inside the gateway.

## Usage

```text
/switch list                    → all accounts, priorities, usage (⛔ = benched, ✖ = dead)

/switch snailberg@              → move that account to the front
/switch plan snailberg@         → dry-run preview
/switch list --provider=openai  → other pooled providers (flag works in any position)
```

Agent tool:

```json
{"action": "switch", "target": "hendrik@kaiser-mail.de", "provider": "anthropic"}
```

(real switches are escalated to human approval; `action: "list"` / `"plan"` run directly)

CLI:

```bash
hermes credential-pool-switcher list
hermes credential-pool-switcher switch '#2'
hermes credential-pool-switcher plan work
hermes credential-pool-switcher status
```

## Targets

A target can be `#index`, an exact label, an entry id, or a unique label prefix (`/switch work` matches `work@example.com` if only one label starts with `work`).

## Development

```bash
PYTHONPATH=/path/to/hermes-agent pytest tests/
```

`tests/test_logic.py` is hermetic (fake Hermes modules, no network); `tests/test_dispatch.py` exercises the real tool registry and skips without hermes-agent.

## License

MIT
