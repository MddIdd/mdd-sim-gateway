import assert from 'node:assert/strict'
import test from 'node:test'

import {
  FALLBACK_FORMATS, attachableFormats, acceptAttribute, isAttachable,
} from '../src/mmsFormats.js'

test('attachableFormats uses the attachable flag when the table carries one', () => {
  const formats = [
    { content_type: 'image/jpeg', policy: 'send', attachable: true },
    { content_type: 'image/webp', policy: 'convert', attachable: true },
    { content_type: 'image/heic', policy: 'convert', attachable: false },
    { content_type: 'image/vnd.wap.wbmp', policy: 'receive', attachable: false },
  ]
  assert.deepEqual(attachableFormats(formats).map((f) => f.content_type),
    ['image/jpeg', 'image/webp'])
})

test('attachableFormats falls back to policy === send for a table with no attachable field', () => {
  const formats = [
    { content_type: 'image/jpeg', policy: 'send' },
    { content_type: 'image/webp', policy: 'convert' },
    { content_type: 'image/vnd.wap.wbmp', policy: 'receive' },
  ]
  assert.deepEqual(attachableFormats(formats).map((f) => f.content_type), ['image/jpeg'])
})

test('attachableFormats defaults to FALLBACK_FORMATS when called with no argument', () => {
  assert.equal(attachableFormats().length, FALLBACK_FORMATS.length)
  assert.ok(FALLBACK_FORMATS.every((f) => f.policy === 'send'))
})

test('acceptAttribute lists every attachable content type, alias and extension', () => {
  const formats = [
    { content_type: 'text/x-vCard', policy: 'send', attachable: true, extensions: ['vcf'], aliases: ['text/vcard'] },
    { content_type: 'image/vnd.wap.wbmp', policy: 'receive', attachable: false, extensions: ['wbmp'], aliases: [] },
  ]
  const accept = acceptAttribute(formats).split(',')
  assert.ok(accept.includes('text/x-vCard'))
  assert.ok(accept.includes('text/vcard'))
  assert.ok(accept.includes('.vcf'))
  // A receive-only format never appears -- it can be shown if received, never attached.
  assert.ok(!accept.includes('image/vnd.wap.wbmp'))
  assert.ok(!accept.includes('.wbmp'))
})

test('acceptAttribute on the fallback table includes MIME types and extension fallbacks', () => {
  const accept = acceptAttribute().split(',')
  assert.ok(accept.includes('image/jpeg'))
  assert.ok(accept.includes('audio/amr'))
  assert.ok(accept.includes('.amr')) // browsers often hand over an empty type for .amr
  assert.ok(accept.includes('.vcf'))
  assert.ok(accept.includes('.3gp'))
})

test('acceptAttribute offers a convert format only when the gateway marks it attachable', () => {
  const formats = [
    { content_type: 'image/heic', policy: 'convert', attachable: false, extensions: ['heic'], aliases: [] },
  ]
  assert.equal(acceptAttribute(formats), '')
  const converting = [
    { content_type: 'image/heic', policy: 'convert', attachable: true, extensions: ['heic'], aliases: [] },
  ]
  assert.ok(acceptAttribute(converting).split(',').includes('image/heic'))
})

test('isAttachable matches by MIME type, case-insensitively and ignoring parameters', () => {
  assert.equal(isAttachable({ type: 'IMAGE/JPEG', name: 'a' }), true)
  assert.equal(isAttachable({ type: 'audio/mpeg;codecs=mp3', name: 'a' }), true)
  assert.equal(isAttachable({ type: 'application/pdf', name: 'a.pdf' }), false)
})

test('isAttachable matches an alias', () => {
  assert.equal(isAttachable({ type: 'text/vcard', name: 'card' }), true)
  assert.equal(isAttachable({ type: 'text/x-vcard', name: 'card' }), true) // case-insensitive alias match
})

test('isAttachable falls back to file extension when type is empty or a generic placeholder', () => {
  assert.equal(isAttachable({ type: '', name: 'clip.3gp' }), true)
  assert.equal(isAttachable({ type: 'application/octet-stream', name: 'voice.amr' }), true)
  assert.equal(isAttachable({ type: '', name: 'contact.vcf' }), true)
  assert.equal(isAttachable({ type: '', name: 'unknown.zip' }), false)
})

test('isAttachable takes a convert format with an empty type by extension only when the gateway marks it attachable', () => {
  const formats = [
    { content_type: 'image/heic', policy: 'convert', attachable: false, extensions: ['heic'], aliases: [] },
  ]
  assert.equal(isAttachable({ type: '', name: 'IMG_1234.HEIC' }, formats), false)
  const converting = [
    { content_type: 'image/heic', policy: 'convert', attachable: true, extensions: ['heic'], aliases: [] },
  ]
  assert.equal(isAttachable({ type: '', name: 'IMG_1234.HEIC' }, converting), true)
  assert.equal(isAttachable({ type: '', name: 'IMG_1234.heic' }, converting), true)
})

test('isAttachable never uses extension as a fallback for a real, non-matching type', () => {
  // A mislabeled file's declared type is not overridden by a matching extension.
  assert.equal(isAttachable({ type: 'application/zip', name: 'photo.jpg' }), false)
})

test('isAttachable honors a custom formats table rather than always using the fallback', () => {
  const formats = [{ content_type: 'application/x-custom', policy: 'send', extensions: ['xcf'], aliases: [] }]
  assert.equal(isAttachable({ type: 'application/x-custom', name: 'a' }, formats), true)
  assert.equal(isAttachable({ type: 'image/jpeg', name: 'a.jpg' }, formats), false)
})
