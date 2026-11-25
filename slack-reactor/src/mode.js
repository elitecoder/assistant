// Default or Pro mode, as src/assistant/mode.py current() decides it. The reactor asks when it handles a
// reaction, not only at start: the mode can change while it runs.
//
// The reactor is in Pro mode only when bin/assistant-mode.py prints `pro`. Python reads the config with its own
// quirks (a file it can't decode, a huge integer or deep nesting makes it answer default though the file says
// "pro"), and those are not copied here. This file's own reading of ~/.assistant/config.json can only say
// "default" (a saved mode other than "pro", a file it can't read or parse, anything but an object: no process is
// run) or "ask Python" (the saved mode is "pro", or the key is absent or null). Anything that goes wrong is the
// default mode.
import fs from 'node:fs'
import { execFile } from 'node:child_process'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

const CACHE_MS = 60 * 1000 // a burst of reactions costs one answer from Python
const TIMEOUT_MS = 5000
export const MODE_SCRIPT = fileURLToPath(new URL('../../bin/assistant-mode.py', import.meta.url))

// The interpreters install.sh tries, in its order (ASSISTANT_PYTHON, colon-separated, replaces the list).
// It also tries `brew --prefix`/bin first; the two Homebrew locations that names are in the list anyway.
export const pythonCandidates = (env) =>
  env.ASSISTANT_PYTHON
    ? env.ASSISTANT_PYTHON.split(':').filter(Boolean)
    : ['/opt/homebrew/bin/python3', '/usr/local/bin/python3', '/usr/bin/python3']

const isExecutable = (p) => {
  try {
    fs.accessSync(p, fs.constants.X_OK)
    return true
  } catch {
    return false
  }
}

// Run a command; resolves { code, stdout } and never rejects. code is null when it could not start or timed out.
export const runCommand = (file, args, timeout = TIMEOUT_MS) =>
  new Promise((resolve) => {
    execFile(file, args, { timeout, encoding: 'utf8' }, (error, stdout) => {
      resolve({ code: error ? (typeof error.code === 'number' ? error.code : null) : 0, stdout: String(stdout) })
    })
  })

// What Python says the mode is: the first interpreter of 3.11 or newer, else the first one found,
// else python3 on PATH, asked to print the mode.
async function askPython({ env, run, isExec, script }) {
  const found = pythonCandidates(env).filter(isExec)
  let python = found[0] ?? 'python3'
  for (const candidate of found) {
    if ((await run(candidate, ['-c', 'import sys; sys.exit(sys.version_info < (3, 11))'])).code === 0) {
      python = candidate
      break
    }
  }
  const answer = await run(python, [script])
  const mode = answer.stdout.trim()
  if (answer.code !== 0) throw new Error(`${python} ${script} did not finish cleanly`)
  if (mode !== 'pro' && mode !== 'default') throw new Error(`${script} printed something unexpected`)
  return mode
}

/**
 * @returns {() => Promise<'pro' | 'default'>} never rejects. `run`, `isExec`, `now` and `log` can be replaced in tests.
 */
export function makeModeReader({
  home, env = process.env, run = runCommand, isExec = isExecutable, now = Date.now, log = console.error, script = MODE_SCRIPT,
}) {
  let cached = null // { at, answer: Promise }
  let told = false
  const heuristic = () => {
    if (!cached || now() - cached.at >= CACHE_MS) {
      const answer = askPython({ env, run, isExec, script }).catch((e) => {
        if (!told) {
          told = true
          log(`[reactor] couldn't tell whether this is Pro mode (${e.message}), so it is treated as the default mode`)
        }
        return 'default'
      })
      cached = { at: now(), answer }
    }
    return cached.answer
  }
  return async () => {
    let value
    try {
      const config = JSON.parse(fs.readFileSync(join(home, '.assistant', 'config.json'), 'utf8'))
      if (config === null || typeof config !== 'object' || Array.isArray(config)) return 'default'
      value = config.mode
    } catch (e) {
      if (e.code !== 'ENOENT') return 'default' // unreadable or not JSON
    }
    if (value === undefined || value === null || value === 'pro') return heuristic()
    return 'default'
  }
}
