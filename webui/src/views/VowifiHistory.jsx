import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api.js'
import { useI18n } from '../i18n.jsx'

// Connectivity timeline for one VoWiFi line. The backend records merged up/down segments and
// reports the periods it was not running as `unknown`, so this only has to draw what it is
// given — it never interpolates across a hole in the record.
const STATES = ['up', 'down', 'off', 'unknown']
const STATE_LABEL = {
  up: 'Connected', down: 'Disconnected', off: 'Stopped', unknown: 'Not recorded',
}
// Short labels for the status machine's reason codes — the cause a down segment began with.
// An unmapped code falls back to itself rather than hiding information.
const REASON_LABEL = {
  no_card: 'SIM status unavailable', wrong_card: "Reader holds another line's SIM",
  pin_wrong: 'PIN incorrect', pin_blocked: 'PIN blocked (PUK required)',
  pin_required: 'PIN required', epdg_unresolved: 'ePDG address unresolved',
  tunnel_network: 'Server ePDG did not answer IKE',
  tunnel_child_rekey_timeout: 'Server ePDG did not answer CHILD_SA rekey',
  tunnel_ike_rekey_timeout: 'Server ePDG did not answer IKE_SA rekey',
  tunnel_rekey_send_error: 'Client failed to send IPsec rekey',
  tunnel_sim_auth: 'Client SIM authentication failed',
  tunnel_not_authorized: 'Server ePDG rejected client network/identity',
  tunnel_proposal: 'Server ePDG rejected client IKE proposal',
  tunnel_setup: 'Tunnel unavailable — exact cause not captured',
  registering: 'Client has not completed IMS registration',
  reg_rejected: 'Server P-CSCF rejected IMS registration',
  reg_reauth_failed: 'Client did not complete IMS re-authentication',
  reg_unanswered: 'Server P-CSCF did not answer IMS registration',
  maintenance_rebuild: 'Client maintenance restarted the line',
  client_engine_failure: 'Client Engine failed to establish the tunnel',
}
const reasonLabel = (reason, t) => (reason ? t(REASON_LABEL[reason] || reason) : '—')
const EVIDENCE_LABEL = {
  client_dns_unresolved: 'Client could not resolve ePDG {peer}; DNS servers: {servers}',
  server_epdg_child_rekey_unanswered: 'Server ePDG {peer} did not answer client CREATE_CHILD_SA after all retries',
  server_epdg_ike_rekey_unanswered: 'Server ePDG {peer} did not answer client IKE_SA rekey after all retries',
  client_rekey_send_failed: 'Client failed to send the IPsec rekey request to ePDG {peer}',
  server_epdg_ike_unanswered: 'Server ePDG {peer} did not answer the client IKE request',
  client_sim_auth_failed: 'Client SIM did not complete EAP-AKA authentication with ePDG {peer}',
  server_epdg_identity_rejected: 'Server ePDG {peer} rejected the client network or identity',
  server_epdg_proposal_rejected: 'Server ePDG {peer} rejected the client IKE proposal',
  tunnel_cause_not_captured: 'The IPsec tunnel to ePDG {peer} was unavailable; no earlier failure evidence was captured',
  // Compatibility for records written before recovery actions stopped being used as causes.
  client_tunnel_rebuilding: 'The IPsec tunnel to ePDG {peer} was unavailable; no earlier failure evidence was captured',
  server_pcscf_register_unanswered: 'Server P-CSCF {peer} did not answer client SIP REGISTER',
  server_pcscf_sip_rejected: 'Server P-CSCF {peer} returned SIP {status}',
  server_pcscf_401_missing_security_server: 'Server P-CSCF {peer} returned SIP 401 without Security-Server; client re-authentication did not complete',
  client_registration_incomplete: 'Client did not complete IMS registration with P-CSCF {peer}',
  client_maintenance_rebuild: 'A client-side maintenance or deployment restarted the line',
  client_engine_worker_failed: 'The client Engine data-plane worker failed to start',
}
function evidenceLabel(detail, t) {
  if (!detail) return ''
  try {
    const evidence = JSON.parse(detail)
    const key = EVIDENCE_LABEL[evidence.code]
    if (!key) return ''
    return t(key, { ...evidence, servers: (evidence.servers || []).join(', ') || '—',
      status: evidence.status || '—', peer: evidence.peer || '—' })
  } catch (_) { return '' }
}
function outageCause(segment, t) {
  const evidence = evidenceLabel(segment.detail, t)
  if (evidence) return { summary: evidence, legacyDetail: '' }
  // Structured evidence is internal data. If a future code is not yet mapped, retain the
  // readable reason without exposing its JSON payload in the UI. Plain-text legacy detail
  // remains visible because it may be the only diagnostic evidence on older segments.
  let structured = false
  try { structured = Boolean(segment.detail && JSON.parse(segment.detail)?.code) } catch (_) { /* legacy */ }
  return {
    summary: reasonLabel(segment.reason, t),
    legacyDetail: structured ? '' : segment.detail,
  }
}
const TICK_STEPS = [900, 1800, 3600, 2 * 3600, 3 * 3600, 6 * 3600, 12 * 3600, 24 * 3600]
const REFRESH_MS = 30000

