import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api.js'
import { useI18n } from '../i18n.jsx'

const DOWNLOAD_STEPS = [
  ['es10a_get_euicc_configured_addresses', 'Read default SM-DP+'],
  ['es10b_get_euicc_challenge_and_info', 'Get eUICC challenge'],
  ['es9p_initiate_authentication', 'Authenticate with SM-DP+'],
  ['es10b_authenticate_server', 'Authenticate server'],
  ['es9p_authenticate_client', 'Authenticate client'],
  ['es8p_metadata_parse', 'Profile preview'],
  ['es10b_prepare_download', 'Prepare download'],
  ['es9p_get_bound_profile_package', 'Fetch profile package'],
  ['es10b_load_bound_profile_package', 'Install on eUICC'],
]

/** Map lpac progress/error names onto the pipeline. Ignore cancel_session etc. */
function resolveDownloadStep(step) {
  const s = String(step || '')
  if (!s || s === 'started' || s === 'completed' || s === 'cancelling') return null
  // lpac may emit "es10b_load_bound_profile_package:result"
  const base = s.split(':')[0]
  const idx = DOWNLOAD_STEPS.findIndex(([k]) => k === s || k === base)
  return idx >= 0 ? DOWNLOAD_STEPS[idx][0] : null
}

function isNonEuiccError(msg) {
  const s = String(msg || '')
  return /euicc_init|does not appear to be an eUICC|not an eUICC|ordinary USIM/i.test(s)
}

/** An empty reader fails euicc_init exactly like an ordinary USIM does, so without this the
 * UI accuses a perfectly good eSIM of not being one — the modem's virtual reader is empty
 * whenever its SIM bridge is down. Check this first: lpac reports both symptoms at once. */
function isNoCardError(msg) {
  const s = String(msg || '')
  return /SCARD_E_NO_SMARTCARD|SCARD_W_REMOVED_CARD|no smart ?card|card (is )?(absent|removed|not present)|SCardConnect/i.test(s)
}

function copyText(text, showToast, t) {
  if (!text) return
  navigator.clipboard?.writeText(text).then(
    () => showToast?.(t('Copied')),
    () => showToast?.(t('Copy failed')),
  )
}

function parseActivationCode(raw) {
  let s = (raw || '').trim()
  if (!s) return null
  if (/^LPA:/i.test(s)) s = s.slice(4)
  const parts = s.split('$')
  if (parts.length < 3 || parts[0] !== '1') return null
  return {
    smdp: parts[1] || '',
    matching_id: parts[2] || '',
    confirmation_required: parts[4] === '1',
  }
}

/** Decode a QR code from an image file entirely in the browser — the image never leaves the
 * gateway UI. Photos need native resolution for small modules while huge screenshots decode
 * faster downscaled, so a few sizes are attempted. */
async function decodeQrImage(file) {
  const jsQR = (await import('jsqr')).default
  const bitmap = await createImageBitmap(file)
  try {
    for (const max of [1500, 800, 3000]) {
      const scale = Math.min(1, max / Math.max(bitmap.width, bitmap.height))
      const w = Math.max(1, Math.round(bitmap.width * scale))
      const h = Math.max(1, Math.round(bitmap.height * scale))
      const canvas = document.createElement('canvas')
      canvas.width = w
      canvas.height = h
      const ctx = canvas.getContext('2d', { willReadFrequently: true })
      ctx.drawImage(bitmap, 0, 0, w, h)
      const pixels = ctx.getImageData(0, 0, w, h)
      const hit = jsQR(pixels.data, w, h, { inversionAttempts: 'attemptBoth' })
      if (hit?.data?.trim()) return hit.data.trim()
      if (scale === 1) break // retrying identical dimensions cannot find more
    }
  } finally {
    bitmap.close?.()
  }
  return null
}

function StatePill({ state }) {
  const { t } = useI18n()
  const enabled = String(state || '').toLowerCase() === 'enabled'
  return (
    <span style={{
      fontSize: 11, fontWeight: 700, padding: '2px 8px', borderRadius: 999,
      background: enabled ? '#dcfce7' : 'var(--hover)',
      color: enabled ? '#166534' : 'var(--text-dim)',
    }}>
      {t(enabled ? 'Enabled' : 'Disabled')}
    </span>
  )
}

/** Match sigmo: nickname takes priority, else profileName. */
function profileDisplayName(p, fallback = 'Profile') {
  const nick = (p.profileNickname || '').trim()
  if (nick) return nick
  return (p.profileName || p.serviceProviderName || fallback).trim() || fallback
}

