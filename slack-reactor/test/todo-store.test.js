import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, readdirSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

// TODO_PATH is fixed when the module loads, so the home folder is set first and the real one is never read.
const home = mkdtempSync(join(tmpdir(), 'todo-store-'))
process.env.HOME = home
mkdirSync(join(home, '.claude'))
const { addTodo, TODO_PATH } = await import('../src/todo-store.js')

const TRACE = /^tr-[0-9a-f]{16}$/
const read = () => JSON.parse(readFileSync(TODO_PATH, 'utf8'))
const TASK = { title: 'Look at the thread', detail: 'd', url: 'https://x.slack.com/archives/C1/p1', source: 'slack:C1:1' }

test('a captured thread becomes a task with a trace id, and a thread captured again keeps its task and id', () => {
  // A task written before ids existed, and one that has an id already: neither is touched.
  const old = { id: 'td-001', status: 'open', source: 'manual:2026-01-01' }
  const kept = { id: 'td-002', status: 'open', source: 'manual:2026-01-02', trace: 'tr-0123456789abcdef' }
  writeFileSync(TODO_PATH, JSON.stringify({ items: [old, kept], completed: [], removed: [] }))
  const made = addTodo(TASK)
  assert.deepEqual(made, { id: 'td-003', created: true })
  const [first, second, task] = read().items
  assert.deepEqual([first, second], [old, kept])
  assert.match(task.trace, TRACE)
  assert.deepEqual(Object.keys(task).filter((k) => k !== 'trace'),
    ['id', 'priority', 'title', 'detail', 'url', 'source', 'createdAt', 'status', 'autoDispatch', 'capturedBy'])
  const before = readFileSync(TODO_PATH, 'utf8')
  assert.deepEqual(addTodo(TASK), { id: 'td-003', created: false })
  assert.equal(readFileSync(TODO_PATH, 'utf8'), before)
})

test('each captured thread gets its own id', () => {
  writeFileSync(TODO_PATH, JSON.stringify({ items: [] }))
  addTodo(TASK)
  addTodo({ ...TASK, source: 'slack:C1:2' })
  const [a, b] = read().items.map((i) => i.trace)
  assert.match(a, TRACE)
  assert.match(b, TRACE)
  assert.notEqual(a, b)
})

test('the file is written through a temporary file of its own, which never collides with another writer\'s or lingers', () => {
  writeFileSync(TODO_PATH, JSON.stringify({ items: [] }))
  const theirs = `${TODO_PATH}.tmp` // the name the pulse used to share with this writer
  writeFileSync(theirs, 'another writer\'s half-written file')
  addTodo(TASK)
  assert.equal(readFileSync(theirs, 'utf8'), 'another writer\'s half-written file')
  assert.deepEqual(readdirSync(join(home, '.claude')).filter((f) => f.endsWith('.tmp')), ['assistant-todo.json.tmp'])
  assert.equal(read().items.length, 1)
})
