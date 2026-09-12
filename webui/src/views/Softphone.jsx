import React, { useEffect, useRef, useState, useCallback } from 'react'
import { api } from '../api.js'
import { Softphone as Phone, audioInputPresence, microphoneMessage, MEDIA_FAIL_CAUSE } from '../softphone.js'
import SimSelector from './SimSelector.jsx'
import { useI18n } from '../i18n.jsx'

const GREEN = '#22c55e', RED = '#ef4444'
const KEYS = [['1', ''], ['2', 'ABC'], ['3', 'DEF'], ['4', 'GHI'], ['5', 'JKL'],
  ['6', 'MNO'], ['7', 'PQRS'], ['8', 'TUV'], ['9', 'WXYZ'], ['*', ''], ['0', '+'], ['#', '']]

const fmtDur = (s) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`

// Call-history dispositions as recorded by the backend (_call_disposition), mapped to
// translatable labels.
function VoicemailRow({ instanceId, voicemail, open, onOpen, onHeard, onDelete, t }) {
  // The recording is fetched only when the user asks for it: rendering an <audio src> per row
  // would make opening the call log download every message on the line.
  const mins = Math.floor((voicemail.duration_seconds || 0) / 60)
  const secs = String((voicemail.duration_seconds || 0) % 60).padStart(2, '0')
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 9, marginTop: 7, padding: '7px 10px',
      border: '1px solid var(--border)', borderRadius: 9, background: 'var(--hover)', maxWidth: 360 }}>
      {!voicemail.listened && <i title={t('Not played yet')} style={{ flex: 'none', width: 7, height: 7,
        borderRadius: '50%', background: RED }} />}
      {open ? (
        <audio controls autoPlay style={{ flex: 1, height: 32 }} onPlay={onHeard}
          src={api.voicemailAudioUrl(instanceId, voicemail.id)} />
      ) : (
        <button className="btn btn-ghost" style={{ padding: '3px 10px', fontSize: 12 }}
          onClick={onOpen}>▶ {t('Play')}</button>
      )}
      <span style={{ flex: 'none', color: 'var(--text-mute)', fontSize: 11 }}>{mins}:{secs}</span>
      <button className="row-del" style={{ opacity: .6 }} title={t('Delete this voicemail')}
        onClick={onDelete}>🗑</button>
    </div>
  )
}

const CALL_STATUS_LABEL = {
  answered: 'Answered', missed: 'Missed', rejected: 'Declined', busy: 'Busy',
  'no answer': 'No answer', cancelled: 'Cancelled', failed: 'Failed',
  ringing: 'Ringing', dialing: 'Dialing', unknown: 'Unknown',
  // Service codes are scored on their own scale by the control plane: whether the carrier
  // acted on the request, not whether a conversation happened.
  'code accepted': 'Carrier accepted', 'code unsupported': 'Not supported by carrier',
  'code rejected': 'Carrier refused', 'code failed': 'Carrier could not handle it',
}

// A service code is answered and torn down immediately, so every call-shaped ending would
// misdescribe it. JsSIP's cause carries the SIP response that decided the outcome, which is
// the only place the carrier says whether it serves the code at all. A cause listed here is a
// VERDICT — the carrier has spoken, so there is nothing left to wait for.
const SERVICE_CODE_END_LABEL = {
  'Not Found': 'The carrier does not recognise this code.',
  'Address Incomplete': 'The carrier rejected this code as malformed.',
  'Incompatible SDP': 'The carrier will not serve this code on this line.',
  Rejected: 'The carrier refused this code.',
  Unavailable: 'The carrier could not serve this code right now.',
  Canceled: 'Cancelled before the carrier answered.',
}

// The service-code outcomes that are VERDICTS. 'dialing'/'ringing' are the call still being
// in progress, not a conclusion — quoting one as the result printed "Dialing" on the screen
// that reports how the call ended.
const SETTLED_CODE_STATUS = new Set([
  'code accepted', 'code unsupported', 'code rejected', 'code failed',
])

// A privacy extension that blocks WebRTC does not remove RTCPeerConnection — it replaces it
// with something that is not a constructor. JsSIP then stalls inside connect() without ever
// emitting an error: no SDP is generated, no INVITE is sent, and the call screen runs its full
// course on a request that never left the browser. Diagnosed from a user whose gateway looked
// broken for two days; the engine, dialplan and carrier were never involved.
const WEBRTC_AVAILABLE = typeof RTCPeerConnection === 'function'

// SIP registration states reported by the JsSIP wrapper.
const REG_LABEL = {
  loading: 'Loading…', idle: 'Idle', connecting: 'Connecting', registered: 'Registered',
  unregistered: 'Unregistered', disconnected: 'Disconnected', failed: 'Registration failed',
}

// MMI codes a handset answers by itself: they are never sent to the network. Here the
// "handset" is the engine, so these are answered from the line's own provisioning — dialling
// them out would only wait for a response no carrier ever sends. Maps code -> instance field.
export const LOCAL_MMI = { '*#06#': 'imei' }

export const normalizeDialTarget = (value) => {
  let number = String(value || '').replace(/[\s().-]/g, '')
  // Carrier service short codes (balance, voicemail, support, etc.) are intentionally
  // dialled as-is and are not E.164 numbers. Keep the bound tight so a normal national
  // number is not accidentally sent without its country code.
  if (/^\d{2,6}$/.test(number)) return number
  // Supplementary-service and USSD codes (*21*<number>#, *#21#, #225#). What they mean is
  // decided by the carrier's IMS, not here, so pass them through verbatim. The 180-character
  // ceiling is the USSD limit from 3GPP TS 22.030.
  if (/^[*#][*#\d]{1,180}$/.test(number)) return number
  if (number.startsWith('00')) number = `+${number.slice(2)}`
  return /^\+[1-9]\d{6,14}$/.test(number) ? number : ''
}

// A service code is signalling, not a number: it has no audio path and the cellular backend
// dials it as a voice call, so it must not be routed there.
export const isServiceCode = (target) => /[*#]/.test(String(target || ''))

function Avatar({ label, color = 'var(--primary)', size = 96 }) {
  return (
    <div style={{ width: size, height: size, borderRadius: '50%', background: color + '22',
      border: `2px solid ${color}55`, display: 'flex', alignItems: 'center', justifyContent: 'center',
      fontSize: size * 0.42, color, margin: '0 auto' }}>☎</div>
  )
}

function RoundBtn({ icon, label, color, bg, onClick, active }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 6 }}>
      <button onClick={onClick} style={{
        width: 58, height: 58, borderRadius: '50%', cursor: 'pointer', fontSize: 22,
        border: '1px solid ' + (active ? color : 'var(--border-strong)'),
        background: bg || (active ? color + '22' : 'var(--hover)'),
        color: active ? color : 'var(--text-soft)', display: 'flex', alignItems: 'center', justifyContent: 'center',
      }}>{icon}</button>
      <span style={{ fontSize: 11, color: 'var(--text-mute)' }}>{label}</span>
    </div>
  )
}

export default function Softphone({ selected, subscribe, instances, cards, devices, setSelected, showToast, initialLoading, loadErrors }) {
  const { t } = useI18n()
  const id = selected?.id
  const [prov, setProv] = useState(null)
  const [reg, setReg] = useState('loading')
  const [call, setCall] = useState(null)     // {dir, number, state, startedAt, endCause}
  const [callTransport, setCallTransport] = useState('vowifi')
  const [cellularBusy, setCellularBusy] = useState(false)
  const [num, setNum] = useState('')
  const [dur, setDur] = useState(0)
  const [muted, setMuted] = useState(false)
  const [keypad, setKeypad] = useState(false)
  const [dtmfSeq, setDtmfSeq] = useState('')   // digits/symbols entered since the keypad opened
  const [recording, setRecording] = useState(false)
  // 'present' | 'none' | 'insecure' | 'unknown'. Starts optimistic so the warning banner can
  // only ever appear once the probe has actually answered.
  const [micPresence, setMicPresence] = useState('present')
  const [calls, setCalls] = useState([])
  const [callSelMode, setCallSelMode] = useState(false)
  const [callSel, setCallSel] = useState(() => new Set())
  // Keyed by voicemail id. The <audio> is only created once the user asks to play, so a log
  // full of messages does not open a fetch per row.
  const [voicemails, setVoicemails] = useState({})
  const [historyLoading, setHistoryLoading] = useState(false)
  const [vmOpen, setVmOpen] = useState(null)
  const phone = useRef(null)
  // JsSIP can emit `unregistered` while a freshly-created UA is still opening its websocket.
  // That is an initial condition, not evidence that a working registration was lost.
  const registeredOnce = useRef(false)
  // Persistent, DOM-rendered <audio> sink. One stable element (primed on the first click via
  // unlockAudio) is what makes remote WebRTC audio play under Chrome/Edge autoplay policy.
  const audioRef = useRef(null)
  const selectedDevice = devices.find((device) => device.present === true
    && device.device_type === 'modem'
    && String(device.instance_id || '') === String(id || ''))
  const cellularAvailable = Boolean(selectedDevice)
  const cellularCapability = selectedDevice?.capabilities?.cellular || selectedDevice?.cellular || {}
  const cellularDesired = typeof cellularCapability === 'object'
    ? Boolean(cellularCapability.desired ?? cellularCapability.enabled) : Boolean(cellularCapability)
  const cellularActual = String(typeof cellularCapability === 'object'
    ? (cellularCapability.actual || cellularCapability.state || '') : cellularCapability).toLowerCase()
  const cellularReady = cellularAvailable && cellularDesired
    && ['on', 'connected', 'registered', 'active'].includes(cellularActual)

  // Several websocket events land in the same instant when a service code ends (the reply
  // and the outcome arrive separately), and each asks for a refresh. Those responses can
  // return out of order: an older one landing last would put a stale list back on screen and
  // the verdict would flip back to "waiting". Only the newest request may write.
  const loadSeq = useRef(0)
  const loadCalls = useCallback((showLoading = false) => {
    if (!id) return
    const seq = ++loadSeq.current
    if (showLoading) setHistoryLoading(true)
    api.calls(id).then((r) => { if (seq === loadSeq.current) setCalls(r.calls || []) }).catch(() => {})
      .finally(() => { if (seq === loadSeq.current) setHistoryLoading(false) })
  }, [id])
  const markHeard = (vid) => {
    if (voicemails[vid]?.listened) return
    setVoicemails((v) => ({ ...v, [vid]: { ...v[vid], listened: true } }))
    api.markVoicemailListened(id, vid).catch(() => {})
  }
  const deleteVoicemail = async (vid, e) => {
    e?.stopPropagation()
    try {
      await api.deleteVoicemails(id, { ids: [vid] })
      if (vmOpen === vid) setVmOpen(null)
      loadVoicemails(); loadCalls()
    } catch (error) { showToast?.(error.message) }
  }
  const loadVoicemails = useCallback(() => {
    if (!id) return
    api.voicemails(id)
      .then((r) => setVoicemails(Object.fromEntries((r.voicemails || []).map((v) => [v.id, v]))))
      .catch(() => {})
  }, [id])
  useEffect(() => { loadCalls(true); loadVoicemails() }, [loadCalls, loadVoicemails])
  useEffect(() => { setCallSelMode(false); setCallSel(new Set()); setCallTransport('vowifi') }, [id])
  useEffect(() => {
    if (!cellularReady && callTransport === 'cellular') setCallTransport('vowifi')
  }, [cellularReady, callTransport])
  // if the list empties (own delete, or another client's clear-all over WS), leave select
  // mode so the toolbar/checkbox UI can't get stranded on an empty list.
  useEffect(() => { if (!calls.length) { setCallSelMode(false); setCallSel(new Set()) } }, [calls.length])
  // A carrier's answer to a service code arrives after the call is already tearing down (it
  // rides the BYE), so it reaches the browser over the websocket rather than through JsSIP.
  // Graft it onto the call still on screen so the user sees the reply where they asked.
  useEffect(() => subscribe && subscribe((m) => {
    if (m.type === 'voicemail' && m.instance === id) { loadVoicemails(); loadCalls(); return }
    if (m.type !== 'call' || m.instance !== id) return
    // The manager decides an outcome from the Q.850 cause, which is strictly better evidence
    // than the SIP cause JsSIP hands us — and the history list already shows ITS verdict.
    // Carry it onto the live call so the ended screen cannot contradict the row behind it.
    if (m.call && (m.call.ussd_text || m.call.status)) {
      setCall((c) => (c && c.number === m.call.peer
        ? { ...c, ussdText: m.call.ussd_text || c.ussdText, backendStatus: m.call.status }
        : c))
    }
    loadCalls()
  }), [subscribe, id, loadCalls, loadVoicemails])

  // The manager's verdict for the call on screen. Prefer what the websocket pushed, but fall
  // back to the history list — it is the same data fetched over the API, so this no longer
  // depends on one message arriving at the right moment. The list is newest-first, so a code
  // dialled repeatedly resolves to the current attempt.
  const rawVerdict = call?.backendStatus
    || (call?.number ? calls.find((c) => c.peer === call.number && c.direction === 'out')?.status : null)
  const seenVerdict = SETTLED_CODE_STATUS.has(rawVerdict) ? rawVerdict : null
  // A verdict is the carrier's final word. Once it has been shown, no later refresh may take
  // it away again — otherwise the screen alternates between an answer and "waiting for one".
  const [stickyVerdict, setStickyVerdict] = useState(null)
  // Clear only when a NEW call starts. Clearing on every state change discarded a verdict
  // that had already arrived while the call was still up: the active -> ended transition wiped
  // it, so the screen fell back to "waiting" for an instant and then jumped forward again.
  useEffect(() => {
    if (call?.state === 'calling' || call?.state === 'incoming') setStickyVerdict(null)
  }, [call?.state])
  useEffect(() => { if (seenVerdict) setStickyVerdict(seenVerdict) }, [seenVerdict])
  const backendVerdict = stickyVerdict || seenVerdict

  const toast = (m) => (showToast ? showToast(m) : null)
  const toggleCallSel = (cid) => setCallSel((s) => { const n = new Set(s); n.has(cid) ? n.delete(cid) : n.add(cid); return n })
  // Reload only if still on the same line (a delete may resolve after the user switched SIMs).
  const reloadIfSame = (forId) => { if (forId === id) loadCalls() }

  const deleteSelectedCalls = async () => {
    if (!callSel.size) return
    if (!confirm(`Delete ${callSel.size} selected call${callSel.size > 1 ? 's' : ''}?`)) return
    const forId = id
    try {
      await api.deleteCalls(forId, { ids: [...callSel] })
      setCallSelMode(false); setCallSel(new Set()); reloadIfSame(forId); toast('Calls deleted')
    } catch (e) { toast('Delete failed: ' + e.message) }
  }
  const deleteOneCall = async (cid, e) => {
    if (e) e.stopPropagation()
    const forId = id
    try { await api.deleteCalls(forId, { ids: [cid] }); reloadIfSame(forId) } catch (e2) { toast('Delete failed: ' + e2.message) }
  }
  const clearAllCalls = async () => {
    if (!calls.length) return
    if (!confirm('Clear the entire call history for this line?')) return
    const forId = id
    try { await api.deleteCalls(forId, { all: true }); setCallSelMode(false); setCallSel(new Set()); reloadIfSame(forId); toast('Call history cleared') }
    catch (e) { toast('Delete failed: ' + e.message) }
  }

  // provisioning + connect (only while this page is mounted => only listens for incoming here)
  useEffect(() => {
    if (!id) return
    let alive = true
    registeredOnce.current = false
    setProv(null); setReg('loading'); setCall(null)
    api.softphone(id).then((p) => {
      if (!alive) return
      setProv(p)
      if (!p?.enabled) setReg('idle')
    }).catch(() => { if (alive) setReg('failed') })
    return () => { alive = false; if (phone.current) { phone.current.stop(); phone.current = null } }
  }, [id])

  const clearCallSoon = (endCause) => {
    setCall((c) => c ? { ...c, state: 'ended', endCause } : null)
    setKeypad(false); setMuted(false); setRecording(false)
    loadCalls()
  }
  // How long the 'ended' screen stays up. A service code's answer is NOT carried by the call:
  // it rides an in-dialog request, is parsed by the manager and arrives over the websocket
  // after the call is already torn down. Clearing on the ordinary 2.5s would hide the very
  // thing the user dialled for, so wait for the reply, then leave it up long enough to read.
  const [ussdTimedOut, setUssdTimedOut] = useState(false)
  const [dismissIn, setDismissIn] = useState(null)   // seconds left on the Back button
  // A service code's answer arrives after the call is over, so the 'ended' screen has to
  // outlive the call. Two questions decide what it does, and they are computed here as plain
  // booleans on purpose: the timers below depend on THESE, not on the reply itself, so a
  // verdict or a text landing a second later cannot restart a countdown already running.
  // 'accepted' means the carrier took the request, so a reply may still be on its way — the
  // verdict and the text arrive separately and the verdict usually wins. Any other verdict
  // (refused, unsupported, could-not-handle) is the end of it; nothing more is coming.
  const mayStillReply = !backendVerdict || backendVerdict === 'code accepted'
  const awaitingReply = call?.state === 'ended' && Boolean(call.serviceCode)
    && !call.ussdText && mayStillReply && !ussdTimedOut
  const codeSettled = call?.state === 'ended' && Boolean(call.serviceCode) && !awaitingReply

  useEffect(() => { setUssdTimedOut(false); setDismissIn(null) }, [call?.number, call?.state])
  // Hold the screen while the carrier is still expected to speak, but stop short of claiming
  // nothing is coming until the window has actually elapsed.
  useEffect(() => {
    if (!awaitingReply) return
    // A verdict already in hand proves the path works, so a reply — if there is one — follows
    // within about a second. With no verdict yet the manager itself may be slow, so allow far
    // longer before concluding anything.
    const t = setTimeout(() => setUssdTimedOut(true), backendVerdict ? 3000 : 10000)
    return () => clearTimeout(t)
  }, [awaitingReply, backendVerdict])
  // An ordinary call has nothing to read, so it clears itself as before.
  useEffect(() => {
    if (call?.state !== 'ended' || call.serviceCode) return
    const t = setTimeout(() => setCall(null), 2500)
    return () => clearTimeout(t)
  }, [call?.state, call?.serviceCode])
  // A settled service code carries text the user has to READ. Vanishing on a timer can take
  // the answer away mid-sentence, so count down visibly on a Back button they can also just
  // press — the wait becomes theirs to end, not the UI's to impose.
  useEffect(() => {
    if (!codeSettled) return
    setDismissIn(30)
    const iv = setInterval(() => setDismissIn((n) => (n === null ? null : n - 1)), 1000)
    return () => clearInterval(iv)
  }, [codeSettled])
  useEffect(() => { if (dismissIn === 0) setCall(null) }, [dismissIn])

  const connect = useCallback(() => {
    if (!prov || !prov.enabled || phone.current) return
    const ph = new Phone((type, data) => {
      if (type === 'registered' && data) {
        registeredOnce.current = true
        setReg('registered')
      } else if (type === 'registered') {
        setReg(registeredOnce.current ? 'unregistered' : 'connecting')
      }
      else if (type === 'ws') setReg((r) => data === 'connected' ? (r === 'registered' ? r : 'connecting') : 'disconnected')
      else if (type === 'regfail') setReg('failed')
      else if (type === 'incoming') setCall({ dir: 'in', number: data.from || 'Unknown', state: 'incoming', transport: 'vowifi' })
      else if (type === 'calling') setCall({ dir: 'out', number: data.to, state: 'calling', transport: 'vowifi', serviceCode: isServiceCode(data.to) })
      // 'progress' fires for BOTH directions. On an incoming call JsSIP auto-sends 180 and
      // emits progress('local'); mapping that to 'ringing' would blow away the 'incoming'
      // state and hide the Answer/Decline overlay. Only an OUTGOING call still in the
      // dialing/ringing phase should advance to 'ringing' — leave incoming/active/ended alone.
      else if (type === 'progress') setCall((c) => (c && c.dir === 'out' && (c.state === 'calling' || c.state === 'ringing')) ? { ...c, state: 'ringing' } : c)
      else if (type === 'active') setCall((c) => c ? { ...c, state: 'active', startedAt: Date.now() } : c)
      else if (type === 'ended') clearCallSoon(data && data.cause)
      else if (type === 'failed') clearCallSoon(data && data.cause)
      // The device can also disappear or be grabbed between the pre-dial check and the call
      // (or on answering an incoming one, which has no pre-dial check at all). 'failed' has
      // already reset the screen by now; this is what tells the user why.
      else if (type === 'mediafail') toast(t(microphoneMessage(data)))
    }, audioRef.current)
    ph.start(prov, prov.host || location.hostname)
    phone.current = ph
    setReg('connecting')
  }, [prov])

  useEffect(() => { if (prov && prov.enabled && !phone.current) connect() }, [prov, connect])

  // The <audio> element mounts with the component; make sure the phone (which may have been
  // created before the ref attached) points at it.
  useEffect(() => { if (phone.current && audioRef.current) phone.current.setAudioEl(audioRef.current) })

  // Probe for a microphone once, and again whenever the set of devices changes (a headset
  // plugged in should clear the warning without a reload). enumerateDevices() prompts for
  // nothing and opens nothing, so this is free to run on mount.
  useEffect(() => {
    let alive = true
    const probe = () => { audioInputPresence().then((p) => { if (alive) setMicPresence(p) }).catch(() => {}) }
    probe()
    const media = navigator.mediaDevices
    media?.addEventListener?.('devicechange', probe)
    return () => { alive = false; media?.removeEventListener?.('devicechange', probe) }
  }, [])

  // in-call duration timer
  useEffect(() => {
    if (call?.state !== 'active' || !call.startedAt) { setDur(0); return }
    const t = setInterval(() => setDur(Math.floor((Date.now() - call.startedAt) / 1000)), 500)
    return () => clearInterval(t)
  }, [call?.state, call?.startedAt])

  // ModemManager owns the cellular call object and exposes its signalling state. Poll only
  // while our experimental cellular dial UI is active; no microphone/audio path is implied.
  useEffect(() => {
    if (!id || call?.transport !== 'cellular' || call.state === 'ended') return
    let alive = true
    const poll = async () => {
      try {
        const result = await api.cellularCallStatus(id)
        if (!alive) return
        const state = result.status
        if (result.unavailable || state === 'failed') {
          toast(`${t('Cellular call ended')}: ${result.error || t('Cellular modem is unavailable')}`)
          clearCallSoon('Failed')
          return
        }
        if (state === 'active') {
          setCall((current) => current?.transport === 'cellular'
            ? { ...current, state: 'active', startedAt: current.startedAt || Date.now() } : current)
        } else if (state === 'dialing' || state === 'ringing-out') {
          setCall((current) => current?.transport === 'cellular' ? { ...current, state: 'ringing' } : current)
        } else if (state === 'terminated' || state === 'idle' || state === 'ended') {
          clearCallSoon(result.call?.reason || 'Ended')
        }
      } catch (error) {
        if (alive) toast(`${t('Could not read cellular call status')}: ${error.message}`)
      }
    }
    poll()
    const timer = setInterval(poll, 2000)
    return () => { alive = false; clearInterval(timer) }
  }, [id, call?.transport, call?.state])

  // Physical-keyboard DTMF: while the in-call keypad is open, let the user type 0-9 * #
  // directly instead of only clicking. Clear the echo strip each time the keypad opens.
  useEffect(() => {
    if (!(keypad && call?.state === 'active')) return
    setDtmfSeq('')
    const onKey = (e) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return
      // Shift+3 produces '#'; a bare '3' should stay '3'. e.key already reflects the shifted
      // character, so match on the resulting character directly.
      const k = e.key
      if (/^[0-9*#]$/.test(k)) { e.preventDefault(); pressDTMF(k) }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [keypad, call?.state])

  // Watchdog: a call parked in a non-terminal setup phase (calling/ringing/incoming) that
  // never advances to 'active' or 'ended' — e.g. a BYE/terminal JsSIP event was dropped —
  // would otherwise strand the UI forever. Force it back to idle after a timeout. 'active'
  // has no timeout (calls can be long); 'ended' clears on its own via clearCallSoon.
  useEffect(() => {
    if (!call || call.state === 'active' || call.state === 'ended') return
    const ms = call.state === 'incoming' ? 60000 : 65000
    const t = setTimeout(() => {
      // Ask the phone to tear down whatever it thinks it has, then reset the UI.
      try {
        if (call.transport === 'cellular') api.cellularCallHangup(id).catch(() => {})
        else phone.current?.hangup()
      } catch {}
      setCall(null); setKeypad(false); setMuted(false); setRecording(false)
    }, ms)
    return () => clearTimeout(t)
  }, [call?.state])

  const dialKey = (k) => {
    if (call?.state === 'active') { phone.current?.sendDTMF(k); setNum((n) => n + k) }
    else setNum((n) => n + k)
  }
  // In-call DTMF: send the tone and echo it into the keypad's display strip.
  const pressDTMF = (k) => { phone.current?.sendDTMF(k); setDtmfSeq((s) => (s + k).slice(-32)) }
  const placeCall = async (number = num) => {
    if (!number) return
    const target = normalizeDialTarget(number)
    if (!target) { toast(t('Use a service short code or international format, for example +8613800138000.')); return }
    // Answer local MMI codes here instead of dialling them; the carrier never replies to these.
    const localField = LOCAL_MMI[target]
    if (localField) {
      const value = String(selected?.[localField] || '')
      toast(value ? `${target} \u2192 ${value}` : t('This line has no value provisioned for that code.'))
      setNum('')
      return
    }
    if (callTransport === 'vowifi' && !WEBRTC_AVAILABLE) {
      toast(t('This browser has WebRTC disabled, so no call can be placed. A privacy or ad-blocking extension is the usual cause — allow WebRTC for this site, or open it in a private window.'))
      return
    }
    // The status dot already says the line is down, but the Call button did not read it: the
    // INVITE went into a websocket that is not there and the screen reported an ordinary
    // failed call. 'connecting' is deliberately allowed through — the transport may come up
    // within the call setup — while these three states cannot carry a call at all.
    if (callTransport === 'vowifi' && ['disconnected', 'failed', 'unregistered'].includes(reg)) {
      toast(t(reg === 'disconnected'
        ? 'This browser is not connected to the line’s engine, so the call cannot be placed. Check that the engine for this SIM is running.'
        : 'This line is not registered right now, so the call cannot be placed. Wait for the Registered indicator, or check the line’s VoWiFi status.'))
      return
    }
    if (callTransport === 'cellular') {
      // The cellular backend places a voice call. A service code is supplementary-service
      // signalling, which needs AT+CUSD instead, so fail loudly rather than dialling nonsense.
      if (isServiceCode(target)) {
        toast(t('Service codes can only be dialled over VoWiFi, not the cellular modem.'))
        return
      }
      if (!cellularReady) { toast(t('Turn on 4G and wait for the cellular modem to become ready first.')); return }
      if (!window.confirm(t('Place this call through the cellular modem? This experimental mode has no browser audio; the called phone may ring and normal call charges may apply.'))) return
      setCellularBusy(true)
      try {
        const result = await api.cellularCall(id, target)
        if (!result.ok && !result.uncertain) {
          toast(`${t('Cellular call failed')}: ${result.error || t('Unknown')}`)
          return
        }
        setCall({ dir: 'out', number: target, state: 'calling', transport: 'cellular' })
        setNum('')
        toast(result.uncertain
          ? t('Call start is uncertain. Use Hang up before trying again.')
          : t('Cellular dial started. Audio is not connected to the browser.'))
      } catch (error) { toast(`${t('Cellular call failed')}: ${error.message}`) }
      finally { setCellularBusy(false) }
      return
    }
    if (!phone.current) return
    // Must stay synchronous inside the click: it is the transient user activation that makes
    // remote audio playable later. Harmless if the microphone check below then aborts.
    phone.current.unlockAudio()
    // Re-probe rather than trust the banner's state: a microphone can be unplugged between
    // the page loading and this click.
    const presence = await audioInputPresence()
    if (presence === 'none' || presence === 'insecure') {
      setMicPresence(presence)
      toast(t(microphoneMessage(presence)))
      return
    }
    phone.current.call(target); setNum('')
  }
  const answer = () => { phone.current?.unlockAudio(); phone.current?.answer() }
  // Optimistically move to 'ended' on a local hangup. JsSIP will still fire 'ended'
  // (→ clearCallSoon), but if that event is delayed or missed the UI has already left the
  // active/ringing screen instead of stranding on it.
  const hangup = async () => {
    if (call?.transport === 'cellular') {
      setCellularBusy(true)
      try {
        const result = await api.cellularCallHangup(id)
        if (!result.ok) {
          toast(`${t('Cellular hangup failed')}: ${result.error || t('Call state is unknown')}`)
          return
        }
      }
      catch (error) { toast(`${t('Cellular hangup failed')}: ${error.message}`); return }
      finally { setCellularBusy(false) }
    } else phone.current?.hangup()
    setCall((c) => (c && c.state !== 'ended') ? { ...c, state: 'ended', endCause: c.endCause } : c)
    setKeypad(false); setMuted(false); setRecording(false)
    setTimeout(() => setCall((c) => (c && c.state === 'ended') ? null : c), 2500)
  }
  // Declining a ringing incoming call must send 603 (→ "declined"), not a bare hangup
  // (→ "missed"). reject() picks the right signalling for an un-answered incoming session.
  const decline = () => {
    phone.current?.reject()
    setCall((c) => (c && c.state !== 'ended') ? { ...c, state: 'ended', endCause: 'Rejected' } : c)
    setKeypad(false); setMuted(false); setRecording(false)
    setTimeout(() => setCall((c) => (c && c.state === 'ended') ? null : c), 2500)
  }
  const toggleMute = () => { const m = !muted; setMuted(m); phone.current?.setMuted(m) }
  const toggleRecord = async () => {
    if (!phone.current) return
    if (recording) {
      const blob = await phone.current.stopRecording(); setRecording(false)
      if (blob) {
        const url = URL.createObjectURL(blob)
        const a = document.createElement('a')
        a.href = url; a.download = `call-${call?.number || 'rec'}-${Date.now()}.webm`; a.click()
        setTimeout(() => URL.revokeObjectURL(url), 10000)
      }
    } else { const ok = await phone.current.startRecording(); setRecording(ok) }
  }

  if (initialLoading && !id) return <p role="status">{t('Loading')}…</p>
  if (loadErrors?.instances && !id) return <p className="u-error">{t('Loading failed')}</p>
  if (!id) return (
    <div>
      <SimSelector instances={instances} cards={cards} devices={devices} selected={selected} setSelected={setSelected} />
      <div style={{ color: 'var(--text-dim)' }}>{t('Select a SIM / line to use the softphone.')}</div>
    </div>
  )

  const regColor = reg === 'registered' ? GREEN : reg === 'failed' || reg === 'disconnected' ? RED : '#eab308'
  const inCall = call && (call.state === 'active' || call.state === 'calling' || call.state === 'ringing' || call.state === 'incoming' || call.state === 'ended')
  const endLabel = (c, isCode) => (isCode
    ? t(SERVICE_CODE_END_LABEL[c] || 'The carrier gave no usable answer to this code.')
    // A local media failure never reached the carrier, so reporting it as a plain "Call
    // ended" describes the one thing that did NOT happen. Name it: the toast explains what
    // to do about it, and this line stops the screen from blaming the call.
    : t(c === MEDIA_FAIL_CAUSE ? 'Microphone unavailable'
      : c === 'Rejected' ? 'Call declined' : c === 'Busy' ? 'Busy' : c === 'Canceled' || c === 'Canceled/Rejected' ? 'Call cancelled' : 'Call ended'))

  // Google-Voice-style incoming-call overlay (prominent, full-panel)
  const IncomingOverlay = call?.state === 'incoming' ? (
    <div style={{ position: 'fixed', inset: 0, zIndex: 100, background: 'rgba(6,10,20,0.82)',
      backdropFilter: 'blur(3px)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
      <div className="card" style={{ padding: 40, width: 380, textAlign: 'center',
        boxShadow: '0 20px 60px rgba(0,0,0,.6)', animation: 'none' }}>
        <div style={{ fontSize: 13, color: 'var(--text-mute)', letterSpacing: 1, textTransform: 'uppercase' }}>{t('Incoming call')}</div>
        <div style={{ margin: '22px 0' }}><Avatar label={call.number} color={GREEN} size={110} /></div>
        <div className="mono" style={{ fontSize: 26, fontWeight: 800 }}>{call.number || 'Unknown'}</div>
        <div style={{ fontSize: 13, color: 'var(--text-mute)', marginTop: 6 }}>{selected?.name || 'VoWiFi line'}</div>
        <div style={{ display: 'flex', justifyContent: 'center', gap: 56, marginTop: 34 }}>
          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 8 }}>
            <button onClick={decline} style={{ width: 68, height: 68, borderRadius: '50%', border: 'none',
              cursor: 'pointer', fontSize: 26, background: RED, color: '#fff' }}>✕</button>
            <span style={{ fontSize: 13, color: 'var(--text-soft)' }}>{t('Decline')}</span>
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 8 }}>
            <button onClick={answer} style={{ width: 68, height: 68, borderRadius: '50%', border: 'none',
              cursor: 'pointer', fontSize: 26, background: GREEN, color: '#fff',
              boxShadow: `0 0 0 0 ${GREEN}`, animation: 'ringpulse 1.4s infinite' }}>✆</button>
            <span style={{ fontSize: 13, color: 'var(--text-soft)' }}>{t('Answer')}</span>
          </div>
        </div>
      </div>
    </div>
  ) : null

  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
      {/* Persistent remote-audio sink: JsSIP writes the remote MediaStream here. autoPlay +
          a stable DOM element + unlockAudio() on the first click = reliable playback. */}
      <audio ref={audioRef} autoPlay playsInline style={{ display: 'none' }} />
      <div style={{ flexShrink: 0 }}>
        <SimSelector instances={instances} cards={cards} devices={devices} selected={selected} setSelected={setSelected} />
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: '380px 1fr', gridTemplateRows: 'minmax(0, 1fr)', gap: 16, flex: 1, minHeight: 0 }}>
      {IncomingOverlay}
      <style>{`@keyframes ringpulse{0%{box-shadow:0 0 0 0 ${GREEN}88}70%{box-shadow:0 0 0 16px ${GREEN}00}100%{box-shadow:0 0 0 0 ${GREEN}00}}`}</style>
      {/* ---- Phone panel (Google-Voice style) ---- */}
      <div className="card" style={{ padding: 24, minHeight: 520, display: 'flex', flexDirection: 'column', overflow: 'auto' }}>
        {/* One row in a 380px panel. A <select> sized 'auto' takes its width from the LONGEST
            option, which is long enough here to squeeze both labels until they wrapped one
            character per line. So: labels never wrap and never shrink, and the select absorbs
            whatever width is left. */}
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 10, marginBottom: 8 }}>
          {cellularAvailable ? <label style={{ display: 'flex', alignItems: 'center', gap: 7, fontSize: 12,
            color: 'var(--text-dim)', flex: 1, minWidth: 0 }}>
            <span style={{ whiteSpace: 'nowrap', flexShrink: 0 }}>{t('Call via')}</span>
            <select value={callTransport} disabled={Boolean(inCall)} onChange={(event) => setCallTransport(event.target.value)}
              style={{ flex: 1, minWidth: 0, textOverflow: 'ellipsis' }}>
              <option value="vowifi">VoWiFi</option>
              <option value="cellular" disabled={!cellularReady}>{t('Cellular modem (experimental, no audio)')}{!cellularReady ? ` — ${t('4G off')}` : ''}</option>
            </select>
          </label> : <div style={{ fontSize: 13, color: 'var(--text-dim)', flex: 1, minWidth: 0 }}>{t('Softphone')}</div>}
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: regColor,
            whiteSpace: 'nowrap', flexShrink: 0 }}>
            <span style={{ width: 8, height: 8, borderRadius: 999, flexShrink: 0,
              background: callTransport === 'cellular' ? '#f59e0b' : regColor }} />
            {callTransport === 'cellular' ? t('No audio') : t(REG_LABEL[reg] || reg)}
          </div>
        </div>

        {/* Say it before a call is attempted, not after one silently fails: the failure mode
            is a screen that runs its full course and then blames the carrier. */}
        {callTransport === 'vowifi' && !WEBRTC_AVAILABLE && (
          <div style={{ margin: '12px 0', padding: '10px 12px', borderRadius: 8, fontSize: 13,
            lineHeight: 1.5, color: '#b45309', background: '#fffbeb', border: '1px solid #fcd34d' }}>
            {t('This browser has WebRTC disabled, so no call can be placed. A privacy or ad-blocking extension is the usual cause — allow WebRTC for this site, or open it in a private window.')}
          </div>
        )}
        {callTransport === 'vowifi' && (micPresence === 'none' || micPresence === 'insecure') && (
          <div style={{ margin: '12px 0', padding: '10px 12px', borderRadius: 8, fontSize: 13,
            lineHeight: 1.5, color: '#b45309', background: '#fffbeb', border: '1px solid #fcd34d' }}>
            {t(microphoneMessage(micPresence))}
          </div>
        )}
        {callTransport === 'vowifi' && prov && !prov.enabled && (
          <div style={{ color: '#f97316', fontSize: 13, margin: '12px 0' }}>
            {t('WebRTC is disabled for this SIM. Enable it in SIM Config (needs HTTPS/TLS) to use the browser phone.')}
          </div>
        )}
        {callTransport === 'cellular' && <div className="u-note" style={{ margin: '8px 0 12px', color: '#f59e0b' }}>
          {t('Experimental signalling only: the modem can dial and hang up, but browser audio, microphone, DTMF and recording are unavailable. The called party may still answer and charges may apply.')}
        </div>}

        {/* ===== INCOMING handled by full-screen overlay above ===== */}

        {/* ===== OUTGOING RINGING ===== */}
        {(call?.state === 'calling' || call?.state === 'ringing') && (
          <div style={{ flex: 1, display: 'flex', flexDirection: 'column', justifyContent: 'center', textAlign: 'center', gap: 16 }}>
            <Avatar label={call.number} />
            <div>
              <div className="mono" style={{ fontSize: 22, fontWeight: 700 }}>{call.number}</div>
              <div style={{ fontSize: 13, color: 'var(--text-mute)', marginTop: 4 }}>{call.serviceCode
                ? t('Sending the code to the carrier…')
                : (call.state === 'ringing' ? t('Ringing…') : t('Calling…'))}</div>
            </div>
            <div style={{ display: 'flex', justifyContent: 'center', marginTop: 10 }}>
              <RoundBtn icon="✕" label={t('End')} color="#fff" bg={RED} onClick={hangup} />
            </div>
          </div>
        )}

        {/* ===== IN CALL ===== */}
        {call?.state === 'active' && (
          <div style={{ flex: 1, display: 'flex', flexDirection: 'column', justifyContent: 'center', textAlign: 'center', gap: 14 }}>
            <Avatar label={call.number} color={GREEN} size={84} />
            <div>
              <div className="mono" style={{ fontSize: 20, fontWeight: 700 }}>{call.number || 'Unknown'}</div>
              {call.serviceCode
                ? <div style={{ fontSize: 13, color: GREEN, marginTop: 4 }}>{call.ussdText || t('Carrier accepted the code. Waiting for its reply…')}</div>
                : <div style={{ fontSize: 15, color: GREEN, marginTop: 4, fontVariantNumeric: 'tabular-nums' }}>{fmtDur(dur)}</div>}
              {call.transport === 'cellular' && <div style={{ fontSize: 12, color: '#f59e0b', marginTop: 5 }}>{t('Cellular call connected · browser audio unavailable')}</div>}
              {recording && <div style={{ fontSize: 12, color: RED, marginTop: 2 }}>● Recording</div>}
            </div>
            {call.transport !== 'cellular' && !call.serviceCode && keypad && (
              <div style={{ maxWidth: 220, margin: '0 auto', display: 'flex', flexDirection: 'column', gap: 8 }}>
                {/* Echo strip: shows every digit/symbol entered via click or physical keyboard */}
                <div className="mono" style={{ minHeight: 40, padding: '8px 12px', borderRadius: 8,
                  background: 'var(--surface-2, rgba(255,255,255,0.06))', border: '1px solid var(--border, rgba(255,255,255,0.12))',
                  fontSize: 20, letterSpacing: 2, textAlign: 'center', overflow: 'hidden', whiteSpace: 'nowrap',
                  direction: 'rtl', color: dtmfSeq ? 'var(--text)' : 'var(--text-mute)' }}>
                  {dtmfSeq || 'Type or tap keys'}
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3,1fr)', gap: 8 }}>
                  {KEYS.map(([k]) => (
                    <button key={k} className="btn btn-ghost" style={{ padding: 12, fontSize: 18 }}
                      onClick={() => pressDTMF(k)}>{k}</button>
                  ))}
                </div>
              </div>
            )}
            {/* Mute, keypad and record all act on audio, and a service code has none: it is
                signalling that the carrier answers and tears down in about a second. Offering
                them implies an audio call that is not happening — only Hang up is real here. */}
            {call.transport !== 'cellular' && !call.serviceCode && <div style={{ display: 'flex', justifyContent: 'center', gap: 22, marginTop: 8 }}>
              <RoundBtn icon={muted ? '🔇' : '🎙'} label={t(muted ? 'Unmute' : 'Mute')} color="#60a5fa" onClick={toggleMute} active={muted} />
              <RoundBtn icon="⌨" label={t('Keypad')} color="#a78bfa" onClick={() => setKeypad((v) => !v)} active={keypad} />
              <RoundBtn icon="⏺" label={t(recording ? 'Stop' : 'Record')} color={RED} onClick={toggleRecord} active={recording} />
            </div>}
            <div style={{ display: 'flex', justifyContent: 'center', marginTop: 6 }}>
              <RoundBtn icon="✕" label={t('Hangup')} color="#fff" bg={RED} onClick={hangup} />
            </div>
          </div>
        )}

        {/* ===== ENDED (brief) ===== */}
        {call?.state === 'ended' && (
          <div style={{ flex: 1, display: 'flex', flexDirection: 'column', justifyContent: 'center', textAlign: 'center', gap: 12 }}>
            <Avatar label={call.number} color={call.endCause === 'Rejected' ? RED : 'var(--text-mute)'} />
            <div className="mono" style={{ fontSize: 20, fontWeight: 700 }}>{call.number || 'Unknown'}</div>
            {call.ussdText && (
              <div style={{ maxWidth: 320, margin: '0 auto', padding: '12px 14px', borderRadius: 10,
                background: 'var(--input-bg)', border: '1px solid var(--border-strong)',
                fontSize: 14, lineHeight: 1.5, textAlign: 'left', whiteSpace: 'pre-wrap',
                wordBreak: 'break-word', color: 'var(--text)' }}>{call.ussdText}</div>
            )}
            {(() => {
              if (!call.serviceCode) {
                return <div style={{ fontSize: 14, color: call.endCause === 'Rejected' ? RED : 'var(--text-mute)' }}>{endLabel(call.endCause, false)}</div>
              }
              // A service code's verdict comes from the manager, which reads the Q.850 cause.
              // JsSIP's SIP cause is coarser — it reports a network failure (cause 38) as
              // "Rejected" — so showing it while the real verdict is still in flight means
              // announcing one outcome and then correcting it in front of the user. Wait
              // instead: the answer is a second away, and a brief "waiting" beats a wrong
              // answer that changes. The SIP cause is only consulted if nothing ever arrives.
              if (call.ussdText) {
                return <div style={{ fontSize: 14, color: GREEN }}>{t('Carrier replied')}</div>
              }
              if (CALL_STATUS_LABEL[backendVerdict]) {
                const bad = backendVerdict !== 'code accepted'
                // Accepted with nothing to show is the normal outcome for an ACTION code
                // (#21# cancels call forwarding; *#21# would query it). Saying only
                // "accepted" next to an empty screen reads as "nothing happened", which
                // invites dialling again to check — say that silence is expected instead.
                if (!bad) {
                  // Only say a code returns nothing once the wait has actually elapsed;
                  // saying it while the text is still in flight is the same mistake as
                  // reporting "no reply" a second before one arrives.
                  return <div style={{ fontSize: 14, color: GREEN }}>{t(ussdTimedOut
                    ? 'Carrier accepted the code. This kind of code returns no text.'
                    : 'Carrier accepted the code. Waiting for its reply…')}</div>
                }
                return <div style={{ fontSize: 14, color: RED }}>{t(CALL_STATUS_LABEL[backendVerdict])}</div>
              }
              if (!ussdTimedOut) {
                return <div style={{ fontSize: 14, color: 'var(--text-mute)' }}>{t('Waiting for the carrier\u2019s reply\u2026')}</div>
              }
              // Nothing was recorded for this call at all. The dialplan logs a call the
              // moment it matches, so no record means the code never matched — it was
              // rejected by our own Asterisk, not by the network, and an engine image older
              // than service-code support does exactly that. Blaming the carrier here sends
              // the user to their operator over a stale image on their own machine.
              if (!rawVerdict) {
                return <div style={{ fontSize: 13.5, color: '#f59e0b', maxWidth: 300, margin: '0 auto' }}>
                  {t('The gateway did not send this code. Its engine image may be older than service-code support — reload the installation to update it.')}
                </div>
              }
              return <div style={{ fontSize: 14, color: 'var(--text-mute)' }}>{endLabel(call.endCause, true)}</div>
            })()}
            {dismissIn !== null && (
              <button onClick={() => setCall(null)} style={{ margin: '4px auto 0', padding: '9px 22px',
                borderRadius: 10, cursor: 'pointer', background: 'var(--hover)', color: 'var(--text-soft)',
                border: '1px solid var(--border-strong)', fontSize: 13.5, fontVariantNumeric: 'tabular-nums' }}>
                {t('Back')} ({dismissIn}s)
              </button>
            )}
          </div>
        )}

        {/* ===== DIALER (idle) ===== */}
        {!inCall && (
          <div style={{ flex: 1, display: 'flex', flexDirection: 'column' }}>
            <input value={num} onChange={(e) => setNum(e.target.value)} placeholder={t('Enter a number')}
              className="mono" style={{ fontSize: 24, textAlign: 'center', margin: '10px 0 16px', letterSpacing: 1, border: 'none', background: 'transparent' }} />
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3,1fr)', gap: 10 }}>
              {KEYS.map(([k, sub]) => (
                <button key={k} onClick={() => dialKey(k)} style={{
                  padding: '10px 0', borderRadius: 12, cursor: 'pointer', background: 'var(--hover)',
                  border: '1px solid var(--border)', color: 'var(--text)', display: 'flex', flexDirection: 'column', alignItems: 'center',
                }}>
                  <span style={{ fontSize: 22, fontWeight: 600 }}>{k}</span>
                  <span style={{ fontSize: 9, color: 'var(--text-mute)', letterSpacing: 1, height: 10 }}>{sub}</span>
                </button>
              ))}
            </div>
            <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', gap: 24, marginTop: 16 }}>
              <div style={{ width: 58 }} />
              <button onClick={() => placeCall()} disabled={cellularBusy || !num || (callTransport === 'vowifi' ? reg !== 'registered' : !cellularReady)} style={{
                width: 64, height: 64, borderRadius: '50%', border: 'none', cursor: 'pointer', fontSize: 26,
                background: (num && (callTransport === 'cellular' ? cellularReady : reg === 'registered')) ? GREEN : 'var(--border-strong)', color: '#fff',
              }}>✆</button>
              <button onClick={() => setNum((n) => n.slice(0, -1))} style={{
                width: 58, height: 58, borderRadius: '50%', border: 'none', background: 'transparent',
                color: 'var(--text-mute)', cursor: 'pointer', fontSize: 22, visibility: num ? 'visible' : 'hidden',
              }}>⌫</button>
            </div>
          </div>
        )}
      </div>

      {/* ---- Recent calls ---- */}
      <div className="card" style={{ padding: 20, display: 'flex', flexDirection: 'column', minHeight: 0 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12, flexShrink: 0 }}>
          <div style={{ fontSize: 15, fontWeight: 600 }}>{t('Recent calls')}</div>
          {calls.length > 0 && (
            callSelMode ? (
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <span style={{ fontSize: 12, color: 'var(--text-mute)' }}>{callSel.size} selected</span>
                <button className="btn btn-ghost" style={{ padding: '4px 10px', fontSize: 12, color: RED }}
                  disabled={!callSel.size} onClick={deleteSelectedCalls}>{t('Delete')}</button>
                <button className="btn btn-ghost" style={{ padding: '4px 10px', fontSize: 12 }}
                  onClick={() => { setCallSelMode(false); setCallSel(new Set()) }}>{t('Cancel')}</button>
              </div>
            ) : (
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <button className="btn btn-ghost" style={{ padding: '4px 10px', fontSize: 12 }}
                  onClick={() => setCallSelMode(true)}>{t('Select')}</button>
                <button className="btn btn-ghost" style={{ padding: '4px 10px', fontSize: 12, color: RED }}
                  onClick={clearAllCalls}>{t('Clear all')}</button>
              </div>
            )
          )}
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6, flex: 1, minHeight: 0, overflow: 'auto' }}>
          {historyLoading && calls.length === 0 && <div role="status" style={{ fontSize: 13, color: 'var(--text-mute)' }}>{t('Loading')}…</div>}
          {!historyLoading && calls.length === 0 && <div style={{ fontSize: 13, color: 'var(--text-mute)' }}>{t('No calls yet.')}</div>}
          {calls.map((c) => {
            const s = (c.status || '').toLowerCase()
            const color = s === 'answered' ? GREEN : (s === 'rejected' || s === 'busy' || s === 'failed') ? RED
              : (s === 'no answer' || s === 'cancelled' || s === 'missed') ? '#eab308' : 'var(--text-dim)'
            const dlabel = c.direction === 'in' ? `↙ ${t('Incoming')}` : `↗ ${t('Outgoing')}`
            // Dispositions the backend records are translated; an unmapped one (a raw
            // DIALSTATUS from an unusual hangup) is shown as recorded rather than hidden.
            const statusKey = CALL_STATUS_LABEL[s || 'ringing']
            const checked = callSel.has(c.id)
            return (
              <div key={c.id} onClick={() => callSelMode && toggleCallSel(c.id)} className="hover-row"
                style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8,
                  fontSize: 13.5, padding: '10px 12px', borderRadius: 10, cursor: callSelMode ? 'pointer' : 'default',
                  background: checked ? 'var(--active)' : 'var(--input-bg)' }}>
                {callSelMode && <input type="checkbox" readOnly checked={checked} style={{ width: 'auto', flexShrink: 0 }} />}
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div className="mono" style={{ fontWeight: 600 }}>{c.peer}</div>
                  <div style={{ fontSize: 11, color: 'var(--text-mute)' }}>{dlabel} · {new Date(c.start_ts * 1000).toLocaleString()}{c.transport === 'cellular' ? ` · ${t('Cellular modem')}` : ''}</div>
                  {c.ussd_text && (
                    <div title={c.ussd_text} style={{ fontSize: 11.5, marginTop: 3, color: 'var(--text-soft)',
                      overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{c.ussd_text}</div>
                  )}
                  {c.voicemail_id && voicemails[c.voicemail_id] && (
                    <VoicemailRow instanceId={id} voicemail={voicemails[c.voicemail_id]} t={t}
                      open={vmOpen === c.voicemail_id}
                      onOpen={(e) => { e.stopPropagation(); setVmOpen(c.voicemail_id) }}
                      onHeard={() => markHeard(c.voicemail_id)}
                      onDelete={(e) => deleteVoicemail(c.voicemail_id, e)} />
                  )}
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                  <span style={{ color, fontWeight: 600, ...(statusKey ? {} : { textTransform: 'capitalize' }) }}>
                    {statusKey ? t(statusKey) : c.status}</span>
                  {!callSelMode && <>
                    <button className="btn btn-ghost" style={{ padding: '5px 10px' }}
                      disabled={callTransport === 'vowifi' ? reg !== 'registered' : !cellularReady}
                      onClick={(e) => { e.stopPropagation(); setNum(c.peer); placeCall(c.peer) }}>{t('Call')}</button>
                    <button className="row-del" title={t('Delete this call')} aria-label={t('Delete this call')}
                      onClick={(e) => deleteOneCall(c.id, e)}>🗑</button>
                  </>}
                </div>
              </div>
            )
          })}
        </div>
      </div>
      </div>
    </div>
  )
}
