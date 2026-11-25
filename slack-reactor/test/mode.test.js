import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { makeModeReader, pythonCandidates, runCommand, MODE_SCRIPT } from '../src/mode.js'

const tmp = () => fs.mkdtempSync(path.join(os.tmpdir(), 'reactor-mode-'))
function homeWith(config) {
  const home = tmp()
  if (config !== undefined) {
    fs.mkdirSync(path.join(home, '.assistant'))
    fs.writeFileSync(path.join(home, '.assistant', 'config.json'), typeof config === 'string' ? config : JSON.stringify(config))
  }
  return home
}

// A stand-in for the interpreter: records each call, answers the version probe and the mode script.
function fake({ answer = { code: 0, stdout: 'pro\n' }, version = { code: 0 }, throws } = {}) {
  const calls = []
  const run = async (file, args) => {
    calls.push([file, ...args])
    if (throws) throw throws
    return args[0] === '-c' ? { stdout: '', ...version } : answer
  }
  return { calls, run }
}
const reader = (config, opts = {}) => {
  const logs = []
  const read = makeModeReader({ home: homeWith(config), env: {}, isExec: () => true, log: (l) => logs.push(l), ...opts })
  return { read, logs }
}

test('a saved "pro" is believed only when bin/assistant-mode.py says pro', async () => {
  for (const [printed, expected] of [['pro\n', 'pro'], ['default\n', 'default'], ['\n', 'default'], ['maybe\n', 'default']]) {
    const f = fake({ answer: { code: 0, stdout: printed } })
    assert.equal(await reader({ mode: 'pro' }, { run: f.run }).read(), expected, printed)
    assert.deepEqual(f.calls.at(-1), ['/opt/homebrew/bin/python3', MODE_SCRIPT])
  }
})

test('a saved "pro" with a command that fails or times out is the default mode, said once', async () => {
  const { read, logs } = reader({ mode: 'pro' }, { run: fake({ answer: { code: null, stdout: '' } }).run, now: () => 0 })
  assert.equal(await read(), 'default')
  assert.equal(await read(), 'default')
  assert.equal(logs.length, 1)
})

test('a saved "pro" the command confirms is asked once a minute, not once a reaction', async () => {
  const f = fake()
  const { read } = reader({ mode: 'pro' }, { run: f.run, now: () => 5 })
  assert.equal(await read(), 'pro')
  assert.equal(await read(), 'pro')
  assert.equal(f.calls.filter((c) => c[1] === MODE_SCRIPT).length, 1)
})

test('a file Python cannot read but that says "pro" is the default mode when Python says so', async () => {
  const stubSaysDefault = fake({ answer: { code: 0, stdout: 'default\n' } }) // what Python really prints for these
  const invalidUtf8 = Buffer.concat([Buffer.from('{"mode":"pro","note":"'), Buffer.from([0xff, 0xfe]), Buffer.from('"}')])
  const huge = `{"mode":"pro","n":${'9'.repeat(5000)}}`
  const deep = `{"mode":"pro","d":${'['.repeat(5000)}${']'.repeat(5000)}}`
  for (const body of [invalidUtf8, huge, deep]) {
    const home = homeWith()
    fs.mkdirSync(path.join(home, '.assistant'))
    fs.writeFileSync(path.join(home, '.assistant', 'config.json'), body)
    const read = makeModeReader({ home, env: {}, run: stubSaysDefault.run, isExec: () => true, now: () => 0 })
    assert.equal(await read(), 'default')
  }
  assert.ok(stubSaysDefault.calls.some((c) => c[1] === MODE_SCRIPT)) // it was asked, and its answer was taken
})

test('any other saved value is the default mode, a known mode or not, and no interpreter is asked', async () => {
  const f = fake()
  for (const mode of ['default', 'Pro', 'expert', '', 0, false, ['pro'], {}]) {
    assert.equal(await reader({ mode }, { run: f.run }).read(), 'default', JSON.stringify(mode))
  }
  assert.deepEqual(f.calls, [])
})

test('a config that cannot be read or parsed is the default mode, and no interpreter is asked', async () => {
  const f = fake()
  for (const config of ['not json', '[]', 'null', '"pro"', '']) {
    assert.equal(await reader(config, { run: f.run }).read(), 'default', config)
  }
  const unreadable = homeWith()
  fs.mkdirSync(path.join(unreadable, '.assistant', 'config.json'), { recursive: true }) // a folder where the file should be
  assert.equal(await makeModeReader({ home: unreadable, env: {}, run: f.run, isExec: () => true })(), 'default')
  assert.deepEqual(f.calls, [])
})

test('with no saved mode, the answer is what bin/assistant-mode.py prints', async () => {
  for (const config of [undefined, {}, { mode: null }, { slack: {} }]) {
    for (const printed of ['pro', 'default']) {
      const f = fake({ answer: { code: 0, stdout: `${printed}\n` } })
      assert.equal(await reader(config, { run: f.run }).read(), printed)
      assert.deepEqual(f.calls.at(-1), ['/opt/homebrew/bin/python3', MODE_SCRIPT])
    }
  }
})

test('the script is the one in this repository', () => {
  assert.ok(MODE_SCRIPT.endsWith(path.join('bin', 'assistant-mode.py')) && fs.existsSync(MODE_SCRIPT))
})