function tickStep(span, maxTicks) {
  return TICK_STEPS.find(step => span / step <= maxTicks) || TICK_STEPS[TICK_STEPS.length - 1]
}

/** Axis ticks on local-time boundaries, so labels land on :00 rather than on the window edge. */
function axisTicks(start, end, maxTicks = 7) {
  const step = tickStep(Math.max(60, end - start), maxTicks)
  const shift = -new Date().getTimezoneOffset() * 60
  const ticks = []
  for (let ts = Math.ceil((start + shift) / step) * step - shift; ts <= end; ts += step) {
    if (ts >= start) ticks.push({ ts })
  }
  return ticks
}

function clockLabel(ts, language) {
  return new Date(ts * 1000).toLocaleTimeString(language === 'zh' ? 'zh-CN' : 'en-GB',
    { hour: '2-digit', minute: '2-digit', hour12: false })
}

function dayLabel(ts, language) {
  return new Date(ts * 1000).toLocaleDateString(language === 'zh' ? 'zh-CN' : 'en-GB',
    { month: 'short', day: 'numeric' })
}

function stampLabel(ts, language) {
  return `${dayLabel(ts, language)} ${clockLabel(ts, language)}`
}

/** Two largest units — "1 h 20 min" reads better than 4800 s or a bare "1 h". */
function duration(seconds, t) {
  const total = Math.max(0, Math.round(seconds))
  const units = [['{count} d', 86400], ['{count} h', 3600], ['{count} min', 60], ['{count} s', 1]]
  const parts = []
  let rest = total
  for (const [key, size] of units) {
    const value = Math.floor(rest / size)
    rest -= value * size
    if (value) parts.push(t(key, { count: value }))
    if (parts.length === 2) break
  }
  return parts.length ? parts.join(' ') : t('{count} s', { count: 0 })
}

