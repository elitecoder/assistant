import test from 'node:test'
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import { spawnSync } from 'node:child_process'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe as describeError, installSafeOutput, render, summarize } from '../src/safe-output.js'
import { BOT_TOKEN, APP_TOKEN, leakyError } from './fixtures/leaky-error.mjs'

const here = path.dirname(fileURLToPath(import.meta.url))
const INDEX = path.join(here, '..', 'src', 'index.js')
const REGISTER = path.join(here, 'fixtures', 'stub-register.mjs')
const PLAIN_SECRET = 'abcdefgh12345678'
const SECRET_PARTS = ['AbCdEfGhIjKlMnOpQrStUvWx', '0123456789abcdef0123456789abcdef', PLAIN_SECRET]

function fakeStream() {
  const chunks = []
  return { chunks, write: (chunk) => (chunks.push(chunk), true) }
}

function fakeWorld() {
  const proc = Object.assign(new EventEmitter(), { stdout: fakeStream(), stderr: fakeStream(), exits: [] })
  proc.exit = (code) => proc.exits.push(code)
  const lines = []
  const con = {}
  for (const m of ['log', 'info', 'debug', 'warn', 'error']) con[m] = (...a) => lines.push([m, ...a])
  return { proc, con, lines }
}

const noSecret = (text) => SECRET_PARTS.every((part) => !text.includes(part))

test('render: null, plain objects and error-like objects', () => {
  assert.equal(render(['value', null, { ok: 1 }]), 'value null { ok: 1 }')
  const like = { message: 'from another realm', stack: 's', config: { headers: { Authorization: `Bearer ${BOT_TOKEN}` } } }
  assert.equal(render([like]), 'Error: from another realm')
})

test('summarize: name, message, code, status, Slack error, real reason, cause and aggregate members', () => {
  const err = leakyError('bad\nthing')
  err.data = { error: 'invalid_auth' }
  err.statusCode = 429
  assert.equal(summarize(err), 'Error: bad thing code=slack_webapi_request_error status=429 data.error=invalid_auth original=ENOTFOUND')
  assert.equal(summarize({ message: undefined, status: 502, original: { message: 'socket hang up' } }), 'Error:  status=502 original=socket hang up')
  const wrapped = new Error('outer', { cause: new Error('inner', { cause: new Error('too deep') }) })
  assert.equal(summarize(wrapped), 'Error: outer cause=[Error: inner]')
  const many = new AggregateError([new Error('first one'), new Error('second')], 'several failed')
  assert.equal(summarize(many), 'AggregateError: several failed errors=2 first=[Error: first one]')
  assert.equal(summarize(new AggregateError(['plain text'], 'x')), 'AggregateError: x errors=1 first=[plain text]')
  assert.equal(summarize(new Error('c', { cause: 'not an error' })), 'Error: c')
})

test('describe: the summary then at most 8 stack frames, with tokens removed everywhere', () => {
  const err = new Error(`failed with token=${PLAIN_SECRET}`)
  err.stack = `Error: failed with token=${PLAIN_SECRET}\n` +
    Array.from({ length: 12 }, (_, i) => `    at fn${i} (/srv/app/index.js:${100 + i}:4)`).join('\n') +
    `\n    at leak (/srv/${BOT_TOKEN}/x.js:1:1)`
  const out = describeError(err)
  const lines = out.split('\n')
  assert.equal(lines.length, 1 + 8)
  assert.equal(lines[1], '    at fn0 (/srv/app/index.js:100:4)')
  assert.ok(noSecret(out), out)
  assert.ok(lines[0].includes('token=<redacted>'))
  const noStack = Object.assign(new Error('plain'), { stack: undefined })
  assert.equal(describeError(noStack), 'Error: plain')
  assert.ok(noSecret(describeError(Object.assign(new Error('x'), { stack: `Error: x\n   at ${BOT_TOKEN}` }))))
})

test('render: a thrown HTTP error object never prints a token', () => {
  const out = render(['[reactor] failed:', leakyError(), { nested: leakyError() }, 'tail'])
  assert.ok(noSecret(out), out)
  assert.ok(out.startsWith('[reactor] failed: Error: A request error occurred'))
  const dump = render([leakyError().config, leakyError().request])
  assert.ok(noSecret(dump), dump)
})

