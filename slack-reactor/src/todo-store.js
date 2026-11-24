// TODO store writer — mirrors the /todo skill schema + atomic write.
// Single source of truth: ~/.claude/assistant-todo.json (the dashboard + pulse
// read the same file). Every Python writer wraps its read-modify-write in
// todostore.todo_lock(), an flock on ~/.claude/assistant-todo.lock. The reactor
// takes that same lock: on macOS, opening the lock file with O_EXLOCK takes the
// very lock flock() takes, so Node needs no native code to join the queue.
import fs from 'node:fs'
import { homedir, hostname } from 'node:os'
import { join } from 'node:path'
import { randomBytes } from 'node:crypto'

export const TODO_PATH = join(homedir(), '.claude', 'assistant-todo.json')
export const MACHINE = hostname()

const todoPath = () => join(homedir(), '.claude', 'assistant-todo.json')
const lockPath = () => join(homedir(), '.claude', 'assistant-todo.lock')

const LOCK_WAIT_MS = 5000 // how long an add waits for another writer before it gives up
const LOCK_RETRY_MS = 10
const O_EXLOCK_DARWIN = 0x20 // Node does not export it; this is its value on macOS

function nowIso() {
  // ISO without millis, Z-suffixed — matches the Python writers' format.
  return new Date().toISOString().replace(/\.\d{3}Z$/, 'Z')
}

function nextId(data) {
  let max = 0
  for (const bucket of ['items', 'completed', 'removed']) {
    for (const it of data[bucket] ?? []) {
      const m = /^td-(\d+)/.exec(it.id ?? '')
      if (m) max = Math.max(max, parseInt(m[1], 10))
    }
  }
  return `td-${String(max + 1).padStart(3, '0')}`
}

// Run `fn` while holding the task-list lock, retrying without blocking the event loop.
// Never runs `fn` without the lock: another platform, or a lock still held at the limit, throws.
export async function withTodoLock(fn, { platform = process.platform, waitMs = LOCK_WAIT_MS, retryMs = LOCK_RETRY_MS } = {}) {
  if (platform !== 'darwin') throw new Error(`cannot take the task-list lock on ${platform}`)
  const flags = fs.constants.O_RDWR | fs.constants.O_CREAT | O_EXLOCK_DARWIN | fs.constants.O_NONBLOCK
  const deadline = Date.now() + waitMs
  let fd
  while (fd === undefined) {
    try {
      fd = fs.openSync(lockPath(), flags, 0o600)
    } catch (e) {
      if (e.code !== 'EAGAIN' && e.code !== 'EWOULDBLOCK') throw e
      if (Date.now() >= deadline) throw new Error(`task-list lock still held by another writer after ${waitMs} ms`)
      await new Promise((resolve) => setTimeout(resolve, retryMs))
    }
  }
  try {
    return fn()
  } finally {
    fs.closeSync(fd) // closing the descriptor releases the lock
  }
}

function writeAtomic(data, rename) {
  data._lastUpdated = nowIso()
  // A name of its own, so no other writer's temporary file is ever this one.
  const tmp = `${todoPath()}.${process.pid}.${randomBytes(4).toString('hex')}.tmp`
  fs.writeFileSync(tmp, JSON.stringify(data, null, 2), { mode: 0o600 })
  try {
    rename(tmp, todoPath())
  } catch (e) {
    fs.rmSync(tmp, { force: true })
    throw e
  }
}

/**
 * Append a captured-thread TODO. De-dups on `source` (the thread identity), so
 * re-reacting the same thread is a no-op. The whole read-modify-write runs under
 * the task-list lock.
 *
 * `autoDispatch` true starts by itself, false is manual only, null means "Ask me".
 *
 * @returns {Promise<{ id: string, created: boolean }>} — created=false means it already existed.
 */
export async function addTodo({ title, detail, url, source, priority = 'P2', autoDispatch = true, reactedBy }, opts = {}) {
  if (!fs.existsSync(todoPath())) {
    throw new Error(`todo store not found: ${todoPath()}`)
  }
  return withTodoLock(() => {
    const data = JSON.parse(fs.readFileSync(todoPath(), 'utf8'))
    const items = (data.items ??= [])

    for (const it of items) {
      if (it.source === source && !['done', 'deferred'].includes(it.status)) {
        return { id: it.id, created: false }
      }
    }

    const id = nextId(data)
    items.push({
      id,
      priority: ['P0', 'P1', 'P2', 'P3', 'P4'].includes(priority) ? priority : 'P2',
      title,
      detail,
      url,
      source,
      createdAt: new Date().toISOString().slice(0, 10),
      status: 'open',
      autoDispatch,
      capturedBy: MACHINE,
      ...(reactedBy ? { reactedBy } : {}),
    })
    writeAtomic(data, opts.rename ?? fs.renameSync)
    return { id, created: true }
  }, opts)
}
