import React, { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { useI18n } from '../i18n.jsx'
import { forgetContactNames } from '../contactNames.js'

const emptyContact = () => ({ name: '', company: '', note: '', numbers: [{ label: '', number: '' }] })

function ContactForm({ value, onCancel, onSaved, showToast, t }) {
  const [form, setForm] = useState(() => ({
    ...emptyContact(), ...value,
    numbers: (value?.numbers?.length ? value.numbers : emptyContact().numbers).map(n => ({ ...n })),
  }))
  const [busy, setBusy] = useState(false)
  const set = patch => setForm(f => ({ ...f, ...patch }))
  const setNumber = (index, patch) => set({ numbers: form.numbers.map((n, i) => i === index ? { ...n, ...patch } : n) })
  const submit = async e => {
    e.preventDefault()
    if (busy) return
    setBusy(true)
    const body = { ...form, numbers: form.numbers.filter(n => n.number.trim()) }
    try {
      const saved = value?.id ? await api.updateContact(value.id, body) : await api.createContact(body)
      forgetContactNames()
      onSaved(saved.contact)
      showToast(t(value?.id ? 'Saved' : 'Contact added'))
    } catch (err) { showToast(err.message) } finally { setBusy(false) }
  }
  return <form className="card u-panel" onSubmit={submit}>
    <div className="u-form-grid">
      <div><label>{t('Name')}</label><input value={form.name} onChange={e => set({ name: e.target.value })} autoComplete="off" /></div>
      <div><label>{t('Company')}</label><input value={form.company} onChange={e => set({ company: e.target.value })} autoComplete="off" /></div>
    </div>
    <label>{t('Numbers')}</label>
    {form.numbers.map((number, index) => <div className="u-form-grid" key={index}>
      <div><input value={number.number} onChange={e => setNumber(index, { number: e.target.value })} placeholder="+44 7700 900123" inputMode="tel" autoComplete="off" /></div>
      <div style={{ display: 'flex', gap: 8 }}>
        <input value={number.label} onChange={e => setNumber(index, { label: e.target.value })} placeholder={t('Label (mobile, work…)')} autoComplete="off" />
        {form.numbers.length > 1 && <button type="button" className="btn btn-ghost" onClick={() => set({ numbers: form.numbers.filter((_, i) => i !== index) })}>{t('Remove')}</button>}
      </div>
    </div>)}
    <button type="button" className="btn btn-ghost" onClick={() => set({ numbers: [...form.numbers, { label: '', number: '' }] })}>{t('Add a number')}</button>
    <label>{t('Note')}</label>
    <input value={form.note} onChange={e => set({ note: e.target.value })} autoComplete="off" />
    <div className="u-settings-actions" style={{ gap: 10 }}>
      <button type="button" className="btn btn-ghost" onClick={onCancel}>{t('Cancel')}</button>
      <button type="submit" className="btn btn-primary" disabled={busy || !form.numbers.some(n => n.number.trim())}>{t(busy ? 'Please wait…' : 'Save')}</button>
    </div>
  </form>
}

function ImportPanel({ onImported, showToast, t }) {
  const fileRef = useRef(null)
  const [report, setReport] = useState(null)
  const [busy, setBusy] = useState(false)
  const pick = async event => {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file || busy) return
    setBusy(true)
    try {
      const result = await api.importContacts(file)
      forgetContactNames()
      setReport(result)
      onImported()
      showToast(t('{added} added, {merged} merged, {skipped} already there', result))
    } catch (err) { showToast(err.message) } finally { setBusy(false) }
  }
  return <div className="card u-panel">
    <div className="u-card-head"><div><h2>{t('Import and export')}</h2><p>{t('vCard (.vcf) or CSV, as exported by a phone or by this page. Importing an export again folds into what is already here instead of doubling it.')}</p></div></div>
    <input ref={fileRef} type="file" accept=".vcf,.vcard,.csv,text/vcard,text/csv" style={{ display: 'none' }} onChange={pick} />
    <div className="u-action-list">
      <button className="btn btn-ghost" disabled={busy} onClick={() => fileRef.current?.click()}>{t(busy ? 'Importing…' : 'Import a file')}</button>
      <a className="btn btn-ghost" href={api.contactsExportUrl('vcf')}>{t('Export as vCard')}</a>
      <a className="btn btn-ghost" href={api.contactsExportUrl('csv')}>{t('Export as CSV')}</a>
    </div>
    {report && <>
      <p className="u-note">{t('{read} read · {added} added · {merged} merged · {skipped} already there', report)}</p>
      {!!report.problems?.length && <div className="u-details">{report.problems.map((problem, index) => <div className="u-detail" key={index}><span>{t('Not imported')}</span><b>{problem}</b></div>)}</div>}
    </>}
  </div>
}

export default function ContactsPage({ showToast }) {
  const { t } = useI18n()
  const [query, setQuery] = useState('')
  const [state, setState] = useState(null)
  const [loadError, setLoadError] = useState(false)
  const [editing, setEditing] = useState(null)   // a contact, {} for a new one, null for none
  const reload = (text = query) => api.contacts(text).then(value => { setState(value); setLoadError(false) }).catch(() => setLoadError(true))
  // Search runs against the gateway, not against a list held here: the book can be thousands
  // of entries and the match is on trailing digits, which the browser cannot reproduce.
  useEffect(() => { const timer = setTimeout(() => reload(query), 200); return () => clearTimeout(timer) }, [query])
  if (!state) return <p className={loadError ? 'u-error' : ''}>{t(loadError ? 'Loading failed' : 'Loading')}{!loadError && '…'}</p>
  const contacts = state.contacts || []
  const remove = async contact => {
    if (!window.confirm(t('Remove {name}?', { name: contact.name }))) return
    try { await api.deleteContact(contact.id); forgetContactNames(); showToast(t('Contact removed')); reload() } catch (err) { showToast(err.message) }
  }
  return <div className="u-page">
    <div className="u-section-title">
      <div><h2>{t('Contacts')}</h2><p>{t('{total} contacts. Names from here are shown on messages and calls.', { total: state.total || 0 })}</p></div>
      <button className="btn btn-primary" onClick={() => setEditing({})}>{t('Add contact')}</button>
    </div>
    <input value={query} onChange={e => setQuery(e.target.value)} placeholder={t('Search by name, company or number')} autoComplete="off" />
    {editing && <ContactForm value={editing.id ? editing : null} showToast={showToast} t={t}
      onCancel={() => setEditing(null)} onSaved={() => { setEditing(null); reload() }} />}
    {contacts.length
      ? <div className="u-device-grid">{contacts.map(contact => <div className="card u-panel" key={contact.id}>
        <div className="u-card-head"><div><h2>{contact.name}</h2>{!!contact.company && <p>{contact.company}</p>}</div></div>
        <div className="u-details">{contact.numbers.map((number, index) => <div className="u-detail" key={index}>
          <span>{number.label || t('Number')}</span><b>{number.number}</b>
        </div>)}</div>
        {!!contact.note && <p className="u-note">{contact.note}</p>}
        <div className="u-settings-actions" style={{ gap: 10 }}>
          <button className="btn btn-ghost" onClick={() => setEditing(contact)}>{t('Edit')}</button>
          <button className="btn btn-ghost" onClick={() => remove(contact)}>{t('Remove')}</button>
        </div>
      </div>)}</div>
      : <p className="u-note">{t(query ? 'No contact matches this search.' : 'No contacts yet. Add one, or import a vCard or CSV export.')}</p>}
    <ImportPanel onImported={() => reload()} showToast={showToast} t={t} />
  </div>
}