test('console and stream wrappers: tokens are removed, ordinary lines are unchanged', () => {
  const { proc, con, lines } = fakeWorld()
  const rawStderr = proc.stderr
  installSafeOutput(proc, con)
  con.error('ordinary line 100%', 5)
  con.warn(`Authorization: Bearer ${BOT_TOKEN}`)
  con.log(leakyError())
  con.info('a'); con.debug('b')
  assert.deepEqual(lines[0], ['error', 'ordinary line 100% 5'])
  assert.ok(lines.every((l) => noSecret(l.join(' '))))
  assert.equal(lines.length, 5)
  proc.stderr.write(`dump data: 'token=${BOT_TOKEN}'`, 'utf8', () => {})
  proc.stdout.write(Buffer.from(`xapp-${APP_TOKEN.slice(5)}`))
  proc.stdout.write(7) // not text: passed through untouched
  proc.stdout.write('plain\n')
  assert.ok(rawStderr.chunks.concat(proc.stdout.chunks).every((c) => typeof c !== 'string' || noSecret(c)))
  assert.equal(proc.stdout.chunks.at(-1), 'plain\n')
  assert.equal(proc.stdout.chunks.at(-2), 7)
  assert.ok(noSecret(String(proc.stdout.chunks[0])))
})

test('process handlers print a timestamped redacted summary with stack frames and exit non-zero', () => {
  const { proc, con, lines } = fakeWorld()
  installSafeOutput(proc, con)
  const crash = leakyError(`boom token=${PLAIN_SECRET}`)
  crash.stack = `Error: boom\n    at fn (/srv/app/index.js:123:4)\n    at next (node:internal/x:1:1)`
  proc.emit('uncaughtException', crash)
  proc.emit('unhandledRejection', leakyError())
  assert.deepEqual(proc.exits, [1, 1])
  assert.ok(lines.every((l) => noSecret(l.join(' '))))
  assert.match(lines[0][1], /^\d{4}-\d\d-\d\dT[\d:.]+Z \[reactor\] uncaught exception: Error: boom token=<redacted> code=slack_webapi_request_error original=ENOTFOUND\n    at fn \(\/srv\/app\/index\.js:123:4\)/)
  assert.match(lines[1][1], /^\d{4}-.* \[reactor\] unhandled rejection: Error: A request error occurred/)
})

function bootReactor(env = {}) {
  return spawnSync(process.execPath, ['--import', REGISTER, INDEX], {
    env: { PATH: process.env.PATH, SLACK_BOT_TOKEN: BOT_TOKEN, SLACK_APP_TOKEN: APP_TOKEN, ...env },
    encoding: 'utf8',
    timeout: 20000,
  })
}

test('the real reactor: a rejection while starting prints no token and exits non-zero', (t) => {
  const home = path.join(process.env.TMPDIR ?? '/tmp', `reactor-home-${process.pid}`)
  const run = bootReactor({ HOME: home, STUB_START: 'reject' })
  const all = run.stdout + run.stderr
  assert.ok(noSecret(all), all)
  assert.ok(!all.includes(BOT_TOKEN) && !all.includes(APP_TOKEN))
  assert.match(all, /unhandled rejection: Error: A request error occurred code=slack_webapi_request_error original=ENOTFOUND\n\s+at leakyError .*leaky-error\.mjs:\d+:\d+\)\n(?:.*\n)*\s+at .*src\/index\.js:\d+:\d+/)
  assert.equal(run.status, 1)
})

test('the real reactor: Bolt logging a whole error object prints no token; normal boot is unchanged', () => {
  const home = path.join(process.env.TMPDIR ?? '/tmp', `reactor-home-${process.pid}`)
  const run = bootReactor({ HOME: home, STUB_START: 'log' })
  assert.ok(noSecret(run.stdout + run.stderr), run.stderr)
  assert.match(run.stderr, /Bolt logger: Error: A request error occurred/)
  assert.match(run.stderr, /Socket Mode connected/)
  assert.match(run.stderr, /slack-reactor up \(Socket Mode\)/)
})
