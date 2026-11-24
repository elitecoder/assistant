import test from 'node:test'
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import { spawnSync } from 'node:child_process'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { installSafeOutput, render, summarize } from '../src/safe-output.js'
import { redact } from '../src/redact.js'
import { BOT_TOKEN, APP_TOKEN, leakyError } from './fixtures/leaky-error.mjs'

const here = path.dirname(fileURLToPath(import.meta.url))
const INDEX = path.join(here, '..', 'src', 'index.js')
const REGISTER = path.join(here, 'fixtures', 'stub-register.mjs')
const SECRET_PARTS = ['AbCdEfGhIjKlMnOpQrStUvWx', '0123456789abcdef0123456789abcdef']

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

test('redact: every token shape', () => {
  assert.equal(redact(`xoxb-123456-abcdefgh`), 'xoxb-<redacted>')
  assert.equal(redact(`xoxp-123456-abcdefgh`), 'xoxp-<redacted>')
  assert.equal(redact(`xapp-1-A0123-abcdef`), 'xapp-<redacted>')
  assert.equal(redact('Authorization: Bearer abc.def-ghi_jkl'), 'Authorization: <redacted>')
  assert.equal(redact("Authorization: 'Bearer xoxb-123456-abcdef',"), "Authorization: '<redacted>',")
  assert.equal(redact('Authorization: Bearer xoxb-123456-abcdef\\r\\nHost: slack.com'), 'Authorization: <redacted>\\r\\nHost: slack.com')
  assert.equal(redact('call with Bearer abcdefghijkl1234'), 'call with Bearer <redacted>')
  assert.equal(redact("data: 'token=xoxb-123456-abcdef&channel=C1',"), "data: 'token=<redacted>&channel=C1',")
  assert.equal(redact('access_token=abc123&x=1'), 'access_token=<redacted>&x=1')
  assert.equal(redact('{"refresh_token": "r-123", "ok": true}'), '{"refresh_token": "<redacted>", "ok": true}')
  assert.equal(redact('client_secret=shh'), 'client_secret=<redacted>')
  assert.equal(redact("token: 'abc'"), "token: '<redacted>'")
})

test('redact: look-alikes and ordinary lines are unchanged', () => {
  for (const line of [
    'the token expired, ask for a new token',
    'token: expired',
    'commit 3f786850e387550fdab836ed7e6dc881de23001b',
    'id 123e4567-e89b-12d3-a456-426614174000',
    'https://example.com/a/b?x=1&y=2',
    'max_tokens=100 input_tokens=5',
    '[2026-01-02T03:04:05.000Z] :mukuls2: -> td-1 (new) on laptop: fix the thing',
  ]) assert.equal(redact(line), line)
})

test('redact: never throws and a hostile megabyte line is fast', () => {
  assert.match(redact({ toString() { throw new Error('boom') } }), /withheld/)
  const started = Date.now()
  for (const unit of ['Bearer ', 'token=', 'xoxb-', 'authorization: ', 'a'.repeat(64) + ' ', 'token' + ' '.repeat(7)]) {
    redact(unit.repeat(Math.ceil(1_000_000 / unit.length)))
  }
  assert.ok(Date.now() - started < 5000, `took ${Date.now() - started} ms`)
})

test('render: null, plain objects and error-like objects', () => {
  assert.equal(render(['value', null, { ok: 1 }]), 'value null { ok: 1 }')
  const like = { message: 'from another realm', stack: 's', config: { headers: { Authorization: `Bearer ${BOT_TOKEN}` } } }
  assert.equal(render([like]), 'Error: from another realm')
})

test('summarize: name, message, code and Slack data.error only', () => {
  const err = leakyError('bad\nthing')
  err.data = { error: 'invalid_auth' }
  const line = summarize(err)
  assert.equal(line, 'Error: bad thing code=slack_webapi_request_error data.error=invalid_auth')
  assert.equal(summarize({ message: undefined }), 'Error: ')
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

test('process handlers print a redacted summary and exit non-zero', () => {
  const { proc, con, lines } = fakeWorld()
  installSafeOutput(proc, con)
  proc.emit('uncaughtException', leakyError())
  proc.emit('unhandledRejection', leakyError())
  assert.deepEqual(proc.exits, [1, 1])
  assert.ok(lines.every((l) => noSecret(l.join(' '))))
  assert.match(lines[0][1], /uncaught exception: Error: A request error occurred/)
  assert.match(lines[1][1], /unhandled rejection: Error: A request error occurred/)
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
  assert.match(all, /unhandled rejection: Error: A request error occurred/)
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
