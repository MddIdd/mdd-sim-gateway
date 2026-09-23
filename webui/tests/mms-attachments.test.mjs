import assert from 'node:assert/strict'
import test from 'node:test'

import { formatBytes } from '../src/mmsAttachments.js'

test('formatBytes shows KB with no decimals below 1 MB', () => {
  assert.equal(formatBytes(0), '0 KB')
  assert.equal(formatBytes(512), '1 KB') // rounds to nearest KB
  assert.equal(formatBytes(2048), '2 KB')
  assert.equal(formatBytes(300 * 1024), '300 KB')
})

test('formatBytes shows MB with one decimal at or above 1 MB', () => {
  assert.equal(formatBytes(1024 * 1024), '1.0 MB')
  assert.equal(formatBytes(1.5 * 1024 * 1024), '1.5 MB')
  assert.equal(formatBytes(10 * 1024 * 1024), '10.0 MB')
})

test('formatBytes treats missing or invalid input as zero', () => {
  assert.equal(formatBytes(undefined), '0 KB')
  assert.equal(formatBytes(null), '0 KB')
  assert.equal(formatBytes(NaN), '0 KB')
})
