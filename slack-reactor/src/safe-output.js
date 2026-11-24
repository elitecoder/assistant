// Make it impossible for a token to reach this process's stdout or stderr.
// Node prints a whole HTTP error object (headers, request body, raw request) when
// something logs or throws one; that is how the bot token reached the launchd
// log. Three layers: console.* renders errors as a one-line summary and redacts
// everything; the stream writes redact whatever else gets through; the process
// handlers print a summary of a crash instead of Node's full dump.
import { format } from 'node:util'
import { redact } from './redact.js'

const METHODS = ['log', 'info', 'debug', 'warn', 'error']

// A safe one-line view of an error: name, message, code, and Slack's data.error.
export function summarize(err) {
  const parts = [`${err.name ?? 'Error'}: ${String(err.message ?? '').replace(/\s+/g, ' ')}`]
  if (err.code !== undefined) parts.push(`code=${err.code}`)
  if (err.data?.error) parts.push(`data.error=${err.data.error}`)
  return redact(parts.join(' '))
}

const isError = (v) => v instanceof Error || (v !== null && typeof v === 'object' && typeof v.message === 'string' && 'stack' in v)

export function render(args) {
  return redact(format(...args.map((a) => (isError(a) ? summarize(a) : a))))
}

function redactWrite(stream) {
  const write = stream.write.bind(stream)
  stream.write = (chunk, ...rest) => write(
    typeof chunk === 'string' ? redact(chunk) : chunk instanceof Uint8Array ? redact(Buffer.from(chunk).toString('utf8')) : chunk,
    ...rest,
  )
}

export function installSafeOutput(proc = process, con = console) {
  redactWrite(proc.stdout)
  redactWrite(proc.stderr)
  for (const method of METHODS) {
    const original = con[method].bind(con)
    con[method] = (...args) => original(render(args))
  }
  // Exit non-zero so launchd restarts the service, as it did before.
  proc.on('uncaughtException', (err) => {
    con.error('[reactor] uncaught exception:', err)
    proc.exit(1)
  })
  proc.on('unhandledRejection', (reason) => {
    con.error('[reactor] unhandled rejection:', reason)
    proc.exit(1)
  })
}
