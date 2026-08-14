import React, { useCallback, useEffect, useRef, useState } from 'react'
import { api, connectWs, setCsrf } from './api.js'
import Softphone from './views/Softphone.jsx'
import Messages from './views/Messages.jsx'
import Esim from './views/Esim.jsx'
import { UnifiedOverview, DevicesPage, EgressPage, NotificationsPage, SystemPage, DiagnosticsPage } from './views/UnifiedPages.jsx'
import { useI18n } from './i18n.jsx'

const NAV = [
  ['overview', 'Overview', '⌂'], ['devices', 'Devices', '▣'], ['calls', 'Calls', '☎'],
  ['messages', 'Messages', '✉'], ['esim', 'eSIM', '◎'], ['egress', 'Network exits', '⇄'],
  ['notifications', 'Notifications', '◉'], ['settings', 'System settings', '⚙'], ['diagnostics', 'Diagnostics', '≣'],
]

// Each page is addressable as #/<key>, so a refresh (or a bookmark) lands on the same page
// instead of falling back to the overview. An unknown hash means the overview.
const viewFromHash = () => {
  const key = window.location.hash.replace(/^#\/?/, '')
  return NAV.some(([k]) => k === key) ? key : 'overview'
}

// GitHub's own abbreviation, so the console reads the same as the repository page: exact
// below a thousand, one decimal above it, and the decimal dropped once it stops adding
// precision (1000 -> 1k, 1250 -> 1.3k, 13300 -> 13.3k).
function starCount(value) {
  // An unreadable count is absent, not zero: Number(null) is 0, and rendering that would
  // claim the repository has no stars whenever GitHub could not be reached.
  if (value === null || value === undefined || value === '') return ''
  const count = Number(value)
  if (!Number.isFinite(count) || count < 0) return ''
  if (count < 1000) return String(count)
  const thousands = count / 1000
  return `${thousands >= 100 ? Math.round(thousands) : Number(thousands.toFixed(1))}k`
}

function lineCapabilityState(status, desired = true) {
  const state = String(status?.state || '').toUpperCase()
  if (state === 'OK') return 'on'
  if (state === 'STOPPED') return desired ? 'degraded' : 'off'
  if (['ERROR', 'NO_CARD', 'PIN_PROBLEM'].includes(state)) return 'error'
  return desired ? 'starting' : 'off'
}

function mergeLiveLineStatus(device, status) {
  const currentCapability = device.capabilities?.vowifi || {}
  const isDraft = device.provisioning?.state === 'draft'
  // A draft has two simultaneously true backend facts: its engine is stopped and automatic
  // setup is waiting for required SIM/hardware fields.  The periodic device snapshot exposes
  // the useful provisioning explanation, while a generic live STOPPED event only describes
  // the engine.  Preserve the draft explanation so those two feeds cannot make the card text
  // alternate every few seconds.
  const actual = isDraft
    ? (currentCapability.actual || 'off')
    : lineCapabilityState(status, currentCapability.desired !== false)
  const reason = isDraft
    ? (currentCapability.reason || 'Automatic setup is waiting for SIM or hardware information')
    : (status.reason || '')
  return {
    ...device,
    status,
    vowifi: {
      ...(device.vowifi || {}),
      epdg: status.detail || {},
      ims: isDraft ? (device.vowifi?.ims || '') : (status.label || ''),
    },
    capabilities: {
      ...(device.capabilities || {}),
      vowifi: { ...currentCapability, actual, reason },
    },
  }
}

function legacyDevices(instances, cards) {
  const used = new Set()
  const fromInstances = instances.map((inst, i) => {
    const reader = inst.reader || inst.reader_name || inst.config?.reader
    if (reader) used.add(reader)
    const state = String(inst.status?.state || '').toUpperCase()
    const running = ['OK', 'WORKING', 'REGISTERED'].includes(state) || inst.status?.label === 'Working'
    return {
      id: inst.device_id || inst.id,
      name: inst.name || inst.id,
      reader,
      model: inst.modem_name || inst.modem,
      sim: { name: inst.carrier || inst.name, number: inst.number || inst.msisdn },
      status: inst.status,
      compatibilityOnly: true,
      capabilities: {
        cellular: { desired: false, actual: 'unsupported', reason: 'Unified cellular status has not been exposed by this backend.' },
        vowifi: { desired: running, actual: running ? 'on' : (state === 'ERROR' ? 'error' : 'off'), reason: inst.status?.reason || '' },
      },
    }
  })
  const readers = cards.filter(c => !used.has(c.reader || c.name)).map((c, i) => ({
    id: `reader:${c.reader || c.name || i}`, name: c.modem_name || c.reader || c.name || `Reader ${i + 1}`,
    reader: c.reader || c.name, present: c.present !== false, sim: { name: c.carrier || 'SIM' }, compatibilityOnly: true,
    capabilities: { cellular: { desired: false, actual: 'unsupported' }, vowifi: { desired: false, actual: 'off' } },
  }))
  return [...fromInstances, ...readers]
}

export default function App() {
  const { t } = useI18n()
  const [view, setView] = useState(viewFromHash); const [menuOpen, setMenuOpen] = useState(false)
  const [instances, setInstances] = useState([]); const [cards, setCards] = useState([]); const [devices, setDevices] = useState([])
  // Sessions live in memory, so signing in normally happens seconds after the control plane
  // restarted — while its first card scan is still running. Until that scan has answered,
  // an empty list means "not known yet", not "no devices".
  const [discovering, setDiscovering] = useState(true)
  const [selected, setSelected] = useState(null); const [toast, setToast] = useState(null)
  const [selectedDeviceId, setSelectedDeviceId] = useState(null)
  const [theme, setTheme] = useState(() => localStorage.getItem('theme') || 'auto')
  const [systemMeta, setSystemMeta] = useState({ version: '', repository_url: '' })
  const [updateOpen, setUpdateOpen] = useState(false)
  const [authState, setAuthState] = useState(null)
  const wsEvents = useRef({ handlers: new Set() }); const toastTimer = useRef(null); const unifiedAvailable = useRef(false)
  const refreshInFlight = useRef(false)

  useEffect(() => { document.documentElement.dataset.theme = theme; localStorage.setItem('theme', theme) }, [theme])
  // Keep the address bar on the current page without growing history, and follow the hash
  // when the user edits it or navigates back/forward (replaceState never fires hashchange,
  // so the two effects cannot feed each other).
  useEffect(() => {
    const wanted = `#/${view}`
    if (window.location.hash !== wanted) window.history.replaceState(null, '', wanted)
  }, [view])
  useEffect(() => {
    const onHash = () => setView(viewFromHash())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])
  const showToast = useCallback((message) => { clearTimeout(toastTimer.current); setToast({ message, id: Date.now() }); toastTimer.current=setTimeout(()=>setToast(null),5000) }, [])
  const openUpdateDialog=useCallback(update=>{setSystemMeta(s=>({...s,update}));setUpdateOpen(true)},[])
  const expireAuth=useCallback(()=>{
    setCsrf('')
    setAuthState(s=>({...s,configured:true,authenticated:false,csrf:''}))
  },[])

  const refresh = useCallback(async () => {
    if (refreshInFlight.current) return
    refreshInFlight.current = true
    try {
      const [instancesResult, cardsResult, devicesResult] = await Promise.allSettled([
        api.instances(), api.cards(), api.devices(),
      ])
      const nextInstances = instancesResult.status === 'fulfilled' ? instancesResult.value.instances || [] : null
      const nextCards = cardsResult.status === 'fulfilled' ? cardsResult.value.cards || [] : null
      if (nextInstances) {
        setInstances(nextInstances)
        // Selection is view context, not a global default. In particular, opening an offline
        // device must never silently put the first unrelated saved SIM into its edit/delete
        // form. Calls and Messages select their first live line in SimSelector instead.
        setSelected(s => s && nextInstances.some(item => String(item.id) === String(s)) ? s : null)
      }
      if (nextCards) setCards(nextCards)
      if (devicesResult.status === 'fulfilled') {
        const r=devicesResult.value; const list=Array.isArray(r)?r:(r.devices||[])
        unifiedAvailable.current=true; setDevices(list); setDiscovering(!!r.discovering)
      // Compatibility mode is only for an older backend that does not implement the unified
      // endpoint. A transient network failure must not turn every saved line and reader into
      // a temporary "device" until the next poll succeeds.
      } else if (devicesResult.reason?.status === 404 && nextInstances && nextCards) {
        unifiedAvailable.current=false; setDevices(legacyDevices(nextInstances,nextCards)); setDiscovering(false)
      }
    } finally {
      refreshInFlight.current = false
    }
  }, [])
  useEffect(()=>{
    window.addEventListener('mdd-auth-expired',expireAuth)
    return()=>window.removeEventListener('mdd-auth-expired',expireAuth)
  },[expireAuth])
  useEffect(()=>{ api.authStatus().then(s=>{ setCsrf(s.csrf); setAuthState(s) }).catch(()=>setAuthState({configured:true,authenticated:false})) },[])
  useEffect(()=>{ if(authState?.authenticated) refresh() },[authState?.authenticated]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(()=>{ if(!authState?.authenticated)return;
    const load=()=>api.systemStatus().then(setSystemMeta).catch(()=>{})
    load(); const timer=setInterval(load,60*1000); return()=>clearInterval(timer) },[authState?.authenticated])
  useEffect(()=>{if(!authState?.authenticated)return;const check=()=>api.checkUpdate().then(update=>setSystemMeta(s=>({...s,update}))).catch(()=>{});check();const timer=setInterval(check,6*60*60*1000);return()=>clearInterval(timer)},[authState?.authenticated])
  // The self-update restarts the control plane, which drops the session mid-update — surface
  // the final outcome on the next sign-in instead.
  useEffect(()=>{if(!authState?.authenticated)return;api.updateProgress().then(s=>{
    const age=Date.now()/1000-(s.updated_at||0)
    if(s.state==='failed'&&age<1800)showToast(t('Update failed: {error}',{error:String(s.error||'').split('\n')[0].slice(0,160)}))
    else if(s.state==='success'&&age<900)showToast(t('Updated to v{version}',{version:s.target||''}))
  }).catch(()=>{})},[authState?.authenticated,showToast,t])
  useEffect(()=>{ if(!authState?.authenticated)return; const timer=setInterval(refresh,10000); return()=>clearInterval(timer) },[refresh,authState?.authenticated])

  useEffect(()=>{ if(!authState?.authenticated)return; return connectWs(msg=>{
    if(msg.type==='status'){
      const status=Object.fromEntries(Object.entries(msg).filter(([k])=>!['type','instance'].includes(k)))
      setInstances(list=>list.map(i=>String(i.id)===String(msg.instance)?{...i,status}:i))
      setDevices(list=>list.map(d=>String(d.instance_id)===String(msg.instance)
        ? mergeLiveLineStatus(d, status) : d))
    }
    // The card scan is what makes readers (and their lines) appear. Rebuild the device list
    // from it immediately instead of leaving the page empty until the next 10s poll.
    if(msg.type==='cards'){setCards(msg.cards||[]);refresh()}
    if(msg.type==='engine'&&['card_removed','reader_lost','reader_added','reader_removed'].includes(msg.event)){
      const name=msg.args?.[0]
      showToast({card_removed:t('SIM removed — line stopped'),reader_lost:t('Reader unplugged — line stopped'),reader_added:`${t('Card reader connected')}${name?`: ${name}`:''}`,reader_removed:`${t('Card reader disconnected')}${name?`: ${name}`:''}`}[msg.event])
    }
    if(['device','capability','cellular','engine'].includes(msg.type)) refresh()
    wsEvents.current.handlers.forEach(h=>h(msg))
    if(msg.type==='sms'&&msg.message?.direction==='in')showToast(t('SMS from {peer}',{peer:msg.message.peer}))
    if(msg.type==='call'&&msg.call?.direction==='in')showToast(t('Incoming call from {peer}',{peer:msg.call.peer}))
  },expireAuth)},[refresh,showToast,t,authState?.authenticated,expireAuth])
  const subscribe=useCallback(h=>{wsEvents.current.handlers.add(h);return()=>wsEvents.current.handlers.delete(h)},[])
  if (!authState) return <div className="auth-shell"><div className="auth-card"><h1>MDD Sim Gateway</h1><p>{t('Loading…')}</p></div></div>
  if (!authState.authenticated) return <AuthScreen configured={authState.configured} accountUsername={authState.username} t={t} onDone={result=>{setCsrf(result.csrf);setAuthState(s=>({...s,configured:true,authenticated:true,csrf:result.csrf}))}} />
  const sel=instances.find(i=>i.id===selected)
  const common={devices,discovering,refreshDevices:refresh,instances,cards,selected:sel,setSelected,refresh,subscribe,showToast,setView,selectedDeviceId,setSelectedDeviceId,openUpdateDialog,setSystemMeta}
  const content={
    overview:<UnifiedOverview {...common}/>, devices:<DevicesPage {...common}/>, calls:<Softphone {...common}/>,
    messages:<Messages {...common}/>, esim:<Esim {...common}/>, egress:<EgressPage {...common}/>,
    notifications:<NotificationsPage {...common}/>, settings:<SystemPage {...common}/>, diagnostics:<DiagnosticsPage {...common}/>,
  }[view]
  const issueUrl = `${(systemMeta.repository_url || 'https://github.com/MddIdd/mdd-sim-gateway').replace(/\/$/, '')}/issues/new/choose`
  return <div className="u-shell">
    <aside className={`u-sidebar ${menuOpen?'open':''}`}>
      <div className="u-brand"><img src="/logo.svg" alt="" /><div>MDD Sim Gateway<small>{t('4G + VoWiFi unified')}</small></div></div>
      <nav>{NAV.map(([key,label,icon])=><button key={key} className={view===key?'active':''} onClick={()=>{setView(key);setMenuOpen(false)}}><span>{icon}</span>{t(label)}{key==='diagnostics'&&!!systemMeta.host_alerts?.length&&<i className={`u-nav-dot ${systemMeta.host_alerts.some(a=>a.severity==='critical')?'critical':'warning'}`} title={t('The gateway host needs attention')}/>}</button>)}</nav>
      <div className="u-sidebar-foot"><div className="u-theme">{[['auto','◐'],['light','☀'],['dark','☾']].map(([k,x])=><button key={k} className={theme===k?'active':''} onClick={()=>setTheme(k)} title={t(k)}>{x}</button>)}</div><small>{discovering&&!devices.length?t('Detecting devices…'):`${devices.length} ${t(devices.length === 1 ? 'device' : 'devices')}`}</small><a className="u-feedback-link" href={issueUrl} target="_blank" rel="noreferrer"><span>◉</span>{t('Issues and suggestions')}<b>↗</b></a><div className="u-project-meta">{systemMeta.update?.update_available&&systemMeta.update?.release_url?<a className="u-version has-update" href={systemMeta.update.release_url} onClick={e=>{e.preventDefault();setUpdateOpen(true)}} title={t('New version available: v{version}',{version:systemMeta.update.latest})}><i />v{systemMeta.version}</a>:<span className="u-version">{systemMeta.version ? `v${systemMeta.version}` : '—'}</span>}<span className="u-repo-actions">{systemMeta.repository_url&&<><a href={systemMeta.repository_url} target="_blank" rel="noreferrer" aria-label="GitHub" title="GitHub"><svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M12 .7a11.5 11.5 0 0 0-3.64 22.4c.58.1.79-.25.79-.56v-2.23c-3.22.7-3.9-1.37-3.9-1.37-.52-1.34-1.29-1.69-1.29-1.69-1.05-.72.08-.71.08-.71 1.17.08 1.78 1.2 1.78 1.2 1.04 1.78 2.72 1.27 3.38.97.1-.75.4-1.27.74-1.56-2.57-.29-5.27-1.29-5.27-5.69 0-1.26.45-2.29 1.19-3.1-.12-.29-.52-1.47.11-3.06 0 0 .97-.31 3.16 1.18a10.9 10.9 0 0 1 5.75 0c2.19-1.49 3.16-1.18 3.16-1.18.63 1.59.23 2.77.11 3.06.74.81 1.19 1.84 1.19 3.1 0 4.42-2.71 5.39-5.29 5.68.42.36.79 1.07.79 2.16v3.2c0 .31.21.67.8.56A11.5 11.5 0 0 0 12 .7Z"/></svg></a><a className="u-star-link" href={systemMeta.repository_url} target="_blank" rel="noreferrer" aria-label={t('Star on GitHub')} title={t('Star on GitHub')}><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m12 2.7 2.75 5.58 6.16.9-4.46 4.34 1.05 6.13L12 16.76l-5.5 2.89 1.05-6.13-4.46-4.34 6.16-.9L12 2.7Z" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round"/></svg>{!!starCount(systemMeta.update?.stars)&&<b>{starCount(systemMeta.update.stars)}</b>}</a></>}</span></div><button className="btn btn-ghost" onClick={async()=>{try{await api.authLogout()}finally{setCsrf('');setAuthState(s=>({...s,configured:true,authenticated:false,csrf:''}))}}}>{t('Sign out')}</button></div>
    </aside>
    <button className="u-menu" onClick={()=>setMenuOpen(!menuOpen)}>☰</button>
    {menuOpen&&<button className="u-scrim" aria-label={t('Close menu')} onClick={()=>setMenuOpen(false)}/>}
    <main className="u-main"><header><div><h1>{t(NAV.find(x=>x[0]===view)?.[1]||view)}</h1><p>{t(`page.${view}.subtitle`)}</p></div><div className="u-live"><span className="u-dot" />{unifiedAvailable.current?t('Live device control'):t('Compatibility view')}</div></header><div className="u-content"><div className="u-note" role="note">{t('Responsible use notice')}</div>{content}</div></main>
    {toast&&<div className="u-toast" key={toast.id} role="status">{toast.message}</div>}
    {updateOpen&&systemMeta.update?.update_available&&<UpdateModal update={systemMeta.update} current={systemMeta.version} t={t} onClose={()=>setUpdateOpen(false)}/>}
  </div>
}

const UPDATE_PHASES = {
  requested: 'Contacting the host…', launching: 'Contacting the host…',
  downloading: 'Downloading the new release…', verifying: 'Verifying the package…',
  backup: 'Backing up the current version…', applying: 'Applying files…',
  reloading: 'Rebuilding and restarting services…',
}

function UpdateModal({ update, current, t, onClose }) {
  const [mode, setMode] = useState('confirm') // confirm | working | restarting | failed
  const [phase, setPhase] = useState('requested')
  const [error, setError] = useState('')
  // Polling starts only after POST /update/apply has reset the status file, otherwise the
  // first poll can read a stale success/failure left over from a previous update run.
  const [polling, setPolling] = useState(false)
  const primaryAction = useRef(null)
  const canClose = mode === 'confirm' || mode === 'failed'
  useEffect(() => { if (mode === 'confirm') primaryAction.current?.focus() }, [mode])
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape' && canClose) onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose, canClose])
  useEffect(() => { // reopening while an update runs resumes the progress view
    api.updateProgress().then(s => { if (s.state === 'running') { setPhase(s.phase || 'requested'); setMode('working'); setPolling(true) } }).catch(() => {})
  }, [])
  useEffect(() => {
    if (mode !== 'working' || !polling) return
    let stop = false, lastPhase = 'requested'
    const tick = async () => {
      if (stop) return
      try {
        const s = await api.updateProgress()
        if (stop) return
        lastPhase = s.phase || lastPhase
        setPhase(lastPhase)
        if (s.state === 'failed') { setError(s.error || ''); setMode('failed'); return }
        if (s.state === 'stalled') { setError(t('The host orchestrator has not picked up the update request. Check the mdd-sim-gateway-orchestrator service on the host.')); setMode('failed'); return }
        if (s.state === 'success') { window.location.reload(); return }
      } catch (err) {
        if (stop) return
        // The gateway restarts near the end of the update: the API drops, then answers 401
        // once the new control plane is up (sessions are in-memory). Anything else is a blip.
        if (err?.status === 401 || lastPhase === 'reloading') { setMode('restarting'); return }
      }
      setTimeout(tick, 3000)
    }
    tick()
    return () => { stop = true }
  }, [mode, polling, t])
  useEffect(() => {
    if (mode !== 'restarting') return
    let stop = false
    const tick = () => api.authStatus().then(() => { if (!stop) window.location.reload() }).catch(() => { if (!stop) setTimeout(tick, 3000) })
    const timer = setTimeout(tick, 3000)
    return () => { stop = true; clearTimeout(timer) }
  }, [mode])
  const begin = async () => {
    setError(''); setPhase('requested'); setPolling(false); setMode('working')
    try {
      const result = await api.applyUpdate()
      if (result?.ok === false && result?.error_code !== 'update.error.in_progress') {
        setError(result.error || result.error_code || ''); setMode('failed'); return
      }
      setPolling(true)
    } catch (err) { setError(err.message); setMode('failed') }
  }
  const mute = { fontSize: 12, color: 'var(--text-mute)' }
  return (
    <div className="u-modal-backdrop" onClick={canClose ? onClose : undefined}>
      <div className="card u-update-modal" role="dialog" aria-modal="true" aria-labelledby="update-dialog-title" onClick={(e) => e.stopPropagation()}>
        {mode === 'confirm' && <>
          <div id="update-dialog-title" style={{ fontWeight: 700, fontSize: 16, marginBottom: 6 }}>{t('New version available: v{version}', { version: update.latest })}</div>
          <div style={{ ...mute, marginBottom: 12 }}>v{current} → v{update.latest}</div>
          {update.notes && <>
            <div style={{ ...mute, marginBottom: 4 }}>{t('Release notes')}</div>
            <div style={{ maxHeight: '40vh', overflowY: 'auto', whiteSpace: 'pre-wrap', fontSize: 13, lineHeight: 1.5, border: '1px solid var(--border, #8883)', borderRadius: 8, padding: '8px 10px', marginBottom: 12 }}>{update.notes}</div>
          </>}
          <p style={{ ...mute, margin: '0 0 14px' }}>{t('The update downloads the new release on the host, rebuilds and restarts the gateway. The page reloads when it is done and you will need to sign in again.')}</p>
          <div className="u-modal-actions">
            <button className="btn btn-ghost" onClick={onClose}>{t('Cancel')}</button>
            <a className="btn btn-ghost" href={update.release_url} target="_blank" rel="noreferrer">{t('Release page')}</a>
            <button ref={primaryAction} className="btn btn-primary" onClick={begin}>{t('Update now')}</button>
          </div>
        </>}
        {(mode === 'working' || mode === 'restarting') && <>
          <div style={{ fontWeight: 700, fontSize: 16, marginBottom: 10 }}>{t('Updating to v{version}…', { version: update.latest })}</div>
          <p style={{ fontSize: 13, margin: '0 0 6px' }}>
            {mode === 'restarting' ? t('The gateway is restarting — the page will reload automatically. Sign in again afterwards.') : t(UPDATE_PHASES[phase] || UPDATE_PHASES.requested)}
          </p>
          <p style={{ ...mute, margin: 0 }}>{t('Keep the gateway powered on. This can take a few minutes.')}</p>
        </>}
        {mode === 'failed' && <>
          <div style={{ fontWeight: 700, fontSize: 16, marginBottom: 10 }}>{t('Update failed')}</div>
          {error && <div style={{ maxHeight: '30vh', overflowY: 'auto', whiteSpace: 'pre-wrap', fontSize: 12, lineHeight: 1.5, border: '1px solid var(--border, #8883)', borderRadius: 8, padding: '8px 10px', marginBottom: 12, wordBreak: 'break-all' }}>{error}</div>}
          <div className="u-modal-actions">
            <button className="btn btn-ghost" onClick={onClose}>{t('Cancel')}</button>
            <button className="btn btn-primary" onClick={begin}>{t('Retry')}</button>
          </div>
        </>}
      </div>
    </div>
  )
}

function AuthScreen({ configured, accountUsername, t, onDone }) {
  const [username,setUsername]=useState(configured ? (accountUsername || 'admin') : 'admin'); const [password,setPassword]=useState(''); const [confirm,setConfirm]=useState(''); const [error,setError]=useState(''); const [busy,setBusy]=useState(false); const [retry,setRetry]=useState(0)
  useEffect(()=>{if(!retry)return;const timer=setInterval(()=>setRetry(v=>Math.max(0,v-1)),1000);return()=>clearInterval(timer)},[retry])
  const submit=async()=>{if(busy||retry||!password)return;setError('');if(!configured&&password!==confirm){setError(t('Passwords do not match'));return}setBusy(true);try{onDone(await (configured?api.authLogin(username,password):api.authSetup(username,password)))}catch(err){if(err.status===429){const seconds=Math.max(1,Number(err.data?.retry_after)||60);setRetry(seconds);setError(t('Too many attempts. Try again in {seconds} seconds.',{seconds}))}else setError(err.message)}finally{setBusy(false)}}
  return <div className="auth-shell"><form className="auth-card" onSubmit={e=>{e.preventDefault();submit()}}><div className="auth-brand"><div className="auth-mark">M</div><h1>MDD Sim Gateway</h1></div><p>{t(configured?'Sign in to manage the gateway':'Create the administrator account')}</p><label>{t('Username')}<input value={username} onChange={e=>setUsername(e.target.value)} readOnly={configured} autoComplete="off" data-1p-ignore="true" data-lpignore="true" required /></label><label>{t('Password')}<input type="password" value={password} onChange={e=>setPassword(e.target.value)} autoComplete="off" data-1p-ignore="true" data-lpignore="true" minLength="10" required /></label>{!configured&&<label>{t('Confirm password')}<input type="password" value={confirm} onChange={e=>setConfirm(e.target.value)} autoComplete="new-password" minLength="10" required /></label>}{error&&<p className="auth-error">{retry?t('Too many attempts. Try again in {seconds} seconds.',{seconds:retry}):error}</p>}<button type="submit" className="primary" disabled={busy||retry>0||!password}>{retry?t('Try again in {seconds}s',{seconds:retry}):t(busy?'Please wait…':configured?'Sign in':'Create account')}</button>{!configured&&<small>{t('Use at least 10 characters. Reset it from the host if it is lost.')}</small>}</form></div>
}
