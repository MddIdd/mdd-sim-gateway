// Pure helpers for the MMS composer's attachment chips (Messages.jsx). No DOM, so they are
// unit tested directly (webui/tests/mms-attachments.test.mjs).

/** A human-scale byte size for a chip's size line: KB with no decimals below 1 MB (fine
 * enough for judging "does this fit" against a limit that is itself in KB), MB with one
 * decimal at or above it. */
export function formatBytes(bytes) {
  const n = Number(bytes) || 0
  if (n < 1024 * 1024) return `${Math.max(0, Math.round(n / 1024))} KB`
  return `${(n / (1024 * 1024)).toFixed(1)} MB`
}