export default function VowifiHistory({ instanceId, subscribe, compact = false }) {
  const { t, language } = useI18n()
  const [data, setData] = useState(null)
  const [error, setError] = useState('')
  const [hover, setHover] = useState(null)
  const requestGeneration = useRef(0)

  const load = useCallback(() => {
    const generation = ++requestGeneration.current
    if (!instanceId) return
    api.lineAvailability(instanceId)
      .then(result => {
        if (generation !== requestGeneration.current) return
        setData(result); setError('')
      })
      .catch(err => {
        if (generation !== requestGeneration.current) return
        setError(err.message)
      })
  }, [instanceId])

  useEffect(() => {
    setData(null); setHover(null); setError('')
    load()
    const timer = setInterval(load, REFRESH_MS)
    return () => {
      ++requestGeneration.current
      clearInterval(timer)
    }
  }, [load])

  // Status events arrive every few seconds whether or not anything changed. Only a real
  // transition is worth a refetch; the interval bounds how stale the right edge can get.
  const lastState = useRef(null)
  useEffect(() => { lastState.current = null }, [instanceId])
  useEffect(() => subscribe?.(msg => {
    if (msg.type !== 'status' || String(msg.instance) !== String(instanceId)) return
    const state = String(msg.state || '')
    if (lastState.current !== null && lastState.current !== state) load()
    lastState.current = state
  }), [subscribe, instanceId, load])

  const segments = data?.segments || []
  const span = Math.max(1, (data?.end || 0) - (data?.start || 0))
  const ticks = useMemo(() => (data ? axisTicks(data.start, data.end, compact ? 5 : 7) : []),
    [data?.start, data?.end, compact]) // eslint-disable-line react-hooks/exhaustive-deps
  const outages = useMemo(() => segments.filter(s => s.state === 'down')
    .slice(-20).reverse(), [segments])

  // On the overview a card without a line has nothing to say; the device page explains why.
  if (!instanceId) {
    return compact ? null : <div className="u-note">{t('Connectivity history starts once this device has a configured SIM line.')}</div>
  }
  if (error) return compact ? null : <div className="u-note u-error">{`${t('Error')}: ${error}`}</div>
  if (!data) return <div className="u-note">{t('Loading')}…</div>

  const summary = data.summary || {}
  const ratio = summary.uptime_ratio
  const uptime = ratio == null ? '—' : `${(ratio * 100).toFixed(ratio > 0.999 ? 2 : 1)}%`
  const present = STATES.filter(state => (summary[state] || 0) > 0)
  const ariaLabel = present.map(state =>
    `${t(STATE_LABEL[state])} ${duration(summary[state], t)}`).join('; ')
  // The compact card has no room for states this window never entered; the full view keeps
  // all four so the vocabulary stays stable while you read across lines.
  const legend = compact ? (present.length ? present : ['up']) : STATES

  return <div className={`u-uptime ${compact ? 'compact' : ''}`}>
    <div className="u-uptime-head">
      <div>
        <h4>{t('Connection history')}</h4>
        <p>{compact
          ? t('Past {window}', { window: duration(span, t) })
          : t('Showing the past {window}, recorded every few seconds. Up to 2 days is kept.',
            { window: duration(span, t) })}</p>
      </div>
      <div className="u-uptime-figure">
        <strong>{uptime}</strong>
        <span>{t('Connected while observed')}</span>
      </div>
    </div>

    <div className="u-uptime-legend">
      {legend.map(state => <span key={state} className={`u-uptime-key is-${state}`}>
        <i />{t(STATE_LABEL[state])}
      </span>)}
    </div>

    <div className="u-uptime-plot">
      <div className="u-uptime-track" role="img" aria-label={ariaLabel}
        onMouseLeave={() => setHover(null)}>
        {segments.map((segment, index) => {
          const left = ((segment.start - data.start) / span) * 100
          const width = ((segment.end - segment.start) / span) * 100
          return <div key={`${segment.start}-${index}`}
            className={`u-uptime-seg is-${segment.state}`}
            style={{ left: `${left}%`, width: `${width}%` }}
            onMouseEnter={() => setHover({ ...segment, center: left + width / 2 })} />
        })}
      </div>
      {hover && <div className="u-uptime-tip" style={{ left: `${hover.center}%` }}>
        <b>{t(STATE_LABEL[hover.state])}</b>
        {hover.state === 'down' && hover.reason && <span>{outageCause(hover, t).summary}</span>}
        <span>{duration(hover.end - hover.start, t)}</span>
        <span>{stampLabel(hover.start, language)} → {clockLabel(hover.end, language)}</span>
      </div>}
      <div className="u-uptime-axis">
        {ticks.map(({ ts }) => {
          const at = ((ts - data.start) / span) * 100
          // A tick sitting on a window edge is pulled inside it instead of being clipped.
          const anchor = at < 5 ? 'translateX(0)' : at > 95 ? 'translateX(-100%)' : 'translateX(-50%)'
          return <span key={ts} style={{ left: `${at}%`, transform: anchor }}>
            {new Date(ts * 1000).getHours() === 0 ? dayLabel(ts, language) : clockLabel(ts, language)}
          </span>
        })}
      </div>
    </div>

    <div className="u-uptime-facts">
      <span>{t('Outages')}: <b>{summary.outages || 0}</b></span>
      <span>{t('Longest outage')}: <b>{summary.longest_outage_seconds
        ? duration(summary.longest_outage_seconds, t) : '—'}</b></span>
      {!compact && <span>{t('Observed')}: <b>{duration(summary.observed_seconds || 0, t)}</b></span>}
    </div>

    {!compact && <details className="u-uptime-table">
      <summary>{t('Outage list')}</summary>
      {outages.length ? <table><thead><tr>
        <th>{t('Started')}</th><th>{t('Ended')}</th><th>{t('Duration')}</th><th>{t('Reason')}</th>
      </tr></thead><tbody>
        {outages.map((segment, index) => {
          const cause = outageCause(segment, t)
          return <tr key={`${segment.start}-${index}`}>
            <td>{stampLabel(segment.start, language)}</td>
            <td>{segment.end >= data.end ? t('Ongoing') : stampLabel(segment.end, language)}</td>
            <td>{duration(segment.end - segment.start, t)}</td>
            <td>{cause.summary}
              {cause.legacyDetail && <small className="u-outage-detail">{cause.legacyDetail}</small>}</td>
          </tr>
        })}
      </tbody></table> : <p className="u-muted">{t('No disconnection was recorded in this window.')}</p>}
    </details>}
  </div>
}
