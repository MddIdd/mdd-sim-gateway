import React, { useEffect, useState } from 'react'
import { api } from '../api.js'
import { useI18n } from '../i18n.jsx'

const emptyInstance = () => ({
  id: '', name: '', imsi: '', mcc: '', mnc: '', imei: '', imeisv: '', pin: '', reader: '', proxy_country: '',
  reader_index: 0, reader_port: '', msisdn: '', smsc: '', enabled: true, apn: 'ims', idr_mode: 'apn', cp_mode: 'auto',
  sip: { webrtc: { enable: true } },
  debug: { asterisk: false, charon: false },
})

function Field({ label, children }) {
  return <div><label>{label}</label>{children}</div>
}

function nextInstanceId(instances) {
  const used = new Set(instances.map((item) => String(item.id)))
  let candidate = 1
  while (used.has(String(candidate))) candidate += 1
  return String(candidate)
}

export default function SimConfig({ instances, selected, refresh, cards, setSelected, targetDevice }) {
  const { t } = useI18n()
  const [readers, setReaders] = useState([])
  const [readersLoading, setReadersLoading] = useState(true)
  const [card, setCard] = useState(null)
  const [pin, setPin] = useState('')
  const [pinMsg, setPinMsg] = useState('')
  const [form, setForm] = useState(emptyInstance())
  const [saving, setSaving] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [deleteHistory, setDeleteHistory] = useState(true)
  const [creating, setCreating] = useState(false)
  const [savedLineId, setSavedLineId] = useState('')
  const [smscMode, setSmscMode] = useState('auto')   // 'auto' = read from SIM, 'manual' = typed
  // Only to label the "use the system default" option with what that default currently is,
  // so the choice does not require a trip to the settings page to interpret.
  const [systemDefaultVm, setSystemDefaultVm] = useState(null)
  useEffect(() => {
    let cancelled = false
    api.settings().then((s) => { if (!cancelled) setSystemDefaultVm(!!s.vm_enabled) })
      .catch(() => {})
    return () => { cancelled = true }
  }, [])

  // Refresh the physical-reader list whenever the detected-card set changes (hotplug).
  // pcsc-lite can briefly reject a new context while the modem bridge reconnects. Keep the
  // last useful list (or derive it from the card monitor) and retry instead of replacing the
  // selector with the misleading "No readers" state after one failed request.
  useEffect(() => {
    let cancelled = false
    let retryTimer
    const cached = [...cards]
      .filter((item) => item.name && item.index != null)
      .sort((a, b) => a.index - b.index)
      .map((item) => item.name)
    const load = async () => {
      if (!readers.length) setReadersLoading(true)
      try {
        const result = await api.readers()
        if (cancelled) return
        const next = Array.isArray(result.readers) ? result.readers : []
        setReaders((previous) => next.length ? next : previous.length ? previous : cached)
        setReadersLoading(false)
        if (result.stale) retryTimer = setTimeout(load, 2000)
      } catch {
        if (cancelled) return
        setReaders((previous) => previous.length ? previous : cached)
        setReadersLoading(false)
        retryTimer = setTimeout(load, 2000)
      }
    }
    load()
    return () => { cancelled = true; clearTimeout(retryTimer) }
  }, [cards.map((c) => `${c.index}:${c.name}`).join('|')])
  const boundLineId = String(targetDevice?.instance_id || '')
  const managedSelected = selected && String(selected.id) === String(boundLineId || savedLineId)
    ? selected : null
  useEffect(() => {
    if (!managedSelected) {
      if (targetDevice?.present === false) setForm(emptyInstance())
      return
    }
    setCreating(managedSelected.provisioning_state === 'draft')
    setForm({ ...emptyInstance(), ...managedSelected })
  }, [managedSelected?.id, managedSelected?.provisioning_state, targetDevice?.present])
  // Opening the SIM tab for an unconfigured physical reader starts a new-line form bound to
  // that reader. This avoids silently editing the currently selected (unrelated) line.
  useEffect(() => {
    if (!targetDevice) return
    if (targetDevice.instance_id) {
      setSavedLineId('')
      setSelected(String(targetDevice.instance_id))
      return
    }
    // With no authoritative device->SIM association, clear global selection. A saved line is
    // loaded only after the user explicitly chooses it in the separate manager below.
    setSavedLineId('')
    setSelected(null)
    setForm(emptyInstance())
    // An offline saved hardware record has no authoritative SIM association. Do not turn it
    // into a new-line form merely because another reader exists; saved lines remain selectable
    // below and their delete action stays available regardless of hardware presence.
    if (targetDevice.present === false || targetDevice.sim?.present === false) {
      setCreating(false)
      return
    }
    if (!readers.length) return
    const wanted = targetDevice.reader || targetDevice.name
    const index = Math.max(0, readers.findIndex((reader) => reader === wanted))
    setCreating(true)
    setCard(null); setPin(''); setPinMsg('')
    setForm({ ...emptyInstance(), id: nextInstanceId(instances), reader_index: index,
      reader_port: (cards.find((item) => item.index === index) || {}).reader_port || '' })
  }, [targetDevice?.id, targetDevice?.instance_id, readers.join('|')]) // eslint-disable-line react-hooks/exhaustive-deps
  // Keep the reader selection valid for the CURRENT hardware. A stored reader_index can be
  // stale — saved when more readers were attached — and point past the live reader list; the
  // <select> then has no matching option and "Detect card" probes a phantom reader ("No SIM
  // card in reader N"). Clamp any out-of-range index back onto a reader that actually exists.
  useEffect(() => {
    if (!readers.length) return
    setForm((f) => (f.reader_index >= readers.length || f.reader_index < 0)
      ? { ...f, reader_index: 0 } : f)
  }, [readers.length])
  // Keep the "PIN saved?" indicator in sync when it changes server-side (delete-PIN,
  // start-with-PIN) without a full line switch — mirror the fresh value onto the form.
  useEffect(() => { if (managedSelected) setForm((f) => ({ ...f, has_pin: managedSelected.has_pin })) }, [managedSelected?.has_pin])

  const upd = (patch) => setForm((f) => ({ ...f, ...patch }))
  const updSip = (patch) => setForm((f) => ({ ...f, sip: { ...f.sip, ...patch } }))
  // The reader index to act on, clamped to a reader that currently exists (never probe a
  // stale/out-of-range index that would report a phantom empty reader).
  const readerIdx = () => (form.reader_index >= 0 && form.reader_index < readers.length) ? form.reader_index : 0
  // Stable USB port path of a reader index (from the live card monitor). A line binds to this
  // port, not the enumeration index, so it sticks to the physical reader socket even when pcscd
  // re-enumerates two identical readers in a different order.
  const portForIdx = (i) => (cards.find((c) => c.index === i) || {}).reader_port || ''

  const applyCard = (c) => {
    setCard(c)
    if (!c.present) {
      setPinMsg(t('No SIM card in this reader.'))
      return
    }
    const patch = { imsi: c.imsi || form.imsi, mcc: c.mcc || form.mcc, mnc: c.mnc || form.mnc }
    if (c.smsc && smscMode === 'auto') patch.smsc = c.smsc   // SMSC from the SIM (EF_SMSP)
    if (c.imsi) patch.reader = `imsi:${c.imsi}`
    // Bind the line to the reader's stable physical USB port (from the detected card, else the
    // live monitor). Persisted so start-time re-resolves the correct index for this socket.
    const port = c.reader_port || portForIdx(readerIdx())
    if (port) patch.reader_port = port
    if (!form.id) patch.id = nextInstanceId(instances)
    upd(patch)
    if (c.imsi) setPinMsg(t('Card read.'))
    else if (c.error) setPinMsg(t('Card error: {error}', { error: c.error }))
    else setPinMsg(t('Card present; enter PIN to read IMSI. ICCID {iccid}, {tries} tries left.', { iccid: c.iccid || '?', tries: c.pin_tries ?? '?' }))
  }

  const detect = async () => {
    setPinMsg(t('Detecting…'))
    try {
      applyCard(await api.detect(readerIdx()))
    } catch (e) { setPinMsg(`${t('Error')}: ${e.message}`) }
  }

  const verifyPin = async () => {
    setPinMsg(t('Verifying…'))
    try {
      const r = await api.verifyPin(pin, readerIdx(), readers[readerIdx()], portForIdx(readerIdx()))
      if (r.ok) {
        upd({ pin })
        // The backend already re-read the card with the PIN in the same request (r.card).
        // Re-detecting here would open a fresh no-PIN connection and lock the card again.
        if (r.card) applyCard(r.card)
        setPinMsg(t('PIN OK ✓'))
      } else {
        setPinMsg(t('PIN failed: {error} ({tries} tries left)', { error: r.error, tries: r.tries }))
      }
    } catch (e) { setPinMsg(`${t('Error')}: ${e.message}`) }
  }

  const save = async () => {
    setSaving(true)
    try {
      const body = { ...form, mnc: String(form.mnc).padStart(3, '0') }
      const editedNumber = String(form.msisdn || '').trim() !== String(managedSelected?.msisdn || '').trim()
      if (editedNumber) body.msisdn_source = String(form.msisdn || '').trim() ? 'manual' : ''
      // Strip runtime-only fields that ride along on the instance object from /api/instances
      // (they are computed per-request, not config — never persist them).
      delete body.status; delete body.has_pin
      // Never send an empty PIN — the stored PIN (tied to this IMSI) must survive edits to
      // unrelated fields. `pin` state is only set when the user re-enters/verifies a PIN
      // here; only then do we forward it to update the saved credential.
      delete body.pin
      // Device identity belongs to the physical modem/reader and is managed on the
      // Hardware tab. Never let a stale SIM form overwrite the current hardware snapshot.
      delete body.imei; delete body.imeisv
      if (pin) body.pin = pin
      const res = creating ? await api.provision(body) : await api.saveInstance(body)
      await refresh()
      if (creating) {
        setCreating(false)
        setSelected(String(res.instance.id))
      }
      // Runtime configuration changes restart a running line server-side so Asterisk/IKE can
      // apply them. A display-name-only edit is metadata and saves without interrupting it.
      setPinMsg(t(creating ? 'Line created and starting…' : res?.applied ? 'Saved — restarting the line to apply changes…' : 'Saved.'))
    } catch (e) { alert(e.message) }
    setSaving(false)
  }

  const del = async () => {
    const lineLabel = form.name || `${form.mcc || ''}-${form.mnc || ''}` || form.id
    const warning = deleteHistory
      ? t('Delete this SIM line and all of its messages and call records?')
      : t('Delete this SIM line? Messages and call records will be preserved.')
    if (!confirm(`${t('You are deleting SIM line “{name}” (ID {id}).', { name: lineLabel, id: form.id })}\n\n${warning}\n\n${t('If the SIM is still inserted, automatic setup pauses until it is removed and inserted again.')}`)) return
    const typed = prompt(t('Type the line ID “{id}” to confirm deletion.', { id: form.id }), '')
    if (String(typed || '').trim() !== String(form.id)) {
      if (typed !== null) alert(t('Line ID did not match. Nothing was deleted.'))
      return
    }
    setDeleting(true)
    try {
      await api.deleteInstance(form.id, deleteHistory)
      setSelected(null)
      setSavedLineId('')
      setForm(emptyInstance())
      await refresh()
      setPinMsg(t(deleteHistory ? 'SIM line and history deleted.' : 'SIM line deleted; history preserved.'))
    } catch (error) { alert(error.message) }
    finally { setDeleting(false) }
  }

  const deleteSavedPin = async () => {
    if (!form.id) return
    if (!confirm(t('Delete the saved SIM PIN for this line?\n\nThe line will be stopped and the PIN will be requested again on next start.'))) return
    try {
      const r = await api.clearPin(form.id)
      upd({ has_pin: false })          // reflect immediately (form is local state)
      await refresh()
      setPinMsg(t(r.had_pin ? 'Saved PIN deleted — the line will ask for it on next start.'
                            : 'No saved PIN to delete.'))
    } catch (e) { alert(e.message) }
  }

  const missing = targetDevice?.provisioning?.missing || []
  const missingLabels = { imsi: 'IMSI / PIN', imei: 'IMEI', smsc: t('SMS centre (SMSC)') }
  const imeiReady = String(targetDevice?.imei || '').replace(/[^0-9]/g, '').length === 15
  const existingLine = instances.some(line => String(line.id) === String(form.id))

  return (
    <div style={{ maxWidth: 1000 }}>
      {!!instances.length && <div className="u-saved-lines">
        <div><label>{t('Saved SIM line')}</label><p>{t('Select a saved SIM line to edit or delete it. This list does not depend on whether its former device is connected.')}</p></div>
        <select value={boundLineId || savedLineId} disabled={!!boundLineId} onChange={event => {
          const value = event.target.value
          setCreating(false)
          setSavedLineId(value)
          setSelected(value || null)
        }}>
          <option value="">{t('Choose a saved line')}</option>
          {instances.map(line => <option value={line.id} key={line.id}>{line.name || `${line.mcc || ''}-${line.mnc || ''}`} · {t(line.status?.label || 'Stopped')}</option>)}
        </select>
      </div>}
      {creating && <div className="u-note" style={{ marginBottom: 14 }}>
        <b>{t('A new line was created automatically for this SIM.')}</b><br />
        {t('Country routing was detected from the SIM. Complete only the missing fields below, then start VoWiFi.')}
        {!!missing.length && <div style={{ marginTop: 6 }}>{t('Missing information')}: {missing.map(key => missingLabels[key] || key).join('、')}</div>}
      </div>}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
      {/* Card / PIN panel */}
      <div className="card" style={{ padding: 20 }}>
        <h3 style={{ marginTop: 0 }}>{t('SIM card')}</h3>
        <Field label={t('Reader')}>
          <select value={form.reader_index} disabled={!!targetDevice} onChange={(e) => upd({ reader_index: +e.target.value, reader_port: portForIdx(+e.target.value) || form.reader_port })}>
            {readers.map((r, i) => <option key={i} value={i}>{i}: {r}{portForIdx(i) ? ` — USB ${portForIdx(i)}` : ''}</option>)}
            {readers.length === 0 && <option>{readersLoading ? `${t('Loading')}…` : t('No readers')}</option>}
          </select>
        </Field>
        {form.reader_port &&
          <div className="mono" style={{ fontSize: 11, color: 'var(--text-mute)', marginTop: 4 }}>
            {t('Bound to USB port {port} (stable across reader re-enumeration)', { port: form.reader_port })}
          </div>}
        <button className="btn btn-ghost" style={{ marginTop: 10 }} onClick={detect}>{t('Detect card')}</button>
        {card && (
          <div className="mono" style={{ fontSize: 12, color: card.present ? 'var(--text-dim)' : '#ef4444', marginTop: 12, lineHeight: 1.6 }}>
            {card.present ? (<>
              ICCID: {card.iccid || '—'}<br />IMSI: {card.imsi || t('(locked)')}<br />
              {card.reader_port && <>{t('USB port')}: {card.reader_port}<br /></>}
              PIN: {card.pin_enabled ? t('enabled, {tries} tries', { tries: card.pin_tries }) : t('disabled')}
            </>) : (<>{t('No SIM card in reader {reader}.', { reader: card.reader_index })}</>)}
          </div>
        )}
        <hr style={{ borderColor: 'var(--border)', margin: '16px 0' }} />
        <Field label={t('SIM PIN (CHV1)')}>
          <input type="password" value={pin} onChange={(e) => setPin(e.target.value)} placeholder={t('e.g. 123456')} />
        </Field>
        <div style={{ display: 'flex', gap: 8, marginTop: 10, flexWrap: 'wrap' }}>
          <button className="btn btn-primary" onClick={verifyPin} disabled={!pin}>{t('Verify PIN')}</button>
          {form.id && form.has_pin &&
            <button className="btn btn-ghost" style={{ color: '#ef4444' }} onClick={deleteSavedPin}>{t('Delete saved PIN')}</button>}
        </div>
        {form.id && (
          <div style={{ fontSize: 12, color: 'var(--text-mute)', marginTop: 8 }}>
            {form.has_pin
              ? t('A PIN is saved for this line and used automatically on start.')
              : t('No PIN saved — you will be asked for it when the line starts if required.')}
          </div>
        )}
        {pinMsg && <div style={{ fontSize: 13, marginTop: 10, color: pinMsg.includes('OK') || pinMsg.includes('read') || pinMsg === 'Saved.' || pinMsg.includes('deleted') ? '#22c55e' : '#eab308' }}>{pinMsg}</div>}
      </div>

      {/* Instance form */}
      <div className="card" style={{ padding: 20 }}>
        <h3 style={{ marginTop: 0 }}>{t('Line configuration')}</h3>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
          {!creating && <Field label={t('Instance ID')}><input value={form.id} onChange={(e) => upd({ id: e.target.value })} placeholder="1" /></Field>}
          <Field label={t('Name')}><input value={form.name} onChange={(e) => upd({ name: e.target.value })} placeholder="Telus" /></Field>
          <Field label="IMSI"><input className="mono" value={form.imsi} onChange={(e) => upd({ imsi: e.target.value })} /></Field>
          <Field label="MCC"><input value={form.mcc} onChange={(e) => upd({ mcc: e.target.value })} /></Field>
          <Field label="MNC"><input value={form.mnc} onChange={(e) => upd({ mnc: e.target.value })} /></Field>
          <Field label={t('Proxy country override')}><input className="mono" value={form.proxy_country || ''} maxLength={2}
            onChange={(e) => upd({ proxy_country: e.target.value.replace(/[^a-z]/gi, '').toLowerCase() })}
            placeholder={`auto (${(form.proxy_country_effective || 'MCC').toUpperCase()})`} /></Field>
          <Field label={t('Phone number (MSISDN)')}><input className="mono" value={form.msisdn} onChange={(e) => upd({ msisdn: e.target.value })} placeholder={t('auto-learned')} /></Field>
          <Field label={t('SMS centre (SMSC)')}>
            <div style={{ display: 'flex', gap: 12, marginBottom: 6, fontSize: 13 }}>
              <label style={{ display: 'flex', alignItems: 'center', gap: 5, cursor: 'pointer' }}>
                <input type="radio" name="scmode" checked={smscMode === 'auto'} style={{ width: 'auto' }}
                  onChange={() => { setSmscMode('auto'); if (card?.smsc) upd({ smsc: card.smsc }) }} />{t('Auto (from SIM)')}
              </label>
              <label style={{ display: 'flex', alignItems: 'center', gap: 5, cursor: 'pointer' }}>
                <input type="radio" name="scmode" checked={smscMode === 'manual'} style={{ width: 'auto' }}
                  onChange={() => setSmscMode('manual')} />{t('Manual')}
              </label>
            </div>
            <input className="mono" value={form.smsc} readOnly={smscMode === 'auto'}
              onChange={(e) => upd({ smsc: e.target.value })}
              placeholder={smscMode === 'auto' ? t('detect card / verify PIN to read from SIM') : '+1...'}
              style={smscMode === 'auto' ? { opacity: .7 } : undefined} />
          </Field>
          {!creating && <Field label={t('Reader match')}><input className="mono" value={form.reader} onChange={(e) => upd({ reader: e.target.value })} placeholder="imsi:302..." /></Field>}
          <Field label="APN"><input className="mono" value={form.apn ?? 'ims'} onChange={(e) => upd({ apn: e.target.value })} placeholder="ims" /></Field>
          <Field label={t('ePDG identity (IDr)')}>
            <select value={form.idr_mode ?? 'apn'} onChange={(e) => upd({ idr_mode: e.target.value })}>
              <option value="apn">{t('Bare APN (default)')}</option>
              <option value="fqdn">APN-FQDN</option>
            </select>
          </Field>
          <Field label={t('IMS address family (CP)')}>
            <select value={form.cp_mode ?? 'auto'} onChange={(e) => upd({ cp_mode: e.target.value })}>
              <option value="auto">{t('Auto-detect (recommended)')}</option>
              <option value="dual">{t('Dual-stack (IPv4+IPv6)')}</option>
              <option value="v6">{t('IPv6 only')}</option>
              <option value="v4">{t('IPv4 only')}</option>
            </select>
            {form.cp_mode && form.cp_mode !== 'auto' && form.cp_mode_source === 'auto' && (
              <div style={{ fontSize: 11, color: 'var(--text-mute)', marginTop: 2 }}>
                {t('Auto-detected: {mode}. Switch back to Auto-detect to re-probe.', { mode: form.cp_mode.toUpperCase() })}
              </div>
            )}
          </Field>
        </div>
        <p className="u-note">{imeiReady
          ? t('The IMEI is inherited from this device’s Hardware settings.')
          : t('Set a 15-digit IMEI on the Hardware tab before starting VoWiFi.')}</p>
        <div style={{ fontSize: 11, color: 'var(--text-mute)', marginTop: 4 }}>
          {t('IDr help')}
        </div>
        <div style={{ fontSize: 11, color: 'var(--text-mute)', marginTop: 4 }}>
          {t('IMS address family help')}
        </div>

        <h4 style={{ marginBottom: 6 }}>{t('Browser softphone')}</h4>
        <label style={{ marginTop: 8 }}>
          <input type="checkbox" style={{ width: 'auto', marginRight: 8 }} checked={!!form.sip.webrtc?.enable}
            onChange={(e) => updSip({ webrtc: { ...form.sip.webrtc, enable: e.target.checked } })} />
          {t('Enable browser softphone (WebRTC)')}
        </label>

        <h4 style={{ marginBottom: 6, marginTop: 18 }}>{t('Voicemail')}</h4>
        <p style={{ fontSize: 11, color: 'var(--text-mute)', margin: '0 0 6px', lineHeight: 1.5 }}>
          {t('Record a message when a call to this line goes unanswered — including when no browser is open. A call you decline on the softphone is never recorded.')}
        </p>
        {/* Three states, not two: "follow the global default" has to stay distinguishable from
            "off for this line", or turning the global on would silently switch on every line
            that had merely been left alone. */}
        <select value={form.sip.vm_enabled === undefined || form.sip.vm_enabled === null
          ? 'default' : (form.sip.vm_enabled ? 'on' : 'off')}
          onChange={(e) => updSip({ vm_enabled: e.target.value === 'default' ? undefined
            : e.target.value === 'on' })}>
          <option value="default">{t('Use the system default ({state})',
            { state: systemDefaultVm === null ? `${t('Loading')}…` : systemDefaultVm ? t('on') : t('off') })}</option>
          <option value="on">{t('On for this line')}</option>
          <option value="off">{t('Off for this line')}</option>
        </select>

        <details style={{ marginTop: 12 }}>
          <summary>{t('Advanced IMS identity')}</summary>
          <p className="u-note">{t('Carrier defaults are applied automatically. Change these fields only when required by the carrier.')}</p>
          <div style={{ display: 'grid', gridTemplateColumns: '2fr 1fr', gap: 10 }}>
            <Field label="P-Access-Network-Info (PANI)">
              <input className="mono" value={form.sip.pani || ''} onChange={(e) => updSip({ pani: e.target.value })}
                placeholder={t('Automatic carrier default')} />
            </Field>
            <Field label={t('IMS access type')}>
              <input className="mono" value={form.sip.access_type || ''} onChange={(e) => updSip({ access_type: e.target.value })}
                placeholder={t('Automatic carrier default')} />
            </Field>
          </div>
          <Field label={t('Device User-Agent (how the line identifies to the carrier)')}>
            <input className="mono" maxLength={64} value={form.sip.user_agent || ''}
              onChange={(e) => updSip({ user_agent: e.target.value })}
              placeholder="MDD-Sim-Gateway" />
            <div style={{ fontSize: 11, color: 'var(--text-mute)', marginTop: 2 }}>
              {t('Leave empty to identify as MDD-Sim-Gateway. Set this only when the carrier rejects registration from an unrecognised terminal.')}
            </div>
          </Field>
          <label style={{ marginTop: 8 }}>
            <input type="checkbox" style={{ width: 'auto', marginRight: 8 }} checked={!!form.sip.user_eq_phone}
              onChange={(e) => updSip({ user_eq_phone: e.target.checked })} />
            {t('Add ;user=phone to telephone-number SIP requests')}
          </label>
        </details>

        <div style={{ display: 'flex', gap: 8, marginTop: 18 }}>
          <button className="btn btn-primary" onClick={save} disabled={saving || !form.id || !form.imsi || (creating && !imeiReady)}>{t(creating ? 'Complete and start VoWiFi' : 'Save')}</button>
        </div>
        {existingLine && <div className="u-line-delete">
          <div><h4>{t('Delete SIM line')}</h4><p>{t('Deletes IMS settings, saved PIN, routing and runtime files for this SIM. The physical device record is not affected.')}</p>
            <label><input type="checkbox" checked={deleteHistory} onChange={event => setDeleteHistory(event.target.checked)} />{t('Also delete all messages and call records')}</label>
          </div>
          <button className="btn btn-danger" disabled={deleting} onClick={del}>{t(deleting ? 'Deleting…' : 'Delete SIM line')}</button>
        </div>}
      </div>
      </div>
    </div>
  )
}
