// Browser softphone: JsSIP UA over WSS to the engine's Asterisk WebRTC transport.
import JsSIP from 'jssip'

// Surface JsSIP internals in the console to aid troubleshooting (registration, ICE, etc.)
try { JsSIP.debug.enable('JsSIP:*') } catch {}

// A call can die about ten milliseconds after the click with no INVITE ever sent, because
// JsSIP's very first step is getUserMedia: no microphone, no call. JsSIP reports every one of
// those failures as the single cause 'User Denied Media Access' (RTCSession's getUserMedia
// catch), which says nothing about a machine that simply has no audio input — the case
// reported from a desktop browser, where the dial screen vanished instantly and the console
// showed only NotFoundError. Name the real reason instead, before the call is attempted.
export const MEDIA_FAIL_CAUSE = 'User Denied Media Access'

// Does this browser have a microphone at all? enumerateDevices() needs no permission and does
// not open the device, and Chromium-family browsers still list one entry per AVAILABLE kind
// before permission is granted — so a non-empty list with no 'audioinput' is proof there is no
// microphone. An empty list means the browser is withholding device info, which proves
// nothing: report 'unknown' rather than warn about a microphone that is probably there.
export async function audioInputPresence() {
  const media = navigator.mediaDevices
  if (!media || !media.getUserMedia) return 'insecure'
  if (!media.enumerateDevices) return 'unknown'
  try {
    const devices = await media.enumerateDevices()
    if (!devices.length) return 'unknown'
    return devices.some((device) => device.kind === 'audioinput') ? 'present' : 'none'
  } catch { return 'unknown' }
}

// What to tell the user, keyed by the DOMException name the browser reported (or a presence
// verdict). None of these stop a call: a call with no microphone still carries the carrier's
// audio and is worth placing (a voicemail box, a service code, an announcement). They say
// what the call WILL be, so nobody discovers it by being unheard. The returned strings are
// the i18n keys; the caller translates them.
export function microphoneMessage(reason) {
  switch (reason) {
    case 'none':
    case 'NotFoundError':
    case 'DevicesNotFoundError':      // legacy Chrome name for the same condition
      return 'No microphone was found. Calls can still be placed and you will hear the other side, but they will not hear you.'
    case 'NotAllowedError':
    case 'PermissionDeniedError':
      return 'Microphone access is blocked for this site. Calls can still be placed and you will hear the other side, but they will not hear you until you allow it in the browser.'
    case 'NotReadableError':
    case 'TrackStartError':
      return 'The microphone is being held by another application. Calls can still be placed and you will hear the other side, but they will not hear you until it is released.'
    case 'insecure':
      return 'Browsers only allow microphone access over HTTPS. Calls can still be placed on this address and you will hear the other side, but they will not hear you.'
    default:
      return 'The browser could not open the microphone. Calls can still be placed and you will hear the other side, but they will not hear you.'
  }
}

// A local audio track is not optional: WebRTC has no offer to make without one, which is why
// a missing microphone used to end the call before an INVITE was ever sent. Silence is a
// perfectly good track. An oscillator at zero gain keeps the graph running so the destination
// really does produce (empty) frames rather than nothing at all.
function silentAudioStream() {
  const Ctx = window.AudioContext || window.webkitAudioContext
  if (!Ctx) return null
  try {
    const ctx = new Ctx()
    const dest = ctx.createMediaStreamDestination()
    const osc = ctx.createOscillator()
    const gain = ctx.createGain()
    gain.gain.value = 0
    osc.connect(gain).connect(dest)
    osc.start()
    ctx.resume?.().catch?.(() => {})
    return { stream: dest.stream, ctx, osc }
  } catch { return null }
}

