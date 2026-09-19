import React, { useEffect, useLayoutEffect, useState, useCallback, useRef } from 'react'
import { api } from '../api.js'
import SimSelector from './SimSelector.jsx'
import MmsSettings from './MmsSettings.jsx'
import { fitAttachments } from '../mmsImage.js'
import { useI18n } from '../i18n.jsx'

// application/smil is the MMS presentation part (layout/timing for the other parts); it is
// never itself content, so it is never rendered as an attachment.
const MMS_SMIL_TYPE = 'application/smil'
// Inbound states where the content is not yet available locally and a download/retry action
// applies (or the wait is worth explaining). 'retrieved' has full parts and needs neither.
const MMS_PENDING_STATES = new Set(['notified', 'downloading', 'failed', 'expired'])

export default function Messages({ selected, subscribe, showToast, instances, cards, devices, setSelected, initialLoading, loadErrors }) {
  const { t: tr } = useI18n()
  const id = selected?.id
  const [threads, setThreads] = useState([])
  const [threadsLoading, setThreadsLoading] = useState(false)
  const [peer, setPeer] = useState(null)
  const [msgs, setMsgs] = useState([])
  const [messagesLoading, setMessagesLoading] = useState(false)
  const [text, setText] = useState('')
  const [newTo, setNewTo] = useState('')
  const [transport, setTransport] = useState('auto')
  const [sending, setSending] = useState(false)
  const [selMode, setSelMode] = useState(false)      // multi-select messages to delete
  const [selIds, setSelIds] = useState(() => new Set())
  const [binary, setBinary] = useState([])           // filed non-text payloads (see BinaryPayloads)
  const [attachments, setAttachments] = useState([]) // [{file, url}] queued for the next MMS
  const [subject, setSubject] = useState('')
  const [mmsCfg, setMmsCfg] = useState(null)         // this line's effective MMS settings
  const [mmsBusy, setMmsBusy] = useState(() => new Set())  // message ids mid-download
  const [showMmsSettings, setShowMmsSettings] = useState(false)
  const activeId = useRef(id)
  const activePeer = useRef(peer)
  const threadsRequest = useRef(0)
  const messagesRequest = useRef(0)
  const sendingRef = useRef(false)
  const fileInputRef = useRef(null)
  const attachmentsRef = useRef(attachments)
  const listRef = useRef(null)
  const listContentRef = useRef(null)
  // Whether the message list should follow its bottom edge: true when a conversation is
  // opened and while the reader stays at (or near) the newest message, false once they
  // scroll up to read older history so an incoming message does not yank them back down.
  const stickToBottom = useRef(true)
  // The list's scrollTop at the last scroll event, to tell the reader scrolling up from
  // everything else that fires a scroll event.
  const lastScrollTop = useRef(0)
  activeId.current = id
  activePeer.current = peer
  attachmentsRef.current = attachments

  // Cellular SMS is available only when this line is currently attached to a live modem.
  // Older backends do not expose a dedicated SMS capability, so use the unified device type
  // instead; the backend still performs the authoritative ModemManager capability check.
  const selectedDevice = devices.find((device) => device.present === true
    && device.device_type === 'modem'
    && String(device.instance_id || '') === String(id || ''))
  const cellularAvailable = Boolean(selectedDevice)
  // Absent settings (not loaded yet, or an older backend without the endpoint) never block
  // attaching a file; only an explicit "off" or "unconfigured" answer does.
  const mmsDisabled = Boolean(mmsCfg && (!mmsCfg.enabled || !mmsCfg.configured))

  const loadThreads = useCallback(async (showLoading = false) => {
    if (!id) return
    const request = ++threadsRequest.current
    if (showLoading) setThreadsLoading(true)
    try {
      const r = await api.threads(id)
      if (request === threadsRequest.current && activeId.current === id) setThreads(r.threads)
    } catch {}
    finally {
      if (request === threadsRequest.current && activeId.current === id) setThreadsLoading(false)
    }
  }, [id])

  // Filed payloads never arrive through the SMS websocket event (they are deliberately not
  // broadcast), so they are fetched alongside the threads rather than pushed. A backend that
  // predates the endpoint simply yields an empty list and the panel stays hidden.
  const loadBinary = useCallback(async () => {
    if (!id) return
    try {
      const r = await api.binarySms(id)
      if (activeId.current === id) setBinary(r.payloads || [])
    } catch { if (activeId.current === id) setBinary([]) }
  }, [id])

  // The line's MMS enablement/config, used to gate the attach button and to size-plan
  // attachments client-side. Missing/erroring is treated as "unknown" (attach stays enabled)
  // rather than as "disabled", so an older backend without this endpoint never blocks MMS.
  const loadMmsCfg = useCallback(async () => {
    if (!id) return
    try {
      const r = await api.mmsSettings(id)
      if (activeId.current === id) setMmsCfg(r.effective)
    } catch { if (activeId.current === id) setMmsCfg(null) }
  }, [id])

  const clearAttachments = useCallback(() => {
    setAttachments((prev) => { prev.forEach((a) => a.url && URL.revokeObjectURL(a.url)); return [] })
  }, [])

  const addAttachments = (fileList) => {
    const files = Array.from(fileList || [])
    if (!files.length) return
    setAttachments((prev) => [...prev, ...files.map((file) => ({
      file, url: file.type.startsWith('image/') ? URL.createObjectURL(file) : null,
    }))])
  }

  // Files that can travel in an MMS; the same set the attach button's picker offers.
  const isSendableFile = (file) => /^(image|audio|video)\//.test(file.type)
    || ['text/vcard', 'text/x-vcard'].includes(file.type)

  // Clipboard images (screenshots, "copy image") all arrive named image.png or with no name
  // at all; give each a distinct, dated name so several pasted pictures stay tellable apart.
  const nameClipboardFile = (file, index) => {
    if (file.name && file.name !== 'image.png') return file
    const extension = (file.type.split('/')[1] || 'bin').split('+')[0]
    const stamp = new Date().toISOString().replace(/[-:]/g, '').replace(/\..*$/, '')
    return new File([file], `pasted-${stamp}${index ? `-${index + 1}` : ''}.${extension}`,
      { type: file.type, lastModified: file.lastModified })
  }

  // Paste or drop files anywhere in the composer. Plain text still pastes as text: only the
  // file items of the clipboard are taken, and the default is prevented only when the
  // clipboard holds nothing but files.
  const takeFiles = (files, event) => {
    const usable = files.filter(isSendableFile)
    if (!usable.length) return false
    if (sending) { event.preventDefault(); return true }
    if (mmsDisabled) {
      event.preventDefault()
      toast(tr('MMS is not configured for this line'))
      return true
    }
    addAttachments(usable.map(nameClipboardFile))
    return true
  }

  const onComposerPaste = (event) => {
    const data = event.clipboardData
    if (!data) return
    const files = Array.from(data.items || [])
      .filter((item) => item.kind === 'file')
      .map((item) => item.getAsFile())
      .filter(Boolean)
    const hasText = Array.from(data.types || []).includes('text/plain')
    if (takeFiles(files, event) && !hasText) event.preventDefault()
  }

  const onComposerDrop = (event) => {
    const files = Array.from(event.dataTransfer?.files || [])
    if (!files.length) return
    event.preventDefault()
    takeFiles(files, event)
  }

  const removeAttachment = (index) => {
    setAttachments((prev) => {
      const next = prev.slice()
      const [removed] = next.splice(index, 1)
      if (removed?.url) URL.revokeObjectURL(removed.url)
      return next
    })
  }

  // Object URLs are per-attachment, so they must be revoked individually on removal/clear
  // (above) and, for whatever is still queued, once when the component itself unmounts.
  useEffect(() => () => { attachmentsRef.current.forEach((a) => a.url && URL.revokeObjectURL(a.url)) }, [])

  const loadMsgs = useCallback(async (p, showLoading = false) => {
    if (!id || !p) return
    const request = ++messagesRequest.current
    if (showLoading) setMessagesLoading(true)
    try {
      const r = await api.messages(id, p)
      if (request === messagesRequest.current && activeId.current === id && activePeer.current === p) setMsgs(r.messages)
    } catch {}
    finally {
      if (request === messagesRequest.current && activeId.current === id && activePeer.current === p) setMessagesLoading(false)
    }
  }, [id])

  // A conversation key is only meaningful inside one line. Clear the old line's local view
  // synchronously when switching SIMs; loadThreads then fills the selected line's history.
  // Without this, an old peer can trigger an empty lookup on the new line and make its existing
  // history appear to have disappeared.
  useEffect(() => {
    ++threadsRequest.current; ++messagesRequest.current
    setThreads([]); setPeer(null); setMsgs([]); setText(''); setNewTo(''); setTransport('auto')
    setBinary([])
    clearAttachments(); setSubject(''); setMmsCfg(null)
    setThreadsLoading(Boolean(id)); setMessagesLoading(false)
    if (id) { loadThreads(true); loadBinary(); loadMmsCfg() }
  }, [id, loadThreads, loadBinary, loadMmsCfg, clearAttachments])
  useEffect(() => {
    if (!cellularAvailable && transport === 'cellular') setTransport('auto')
  }, [cellularAvailable, transport])
  useEffect(() => {
    ++messagesRequest.current
    setMsgs([])
    setMessagesLoading(Boolean(peer))
    if (peer) loadMsgs(peer, true)
  }, [peer, loadMsgs])
  // Open every conversation at its newest message, and keep following it as messages
  // arrive or MMS thumbnails finish loading (which grows the list after the first paint).
  useLayoutEffect(() => { stickToBottom.current = true; lastScrollTop.current = 0 }, [peer])
  const scrollToBottomIfStuck = useCallback(() => {
    const el = listRef.current
    if (el && stickToBottom.current) el.scrollTop = el.scrollHeight
  }, [])
  useLayoutEffect(scrollToBottomIfStuck, [msgs, messagesLoading, scrollToBottomIfStuck])
  // Messages are not the only thing that moves the bottom edge: a picture or video finishing
  // loading grows the list, and the composer growing (attachments, a wrapped line) shrinks the
  // visible area without any scroll or load event. Follow every size change of the list and of
  // its content while the reader is at the bottom.
  useLayoutEffect(() => {
    const list = listRef.current
    const content = listContentRef.current
    if (!list || !content || typeof ResizeObserver === 'undefined') return undefined
    const observer = new ResizeObserver(scrollToBottomIfStuck)
    observer.observe(list)
    observer.observe(content)
    return () => observer.disconnect()
  }, [id, scrollToBottomIfStuck])
  // Only the reader scrolling up stops the following, and reaching the bottom resumes it. A
  // scroll event is dispatched a frame after it happens, by when a picture may have grown the
  // list: judged by the distance to the bottom alone, the list's own jump to the bottom would
  // then read as the reader leaving it, and the conversation would stop half way.
  const onListScroll = (e) => {
    const el = e.currentTarget
    if (el.scrollHeight - el.scrollTop - el.clientHeight < 48) stickToBottom.current = true
    else if (el.scrollTop < lastScrollTop.current - 1) stickToBottom.current = false
    lastScrollTop.current = el.scrollTop
  }
  // leaving/refreshing a thread resets the selection UI
  useEffect(() => { setSelMode(false); setSelIds(new Set()) }, [peer])
  // if the open conversation empties (delete/clear), leave select mode so its toolbar
  // (rendered only while msgs.length>0) can't strand the UI in select state.
  useEffect(() => { if (!msgs.length) { setSelMode(false); setSelIds(new Set()) } }, [msgs.length])
  useEffect(() => subscribe((msg) => {
    if (msg.type === 'sms' && msg.instance === id) {
      loadThreads()
      loadBinary()
      if (peer) loadMsgs(peer)
    }
  }), [subscribe, id, peer, loadThreads, loadMsgs, loadBinary])

  const sendMms = async (to) => {
    const forId = id
    sendingRef.current = true
    setSending(true)
    try {
      const maxSize = mmsCfg?.max_size || 300 * 1024
      const textBytes = new TextEncoder().encode(text || '').length
      let files
      try {
        files = await fitAttachments(attachments.map((a) => a.file), textBytes, maxSize)
      } catch (e) {
        const kb = (n) => Math.ceil((n || 0) / 1024)
        showToast ? showToast(tr('The attachments are too large for an MMS ({size} KB; limit {limit} KB)',
          { size: kb(e.size), limit: kb(maxSize) })) : alert(e.message)
        return
      }
      const res = await api.sendMms(forId, { to, text, subject, files })
      // The backend may canonicalize the peer differently from what was typed: a single
      // recipient is normalized (canonical_peer), and several recipients are joined with
      // ", " — read the stored message's own peer back rather than assuming it matches `to`.
      const peerKey = res?.message?.peer || to
      if (activeId.current === forId) {
        setText(''); setSubject(''); clearAttachments(); setPeer(peerKey); setNewTo('')
        stickToBottom.current = true
        await loadThreads(); await loadMsgs(peerKey)
      }
      if (res && res.ok === false) {
        const msg = 'MMS not sent: ' + (res.error || 'unknown error')
        showToast ? showToast(msg) : alert(msg)
      }
    } catch (e) {
      const msg = 'MMS failed: ' + e.message
      showToast ? showToast(msg) : alert(msg)
    } finally {
      sendingRef.current = false
      setSending(false)
    }
  }

  const send = async () => {
    // React state is updated asynchronously, so `sending` alone leaves a short window where
    // a double click or a repeating Enter key can submit the same billable SMS twice.
    if (sendingRef.current) return
    const to = peer || newTo
    if (!to || (!text && !attachments.length)) return
    if (attachments.length) { await sendMms(to); return }
    const forId = id
    sendingRef.current = true
    setSending(true)
    try {
      const res = await api.sendSms(forId, to, text, transport)
      // A slow modem submit may finish after the operator selected another line. Never erase
      // that line's draft or replace its open conversation with the old line's recipient.
      if (activeId.current === forId) {
        setText(''); setPeer(to); setNewTo('')
        stickToBottom.current = true
        await loadThreads(); await loadMsgs(to)
      }
      if (res && res.ok === false) {
        const msg = res.uncertain
          ? tr('SMS submission timed out; delivery is unknown. Do not retry automatically.')
          : 'SMS not delivered: ' + (res.error || 'unknown error')
        showToast ? showToast(msg) : alert(msg)
      }
    } catch (e) {
      const msg = 'SMS failed: ' + e.message
      showToast ? showToast(msg) : alert(msg)
    } finally {
      sendingRef.current = false
      setSending(false)
    }
  }

  const downloadMms = async (mid) => {
    setMmsBusy((prev) => new Set(prev).add(mid))
    try {
      await api.mmsDownload(id, mid)
    } catch (e) {
      const msg = 'MMS download failed: ' + e.message
      showToast ? showToast(msg) : alert(msg)
    } finally {
      setMmsBusy((prev) => { const next = new Set(prev); next.delete(mid); return next })
    }
  }

  const toast = (m) => (showToast ? showToast(m) : null)

  const toggleSel = (mid) => setSelIds((s) => {
    const n = new Set(s); n.has(mid) ? n.delete(mid) : n.add(mid); return n
  })
  // The awaited delete may resolve after the user switched SIM lines — only refresh if
  // we're still on the same line, so we don't write the old line's data into state.
  const refreshIfSame = async (forId, p) => {
    if (forId !== id) return
    await loadThreads(); if (p) await loadMsgs(p)
  }

  const deleteSelected = async () => {
    if (!selIds.size) return
    if (!confirm(`Delete ${selIds.size} selected message${selIds.size > 1 ? 's' : ''}?`)) return
    const forId = id, p = peer
    try {
      await api.deleteMessages(forId, { ids: [...selIds] })
      setSelMode(false); setSelIds(new Set())
      await refreshIfSame(forId, p)
      toast('Messages deleted')
    } catch (e) { toast('Delete failed: ' + e.message) }
  }

  const deleteThread = async (p, e) => {
    if (e) e.stopPropagation()
    if (!confirm(`Delete the entire conversation with ${p}? This removes all its messages.`)) return
    const forId = id
    try {
      await api.deleteMessages(forId, { peer: p })
      if (peer === p) { setPeer(null); setMsgs([]) }
      if (forId === id) await loadThreads()
      toast('Conversation deleted')
    } catch (e2) { toast('Delete failed: ' + e2.message) }
  }

  const clearAll = async () => {
    if (!threads.length) return
    if (!confirm('Delete ALL messages on this line? This cannot be undone.')) return
    const forId = id
    try {
      await api.deleteMessages(forId, { all: true })
      if (forId === id) { setPeer(null); setMsgs([]); await loadThreads() }
      toast('All messages deleted')
    } catch (e) { toast('Delete failed: ' + e.message) }
  }

  if (initialLoading && !id) return <p role="status">{tr('Loading')}…</p>
  if (loadErrors?.instances && !id) return <p className="u-error">{tr('Loading failed')}</p>
  if (!id) return (
    <div>
      <SimSelector instances={instances} cards={cards} devices={devices} selected={selected} setSelected={setSelected} />
      <div style={{ color: 'var(--text-dim)' }}>{tr('Select a SIM / line to view and send messages.')}</div>
    </div>
  )

  return (
    <div style={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
      <div style={{ flexShrink: 0 }}>
        <SimSelector instances={instances} cards={cards} devices={devices} selected={selected} setSelected={setSelected} />
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: '280px 1fr', gridTemplateRows: 'minmax(0, 1fr)', gap: 16, flex: 1, minHeight: 0 }}>
      <div className="card" style={{ padding: 12, overflow: 'auto', minHeight: 0 }}>
        <button className="btn btn-primary" style={{ width: '100%', marginBottom: 8 }} onClick={() => { setPeer(null); setMsgs([]); setMessagesLoading(false) }}>+ {tr('New message')}</button>
        {threads.length > 0 &&
          <button className="btn btn-ghost" style={{ width: '100%', marginBottom: 10, color: '#ef4444', fontSize: 12 }}
            onClick={clearAll}>{tr('Clear all conversations')}</button>}
        <button className="btn btn-ghost" style={{ width: '100%', marginBottom: 10, fontSize: 12 }}
          onClick={() => setShowMmsSettings(true)}>{tr('MMS settings')}</button>
        {threads.map((t) => (
          <div key={t.peer} onClick={() => setPeer(t.peer)} className="hover-row"
            style={{ padding: 10, borderRadius: 10, cursor: 'pointer', marginBottom: 4, display: 'flex', alignItems: 'center', gap: 8,
              background: peer === t.peer ? 'var(--active)' : 'transparent' }}>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontWeight: 600, fontSize: 14 }} className="mono">{t.peer}</div>
              <div style={{ fontSize: 12, color: 'var(--text-mute)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {t.last_kind === 'mms' ? `[${tr('MMS')}]${t.last_body ? ' ' + t.last_body : ''}` : t.last_body}
              </div>
            </div>
            <button className="row-del" title="Delete conversation" aria-label={`Delete conversation with ${t.peer}`}
              onClick={(e) => deleteThread(t.peer, e)}>🗑</button>
          </div>
        ))}
        {threadsLoading && <div aria-live="polite" style={{ color: 'var(--text-mute)', fontSize: 13, padding: 8 }}>{tr('Loading conversations…')}</div>}
        {!threadsLoading && threads.length === 0 && <div style={{ color: 'var(--text-mute)', fontSize: 13, padding: 8 }}>{tr('No conversations yet.')}</div>}
        <BinaryPayloads payloads={binary} tr={tr} />
      </div>

      <div className="card" style={{ display: 'flex', flexDirection: 'column', padding: 0, minHeight: 0 }}>
        <div style={{ padding: 14, borderBottom: '1px solid var(--border)', display: 'flex', alignItems: 'center', gap: 10, flexShrink: 0 }}>
          {peer ? <span className="mono" style={{ fontWeight: 600, flex: 1 }}>{peer}</span>
            : <input placeholder={tr('Recipient number e.g. +1...')} value={newTo} onChange={(e) => setNewTo(e.target.value)} style={{ maxWidth: 300, flex: 1 }} />}
          {peer && msgs.length > 0 && (
            selMode ? (
              <>
                <span style={{ fontSize: 12, color: 'var(--text-mute)' }}>{selIds.size} {tr('selected')}</span>
                <button className="btn btn-ghost" style={{ padding: '4px 10px', fontSize: 12, color: '#ef4444' }}
                  disabled={!selIds.size} onClick={deleteSelected}>{tr('Delete')}</button>
                <button className="btn btn-ghost" style={{ padding: '4px 10px', fontSize: 12 }}
                  onClick={() => { setSelMode(false); setSelIds(new Set()) }}>{tr('Cancel')}</button>
              </>
            ) : (
              <>
                <button className="btn btn-ghost" style={{ padding: '4px 10px', fontSize: 12 }}
                  onClick={() => setSelMode(true)}>{tr('Select')}</button>
                <button className="btn btn-ghost" title="Delete conversation" style={{ padding: '4px 10px', fontSize: 12, color: '#ef4444' }}
                  onClick={() => deleteThread(peer)}>{tr('Delete all')}</button>
              </>
            )
          )}
        </div>
        {/* overflowAnchor none: the browser's scroll anchoring would otherwise move the view
            when content above grows, and that scroll would read as the reader leaving the bottom. */}
        <div ref={listRef} onScroll={onListScroll}
          style={{ flex: 1, minHeight: 0, overflow: 'auto', overflowAnchor: 'none', padding: 16 }}>
          <div ref={listContentRef} style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {messagesLoading && <div aria-live="polite" style={{ color: 'var(--text-mute)', fontSize: 13 }}>{tr('Loading messages…')}</div>}
          {!messagesLoading && peer && msgs.length === 0 && <div style={{ color: 'var(--text-mute)', fontSize: 13 }}>{tr('No messages in this conversation.')}</div>}
          {msgs.map((m) => {
            const failed = m.status === 'failed'
            // Outbound delivery lifecycle: pending -> sent (IMS accepted) -> delivered | failed.
            // 'delivered' is confirmed by the network's SMS submit report; 'sent' means accepted
            // but delivery not yet confirmed.
            const delivered = m.status === 'delivered'
            const sent = m.status === 'sent'
            const uncertain = m.status === 'unknown'
            const isMms = m.kind === 'mms'
            const mmsSending = isMms && m.direction === 'out' && m.mms?.state === 'sending'
            const statusText = failed ? ` · ${tr('Failed to deliver')}`
              : mmsSending ? ` · ${tr('Sending MMS…')}`
              : m.status === 'pending' ? ` · ${tr('sending…')}`
              : sent ? ` · ${tr('Sent')}`
              : delivered ? ` · ${tr('Delivered ✓')}`
              : uncertain ? ` · ${tr('Delivery unknown')}`
              : ''
            const statusColor = failed ? '#ef4444' : uncertain ? '#f59e0b' : delivered ? '#22c55e' : 'var(--text-mute)'
            const checked = selIds.has(m.id)
            return (
              <div key={m.id} onClick={() => selMode && toggleSel(m.id)}
                style={{ alignSelf: m.direction === 'out' ? 'flex-end' : 'flex-start', maxWidth: '74%',
                  cursor: selMode ? 'pointer' : 'default', display: 'flex', alignItems: 'center', gap: 8,
                  flexDirection: m.direction === 'out' ? 'row-reverse' : 'row' }}>
                {selMode && <input type="checkbox" readOnly checked={checked} style={{ width: 'auto', flexShrink: 0 }} />}
                <div style={{ minWidth: 0 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6,
                    flexDirection: m.direction === 'out' ? 'row' : 'row-reverse' }}>
                    {failed && <span title={m.error || 'Delivery failed'}
                      style={{ color: '#ef4444', fontWeight: 800, cursor: 'help', fontSize: 15 }}>❗</span>}
                    {uncertain && <span title={m.error || tr('Delivery unknown')}
                      style={{ color: '#f59e0b', fontWeight: 800, cursor: 'help', fontSize: 15 }}>⚠</span>}
                    <div style={{
                      background: checked ? 'var(--active)' : failed ? 'rgba(239,68,68,.15)' : uncertain ? 'rgba(245,158,11,.14)' : (m.direction === 'out' ? 'var(--primary)' : 'var(--hover)'),
                      border: failed ? '1px solid rgba(239,68,68,.55)' : uncertain ? '1px solid rgba(245,158,11,.55)' : '1px solid transparent',
                      padding: '8px 12px', borderRadius: 12, fontSize: 14,
                    }}>{isMms ? <MmsContent m={m} id={id} tr={tr} busy={mmsBusy.has(m.id)} onDownload={downloadMms} /> : m.body}</div>
                  </div>
                  <div style={{ fontSize: 10, color: statusColor,
                    textAlign: m.direction === 'out' ? 'right' : 'left', marginTop: 2 }}>
                    {new Date(m.ts * 1000).toLocaleString()}
                    {m.transport === 'cellular' ? ` · ${tr('4G SMS')}` : ''}
                    {isMms ? ` · ${tr('MMS')}` : ''}
                    {statusText}
                  </div>
                  {failed && m.error && (
                    <div style={{ fontSize: 10.5, color: '#ef4444', marginTop: 1,
                      textAlign: m.direction === 'out' ? 'right' : 'left', maxWidth: 280 }}>{m.error}</div>
                  )}
                  {uncertain && m.error && (
                    <div style={{ fontSize: 10.5, color: '#f59e0b', marginTop: 1,
                      textAlign: m.direction === 'out' ? 'right' : 'left', maxWidth: 280 }}>{m.error}</div>
                  )}
                </div>
              </div>
            )
          })}
          </div>
        </div>
        <div onPaste={onComposerPaste} onDrop={onComposerDrop}
          onDragOver={(e) => { if (Array.from(e.dataTransfer?.types || []).includes('Files')) e.preventDefault() }}
          style={{ display: 'flex', flexDirection: 'column', gap: 8, padding: 12, borderTop: '1px solid var(--border)', flexShrink: 0 }}>
          {attachments.length > 0 && (
            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
              {attachments.map((a, i) => (
                <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 4, background: 'var(--hover)',
                  borderRadius: 8, padding: '4px 6px', fontSize: 11, maxWidth: 180 }}>
                  {a.url
                    ? <img src={a.url} alt="" style={{ width: 24, height: 24, objectFit: 'cover', borderRadius: 4, flexShrink: 0 }} />
                    : <span style={{ flexShrink: 0 }}>📎</span>}
                  <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{a.file.name}</span>
                  <button className="btn btn-ghost" type="button" style={{ padding: '0 4px', fontSize: 11, flexShrink: 0 }}
                    aria-label={tr('Remove attachment')} onClick={() => removeAttachment(i)}>✕</button>
                </div>
              ))}
            </div>
          )}
          {attachments.length > 0 && (
            <input placeholder={tr('Subject (optional)')} value={subject} disabled={sending}
              onChange={(e) => setSubject(e.target.value)} style={{ fontSize: 12 }} />
          )}
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
            <input ref={fileInputRef} type="file" multiple
              accept="image/*,audio/*,video/*,text/vcard,text/x-vcard"
              style={{ display: 'none' }}
              onChange={(e) => { addAttachments(e.target.files); e.target.value = '' }} />
            <button className="btn btn-ghost" type="button" disabled={sending || mmsDisabled}
              title={mmsDisabled ? tr('MMS is not configured for this line') : tr('Attach files, or paste or drop them here')}
              aria-label={tr('Attach files')}
              onClick={() => fileInputRef.current?.click()} style={{ padding: '6px 10px' }}>📎</button>
            {attachments.length === 0 ? (
              <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: 'var(--text-mute)', whiteSpace: 'nowrap' }}>
                {tr('Send via')}
                <select value={transport} disabled={sending}
                  onChange={(e) => setTransport(e.target.value)}
                  aria-label={tr('Send via')}
                  title={!cellularAvailable ? tr('This line does not have an available cellular modem.') : ''}
                  style={{ width: 'auto', minWidth: 150 }}>
                  <option value="auto">{tr('Auto (VoWiFi first)')}</option>
                  <option value="vowifi">VoWiFi</option>
                  <option value="cellular" disabled={!cellularAvailable}>
                    {tr('Cellular network (Modem)')}{!cellularAvailable ? ` — ${tr('Unavailable')}` : ''}
                  </option>
                </select>
              </label>
            ) : (
              <span style={{ fontSize: 12, color: 'var(--text-mute)', whiteSpace: 'nowrap' }}>{tr('Sent as MMS')}</span>
            )}
            <input placeholder={tr('Type a message…')} value={text} disabled={sending}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key !== 'Enter') return
                e.preventDefault()
                if (!e.repeat) send()
              }} style={{ flex: '1 1 220px' }} />
            <button className="btn btn-primary" disabled={sending || (!peer && !newTo) || (!text && !attachments.length)}
              onClick={send}>{tr('Send')}</button>
          </div>
        </div>
      </div>
      </div>
      {showMmsSettings && (
        <MmsSettings id={id} showToast={showToast} onClose={() => setShowMmsSettings(false)} />
      )}
    </div>
  )
}

