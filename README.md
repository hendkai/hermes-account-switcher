# credential-pool-switcher

A [Hermes Agent](https://hermes-agent.nousresearch.com) plugin that lets you switch between pooled provider accounts — for example between several Claude (Anthropic) logins — from chat, the CLI, or by the agent itself.

## Why

Hermes rotates a *credential pool* per provider: add several OAuth logins (`hermes auth add anthropic --type oauth`) and it walks them in priority order, skipping exhausted ones automatically. What the core does not give you is a quick, safe way to say **"use THIS account next"** — e.g. the account whose weekly quota just reset — without reading docs about priorities.

This plugin adds exactly that:

- **`/switch`** in any chat — `/switch list`, `/switch hendrik@…`, `/switch plan work`
- **Tool `account_switch`** — the agent can list and plan; a real switch is escalated to the human-approval gate (see Security)
- **CLI `hermes credential-pool-switcher list|switch|plan`**

## What it does

- Lists every pooled account: index, label, auth type, priority, and which entry the pool currently has marked.
- Switching = moving the target account to **priority 0** via the public `hermes auth priority` command; everything else is renumbered automatically by Hermes. On the next model call, that account is tried first.
- **Refuses unsafe switches**: it blocks switching the rotation onto `env:`-fed API-key entries (that would not do what you think) and tells you what to do instead.
- **Plan mode** (`plan` / `dry_run`) previews the change before touching anything.
- Verifies the switch afterwards by re-reading the pool order.

## Security

- **Agent-initiated switches require human approval.** Reordering the pool changes which account gets billed first, so the plugin registers a `pre_tool_call` hook that escalates every model-issued `switch` (not `list`/`plan`) to Hermes' approval gate. `/switch` and the CLI are unaffected — they are already user actions.
- **Tokens:** switching itself never reads, prints, or writes tokens and never edits `auth.json` — it only re-orders already-authenticated entries via Hermes' own CLI. The optional usage percentages do read each pooled OAuth token in-process and send it to the provider's own API (`api.anthropic.com`) through Hermes' public `agent.account_usage.fetch_account_usage` helper; tokens are never printed, logged, or written anywhere.

## Install

```bash
hermes plugins install hendkai/hermes-account-switcher
```

or clone into `~/.hermes/plugins/credential-pool-switcher` and `hermes plugins enable credential-pool-switcher`.

## Usage

```text
/switch list                 → show all accounts and priorities
/switch snailberg@           → move that account to the front
/switch plan snailberg@      → dry-run preview
/switch --provider=anthropic …  → other pooled providers
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
```

## Targets

A target can be `#index`, an exact label, an entry id, or a unique label prefix (`/switch work` matches `work@example.com` if only one label starts with `work`).

## License

MIT