export class Softphone {
  // audioEl: a persistent <audio> element rendered by React and handed in via ref. Using one
  // stable, DOM-attached element (instead of a per-call `new Audio()`) is what makes remote
  // audio reliable under Chrome/Edge autoplay policy: the element is primed once inside a user
  // gesture (unlockAudio) and then every later srcObject swap plays without a NotAllowedError.
  constructor(onEvent, audioEl) {
    this.onEvent = onEvent            // (type, data) => void
    this.ua = null
    this.session = null
    this.remoteAudio = audioEl || null
    this._dead = false                // set true by stop() to inert late JsSIP events
    this._unlocked = false
    this._rec = null
    this._recCtx = null
    this._recChunks = []
    this._local = null                // local audio handed to JsSIP; ours to release
  }

  emit(type, data) { try { this.onEvent(type, data) } catch {} }

  // Point the class at the React-owned <audio> element. Called from the component's ref effect.
  setAudioEl(el) { if (el) this.remoteAudio = el }

  ensureAudio() {
    // Fallback only: if no element was injected (shouldn't happen in the React app), create a
    // hidden, DOM-attached one so audio can still render.
    if (!this.remoteAudio) {
      const el = new Audio()
      el.autoplay = true
      el.setAttribute('playsinline', '')
      el.style.display = 'none'
      try { document.body.appendChild(el) } catch {}
      this.remoteAudio = el
    }
    return this.remoteAudio
  }

  // Prime the sink INSIDE a user gesture (Call / Answer / Connect click). Playing the element
  // (even empty) while the page has transient activation marks it user-activated; every later
  // play() on this SAME element then resolves, defeating the autoplay policy. Must be called
  // synchronously from the click handler — do not await anything before it.
  unlockAudio() {
    if (this._unlocked) return
    const el = this.ensureAudio()
    try {
      el.muted = true
      const p = el.play()
      if (p && p.then) p.then(() => { try { el.pause(); el.currentTime = 0 } catch {} el.muted = false })
                        .catch(() => { el.muted = false })
      else { try { el.pause() } catch {}; el.muted = false }
      this._unlocked = true
    } catch { el.muted = false }
  }

  // Attach a remote MediaStream to the audio element and force playback. The element was
  // primed by unlockAudio() on the click, so play() should resolve; we keep the catch as
  // telemetry + arm a one-time gesture retry as a last resort.
  attachRemote(stream) {
    if (!stream) return
    const el = this.ensureAudio()
    if (el.srcObject !== stream) el.srcObject = stream
    el.muted = false
    el.volume = 1
    const p = el.play()
    if (p && p.catch) p.catch((err) => {
      this.emit('audioblocked', (err && err.name) || 'play-failed')
      const resume = () => { el.play().finally(() => {
        document.removeEventListener('click', resume, true)
        document.removeEventListener('touchend', resume, true)
      }) }
      document.addEventListener('click', resume, true)
      document.addEventListener('touchend', resume, true)
    })
  }

  // prov: { username, password, ws_port, host, realm }
  start(prov, host) {
    if (this.ua) this.stop()
    const wsUrl = `wss://${host}:${prov.ws_port}/ws`
    const socket = new JsSIP.WebSocketInterface(wsUrl)
    const domain = prov.domain || host
    this.ua = new JsSIP.UA({
      sockets: [socket],
      uri: `sip:${prov.username}@${domain}`,
      password: prov.password,
      register: true,
      session_timers: false,
      contact_uri: `sip:${prov.username}@${domain};transport=wss`,
    })
    this.ua.on('connected', () => this.emit('ws', 'connected'))
    // Only the 'disconnected' event is gated on _dead: ua.stop() (called when the user switches
    // lines) fires 'disconnected' ASYNCHRONOUSLY ~1s later, and without this guard that late event
    // from the torn-down UA would flip the already-registered NEW line's status to red. All other
    // events (incoming, registered, …) must always pass — gating them broke the incoming-call
    // Answer/Decline overlay.
    this.ua.on('disconnected', () => { if (!this._dead) this.emit('ws', 'disconnected') })
    this.ua.on('registered', () => this.emit('registered', true))
    this.ua.on('unregistered', () => this.emit('registered', false))
    this.ua.on('registrationFailed', (e) => this.emit('regfail', (e && e.cause) || 'failed'))
    this.ua.on('newRTCSession', (e) => this.handleSession(e))
    this.ua.start()
  }

