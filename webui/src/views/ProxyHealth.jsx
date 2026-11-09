import React, { useState } from 'react'
import { api } from '../api.js'
import { useI18n } from '../i18n.jsx'

const MESSAGES = {
  unknown: 'Proxy status awaiting check',
  checking: 'Checking proxy UDP connectivity…',
  suspect: 'Proxy UDP check failed; confirming…',
  passed: 'Proxy UDP check passed',
  failed: 'Proxy UDP connectivity problem',
}

export default function ProxyHealth({ device, onChanged, showToast }) {
  const { t, language } = useI18n()
  const [busy, setBusy] = useState(false)
  const health = device?.status?.proxy_health
  if (!health || !device.instance_id) return null
  const check = async () => {
    setBusy(true)
    try {
      const result = await api.checkProxyHealth(device.instance_id)
      showToast?.(t(MESSAGES[result.proxy_health?.state] || MESSAGES.unknown))
      await onChanged?.()
    } catch (error) { showToast?.(t('Proxy check could not be completed. Try again shortly.')) }
    finally { setBusy(false) }
  }
  const checked = health.checked_at && new Date(health.checked_at * 1000).toLocaleString(
    language === 'zh' ? 'zh-CN' : 'en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  return <div className={`u-proxy-health ${health.state === 'failed' ? 'warning' : ''}`} role="status">
    <span>{t(MESSAGES[health.state] || MESSAGES.unknown)}
      {device.status?.detail?.registration === 'Registered' && health.state === 'failed'
        && <> · {t('IMS registered')}</>}
      {checked && <small>{t('Last checked: {time}', { time: checked })}</small>}
    </span>
    <button className="btn btn-ghost" disabled={busy || health.checking}
      onClick={check}>{t(busy || health.checking ? 'Checking…' : 'Check again')}</button>
  </div>
}
