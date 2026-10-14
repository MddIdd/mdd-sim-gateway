import React, { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { useI18n } from '../i18n.jsx'
import AllowancePanel from './AllowancePanel.jsx'

// A number is kept alive by CHARGEABLE use, not by being registered: carriers reclaim numbers
// that never bill. So this page answers three questions per line — is it still on a network,
// is it still funded, and is anything keeping it used — and nothing else. Connection quality
// lives on the device page and is deliberately not repeated here.

const DAY = 86400
const KEEPALIVE_MAX_INTERVAL_DAYS = 365

const fmtDate = (ts) => (ts ? new Date(ts * 1000).toLocaleDateString() : '—')
const fmtDateTime = (ts) => (ts ? new Date(ts * 1000).toLocaleString() : '—')

function ago(ts, t) {
  if (!ts) return null
  const days = Math.floor((Date.now() / 1000 - ts) / DAY)
  if (days <= 0) return t('today')
  return t('{days}d ago', { days })
}

function Cell({ tone = '', children, sub }) {
  const color = tone === 'crit' ? '#dc2626' : tone === 'warn' ? '#b45309'
    : tone === 'ok' ? '#15803d' : undefined
  return <div style={{ minWidth: 0 }}>
    <div style={{ color, fontWeight: tone ? 600 : 400 }}>{children}</div>
    {sub ? <span style={{ display: 'block', color: tone === 'crit' ? '#dc2626' : 'var(--text-mute)', fontSize: 11, marginTop: 2 }}>{sub}</span> : null}
  </div>
}

function KeepaliveForm({ line, onSaved, showToast }) {
  const { t } = useI18n()
  const [draft, setDraft] = useState(line.keepalive)
  const [busy, setBusy] = useState(false)
  const dirty = useRef(false)
  // Summary polling replaces `line.keepalive` with a fresh object every 30 seconds. Keep
  // server-side status fresh while the form is untouched, but never erase a draft the user is
  // still editing. Closing the row unmounts the form, so reopening always starts from the server.
  useEffect(() => {
    if (!dirty.current) setDraft(line.keepalive)
  }, [line.keepalive])
  const set = (patch) => {
    dirty.current = true
    setDraft(d => ({ ...d, ...patch }))
  }
  const sms = draft.action === 'sms'
  const save = async () => {
    setBusy(true)
    try {
      const result = await api.saveKeepalive(line.instance, draft)
      dirty.current = false
      setDraft(result.keepalive); showToast?.(t('Saved')); onSaved?.()
    } catch (error) { showToast?.(error.message) } finally { setBusy(false) }
  }
  const runNow = async () => {
    // Spends real money on a real SIM, so it asks first rather than firing on one click.
    if (!window.confirm(sms
      ? t('Send one chargeable SMS on this line right now?')
      : t('Query this line’s balance right now?'))) return
    setBusy(true)
    try {
      const result = await api.runKeepalive(line.instance)
      dirty.current = false
      setDraft(result.keepalive); showToast?.(t('Done')); onSaved?.()
    } catch (error) { showToast?.(error.message) } finally { setBusy(false) }
  }
  return <div className="card u-panel">
    <h4 style={{ margin: '0 0 4px', fontSize: 14 }}>{t('Number keeping')}</h4>
    <p className="u-hint" style={{ margin: '0 0 12px' }}>
      {t('Carriers judge a number active by chargeable use. A prepaid SIM needs a paid action; a plan SIM renews itself and only needs enough balance to pay the next cycle.')}
    </p>
    <label><input type="checkbox" className="u-toggle" checked={!!draft.enabled}
      onChange={e => set({ enabled: e.target.checked })} />{t('Enable number keeping')}</label>
    <div className="u-form-grid" style={{ marginTop: 10 }}>
      <div>
        <label>{t('Keeping method')}</label>
        <select value={draft.action} onChange={e => set({ action: e.target.value })}>
          <option value="sms">{t('Send a chargeable SMS (prepaid SIM)')}</option>
          <option value="balance_watch">{t('Watch the balance, warn when low (plan SIM)')}</option>
        </select>
      </div>
      <div>
        <label>{t('Next run on')}</label>
        <input type="date" value={draft.next_run_date || ''}
          onChange={e => set({ next_run_date: e.target.value })} />
      </div>
    </div>
    <div className="u-form-grid" style={{ marginTop: 10 }}>
      <div>
        <label>{t('Then every (days)')}</label>
        <input type="number" min="1" max={KEEPALIVE_MAX_INTERVAL_DAYS} value={draft.interval_days}
          onChange={e => set({ interval_days: Number(e.target.value) })} />
      </div>
      <div />
    </div>
    {draft.enabled ? (
      <div className="u-note" style={{ marginTop: 10 }}>
        {draft.next_run_date
          ? t('Runs on {date} within 10:00–22:00 local time, then every {days} days after each successful run.',
            { date: draft.next_run_date, days: draft.interval_days })
          : t('Runs at the next opportunity, then every {days} days.', { days: draft.interval_days })}
        {line.keepalive.last_run_ts
          ? ` ${t('Last run {date}.', { date: fmtDate(line.keepalive.last_run_ts) })}` : ''}
      </div>
    ) : null}
    {sms ? <>
      <div className="u-form-grid" style={{ marginTop: 10 }}>
        <div><label>{t('Recipient number')}</label>
          <input className="mono" value={draft.sms_to}
            onChange={e => set({ sms_to: e.target.value })} /></div>
        <div><label>{t('Message body')}</label>
          <input className="mono" value={draft.sms_body}
            onChange={e => set({ sms_body: e.target.value })} /></div>
      </div>
      <label style={{ marginTop: 8 }}>
        <input type="checkbox" className="u-toggle" checked={!!draft.verify_charge}
          onChange={e => set({ verify_charge: e.target.checked })} />
        {t('Check the balance 10 minutes later to confirm the charge (an unchanged balance is not a failure)')}
      </label>
      <p className="u-hint">{t('Point this at another line on this gateway when you can: the far end receiving it confirms both send and receive in one go. The SMS is billed at your carrier’s rate.')}</p>
    </> : <>
      <label style={{ marginTop: 10 }}>{t('Balance threshold (next cycle’s fee)')}</label>
      <input className="mono" style={{ maxWidth: 180 }} value={draft.threshold}
        onChange={e => set({ threshold: e.target.value })} />
      <p className="u-hint">
        {line.has_query_rule
          ? t('The balance is read using this line’s existing query rule. Below the threshold you get a “balance low” notification, repeated every 3 days until it recovers.')
          : t('This line has no balance query rule yet — set one up in the allowance panel first, otherwise there is nothing to read the balance with.')}
      </p>
    </>}
    <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 12 }}>
      <button className="btn btn-ghost" disabled={busy || !line.keepalive.enabled}
        onClick={runNow}>{t('Run once now')}</button>
      <button className="btn btn-primary" disabled={busy} onClick={save}>{t('Save')}</button>
    </div>
  </div>
}

