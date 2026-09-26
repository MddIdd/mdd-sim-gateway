// Names for numbers, resolved by the gateway and cached for the life of the page.
//
// The browser cannot do this itself. A number arrives in whatever shape the network used, and
// reducing two spellings to the same destination needs a numbering plan and the country the
// line is in -- neither of which the page has -- and then a lookup against every stored
// number, which is a database query rather than a scan of a list the page happens to hold. So
// the page asks for a screenful of numbers at once and remembers the answers.
//
// Every answer is scoped to the line the numbers arrived on: the same national number names a
// different destination on a line in another country, so one line's answer is not another's.
import { useEffect, useState } from 'react'
import { api } from './api.js'

const cache = new Map()     // "<line>|<number>" -> name, or '' for "no contact"
const pending = new Map()   // line -> Set of numbers waiting for the next flush
let flushTimer = null
const listeners = new Set()

// A line id is letters, digits, dash and underscore, so a bar cannot be mistaken for content.
const cacheKey = (line, number) => `${line ?? ''}|${number}`

function flush() {
  flushTimer = null
  const batches = [...pending.entries()]
  pending.clear()
  for (const [line, numbers] of batches) {
    const asked = [...numbers]
    if (!asked.length) continue
    api.resolveContacts(asked, line).then((result) => {
      const found = result.contacts || {}
      // Remember the misses too: a conversation with somebody who is not in the book must not
      // re-ask on every render.
      asked.forEach((number) => cache.set(cacheKey(line, number), found[number]?.name || ''))
      listeners.forEach((notify) => notify())
    }).catch(() => {
      asked.forEach((number) => cache.set(cacheKey(line, number), ''))
      listeners.forEach((notify) => notify())
    })
  }
}

function request(line, numbers) {
  const missing = numbers.filter((number) => number && !cache.has(cacheKey(line, number)))
  if (!missing.length) return
  const waiting = pending.get(line) || new Set()
  missing.forEach((number) => waiting.add(number))
  pending.set(line, waiting)
  if (!flushTimer) flushTimer = setTimeout(flush, 30)
}

/** Forget every answer -- after an import, or after a contact was edited. */
export function forgetContactNames() {
  cache.clear()
  listeners.forEach((notify) => notify())
}

/** Names for these numbers as they arrived on `line`: number -> name, misses simply absent. */
export function useContactNames(numbers, line) {
  const [, bump] = useState(0)
  const key = `${line ?? ''}|${numbers.join('|')}`
  useEffect(() => {
    const notify = () => bump((n) => n + 1)
    listeners.add(notify)
    request(line, numbers)
    return () => listeners.delete(notify)
  }, [key]) // eslint-disable-line react-hooks/exhaustive-deps
  const out = {}
  numbers.forEach((number) => {
    const name = cache.get(cacheKey(line, number))
    if (name) out[number] = name
  })
  return out
}
