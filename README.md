# account-switcher

A [Hermes Agent](https://hermes-agent.nousresearch.com) plugin that lets you switch between pooled provider accounts — for example between several Claude (Anthropic) logins — from chat, the CLI, or by the agent itself.

## Why

Hermes rotates a *credential pool* per provider: add several OAuth logins (`hermes auth add anthropic --type oauth`) and it walks them in priority order, skipping exhausted ones automatically. What the core does not give you is a quick, safe way to say **"use THIS account next"** — e.g. the account whose weekly quota just reset — without reading docs about priorities.

This plugin adds exactly that:

- **`/switch`** in any chat — `/switch list`, `/switch hendrik@…`, `/switch plan work`
- **Tool `account_switch`** — the agent can switch accounts when you ask it to ("switch to the work account for this task")
- **CLI `hermes account-switcher list|switch|plan`**

## What it does

- Lists every pooled account: index, label, auth type, priority, and which entry the pool currently has marked.
- Switching = moving the target account to **priority 0** via the public `hermes auth priority` command; everything else is renumbered automatically by Hermes. On the next model call, that account is tried first.
- **Refuses unsafe switches**: it blocks switching the rotation onto `env:`-fed API-key entries (that would not do what you think) and tells you what to do instead.
- **Plan mode** (`plan` / `dry_run`) previews the change before touching anything.
- Verifies the switch afterwards by re-reading the pool order.

Tokens are never read, printed, or written. The plugin only re-orders already-authenticated entries using Hermes' own CLI.

## Install

```bash
hermes plugins install hendkai/hermes-account-switcher
```

or clone into `~/.hermes/plugins/account-switcher` and `hermes plugins enable account-switcher`.

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

CLI:

```bash
hermes account-switcher list
hermes account-switcher switch '#2'
hermes account-switcher plan work
```

## Targets

A target can be `#index`, an exact label, an entry id, or a unique label prefix (`/switch work` matches `work@example.com` if only one label starts with `work`).

## License

MIT