  handleSession(e) {
    const session = e.session
    // If already in a call, reject any second incoming session as busy.
    if (this.session && this.session !== session) {
      if (session.direction === 'incoming') { try { session.terminate({ status_code: 486 }) } catch {} }
      return
    }
    this.session = session
    // Idempotency guard: an outgoing call reaches here twice (once from call(), once from
    // the UA's 'newRTCSession' for the same session). Binding listeners twice would double
    // -fire events; bind exactly once per session.
    if (session.__vowifiBound) return
    session.__vowifiBound = true
    // getUserMedia is JsSIP's first step on BOTH an outgoing call() and an answer(), and it
    // fires the session's 'failed' BEFORE this event — so 'failed' on its own can never say
    // why a call died in milliseconds. Pass the DOMException name up so the UI can name it.
    session.on('getusermediafailed', (err) => this.emit('mediafail', (err && err.name) || 'MediaError'))
    const dir = session.direction  // 'incoming' | 'outgoing'
    if (dir === 'incoming') {
      const from = (session.remote_identity && session.remote_identity.uri && session.remote_identity.uri.user) || 'Unknown'
      this.emit('incoming', { from })
    }
    // A carrier may return an announcement as early media (183 with SDP) and never answer the
    // call — for example for balance, barring, or routing failures. Attach the receiver here as
    // well as on accepted/confirmed so that announcement is audible instead of the UI showing a
    // silent "ringing" state. Retry briefly because some browsers expose the receiver just after
    // JsSIP emits progress while applying the remote SDP.
    session.on('progress', () => {
      this.emit('progress')
      this.attachFromSession(session)
      setTimeout(() => this.attachFromSession(session), 100)
      setTimeout(() => this.attachFromSession(session), 400)
    })
    session.on('accepted', () => { this.emit('active'); this.attachFromSession(session) })
    session.on('confirmed', () => { this.emit('active'); this.attachFromSession(session) })
    // 'ended' (BYE received/sent) and 'failed' (setup error / non-2xx) are the terminal
    // events. Always null the session and tell the view so the UI resets to idle even if
    // only one of them fires.
    session.on('ended', (d) => { if (this.session === session) this.session = null; this._releaseLocal(); this.emit('ended', { cause: d && d.cause }) })
    session.on('failed', (d) => { if (this.session === session) this.session = null; this._releaseLocal(); this.emit('failed', { cause: d && d.cause }) })
    session.on('peerconnection', (ev) => {
      const pc = ev.peerconnection
      // ontrack fires as the remote audio track arrives. te.streams[0] is the usual source,
      // but some stacks deliver a track with no stream — fall back to wrapping the track.
      pc.ontrack = (te) => {
        const stream = (te.streams && te.streams[0]) || new MediaStream([te.track])
        this.attachRemote(stream)
      }
      // Belt-and-suspenders: if a remote track is already present (ontrack raced/missed),
      // build a stream from the receivers so audio still renders.
      const grab = () => {
        try {
          const tracks = pc.getReceivers().map((r) => r.track).filter((t) => t && t.kind === 'audio')
          if (tracks.length) this.attachRemote(new MediaStream(tracks))
        } catch {}
      }
      pc.addEventListener && pc.addEventListener('connectionstatechange', () => {
        if (pc.connectionState === 'connected') grab()
      })
    })
  }

