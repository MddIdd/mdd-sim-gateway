import React, { useEffect, useId, useMemo, useRef, useState } from 'react'
import { api } from '../api.js'
import { useI18n } from '../i18n.jsx'
import SimConfig from './SimConfig.jsx'
import Logs from './Logs.jsx'
import VowifiHistory from './VowifiHistory.jsx'

const CAP_STATES = ['off', 'starting', 'on', 'stopping', 'degraded', 'error', 'unsupported']

function normalizeState(value, desired) {
  const raw = typeof value === 'object' ? (value.actual || value.state) : value
  const state = String(raw || (desired ? 'starting' : 'off')).toLowerCase()
  return CAP_STATES.includes(state) ? state : (['ok', 'working', 'registered', 'connected', 'active'].includes(state) ? 'on' : 'off')
}

function capability(device, key) {
  const cap = device?.capabilities?.[key] || device?.[key] || {}
  const desired = typeof cap === 'object' ? !!(cap.desired ?? cap.enabled) : !!cap
  return { desired, actual: normalizeState(cap, desired), reason: cap.reason || cap.error || '', available: cap.available !== false }
}

function supportsCellular(device) {
  return device?.device_type !== 'reader' && capability(device, 'cellular').actual !== 'unsupported'
}

function exitNodeLabel(device, t) {
  // The node picker lives on the settings page. Showing the running node here without saying
  // it disagrees with the pinned one reads as "my setting was ignored".
  const exit = device?.egress || {}
  if (!exit.node) return t('Not connected')
  if (!exit.pinned_node || exit.pinned_node === exit.node) return exit.node
  return t('{node} (not your pinned {pinned})', { node: exit.node, pinned: exit.pinned_node })
}

const REGIONAL_FLAG_PAIR = /([\u{1F1E6}-\u{1F1FF}]{2})/gu

function isRegionalFlag(value) {
  const points = [...String(value || '')]
  if (points.length !== 2) return false
  const codes = points.map(point => point.codePointAt(0))
  return codes.every(code => code >= 0x1F1E6 && code <= 0x1F1FF)
}

function ProxyNodeName({ text }) {
  return <>{String(text || '').split(REGIONAL_FLAG_PAIR).map((part, index) => {
    return isRegionalFlag(part)
      ? <span key={`flag-${index}`} className="u-proxy-node-flag">{part}</span>
      : <React.Fragment key={`text-${index}`}>{part}</React.Fragment>
  })}</>
}

function selectableProxyProfiles(settings) {
  return Object.entries(settings?.proxy?.profiles || {})
    .filter(([, profile]) => profile?.type !== 'subscription')
}

