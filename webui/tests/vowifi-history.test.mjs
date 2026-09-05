import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const source = readFileSync(new URL('../src/views/VowifiHistory.jsx', import.meta.url), 'utf8')
const body = source.slice(source.indexOf('export default function VowifiHistory'),
  source.indexOf('  const segments = data?.segments'))
  .replace('export default function', 'function')

function harness() {
  const slots = []
  const requests = []
  let cursor = 0
  let effects = []
  let cleanup = []
  const context = {
    useI18n: () => ({ t: value => value, language: 'en' }),
    useState(initial) {
      const index = cursor++
      if (!(index in slots)) slots[index] = initial
      return [slots[index], value => { slots[index] = value }]
    },
    useRef(initial) {
      const index = cursor++
      if (!(index in slots)) slots[index] = { current: initial }
      return slots[index]
    },
    useCallback: callback => callback,
    useEffect: effect => effects.push(effect),
    setInterval: () => 1,
    clearInterval: () => {},
    REFRESH_MS: 30000,
    api: {
      lineAvailability(id) {
        return new Promise((resolve, reject) => requests.push({ id, resolve, reject }))
      },
    },
  }
  vm.createContext(context)
  vm.runInContext(`${body}\nreturn { load }\n}`, context)
  return {
    slots,
    requests,
    render(instanceId) {
      cleanup.forEach(dispose => dispose?.())
      cursor = 0
      effects = []
      const rendered = context.VowifiHistory({ instanceId })
      cleanup = effects.map(effect => effect())
      return rendered
    },
    unmount() { cleanup.forEach(dispose => dispose?.()) },
  }
}

const settle = () => new Promise(resolve => setImmediate(resolve))

test('a previous line cannot overwrite the current line', async () => {
  const app = harness()
  app.render('A')
  app.render('B')
  app.requests[1].resolve({ line: 'B' })
  await settle()
  app.requests[0].resolve({ line: 'A' })
  await settle()
  assert.equal(app.slots[0].line, 'B')
})

test('overlapping refreshes accept only the newest response or error', async () => {
  const app = harness()
  const rendered = app.render('A')
  rendered.load()
  app.requests[1].resolve({ version: 2 })
  await settle()
  app.requests[0].reject(new Error('stale error'))
  await settle()
  assert.equal(app.slots[0].version, 2)
  assert.equal(app.slots[1], '')
  rendered.load()
  rendered.load()
  app.requests[3].resolve({ version: 4 })
  await settle()
  app.requests[2].resolve({ version: 3 })
  await settle()
  assert.equal(app.slots[0].version, 4)
})

test('changing lines clears old errors and unmount invalidates requests', async () => {
  const app = harness()
  app.render('A')
  app.requests[0].reject(new Error('A failed'))
  await settle()
  assert.equal(app.slots[1], 'A failed')
  app.render('B')
  assert.equal(app.slots[1], '')
  app.unmount()
  app.requests[1].resolve({ line: 'B' })
  await settle()
  assert.equal(app.slots[0], null)
})