  // Most reliable remote-audio path: once the call is accepted/confirmed, read the remote
  // audio track straight off the session's RTCPeerConnection receivers and play it. This does
  // not depend on the 'peerconnection'/ontrack event having fired in time (the observed
  // failure was hasStream:false — ontrack never attached), and the server has confirmed RTP
  // is flowing, so a receiver track is present here.
  attachFromSession(session) {
    try {
      const pc = session && session.connection
      if (!pc) return
      const tracks = pc.getReceivers().map((r) => r.track).filter((t) => t && t.kind === 'audio')
      if (tracks.length) this.attachRemote(new MediaStream(tracks))
    } catch {}
  }

  // Open the local audio for a call. JsSIP would do this itself, but only by calling
  // getUserMedia and killing the whole session if it rejects — which is how a PC with no
  // microphone lost the call ~10ms after the click. Take it over: hand JsSIP a real stream
  // when there is one, and silence when there is not, so the call is placed either way and
  // the UI can say which of the two it got.
  async _acquireLocal() {
    this._releaseLocal()              // never leave a previous call's track open
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      this._local = { stream, silent: false }
    } catch (err) {
      // navigator.mediaDevices is absent on an insecure origin, so the failure there is a
      // TypeError from the call itself rather than a DOMException that names a device fault.
      const reason = !navigator.mediaDevices ? 'insecure' : (err && err.name) || 'MediaError'
      const silent = silentAudioStream()
      this._local = { ...(silent || { stream: null }), silent: true, reason }
    }
    return this._local
  }

  // JsSIP only stops tracks it generated itself, so a stream we passed in stays live (and the
  // browser keeps showing the recording indicator) unless we release it here.
  _releaseLocal() {
    const local = this._local
    this._local = null
    if (!local) return
    try { local.stream?.getTracks().forEach((track) => track.stop()) } catch {}
    try { local.osc?.stop() } catch {}
    try { local.ctx?.close() } catch {}
  }

  async call(number) {
    if (!this.ua) return
    const domain = this.ua.configuration.uri.host
    this.emit('calling', { to: number })
    const local = await this._acquireLocal()
    // stop() can land while getUserMedia is still deciding (the user switched lines, or the
    // page navigated away). Do not raise a call on a torn-down UA.
    if (this._dead || !this.ua) { this._releaseLocal(); return }
    if (local.silent) this.emit('mediafallback', local.reason)
    const opts = {
      mediaConstraints: { audio: true, video: false },
      mediaStream: local.stream || undefined,
      pcConfig: { rtcpMuxPolicy: 'require', iceServers: [] },
    }
    // '#' is not a legal SIP URI user character (RFC 3261 25.1), and JsSIP rejects the whole
    // URI rather than escaping it, so service codes like #225# would never leave the browser.
    // Asterisk percent-decodes the user part before dialplan matching, so EXTEN is unchanged.
    const user = String(number).replace(/#/g, '%23')
    try {
      this.session = this.ua.call(`sip:${user}@${domain}`, opts)
      this.handleSession({ session: this.session })
    } catch (err) {
      // ua.call() can throw synchronously (bad target, no media, etc.) before any session
      // event fires — surface it as a terminal 'failed' so the UI doesn't hang on "calling".
      this.session = null
      this._releaseLocal()
      this.emit('failed', { cause: (err && err.message) || 'Call failed' })
    }
  }

  // Answering runs through the same media path as dialling, so an incoming call is answerable
  // without a microphone too — the caller is heard, and the UI says they cannot hear back.
  async answer() {
    const session = this.session
    if (!session) return
    const local = await this._acquireLocal()
    if (this._dead || this.session !== session) { this._releaseLocal(); return }
    if (local.silent) this.emit('mediafallback', local.reason)
    try {
      session.answer({ mediaConstraints: { audio: true, video: false },
                       mediaStream: local.stream || undefined, pcConfig: { iceServers: [] } })
    } catch (err) {
      // answer() throws synchronously on a session that is no longer answerable. Awaiting the
      // media above means that throw would otherwise surface as an unhandled rejection and
      // leave the overlay ringing at a call that is already gone.
      this.session = null
      this._releaseLocal()
      this.emit('failed', { cause: (err && err.message) || 'Answer failed' })
    }
  }

  hangup() {
    const s = this.session
    if (s) {
      this.session = null
      try { s.terminate() } catch {}
    }
    this._releaseLocal()
  }

  // Reject an un-answered INCOMING call. JsSIP's bare terminate() on a ringing incoming
  // session sends 480 Temporarily Unavailable, which Asterisk's Dial maps to NOANSWER →
  // the call is logged as "missed". Sending 603 Decline makes the disposition "rejected"
  // (declined) as the user intended. Falls back to hangup() for an outgoing/active session.
  reject() {
    const s = this.session
    if (!s) return
    if (s.direction === 'incoming' && !s.isEstablished?.()) {
      this.session = null
      try { s.terminate({ status_code: 603, reason_phrase: 'Decline' }) } catch { try { s.terminate() } catch {} }
    } else {
      this.hangup()
    }
  }

  // A second line can ring while another global browser call is already active. Tell the
  // caller this browser is busy without disturbing the first session or recording a voicemail
  // as though nobody was present.
  rejectBusy() {
    const s = this.session
    if (!s || s.direction !== 'incoming' || s.isEstablished?.()) return
    this.session = null
    try { s.terminate({ status_code: 486, reason_phrase: 'Busy Here' }) } catch { try { s.terminate() } catch {} }
  }

  sendDTMF(tone) { if (this.session) try { this.session.sendDTMF(tone) } catch {} }

  setMuted(muted) {
    if (!this.session) return
    try { muted ? this.session.mute({ audio: true }) : this.session.unmute({ audio: true }) } catch {}
  }

  // ---- call recording: mix local mic + remote audio and record to a downloadable blob ----
  async startRecording() {
    if (!this.session || !this.session.connection || this._rec) return false
    const pc = this.session.connection
    const Ctx = window.AudioContext || window.webkitAudioContext
    const ctx = new Ctx()
    const dest = ctx.createMediaStreamDestination()
    const local = pc.getSenders().map((s) => s.track).filter((t) => t && t.kind === 'audio')
    const remote = pc.getReceivers().map((r) => r.track).filter((t) => t && t.kind === 'audio')
    if (local.length) try { ctx.createMediaStreamSource(new MediaStream(local)).connect(dest) } catch {}
    if (remote.length) try { ctx.createMediaStreamSource(new MediaStream(remote)).connect(dest) } catch {}
    this._recChunks = []
    try {
      this._rec = new MediaRecorder(dest.stream)
    } catch { try { ctx.close() } catch {}; return false }
    this._recCtx = ctx
    this._rec.ondataavailable = (ev) => { if (ev.data && ev.data.size) this._recChunks.push(ev.data) }
    this._rec.start()
    return true
  }

  stopRecording() {
    return new Promise((resolve) => {
      if (!this._rec) { resolve(null); return }
      this._rec.onstop = () => {
        const blob = new Blob(this._recChunks, { type: this._rec ? this._rec.mimeType : 'audio/webm' })
        try { this._recCtx.close() } catch {}
        this._rec = null; this._recCtx = null; this._recChunks = []
        resolve(blob)
      }
      try { this._rec.stop() } catch { resolve(null) }
    })
  }

  get recording() { return !!this._rec }

  stop() {
    // Mark dead FIRST so any late JsSIP event from ua.stop() (async 'disconnected'/'unregistered')
    // is swallowed by emit() and cannot clobber a newly-started line's state.
    this._dead = true
    this.hangup()                     // releases the local track on its way through
    if (this._rec) { try { this._rec.stop() } catch {}; this._rec = null }
    if (this.ua) { try { this.ua.stop() } catch {} this.ua = null }
    if (this.remoteAudio) {
      try { this.remoteAudio.srcObject = null; this.remoteAudio.remove() } catch {}
      this.remoteAudio = null
    }
  }
}
