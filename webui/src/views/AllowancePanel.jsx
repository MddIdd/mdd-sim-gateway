import React, { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { useI18n } from '../i18n.jsx'

const FIELDS = [
  ['balance', 'Balance'], ['sms_remaining', 'SMS remaining'],
  ['data_remaining', 'Data remaining'], ['voice_remaining', 'Voice remaining'],
  ['valid_until', 'Valid until'], ['activated_at', 'Activated at'],
]
const EMPTY = Object.fromEntries(FIELDS.map(([key]) => [key, '']))

export default function AllowancePanel({ instanceId, mode = 'overview', transport = 'auto', showToast }) {
  const { t, language } = useI18n()
  const [value, setValue] = useState({ ...EMPTY })
  const [draft, setDraft] = useState({ ...EMPTY })
  const [rule, setRule] = useState(null)
  const [ruleDraft, setRuleDraft] = useState({ recipient: '', body: '' })
  const [editing, setEditing] = useState(false)
  const [editingRule, setEditingRule] = useState(false)
  const [busy, setBusy] = useState(false)
  const pollRef = useRef(null)
  const activeId = useRef(instanceId)
  activeId.current = instanceId

  const toast = (message) => showToast?.(message)
  const load = useCallback(async () => {
    if (!instanceId) return
    const forId = String(instanceId)
    try {
      const [snapshot, queryRule] = await Promise.all([
        api.allowance(forId), api.allowanceQueryRule(forId),
      ])
      if (String(activeId.current) !== forId) return
      setValue(snapshot.allowance || { ...EMPTY })
      setDraft({ ...EMPTY, ...(snapshot.allowance || {}) })
      setRule(queryRule.rule)
      const effective = queryRule.rule?.effective || {}
      setRuleDraft({ recipient: effective.recipient || '', body: effective.body || '' })
    } catch (error) { toast(`${t('Could not load allowance data')}: ${error.message}`) }
  }, [instanceId, t])

  useEffect(() => {
    clearInterval(pollRef.current)
    setEditing(false); setEditingRule(false); setRule(null); setValue({ ...EMPTY })
    load()
    return () => clearInterval(pollRef.current)
  }, [load])

  const saveManual = async () => {
    setBusy(true)
    try {
      const result = await api.saveAllowance(instanceId, draft)
      setValue(result.allowance); setDraft(result.allowance); setEditing(false)
      toast(t('Allowance data saved'))
    } catch (error) { toast(`${t('Save failed')}: ${error.message}`) }
    finally { setBusy(false) }
  }

  const beginPolling = (previousTs) => {
    clearInterval(pollRef.current)
    let attempts = 0
    pollRef.current = setInterval(async () => {
      attempts += 1
      try {
        const result = await api.allowance(instanceId)
        if (String(activeId.current) !== String(instanceId)) return
        const next = result.allowance || { ...EMPTY }
        setValue(next); setDraft({ ...EMPTY, ...next })
        if (next.source === 'sms' && Number(next.updated_ts || 0) > Number(previousTs || 0)) {
          clearInterval(pollRef.current)
          toast(t('Allowance reply received and cached'))
        }
      } catch { /* keep the bounded poll alive */ }
      if (attempts >= 24) clearInterval(pollRef.current)
    }, 2500)
  }

  const query = async () => {
    if (!rule?.effective) {
      if (mode === 'messages') setEditingRule(true)
      else toast(t('The query method for this carrier is unknown. Configure it in Messages.'))
      return
    }
    const { recipient, body } = rule.effective
    if (!window.confirm(t('Send “{body}” to {recipient} to query the allowance? SMS charges may apply.', { body, recipient }))) return
    setBusy(true)
    const previousTs = value.updated_ts
    try {
      const result = await api.queryAllowance(instanceId, mode === 'messages' ? transport : 'auto')
      if (result.ok === false) {
        toast(t('The query SMS was submitted with an uncertain result. Check Messages before retrying.'))
      } else {
        toast(t('Query SMS sent; waiting for the carrier reply'))
      }
      beginPolling(previousTs)
    } catch (error) { toast(`${t('Query failed')}: ${error.message}`) }
    finally { setBusy(false) }
  }

  const saveRule = async () => {
    setBusy(true)
    try {
      const result = await api.saveAllowanceQueryRule(instanceId, ruleDraft)
      setRule(result.rule); setRuleDraft(result.rule.effective); setEditingRule(false)
      toast(t('Allowance query method saved'))
    } catch (error) { toast(`${t('Save failed')}: ${error.message}`) }
    finally { setBusy(false) }
  }

  const resetRule = async () => {
    if (!window.confirm(t('Restore this carrier’s default allowance query method?'))) return
    setBusy(true)
    try {
      const result = await api.resetAllowanceQueryRule(instanceId)
      setRule(result.rule)
      const effective = result.rule.effective || {}
      setRuleDraft({ recipient: effective.recipient || '', body: effective.body || '' })
      setEditingRule(false); toast(t('Default query method restored'))
    } catch (error) { toast(`${t('Restore failed')}: ${error.message}`) }
    finally { setBusy(false) }
  }

  const updated = value.updated_ts
    ? new Date(value.updated_ts * 1000).toLocaleString(language === 'zh' ? 'zh-CN' : 'en-GB') : t('Not recorded')
  const compact = mode === 'messages'
  return <div className="card" style={{ padding: compact ? 12 : 14, marginTop: compact ? 0 : 12, marginBottom: compact ? 12 : 0 }}>
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
      <div style={{ flex: 1, minWidth: 180 }}><b>{t('Balance and allowance')}</b>
        <div style={{ color: 'var(--text-mute)', fontSize: 11 }}>{t('Updated')}: {updated}{value.source === 'sms' ? ` · ${t('Carrier SMS')}` : ''}</div></div>
      {mode === 'overview' && <button className="btn btn-ghost" disabled={busy} onClick={() => { setDraft({ ...EMPTY, ...value }); setEditing(!editing) }}>{editing ? t('Cancel') : t('Edit')}</button>}
      <button className="btn btn-primary" disabled={busy} onClick={query}>{busy ? t('Working…') : t('Query allowance')}</button>
      {mode === 'messages' && <button className="btn btn-ghost" disabled={busy} onClick={() => setEditingRule(!editingRule)}>{t('Query settings')}</button>}
    </div>
    {!editing && <div className="u-details cols" style={compact ? {
      marginTop: 10, gridTemplateColumns: 'repeat(6, minmax(110px, 1fr))',
      gap: '0 14px', overflowX: 'auto',
    } : { marginTop: 10 }}>
      {FIELDS.map(([key, label]) => <div className="u-detail" key={key} style={compact ? {
        minWidth: 0, alignItems: 'flex-start', flexDirection: 'column', justifyContent: 'center', gap: 3,
      } : undefined}><span>{t(label)}</span><b style={compact ? { textAlign: 'left' } : undefined}>{value[key] || (key === 'activated_at' ? t('Configure to enable reminders') : '—')}</b></div>)}
    </div>}
    {mode === 'overview' && editing && <div style={{ marginTop: 10 }}>
      <div className="u-details cols">{FIELDS.map(([key, label]) => <label className="u-detail" key={key}><span>{t(label)}</span><input type={key === 'activated_at' ? 'date' : 'text'} value={draft[key] || ''} maxLength={160} onChange={event => setDraft(current => ({ ...current, [key]: event.target.value }))} /></label>)}</div>
      <div style={{ marginTop: 10, textAlign: 'right' }}><button className="btn btn-primary" disabled={busy} onClick={saveManual}>{t('Save')}</button></div>
    </div>}
    {mode === 'messages' && editingRule && <div style={{ marginTop: 12, borderTop: '1px solid var(--border)', paddingTop: 12 }}>
      <p className="u-note" style={{ marginTop: 0 }}>{rule?.known
        ? t('This carrier has a built-in method. Saving below creates an override; you can restore the default later.')
        : t('The carrier is unknown. Enter the service number and exact SMS query text supplied by the carrier.')}</p>
      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(120px, 1fr) minmax(180px, 2fr)', gap: 8 }}>
        <label><span>{t('Service number')}</span><input value={ruleDraft.recipient} maxLength={32} onChange={event => setRuleDraft(current => ({ ...current, recipient: event.target.value }))} /></label>
        <label><span>{t('Query text')}</span><input value={ruleDraft.body} maxLength={500} onChange={event => setRuleDraft(current => ({ ...current, body: event.target.value }))} /></label>
      </div>
      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 10 }}>
        {rule?.known && rule?.custom && <button className="btn btn-ghost" disabled={busy} onClick={resetRule}>{t('Restore default')}</button>}
        <button className="btn btn-primary" disabled={busy || !ruleDraft.recipient || !ruleDraft.body} onClick={saveRule}>{t('Save query method')}</button>
      </div>
    </div>}
  </div>
}