test('a command that fails, prints something else, cannot start or times out is the default mode, said once', async () => {
  const cases = [
    { code: 1, stdout: 'pro\n' },
    { code: null, stdout: '' }, // could not start, or timed out
    { code: 0, stdout: 'Traceback: something' },
    { code: 0, stdout: '' },
  ]
  for (const answer of cases) {
    const { read, logs } = reader(undefined, { run: fake({ answer }).run, now: () => 0 })
    assert.equal(await read(), 'default')
    assert.equal(await read(), 'default')
    assert.equal(logs.length, 1)
    assert.match(logs[0], /^\[reactor\] couldn't tell whether this is Pro mode \(.*\), so it is treated as the default mode$/)
  }
  const thrown = reader(undefined, { run: fake({ throws: new Error('spawn broke') }).run })
  assert.equal(await thrown.read(), 'default')
  assert.match(thrown.logs[0], /spawn broke/)
})

test('the line is said once even when the answer is asked for again after the cache runs out', async () => {
  let t = 0
  const { read, logs } = reader(undefined, { run: fake({ answer: { code: 1, stdout: '' } }).run, now: () => t })
  await read()
  t += 61000
  await read()
  assert.equal(logs.length, 1)
})

test('the answer is kept for a minute, a burst costs one call, and then it is asked again', async () => {
  let t = 1000
  const f = fake()
  const { read } = reader(undefined, { run: f.run, now: () => t })
  const burst = await Promise.all([read(), read(), read()])
  assert.deepEqual(burst, ['pro', 'pro', 'pro'])
  const scriptCalls = () => f.calls.filter((c) => c[1] === MODE_SCRIPT).length
  assert.equal(scriptCalls(), 1)
  t += 59000
  await read()
  assert.equal(scriptCalls(), 1)
  f.calls.length = 0
  t += 2000
  await read()
  assert.equal(scriptCalls(), 1)
})

test('a saved mode that changes from pro to anything else takes effect on the next reaction, from the cache or not', async () => {
  const home = homeWith({ mode: 'pro' })
  const f = fake()
  const read = makeModeReader({ home, env: {}, run: f.run, isExec: () => true, now: () => 0 })
  assert.equal(await read(), 'pro')
  fs.writeFileSync(path.join(home, '.assistant', 'config.json'), JSON.stringify({ mode: 'default' }))
  assert.equal(await read(), 'default')
  fs.writeFileSync(path.join(home, '.assistant', 'config.json'), 'broken')
  assert.equal(await read(), 'default')
  assert.equal(f.calls.filter((c) => c[1] === MODE_SCRIPT).length, 1)
})

test('a change in the saved mode is seen at once, without waiting out the cache', async () => {
  const home = homeWith({})
  const f = fake()
  const read = makeModeReader({ home, env: {}, run: f.run, isExec: () => true, now: () => 0 })
  assert.equal(await read(), 'pro')
  fs.writeFileSync(path.join(home, '.assistant', 'config.json'), JSON.stringify({ mode: 'default' }))
  assert.equal(await read(), 'default')
})

test('the interpreter is ASSISTANT_PYTHON when set, else the candidates install.sh tries, else python3 on PATH', async () => {
  assert.deepEqual(pythonCandidates({ ASSISTANT_PYTHON: '/a/py::/b/py' }), ['/a/py', '/b/py'])
  assert.deepEqual(pythonCandidates({}), ['/opt/homebrew/bin/python3', '/usr/local/bin/python3', '/usr/bin/python3'])
  const only = (ok) => (p) => ok.includes(p)
  // the first one of 3.11 or newer wins
  let f = fake({ version: { code: 0 } })
  await reader(undefined, { run: f.run, env: { ASSISTANT_PYTHON: '/a/py:/b/py' }, isExec: only(['/b/py']) }).read()
  assert.deepEqual(f.calls.at(-1), ['/b/py', MODE_SCRIPT])
  // none new enough: the first one found
  f = fake({ version: { code: 1 } })
  await reader(undefined, { run: f.run, env: { ASSISTANT_PYTHON: '/a/py:/b/py' }, isExec: () => true }).read()
  assert.deepEqual(f.calls.map((c) => c[0]), ['/a/py', '/b/py', '/a/py'])
  // none at all: python3 on PATH
  f = fake()
  await reader(undefined, { run: f.run, env: { ASSISTANT_PYTHON: '/a/py' }, isExec: () => false }).read()
  assert.deepEqual(f.calls, [['python3', MODE_SCRIPT]])
})

test('the real runner: a command that works, fails, is missing or runs too long', async () => {
  assert.deepEqual(await runCommand(process.execPath, ['-e', 'console.log("pro")']), { code: 0, stdout: 'pro\n' })
  assert.equal((await runCommand(process.execPath, ['-e', 'process.exit(3)'])).code, 3)
  assert.equal((await runCommand('/no/such/python', [])).code, null)
  assert.equal((await runCommand(process.execPath, ['-e', 'setTimeout(()=>{}, 60000)'], 300)).code, null)
})

test('the default interpreter probe runs for real, and an unexecutable path is not a candidate', async () => {
  const home = homeWith({})
  const dir = tmp()
  const py = path.join(dir, 'py')
  fs.writeFileSync(py, '#!/bin/sh\nif [ "$1" = "-c" ]; then exit 0; fi\necho pro\n', { mode: 0o755 })
  const plain = path.join(dir, 'plain')
  fs.writeFileSync(plain, 'x', { mode: 0o644 })
  const read = makeModeReader({ home, env: { ASSISTANT_PYTHON: `${plain}:${py}` } })
  assert.equal(await read(), 'pro')
})