function RenameModal({ profile, busy, onClose, onSave }) {
  const { t } = useI18n()
  const [nick, setNick] = useState(() => profileDisplayName(profile))
  useEffect(() => {
    setNick(profileDisplayName(profile))
  }, [profile])
  if (!profile) return null
  return (
    <div style={{ position: 'fixed', inset: 0, background: '#0008', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 50 }}
      onClick={onClose}>
      <div className="card" style={{ width: 360, maxWidth: '92vw', padding: 20 }} onClick={(e) => e.stopPropagation()}>
        <div style={{ fontWeight: 700, fontSize: 16, marginBottom: 6 }}>{t('Rename')}</div>
        <div style={{ fontSize: 12, color: 'var(--text-mute)', marginBottom: 14, wordBreak: 'break-all' }}>
          {profile.iccid}
        </div>
        <label style={{ display: 'block', marginBottom: 14 }}>
          <div style={{ fontSize: 12, color: 'var(--text-mute)', marginBottom: 4 }}>{t('Nickname')}</div>
          <input
            autoFocus
            value={nick}
            onChange={(e) => setNick(e.target.value)}
            placeholder={t('Nickname')}
            style={{ width: '100%' }}
            onKeyDown={(e) => {
              if (e.key === 'Enter') onSave(nick.trim())
              if (e.key === 'Escape') onClose()
            }}
          />
        </label>
        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
          <button className="btn btn-ghost" onClick={onClose} disabled={busy}>{t('Cancel')}</button>
          <button className="btn btn-primary" disabled={busy} onClick={() => onSave(nick.trim())}>
            {t(busy ? 'Saving…' : 'Update')}
          </button>
        </div>
      </div>
    </div>
  )
}

function ProgressBar({ step, done, error }) {
  const { t } = useI18n()
  const resolved = resolveDownloadStep(step)
  const idx = resolved ? DOWNLOAD_STEPS.findIndex(([k]) => k === resolved) : -1
  // Unknown steps (cancel_session, started) must NOT fall back to index 0 — that looked like a jump to step 1.
  const cur = done ? DOWNLOAD_STEPS.length : (idx >= 0 ? idx : (step === 'started' ? 0 : -1))
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      {DOWNLOAD_STEPS.map(([k, label], i) => {
        const finished = done || (cur >= 0 && i < cur)
        const active = !done && !error && cur >= 0 && i === cur
        const failed = !!error && cur >= 0 && i === cur
        const color = failed ? '#ef4444' : finished ? '#22c55e' : active ? '#eab308' : 'var(--border-strong)'
        return (
          <div key={k} style={{ display: 'flex', alignItems: 'center', gap: 10, fontSize: 13 }}>
            <span style={{
              width: 18, height: 18, borderRadius: 999, background: color,
              color: '#fff', display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
              fontSize: 11, fontWeight: 700, flexShrink: 0,
            }}>
              {finished ? '✓' : failed ? '!' : active ? '…' : i + 1}
            </span>
            <span style={{ color: finished || active || failed ? 'var(--text)' : 'var(--text-mute)', fontWeight: active || failed ? 700 : 500 }}>
              {t(label)}
            </span>
          </div>
        )
      })}
    </div>
  )
}

function formatBytes(bytes) {
  if (bytes == null || Number.isNaN(Number(bytes))) return '—'
  const n = Number(bytes)
  if (n === 0) return '0 B'
  const k = 1024
  const sizes = ['B', 'KiB', 'MiB', 'GiB']
  const i = Math.min(sizes.length - 1, Math.floor(Math.log(n) / Math.log(k)))
  return `${Math.round((n / k ** i) * 100) / 100} ${sizes[i]}`
}

function seTarget(reader, se) {
  return {
    reader,
    se_id: se?.id || se?.seId,
    aid: se?.aid || undefined,
  }
}