function AbsentLines({ lines, onChanged, showToast }) {
  // Kept visible, and kept out of the main table: these SIMs cannot be sent anything, but a
  // SIM sitting in a drawer is exactly the one a carrier reclaims, so its expiry is the most
  // useful thing this page can tell you. Deleting is offered here because this is also where
  // stale lines accumulate — ported-away numbers, early test entries — and the device page
  // cannot reach a line whose reader is absent.
  const { t } = useI18n()
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState('')

  const remove = async (line) => {
    const label = `${line.name}${line.msisdn ? ` (${line.msisdn})` : ''}`
    if (!window.confirm(t('Delete SIM line “{name}” (ID {id})? Its IMS settings, saved PIN and runtime files are removed. Messages and call records are kept.',
      { name: label, id: line.instance }))) return
    const typed = window.prompt(t('Type the line ID “{id}” to confirm deletion.', { id: line.instance }), '')
    if (String(typed || '').trim() !== String(line.instance)) {
      if (typed !== null) window.alert(t('Line ID did not match. Nothing was deleted.'))
      return
    }
    setBusy(line.instance)
    try {
      // History is preserved: these are old numbers, and their message log is often the only
      // remaining record of what they were used for.
      await api.deleteInstance(line.instance, false)
      showToast?.(t('SIM line deleted; history preserved.'))
      onChanged?.()
    } catch (error) { showToast?.(error.message) } finally { setBusy('') }
  }

  return <div className="card" style={{ overflowX: 'auto' }}>
    <button onClick={() => setOpen(o => !o)}
      style={{ width: '100%', display: 'flex', alignItems: 'center', gap: 10, padding: '14px 16px',
        border: 0, background: 'transparent', color: 'var(--text)', cursor: 'pointer', textAlign: 'left' }}>
      <span style={{ color: 'var(--text-mute)', fontSize: 11 }}>{open ? '▾' : '▸'}</span>
      <b style={{ fontSize: 14 }}>{t('Not in the gateway ({count})', { count: lines.length })}</b>
      <span style={{ color: 'var(--text-mute)', fontSize: 12 }}>
        {t('These SIMs are not in a reader or modem right now, so number keeping cannot run for them.')}
      </span>
    </button>
    {open && <div style={{ borderTop: '1px solid var(--border)' }}>
      {lines.map(line => {
        const expiry = line.days_to_expiry
        const tone = expiry === null ? '' : expiry <= 3 ? 'crit' : expiry <= 7 ? 'warn' : ''
        return <div key={line.instance} className="hover-row u-keepalive-absent-row"
          style={{ padding: '11px 16px', fontSize: 13,
            borderBottom: '1px solid var(--border)', opacity: .75 }}>
          <Cell sub={line.msisdn || t('number unknown')}>
            <b style={{ fontSize: 14 }}>{line.name}</b>
            {line.carrier ? <span style={{ color: 'var(--text-mute)', fontWeight: 400 }}> · {line.carrier}</span> : null}
          </Cell>
          <Cell sub={t('SIM not in the gateway')}>
            <span className="u-badge cap-off"><i className="u-dot" />{t('Absent')}</span>
          </Cell>
          <Cell sub={line.allowance?.updated_ts ? fmtDate(line.allowance.updated_ts) : t('not queried')}>
            {line.allowance?.balance || <span style={{ color: 'var(--text-mute)' }}>{t('Unknown')}</span>}
          </Cell>
          <Cell tone={tone} sub={expiry !== null ? t('{days} days left', { days: expiry }) : null}>
            {line.allowance?.valid_until || <span style={{ color: 'var(--text-mute)' }}>{t('Unknown')}</span>}
          </Cell>
          <button className="btn btn-danger-outline" style={{ padding: '4px 12px', fontSize: 12 }}
            disabled={busy === line.instance} onClick={() => remove(line)}>
            {busy === line.instance ? t('Deleting…') : t('Delete')}
          </button>
        </div>
      })}
      <p className="u-hint" style={{ padding: '10px 16px' }}>
        {t('Insert one of these SIMs into a reader or modem and its line becomes manageable again. Delete is for lines you no longer own — a ported-away number, or an entry left over from testing.')}
      </p>
    </div>}
  </div>
}

