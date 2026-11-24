import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { addTodo, withTodoLock, MACHINE } from '../src/todo-store.js'

const DARWIN = { skip: process.platform !== 'darwin' && 'the task-list lock is taken with a macOS flag' }
const O_EXLOCK = 0x20

// A home with a task list; the module reads HOME on every call.
function home(items = []) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'reactor-store-'))
  fs.mkdirSync(path.join(dir, '.claude'))
  fs.writeFileSync(path.join(dir, '.claude', 'assistant-todo.json'), JSON.stringify({ items, completed: [{ id: 'td-009' }] }))
  process.env.HOME = dir
  return { dir, store: path.join(dir, '.claude', 'assistant-todo.json'), lock: path.join(dir, '.claude', 'assistant-todo.lock') }
}
const read = (h) => JSON.parse(fs.readFileSync(h.store, 'utf8'))
const hold = (h) => fs.openSync(h.lock, fs.constants.O_RDWR | fs.constants.O_CREAT | O_EXLOCK | fs.constants.O_NONBLOCK, 0o600)
const todo = (extra = {}) => ({ title: 't', detail: 'd', url: 'u', source: 'slack-react:C:1', ...extra })

test('add: a new task takes the next id and the fields the dashboard reads', DARWIN, async () => {
  const h = home()
  assert.deepEqual(await addTodo(todo({ priority: 'P1', autoDispatch: null, reactedBy: 'U7' })), { id: 'td-010', created: true })
  const item = read(h).items[0]
  assert.deepEqual({ ...item, createdAt: undefined }, {
    id: 'td-010', priority: 'P1', title: 't', detail: 'd', url: 'u', source: 'slack-react:C:1', createdAt: undefined,
    status: 'open', autoDispatch: null, capturedBy: MACHINE, reactedBy: 'U7',
  })
  assert.match(read(h)._lastUpdated, /^\d{4}-\d\d-\d\dT[\d:]+Z$/)
})

test('add: defaults, an unknown priority, and no reactedBy field unless given', DARWIN, async () => {
  const h = home()
  await addTodo(todo({ priority: 'P9' }))
  const item = read(h).items[0]
  assert.deepEqual([item.priority, item.autoDispatch, 'reactedBy' in item], ['P2', true, false])
})

test('add: the same thread is a no-op unless the earlier task is done or deferred', DARWIN, async () => {
  const h = home([{ id: 'td-001', source: 'slack-react:C:1', status: 'open' }, { id: 'td-002', source: 'slack-react:C:2', status: 'done' }])
  assert.deepEqual(await addTodo(todo()), { id: 'td-001', created: false })
  assert.equal((await addTodo(todo({ source: 'slack-react:C:2' }))).created, true)
  assert.equal(read(h).items.length, 3)
})

test('add: a missing task list is an error, and a task list that is not JSON is left alone', DARWIN, async () => {
  const h = home()
  fs.rmSync(h.store)
  await assert.rejects(addTodo(todo()), /todo store not found/)
  fs.writeFileSync(h.store, '{broken')
  await assert.rejects(addTodo(todo()), SyntaxError)
  assert.equal(fs.readFileSync(h.store, 'utf8'), '{broken')
  hold(h) // the failed add let go of the lock: it can be taken again
})

test('lock: an add waits while another writer holds the lock, then writes', DARWIN, async () => {
  const h = home()
  const fd = hold(h)
  const started = Date.now()
  setTimeout(() => fs.closeSync(fd), 300)
  const result = await addTodo(todo(), { retryMs: 10 })
  assert.equal(result.created, true)
  assert.ok(Date.now() - started >= 250)
})

test('lock: it does not block the event loop while it waits', DARWIN, async () => {
  const h = home()
  const fd = hold(h)
  let ticks = 0
  const timer = setInterval(() => ticks++, 10)
  setTimeout(() => fs.closeSync(fd), 300)
  await addTodo(todo(), { retryMs: 10 })
  clearInterval(timer)
  assert.ok(ticks >= 10, `only ${ticks} ticks`)
})

test('lock: at the limit the add fails and writes nothing', DARWIN, async () => {
  const h = home()
  const fd = hold(h)
  const before = fs.readFileSync(h.store, 'utf8')
  await assert.rejects(addTodo(todo(), { waitMs: 100, retryMs: 10 }), /still held by another writer after 100 ms/)
  assert.equal(fs.readFileSync(h.store, 'utf8'), before)
  fs.closeSync(fd)
})

test('lock: on another platform nothing is written', async () => {
  const h = home()
  const before = fs.readFileSync(h.store, 'utf8')
  await assert.rejects(addTodo(todo(), { platform: 'linux' }), /cannot take the task-list lock on linux/)
  assert.equal(fs.readFileSync(h.store, 'utf8'), before)
  assert.ok(!fs.existsSync(h.lock))
})

test('lock: any other failure to open the lock file is raised as it is', DARWIN, async () => {
  const h = home()
  fs.mkdirSync(h.lock)
  await assert.rejects(addTodo(todo()), { code: 'EISDIR' })
})

test('lock: the file is private, and is let go when the work is done or throws', DARWIN, async () => {
  const h = home()
  assert.equal(await withTodoLock(() => 'done'), 'done')
  assert.equal(fs.statSync(h.lock).mode & 0o777, 0o600)
  await assert.rejects(withTodoLock(() => { throw new Error('boom') }), /boom/)
  fs.closeSync(hold(h)) // free again
})

test('write: a temporary file of its own, mode 0600, gone once renamed', DARWIN, async () => {
  const h = home()
  const seen = []
  await addTodo(todo(), { rename: (from, to) => { seen.push([from, fs.statSync(from).mode & 0o777]); fs.renameSync(from, to) } })
  assert.match(seen[0][0], new RegExp(`assistant-todo\\.json\\.${process.pid}\\.[0-9a-f]{8}\\.tmp$`))
  assert.equal(seen[0][1], 0o600)
  assert.deepEqual(fs.readdirSync(path.dirname(h.store)).sort(), ['assistant-todo.json', 'assistant-todo.lock'])
})

test('write: a failed rename removes the temporary file and keeps the task list as it was', DARWIN, async () => {
  const h = home()
  const before = fs.readFileSync(h.store, 'utf8')
  await assert.rejects(addTodo(todo(), { rename: () => { throw new Error('rename broke') } }), /rename broke/)
  assert.deepEqual(fs.readdirSync(path.dirname(h.store)).sort(), ['assistant-todo.json', 'assistant-todo.lock'])
  assert.equal(fs.readFileSync(h.store, 'utf8'), before)
})
