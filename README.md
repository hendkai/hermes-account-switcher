# credential-pool-switcher

A [Hermes Agent](https://hermes-agent.nousresearch.com) plugin that lets you switch between pooled provider accounts — for example between several Claude (Anthropic) logins — from chat, the CLI, or by the agent itself, and rotate them automatically by real quota usage.

## Why

Hermes rotates a *credential pool* per provider: add several OAuth logins (`hermes auth add anthropic --type oauth`) and it walks them in priority order, skipping exhausted ones automatically. What the core does not give you is a quick, safe way to say **"use THIS account next"** — e.g. the account whose weekly quota just reset — without reading docs about priorities.

This plugin adds exactly that:

- **`/switch`** in any chat — `/switch list`, `/switch hendrik@…`, `/switch plan work`, `/switch auto`
- **Tool `account_switch`** — the agent can list and plan; a real switch or auto rotation is escalated to the human-approval gate (see Security)
- **CLI `hermes credential-pool-switcher list|switch|plan|auto`**

## What it does

- Lists every pooled account: index, label, auth type, priority, current marker, bench state (⛔ with remaining cooldown, any exhaustion class) and dead logins (✖ — refresh token rejected, log in anew), plus 5h / weekly / model-scoped usage for Anthropic OAuth logins.
- Switching = moving the target account to **priority 0** (the same `move_entry` call `hermes auth priority` makes; that CLI is the fallback when the pool module is not importable). Everything else is renumbered by Hermes. On the next model call, that account is tried first.
- **Refuses unsafe switches**: dead logins (Hermes never uses them again) and `env:`-fed API-key entries (reordering does not change the env var). Warns when the target is still benched, and when the provider's pool strategy is not `fill_first` (priority then barely matters).
- **Plan mode** (`plan` / `dry_run`) previews the change before touching anything.
- Verifies the switch afterwards by re-reading the pool order.

## Security

- **Agent-initiated switches require human approval.** Reordering the pool changes which account gets billed first, so the plugin registers a `pre_tool_call` hook that escalates every model-issued `switch` and `auto` (not `list`/`plan`/`dry_run`) to Hermes' approval gate. `/switch` and the CLI are unaffected — they are already user actions.
- **Tokens are never printed, logged, returned, or written by the plugin.**
- **Usage percentages** (list + auto) read each pooled OAuth token in-process and send it only to the provider's own API (`api.anthropic.com/api/oauth/usage`). Expiring tokens are refreshed first through the pool's own refresh path, so the rotated single-use refresh token is persisted by Hermes itself.
- **What touches `auth.json`:** switching re-orders priorities through the pool (like `hermes auth priority`). Auto rotation additionally writes bench state (`last_status`, `last_error_*`) for exhausted logins and clears it for logins that are alive again — under Hermes' auth-store lock and with token-generation bases, so a token refreshed concurrently by another Hermes process is never reverted. Token fields are never modified; dead rows are never touched.
- The probe (auto, optional) sends one 4-token request per usage-healthy login to `api.anthropic.com/v1/messages`.

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

| Source | Shows up as | Managed by `auto`? |
| --- | --- | --- |
| `hermes auth add … --type oauth` | your label | yes |
| `ANTHROPIC_API_KEY` / `CLAUDE_CODE_OAUTH_TOKEN` (env or `~/.hermes/.env`) | the variable name | no |
| Claude Code CLI login (`~/.claude/.credentials.json` / Keychain) | `claude_code` | no |

**Dead logins** (✖ in `/switch list`, refresh token rejected — e.g. after a
long idle period) are never used again. Replace them:

```bash
hermes auth remove anthropic 'work@example.com'
hermes auth add anthropic --type oauth --label 'work@example.com'
```

For `claude_code` log in again in the Claude Code CLI; for env entries put a
fresh token into the variable. `/switch list` prints the right command for
each dead entry.

**Unattended rotation:** run `hermes credential-pool-switcher auto` from cron
(e.g. every 30 minutes).

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
  copy the command to revive a dead login or add another; run auto rotation.

It reads `hermes credential-pool-switcher status --json` — a local snapshot
without network calls or token refreshes — every 90 s, and refreshes at once
when a switch or auto run happens inside the gateway.

## Usage

```text
/switch list                    → all accounts, priorities, usage (⛔ = benched, ✖ = dead)
/switch auto [--dry-run] [--no-probe]  → bench exhausted logins, promote the healthiest
/switch snailberg@              → move that account to the front
/switch plan snailberg@         → dry-run preview
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
```

## Targets

A target can be `#index`, an exact label, an entry id, or a unique label prefix (`/switch work` matches `work@example.com` if only one label starts with `work`).

## Auto rotation (`auto`)

Anthropic only, manual OAuth logins only.

- Reads Anthropic's own usage endpoint per login: any active `limits[]` entry at 100% / severity `critical` benches that login until its latest `resets_at`.
- Logins that are already benched with a future reset are answered from the stored bench — no usage call (the endpoint rate-limits per-cycle polling) and no rewrite of the bench.
- An optional 4-token probe double-checks usage-healthy logins before they get priority 0 (catches org spend caps the usage windows do not show). The probe is built like a real Hermes request (Claude Code system prefix, `claude-code/<installed version> (external, cli)` user agent, `x-app: cli`, OAuth betas) — without that identity Anthropic answers every 5.x model with a masked `429 {"message":"Error"}` even on a free login. A 429/402 with a limit message benches the login (until the "regain access" time, else for one hour); a *masked* 429 (bare "Error", no reset date) on a login whose usage endpoint shows headroom is reported as *inconclusive* and never benches; other failures are *inconclusive* too. An unknown probe model falls back to the next one.
- The login with the most weekly headroom (then 5h headroom, then priority) is promoted to priority 0 — a freshly reset login at 0% wins.
- A probed-alive login that still carries a bench is unbenched.
- Dead logins (refresh token rejected) are listed as "log in anew" and left alone.

The tool path is escalated to human approval like `switch`; the CLI (`hermes credential-pool-switcher auto`) is meant for unattended cron runs.

## Development

```bash
PYTHONPATH=/path/to/hermes-agent pytest tests/
```

`tests/test_logic.py` is hermetic (fake Hermes modules, no network); `tests/test_dispatch.py` exercises the real tool registry and skips without hermes-agent.

## License

MIT

## Changelog

- **1.3.2** — Fix: the auto probe lacked the Claude Code identity (system prefix, `(external, cli)` UA with the installed version, `x-app: cli`), so Anthropic answered every 5.x probe with a masked 429 and healthy logins were benched for an hour on every run. The probe now sends the same identity as Hermes; a masked 429 on a usage-free login is *inconclusive* and never benches. Real limit 429/402 (reset date) still bench.