export default function Keepalive({ showToast }) {
  const { t } = useI18n()
  const [rows, setRows] = useState(null)
  const [loadError, setLoadError] = useState(false)
  const [open, setOpen] = useState(null)

  const load = useCallback(async () => {
    try { setRows((await api.keepaliveSummary()).lines || []); setLoadError(false) }
    catch (error) { showToast?.(error.message); setLoadError(true) }
  }, [showToast])
  useEffect(() => { load(); const id = setInterval(load, 30000); return () => clearInterval(id) }, [load])

  if (rows === null) return <p className={loadError ? 'u-error' : ''}>{t(loadError ? 'Loading failed' : 'Loading')} {!loadError && '…'}</p>
  if (!rows.length) return <div className="u-empty"><div className="u-empty-icon">◷</div>
    <h3>{t('No lines yet')}</h3><p>{t('Add a SIM line first; its balance and number keeping show up here.')}</p></div>

  // There are always more configured lines than card slots. Counting a SIM that is out of the
  // gateway as "needs attention" would show a number nobody can act on, so the keeping metrics
  // are scoped to what is actually in the box. Expiry is not: a SIM expiring on a shelf is a
  // real loss, and arguably the one most worth surfacing.
  const present = rows.filter(r => r.in_gateway)
  const absent = rows.filter(r => !r.in_gateway)
  const metrics = {
    total: present.length,
    ok: present.filter(r => r.keepalive.enabled && !['failed', 'balance_low'].includes(r.keepalive.last_status)).length,
    attention: present.filter(r => !r.keepalive.enabled || ['failed', 'balance_low'].includes(r.keepalive.last_status)).length,
    expiring: rows.filter(r => r.days_to_expiry !== null && r.days_to_expiry <= 7).length,
  }

  const statusCell = (line) => {
    const k = line.keepalive
    if (!k.enabled) {
      const idle = k.last_registered_ts ? Math.floor((Date.now() / 1000 - k.last_registered_ts) / DAY) : null
      return <Cell tone="warn">{t('Off')}
        {idle !== null && idle > 60 ? <></> : null}</Cell>
    }
    const map = {
      ok: ['ok', t('OK')], failed: ['crit', t('Failed')],
      balance_low: ['crit', t('Balance low')],
      skipped_offline: ['warn', t('Skipped (offline)')],
      sent_unconfirmed: ['warn', t('Sent, unconfirmed')],
    }
    const [tone, label] = map[k.last_status] || ['', t('Scheduled')]
    return <Cell tone={tone} sub={`${t('Due')} ${fmtDate(k.next_due_ts)}`}>{label}</Cell>
  }

  return <div className="u-page">
    <div className="u-metrics">
      <div className="u-metric"><span>{t('Lines')}</span><strong>{metrics.total}</strong></div>
      <div className="u-metric"><span>{t('Keeping OK')}</span><strong style={{ color: '#15803d' }}>{metrics.ok}</strong></div>
      <div className="u-metric"><span>{t('Needs attention')}</span><strong style={{ color: '#b45309' }}>{metrics.attention}</strong></div>
      <div className="u-metric"><span>{t('Expiring in 7 days')}</span><strong style={{ color: metrics.expiring ? '#dc2626' : undefined }}>{metrics.expiring}</strong></div>
    </div>

    <div className="card" style={{ overflowX: 'auto' }}>
      <div className="u-keepalive-row u-keepalive-head" style={{ color: 'var(--text-mute)', fontSize: 11, fontWeight: 700, borderBottom: '1px solid var(--border)' }}>
        <div>{t('Line')}</div><div>{t('Network')}</div><div>{t('Last online')}</div>
        <div>{t('Balance')}</div><div>{t('Plan expires')}</div><div>{t('Number keeping')}</div><div />
      </div>
      {present.map(line => {
        const isOpen = open === line.instance
        const online = line.state === 'OK'
        const expiry = line.days_to_expiry
        const expiryTone = expiry === null ? '' : expiry <= 3 ? 'crit' : expiry <= 7 ? 'warn' : ''
        return <React.Fragment key={line.instance}>
          <div className="u-keepalive-row" style={{ borderBottom: '1px solid var(--border)', fontSize: 13, cursor: 'pointer', background: isOpen ? 'var(--hover)' : undefined }}
            onClick={() => setOpen(isOpen ? null : line.instance)}>
            <Cell sub={line.msisdn || t('number unknown')}><b style={{ fontSize: 14 }}>{line.name}</b>
              {line.carrier ? <span style={{ color: 'var(--text-mute)', fontWeight: 400 }}> · {line.carrier}</span> : null}</Cell>
            <div><span className={`u-badge ${online ? 'cap-on' : 'cap-error'}`}><i className="u-dot" />{online ? t('Online') : t('Offline')}</span></div>
            <Cell tone={online ? 'ok' : 'crit'}
              sub={line.uptime_ratio !== null && line.uptime_ratio !== undefined
                ? t('{pct}% uptime', { pct: Math.round(line.uptime_ratio * 100) }) : null}>
              {online ? t('Online now') : (ago(line.keepalive.last_registered_ts, t) || t('Unknown'))}
            </Cell>
            <Cell sub={line.allowance?.updated_ts ? fmtDate(line.allowance.updated_ts) : t('not queried')}>
              {line.allowance?.balance || <span style={{ color: 'var(--text-mute)' }}>{t('Unknown')}</span>}
            </Cell>
            <Cell tone={expiryTone} sub={expiry !== null ? t('{days} days left', { days: expiry }) : null}>
              {line.allowance?.valid_until || <span style={{ color: 'var(--text-mute)' }}>{t('Unknown')}</span>}
            </Cell>
            {statusCell(line)}
            <div style={{ color: 'var(--text-mute)', fontSize: 11, textAlign: 'center' }}>{isOpen ? '▾' : '▸'}</div>
          </div>
          {isOpen ? <div style={{ borderBottom: '1px solid var(--border)', background: 'var(--hover)', padding: '16px 18px' }} className="u-keepalive-detail-grid">
            <div>
              <div className="card u-panel">
                <h4 style={{ margin: '0 0 8px', fontSize: 14 }}>{t('Balance and allowance')}</h4>
                <AllowancePanel instanceId={line.instance} showToast={showToast} />
              </div>
              <div className="card u-panel" style={{ marginTop: 12 }}>
                <h4 style={{ margin: '0 0 8px', fontSize: 14 }}>{t('Last run')}</h4>
                {line.keepalive.last_run_ts
                  ? <div className="u-detail" style={{ borderBottom: 0 }}>
                    <span>{fmtDateTime(line.keepalive.last_run_ts)}</span>
                    <b>{line.keepalive.last_detail || line.keepalive.last_status}</b></div>
                  : <p className="u-muted" style={{ margin: 0, fontSize: 12 }}>{t('Never run yet.')}</p>}
              </div>
            </div>
            <div onClick={e => e.stopPropagation()}>
              <KeepaliveForm line={line} onSaved={load} showToast={showToast} />
            </div>
          </div> : null}
        </React.Fragment>
      })}
    </div>
    {absent.length > 0 && <AbsentLines lines={absent} onChanged={load} showToast={showToast} />}

    <p className="u-hint">
      {t('Number keeping performs one real, chargeable action on your SIM at the interval you set — charged at your carrier’s rate. A free balance lookup does not count as usage with most carriers and cannot keep a prepaid number alive.')}
    </p>
  </div>
}
