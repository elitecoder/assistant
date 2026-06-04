// TODO store writer — mirrors the /todo skill schema + atomic write.
// Single source of truth: ~/.claude/assistant-todo.json (the dashboard + pulse
// read the same file).
import { readFileSync, writeFileSync, renameSync, existsSync } from 'node:fs'
import { homedir, hostname } from 'node:os'
import { join } from 'node:path'

export const TODO_PATH = join(homedir(), '.claude', 'assistant-todo.json')
export const MACHINE = hostname()

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

function writeAtomic(data) {
  data._lastUpdated = nowIso()
  const tmp = TODO_PATH + '.tmp'
  writeFileSync(tmp, JSON.stringify(data, null, 2))
  renameSync(tmp, TODO_PATH)
}

/**
 * Append a captured-thread TODO. De-dups on `source` (the thread identity), so
 * re-reacting the same thread is a no-op.
 *
 * @returns {{ id: string, created: boolean }} — created=false means it already existed.
 */
export function addTodo({ title, detail, url, source, priority = 'P2', autoDispatch = true }) {
  if (!existsSync(TODO_PATH)) {
    throw new Error(`todo store not found: ${TODO_PATH}`)
  }
  const data = JSON.parse(readFileSync(TODO_PATH, 'utf8'))
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
  })
  writeAtomic(data)
  return { id, created: true }
}
