// The MMS attachment capability table, mirrored from control/app/mms_media.py (see FORMATS
// there), so the picker, paste and drag-drop follow the same rule the backend enforces on
// upload. `GET /api/instances/{id}/mms/settings` returns this table live as `formats`, each
// entry carrying an `attachable` flag the gateway computes itself -- true for a "send" format
// and, for a "convert" one (HEIC/WebP/BMP/AVIF and friends), true only when the gateway can
// actually convert it. An older backend either omits `formats` entirely (FALLBACK_FORMATS, a
// send-only table, stands in for it) or returns the table without the `attachable` field, in
// which case "send" is the best a client can infer -- a "convert" format would only be found
// out to be unsupported when the gateway later refuses the upload. These helpers are plain
// logic (no DOM) so they can be unit tested directly.

export const SEND = 'send'
export const CONVERT = 'convert'

export const FALLBACK_FORMATS = [
  { content_type: 'image/jpeg', kind: 'image', extensions: ['jpg', 'jpeg'], policy: SEND, aliases: [] },
  { content_type: 'image/gif', kind: 'image', extensions: ['gif'], policy: SEND, aliases: [] },
  { content_type: 'image/png', kind: 'image', extensions: ['png'], policy: SEND, aliases: [] },
  { content_type: 'audio/amr', kind: 'audio', extensions: ['amr'], policy: SEND, aliases: [] },
  { content_type: 'audio/amr-wb', kind: 'audio', extensions: ['awb'], policy: SEND, aliases: [] },
  { content_type: 'audio/mpeg', kind: 'audio', extensions: ['mp3'], policy: SEND, aliases: [] },
  { content_type: 'audio/mp4', kind: 'audio', extensions: ['m4a'], policy: SEND, aliases: [] },
  { content_type: 'audio/3gpp', kind: 'audio', extensions: ['3ga'], policy: SEND, aliases: [] },
  { content_type: 'video/3gpp', kind: 'video', extensions: ['3gp'], policy: SEND, aliases: [] },
  { content_type: 'video/mp4', kind: 'video', extensions: ['mp4', 'm4v'], policy: SEND, aliases: [] },
  { content_type: 'text/plain', kind: 'text', extensions: ['txt'], policy: SEND, aliases: [] },
  { content_type: 'text/x-vCard', kind: 'contact', extensions: ['vcf'], policy: SEND, aliases: ['text/vcard'] },
  { content_type: 'text/x-vCalendar', kind: 'calendar', extensions: ['vcs'], policy: SEND, aliases: [] },
  { content_type: 'text/calendar', kind: 'calendar', extensions: ['ics'], policy: SEND, aliases: [] },
]

// MIME parameters (";codecs=...") and case never matter for matching.
const norm = (type) => String(type || '').split(';')[0].trim().toLowerCase()

// What browsers and file inputs hand over instead of a real type; treated as "no type".
const GENERIC_TYPES = new Set(['', 'application/octet-stream', 'binary/octet-stream', 'application/x-unknown'])

function fileExtension(name) {
  const match = /\.([a-z0-9]{1,10})$/i.exec(String(name || ''))
  return match ? match[1].toLowerCase() : ''
}

// A format is attachable per its own `attachable` flag when the table carries one -- the
// gateway already knows whether it has a converter for a "convert" format -- or, for an
// older table with no such flag, only when it is sent as is.
function isFormatAttachable(f) {
  return typeof f.attachable === 'boolean' ? f.attachable : f.policy === SEND
}

/** Formats a client may attach, per isFormatAttachable. Anything else (e.g. a "convert"
 * format this gateway cannot convert, or policy "receive") can be received but never
 * attached. */
export function attachableFormats(formats = FALLBACK_FORMATS) {
  return (formats || []).filter(isFormatAttachable)
}

/** The file input's `accept` string: every attachable MIME type and alias, plus a `.ext` for
 * each extension. Browsers often hand over an empty MIME type for .amr/.vcf/.vcs/.3gp files,
 * so the extensions matter as much as the MIME types. */
export function acceptAttribute(formats = FALLBACK_FORMATS) {
  const values = new Set()
  attachableFormats(formats).forEach((f) => {
    if (f.content_type) values.add(f.content_type)
    ;(f.aliases || []).forEach((a) => a && values.add(a))
    ;(f.extensions || []).forEach((e) => e && values.add(`.${e}`))
  })
  return [...values].join(',')
}

/** Whether `file` is a type/extension this table lets a client attach: matched by file.type
 * (case-insensitively, MIME parameters stripped, aliases included) or, when file.type is empty
 * or a generic placeholder (application/octet-stream and friends), by file extension instead --
 * which is what lets HEIC/HEIF through on platforms that hand them over with no MIME type. */
export function isAttachable(file, formats = FALLBACK_FORMATS) {
  const attachable = attachableFormats(formats)
  const type = norm(file && file.type)
  if (type && !GENERIC_TYPES.has(type)) {
    return attachable.some((f) => norm(f.content_type) === type
      || (f.aliases || []).some((a) => norm(a) === type))
  }
  const ext = fileExtension(file && file.name)
  if (!ext) return false
  return attachable.some((f) => (f.extensions || []).some((e) => String(e).toLowerCase() === ext))
}