function DownloadModal({ reader, ses, imeiDefault, onClose, onStarted, showToast }) {
  const { t } = useI18n()
  const dual = (ses || []).length > 1
  const [mode, setMode] = useState('code') // code | manual
  const [activation, setActivation] = useState('')
  const [smdp, setSmdp] = useState('')
  const [matchingId, setMatchingId] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [imei, setImei] = useState(imeiDefault || '')
  const [seId, setSeId] = useState(dual ? '' : (ses?.[0]?.id || 'default'))
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [qrBusy, setQrBusy] = useState(false)
  const fileInput = useRef(null)

  useEffect(() => {
    const parsed = parseActivationCode(activation)
    if (parsed) {
      setSmdp(parsed.smdp)
      setMatchingId(parsed.matching_id)
    }
  }, [activation])

  const readQr = async (file) => {
    if (!file || qrBusy) return
    setErr('')
    setQrBusy(true)
    try {
      const text = await decodeQrImage(file)
      if (!text) {
        setErr(t('No QR code found in the image.'))
        return
      }
      if (!parseActivationCode(text)) {
        setErr(t('The QR code is not an eSIM activation code: {text}', { text: text.slice(0, 80) }))
        return
      }
      setMode('code')
      setActivation(text)
      showToast?.(t('Activation code recognized'))
    } catch {
      setErr(t('Could not read that image.'))
    } finally {
      setQrBusy(false)
    }
  }
  const readQrRef = useRef(readQr)
  readQrRef.current = readQr

  useEffect(() => { // a pasted screenshot anywhere in the dialog counts as a QR upload
    const onPaste = (e) => {
      const item = Array.from(e.clipboardData?.items || []).find((i) => i.type.startsWith('image/'))
      if (item) {
        e.preventDefault()
        readQrRef.current(item.getAsFile())
      }
    }
    window.addEventListener('paste', onPaste)
    return () => window.removeEventListener('paste', onPaste)
  }, [])

  const submit = async () => {
    setErr('')
    if (dual && !seId) return setErr(t('Select which eUICC (SE) to install onto.'))
    const body = {
      reader,
      se_id: seId || ses?.[0]?.id || 'default',
      imei: imei.trim() || undefined,
      confirmation_code: confirmation.trim() || undefined,
    }
    if (mode === 'code') {
      if (!activation.trim()) return setErr(t('Paste an activation code (LPA:1$…).'))
      body.activation_code = activation.trim()
    } else {
      if (!smdp.trim()) return setErr(t('SM-DP+ address is required.'))
      body.smdp = smdp.trim()
      body.matching_id = matchingId.trim() || undefined
    }
    setBusy(true)
    try {
      await api.esimDownload(body)
      onStarted?.()
      onClose()
    } catch (e) {
      setErr(e.message)
    }
    setBusy(false)
  }

  return (
    <div style={{ position: 'fixed', inset: 0, background: '#0008', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 50 }}
      onClick={onClose}>
      <div className="card" style={{ width: 480, maxWidth: '92vw', padding: 20 }} onClick={(e) => e.stopPropagation()}
        onDragOver={(e) => e.preventDefault()}
        onDrop={(e) => { e.preventDefault(); const f = e.dataTransfer?.files?.[0]; if (f?.type?.startsWith('image/')) readQr(f) }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
          <h2 style={{ margin: 0, fontSize: 18 }}>{t('Download eSIM')}</h2>
          <button className="btn btn-ghost" onClick={onClose}>✕</button>
        </div>
        {dual && (
          <div style={{ marginBottom: 14 }}>
            <div style={{ fontSize: 12, color: 'var(--text-mute)', marginBottom: 6 }}>eUICC</div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              {(ses || []).map((se, i) => {
                const selected = seId === se.id
                return (
                  <button
                    key={se.id}
                    type="button"
                    onClick={() => setSeId(se.id)}
                    style={{
                      display: 'block', width: '100%', textAlign: 'left', cursor: 'pointer',
                      border: `1px solid ${selected ? 'var(--primary)' : 'var(--border)'}`,
                      borderRadius: 10, padding: '8px 10px',
                      background: selected ? 'color-mix(in srgb, var(--primary) 8%, var(--panel))' : 'transparent',
                      color: 'inherit', font: 'inherit',
                    }}
                  >
                    <span style={{ display: 'block', fontFamily: 'ui-monospace, monospace', fontSize: 12, wordBreak: 'break-all' }}>
                      {se.eid || `SE${i + 1} (${t('no EID')})`}
                    </span>
                    <span style={{ display: 'block', fontSize: 12, color: 'var(--text-mute)', marginTop: 2 }}>
                      {se.label || `SE${i + 1}`} · {t('Storage remaining')} {formatBytes(se.freeSpace)}
                    </span>
                  </button>
                )
              })}
            </div>
          </div>
        )}
        <div style={{ display: 'flex', gap: 8, marginBottom: 12 }}>
          {[['code', 'Activation code'], ['manual', 'Manual']].map(([k, label]) => (
            <button key={k} className="btn" onClick={() => setMode(k)}
              style={{ background: mode === k ? 'var(--primary)' : 'var(--hover)', color: mode === k ? '#fff' : 'var(--text-soft)' }}>
              {t(label)}
            </button>
          ))}
        </div>
        {mode === 'code' ? (
          <>
            <label style={{ display: 'block', marginBottom: 6 }}>
              <div style={{ fontSize: 12, color: 'var(--text-mute)', marginBottom: 4 }}>{t('LPA activation code')}</div>
              <textarea value={activation} onChange={(e) => setActivation(e.target.value)} rows={3}
                placeholder="LPA:1$smdp.example.com$MATCHING-ID"
                style={{ width: '100%', resize: 'vertical' }} />
            </label>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12, flexWrap: 'wrap' }}>
              <input ref={fileInput} type="file" accept="image/*" style={{ display: 'none' }}
                onChange={(e) => { readQr(e.target.files?.[0]); e.target.value = '' }} />
              <button type="button" className="btn btn-ghost" disabled={qrBusy} onClick={() => fileInput.current?.click()}>
                {t(qrBusy ? 'Reading QR…' : 'Upload QR image')}
              </button>
              <span style={{ fontSize: 12, color: 'var(--text-mute)' }}>{t('or paste / drop a QR screenshot into this dialog')}</span>
            </div>
          </>
        ) : (
          <>
            <label style={{ display: 'block', marginBottom: 10 }}>
              <div style={{ fontSize: 12, color: 'var(--text-mute)', marginBottom: 4 }}>SM-DP+</div>
              <input value={smdp} onChange={(e) => setSmdp(e.target.value)} placeholder="smdp.example.com" style={{ width: '100%' }} />
            </label>
            <label style={{ display: 'block', marginBottom: 10 }}>
              <div style={{ fontSize: 12, color: 'var(--text-mute)', marginBottom: 4 }}>{t('Matching ID')}</div>
              <input value={matchingId} onChange={(e) => setMatchingId(e.target.value)} style={{ width: '100%' }} />
            </label>
          </>
        )}
        <label style={{ display: 'block', marginBottom: 10 }}>
          <div style={{ fontSize: 12, color: 'var(--text-mute)', marginBottom: 4 }}>{t('Confirmation code (optional)')}</div>
          <input value={confirmation} onChange={(e) => setConfirmation(e.target.value)} style={{ width: '100%' }} />
        </label>
        <label style={{ display: 'block', marginBottom: 10 }}>
          <div style={{ fontSize: 12, color: 'var(--text-mute)', marginBottom: 4 }}>
            IMEI {t(imeiDefault ? '(from matched line — editable)' : '(optional)')}
          </div>
          <input value={imei} onChange={(e) => setImei(e.target.value)} placeholder={t('15-digit IMEI')} style={{ width: '100%' }} />
        </label>
        {err && <div style={{ color: '#ef4444', fontSize: 13, marginBottom: 10 }}>{err}</div>}
        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
          <button className="btn btn-ghost" onClick={onClose}>{t('Cancel')}</button>
          <button className="btn btn-primary" disabled={busy || (dual && !seId)} onClick={submit}>
            {t(busy ? 'Starting…' : 'Download')}
          </button>
        </div>
      </div>
    </div>
  )
}