// Renders one MMS message's content. Inbound messages whose parts have not (yet) been
// downloaded show a compact status card with a Download/Retry action instead of any content
// -- there is nothing to render until the MMSC exchange finishes. Everything else shows the
// subject, every part but the SMIL presentation part and the plain-text part (already folded
// into m.body by the backend), and the text body.
function MmsContent({ m, id, tr, busy, onDownload }) {
  const mms = m.mms || {}
  if (m.direction === 'in' && MMS_PENDING_STATES.has(mms.state)) {
    const sizeText = mms.size ? `${Math.ceil(mms.size / 1024)} KB` : '?'
    const stateText = mms.state === 'notified' ? tr('Waiting to download')
      : mms.state === 'downloading' ? tr('Downloading…')
      : mms.state === 'failed' ? `${tr('Download failed')}${mms.last_error ? ': ' + mms.last_error : ''}`
      : tr('Expired')
    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: 4, minWidth: 160 }}>
        <div style={{ fontWeight: 600, fontSize: 12 }}>{tr('MMS')} · {sizeText}</div>
        <div style={{ fontSize: 12 }}>{stateText}</div>
        <button className="btn btn-ghost" type="button" disabled={busy}
          style={{ fontSize: 11, padding: '3px 8px', alignSelf: 'flex-start' }}
          onClick={() => onDownload(m.id)}>
          {mms.state === 'notified' ? tr('Download') : tr('Retry')}
        </button>
      </div>
    )
  }
  const parts = (mms.parts || []).filter((p) =>
    p.content_type !== MMS_SMIL_TYPE && !String(p.content_type || '').startsWith('text/plain'))
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      {mms.subject && <div style={{ fontWeight: 700, fontSize: 12 }}>{mms.subject}</div>}
      {parts.map((p) => {
        const type = String(p.content_type || '')
        const url = api.mmsPartUrl(id, m.id, p.id)
        if (type.startsWith('image/')) {
          return (
            <a key={p.id} href={url} target="_blank" rel="noreferrer">
              <img src={url} alt={p.name || ''} style={{ maxWidth: 240, maxHeight: 240, borderRadius: 8, display: 'block' }} />
            </a>
          )
        }
        if (type.startsWith('audio/')) return <audio key={p.id} controls src={url} style={{ maxWidth: 240 }} />
        if (type.startsWith('video/')) return <video key={p.id} controls src={url} style={{ maxWidth: 240, borderRadius: 8 }} />
        return (
          <a key={p.id} href={api.mmsPartUrl(id, m.id, p.id, true)} download={p.name || true}
            style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, background: 'rgba(0,0,0,.08)',
              borderRadius: 8, padding: '6px 8px', textDecoration: 'none', color: 'inherit' }}>
            <span>📄</span>
            <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: 160 }}>{p.name || type}</span>
            {p.size ? <span className="mono" style={{ opacity: 0.75 }}>{Math.ceil(p.size / 1024)} KB</span> : null}
          </a>
        )
      })}
      {m.body ? <div>{m.body}</div> : null}
    </div>
  )
}

