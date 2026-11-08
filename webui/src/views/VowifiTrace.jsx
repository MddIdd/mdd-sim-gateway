import React, { useEffect, useState } from 'react'
import { api } from '../api.js'
import { useI18n } from '../i18n.jsx'

export default function VowifiTrace({ devices = [], showToast }) {
  const { t } = useI18n()
  const lines = devices.filter(device => device.instance_id)
  const [line, setLine] = useState('')
  const [state, setState] = useState(null)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState('incoming_failed')
  const [error, setError] = useState('')
  useEffect(() => {
    if (!lines.some(device => String(device.instance_id) === line)) {
      setLine(lines.length ? String(lines[0].instance_id) : '')
    }
  }, [devices, line])
  useEffect(() => {
    setState(null); setError('')
    if (!line) return
    let cancelled = false
    const load = () => api.vowifiTrace(line).then(value => {
      if (!cancelled) { setState(value); setError('') }
    }).catch(() => { if (!cancelled) setError(t('Recording status unavailable')) })
    load()
    const timer = setInterval(load, 5000)
    return () => { cancelled = true; clearInterval(timer) }
  }, [line, t])
  const act = async action => {
    setBusy(true); setError('')
    try {
      const value = await action()
      if (value?.result) {
        showToast?.(t('Test result marked'))
      } else setState(value)
    } catch (failure) { setError(t(failure.message || 'Recording request failed')) }
    finally { setBusy(false) }
  }
  const active = !!state?.active
  return <div className="card u-panel" style={{ marginBottom: 16 }}>
    <h2>{t('VoWiFi recording')}</h2>
    <p className="u-note">{t('Record one line for up to 60 minutes while testing incoming calls. Automatic recovery stays enabled. The support bundle includes anonymized signaling and connection evidence.')}</p>
    {!lines.length ? <p className="u-note">{t('Configure a VoWiFi line before recording.')}</p> : <>
      <label className="field"><span>{t('Line')}</span><select aria-label={t('Recording line')} value={line} disabled={busy} onChange={event => setLine(event.target.value)}>
        {lines.map(device => <option key={device.instance_id} value={String(device.instance_id)}>{device.display_name || device.hardware_name || device.name || device.label || `#${device.instance_id}`}</option>)}
      </select></label>
      {state && <p role="status" className="u-note">{active
        ? state.recording ? t('Recording · {minutes} min remaining', { minutes: Math.ceil(state.remaining_seconds / 60) }) : t('Waiting for the line recorder…')
        : state.has_records ? t('Recording stopped. Download the support bundle to share the results.') : t('Recording is off')}
      </p>}
      {active && !state?.recording && state?.created_at && Date.now() / 1000 - state.created_at > 20 && <p className="u-error">{t('The recorder is not responding. Check that the Engine was upgraded and VoWiFi is running.')}</p>}
      {active && state?.recording && (state.unavailable?.length > 0 || state.packet_capture !== 'running') && <p className="u-note">{t('Some connection evidence is unavailable; available evidence will still be included.')}</p>}
      {error && <p className="u-error" role="alert">{error}</p>}
      <div className="u-inline-actions" style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 12 }}>
        {active ? <button className="btn btn-ghost" disabled={busy} onClick={() => act(() => api.stopVowifiTrace(line))}>{t('Stop recording')}</button>
          : <button className="btn btn-primary" disabled={busy || !state} onClick={() => act(() => api.startVowifiTrace(line))}>{t('Start 60-minute recording')}</button>}
        {active && <><select aria-label={t('Test result')} value={result} disabled={busy} onChange={event => setResult(event.target.value)}>
          <option value="incoming_failed">{t('Incoming call failed')}</option><option value="incoming_rang">{t('Incoming call rang')}</option><option value="incoming_answered">{t('Incoming call answered')}</option>
        </select><button className="btn btn-ghost" disabled={busy || !state?.recording} onClick={() => act(() => api.markVowifiTrace(line, result))}>{t('Mark test result')}</button></>}
      </div>
      <p className="u-note" style={{ marginTop: 12 }}>{t('Start recording before testing. When a call fails, mark the result and leave the line unchanged for another minute, then stop and download the support bundle.')}</p>
    </>}
  </div>
}
