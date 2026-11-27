import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { redact } from '../src/redact.js'

const B = 'AbCdEfGhIjKlMnOpQrStUv0123456789'
// Made-up secrets are built from parts so GitHub's push protection doesn't take them for real ones.
const SECRETS = {
  SLACK_BOT: ['xoxb', '111111111', '222222222', B].join('-'),
  SLACK_USER: ['xoxp', '111111111', B].join('-'),
  SLACK_SESSION: ['xoxc', '111111111', B].join('-'),
  SLACK_APP: ['xapp', '1', 'A0123456789', '9876543210', '0123456789abcdef0123456789abcdef'].join('-'),
  SLACK_ENCODED: ['xoxb', '1111%2F2222%2B3333', B].join('-'),
  SLACK_PCT_PREFIX: ['xoxb', '1111-' + B].join('%2D'),
  GHP: ['ghp', B].join('_'),
  PAT: ['github', 'pat', '11ABCDEFG0', B].join('_'),
  GOOG: ['ya29', 'a0Af_' + B].join('.'),
  REFRESH: ['1', '0g' + B].join('//'),
  GOCSPX: ['GOCSPX', 'abcdefghijklmnopqrstuvwx'].join('-'),
  AWS: ['AK', 'IAIOSFODNN7EXAMPLE'].join(''),
  ANT: ['sk', 'ant', 'api03', B].join('-'),
  SK: ['sk', B].join('-'),
  JWT: ['eyJhbGciOiJSUzI1NiJ9', 'eyJzdWIiOiIxMjM0NTY3ODkwIn0', 'c2lnbmF0dXJlMTIzNDU2'].join('.'),
  WEBHOOK: ['hooks.slack.com', 'services', 'T0AAAA', 'B0BBBB', 'abcdEFGH12345678ijklMNOP'].join('/'),
}
const CASES = JSON.parse(readFileSync(new URL('./fixtures/redact-cases.json', import.meta.url), 'utf8'))
const expand = (text) => text.replace(/\{([A-Z_]+)\}/g, (_, name) => SECRETS[name])

test('redact: every secret shape in the shared case list', () => {
  for (const [name, text, expected] of CASES.redact) {
    assert.equal(redact(expand(text)), expand(expected), name)
    assert.equal(redact(redact(expand(text))), expand(expected), `${name} (twice)`)
  }
})

test('redact: look-alikes and ordinary lines are unchanged', () => {
  for (const line of CASES.keep) assert.equal(redact(line), line)
})

test('redact: the secret part never survives', () => {
  for (const [name, secret] of Object.entries(SECRETS)) {
    const out = redact(`x ${secret} y`)
    assert.ok(!out.includes(B) && !out.includes('abcdEFGH12345678ijklMNOP'), name)
  }
})

test('redact: never throws', () => {
  assert.match(redact({ toString() { throw new Error('boom') } }), /withheld/)
  assert.equal(redact(undefined), 'undefined')
})

test('redact: a hostile megabyte line is fast for every rule', () => {
  const units = [
    'Bearer ', 'Bearer a', 'authorization: ', "authorization: [ '", 'Cookie: ', 'http://a:', 'https://' + 'u'.repeat(200) + ':',
    'hooks.slack.com/services/T/B/', 'token=', "token='", 'token=\\"', 'api_key=', 'password: "', 'client_secret:',
    'A_TOKEN=', 'xoxb-', 'xoxb-1%2F', 'xapp-', 'ghp_', 'github_pat_', 'ya29.', '1//', 'AKIA', 'GOCSPX-', 'sk-ant-',
    'sk-', 'sk-' + '0'.repeat(40) + '/', 'eyJ', 'eyJ' + 'a'.repeat(20) + '.', 'eyJ' + 'a'.repeat(2000) + '.b.', 'a'.repeat(64) + ' ',
    'token' + ' '.repeat(7), 'x'.repeat(40) + '-', ' ', '\\', '%', '-', 'A'.repeat(60) + '_',
  ]
  let slowest = 0
  for (const unit of units) {
    const line = unit.repeat(Math.ceil(1_000_000 / unit.length)).slice(0, 1_000_000)
    const started = Date.now()
    redact(line)
    slowest = Math.max(slowest, Date.now() - started)
  }
  assert.ok(slowest < 2000, `slowest ${slowest} ms`)
})