// Stating the mismatch without the cause reads as an unexplained override, so give the event
// that actually moved the exit: when it happened, and what the line was failing with.
function exitChangeReason(exit, t, language) {
  const change = exit?.last_change
  if (!change?.ts) return ''
  const at = new Date(change.ts * 1000).toLocaleString(language === 'zh' ? 'zh-CN' : 'en-GB',
    { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
  const why = String(change.reason || '').startsWith('health-freeze:')
    ? t('the line failed ({code})', { code: String(change.reason).split(':')[1] })
    : (change.reason || t('an automatic selection'))
  const cooldown = exit.pinned_cooldown_seconds
  return t('Moved at {at}: {why}.', { at, why })
    + (cooldown ? ' ' + t('Your pinned node is held back for another {minutes} min.',
      { minutes: Math.ceil(cooldown / 60) }) : '')
}

function Badge({ state = 'off', children }) {
  const { t } = useI18n()
  return <span className={`u-badge cap-${state}`}><span className="u-dot" />{children || t(`cap.${state}`)}</span>
}

function Empty({ title, detail }) {
  return <div className="u-empty"><div className="u-empty-icon">◇</div><h3>{title}</h3><p>{detail}</p></div>
}

function LineActivity({ device, compact = false }) {
  const { t } = useI18n()
  const status = device?.status
  if (!status) return null
  const activity = status.activity || {}
  const current = activity.current || status.label || t('Checking line status')
  const next = activity.next || ''
  const actual = capability(device, 'vowifi').actual
  const retryCount = Number(activity.retry_count || status.retry?.count || 0)
  const retryMax = Number(activity.retry_max || status.retry?.max || 0)
  return <div className={`u-line-activity ${compact ? 'compact' : ''}`}>
    <div className="u-line-activity-head"><b>{t('Backend activity')}</b><Badge state={actual}>{t(status.label || `cap.${actual}`)}</Badge></div>
    {!compact && status.reason && status.state !== 'OK' && <p className="u-line-reason"><b>{t('Reason')}:</b> {t(status.reason)}</p>}
    <div className="u-line-step"><span>{t('Now')}</span><b>{t(current)}</b></div>
    {next && <div className="u-line-step"><span>{t('Next')}</span><b>{t(next, { seconds: activity.seconds || status.automatic_retry_in || 0 })}</b></div>}
    {retryMax > 0 && status.state !== 'OK' && <div className="u-line-retry">
      <div><span>{t('Recovery progress')}</span><b>{retryCount} / {retryMax}</b></div>
      <i><span style={{ width: `${Math.min(100, (retryCount / retryMax) * 100)}%` }} /></i>
    </div>}
  </div>
}

function LogicalChannels({ value }) {
  const { t } = useI18n()
  if (!value) return null
  return <><div className="u-detail"><span>{t('SIM logical channels')}</span><b>{t('{used} / {total} allocated', { used: value.allocated ?? 0, total: value.capacity ?? 3 })} · {t(`channel.status.${value.status || 'stopped'}`)}</b></div>{(value.items || []).map(item => <div className="u-detail" key={`${item.slot}-${item.channel}`}><span>{t('Logical channel {channel}', { channel: item.channel })}</span><b>{t(`channel.role.${item.role}`)}</b></div>)}{value.error && <p className="u-error">{value.error}</p>}</>
}

export function CapabilitySwitch({ device, kind, onChanged, showToast, compact = false }) {
  const { t } = useI18n()
  const [submitting, setSubmitting] = useState(false)
  const [pendingTarget, setPendingTarget] = useState(null)
  const c = capability(device, kind)
  const pending = submitting || c.actual === 'starting' || c.actual === 'stopping'
  const unavailable = !c.available || c.actual === 'unsupported' || device.compatibilityOnly ||
    device.present === false || (kind === 'cellular' && capability(device, 'flight').desired)
  const title = kind === 'cellular' ? t('4G network') : kind === 'flight' ? t('Flight mode') : t('VoWiFi / WiFi Calling')
  const canRetry = kind === 'vowifi' && c.desired && ['off', 'degraded', 'error'].includes(c.actual) && !unavailable
  const change = async (next, retry = false) => {
    const other = capability(device, kind === 'cellular' ? 'vowifi' : 'cellular')
    const impact = retry
      ? t('Restart the VoWiFi line now? The SIM, ePDG and IMS connection will be rebuilt.')
      : kind === 'cellular' && other.desired
      ? t('Changing 4G rebuilds SIM access. VoWiFi may reconnect for 20–60 seconds. Continue?')
      : t('{action} {name}? The UI will wait for the real device state.', { action: next ? t('Enable') : t('Disable'), name: title })
    if (!window.confirm(impact)) return
    setPendingTarget(next)
    setSubmitting(true)
    try {
      const field = kind === 'flight' ? 'flight_mode' : `${kind}_enabled`
      await api.patchDeviceCapabilities(device.id, { [field]: next })
      showToast?.(t('Request accepted; waiting for device state'))
      await onChanged?.()
    } catch (e) {
      // The start path refuses with a structured 409 when the SIM PIN preflight fails.
      // pin_required / pin_invalid are recoverable right here: ask for the PIN and retry
      // the start with it (the device-level "enabled" intent was already persisted).
      const code = e.data?.detail?.code
      if ((code === 'pin_required' || code === 'pin_invalid') && next && device.instance_id) {
        const tries = e.data.detail.tries ?? '?'
        const pin = window.prompt(code === 'pin_invalid'
          ? t('The saved SIM PIN was rejected ({tries} tries left). Enter the SIM PIN:', { tries })
          : t('This SIM requires a PIN ({tries} tries left). Enter the SIM PIN:', { tries }))
        if (pin) {
          try {
            await api.start(device.instance_id, { pin })
            showToast?.(t('Request accepted; waiting for device state'))
            await onChanged?.()
          } catch (e2) {
            showToast?.(`${t('Capability change failed')}: ${e2.message}`)
          }
          return
        }
      }
      if (code === 'no_card') {
        showToast?.(`${t('Capability change failed')}: ${t('No readable SIM for this line — the reader is empty or holds another card/eSIM profile. For an eSIM, switch the active profile to this line first.')}`)
        return
      }
      showToast?.(`${t('Capability change failed')}: ${e.status === 404 ? t('Unified device control is not available on this backend') : e.message}`)
    } finally { setSubmitting(false); setPendingTarget(null) }
  }
  const toggle = () => change(!c.desired)
  const displayedDesired = pendingTarget == null ? c.desired : pendingTarget
  const displayedState = pendingTarget == null ? c.actual : (pendingTarget ? 'starting' : 'stopping')
  // A healthy line is reported by two feeds: the periodic device snapshot and live status
  // events. One includes the detailed OK reason while the other may omit it. Render one
  // canonical healthy message so those feeds cannot make the text flicker every few seconds.
  const detail = c.actual === 'on'
    ? t('Working — connected to the carrier over Wi-Fi.')
    : (c.reason ? t(c.reason) : t(`cap.help.${c.actual}`))
  return <div className={`u-capability ${compact ? 'compact' : ''}`}>
    <div><b>{title}</b><div className="u-cap-detail">{detail}</div></div>
    <div className="u-cap-actions">{canRetry && <button className="btn btn-ghost" disabled={submitting} onClick={() => change(true, true)}>{t('Restart line')}</button>}<Badge state={displayedState}>{device.present === false ? t('Offline') : null}</Badge><button className={`u-switch ${displayedDesired ? 'on' : ''}`} role="switch" aria-checked={displayedDesired}
      aria-label={title} disabled={pending || unavailable} onClick={toggle}><span /></button></div>
  </div>
}

function deviceTitle(d, index) { return d.name || d.label || d.model || `Device ${index + 1}` }
function simName(d, t) {
  if (d.present === false) return t('Device not connected')
  return d.sim?.present === false ? t('No SIM inserted') : (d.sim?.name || d.carrier || d.operator || 'SIM')
}
function carrierLabel(d, t) {
  const carrier = d.sim?.carrier || {}
  const names = []
  if (carrier.name) names.push(carrier.name)
  if (carrier.home_network && !names.some(value => value.toLowerCase() === carrier.home_network.toLowerCase())) names.push(carrier.home_network)
  const current = carrier.current_network || d.cellular?.operator || d.operator || ''
  if (current && !['--', 'unknown', 'none', 'n/a'].includes(String(current).toLowerCase()) &&
      !names.some(value => value.toLowerCase() === String(current).toLowerCase())) names.push(current)
  const name = names.join(' · ')
  return `${name || t('Unknown carrier')}${carrier.plmn ? ` (${carrier.plmn})` : ''}`
}
function deviceTypeName(d, t) { return d.device_type === 'reader' ? t('Smart-card reader') : t('Cellular modem') }
function stablePathName(d, t) {
  const path = d.stable_path || d.reader
  return path ? `USB ${path}` : t('Stable hardware path unavailable')
}
function deviceSimLine(d, t, language) {
  const name = simName(d, t)
  if (d.present === false || d.sim?.present === false) return name
  const country = d.egress?.detected_country || d.egress?.country
  return country ? `${name} · ${countryName(country, language)}` : name
}
function deviceIdentityLine(d, t) {
  if (d.present === false) return t('Device not connected')
  if (d.sim?.present === false) return t('No SIM inserted')
  const number = d.sim?.number || d.number
  return `${simName(d, t)} · ${number || t('SIM detected')}`
}

function HardwarePanel({ device, refreshDevices, showToast }) {
  const { t } = useI18n()
  const [imei, setImei] = useState(device.imei || '')
  const [saving, setSaving] = useState(false)
  useEffect(() => setImei(device.imei || ''), [device.id, device.imei])
  const isReader = device.device_type === 'reader'
  const save = async () => {
    const digits = String(imei || '').replace(/\D/g, '')
    if (digits.length !== 15) { showToast(t('IMEI must contain exactly 15 digits')); return }
    setSaving(true)
    try {
      const result = await api.saveDeviceHardware(device.id, { imei: digits })
      await refreshDevices()
      showToast(t(result.applied ? 'Hardware IMEI saved and the active line was restarted' : 'Hardware IMEI saved'))
    } catch (error) { showToast(`${t('Error')}: ${error.message}`) }
    finally { setSaving(false) }
  }
  const forget = async () => {
    if (device.present) { showToast(t('Disconnect this device before removing it')); return }
    if (!window.confirm(t('Forget this device? SIM and line configurations will be preserved.'))) return
    try {
      await api.deleteDevice(device.id)
      await refreshDevices()
      showToast(t('Device removed; SIM and line configurations were preserved'))
    } catch (error) { showToast(`${t('Error')}: ${error.message}`) }
  }
  return <div className="card u-panel u-hardware-panel"><div className="u-hardware-intro">
    <h3>{t('Hardware')}</h3>
    <p>{t('The device name identifies this hardware in the interface. Model and firmware appear only when the hardware reports them.')}</p>
  </div>
    <div className="u-details cols u-hardware-facts">
      <div className="u-detail"><span>{t('Device name')}</span><b>{deviceTitle(device, 0)}</b></div>
      <div className="u-detail"><span>{t('Device type')}</span><b>{deviceTypeName(device, t)}</b></div>
      {device.model && <div className="u-detail"><span>{t('Model')}</span><b>{device.model}</b></div>}
      {device.firmware && <div className="u-detail"><span>{t('Firmware version')}</span><b>{device.firmware}</b></div>}
      <div className="u-detail"><span>{t('Stable path')}</span><b>{stablePathName(device, t)}</b></div>
      <LogicalChannels value={device.logical_channels}/>
      {!isReader && <div className="u-detail"><span>IMEI</span><b>{device.imei_masked || t('Hardware did not report')}</b></div>}
    </div>
    {isReader && <div className="u-hardware-action u-hardware-imei">
      <div className="u-hardware-action-copy"><h4>{t('Hardware IMEI')}</h4>
        <p>{t('This IMEI belongs to the physical reader. Any SIM inserted here uses it automatically.')}</p>
      </div>
      <input className="mono" inputMode="numeric" maxLength={18} value={imei}
        onChange={event => setImei(event.target.value.replace(/[^0-9 -]/g, ''))}
        placeholder={t('15-digit IMEI required for VoWiFi')} />
      <button className="btn btn-primary" disabled={saving} onClick={save}>{t('Save')}</button>
    </div>}
    <div className="u-hardware-action u-hardware-danger">
      <div className="u-hardware-action-copy"><h4>{t('Remove device record')}</h4>
        <p>{t('Only disconnected devices can be removed; SIM and line configurations are preserved.')}</p>
      </div>
      <button className="btn btn-danger-outline" disabled={device.present} onClick={forget}>{t('Remove device')}</button>
    </div>
  </div>
}

function Discovering({ t }) {
  return <div className="u-empty"><div className="u-empty-icon u-empty-spinner">◌</div>
    <h3>{t('Detecting devices…')}</h3>
    <p>{t('The gateway is reading the connected readers and modems. This takes a few seconds after a restart.')}</p></div>
}

export function UnifiedOverview({ devices, discovering, loadErrors, refreshDevices, setView, showToast, instances, setSelectedDeviceId, setSelected, subscribe }) {
  const { t } = useI18n()
  // The backend may already know the physical devices while its first card scan is still in
  // progress. Do not render those partial rows as authoritative "No SIM" results.
  const pending = discovering
  const counts = useMemo(() => ({
    devices: devices.length,
    cellular: devices.filter(d => capability(d, 'cellular').actual === 'on').length,
    vowifi: devices.filter(d => capability(d, 'vowifi').actual === 'on').length,
    attention: devices.filter(d => ['error', 'degraded'].includes(capability(d, 'cellular').actual) || ['error', 'degraded'].includes(capability(d, 'vowifi').actual)).length,
  }), [devices])
  return <div className="u-page">
    <div className="u-metrics">
      {[[t('Devices'), counts.devices], [t('4G online'), counts.cellular], [t('VoWiFi online'), counts.vowifi], [t('Needs attention'), counts.attention]].map(([l,v]) => <div className="u-metric" key={l}><span>{l}</span><strong>{pending ? '—' : v}</strong></div>)}
    </div>
    {loadErrors?.devices && !devices.length ? <p className="u-error">{t('Loading failed')}</p> : pending ? <Discovering t={t} /> :
      !devices.length ? <Empty title={t('No communication devices found')} detail={t('Connect a modem or smart-card reader. Discovery updates automatically.')} /> :
      <div className="u-device-grid">{devices.map((d, i) => <div className="card u-device-card" key={d.id}>
        <div className="u-card-head"><div><h2>{deviceTitle(d, i)}</h2><p>{deviceIdentityLine(d, t)}</p></div><Badge state={d.present === false ? 'error' : 'on'}>{d.present === false ? t('Offline') : t('Detected')}</Badge></div>
        <div className="u-card-body">{supportsCellular(d) && <CapabilitySwitch key={`${d.id}:cellular`} device={d} kind="cellular" compact onChanged={refreshDevices} showToast={showToast} />}<CapabilitySwitch key={`${d.id}:vowifi`} device={d} kind="vowifi" compact onChanged={refreshDevices} showToast={showToast} /><LineActivity device={d} compact />{capability(d, 'vowifi').desired && <VowifiHistory instanceId={d.instance_id} subscribe={subscribe} compact />}
          <div className="u-details"><div className="u-detail"><span>{t('Carrier')}</span><b>{carrierLabel(d, t)}</b></div><div className="u-detail"><span>{t('Country exit')}</span><b className="u-proxy-node-text"><ProxyNodeName text={exitNodeLabel(d, t) || d.proxy_node || t('Not connected')} /></b></div></div>
        </div><div className="u-card-foot"><button className="btn btn-ghost" onClick={() => { if (d.instance_id) setSelected(String(d.instance_id)); setView('calls') }}>{t('Call')}</button><button className="btn btn-ghost" onClick={() => { if (d.instance_id) setSelected(String(d.instance_id)); setView('messages') }}>{t('Message')}</button><button className="btn btn-primary" onClick={() => { setSelectedDeviceId(d.id); setView('devices') }}>{t('Details')}</button></div>
      </div>)}</div>}
  </div>
}

export function DevicesPage({ devices, discovering, loadErrors, refreshDevices, instances, cards, selected, setSelected, refresh, showToast, selectedDeviceId, setSelectedDeviceId, subscribe }) {
  const { t, language } = useI18n(); const [tab, setTab] = useState('status')
  const active = devices.some(device => device.id === selectedDeviceId) ? selectedDeviceId : devices[0]?.id
  useEffect(() => { if (active && active !== selectedDeviceId) setSelectedDeviceId(active) }, [active, selectedDeviceId, setSelectedDeviceId])
  const d = devices.find(x => x.id === active)
  useEffect(() => { if (d && !supportsCellular(d) && tab === 'cellular') setTab('status') }, [d, tab])
  if (loadErrors?.devices && !devices.length) return <p className="u-error">{t('Loading failed')}</p>
  if (discovering) return <Discovering t={t} />
  if (!d) return discovering ? <Discovering t={t} /> : <Empty title={t('No communication devices found')} detail={t('Connect a modem or smart-card reader. Discovery updates automatically.')} />
  const tabs = [['status',t('Status')],['sim','SIM'],...(supportsCellular(d) ? [['cellular',t('4G network')]] : []),['vowifi','VoWiFi'],['hardware',t('Hardware')]]
  return <div className="u-split"><aside className="card u-device-list">{devices.map((x,i)=><button key={x.id} className={`u-device-option ${x.id===active?'active':''}`} onClick={()=>setSelectedDeviceId(x.id)}><b className="u-device-option-name">{deviceTitle(x,i)}</b><span className="u-device-option-sim">{deviceSimLine(x, t, language)}</span><span className="u-device-option-status"><Badge state={x.present === false ? 'error' : capability(x,'vowifi').actual} /></span></button>)}</aside>
    <section className="u-page"><div className="u-page-heading"><div><h2>{deviceTitle(d, devices.indexOf(d))}</h2><p>{deviceTypeName(d, t)} · {stablePathName(d, t)}</p></div></div><div className="u-tabs">{tabs.map(([k,l])=><button key={k} className={tab===k?'active':''} onClick={()=>setTab(k)}>{l}</button>)}</div>
      {tab==='status' && <div className="card u-panel">{supportsCellular(d) ? <><CapabilitySwitch key={`${d.id}:cellular`} device={d} kind="cellular" onChanged={refreshDevices} showToast={showToast}/><CapabilitySwitch key={`${d.id}:flight`} device={d} kind="flight" onChanged={refreshDevices} showToast={showToast}/></> : <p className="u-note">{t('This is a smart-card reader. It provides SIM access for VoWiFi and has no 4G radio.')}</p>}<CapabilitySwitch key={`${d.id}:vowifi`} device={d} kind="vowifi" onChanged={refreshDevices} showToast={showToast}/><LineActivity device={d}/><p className="u-note">{t('Cellular data, flight mode and VoWiFi are independent controls. Flight mode disables modem RF; the 4G switch only connects or disconnects mobile data.')}</p><p className="u-note">{t('Software support means the technical path is implemented. Actual availability still depends on the SIM plan, carrier, region, modem firmware and device-identity policy.')}</p></div>}
      {tab==='sim' && <div className="card u-panel"><SimConfig instances={instances} selected={selected} refresh={refresh} cards={cards} setSelected={setSelected} targetDevice={d}/></div>}
      {tab==='cellular' && <div className="card u-panel"><h3>{t('4G network')}</h3>{d.cellular ? <div className="u-details cols"><div className="u-detail"><span>{t('Registration')}</span><b>{d.cellular.registration || t('Not connected')}</b></div><div className="u-detail"><span>{t('Operator')}</span><b>{d.cellular.operator || t('Not connected')}</b></div><div className="u-detail"><span>APN</span><b>{d.cellular.apn || t('Automatic')}</b></div><div className="u-detail"><span>{t('IP address')}</span><b>{d.cellular.ip || t('Waiting')}</b></div><div className="u-detail"><span>{t('Signal')}</span><b>{d.cellular.signal == null ? t('Waiting') : `${d.cellular.signal}%`}</b></div><div className="u-detail"><span>{t('Traffic')}</span><b>↓ {formatBytes(d.cellular.rx_bytes)} · ↑ {formatBytes(d.cellular.tx_bytes)}</b></div><div className="u-detail"><span>{t('Data profile')}</span><b>{d.cellular.profile || t('Automatic')}</b></div><div className="u-detail"><span>{t('Network interface')}</span><b>{d.cellular.interface || t('Waiting')}</b></div></div>:<Empty title={t('Cellular data not connected')} detail={t('Turn on 4G to let the per-device ModemManager backend establish a data bearer.')} />}</div>}
      {tab==='vowifi' && <div className="card u-panel"><h3>VoWiFi</h3><CountryExitControl device={d} refresh={refresh} showToast={showToast}/><LineActivity device={d}/><VowifiHistory instanceId={d.instance_id} subscribe={subscribe}/><div className="u-details cols"><div className="u-detail"><span>ePDG / IKE</span><b>{typeof d.vowifi?.epdg === 'object' ? (d.vowifi.epdg.ike_reason || (d.vowifi.epdg.pcscf ? t('Tunnel connected') : t('Waiting'))) : (d.vowifi?.epdg || d.status?.state || t('Not connected'))}</b></div><div className="u-detail"><span>IMS / SIP</span><b>{d.vowifi?.ims || d.status?.label || t('Not connected')}</b></div><div className="u-detail"><span>{t('Country exit')}</span><b className="u-proxy-node-text"><ProxyNodeName text={exitNodeLabel(d, t)} /></b></div><div className="u-detail"><span>{t('Data channel rekey')}</span><b>{(d.vowifi?.rekey_minutes ?? 30) === 0 ? t(d.vowifi?.accept_epdg_rekey ? 'Initiated by carrier' : 'Off') : `${d.vowifi?.rekey_minutes ?? 30} ${t('minutes')}`}</b></div><div className="u-detail"><span>{t('Control channel rekey')}</span><b>{(d.vowifi?.ike_rekey_minutes ?? 150) === 0 ? t('Off') : `${d.vowifi?.ike_rekey_minutes ?? 150} ${t('minutes')}`}</b></div></div>{!!d.egress?.pinned_node && d.egress.pinned_node !== d.egress.node && !!exitChangeReason(d.egress, t, language) && <p className="u-note u-proxy-node-text"><ProxyNodeName text={exitChangeReason(d.egress, t, language)} /></p>}<p className="u-note">{t('Software support means the technical path is implemented. Actual availability still depends on the SIM plan, carrier, region, modem firmware and device-identity policy.')}</p></div>}
      {tab==='hardware' && <HardwarePanel device={d} refreshDevices={refreshDevices} showToast={showToast}/>}
    </section></div>
}

function CountryExitControl({ device, refresh, showToast }) {
  const { t, language } = useI18n()
  const [saving, setSaving] = useState(false)
  const route = device.egress || {}
  const countries = [...new Set([...(route.available_countries || []), route.detected_country, route.country].filter(Boolean))]
    .sort((a, b) => countryLabel(a, language).localeCompare(countryLabel(b, language)))
  const select = async (country) => {
    if (!device.instance_id) return
    setSaving(true)
    try {
      const result = await api.setLineCountry(device.instance_id, country)
      await refresh()
      showToast(t(country ? 'Country exit changed to {country}' : 'Country exit returned to automatic detection', {
        country: country ? countryLabel(result.effective_country || country, language) : '' }))
    } catch (error) { showToast(`${t('Error')}: ${error.message}`) }
    finally { setSaving(false) }
  }
  return <div className="u-note" style={{ marginBottom: 16 }}>
    <label>{t('Country exit selection')}</label>
    <select value={route.override || ''} disabled={saving || !device.instance_id} onChange={event => select(event.target.value)}>
      <option value="">{route.detected_country
        ? t('Automatic — detected {country}', { country: countryLabel(route.detected_country, language) })
        : t('Automatic — country not detected')}</option>
      {countries.map(country => <option value={country} key={country}>{countryLabel(country, language)}</option>)}
    </select>
    <div style={{ fontSize: 12, color: 'var(--text-mute)', marginTop: 6 }}>
      {device.instance_id
        ? t('The SIM country is detected automatically. Select a country only when the detected route is wrong.')
        : t('Waiting for the SIM identity before a country exit can be selected.')}
    </div>
  </div>
}

const COUNTRY_CODES = `ad ae af ag ai al am ao ar at au az ba bb bd be bf bg bh bi bj bn bo br bs bt bw by bz ca cd cf cg ch ci ck cl cm cn co cr cu cv cy cz de dj dk dm do dz ec ee eg er es et fi fj fm fr ga gb gd ge gh gm gn gq gr gt gw gy hk hn hr ht hu id ie in iq ir is it jm jo jp ke kg kh ki km kn kp kr kw ky kz la lb lc li lk lr ls lt lu lv ly ma mc md me mg mk ml mm mn mo mr mt mu mv mw mx my mz na ne ng ni nl no np nz om pa pe pg ph pk pl pr ps pt pw py qa ro rs ru rw sa sb sc sd se sg si sk sl sm sn so sr ss st sv sy sz td tg th tm tn to tr tt tv tw tz ua ug us uy uz vc ve vg vi vn vu ws ye za zm zw`.split(' ')

function countryLabel(code, language) {
  try { return `${new Intl.DisplayNames([language === 'zh' ? 'zh-CN' : 'en'], { type: 'region' }).of(code.toUpperCase())} (${code.toUpperCase()})` }
  catch { return code.toUpperCase() }
}

function countryName(code, language) {
  try { return new Intl.DisplayNames([language === 'zh' ? 'zh-CN' : 'en'], { type: 'region' }).of(code.toUpperCase()) }
  catch { return code.toUpperCase() }
}

function countryKeywords(code) {
  const values = [code.toUpperCase()]
  for (const locale of [navigator.language || 'zh-CN', 'en']) {
    try { values.push(new Intl.DisplayNames([locale], { type: 'region' }).of(code.toUpperCase())) } catch { /* ISO code remains */ }
  }
  return [...new Set(values.filter(Boolean))]
}

function normalizeCountrySearch(value) {
  return String(value || '').normalize('NFKD').replace(/\p{Diacritic}/gu, '').toLowerCase()
}

function countryMatchesSearch(code, query) {
  const tokens = normalizeCountrySearch(query).trim().split(/\s+/).filter(Boolean)
  if (!tokens.length) return true
  // Search both languages regardless of the current UI locale. Operators often know a country
  // by its English node name even while using the Chinese UI; the ISO code is the quickest path
  // when they know neither localized spelling.
  const haystack = normalizeCountrySearch([
    code,
    countryName(code, 'zh'),
    countryName(code, 'en'),
  ].join(' '))
  return tokens.every(token => haystack.includes(token))
}

function SearchableCountrySelect({ countries, value, onChange, language, placeholder, noResults }) {
  const [query, setQuery] = useState('')
  const [open, setOpen] = useState(false)
  const [activeIndex, setActiveIndex] = useState(0)
  const rootRef = useRef(null)
  const listId = useId()
  const matching = useMemo(
    () => countries.filter(code => countryMatchesSearch(code, query)),
    [countries, query])

  useEffect(() => { setActiveIndex(0) }, [query])
  useEffect(() => {
    if (!open || !matching.length) return
    document.getElementById(`${listId}-${matching[activeIndex]}`)?.scrollIntoView({ block: 'nearest' })
  }, [activeIndex, listId, matching, open])
  useEffect(() => {
    if (!open) return undefined
    const close = event => {
      if (!rootRef.current?.contains(event.target)) { setOpen(false); setQuery('') }
    }
    document.addEventListener('mousedown', close)
    return () => document.removeEventListener('mousedown', close)
  }, [open])

  const choose = code => {
    onChange(code)
    setQuery('')
    setOpen(false)
  }
  const keyDown = event => {
    if (event.key === 'ArrowDown') {
      event.preventDefault()
      setOpen(true)
      setActiveIndex(index => Math.min(index + (open ? 1 : 0), matching.length - 1))
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      setOpen(true)
      setActiveIndex(index => Math.max(index - 1, 0))
    } else if (event.key === 'Home' && open) {
      event.preventDefault(); setActiveIndex(0)
    } else if (event.key === 'End' && open) {
      event.preventDefault(); setActiveIndex(Math.max(0, matching.length - 1))
    } else if (event.key === 'Enter' && open && matching[activeIndex]) {
      event.preventDefault(); choose(matching[activeIndex])
    } else if (event.key === 'Escape' && open) {
      event.preventDefault(); setOpen(false); setQuery('')
    }
  }
  const displayed = open ? query : (value ? countryLabel(value, language) : '')
  return <div className="u-country-picker" ref={rootRef}>
    <input role="combobox" aria-label={placeholder} aria-autocomplete="list" aria-expanded={open}
      aria-controls={listId} aria-activedescendant={open && matching[activeIndex] ? `${listId}-${matching[activeIndex]}` : undefined}
      value={displayed} placeholder={placeholder} autoComplete="off" spellCheck={false}
      onFocus={() => { setQuery(''); setOpen(true); setActiveIndex(0) }}
      onClick={() => { if (!open) { setQuery(''); setOpen(true); setActiveIndex(0) } }}
      onChange={event => { setQuery(event.target.value); onChange(''); setOpen(true) }}
      onKeyDown={keyDown} />
    <span className="u-country-picker-arrow" aria-hidden="true">⌄</span>
    {open && <div className="u-country-picker-list" id={listId} role="listbox">
      {matching.map((code, index) => <button type="button" role="option"
        id={`${listId}-${code}`} aria-selected={index === activeIndex} key={code}
        className={index === activeIndex ? 'active' : ''}
        onMouseEnter={() => setActiveIndex(index)}
        onMouseDown={event => event.preventDefault()} onClick={() => choose(code)}>
        {countryLabel(code, language)}
      </button>)}
      {!matching.length && <div className="u-country-picker-empty" role="status">{noResults}</div>}
    </div>}
  </div>
}

function formatBytes(value) {
  const n = Number(value || 0)
  if (n < 1024) return `${n} B`
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KiB`
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MiB`
  return `${(n / 1024 ** 3).toFixed(1)} GiB`
}

function EyeIcon({ open }) {
  return <svg className="u-eye-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M2.4 12s3.5-6 9.6-6 9.6 6 9.6 6-3.5 6-9.6 6-9.6-6-9.6-6Z"/><circle cx="12" cy="12" r="2.7"/>{!open && <path d="M4 4 20 20"/>}</svg>
}

export function EgressPage({ showToast }) {
  const { t, language } = useI18n()
  const [s, setS] = useState(null)
  const [live, setLive] = useState(null)
  const [loadError, setLoadError] = useState(false)
  const [liveLoading, setLiveLoading] = useState(true)
  const [newCountry, setNewCountry] = useState('')
  const [profileDraft, setProfileDraft] = useState(null)
  const [revealSensitive, setRevealSensitive] = useState(false)
  const [saving, setSaving] = useState(false)
  const [profileTests, setProfileTests] = useState({})
  const [exitTests, setExitTests] = useState({})
  // The exit test measures what the orchestrator is actually running, which is the saved
  // configuration — never the edits still sitting in this form. Tracking the saved proxy
  // settings lets the button say so instead of quietly testing the previous node.
  const [savedProxy, setSavedProxy] = useState(null)
  const loadLive = () => api.egressStatus().then(value => { setLive(value); setLiveLoading(false) }).catch(() => setLiveLoading(false))
  useEffect(() => {
    api.settings().then(value => { setS(value); setSavedProxy(JSON.stringify(value.proxy || {})); setLoadError(false) }).catch(() => setLoadError(true))
    loadLive()
    // The exit node changes on its own when a line fails, so a snapshot taken at mount goes
    // stale with nothing on screen admitting it — the page would still show the node that
    // was in use when it was opened.
    const timer = setInterval(loadLive, 5000)
    return () => clearInterval(timer)
  }, [])
  useEffect(() => {
    if (!profileDraft) return undefined
    const closeOnEscape = event => { if (event.key === 'Escape') setProfileDraft(null) }
    window.addEventListener('keydown', closeOnEscape)
    return () => window.removeEventListener('keydown', closeOnEscape)
  }, [profileDraft])
  if (!s) return <p className={loadError ? 'u-error' : ''}>{t(loadError ? 'Loading failed' : 'Loading')}{!loadError && '…'}</p>
  const proxy = s.proxy || { profiles: {}, exits: {} }
  const patch = p => setS(x => ({ ...x, proxy: { ...x.proxy, ...p } }))
  const profiles = proxy.profiles || {}
  const profileTypeLabel = profile => profile.type === 'subscription' ? t('Subscription link') : profile.type === 'node' ? t('Individual node') : profile.type === 'existing' ? t('Imported outbound') : 'SOCKS5'
  const patchExit = (country, p) => patch({ exits: { ...(proxy.exits || {}), [country]: { ...(proxy.exits?.[country] || {}), ...p } } })
  const patchProfile = (id, p) => patch({ profiles: { ...profiles, [id]: { ...profiles[id], ...p } } })
  const removeExit = country => {
    const exits = { ...(proxy.exits || {}) }; delete exits[country]
    setS(current => ({ ...current, proxy: { ...current.proxy, exits },
      telegram: current.telegram?.proxy_mode === 'country' && current.telegram?.proxy_country === country
        ? { ...current.telegram, proxy_mode: 'direct', proxy_country: '' } : current.telegram,
      updates: current.updates?.proxy_mode === 'country' && current.updates?.proxy_country === country
        ? { ...current.updates, proxy_mode: 'auto', proxy_country: '' } : current.updates }))
  }
  const openAddProfile = () => setProfileDraft({ type: 'subscription', name: '', url: '', refresh_minutes: 30, value: '', server: '', port: 1080, username: '', password: '' })
  const confirmAddProfile = () => {
    if (!profileDraft) return
    const id = `proxy-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`
    const name = profileDraft.name.trim() || t(profileDraft.type === 'subscription' ? 'New subscription' : profileDraft.type === 'node' ? 'New node' : 'New SOCKS5 proxy')
    const detail = profileDraft.type === 'subscription'
      ? { url: profileDraft.url.trim(), refresh_minutes: profileDraft.refresh_minutes || 30 }
      : profileDraft.type === 'node'
        ? { value: profileDraft.value.trim() }
        : { server: profileDraft.server.trim(), port: profileDraft.port || 1080, username: profileDraft.username, password: profileDraft.password }
    patch({ profiles: { ...profiles, [id]: { name, type: profileDraft.type, ...detail } } })
    setProfileDraft(null)
  }
  const draftReady = !!profileDraft && (profileDraft.type === 'subscription' ? !!profileDraft.url.trim() : profileDraft.type === 'node' ? !!profileDraft.value.trim() : !!profileDraft.server.trim() && +profileDraft.port > 0 && +profileDraft.port <= 65535)
  const removeProfile = id => {
    const countries = Object.entries(proxy.exits || {}).filter(([, ex]) => ex.profile_id === id).map(([country]) => country.toUpperCase())
    if (countries.length) { showToast(t('This proxy is used by: {countries}', { countries: countries.join(', ') })); return }
    const next = { ...profiles }; delete next[id]
    setS(current => ({ ...current, proxy: { ...current.proxy, profiles: next },
      telegram: current.telegram?.proxy_profile_id === id
        ? { ...current.telegram, proxy_mode: 'direct', proxy_profile_id: '' } : current.telegram,
      updates: current.updates?.proxy_profile_id === id
        ? { ...current.updates, proxy_mode: 'auto', proxy_profile_id: '' } : current.updates }))
  }
  const testProfile = async id => {
    setProfileTests(x => ({ ...x, [id]: { busy: true } }))
    try {
      const result = await api.testProxyProfile(id, profiles[id])
      setProfileTests(x => ({ ...x, [id]: { ok: true, latency: result.latency_ms, parsed: result.parsed } }))
      showToast(t('UDP test passed ({latency} ms)', { latency: result.latency_ms }))
    } catch (error) {
      // The gateway now explains itself: keep its own words when it has any, and show what
      // it parsed out of the link so a node that works elsewhere can be compared field by field.
      const detail = error.data?.detail
      const raw = (typeof detail === 'object' && detail?.message) || error.message
      const translated = t(raw)
      const message = translated === raw && !/[.:]/.test(raw)
        ? t('UDP test failed. Check the proxy address, credentials, protocol and UDP support.')
        : translated
      setProfileTests(x => ({ ...x, [id]: { ok: false, error: message, parsed: detail?.parsed } }))
      showToast(message)
    }
  }
  /** How the gateway read a pasted link. The SNI names the operator's own server, so it
   * follows the sensitive-information switch — a screenshot of a failed test used to carry
   * it into a public issue. Nothing else here identifies the host. */
  const parsedSummary = parsed => {
    if (!parsed || typeof parsed !== 'object') return ''
    if (parsed.error) return parsed.error
    const parts = [parsed.protocol]
    if (parsed.transport) parts.push(parsed.transport)
    if (parsed.tls) parts.push(parsed.reality ? 'reality' : 'tls')
    if (parsed.sni) parts.push(`sni=${revealSensitive ? parsed.sni : '••••'}`)
    if (parsed.alpn?.length) parts.push(`alpn=${parsed.alpn.join(',')}`)
    if (parsed.obfs) parts.push(`obfs=${parsed.obfs}${parsed.obfs_password_set ? '+pw' : ''}`)
    if (parsed.encryption) parts.push(`encryption=${parsed.encryption}`)
    if (parsed.flow) parts.push(`flow=${parsed.flow}`)
    if (parsed.skip_cert_verify) parts.push('insecure')
    parts.push(parsed.udp_capable ? 'UDP ✓' : 'UDP ✗')
    if (parsed.engine) parts.push(parsed.engine)
    return parts.filter(Boolean).join(' · ')
  }
  const proxyDirty = savedProxy !== null && JSON.stringify(s.proxy || {}) !== savedProxy
  const testExit = async country => {
    setExitTests(x => ({ ...x, [country]: { busy: true } }))
    try {
      const result = await api.testEgress(country)
      await loadLive()
      // Name the node that answered: the exit may be running something other than the
      // selection on screen, and a bare "succeeded" hides which one was measured.
      showToast(result.node
        ? t('Exit test passed via {node} · {latency} ms', { node: result.node, latency: result.latency_ms })
        : t('Exit test passed · {latency} ms', { latency: result.latency_ms }))
    } catch (e) {
      showToast(e.message)
    } finally {
      setExitTests(x => ({ ...x, [country]: { busy: false } }))
    }
  }
  const addExit = () => { if (!newCountry) return; patchExit(newCountry, { enabled: true, profile_id: '', keywords: countryKeywords(newCountry) }); setNewCountry('') }
  const available = COUNTRY_CODES.filter(code => !proxy.exits?.[code]).sort((a, b) => countryLabel(a, language).localeCompare(countryLabel(b, language)))
  const save = async () => { setSaving(true); try { await api.saveSettings(s); setSavedProxy(JSON.stringify(s.proxy || {})); await api.refreshEgress(); showToast(t('Saved')); setTimeout(loadLive, 1000) } catch (e) { showToast(`${t('Error')}: ${e.message}`) } finally { setSaving(false) } }
  return <div className="u-page">
    <div className="card u-panel u-routing-policy"><div className="u-card-head"><div><h2>{t('Country proxy routing')}</h2><p>{t('When enabled, VoWiFi uses the proxy assigned to its SIM country and never falls back to the default network if that exit fails.')}</p></div><div className="u-head-actions"><Badge state={proxy.enabled && live ? 'on' : 'off'}>{proxy.enabled ? (liveLoading ? `${t('Loading')}…` : live ? t('Enabled') : t('Status unavailable')) : t('Disabled')}</Badge><label className="u-title-toggle"><span>{t('Enable country proxy exits')}</span><input type="checkbox" className="u-toggle" checked={!!proxy.enabled} onChange={e => patch({ enabled: e.target.checked })} /></label></div></div><p className="u-routing-impact">{proxy.enabled ? t('On: each line uses its country exit. If the proxy or UDP validation fails, only that line’s VoWiFi stops; it will not leak through the host’s default network.') : t('Off: country exits are bypassed and VoWiFi uses the host’s default network. Country assignments and proxy settings are kept for later.')}</p>{Object.values(profiles).some(profile => profile.type === 'existing') && <><label>{t('Existing sing-box config')}</label><input className="mono" value={proxy.existing_singbox_config || ''} onChange={e => patch({ existing_singbox_config: e.target.value })} placeholder="/etc/sing-box/config.json" /></>}</div>
    <div className="u-section-title u-proxy-library-head"><div><h2>{t('Proxy library')}</h2><p>{t('Add reusable subscriptions, individual nodes, or SOCKS5 proxies, then assign them to country exits below.')}</p></div><div className="u-proxy-toolbar"><button className="u-icon-button" type="button" aria-pressed={revealSensitive} onClick={() => setRevealSensitive(x => !x)} title={t(revealSensitive ? 'Hide sensitive information' : 'Show sensitive information')}><EyeIcon open={revealSensitive}/><span>{t('Sensitive information')}</span></button><button className="btn btn-primary" onClick={openAddProfile}>{t('+ Add proxy')}</button></div></div>
    {!Object.keys(profiles).length ? <Empty title={t('No proxies configured')} detail={t('Add a subscription, individual node, or SOCKS5 proxy above.')} /> : <div className="u-proxy-list">{Object.entries(profiles).map(([id, profile]) => {
      const usedBy = Object.entries(proxy.exits || {}).filter(([, ex]) => ex.profile_id === id).map(([country]) => countryLabel(country, language))
      return <div className="card u-proxy-row" key={id}>
        <div className="u-proxy-identity"><span className="u-proxy-kind">{profileTypeLabel(profile)}</span><input aria-label={t('Name')} value={profile.name || ''} onChange={e => patchProfile(id, { name: e.target.value })} />{usedBy.length ? <small>{t('Used by {countries}', { countries: usedBy.join(', ') })}</small> : <small>{t('Not assigned to a country exit')}</small>}</div>
        <div className="u-proxy-primary">
          {profile.type === 'subscription' && <><label>{t('Subscription URL')}</label><input className="mono" type={revealSensitive ? 'text' : 'password'} autoComplete="off" value={profile.url || ''} onChange={e => patchProfile(id, { url: e.target.value })} placeholder="https://…" /></>}
          {profile.type === 'node' && <><label>{t('Node share link')}</label><input className="mono" type={revealSensitive ? 'text' : 'password'} autoComplete="off" value={profile.value || ''} onChange={e => patchProfile(id, { value: e.target.value })} placeholder="vless://…" /></>}
          {profile.type === 'socks5' && <><label>{t('Server')}</label><input className="mono" type={revealSensitive ? 'text' : 'password'} value={profile.server || ''} onChange={e => patchProfile(id, { server: e.target.value })} /></>}
          {profile.type === 'existing' && <><label>{t('Existing outbound tag')}</label><input value={profile.outbound_tag || ''} onChange={e => patchProfile(id, { outbound_tag: e.target.value })} /></>}
        </div>
        <div className="u-proxy-secondary">
          {profile.type === 'subscription' && <><label>{t('Refresh interval')}</label><div className="u-number-suffix"><input type="number" min="1" value={profile.refresh_minutes || 30} onChange={e => patchProfile(id, { refresh_minutes: +e.target.value })} /><span>{t('minutes')}</span></div></>}
          {profile.type === 'node' && <small className="u-proxy-hint">{t('Reality/XHTTP and common share-link protocols')}</small>}
          {profile.type === 'socks5' && <><label>{t('Port')}</label><input type="number" min="1" max="65535" value={profile.port || 1080} onChange={e => patchProfile(id, { port: +e.target.value })} /></>}
          {profile.type === 'existing' && <small>{t('Compatibility entry')}</small>}
        </div>
        {profile.type === 'socks5' && <div className="u-proxy-auth"><div><label>{t('Username')}</label><input type={revealSensitive ? 'text' : 'password'} autoComplete="off" value={profile.username || ''} onChange={e => patchProfile(id, { username: e.target.value })} /></div><div><label>{t('Password')}</label><input type={revealSensitive ? 'text' : 'password'} autoComplete="new-password" value={profile.password || ''} onChange={e => patchProfile(id, { password: e.target.value })} /></div></div>}
        <div className="u-proxy-actions">{['node', 'socks5'].includes(profile.type) && <button className="btn btn-ghost" disabled={profileTests[id]?.busy} onClick={() => testProfile(id)}>{t(profileTests[id]?.busy ? 'Testing…' : 'Test UDP')}</button>}<button className="btn btn-ghost u-proxy-remove" onClick={() => removeProfile(id)}>{t('Remove')}</button></div>
        {/* A verdict and the parsed link are two different things and neither is short. Sharing
            one crowded row truncated both — the summary that explains a failure was the part
            that got cut. They get their own full-width line under the node instead. */}
        {['node', 'socks5'].includes(profile.type) && profileTests[id] && !profileTests[id].busy
          && <div className={`u-test-row ${profileTests[id].ok ? 'is-ok' : 'is-error'}`}>
            <span className="u-test-verdict">
              {profileTests[id].ok ? `${t('Passed')} · ${profileTests[id].latency} ms` : t('Failed')}
            </span>
            {parsedSummary(profileTests[id].parsed)
              && <span className="u-test-parsed" title={t('How this gateway read the link')}>{parsedSummary(profileTests[id].parsed)}</span>}
            {!profileTests[id].ok && profileTests[id].error
              && <span className="u-test-detail">{profileTests[id].error}</span>}
          </div>}
      </div>
    })}</div>}
    <div className="u-section-title"><div><h2>{t('Country exits')}</h2><p>{t('If no healthy UDP exit exists, only that SIM’s VoWiFi stops; 4G remains available.')}</p></div><div className="u-inline u-add-exit"><SearchableCountrySelect countries={available} value={newCountry} onChange={setNewCountry} language={language} placeholder={t('Search countries/regions…')} noResults={t('No matching countries/regions')} /><button className="btn btn-primary" disabled={!newCountry} onClick={addExit}>{t('+ Add')}</button></div></div>
    {!Object.keys(proxy.exits || {}).length ? <Empty title={t('No country exits configured')} detail={t('Choose a country above, then configure its node source and keywords.')} /> : <div className="u-device-grid">{Object.entries(proxy.exits).map(([country, ex]) => {
      const st = live?.exits?.[country]
      const selected = profiles[ex.profile_id]
      const subscription = selected?.type === 'subscription'
      return <div className="card u-panel" key={country}><div className="u-card-head"><h3>{countryLabel(country, language)}</h3><div className="u-head-actions"><Badge state={st?.ready ? 'on' : st ? 'error' : 'off'}>{liveLoading ? `${t('Loading')}…` : st?.ready ? t('UDP verified') : t('Not connected')}</Badge><label className="u-title-toggle"><span>{t('Enabled')}</span><input type="checkbox" className="u-toggle" checked={ex.enabled !== false} onChange={e => patchExit(country, { enabled: e.target.checked })} /></label></div></div>
        <label>{t('Exit proxy')}</label><select value={ex.mode === 'direct' ? '__direct' : ex.profile_id || ''} onChange={e => patchExit(country, e.target.value === '__direct' ? { mode: 'direct', profile_id: '' } : { mode: '', profile_id: e.target.value })}><option value="">{t('Select a proxy…')}</option>{Object.entries(profiles).map(([id, item]) => <option key={id} value={id}>{item.name || t('Unnamed proxy')} · {profileTypeLabel(item)}</option>)}<option value="__direct">{t('Explicit direct connection')}</option></select>
        {subscription && <><label>{t('Node-name keywords (comma-separated)')}</label><input value={(ex.keywords || []).join(', ')} onChange={e => patchExit(country, { keywords: e.target.value.split(',').map(x => x.trim()).filter(Boolean) })} /></>}
        {subscription
          ? <><label>{t('Current node')}</label>
            {/* The pinned name is kept in the list even when the live status is missing, so
                opening this page before the orchestrator answers cannot silently drop it. */}
            <select className="u-proxy-node-select" value={ex.pinned_node || ''} onChange={e => patchExit(country, { pinned_node: e.target.value })}>
              <option value="">{st?.node ? t('Automatic — changes only when a line fails ({node})', { node: st.node }) : t('Automatic')}</option>
              {[...new Set([...(st?.candidates || []), ...(ex.pinned_node ? [ex.pinned_node] : [])])].map(name => <option key={name} value={name}>{name}</option>)}
            </select>
            {st?.pinned_missing && <p className="u-error u-proxy-node-text"><ProxyNodeName text={t('Pinned node “{node}” is no longer offered by the subscription; automatic selection is in use.', { node: ex.pinned_node })} /></p>}
            {/* Without this the picker shows the chosen node while the exit quietly runs on
                another one, which reads as "my setting did nothing". */}
            {!!ex.pinned_node && !!st?.node && st.node !== ex.pinned_node && !st?.pinned_missing
              && ((ex.pin_mode || 'lock') === 'lock'
                ? <p className="u-error u-proxy-node-text"><ProxyNodeName text={t('Not in use: the exit is running on “{node}”. Check whether the locked node is reachable.', { node: st.node })} /></p>
                : <p className="u-note u-proxy-node-text"><ProxyNodeName text={`${t('The preferred node is not in use; the exit is running on “{node}”. It returns to your preferred node the next time the exit has to change.', { node: st.node })} ${exitChangeReason(st, t, language)}`} /></p>)}
            {!!ex.pinned_node && <><label>{t('If that node stops working')}</label>
              <select value={ex.pin_mode || 'lock'} onChange={e => patchExit(country, { pin_mode: e.target.value })}>
                <option value="lock">{t('Keep using it — never switch automatically')}</option>
                <option value="prefer">{t('Move to another node, and come back to this one later')}</option>
              </select>
              <p className="u-note">{(ex.pin_mode || 'lock') === 'lock'
                ? t('Locked: the line stays down until you change this. Use it for a controlled comparison.')
                : t('Preferred: a failing line moves to another node, and returns to this one the next time the exit has to change anyway.')}</p></>}</>
          : <div className="u-detail"><span>{t('Current node')}</span><b className="u-proxy-node-text"><ProxyNodeName text={st?.node || '—'} /></b></div>}
        {st?.error && <p className="u-error">{st.error}</p>}
        <div className="u-inline"><button className="btn btn-ghost" disabled={exitTests[country]?.busy || proxyDirty} title={proxyDirty ? t('This tests the running exit, so save and apply the change first.') : ''} onClick={() => testExit(country)}>{t(exitTests[country]?.busy ? 'Testing…' : 'Test UDP')}</button>{proxyDirty && <small className="u-test-parsed">{t('This tests the running exit, so save and apply the change first.')}</small>}<button className="btn btn-ghost" onClick={() => removeExit(country)}>{t('Remove')}</button></div>
      </div>
    })}</div>}
    <button className="btn btn-primary" disabled={saving} onClick={save}>{t('Save and apply')}</button>
    {profileDraft && <div className="u-modal-backdrop" onClick={() => setProfileDraft(null)}>
      <div className="card u-proxy-modal" role="dialog" aria-modal="true" aria-labelledby="add-proxy-title" onClick={e => e.stopPropagation()}>
        <div className="u-proxy-modal-head"><div><h2 id="add-proxy-title">{t('Add proxy')}</h2><p>{t('Choose a source type. You can change the details before adding it to the library.')}</p></div><button className="u-modal-close" type="button" onClick={() => setProfileDraft(null)} aria-label={t('Cancel')}>×</button></div>
        <div className="u-proxy-type-grid">
          {[
            ['subscription', t('Subscription link'), t('Paste a Clash subscription URL. The gateway fetches it automatically, extracts compatible nodes, and refreshes it on schedule.'), '📡'],
            ['node', t('Individual node'), t('Paste one share link. Supports VLESS Reality/XHTTP, Trojan, Hysteria2, Shadowsocks and VMess.'), '🔗'],
            ['socks5', 'SOCKS5', t('Connect to a SOCKS5 server directly. It must support UDP ASSOCIATE for VoWiFi.'), '🧦'],
          ].map(([type, title, detail, icon]) => <button type="button" key={type} className={`u-proxy-type ${profileDraft.type === type ? 'active' : ''}`} onClick={() => setProfileDraft({ ...profileDraft, type })}><span className="u-proxy-type-icon" aria-hidden="true">{icon}</span><b>{title}</b><small>{detail}</small></button>)}
        </div>
        <div className="u-proxy-modal-form">
          <label>{t('Name')} <span>{t('optional')}</span></label><input autoFocus value={profileDraft.name} onChange={e => setProfileDraft({ ...profileDraft, name: e.target.value })} placeholder={t(profileDraft.type === 'subscription' ? 'New subscription' : profileDraft.type === 'node' ? 'New node' : 'New SOCKS5 proxy')} />
          {profileDraft.type === 'subscription' && <><label>{t('Subscription URL')}</label><input className="mono" type={revealSensitive ? 'text' : 'password'} autoComplete="off" value={profileDraft.url} onChange={e => setProfileDraft({ ...profileDraft, url: e.target.value })} placeholder="https://…" /><label>{t('Refresh interval (minutes)')}</label><input type="number" min="1" value={profileDraft.refresh_minutes} onChange={e => setProfileDraft({ ...profileDraft, refresh_minutes: +e.target.value })} /></>}
          {profileDraft.type === 'node' && <><label>{t('Node share link')}</label><textarea className="mono" rows="4" value={profileDraft.value} onChange={e => setProfileDraft({ ...profileDraft, value: e.target.value })} placeholder="vless://…" /></>}
          {profileDraft.type === 'socks5' && <div className="u-form-grid"><div><label>{t('Server')}</label><input className="mono" value={profileDraft.server} onChange={e => setProfileDraft({ ...profileDraft, server: e.target.value })} placeholder="proxy.example.com" /></div><div><label>{t('Port')}</label><input type="number" min="1" max="65535" value={profileDraft.port} onChange={e => setProfileDraft({ ...profileDraft, port: +e.target.value })} /></div><div><label>{t('Username (optional)')}</label><input value={profileDraft.username} onChange={e => setProfileDraft({ ...profileDraft, username: e.target.value })} /></div><div><label>{t('Password (optional)')}</label><input type={revealSensitive ? 'text' : 'password'} autoComplete="new-password" value={profileDraft.password} onChange={e => setProfileDraft({ ...profileDraft, password: e.target.value })} /></div></div>}
        </div>
        <div className="u-modal-actions"><button className="btn btn-ghost" onClick={() => setProfileDraft(null)}>{t('Cancel')}</button><button className="btn btn-primary" disabled={!draftReady} onClick={confirmAddProfile}>{t('Add to proxy library')}</button></div>
      </div>
    </div>}
  </div>
}

const NOTIFICATION_TEMPLATE_EVENTS = [
  ['incoming_call', 'Incoming call'], ['missed_call', 'Missed call'],
  ['voicemail_received', 'New voicemail'], ['incoming_sms', 'Incoming SMS'],
  ['host_alert', 'Host alert'], ['number_changed', 'Line number changed'],
  ['line_unrecoverable', 'Line cannot recover'], ['keepalive_result', 'Number keeping result'],
  ['balance_low', 'Balance low'], ['software_update', 'Software update'],
]

const TEMPLATE_SAMPLE = {
  event: 'incoming_sms', instance: '1', sim_name: 'UK SIM', iccid: '8900…',
  msisdn: '+441234567890', from: '+447000000000', text: 'Example notification text',
  title: 'MDD · VoWiFi 短信 · UK SIM',
  content: 'SIM: UK SIM\n本机号码: +441234567890\n来源号码: +447000000000\n\nExample notification text',
}

function MessageTemplateEditor({ channel, config, onChange, onTest }) {
  const { t } = useI18n()
  const [event, setEvent] = useState('incoming_sms')
  const [testing, setTesting] = useState(false)
  const templates = config.message_templates || {}
  const current = templates[event] || {}
  const update = (field, value) => {
    const nextEvent = { ...current, [field]: value }
    const next = { ...templates }
    if (!String(nextEvent.title || '').trim() && !String(nextEvent.content || '').trim()) delete next[event]
    else next[event] = nextEvent
    onChange(next)
  }
  const reset = () => { const next = { ...templates }; delete next[event]; onChange(next) }
  const eventLabel = NOTIFICATION_TEMPLATE_EVENTS.find(([key]) => key === event)?.[1] || event
  const sample = {
    ...TEMPLATE_SAMPLE,
    event,
    title: `MDD · ${t(eventLabel)} · ${TEMPLATE_SAMPLE.sim_name}`,
    content: `${t(eventLabel)}\nSIM: ${TEMPLATE_SAMPLE.sim_name}\n${TEMPLATE_SAMPLE.text}`,
  }
  const render = value => String(value || '').replace(/\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}/g, (_all, key) => sample[key] ?? '')
  const previewTitle = render(current.title)
  const previewContent = render(current.content)
  const custom = !!(String(current.title || '').trim() || String(current.content || '').trim())
  const test = async () => {
    setTesting(true)
    try { await onTest(event) } finally { setTesting(false) }
  }
  return <details className="u-template-editor"><summary>{t('Customize notification messages')}</summary>
    <p className="u-note">{t('Override one event at a time. Empty fields keep the built-in wording; templates only replace the listed fields and cannot run code.')}</p>
    <label>{t('Template event')}</label><select value={event} onChange={e => setEvent(e.target.value)}>{NOTIFICATION_TEMPLATE_EVENTS.map(([key, label]) => <option key={key} value={key}>{t(label)}</option>)}</select>
    <label>{t('Title template')}</label><input value={current.title || ''} onChange={e => update('title', e.target.value)} placeholder="{{title}}" />
    <label>{t('Content template')}</label><textarea rows="5" value={current.content || ''} onChange={e => update('content', e.target.value)} placeholder="{{content}}" />
    <p className="u-template-fields"><b>{t('Available variables')}:</b> <code>{'{{title}} {{content}} {{event}} {{sim_name}} {{msisdn}} {{from}} {{text}} {{instance}} {{iccid}}'}</code></p>
    <div className="u-template-preview"><b>{t('Preview')} · {channel}</b>{custom ? <><strong>{previewTitle || sample.title}</strong><pre>{previewContent || sample.content}</pre></> : <p>{t('This event is using the built-in message format.')}</p>}</div>
    <div className="u-inline"><button type="button" className="btn btn-ghost" disabled={!custom || testing} onClick={reset}>{t('Restore this event template')}</button><button type="button" className="btn btn-ghost" disabled={testing} onClick={test}>{t(testing ? 'Testing…' : 'Test this event')}</button></div>
  </details>
}

export function NotificationsPage({ showToast, instances = [] }) {
  const { t } = useI18n(); const [s, setS] = useState(null); const [loadError, setLoadError] = useState(false); const [tab, setTab] = useState('channels'); const [deliveries, setDeliveries] = useState(null); const [deliveriesLoading, setDeliveriesLoading] = useState(true); const [deliveriesError, setDeliveriesError] = useState(false); const [channelTesting, setChannelTesting] = useState(''); const [activeFeishuId, setActiveFeishuId] = useState('')
  const loadDeliveries = () => { setDeliveriesLoading(true); return api.notificationDeliveries().then(value => { setDeliveries(value); setDeliveriesError(false) }).catch(() => setDeliveriesError(true)).finally(() => setDeliveriesLoading(false)) }
  useEffect(() => { api.settings().then(value => { setS(value); setLoadError(false) }).catch(() => setLoadError(true)); loadDeliveries() }, [])
  useEffect(() => { if (tab === 'delivery') loadDeliveries() }, [tab])
  const feishuChannels = Array.isArray(s?.feishu?.channels) ? s.feishu.channels : []
  useEffect(() => { if (feishuChannels.length && !feishuChannels.some(channel => channel.id === activeFeishuId)) setActiveFeishuId(feishuChannels[0].id); if (!feishuChannels.length && activeFeishuId) setActiveFeishuId('') }, [feishuChannels, activeFeishuId])
  if (!s) return <p className={loadError ? 'u-error' : ''}>{t(loadError ? 'Loading failed' : 'Loading')}{!loadError && '…'}</p>
  const wh = s.webhook || {}, tg = s.telegram || {}, pp = s.pushplus || {}, fs = s.feishu || {}
  const setChannel = (key, patch) => setS(x => ({ ...x, [key]: { ...(x[key] || {}), ...patch } }))
  const setEvent = (key, cfg, event, checked) => setChannel(key, { events: { ...(cfg.events || {}), [event]: checked } })
  const eventOptions = (key, cfg) => <details className="u-event-options"><summary>{t('Forward these events')}</summary><div className="u-inline">{NOTIFICATION_TEMPLATE_EVENTS.map(([event, label]) => <label key={event}><input type="checkbox" className="u-toggle" checked={cfg.events?.[event] !== false} onChange={e => setEvent(key, cfg, event, e.target.checked)} />{t(label)}</label>)}</div></details>
  const runChannelTest = async (key, send, config) => {
    if (channelTesting) return
    setChannelTesting(key)
    try { await send(config); showToast(t('Test succeeded')) } catch (e) { showToast(e.message) } finally { setChannelTesting('') }
  }
  const testButton = (key, send, config) => <button type="button" className="btn btn-ghost" disabled={!!channelTesting} onClick={() => runChannelTest(key, send, config)}>{t(channelTesting === key ? 'Testing…' : 'Test')}</button>
  const activeFeishuChannel = feishuChannels.find(channel => channel.id === activeFeishuId) || feishuChannels[0]
  const setFeishuChannels = channels => setChannel('feishu', { channels })
  const updateFeishuChannel = (id, patch) => setFeishuChannels(feishuChannels.map(channel => channel.id === id ? { ...channel, ...patch } : channel))
  const removeFeishuChannel = id => setFeishuChannels(feishuChannels.filter(channel => channel.id !== id))
  const addFeishuChannel = () => {
    const used = new Set(feishuChannels.map(channel => channel.id)); let serial = feishuChannels.length + 1
    while (used.has(`bot-${serial}`)) serial += 1
    const id = `bot-${serial}`; setFeishuChannels([...feishuChannels, { id, name: `${t('Feishu bot')} ${serial}`, enabled: false, url: '', secret: '', instances: [], events: {}, message_templates: {} }]); setActiveFeishuId(id)
  }
  const setFeishuEvent = (channel, event, checked) => updateFeishuChannel(channel.id, { events: { ...(channel.events || {}), [event]: checked } })
  const toggleFeishuInstance = (channel, instanceId, checked) => { const selected = new Set((channel.instances || []).map(String)); if (checked) selected.add(String(instanceId)); else selected.delete(String(instanceId)); updateFeishuChannel(channel.id, { instances: [...selected] }) }
  const feishuEventOptions = channel => <details className="u-event-options"><summary>{t('Forward these events')}</summary><div className="u-inline">{NOTIFICATION_TEMPLATE_EVENTS.map(([event, label]) => <label key={event}><input type="checkbox" className="u-toggle" checked={channel.events?.[event] !== false} onChange={e => setFeishuEvent(channel, event, e.target.checked)} />{t(label)}</label>)}</div></details>
  const save = async () => { try { await api.saveSettings(s); showToast(t('Saved')) } catch (e) { showToast(e.message) } }
  return <div className="u-page u-notifications-page"><div className="u-tabs"><button className={tab === 'channels' ? 'active' : ''} onClick={() => setTab('channels')}>{t('Channels')}</button><button className={tab === 'delivery' ? 'active' : ''} onClick={() => setTab('delivery')}>{t('Delivery log')}</button></div>
    {tab === 'channels' && <div className="u-device-grid"><div className="card u-panel"><div className="u-card-head"><div><h2>Webhook</h2><p>{t('Standard GET or POST webhook with optional custom fields.')}</p></div><input type="checkbox" className="u-toggle" checked={!!wh.enabled} onChange={e => setChannel('webhook', { enabled: e.target.checked })} /></div><label>{t('Payload format')}</label><select value={wh.format || 'generic'} onChange={e => setChannel('webhook', { format: e.target.value })}><option value="generic">{t('Standard event fields')}</option><option value="custom">{t('Custom template')}</option></select><label>{t('Webhook URL')}</label><input value={wh.url || ''} onChange={e => setChannel('webhook', { url: e.target.value })} /><div className="u-form-grid"><div><label>{t('Method')}</label><select value={wh.method || 'POST'} onChange={e => setChannel('webhook', { method: e.target.value })}><option>POST</option><option>GET</option></select></div><div><label>{t('Body format')}</label><select value={wh.body_mode || 'json'} onChange={e => setChannel('webhook', { body_mode: e.target.value })}><option value="json">JSON</option><option value="form">Form</option><option value="raw">Raw</option></select></div></div>{wh.format === 'custom' && <><label>{t('Payload template')}</label><textarea rows="5" value={wh.payload_template || ''} onChange={e => setChannel('webhook', { payload_template: e.target.value })} placeholder={'{"title":"{{title}}","text":"{{text}}"}'} /></>}<label>{t('Custom headers (JSON)')}</label><textarea rows="3" value={wh.headers_json || '{}'} onChange={e => setChannel('webhook', { headers_json: e.target.value })} /><label><input type="checkbox" className="u-toggle" checked={wh.verify_tls !== false} onChange={e => setChannel('webhook', { verify_tls: e.target.checked })} />{t('Verify remote TLS certificate')}</label><MessageTemplateEditor channel="Webhook" config={wh} onChange={message_templates => setChannel('webhook', { message_templates })} onTest={async event => { try { await api.testWebhook({ ...wh, _test_event: event }); showToast(t('Test succeeded')) } catch (e) { showToast(e.message) } }} />{eventOptions('webhook', wh)}{testButton('webhook', api.testWebhook, wh)}</div>
      <div className="card u-panel">
        <div className="u-card-head"><div><h2>Telegram</h2><p>{t('Direct, a proxy library entry, or an existing country exit.')}</p></div><input type="checkbox" className="u-toggle" checked={!!tg.enabled} onChange={e => setChannel('telegram', { enabled: e.target.checked })} /></div>
        <label>{t('Bot token')}</label><input type="password" value={tg.bot_token || ''} onChange={e => setChannel('telegram', { bot_token: e.target.value })} />
        <label>{t('Chat / Channel ID')}</label><input value={tg.chat_id || ''} onChange={e => setChannel('telegram', { chat_id: e.target.value })} />
        <label>{t('Connection')}</label><select value={tg.proxy_mode || 'direct'} onChange={e => { const mode = e.target.value; setChannel('telegram', { proxy_mode: mode, proxy_profile_id: mode === 'library' ? (tg.proxy_profile_id || '') : '', proxy_country: mode === 'country' ? (tg.proxy_country || '') : '' }) }}><option value="direct">{t('Direct')}</option><option value="library">{t('Proxy library')}</option><option value="country">{t('Country exit')}</option>{tg.proxy_mode === 'manual' && <option value="manual">{t('Manual HTTP/SOCKS proxy')} · {t('Legacy setting')}</option>}</select>
        {tg.proxy_mode === 'library' && <><label>{t('Proxy')}</label><select value={tg.proxy_profile_id || ''} onChange={e => setChannel('telegram', { proxy_profile_id: e.target.value })}><option value="">{t('Select a proxy…')}</option>{selectableProxyProfiles(s).map(([id, profile]) => <option key={id} value={id}>{profile.name || t('Unnamed proxy')}</option>)}</select></>}
        {tg.proxy_mode === 'manual' && <><label>{t('Proxy URL')}</label><input value={tg.proxy_url || ''} onChange={e => setChannel('telegram', { proxy_url: e.target.value })} /></>}
        {tg.proxy_mode === 'country' && <><label>{t('Country exit')}</label><select value={tg.proxy_country || ''} onChange={e => setChannel('telegram', { proxy_country: e.target.value })}><option value="">{t('Select a country/region…')}</option>{Object.keys(s.proxy?.exits || {}).map(country => <option key={country} value={country}>{country.toUpperCase()}</option>)}</select></>}
        <MessageTemplateEditor channel="Telegram" config={tg} onChange={message_templates => setChannel('telegram', { message_templates })} onTest={async event => { try { await api.testTelegram({ ...tg, _test_event: event }); showToast(t('Test succeeded')) } catch (e) { showToast(e.message) } }} />{eventOptions('telegram', tg)}{testButton('telegram', api.testTelegram, tg)}
      </div>
      <div className="card u-panel"><div className="u-card-head"><div><h2>PushPlus</h2><p>{t('Push through the official PushPlus service.')}</p></div><input type="checkbox" className="u-toggle" checked={!!pp.enabled} onChange={e => setChannel('pushplus', { enabled: e.target.checked })} /></div><label>{t('PushPlus token')}</label><input type="password" value={pp.token || ''} onChange={e => setChannel('pushplus', { token: e.target.value })} /><label>{t('Topic code (optional)')}</label><input value={pp.topic || ''} onChange={e => setChannel('pushplus', { topic: e.target.value })} /><div className="u-form-grid"><div><label>{t('Content format')}</label><select value={pp.template || 'html'} onChange={e => setChannel('pushplus', { template: e.target.value })}><option value="html">HTML</option><option value="txt">{t('Plain text')}</option><option value="markdown">Markdown</option><option value="json">JSON</option></select></div><div><label>{t('PushPlus channel')}</label><select value={pp.channel || 'wechat'} onChange={e => setChannel('pushplus', { channel: e.target.value })}><option value="wechat">{t('WeChat')}</option><option value="app">App</option><option value="mail">{t('Email')}</option><option value="webhook">Webhook</option><option value="cp">{t('WeCom')}</option><option value="clawbot">ClawBot</option></select></div></div><MessageTemplateEditor channel="PushPlus" config={pp} onChange={message_templates => setChannel('pushplus', { message_templates })} onTest={async event => { try { await api.testPushPlus({ ...pp, _test_event: event }); showToast(t('Test succeeded')) } catch (e) { showToast(e.message) } }} />{eventOptions('pushplus', pp)}{testButton('pushplus', api.testPushPlus, pp)}</div>
      <div className="card u-panel"><div className="u-card-head"><div><h2>Feishu / Lark</h2><p>{t('Send to multiple custom bots and route each bot by SIM line.')}</p></div><button type="button" className="btn btn-ghost" onClick={addFeishuChannel}>{t('Add bot')}</button></div>{!!feishuChannels.length && <div className="u-tabs" role="tablist" aria-label="Feishu / Lark bots">{feishuChannels.map(channel => <button type="button" role="tab" aria-selected={channel.id === activeFeishuChannel?.id} key={channel.id} className={channel.id === activeFeishuChannel?.id ? 'active' : ''} onClick={() => setActiveFeishuId(channel.id)}><span className={channel.enabled ? 'u-dot good' : 'u-dot'} />{channel.name || channel.id}</button>)}</div>}{!activeFeishuChannel && <p className="u-muted">{t('No Feishu bots configured')}</p>}{activeFeishuChannel && <section className="u-settings-card" role="tabpanel" key={activeFeishuChannel.id}><div className="u-card-head"><div><h3>{activeFeishuChannel.name || activeFeishuChannel.id}</h3><p><code>{activeFeishuChannel.id}</code></p></div><input type="checkbox" className="u-toggle" checked={!!activeFeishuChannel.enabled} onChange={e => updateFeishuChannel(activeFeishuChannel.id, { enabled: e.target.checked })} /></div><label>{t('Channel name')}</label><input value={activeFeishuChannel.name || ''} maxLength={120} onChange={e => updateFeishuChannel(activeFeishuChannel.id, { name: e.target.value })} /><label>{t('Feishu webhook URL')}</label><input type="url" value={activeFeishuChannel.url || ''} onChange={e => updateFeishuChannel(activeFeishuChannel.id, { url: e.target.value })} placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/…" /><label>{t('Signing secret (optional)')}</label><input type="password" value={activeFeishuChannel.secret || ''} onChange={e => updateFeishuChannel(activeFeishuChannel.id, { secret: e.target.value })} /><p className="u-note">{t('Use the secret only when signature verification is enabled for the custom bot.')}</p><details className="u-event-options"><summary>{t('SIM line routing')}</summary><p className="u-note">{t('No selected lines means this bot receives every line and system event.')}</p><div className="u-inline">{instances.map(instance => <label key={instance.id}><input type="checkbox" className="u-toggle" checked={(activeFeishuChannel.instances || []).map(String).includes(String(instance.id))} onChange={e => toggleFeishuInstance(activeFeishuChannel, instance.id, e.target.checked)} />{instance.name || instance.msisdn || instance.id}</label>)}</div></details><MessageTemplateEditor channel={activeFeishuChannel.name || activeFeishuChannel.id} config={activeFeishuChannel} onChange={message_templates => updateFeishuChannel(activeFeishuChannel.id, { message_templates })} onTest={event => runChannelTest(`feishu:${activeFeishuChannel.id}`, api.testFeishu, { ...activeFeishuChannel, _test_event: event })} />{feishuEventOptions(activeFeishuChannel)}<div className="u-inline">{testButton(`feishu:${activeFeishuChannel.id}`, api.testFeishu, activeFeishuChannel)}<button type="button" className="btn btn-ghost" onClick={() => removeFeishuChannel(activeFeishuChannel.id)}>{t('Delete bot')}</button></div></section>}</div></div>}
    {tab === 'delivery' && <div className="card u-panel"><div className="u-card-head"><div><h2>{t('Delivery log')}</h2><p>{t('Failed deliveries retry automatically up to three times.')}</p></div><div className="u-inline"><button className="btn btn-ghost" disabled={deliveriesLoading} onClick={loadDeliveries}>{t(deliveriesLoading ? 'Loading…' : 'Refresh')}</button><button className="btn btn-ghost" onClick={async () => { await api.clearNotificationDeliveries(); loadDeliveries() }}>{t('Clear')}</button></div></div>{!deliveries && <p className={deliveriesError ? 'u-error' : 'u-muted'}>{t(deliveriesError ? 'Loading failed' : 'Loading')}{!deliveriesError && '…'}</p>}{deliveries?.pending.map(row => <div className="u-detail" key={row.id}><span>{row.channel} · {row.event}</span><b>{t('Retrying')} ({row.attempts}/3)</b></div>)}{deliveries?.history.map(row => <div className="u-detail" key={row.id}><span>{new Date(row.finished_at * 1000).toLocaleString()} · {row.channel} · {row.event}</span><b>{row.status} · {row.attempts}</b></div>)}{deliveries && !deliveries.pending.length && !deliveries.history.length && <p className="u-muted">{t('No delivery records')}</p>}</div>}
    {tab !== 'delivery' && <button className="btn btn-primary" onClick={save}>{t('Save')}</button>}
  </div>
}

export function SystemPage({ showToast, openUpdateDialog }) {
  const { t, language, setLanguage } = useI18n(); const [s, setS] = useState(null); const [loadError, setLoadError] = useState(false); const [tab, setTab] = useState('general'); const [status, setStatus] = useState(null); const [statusLoaded, setStatusLoaded] = useState(false); const [statusError, setStatusError] = useState(false); const [update,setUpdate]=useState(null); const [releaseChoices,setReleaseChoices]=useState([]); const [selectedRelease,setSelectedRelease]=useState(''); const [checking,setChecking]=useState(false); const [passwordForm,setPasswordForm]=useState({current:'',next:'',confirm:''}); const [restarting,setRestarting]=useState(null); const [maintenanceBusy,setMaintenanceBusy]=useState('')
  const loadStatus = () => api.systemStatus().then(value => { setStatus(value); setStatusError(false) }).catch(() => setStatusError(true)).finally(() => setStatusLoaded(true))
  useEffect(() => { api.settings().then(value => { setS(value); setLoadError(false) }).catch(() => setLoadError(true)); loadStatus() }, [])
  const loadReleases = async (force = false) => {
    const result = await api.updateReleases(force)
    if (!result.ok) throw new Error(t(result.error_code || result.error))
    const choices = result.releases || []
    setReleaseChoices(choices)
    setSelectedRelease(current => choices.some(item => item.latest === current)
      ? current : (choices.find(item => !item.prerelease)?.latest || choices[0]?.latest || ''))
    return result
  }
  useEffect(() => {
    if (tab !== 'backup') return
    loadReleases().catch(() => {})
    // App already checks once after login. This call normally reuses that cached result, so
    // the settings page shows the last observed version/time instead of owning an unrelated
    // empty state until the user presses the button again.
    api.checkUpdate().then(setUpdate).catch(() => {})
  }, [tab])
  useEffect(() => {
    if (!restarting) return
    let stop = false, wentDown = false
    const tick = async () => {
      if (stop) return
      try {
        // The API going away IS the restart happening; from then on the only question is when
        // it answers again. Until then the orchestrator's document is what can report failure.
        if (wentDown) { await api.authStatus(); window.location.reload(); return }
        const progress = await api.restartProgress()
        if (['failed', 'stalled'].includes(progress.state)) {
          setRestarting(null); showToast(t(progress.error_code || 'restart.error.failed')); return
        }
        if (progress.state === 'success') { setRestarting(null); showToast(t('Operation completed')); return }
      } catch (err) { wentDown = true }
      setTimeout(tick, 3000)
    }
    const timer = setTimeout(tick, 2000)
    return () => { stop = true; clearTimeout(timer) }
  }, [restarting, showToast, t])
  if (!s || !statusLoaded) return <p className={loadError || statusError ? 'u-error' : ''}>{t(loadError || statusError ? 'Loading failed' : 'Loading')}{!loadError && !statusError && '…'}</p>
  const tabs = [['general', t('General')], ['web', t('Web access')], ['voice', t('Calls & VoWiFi')], ['security', t('Security')], ['backup', t('Backup & updates')], ['maintenance', t('Maintenance')]]
  const buildCacheReclaimable = status?.host?.project_storage?.build_cache_reclaimable_bytes
  const oldImagesReclaimable = status?.host?.project_storage?.mdd_old_images_reclaimable_bytes
  const save = async () => { try { const saved = await api.saveSettings(s); setS(saved); showToast(t('Saved')) } catch (e) { showToast(e.message) } }
  const action = async name => { try { const result = name === 'backup' ? await api.createBackup() : await api.maintenance(name); showToast(result.ok ? t('Operation completed') : t('Operation completed with errors')); loadStatus() } catch (e) { showToast(e.message) } }
  const pruneBuildCache = async () => {
    if (!window.confirm(t('Clear dangling Docker build cache? Images, containers and volumes are kept.'))) return
    setMaintenanceBusy('prune_build_cache')
    try {
      const result = await api.maintenance('prune_build_cache')
      showToast(t('Build cache cleaned · {size} reclaimed', { size: formatBytes(result.space_reclaimed_bytes) }))
      loadStatus()
    } catch (e) { showToast(e.message) } finally { setMaintenanceBusy('') }
  }
  const pruneOldImages = async () => {
    if (!window.confirm(t('Delete unused old and rollback MDD images? Current images and the trusted Engine base are kept, but one-click rollback will no longer be available.'))) return
    setMaintenanceBusy('prune_old_images')
    try {
      const result = await api.maintenance('prune_old_images')
      showToast(t('Old images cleaned · {count} removed · {size} reclaimed', { count: result.removed_images, size: formatBytes(result.space_reclaimed_bytes) }))
      loadStatus()
    } catch (e) { showToast(e.message) } finally { setMaintenanceBusy('') }
  }
  const deleteBackup = async name => { if (!window.confirm(t('Delete this local backup? This cannot be undone.'))) return; try { await api.deleteBackup(name); showToast(t('Backup deleted')); loadStatus() } catch (e) { showToast(e.message) } }
  const restart = async scope => {
    if (!window.confirm(t(`restart.confirm.${scope}`))) return
    try {
      const result = await api.maintenance(`restart_${scope}`)
      if (result?.ok === false) { showToast(t(result.error_code || result.error)); return }
      setRestarting(scope)
    } catch (e) { showToast(e.message) }
  }
  const checkUpdate=async()=>{setChecking(true);try{const [result]=await Promise.all([api.checkUpdate(true),loadReleases(true)]);setUpdate(result);showToast(result.update_available?t('Update available'):(result.ok?t('Already up to date'):t(result.error_code||result.error)))}catch(e){showToast(e.message)}finally{setChecking(false)}}
  const chosenRelease = releaseChoices.find(item => item.latest === selectedRelease)
  const lastUpdateCheck = update?.last_check_at ? new Date(update.last_check_at * 1000).toLocaleString(language === 'zh' ? 'zh-CN' : 'en-US', { hour12: false }) : ''
  const changePassword=async()=>{if(passwordForm.next!==passwordForm.confirm){showToast(t('Passwords do not match'));return}try{await api.authPassword(passwordForm.current,passwordForm.next);window.location.reload()}catch(e){showToast(e.message)}}
  return <div className="u-page"><div className="u-tabs">{tabs.map(([k, l]) => <button key={k} className={tab === k ? 'active' : ''} onClick={() => setTab(k)}>{l}</button>)}</div><div className={['backup', 'maintenance'].includes(tab) ? 'u-settings-shell' : 'card u-panel'}>
    {tab === 'general' && <><h2>{t('General')}</h2><div className="u-form-grid"><div><label>{t('Language')}</label><select value={language} onChange={e => setLanguage(e.target.value)}><option value="zh">中文</option><option value="en">English</option></select></div><div><label>{t('Timezone')}</label><input list="timezones" value={s.timezone || ''} onChange={e => setS({ ...s, timezone: e.target.value })} /><datalist id="timezones"><option>Asia/Shanghai</option><option>Europe/London</option><option>America/New_York</option><option>America/Los_Angeles</option><option>Asia/Tokyo</option><option>UTC</option></datalist></div></div><h3>{t('New device defaults')}</h3><label><input type="checkbox" className="u-toggle" checked={!!s.device_defaults?.cellular_enabled} onChange={e => setS({ ...s, device_defaults: { ...s.device_defaults, cellular_enabled: e.target.checked } })} />{t('Enable 4G for newly detected modems')}</label><label><input type="checkbox" className="u-toggle" checked={s.device_defaults?.vowifi_enabled !== false} onChange={e => setS({ ...s, device_defaults: { ...s.device_defaults, vowifi_enabled: e.target.checked } })} />{t('Enable VoWiFi for newly detected modems')}</label><h3>{t('Hardware')}</h3><label><input type="checkbox" className="u-toggle" checked={s.hardware?.modem_backend === 'serial'} onChange={e => {
      const serial = e.target.checked
      if (!window.confirm(serial ? t('serialModeEnableConfirm') : t('serialModeDisableConfirm'))) return
      setS({ ...s, hardware: { ...s.hardware, modem_backend: serial ? 'serial' : 'auto' } })
    }} />{t('VoWiFi-only mode (do not run ModemManager)')}</label><p className="u-hint">{t('serialModeHint')}</p></>}
    {tab === 'web' && <><h2>{t('Web access')}</h2><label><input type="checkbox" className="u-toggle" checked={!!s.tls?.self_signed} onChange={e => setS({ ...s, tls: { ...s.tls, self_signed: e.target.checked } })} />{t('Use self-signed certificate')}</label><div className="u-form-grid"><div><label>{t('Bind address')}</label><input value={s.bind || ''} onChange={e => setS({ ...s, bind: e.target.value })} /></div><div><label>{t('HTTPS port')}</label><input type="number" value={s.http_port || 8443} onChange={e => setS({ ...s, http_port: +e.target.value })} /></div><div><label>{t('Domain')}</label><input value={s.tls?.domain || ''} onChange={e => setS({ ...s, tls: { ...s.tls, domain: e.target.value } })} /></div><div><label>{t('Certificate path')}</label><input value={s.tls?.cert_path || ''} onChange={e => setS({ ...s, tls: { ...s.tls, cert_path: e.target.value } })} /></div><div><label>{t('Private key path')}</label><input value={s.tls?.key_path || ''} onChange={e => setS({ ...s, tls: { ...s.tls, key_path: e.target.value } })} /></div></div></>}
    {tab === 'voice' && <><h2>{t('Calls & VoWiFi')}</h2><div className="u-form-grid"><div><label>{t('Ring timeout (seconds)')}</label><input type="number" value={s.ring_timeout ?? 35} onChange={e => setS({ ...s, ring_timeout: +e.target.value })} /></div><div><label>{t('Max retries')}</label><input type="number" value={s.retry?.max ?? 3} onChange={e => setS({ ...s, retry: { ...s.retry, max: +e.target.value } })} /></div><div><label>{t('Seconds per attempt')}</label><input type="number" value={s.retry?.interval ?? 30} onChange={e => setS({ ...s, retry: { ...s.retry, interval: +e.target.value } })} /></div><div><label>{t('Data channel rekey interval (minutes, 0 = not initiated by the gateway)')}</label><input type="number" value={s.rekey?.minutes ?? 30} onChange={e => setS({ ...s, rekey: { ...s.rekey, minutes: +e.target.value } })} /></div><div><label>{t('Control channel rekey interval (minutes, 0 = off)')}</label><input type="number" value={s.rekey?.ike_minutes ?? 150} onChange={e => setS({ ...s, rekey: { ...s.rekey, ike_minutes: +e.target.value } })} /></div></div>
      <p className="u-hint" style={{ margin: '4px 0 0' }}>{t('Some carriers silently expire a VoWiFi session on a fixed clock (observed: ~2h50m). The control channel (IKE) rekey renews the session before that clock fires; keep it below the shortest carrier interval. 0 disables it.')}</p>
      <h3 style={{ marginBottom: 4 }}>{t('Voicemail')}</h3>
      <p className="u-hint" style={{ margin: '0 0 8px' }}>{t('Record a message when an incoming call goes unanswered — including when no browser is open. Recordings stay on the gateway and are played from the call log.')}</p>
      <label><input type="checkbox" className="u-toggle" checked={!!s.vm_enabled} onChange={e => setS({ ...s, vm_enabled: e.target.checked })} />{t('Enable voicemail (default for new lines)')}</label>
      <div className="u-form-grid" style={{ marginTop: 10 }}>
        <div><label>{t('Ring before answering (seconds)')}</label><input type="number" min="5" max="55" value={s.vm_ring_seconds ?? 25} onChange={e => setS({ ...s, vm_ring_seconds: +e.target.value })} /></div>
        <div><label>{t('Maximum message length (seconds)')}</label><input type="number" min="30" max="300" value={s.vm_max_seconds ?? 120} onChange={e => setS({ ...s, vm_max_seconds: +e.target.value })} /></div>
      </div>
      <p className="u-hint">{t('A call you decline on the softphone is never recorded. Changing these restarts the engine of each running line, which reconnects briefly.')}</p></>}
    {tab === 'security' && <><h2>{t('Security')}</h2><div className="u-detail"><span>{t('HTTPS')}</span><b>{status?.security?.https ? t('Enabled') : t('Disabled')}</b></div><div className="u-detail"><span>{t('Certificate mode')}</span><b>{status?.security?.certificate_mode ? t(status.security.certificate_mode) : '—'}</b></div><h3>{t('Change administrator password')}</h3><div className="u-form-grid"><div><label>{t('Current password')}</label><input type="password" autoComplete="current-password" value={passwordForm.current} onChange={e=>setPasswordForm({...passwordForm,current:e.target.value})}/></div><div><label>{t('New password (at least 10 characters)')}</label><input type="password" autoComplete="new-password" minLength="10" value={passwordForm.next} onChange={e=>setPasswordForm({...passwordForm,next:e.target.value})}/></div><div><label>{t('Confirm password')}</label><input type="password" autoComplete="new-password" minLength="10" value={passwordForm.confirm} onChange={e=>setPasswordForm({...passwordForm,confirm:e.target.value})}/></div></div><button className="btn btn-ghost" disabled={!passwordForm.current||passwordForm.next.length<10||!passwordForm.confirm} onClick={changePassword}>{t('Change password')}</button><label><input type="checkbox" className="u-toggle" checked={s.security?.audit_enabled !== false} onChange={e => setS({ ...s, security: { ...s.security, audit_enabled: e.target.checked } })} />{t('Record administrative operations')}</label><label>{t('Trusted reverse proxies (comma-separated)')}</label><input value={(s.security?.trusted_proxies || []).join(', ')} onChange={e => setS({ ...s, security: { ...s.security, trusted_proxies: e.target.value.split(',').map(x => x.trim()).filter(Boolean) } })} /></>}
    {tab === 'backup' && <div className="u-settings-stack">
      <div className="u-settings-grid">
        <section className="card u-panel u-settings-card">
          <div className="u-settings-card-head"><div><h2>{t('Local backups')}</h2><p>{t('Backups stay encrypted by host permissions and are not downloaded through the browser.')}</p></div><button className="btn btn-primary" onClick={() => action('backup')}>{t('Create local backup')}</button></div>
          <div className="u-settings-list">{(status?.backups || []).map(item => <div className="u-backup-item" key={item.name}><div><span>{item.name}</span><b>{formatBytes(item.size)} · {new Date(item.created_at * 1000).toLocaleString()}</b></div><button className="btn btn-danger-outline" onClick={() => deleteBackup(item.name)}>{t('Delete')}</button></div>)}</div>
        </section>
        <section className="card u-panel u-settings-card">
          <div className="u-settings-card-head"><div><h2>{t('Software updates')}</h2><p>{t('Check the latest release and review it before a manual update.')}</p></div><button className="btn btn-ghost" disabled={checking} onClick={checkUpdate}>{t(checking?'Checking…':'Check for updates')}</button></div>
          <div className="u-settings-facts"><div><span>{t('Running version')}</span><b>v{status?.version || '—'}</b></div><div><span>{t('Latest version')}</span><b>{update?.latest ? `v${update.latest}` : t(update ? 'Update check failed' : 'Not checked')}</b>{lastUpdateCheck&&<small>{t('Last checked: {time}', { time: lastUpdateCheck })}</small>}</div></div>
          <div className="u-release-picker">
            <label>{t('Select update version')}</label>
            <div>
              <select value={selectedRelease} disabled={checking || !releaseChoices.length} onChange={e=>setSelectedRelease(e.target.value)}>
                {!releaseChoices.length&&<option value="">{t(checking?'Checking…':'No published versions')}</option>}
                {!!releaseChoices.some(item=>!item.prerelease)&&<optgroup label={t('Normal version')}>{releaseChoices.filter(item=>!item.prerelease).map(item=><option key={item.latest} value={item.latest}>v{item.latest}{item.latest===status?.version?` · ${t('Currently installed')}`:''}</option>)}</optgroup>}
                {!!releaseChoices.some(item=>item.prerelease)&&<optgroup label={t('Test versions')}>{releaseChoices.filter(item=>item.prerelease).map(item=><option key={item.latest} value={item.latest}>v{item.latest}{item.latest===status?.version?` · ${t('Currently installed')}`:''}</option>)}</optgroup>}
              </select>
              <button className="btn btn-primary" disabled={!chosenRelease||chosenRelease.latest===status?.version} onClick={()=>openUpdateDialog(chosenRelease)}>{t(chosenRelease?.prerelease?'Install test version':'Switch to normal version')}</button>
            </div>
            <p className="u-hint">{t('Test versions are published prereleases. You can switch back to the latest normal version here at any time.')}</p>
          </div>
          {update?.update_available&&<div className="u-note u-update-note"><span>{t('A new version is available. Review the release notes before updating.')}</span><button className="btn btn-primary" onClick={()=>openUpdateDialog(update)}>{t('Review update')}</button></div>}
          {update&&!update.ok&&<p className="u-error">{t(update.error_code||update.error)}</p>}
        </section>
      </div>
      <section className="card u-panel u-settings-card">
        <div className="u-settings-card-head"><div><h2>{t('Update policy')}</h2><p>{t('Choose automatic installation or notify-only, then select which versions it applies to.')}</p></div></div>
        <div className="u-form-grid">
          <div className="u-update-network-row"><label>{t('Connection')}</label><select value={s.updates?.proxy_mode || 'auto'} onChange={e => { const mode = e.target.value; setS({ ...s, updates: { ...s.updates, proxy_mode: mode, proxy_profile_id: mode === 'library' ? (s.updates?.proxy_profile_id || '') : '', proxy_country: mode === 'country' ? (s.updates?.proxy_country || '') : '' } }) }}><option value="auto">{t('Automatic — direct, then proxy library')}</option><option value="direct">{t('Direct only')}</option><option value="library">{t('Proxy library')}</option><option value="country">{t('Country exit')}</option></select></div>
          {s.updates?.proxy_mode === 'library' && <div className="u-update-network-row"><label>{t('Proxy')}</label><select value={s.updates?.proxy_profile_id || ''} onChange={e => setS({ ...s, updates: { ...s.updates, proxy_mode: 'library', proxy_profile_id: e.target.value } })}><option value="">{t('Select a proxy…')}</option>{selectableProxyProfiles(s).map(([id, profile]) => <option key={id} value={id}>{profile.name || t('Unnamed proxy')}</option>)}</select></div>}
          {s.updates?.proxy_mode === 'country' && <div className="u-update-network-row"><label>{t('Country exit')}</label><select value={s.updates?.proxy_country || ''} onChange={e => setS({ ...s, updates: { ...s.updates, proxy_mode: 'country', proxy_country: e.target.value } })}><option value="">{t('Select a country/region…')}</option>{Object.keys(s.proxy?.exits || {}).map(country => <option key={country} value={country}>{country.toUpperCase()}</option>)}</select></div>}
          <p className="u-note u-update-policy-note">{s.updates?.proxy_mode === 'library' ? t('SOCKS5 entries connect directly. Individual node entries reuse a ready country exit assigned to that proxy; subscriptions are selected through Country exit.') : s.updates?.proxy_mode === 'country' ? t('The selected country exit must be ready; update checks and downloads use its verified proxy route.') : s.updates?.proxy_mode === 'direct' ? t('Update checks and downloads connect directly without a proxy.') : t('Automatic tries a direct connection first, then the available proxy library entries. The route that passes the check is reused for the download.')}</p>
          <div><label>{t('Update method')}</label><select value={s.updates?.update_mode || 'automatic'} onChange={e => { const updateMode = e.target.value; setS({ ...s, updates: { ...s.updates, update_mode: updateMode, version_scope: updateMode === 'automatic' ? 'main' : 'all' } }) }}><option value="automatic">{t('Automatic update')}</option><option value="notify">{t('Notify before updating')}</option></select></div>
          <div><label>{t('Version range')}</label><select value={s.updates?.version_scope || (s.updates?.update_mode === 'notify' ? 'all' : 'main')} onChange={e => setS({ ...s, updates: { ...s.updates, version_scope: e.target.value } })}><option value="main">{t('Main versions only')}</option><option value="all">{t('All versions')}</option></select></div>
          <p className="u-note u-update-policy-note">{s.updates?.update_mode !== 'notify' ? t('The All versions option follows the approved latest Release. Main versions only follows the independently configured stable main Release, even when newer patches exist.') : t('Matching releases only send one notice. Installation starts only after you review and confirm it manually.')}</p>
        </div>
        <div className="u-settings-actions"><button className="btn btn-primary" onClick={save}>{t('Save update settings')}</button></div>
      </section>
    </div>}
    {tab === 'maintenance' && <div className="u-settings-grid u-maintenance-grid">
      <section className="card u-panel u-settings-card"><div className="u-settings-card-head"><div><h2>{t('Routine maintenance')}</h2><p>{t('Refresh runtime state without restarting the host.')}</p></div></div><div className="u-action-list"><button className="btn btn-ghost" onClick={() => action('restart_lines')}>{t('Restart all VoWiFi lines')}</button><button className="btn btn-ghost" onClick={() => action('refresh_egress')}>{t('Refresh country exits')}</button><button className="btn btn-ghost" onClick={() => action('clear_notification_history')}>{t('Clear notification history')}</button><button className="btn btn-ghost" disabled={!!maintenanceBusy} onClick={pruneOldImages}>{maintenanceBusy === 'prune_old_images' ? t('Cleaning old images…') : oldImagesReclaimable != null ? t('Clear old and rollback images · {size} reclaimable', { size: formatBytes(oldImagesReclaimable) }) : t('Clear old and rollback images')}</button><button className="btn btn-ghost" disabled={!!maintenanceBusy} onClick={pruneBuildCache}>{maintenanceBusy === 'prune_build_cache' ? t('Cleaning build cache…') : buildCacheReclaimable != null ? t('Clear build cache · {size} reclaimable', { size: formatBytes(buildCacheReclaimable) }) : t('Clear build cache')}</button></div><p className="u-hint">{t('Old-image cleanup keeps the current images, trusted Engine base and every image used by a container, but removes rollback images. Build-cache cleanup removes only dangling builder records; containers and volumes are always kept.')}</p></section>
      <section className="card u-panel u-settings-card"><div className="u-settings-card-head"><div><h2>{t('Restart')}</h2><p>{t('Ordered by how much they interrupt: the control plane can be restarted without touching a call, the host cannot.')}</p></div></div><div className="u-action-list">
          <button className="btn btn-ghost" disabled={!!restarting} onClick={() => restart('control')}>{t('Restart the control plane')}</button>
          <button className="btn btn-ghost" disabled={!!restarting} onClick={() => restart('services')}>{t('Restart all gateway services')}</button>
          <button className="btn btn-ghost" disabled={!!restarting} onClick={() => restart('host')}>{t('Restart the host')}</button>
        </div>{restarting && <p className="u-note u-restart-note">{t(`restart.waiting.${restarting}`)}</p>}</section>
    </div>}
  </div>{!['backup', 'maintenance'].includes(tab) && <button className="btn btn-primary" onClick={save}>{t('Save')}</button>}</div>
}

const HOST_ALERT_TEXT = {
  undervoltage_now: 'Power is browning out right now. The network port, cellular module and card reader share this supply rail, so every line drops at the same instant.',
  undervoltage_seen: 'Under-voltage has been detected on this host. Every line drops at the moment it happens.',
  throttled_now: 'The CPU is being throttled or frequency-capped.',
  temperature_high: 'Host temperature is high enough to throttle.',
  disk_critical: 'The disk is nearly full; history and runtime state may fail to write.',
  disk_low: 'Disk space is running low.',
  swap_pressure: 'Swap is being paged actively; on an SD card this slows every operation and times out status reads.',
  default_route_changed: 'The default route moved between uplinks, changing the source address every outbound connection uses.',
}

function formatDuration(seconds, t) {
  const days = Math.floor(seconds / 86400), hours = Math.floor((seconds % 86400) / 3600)
  return days ? t('{days}d {hours}h', { days, hours }) : t('{hours}h {minutes}m', { hours, minutes: Math.floor((seconds % 3600) / 60) })
}

function Row({ label, children }) {
  return <div className="u-detail"><span>{label}</span><b>{children}</b></div>
}

// The host is the layer that takes every line down at once and the one nothing else reports:
// the NIC shows no errors, the link stays up, and a brown-out only surfaces minutes later as
// unexplained packet loss. This is where that evidence lives.
function HostPanel({ host, alerts, loading, clearing, onClear, t }) {
  if (loading) return <Empty title={t('Reading host information…')} detail={t('Collecting power, storage, memory and network status.')} />
  if (!host?.model && !host?.memory) return <Empty title={t('Host information unavailable')} detail={t('The control plane has not sampled the host yet.')} />
  const mem = host.memory || {}, disk = host.disk || {}, project = host.project_storage || {}, load = host.load || {}, net = host.network || {}
  const throttle = host.throttling || {}
  const sticky = throttle.since_boot || [], now = throttle.now || []
  return <div className="u-device-grid">
    {!!alerts.length && <div className="card u-panel" style={{ gridColumn: '1 / -1' }}>
      <div className="u-card-head"><h3>{t('Needs attention')}</h3><button className="btn btn-ghost" disabled={clearing} onClick={onClear}>{t(clearing ? 'Clearing…' : 'Clear')}</button></div>
      {alerts.map(a => <p key={a.code} className={a.severity === 'critical' ? 'u-error' : 'u-note'} style={{ marginTop: 8 }}>
        {t(HOST_ALERT_TEXT[a.code] || a.code)}
        {a.detail?.events ? ' ' + t('({count} events, last {last})', { count: a.detail.events, last: a.detail.last || '—' }) : ''}
      </p>)}
    </div>}

    <div className="card u-panel">
      <h3>{t('Machine')}</h3>
      <Row label={t('Model')}>{host.model || '—'}</Row>
      <Row label={t('Uptime')}>{host.uptime_seconds ? formatDuration(host.uptime_seconds, t) : '—'}</Row>
      <Row label={t('Temperature')}>{host.temperature_c != null ? `${host.temperature_c} °C` : '—'}</Row>
      <Row label={t('CPU frequency')}>{host.cpu_mhz ? `${host.cpu_mhz} MHz` : '—'}</Row>
      <Row label={t('Load (1m / per core)')}>{load['1m'] != null ? `${load['1m']} / ${load.per_core} (${load.cores} ${t('cores')})` : '—'}</Row>
    </div>

    <div className="card u-panel">
      <h3>{t('Memory and storage')}</h3>
      <Row label={t('Memory')}>{mem.total_mb ? t('{used}% of {total} MB used', { used: mem.used_percent, total: mem.total_mb }) : '—'}</Row>
      <Row label={t('Swap')}>{mem.swap_total_mb ? t('{used} MB of {total} MB ({percent}%)', { used: mem.swap_used_mb, total: mem.swap_total_mb, percent: mem.swap_used_percent }) : '—'}</Row>
      <Row label={t('Disk')}>{disk.total_bytes ? t('{percent}% used · {used} / {total} · {free} available', { percent: disk.used_percent, used: formatBytes(disk.used_bytes), total: formatBytes(disk.total_bytes), free: formatBytes(disk.free_bytes) }) : '—'}</Row>
      <Row label={t(project.known_total_is_logical ? 'MDD reported usage (logical)' : 'MDD storage on disk')}>{project.known_total_bytes != null ? formatBytes(project.known_total_bytes) : '—'}</Row>
      <Row label={t('Project files')}>{project.files_bytes != null ? formatBytes(project.files_bytes) : '—'}</Row>
      {project.docker_images_bytes != null && <Row label={t('MDD image virtual sizes (shared layers counted repeatedly)')}>{formatBytes(project.docker_images_bytes)}</Row>}
      {project.docker_image_layers_bytes != null && <Row label={t(project.docker_images_all_managed ? 'MDD Docker image layers on disk' : 'All Docker image layers on disk')}>{t('{total} · {reclaimable} reclaimable', { total: formatBytes(project.docker_image_layers_bytes), reclaimable: formatBytes(project.docker_image_reclaimable_bytes) })}</Row>}
      {!!project.container_writable_bytes && <Row label={t('Container writable layers')}>{formatBytes(project.container_writable_bytes)}</Row>}
      {project.build_cache_bytes != null && <Row label={t('Shared Docker build cache')}>{t('{total} · {reclaimable} reclaimable', { total: formatBytes(project.build_cache_bytes), reclaimable: formatBytes(project.build_cache_reclaimable_bytes) })}</Row>}
    </div>

    <div className="card u-panel">
      <h3>{t('Network')}</h3>
      {(net.addresses || []).map(a => <Row key={`${a.interface}-${a.address}`} label={a.interface}>{a.address}</Row>)}
      {!net.addresses?.length && <Row label={t('Addresses')}>—</Row>}
      <Row label={t('Default route')}>{(net.default_interfaces || []).join(', ') || '—'}</Row>
      {net.usb_attached && <Row label={t('Uplink attachment')}>{t('USB — shares its bus and power with the modem and reader')}</Row>}
      {!!net.counters && <Row label={t('Interface errors / dropped')}>{`${(net.counters.rx_errors ?? 0) + (net.counters.tx_errors ?? 0)} / ${(net.counters.rx_dropped ?? 0) + (net.counters.tx_dropped ?? 0)}`}</Row>}
    </div>

    {(!!throttle.raw || !!host.undervoltage) && <div className="card u-panel">
      <h3>{t('Power and throttling')}</h3>
      <Row label={t('Right now')}>{now.length ? now.map(x => t(`throttle.${x}`)).join(', ') : t('Normal')}</Row>
      <Row label={t('Since boot')}>{sticky.length ? sticky.map(x => t(`throttle.${x}`)).join(', ') : t('Nothing recorded')}</Row>
      {!!host.undervoltage?.count && <Row label={t('Under-voltage events')}>{t('{count} · last {last}', { count: host.undervoltage.count, last: host.undervoltage.last || '—' })}</Row>}
      <p className="u-note">{t('Under-voltage is invisible everywhere else: the link stays up and the interface reports no errors, so it surfaces minutes later as packet loss on every line at once.')}</p>
    </div>}

    {!!host.usb_devices?.length && <div className="card u-panel">
      <h3>{t('USB devices')}</h3>
      {host.usb_devices.map((d, i) => <Row key={`${d}-${i}`} label={`#${i + 1}`}>{d}</Row>)}
    </div>}
  </div>
}

export function DiagnosticsPage(props) {
  const { t } = useI18n(); const [tab, setTab] = useState('health'); const [results, setResults] = useState({}); const { devices } = props
  const [system, setSystem] = useState(null)
  const [hostLoading, setHostLoading] = useState(true)
  const [clearingAlerts, setClearingAlerts] = useState(false)
  // The host is where an outage that hits every line at once comes from, so this refreshes
  // on its own rather than showing whatever was true when the page was opened.
  useEffect(() => {
    const load = () => api.systemStatus().then(value => { setSystem(value); props.setSystemMeta?.(value) }).catch(() => {}).finally(() => setHostLoading(false))
    load(); const timer = setInterval(load, 30 * 1000); return () => clearInterval(timer)
  }, [])
  const host = system?.host || {}
  const hostAlerts = system?.host_alerts || []
  const issueUrl = `${(system?.repository_url || 'https://github.com/MddIdd/mdd-sim-gateway').replace(/\/$/, '')}/issues/new/choose`
  const clearHostAlerts = async () => { try { setClearingAlerts(true); await api.clearHostAlerts(); const next = { ...(system || {}), host_alerts: [] }; setSystem(next); props.setSystemMeta?.(s => ({ ...s, host_alerts: [] })); props.showToast(t('Host alerts cleared')) } catch (e) { props.showToast(e.message) } finally { setClearingAlerts(false) } }
  const run = async d => { try { const result = await api.deviceDiagnostics(d.id); setResults(x => ({ ...x, [d.id]: result })); props.showToast(result.ok ? t('Diagnostics passed') : t('Diagnostics found problems')) } catch (e) { props.showToast(e.message) } }
  return <div className="u-page"><div className="u-tabs"><button className={tab === 'health' ? 'active' : ''} onClick={() => setTab('health')}>{t('Health')}</button><button className={tab === 'host' ? 'active' : ''} onClick={() => setTab('host')}>{t('Host')}{!!hostAlerts.length && <i className={`u-nav-dot ${hostAlerts.some(a => a.severity === 'critical') ? 'critical' : 'warning'}`} />}</button><button className={tab === 'logs' ? 'active' : ''} onClick={() => setTab('logs')}>{t('Live logs')}</button><button className={tab === 'bundle' ? 'active' : ''} onClick={() => setTab('bundle')}>{t('Support bundle')}</button></div>
    {tab === 'health' && props.initialLoading ? <p role="status">{t('Loading')}…</p> : tab === 'health' && props.loadErrors?.devices && !devices.length ? <p className="u-error">{t('Loading failed')}</p> : tab === 'health' && <div className="u-device-grid">{devices.map((d, i) => <div className="card u-panel" key={d.id}><h3>{deviceTitle(d, i)}</h3><div className="u-detail"><span>{t('4G network')}</span><Badge state={capability(d, 'cellular').actual} /></div><div className="u-detail"><span>VoWiFi / IMS</span><Badge state={capability(d, 'vowifi').actual} /></div><button className="btn btn-ghost" onClick={() => run(d)}>{t('Run diagnostics')}</button>{results[d.id]?.checks?.map(check => <div className="u-detail" key={check.name}><span>{check.name}</span><b>{check.ok ? '✓' : '✕'} {check.detail}</b></div>)}</div>)}</div>}
    {tab === 'host' && <HostPanel host={host} alerts={hostAlerts} loading={hostLoading} clearing={clearingAlerts} onClear={clearHostAlerts} t={t} />}
    {tab === 'logs' && <Logs {...props} />}
    {tab === 'bundle' && <div className="card u-panel"><h2>{t('Redacted support bundle')}</h2><p>{t('Contains status, configuration shape and bounded logs. SIM identities, phone numbers, credentials and cryptographic material are removed.')}</p><div className="u-support-actions"><a className="btn btn-primary" href={api.supportBundleUrl}>{t('Download support bundle')}</a><div><b>{t('Found a problem or have a suggestion?')}</b><p>{t('Open a GitHub Issue. For faults, attach the redacted support bundle when appropriate.')}</p><a href={issueUrl} target="_blank" rel="noreferrer">{t('Submit an Issue')} ↗</a></div></div></div>}
  </div>
}