// Payloads that were filed instead of shown: binary SMS, SIM-addressed messages, silent
// service pushes. Deliberately reachable rather than invisible — the classification reads the
// PDU header, and a carrier that mislabels a real text's TP-DCS would otherwise hide it for
// good with no way to notice. Collapsed by default so it costs nothing when there is nothing
// to see, and never renders at all when the line has received none.
const PAYLOAD_TAG_LABEL = {
  '8bit': '8-bit binary data',
  sim_class: 'Addressed to the SIM (class 2)',
  sim_download: 'SIM data download',
  unreported: 'Classified by content — this engine does not report the PDU header',
}

function BinaryPayloads({ payloads, tr }) {
  const [open, setOpen] = useState(false)
  const [shown, setShown] = useState(() => new Set())
  if (!payloads.length) return null
  const toggle = (id) => setShown((prev) => {
    const next = new Set(prev)
    next.has(id) ? next.delete(id) : next.add(id)
    return next
  })
  return (
    <div style={{ marginTop: 12, borderTop: '1px solid var(--border)', paddingTop: 8 }}>
      <div onClick={() => setOpen(!open)} role="button" tabIndex={0}
        onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setOpen(!open) } }}
        style={{ cursor: 'pointer', fontSize: 12, color: 'var(--text-mute)', display: 'flex', alignItems: 'center', gap: 6, padding: '4px 6px', borderRadius: 8 }}>
        <span style={{ fontSize: 10 }}>{open ? '▾' : '▸'}</span>
        <span style={{ flex: 1 }}>{tr('Non-text payloads')}</span>
        <span className="mono">{payloads.length}</span>
      </div>
      {open && (
        <div style={{ marginTop: 4 }}>
          <div style={{ fontSize: 11, color: 'var(--text-mute)', padding: '2px 6px 8px', lineHeight: 1.5 }}>
            {tr('Messages addressed to the SIM or to an application rather than to you. They are kept out of your conversations but not discarded.')}
          </div>
          {payloads.map((p) => (
            <div key={p.id} style={{ padding: '6px 6px 7px', borderRadius: 8, marginBottom: 2, background: 'var(--hover)' }}>
              <div onClick={() => toggle(p.id)} style={{ cursor: 'pointer', display: 'flex', alignItems: 'baseline', gap: 6 }}>
                <span className="mono" style={{ fontSize: 12, fontWeight: 600 }}>{p.peer}</span>
                <span style={{ fontSize: 10, color: 'var(--text-mute)', flex: 1 }}>
                  {new Date(p.ts * 1000).toLocaleString()}
                </span>
                <span className="mono" style={{ fontSize: 10, color: 'var(--text-mute)' }}>
                  {tr('{n} bytes', { n: Math.floor((p.body_hex || '').length / 2) })}
                </span>
              </div>
              <div style={{ fontSize: 10, color: 'var(--text-mute)', marginTop: 2 }}>
                {(p.tags || []).map((tag) => tr(PAYLOAD_TAG_LABEL[tag] || tag)).join(' · ')}
                {p.concat_total ? ` · ${tr('part {seq}/{total}', { seq: p.concat_seq, total: p.concat_total })}` : ''}
              </div>
              {shown.has(p.id) && (
                <div style={{ marginTop: 6 }}>
                  {/* The bytes as they arrived. An encrypted payload can only be identified from
                      the PDU itself, so this is shown raw rather than decoded into anything. */}
                  <div style={{ fontSize: 10, color: 'var(--text-mute)', marginBottom: 2 }}>{tr('Payload')}</div>
                  <div className="mono" style={{ fontSize: 10, wordBreak: 'break-all', lineHeight: 1.5, color: 'var(--text-mute)' }}>
                    {p.body_hex || '—'}
                  </div>
                  {p.udh_hex && (
                    <>
                      <div style={{ fontSize: 10, color: 'var(--text-mute)', margin: '5px 0 2px' }}>{tr('User data header')}</div>
                      <div className="mono" style={{ fontSize: 10, wordBreak: 'break-all', color: 'var(--text-mute)' }}>{p.udh_hex}</div>
                    </>
                  )}
                  <div style={{ fontSize: 10, color: 'var(--text-mute)', marginTop: 5 }}>
                    {p.tp_dcs === null || p.tp_dcs === undefined
                      ? tr('TP-DCS not reported')
                      : `TP-DCS 0x${Number(p.tp_dcs).toString(16).padStart(2, '0')} · TP-PID 0x${Number(p.tp_pid || 0).toString(16).padStart(2, '0')}`}
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