function instanceForCard(card, instances) {
  if (!card) return null
  if (card.matched != null) {
    const byId = instances.find((i) => String(i.id) === String(card.matched))
    if (byId) return byId
  }
  if (card.iccid) {
    return instances.find((i) => i.iccid && i.iccid === card.iccid) || null
  }
  return null
}

function isLineRunning(inst) {
  const st = inst?.status?.state
  return !!(st && st !== 'STOPPED')
}

export default function Esim({ cards, instances, refresh, subscribe, showToast }) {
  const { t } = useI18n()
  const present = useMemo(
    () => [...cards].filter((c) => c.present).sort((a, b) => (a.index ?? 0) - (b.index ?? 0)),
    [cards],
  )
  const [reader, setReader] = useState('')
  const [status, setStatus] = useState(null)
  const [ses, setSes] = useState([])
  const [meta, setMeta] = useState({ imei: '' })
  const [loaded, setLoaded] = useState(false)
  const [emptyReason, setEmptyReason] = useState('not-euicc') // why no chip was found
  const [cachedAt, setCachedAt] = useState(0)
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState('')
  const [showDl, setShowDl] = useState(false)
  const [dl, setDl] = useState(null) // {step, event, metadata, error, done}
  const [renameTarget, setRenameTarget] = useState(null) // { se, profile }
  const [busyOp, setBusyOp] = useState('')

  useEffect(() => {
    if (!reader && present[0]) setReader(present[0].name)
    if (reader && !present.find((c) => c.name === reader) && present[0]) setReader(present[0].name)
  }, [present, reader])

  const selectedCard = present.find((c) => c.name === reader)
  const matchedInst = useMemo(
    () => instanceForCard(selectedCard, instances),
    [selectedCard, instances],
  )
  const lineRunning = isLineRunning(matchedInst)
  const imeiDefault = meta.imei || matchedInst?.imei || ''
  const dual = ses.length > 1
  const profiles = useMemo(
    () => ses.flatMap((se) => se.profiles || []),
    [ses],
  )
  const notifications = useMemo(
    () => ses.flatMap((se) => se.notifications || []),
    [ses],
  )
  const hasEuicc = ses.some((se) => se.eid || se.chip || (se.profiles || []).length)

  // Lightweight: lpac presence only — never touch the card on page enter / reader switch.
  useEffect(() => {
    let cancelled = false
    api.esimStatus().then((st) => {
      if (!cancelled) setStatus(st)
    }).catch(() => {
      if (!cancelled) setStatus({ available: false })
    })
    return () => { cancelled = true }
  }, [])

  useEffect(() => {
    setSes([])
    setMeta({ imei: '' })
    setLoaded(false)
    setCachedAt(0)
    setErr('')
    setDl(null)
    setRenameTarget(null)
  }, [reader])

  // Without a fresh read, show the gateway's persisted last read for this card (matched
  // server-side by the inserted card's ICCID) so switching profiles does not force a
  // stop-line read first — and the cache works from any browser.
  useEffect(() => {
    if (loaded || loading || ses.length || !reader) return
    let cancelled = false
    api.esimChipCached(reader).then((r) => {
      if (cancelled || !r?.cached) return
      setSes(r.ses || [])
      setMeta({ imei: r.imei || '' })
      setCachedAt((r.ts || 0) * 1000)
    }).catch(() => {})
    return () => { cancelled = true }
  }, [loaded, loading, ses.length, reader, selectedCard?.iccid])

  const loadAll = useCallback(async () => {
    if (!reader) return
    setLoading(true)
    setErr('')
    setSes([])
    setMeta({ imei: '' })
    try {
      const st = await api.esimStatus()
      setStatus(st)
      if (!st.available) {
        setErr(t('lpac is not installed. Run "sudo ./install.sh build-lpac" on the host.'))
        setLoaded(false)
        return
      }
      // One call loads every SE (chip + profiles + notifications).
      const c = await api.esimChip(reader)
      const list = c.ses || []
      setSes(list)
      setMeta({ imei: c.imei || '' })
      const seErr = list.map((s) => s.error).filter(Boolean)
      setEmptyReason(seErr.some((m) => isNoCardError(m)) ? 'no-card' : 'not-euicc')
      if (!list.length) {
        setErr('')
      } else if (list.every((s) => s.error) && seErr.every((m) => isNonEuiccError(m))) {
        setErr('')
      } else if (list.every((s) => s.error)) {
        setErr(seErr[0] || t('eUICC load failed'))
      } else {
        setErr('')
      }
      setLoaded(true)
      setCachedAt(0)
    } catch (e) {
      // Non-eUICC cards surface as a calm empty state, not a red error banner.
      setEmptyReason(isNoCardError(e.message) ? 'no-card' : 'not-euicc')
      setErr(isNonEuiccError(e.message) || isNoCardError(e.message) ? '' : e.message)
      setSes([])
      setMeta({ imei: '' })
      setLoaded(true)
    } finally {
      setLoading(false)
    }
  }, [reader, t])

  /** One-click profile switch: stops the running line first when needed; the line matching
   * the newly enabled profile is started again by auto-provisioning. Works from the cached
   * view, so no fresh exclusive read is required. */
  const switchProfile = async (p, se) => {
    const title = profileDisplayName(p, t('Profile'))
    const target = seTarget(reader, se)
    if (lineRunning && matchedInst) {
      const ok = confirm(t('Switch to "{name}"? Running line {id} stops first; the line for the newly enabled profile starts again automatically.', { name: title, id: matchedInst.id }))
      if (!ok) return
    }
    setBusyOp('Enable')
    setErr('')
    try {
      if (lineRunning && matchedInst) {
        await api.stop(matchedInst.id)
      }
      await api.esimEnable(p.iccid, target)
      // Optimistic view: enabling implicitly disables the previously enabled profile. A
      // fresh read here would race the auto-provisioned line that re-grabs the reader.
      setSes((list) => list.map((s) => (s.id !== se.id ? s : {
        ...s,
        profiles: (s.profiles || []).map((x) => ({
          ...x,
          profileState: x.iccid === p.iccid ? 'enabled'
            : String(x.profileState || '').toLowerCase() === 'enabled' ? 'disabled' : x.profileState,
        })),
      })))
      showToast?.(t('Switched to {name} — its line starts automatically', { name: title }))
      await refresh?.()
    } catch (e) {
      showToast?.(e.message)
      setErr(e.message)
    }
    setBusyOp('')
  }

  const requestLoad = useCallback(async () => {
    if (!reader || loading || busyOp) return
    if (lineRunning && matchedInst) {
      const label = matchedInst.name
        ? `${t('line')} ${matchedInst.id} (${matchedInst.name})`
        : `${t('line')} ${matchedInst.id}`
      const ok = confirm(
        t('VoWiFi {line} is running on this reader.\n\nLoading eSIM info needs exclusive PC/SC access and will stop the line.\n\nStop VoWiFi and Load?', { line: label }),
      )
      if (!ok) return
      setBusyOp('stop')
      try {
        await api.stop(matchedInst.id)
        showToast?.(t('Line {id} stopped', { id: matchedInst.id }))
        await refresh?.()
      } catch (e) {
        showToast?.(e.message)
        setBusyOp('')
        return
      }
      setBusyOp('')
    }
    await loadAll()
  }, [reader, loading, busyOp, lineRunning, matchedInst, loadAll, refresh, showToast, t])

  const requestDownload = useCallback(async () => {
    if (!reader || busyOp) return
    if (lineRunning && matchedInst) {
      const ok = confirm(t('Downloading an eSIM needs exclusive access and will stop VoWiFi line {id}. Continue?', { id: matchedInst.id }))
      if (!ok) return
      setBusyOp('stop')
      try {
        await api.stop(matchedInst.id)
        await refresh?.()
      } catch (e) {
        showToast?.(e.message)
        setBusyOp('')
        return
      }
      setBusyOp('')
    }
    setShowDl(true)
  }, [reader, busyOp, lineRunning, matchedInst, refresh, showToast, t])

  useEffect(() => {
    if (!subscribe) return undefined
    return subscribe((msg) => {
      if (msg.type !== 'esim_download') return
      if (reader && msg.reader && msg.reader !== reader) return
      if (msg.event === 'started') {
        setDl({ step: 'started', event: 'started', done: false })
      } else if (msg.event === 'progress' || msg.event === 'preview') {
        // lpac emits cancel_session progress on failure — ignore non-pipeline steps so the bar
        // does not jump back to step 1 before the error event arrives.
        const known = resolveDownloadStep(msg.step)
        setDl((d) => ({
          ...(d || {}),
          step: known || d?.step || 'started',
          event: msg.event,
          metadata: msg.metadata || msg.data || d?.metadata,
          done: false,
        }))
      } else if (msg.event === 'completed') {
        setDl((d) => ({ ...(d || {}), step: 'completed', event: 'completed', done: true, result: msg.result }))
        showToast?.(t('Profile downloaded'))
        loadAll()
        refresh?.()
      } else if (msg.event === 'error') {
        // Prefer lpac failing function name when it maps to a pipeline step; else keep last progress.
        const known = resolveDownloadStep(msg.step)
        setDl((d) => ({
          ...(d || {}),
          step: known || resolveDownloadStep(d?.step) || d?.step || 'started',
          event: 'error',
          error: msg.error,
          done: false,
        }))
        showToast?.(msg.error || t('Download failed'))
      } else if (msg.event === 'cancelling') {
        setDl((d) => ({ ...(d || {}), event: 'cancelling' }))
      }
    })
  }, [subscribe, reader, loadAll, refresh, showToast, t])

  const stopLine = async () => {
    if (!matchedInst) return
    setBusyOp('stop')
    try {
      await api.stop(matchedInst.id)
      showToast?.(t('Line {id} stopped', { id: matchedInst.id }))
      await refresh?.()
    } catch (e) {
      showToast?.(e.message)
    }
    setBusyOp('')
  }

  const runProfileOp = async (label, fn) => {
    setBusyOp(label)
    try {
      await fn()
      showToast?.(t('{action} OK', { action: t(label) }))
      await loadAll()
      await refresh?.()
    } catch (e) {
      showToast?.(e.message)
      setErr(e.message)
    }
    setBusyOp('')
  }

  if (!present.length) {
    return (
      <div className="card" style={{ padding: 24, color: 'var(--text-dim)' }}>
        {t('No SIM present. Insert an eUICC into a PC/SC reader to manage profiles.')}
      </div>
    )
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div style={{ display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
        <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontWeight: 600 }}>
          {t('Reader')}
          <select value={reader} onChange={(e) => setReader(e.target.value)} style={{ minWidth: 220 }}>
            {present.map((c) => (
              <option key={c.name} value={c.name}>
                #{c.index} · {c.name}{c.iccid ? ` · ${c.iccid}` : ''}
              </option>
            ))}
          </select>
        </label>
        <button className="btn btn-ghost" onClick={requestLoad} disabled={loading || !!busyOp}>
          {t(loading ? 'Loading…' : 'Load')}
        </button>
        <button className="btn btn-primary" onClick={requestDownload}
          disabled={!status?.available || !hasEuicc || !!busyOp || !!dl && !dl.done && !dl.error}
          title={!hasEuicc ? t('Read this eSIM once before downloading a new one.') : ''}>
          {t('Download eSIM')}
        </button>
        {status?.available && !hasEuicc && <span style={{ fontSize: 12, color: 'var(--text-mute)' }}>{t('Read this eSIM once before downloading a new one.')}</span>}
      </div>

      {lineRunning && (
        <div className="card" style={{ padding: 14, borderColor: '#f59e0b', background: 'color-mix(in srgb, #f59e0b 12%, var(--panel))' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
            <div>
              <div style={{ fontWeight: 700 }}>{t('VoWiFi running on this reader')}</div>
              <div style={{ fontSize: 13, color: 'var(--text-dim)' }}>
                {t('Click Load to stop the line and read eSIM info — lpac needs exclusive PC/SC access.')}
                {matchedInst ? ` (${t('line')} ${matchedInst.id}${matchedInst.name ? ` · ${matchedInst.name}` : ''})` : ''}
              </div>
            </div>
            <button className="btn btn-primary" onClick={stopLine} disabled={busyOp === 'stop' || !matchedInst}>
              {t(busyOp === 'stop' ? 'Stopping…' : 'Stop line')}
            </button>
          </div>
        </div>
      )}

      {err && (
        <div className="card" style={{ padding: 14, color: '#b91c1c', borderColor: '#fecaca' }}>
          {err}
        </div>
      )}

      {dl && (
        <div className="card" style={{ padding: 16 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 12 }}>
            <div style={{ fontWeight: 700 }}>
              {t(dl.done ? 'Download complete' : dl.error ? 'Download failed' : dl.event === 'cancelling' ? 'Cancelling…' : 'Downloading…')}
            </div>
            <div style={{ display: 'flex', gap: 8 }}>
              {!dl.done && !dl.error && (
                <button className="btn btn-ghost" onClick={() => api.esimDownloadCancel({ reader }).catch((e) => showToast?.(e.message))}>
                  {t('Cancel')}
                </button>
              )}
              {(dl.done || dl.error) && (
                <button className="btn btn-ghost" onClick={() => setDl(null)}>{t('Dismiss')}</button>
              )}
            </div>
          </div>
          <ProgressBar step={dl.step} done={dl.done} error={dl.error} />
          {dl.metadata && (
            <div style={{ marginTop: 12, fontSize: 13, color: 'var(--text-soft)' }}>
              {t('Preview')}: {dl.metadata.profileName || dl.metadata.serviceProviderName || t('profile')}
              {dl.metadata.iccid ? ` · ${dl.metadata.iccid}` : ''}
            </div>
          )}
          {dl.error && <div style={{ marginTop: 10, color: '#ef4444', fontSize: 13 }}>{dl.error}</div>}
        </div>
      )}

      <div className="card" style={{ padding: 16 }}>
        <div style={{ fontWeight: 700, marginBottom: 10 }}>{t('Chip')}</div>
        {!hasEuicc ? (
          <div style={{ color: 'var(--text-mute)', fontSize: 13, lineHeight: 1.5 }}>
            {status && !status.available
              ? t('lpac is not installed. Run "sudo ./install.sh build-lpac" on the host.')
              : loading
                ? t('Reading…')
                : !loaded
                  ? t('Click Load to read chip info from this card (uses lpac / PC/SC).')
                  : emptyReason === 'no-card'
                    ? t('This reader holds no card, so nothing could be read. For a cellular module the card arrives over its SIM bridge — check that the module is still connected.')
                    : t('This card is not an eUICC / eSIM. Ordinary USIM cards cannot be managed here.')}
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12, fontSize: 13 }}>
            {dual ? (
              <>
                {ses.map((se, i) => (
                  <div key={se.id} style={{ display: 'flex', justifyContent: 'space-between', gap: 12, alignItems: 'flex-start' }}>
                    <div style={{ color: 'var(--text-mute)', fontSize: 11, flexShrink: 0, paddingTop: 2 }}>EID{i + 1}</div>
                    <div style={{ fontFamily: 'ui-monospace, monospace', wordBreak: 'break-all', textAlign: 'right' }}>
                      {se.eid || '—'}
                      {se.eid && (
                        <button className="btn btn-ghost" style={{ marginLeft: 6, padding: '2px 8px', fontSize: 11 }}
                          onClick={() => copyText(se.eid, showToast, t)}>{t('Copy')}</button>
                      )}
                    </div>
                  </div>
                ))}
                <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
                  <div style={{ color: 'var(--text-mute)', fontSize: 11 }}>{t('Storage remaining')}</div>
                  <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', fontWeight: 600 }}>
                    {ses.map((se, i) => (
                      <span key={se.id}>SE{i + 1}: {formatBytes(se.freeSpace)}</span>
                    ))}
                  </div>
                </div>
              </>
            ) : (
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 12 }}>
                <div>
                  <div style={{ color: 'var(--text-mute)', fontSize: 11 }}>EID</div>
                  <div style={{ fontFamily: 'ui-monospace, monospace', wordBreak: 'break-all' }}>
                    {ses[0]?.eid || '—'}
                    {ses[0]?.eid && (
                      <button className="btn btn-ghost" style={{ marginLeft: 6, padding: '2px 8px', fontSize: 11 }}
                        onClick={() => copyText(ses[0].eid, showToast, t)}>{t('Copy')}</button>
                    )}
                  </div>
                </div>
                <div>
                  <div style={{ color: 'var(--text-mute)', fontSize: 11 }}>{t('Default SM-DP+')}</div>
                  <div>{ses[0]?.defaultDpAddress || ses[0]?.chip?.defaultDpAddress || '—'}</div>
                </div>
                <div>
                  <div style={{ color: 'var(--text-mute)', fontSize: 11 }}>{t('Free NVM')}</div>
                  <div>{formatBytes(ses[0]?.freeSpace ?? ses[0]?.chip?.freeNonVolatileMemory)}</div>
                </div>
              </div>
            )}
            <div>
              <div style={{ color: 'var(--text-mute)', fontSize: 11 }}>{t('IMEI for download')}</div>
              <div>{imeiDefault || t('— (lpac default TAC)')}</div>
            </div>
          </div>
        )}
      </div>

      <div className="card" style={{ padding: 16 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
          <div style={{ fontWeight: 700 }}>{t('Profiles')}</div>
          <div style={{ fontSize: 12, color: 'var(--text-mute)' }}>{t('{count} profile(s)', { count: profiles.length })}</div>
        </div>
        {cachedAt > 0 && !loaded && (
          <div style={{ fontSize: 12, color: 'var(--text-mute)', marginBottom: 10 }}>
            {t('Cached view from {time} — switching works from here; click Load for a live read.', { time: new Date(cachedAt).toLocaleString() })}
          </div>
        )}
        {!profiles.length ? (
          <div style={{ color: 'var(--text-mute)', fontSize: 13 }}>
            {loading
              ? t('Reading…')
              : !loaded
                ? t('Click Load to list profiles.')
                : hasEuicc
                  ? t('No profiles on this eUICC.')
                  : t('No profiles to show.')}
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
            {(dual ? ses : [{ ...ses[0], profiles }]).map((se) => {
              const seProfiles = se.profiles || []
              if (dual && !seProfiles.length && !se.eid) return null
              return (
                <div key={se.id || 'default'}>
                  {dual && (
                    <div style={{
                      display: 'flex', justifyContent: 'space-between', alignItems: 'baseline',
                      marginBottom: 10, fontSize: 13, fontWeight: 700,
                    }}>
                      <span>{se.label || se.id}: <span style={{ fontFamily: 'ui-monospace, monospace', fontWeight: 500, fontSize: 12 }}>{se.eid || '—'}</span></span>
                      <span style={{ fontSize: 12, color: 'var(--text-mute)', fontWeight: 500 }}>{seProfiles.length}</span>
                    </div>
                  )}
                  {!seProfiles.length ? (
                    <div style={{ color: 'var(--text-mute)', fontSize: 13, marginBottom: dual ? 8 : 0 }}>{t('No profiles on this SE.')}</div>
                  ) : (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                      {seProfiles.map((p) => {
                        const enabled = String(p.profileState || '').toLowerCase() === 'enabled'
                        const target = seTarget(reader, se)
                        const title = profileDisplayName(p, t('Profile'))
                        return (
                          <div key={`${se.id}:${p.iccid}`} style={{
                            border: `1px solid ${enabled ? 'color-mix(in srgb, var(--primary) 35%, var(--border))' : 'var(--border)'}`,
                            borderRadius: 12, padding: '12px 14px',
                            background: enabled ? 'color-mix(in srgb, var(--primary) 6%, var(--panel))' : 'transparent',
                            display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12,
                          }}>
                            <div style={{ minWidth: 0, flex: 1 }}>
                              <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 }}>
                                <span style={{
                                  fontWeight: 700, fontSize: 14, overflow: 'hidden',
                                  textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                                }}>
                                  {title}
                                </span>
                                <StatePill state={p.profileState} />
                              </div>
                              <div style={{
                                marginTop: 4, fontSize: 12, color: 'var(--text-mute)',
                                fontFamily: 'ui-monospace, monospace', overflow: 'hidden',
                                textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                              }}>
                                {p.iccid}
                              </div>
                            </div>
                            <div style={{ display: 'flex', gap: 6, flexShrink: 0, flexWrap: 'wrap', justifyContent: 'flex-end' }}>
                              {!enabled && (
                                <button className="btn btn-primary" disabled={!!busyOp}
                                  onClick={() => switchProfile(p, se)}>
                                  {t('Enable')}
                                </button>
                              )}
                              {enabled && (
                                <button className="btn btn-ghost" disabled={!!busyOp || lineRunning}
                                  onClick={() => runProfileOp('Disable', () => api.esimDisable(p.iccid, target))}>
                                  {t('Disable')}
                                </button>
                              )}
                              <button className="btn btn-ghost" disabled={!!busyOp || lineRunning}
                                onClick={() => setRenameTarget({ se, profile: p })}>
                                {t('Rename')}
                              </button>
                              <button className="btn btn-ghost" disabled={!!busyOp || lineRunning}
                                onClick={() => {
                                  if (!confirm(t('Delete profile {iccid}?', { iccid: p.iccid }))) return
                                  runProfileOp('Delete', () => api.esimDelete(p.iccid, target))
                                }}>
                                {t('Delete')}
                              </button>
                            </div>
                          </div>
                        )
                      })}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        )}
      </div>

      <div className="card" style={{ padding: 16 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
          <div style={{ fontWeight: 700 }}>{t('Notifications')}</div>
          <button className="btn btn-ghost" disabled={!!busyOp || lineRunning || !notifications.length}
            onClick={() => runProfileOp('Process notifications', async () => {
              for (const se of ses) {
                if (!(se.notifications || []).length) continue
                await api.esimNotificationsProcess(seTarget(reader, se))
              }
            })}>
            {t('Process all')}
          </button>
        </div>
        {!notifications.length ? (
          <div style={{ color: 'var(--text-mute)', fontSize: 13 }}>
            {loading
              ? t('Reading…')
              : !loaded
                ? t('Click Load to list notifications.')
                : hasEuicc
                  ? t('No pending notifications.')
                  : t('No notifications to show.')}
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            {(dual ? ses : [{ ...ses[0], notifications }]).map((se) => {
              const notes = se.notifications || []
              if (dual && !notes.length) return null
              return (
                <div key={`n-${se.id || 'default'}`}>
                  {dual && (
                    <div style={{ marginBottom: 8, fontSize: 13, fontWeight: 700 }}>
                      {se.label || se.id}: <span style={{ fontFamily: 'ui-monospace, monospace', fontWeight: 500, fontSize: 12 }}>{se.eid || '—'}</span>
                    </div>
                  )}
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                    {notes.map((n) => {
                      const target = { ...seTarget(reader, se), seq: n.seqNumber ?? n.seq }
                      return (
                        <div key={`${se.id}:${n.seqNumber ?? n.seq}`} style={{
                          display: 'flex', justifyContent: 'space-between', gap: 8, alignItems: 'center',
                          border: '1px solid var(--border)', borderRadius: 10, padding: '8px 10px', fontSize: 13,
                        }}>
                          <div>
                            <div style={{ fontWeight: 600 }}>#{n.seqNumber ?? n.seq} · {n.profileManagementOperation || n.operation || 'notify'}</div>
                            <div style={{ color: 'var(--text-dim)', fontSize: 12 }}>
                              {n.iccid || '—'} · {n.notificationAddress || n.address || ''}
                            </div>
                          </div>
                          <div style={{ display: 'flex', gap: 6 }}>
                            <button className="btn btn-ghost" disabled={!!busyOp || lineRunning}
                              onClick={() => runProfileOp('Process', () => api.esimNotificationsProcess(target))}>
                              {t('Send')}
                            </button>
                            <button className="btn btn-ghost" disabled={!!busyOp || lineRunning}
                              onClick={() => runProfileOp('Remove', () => api.esimNotificationRemove(n.seqNumber ?? n.seq, seTarget(reader, se)))}>
                              {t('Remove')}
                            </button>
                          </div>
                        </div>
                      )
                    })}
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </div>

      {showDl && (
        <DownloadModal
          reader={reader}
          ses={ses}
          imeiDefault={imeiDefault}
          showToast={showToast}
          onClose={() => setShowDl(false)}
          onStarted={() => setDl({ step: 'started', event: 'started', done: false })}
        />
      )}

      {renameTarget && (
        <RenameModal
          profile={renameTarget.profile}
          busy={busyOp === 'Nickname'}
          onClose={() => setRenameTarget(null)}
          onSave={(nick) => {
            const { se, profile } = renameTarget
            const target = seTarget(reader, se)
            runProfileOp('Nickname', () => api.esimNickname(profile.iccid, nick, target)
              .then(() => setRenameTarget(null)))
          }}
        />
      )}
    </div>
  )
}
