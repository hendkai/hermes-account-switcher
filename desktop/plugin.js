/**
 * credential-pool-switcher — status bar indicator for the account switcher.
 *
 * Chip (bottom right):
 *   • no accounts yet  → "Set up Claude accounts" (amber) — click shows the onboarding steps
 *   • accounts present → the account Hermes uses next, coloured by pool health
 *                        (green = all usable, amber = some benched/dead, red = none usable)
 * Popover: every pooled account with its state and, while the popover is open,
 * its 5h / weekly (and model-scoped) usage bars; click a usable one to move it to
 * the front; copyable commands for adding accounts and reviving dead logins;
 * buttons for auto rotation and refresh.
 *
 * Data: host.request('cli.exec', ['credential-pool-switcher', 'status', '--json'])
 * — a cheap pool snapshot (no network, no token refresh) from the Python half.
 * Usage: the same command with '--usage' (one read-only call per login), fetched
 * only while the popover is open.
 * Live updates: the Python half broadcasts plugin.credential-pool-switcher.pool.changed
 * when a switch or auto run happens inside the gateway; polling covers cron runs.
 *
 * Plain ESM, loaded uncompiled: only @hermes/plugin-sdk, react, react/jsx-runtime resolve.
 */

import * as sdk from '@hermes/plugin-sdk'
import { useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const {
  Button, CopyButton, Popover, PopoverContent, PopoverTrigger, STATUSBAR_AREAS, StatusDot,
  cn, host, queryClient, useQuery, usePluginI18n
} = sdk

const ID = 'credential-pool-switcher'
const CLI = 'credential-pool-switcher'
const PREFIX = 'CPS_STATUS '
const RESULT_PREFIX = 'CPS_RESULT '
const QK = [ID, 'status']
const UQK = [ID, 'usage']
const POLL_MS = 90_000
const USAGE_MS = 60_000

const MESSAGES = {
  en: {
    title: 'Claude accounts',
    setup: 'Set up Claude accounts',
    setupIntro: 'No accounts yet. The switcher works on Hermes\u2019 credential pool (~/.hermes/auth.json).',
    step1: 'Add each Claude account once \u2014 opens the browser login:',
    step2: 'Repeat for every account, then come back here: the indicator switches to the account in use.',
    step3: 'Automatic switching is off by default. Switch by clicking an account, or turn it on: plugins.credential-pool-switcher.auto_rotate: true',
    allExhausted: 'Claude: all exhausted',
    readyAt: (t) => `next ready ${t}`,
    inUse: 'in use',
    benchedFor: (left) => `benched ${left}`,
    benched: 'benched',
    cooldownUnknown: 'benched \u00b7 cooldown unknown',
    cooldownUnknownTip: 'Exhausted, and Hermes stored no reset time: the end of the cooldown is unknown',
    dead: 'log in anew',
    ready: 'ready',
    clickToSwitch: 'Click to use this account first',
    usable: (n, total) => `${n} of ${total} usable`,
    revive: 'Revive dead login:',
    addMore: 'Add another account:',
    auto: 'Auto-rotate now',
    refresh: 'Refresh',
    fiveHour: '5 h',
    week: 'week',
    scoped: 'model',
    usageLoading: 'Loading usage\u2026',
    usageUnavailable: 'Usage unavailable right now',
    usageError: 'Usage could not be read \u2014 the credential pool was unavailable',
    switched: (label) => `Claude account switched: ${label} is used first`,
    switchFailed: (msg) => `Switch failed: ${msg}`,
    switchUnverified: (label) => `The switcher could not confirm that ${label} is now used first — the order may or may not have changed; check the list.`,
    autoDone: 'Auto rotation finished',
    autoFailed: (msg) => `Auto rotation failed: ${msg}`,
    autoOn: (mins, last) => `Auto-switch on \u00b7 every ${mins} min` + (last ? ` \u00b7 last check ${last}` : ''),
    autoOff: 'Auto-switch off (opt in: plugins.credential-pool-switcher.auto_rotate)',
    loadFailed: 'Account switcher unavailable',
    retry: 'click to retry'
  },
  de: {
    title: 'Claude-Accounts',
    setup: 'Claude-Accounts einrichten',
    setupIntro: 'Noch keine Accounts hinterlegt. Der Switcher nutzt den Credential-Pool von Hermes (~/.hermes/auth.json).',
    step1: 'Jeden Claude-Account einmal hinzuf\u00fcgen \u2014 \u00f6ffnet den Browser-Login:',
    step2: 'F\u00fcr jeden Account wiederholen. Danach zeigt der Indikator hier den aktiven Account.',
    step3: 'Automatisches Wechseln ist standardm\u00e4\u00dfig aus. Account anklicken zum Wechseln, oder einschalten: plugins.credential-pool-switcher.auto_rotate: true',
    allExhausted: 'Claude: alle ersch\u00f6pft',
    readyAt: (t) => `wieder frei ${t}`,
    inUse: 'aktiv',
    benchedFor: (left) => `gesperrt ${left}`,
    benched: 'gesperrt',
    cooldownUnknown: 'gesperrt \u00b7 Cooldown unbekannt',
    cooldownUnknownTip: 'Ersch\u00f6pft, und Hermes hat keine Freigabezeit gespeichert: das Ende der Sperre ist unbekannt',
    dead: 'neu einloggen',
    ready: 'bereit',
    clickToSwitch: 'Klicken, um diesen Account zuerst zu nutzen',
    usable: (n, total) => `${n} von ${total} nutzbar`,
    revive: 'Toten Login wiederbeleben:',
    addMore: 'Weiteren Account hinzuf\u00fcgen:',
    auto: 'Jetzt auto-rotieren',
    refresh: 'Aktualisieren',
    fiveHour: '5 Std.',
    week: 'Woche',
    scoped: 'Modell',
    usageLoading: 'Lade Nutzung\u2026',
    usageUnavailable: 'Nutzung gerade nicht verf\u00fcgbar',
    usageError: 'Nutzung nicht lesbar \u2014 der Credential-Pool war nicht erreichbar',
    switched: (label) => `Claude-Account gewechselt: ${label} wird zuerst genutzt`,
    switchFailed: (msg) => `Wechsel fehlgeschlagen: ${msg}`,
    switchUnverified: (label) => `Der Switcher konnte nicht best\u00e4tigen, dass ${label} jetzt zuerst genutzt wird — die Reihenfolge hat sich eventuell ge\u00e4ndert; Liste pr\u00fcfen.`,
    autoDone: 'Auto-Rotation fertig',
    autoFailed: (msg) => `Auto-Rotation fehlgeschlagen: ${msg}`,
    autoOn: (mins, last) => `Auto-Switch an \u00b7 alle ${mins} Min.` + (last ? ` \u00b7 zuletzt gepr\u00fcft ${last}` : ''),
    autoOff: 'Auto-Switch aus (aktivieren: plugins.credential-pool-switcher.auto_rotate)',
    loadFailed: 'Account-Switcher nicht erreichbar',
    retry: 'Klick = erneut versuchen'
  }
}

// ── CLI result contract ───────────────────────────────────────────────────────
// `switch` / `plan` / `auto` end with a `CPS_RESULT <outcome>` line and exit nonzero
// when the outcome is not ok. Success is decided ONLY by that pair, never by a
// zero exit or the absence of a 'failed' word: a pool-read error, a refused switch
// and an unverified move all report failure here.
// The CLI line already starts with its own English "Switch failed:" / "Auto rotation failed:";
// the toast adds its (localized) prefix, so drop the CLI's to avoid "Switch failed: Switch failed: …".
const CLI_FAIL_PREFIX = /^(?:Switch failed|Auto rotation failed|Command failed):\s*/i
const stripCliPrefix = (m) => String(m).replace(CLI_FAIL_PREFIX, '')

function cliOutcome(code, output) {
  const r = cliOutcomeRaw(code, output)
  return r.ok ? r : { ...r, message: stripCliPrefix(r.message) }
}

function cliOutcomeRaw(code, output) {
  const lines = output.split('\n').map((l) => l.trim())
  // The result line is read first, whatever the exit code: an unverified move exits nonzero
  // by design, so the exit code alone would turn it into a plain "failed".
  const outcome = lines.filter((l) => l.startsWith(RESULT_PREFIX)).pop()
  const kind = outcome ? outcome.slice(RESULT_PREFIX.length).trim() : ''
  if (kind === 'ok' && code === 0) return { ok: true, kind }
  const body = lines.filter((l) => l && !l.startsWith(RESULT_PREFIX) && !l.startsWith(PREFIX))
  if (kind === 'unverified') {
    // Prefer the "Switch unverified…" line (switch); an auto report has no such line, so use
    // its last line (the "⚠ promotion could not be verified…" error), never the whole report.
    const line = body.find((l) => l.startsWith('Switch unverified'))
    return { ok: false, kind, message: line || body.pop() || kind }
  }
  if (!outcome) {
    return { ok: false, kind: 'failed', message: code !== 0 ? body.pop() || `exit ${code}` : 'no result line from the switcher CLI' }
  }
  // `ok` with a nonzero exit, `failed`, or any unknown kind: never a success.
  if (code !== 0) return { ok: false, kind: kind === 'ok' ? 'failed' : kind, message: body.pop() || `exit ${code}` }
  return { ok: false, kind, message: body.join(' ') || kind }
}

// A row click must carry the credential's entry id: pass it as an exact id (no label /
// #n / prefix matching), so a row whose label equals another row's id cannot be switched.
// Without an id the row is not switchable at all: typed commands may use friendly
// label matching, row clicks may not.
function rowSwitchArgv(row) {
  return row && row.id ? ['switch', '--exact-id', row.id] : null
}

// A move that ran but could not be confirmed deserves its own wording, not "failed".
// The kind comes from the CLI result line; the message regex is only a fallback when no kind is
// known, because a login labelled "unverified-acct" must not turn a failed switch into "moved".
function switchFailText(row, message, t, kind) {
  const unverified = kind ? kind === 'unverified' : /unverified/i.test(String(message))
  return unverified ? t('switchUnverified', row.label) : t('switchFailed', message)
}

// ── data ──────────────────────────────────────────────────────────────────────

async function runCli(argv, timeout = 60) {
  // The RPC timeout must outlast the CLI's own (an auto run probes nothing, but reads usage per login).
  const result = await host.request('cli.exec', { argv: [CLI, ...argv], timeout }, (timeout + 15) * 1000)
  if (result?.blocked) throw new Error(result.hint || 'blocked')
  return { code: result?.code ?? -1, output: String(result?.output || '') }
}

async function loadStatus(extra = [], timeout = 60) {
  const { code, output } = await runCli(['status', '--json', ...extra], timeout)
  const line = output.split('\n').find((l) => l.startsWith(PREFIX))
  if (!line) throw new Error(output.trim().slice(0, 300) || `exit ${code}`)
  return JSON.parse(line.slice(PREFIX.length))
}

const refresh = () => queryClient.invalidateQueries({ queryKey: QK })
const refreshAll = () => {
  refresh()
  queryClient.invalidateQueries({ queryKey: UQK })
}

function useStatus() {
  return useQuery({ queryKey: QK, queryFn: () => loadStatus(), refetchInterval: POLL_MS, staleTime: 20_000, retry: 1 })
}

// Only mounted while the popover is open (see Chip), so usage is fetched on demand.
function useUsage() {
  return useQuery({
    queryKey: UQK,
    queryFn: () => loadStatus(['--usage'], 90),
    staleTime: USAGE_MS,
    retry: false
  })
}

// ── helpers ───────────────────────────────────────────────────────────────────

const TONE = { setup: 'warn', ok: 'good', degraded: 'warn', exhausted: 'bad' }

function localTime(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const sameDay = d.toDateString() === new Date().toDateString()
  return sameDay
    ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    : d.toLocaleString([], { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
}

function rowTone(row) {
  if (row.status === 'dead') return 'bad'
  if (row.status === 'benched') return 'warn'
  return row.active ? 'good' : 'muted'
}

function rowState(row, t) {
  if (row.status === 'dead') return t('dead')
  if (row.status === 'benched') {
    if (row.cooldown_unknown) return t('cooldownUnknown')
    return row.bench_left ? t('benchedFor', row.bench_left) : t('benched')
  }
  return row.active ? t('inUse') : t('ready')
}

function CommandLine({ text }) {
  return jsxs('div', {
    className: 'flex items-center gap-1 rounded-md bg-(--ui-bg-secondary) px-2 py-1',
    children: [
      jsx('code', { className: 'min-w-0 flex-1 truncate font-mono text-[0.6875rem]', title: text, children: text }),
      CopyButton ? jsx(CopyButton, { text, appearance: 'icon', buttonSize: 'icon-xs' }) : null
    ]
  })
}

const usageTone = (pct) => (pct >= 100 ? '#ef4444' : pct >= 80 ? '#f59e0b' : '#22c55e')

function UsageBar({ label, pct }) {
  const value = Math.max(0, Math.min(100, Number(pct) || 0))
  return jsxs('div', {
    className: 'flex items-center gap-2 text-[0.6875rem] text-(--ui-text-tertiary)',
    children: [
      jsx('span', { className: 'w-16 shrink-0 truncate', title: label, children: label }),
      jsx('div', {
        className: 'h-1.5 min-w-0 flex-1 overflow-hidden rounded-full bg-(--ui-bg-secondary)',
        children: jsx('div', {
          className: 'h-full rounded-full',
          style: { width: `${value}%`, background: usageTone(value) }
        })
      }),
      jsx('span', { className: 'w-9 shrink-0 text-right tabular-nums', children: `${Math.round(value)}%` })
    ]
  })
}

function UsageBars({ usage, t }) {
  if (!usage) return null
  const bars = [
    ['five_hour', t('fiveHour')],
    ['week', t('week')],
    // Model-family cap: show the family's own label (e.g. "Opus week"), generic text only as a fallback.
    ['scoped', usage.scoped?.label || t('scoped')]
  ].filter(([key]) => usage[key]?.used_percent != null)
  if (!bars.length) return null
  return jsx('div', {
    className: 'flex flex-col gap-0.5 pb-1 pl-5 pr-1.5',
    children: bars.map(([key, label]) => jsx(UsageBar, { label, pct: usage[key].used_percent }, key))
  })
}

// ── popover content ───────────────────────────────────────────────────────────

function SetupSteps({ status, t }) {
  return jsxs('div', {
    className: 'flex flex-col gap-2',
    children: [
      jsx('p', { className: 'text-(--ui-text-secondary)', children: t('setupIntro') }),
      jsx('p', { children: `1. ${t('step1')}` }),
      jsx(CommandLine, { text: status.add_hint }),
      jsx('p', { children: `2. ${t('step2')}` }),
      jsx('p', { children: `3. ${t('step3')}` })
    ]
  })
}

function AccountRow({ row, t, onSwitch, busy }) {
  // No entry id (old backend row) → no switch: an id-less click could only fall back to
  // label matching, which can hit the wrong account when labels repeat.
  const clickable = row.switchable && !!row.id && !busy
  return jsxs('button', {
    type: 'button',
    disabled: !clickable,
    onClick: clickable ? () => onSwitch(row) : undefined,
    title: clickable
      ? t('clickToSwitch')
      : row.cooldown_unknown
        ? t('cooldownUnknownTip')
        : row.bench_until
          ? localTime(row.bench_until)
          : undefined,
    className: cn(
      'flex w-full items-center gap-2 rounded-md px-1.5 py-1 text-left',
      clickable ? 'cursor-pointer hover:bg-(--chrome-action-hover)' : 'cursor-default'
    ),
    children: [
      jsx(StatusDot, { tone: rowTone(row) }),
      jsx('span', { className: cn('min-w-0 flex-1 truncate', row.active && 'font-semibold'), children: row.label }),
      jsx('span', { className: 'shrink-0 text-(--ui-text-tertiary) tabular-nums', children: rowState(row, t) })
    ]
  })
}

function Panel({ status, t }) {
  const [busy, setBusy] = useState(false)
  const usage = useUsage()
  const usageRows = new Map((usage.data?.accounts || []).map((r) => [r.id ?? r.label, r.usage]))
  const hasUsage = usageRows.size > 0

  const act = async (argv, okMsg, failMsg) => {
    setBusy(true)
    try {
      const { code, output } = await runCli(argv, argv[0] === 'auto' ? 180 : 60)
      const outcome = cliOutcome(code, output)
      if (!outcome.ok) throw Object.assign(new Error(outcome.message), { cliKind: outcome.kind })
      host.notify({ kind: 'success', message: okMsg })
    } catch (err) {
      host.notify({ kind: 'error', message: failMsg(err?.message || String(err), err?.cliKind) })
    } finally {
      setBusy(false)
      refreshAll()
    }
  }

  // A row click switches by exact entry id only (see rowSwitchArgv); a row without an
  // id is never switchable — there is no friendly-label fallback for clicks.
  const onSwitch = (row) => {
    const argv = rowSwitchArgv(row)
    if (argv) act(argv, t('switched', row.label), (m, kind) => switchFailText(row, m, t, kind))
  }
  const dead = status.accounts.filter((r) => r.status === 'dead')

  return jsxs('div', {
    className: 'flex w-80 flex-col gap-2 text-[0.75rem]',
    children: [
      jsxs('div', {
        className: 'flex items-baseline justify-between',
        children: [
          jsx('strong', { children: t('title') }),
          status.count
            ? jsx('span', { className: 'text-(--ui-text-tertiary)', children: t('usable', status.usable, status.count) })
            : null
        ]
      }),
      status.state === 'setup'
        ? jsx(SetupSteps, { status, t })
        : jsx('div', {
            className: 'flex flex-col',
            children: status.accounts.map((row) =>
              jsxs('div', {
                key: `${row.id ?? row.label}-${row.index}`,
                className: 'flex flex-col',
                children: [
                  jsx(AccountRow, { row, t, onSwitch, busy }),
                  jsx(UsageBars, { usage: usageRows.get(row.id ?? row.label), t })
                ]
              }))
          }),
      status.state !== 'setup' && usage.isError
        // The --usage call failed outright (e.g. the pool could not be re-read): its own
        // wording, distinct from ordinary "no usage data".
        ? jsx('p', { className: 'text-(--ui-text-secondary)', children: t('usageError') })
        : status.state !== 'setup' && usage.isFetching && !usage.data
          ? jsx('p', { className: 'text-(--ui-text-tertiary)', children: t('usageLoading') })
          : null,
      status.state !== 'setup' && !usage.isError && !usage.isFetching && !hasUsage && status.count
        ? jsx('p', { className: 'text-(--ui-text-tertiary)', children: t('usageUnavailable') })
        : null,
      status.state === 'exhausted' && status.next_ready_at
        ? jsx('p', { className: 'text-(--ui-text-tertiary)', children: t('readyAt', localTime(status.next_ready_at)) })
        : null,
      status.state !== 'setup' && status.auto
        ? jsx('p', {
            className: 'text-(--ui-text-tertiary)',
            children: status.auto.enabled
              ? t('autoOn', status.auto.interval_minutes, localTime(status.auto.last_run))
              : t('autoOff')
          })
        : null,
      ...dead.map((row) =>
        jsxs('div', {
          className: 'flex flex-col gap-1',
          children: [
            jsx('span', { className: 'text-(--ui-text-tertiary)', children: `${t('revive')} ${row.label}` }),
            jsx(CommandLine, { text: row.relogin_hint })
          ]
        }, `dead-${row.id ?? row.label}`)),
      status.state !== 'setup'
        ? jsxs('div', {
            className: 'flex flex-col gap-1',
            children: [
              jsx('span', { className: 'text-(--ui-text-tertiary)', children: t('addMore') }),
              jsx(CommandLine, { text: status.add_hint })
            ]
          })
        : null,
      status.strategy_note
        ? jsx('p', { className: 'text-(--ui-text-tertiary)', children: `\u26a0 ${status.strategy_note}` })
        : null,
      jsxs('div', {
        className: 'flex justify-end gap-1',
        children: [
          status.state !== 'setup' && status.provider === 'anthropic'
            ? jsx(Button, {
                size: 'sm', variant: 'ghost', disabled: busy,
                onClick: () => act(['auto'], t('autoDone'), (m) => t('autoFailed', m)),
                children: t('auto')
              })
            : null,
          jsx(Button, { size: 'sm', variant: 'ghost', disabled: busy, onClick: refreshAll, children: t('refresh') })
        ]
      })
    ]
  })
}

// ── chip ──────────────────────────────────────────────────────────────────────

const CHIP_CLASS =
  'inline-flex h-full max-w-56 items-center gap-1.5 px-1.5 text-[0.6875rem] text-(--ui-text-tertiary) ' +
  'transition-colors hover:bg-(--chrome-action-hover) hover:text-foreground'

function chipLabel(status, t) {
  if (status.state === 'setup') return t('setup')
  if (status.state === 'exhausted') return t('allExhausted')
  return status.active?.label || t('title')
}

function chipTitle(status, t) {
  if (status.state === 'setup') return t('setup')
  const parts = [`${t('title')}: ${t('usable', status.usable, status.count)}`]
  if (status.active) parts.push(`${t('inUse')}: ${status.active.label}`)
  if (status.state === 'exhausted' && status.next_ready_at) parts.push(t('readyAt', localTime(status.next_ready_at)))
  return parts.join('\n')
}

function Chip() {
  const t = usePluginI18n(ID)
  const { data, error, isLoading } = useStatus()
  const [open, setOpen] = useState(false)

  if (isLoading && !data) return null
  if (error && !data) {
    return jsxs('button', {
      type: 'button',
      className: CHIP_CLASS,
      title: `${t('loadFailed')}: ${error.message || error}\n${t('retry')}`,
      onClick: refresh,
      children: [jsx(StatusDot, { tone: 'muted' }), jsx('span', { className: 'truncate', children: t('title') })]
    })
  }

  const trigger = jsxs('button', {
    type: 'button',
    className: cn(CHIP_CLASS, data.state === 'setup' && 'text-foreground'),
    title: chipTitle(data, t),
    children: [
      jsx(StatusDot, { tone: TONE[data.state] || 'muted' }),
      jsx('span', { className: 'truncate', children: chipLabel(data, t) })
    ]
  })

  if (!Popover || !PopoverTrigger || !PopoverContent) return trigger
  return jsxs(Popover, {
    open,
    onOpenChange: (next) => {
      setOpen(next)
      if (next) refresh()
    },
    children: [
      jsx(PopoverTrigger, { asChild: true, children: trigger }),
      jsx(PopoverContent, {
        side: 'top',
        align: 'end',
        className: 'w-auto p-3',
        children: jsx(Panel, { status: data, t })
      })
    ]
  })
}

export default {
  id: ID,
  name: 'Account switcher',
  register(ctx) {
    ctx.i18n.register(MESSAGES)
    ctx.register({ id: 'status', area: STATUSBAR_AREAS.right, order: 110, render: () => jsx(Chip, {}) })
    ctx.onEvent(`plugin.${ID}.pool.changed`, refreshAll)
  }
}
