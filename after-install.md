# Account switcher — first steps

The switcher works on Hermes' own credential pool (`~/.hermes/auth.json`); it never stores accounts itself.

1. **Add each Claude account once** — this opens Anthropic's login in the browser:

   ```bash
   hermes auth add anthropic --type oauth --label you@example.com
   ```

   Repeat for every account. The `--label` is what `/switch <label>` matches.

2. **Check the pool:** `/switch list` in any chat, or `hermes credential-pool-switcher setup`.
   In the desktop app, enable the plugin's desktop half once under **Capabilities → Plugins**:
   a status bar chip (bottom right) then shows "Set up Claude accounts" until the first account
   exists, and afterwards the account Hermes uses next (green / amber / red by pool health).
   Click it to see the 5h and weekly usage bars, switch accounts, or copy the commands below.

3. **Switch by hand:** `/switch <label>` (or click an account in the status bar chip) or `/switch auto`.
   Automatic background rotation is **off by default**. To enable it, add this to `config.yaml`:

   ```yaml
   plugins:
     credential-pool-switcher:
       auto_rotate: true
   ```

   It then checks usage at most every 10 minutes while Hermes is in use and reorders the pool, which changes
   the account that is billed first. It ranks by account-level 5h/weekly usage; a model-family cap (Opus/Sonnet)
   is only shown as a warning. "Usage-eligible" is not verified by a message: the plugin sends no message request
   and writes no cooldown. Rows with an unknown cooldown are never promoted.

Dead logins (✖, refresh token rejected) are replaced with
`hermes auth remove anthropic '<label>' && hermes auth add anthropic --type oauth --label '<label>'`.
