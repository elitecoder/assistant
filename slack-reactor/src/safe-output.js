// Keep tokens out of this process's stdout and stderr.
// Node prints a whole HTTP error object (headers, request body, raw request) when
// something logs or throws one; that is how the bot token reached the launchd
// log. Three layers: console.* renders an error as a summary plus a few stack frames and redacts
// everything; the stream writes redact whatever else gets through; the process
// handlers print a summary of a crash instead of Node's full dump.
import { format } from 'node:util'
import { redact } from './redact.js'

const METHODS = ['log', 'info', 'debug', 'warn', 'error']

const MAX_FRAMES = 8
const oneLine = (text) => String(text ?? '').replace(/\s+/g, ' ')
const isError = (v) => v instanceof Error || (v !== null && typeof v === 'object' && typeof v.message === 'string' && 'stack' in v)

// A safe one-line view of an error: name, message, code, HTTP status, Slack's
// data.error, the real reason a request failed (original.code, such as ENOTFOUND
// or ETIMEDOUT) and one level of cause. Never the attached request or headers.
export function summarize(err, nested = false) {
  const parts = [`${err.name ?? 'Error'}: ${oneLine(err.message)}`]
  if (err.code !== undefined) parts.push(`code=${err.code}`)
  const status = err.statusCode ?? err.status
  if (status !== undefined) parts.push(`status=${status}`)
  if (err.data?.error) parts.push(`data.error=${err.data.error}`)
  const reason = err.original?.code ?? err.original?.message
  if (reason !== undefined) parts.push(`original=${oneLine(reason)}`)
  if (!nested && isError(err.cause)) parts.push(`cause=[${summarize(err.cause, true)}]`)
  if (!nested && err instanceof AggregateError) {
    const first = err.errors[0]
    parts.push(`errors=${err.errors.length} first=[${isError(first) ? summarize(first, true) : oneLine(first)}]`)
  }
  return redact(parts.join(' '))
}

// The summary, then up to 8 stack frames (the "at ..." lines): enough to find the
// failing line. A V8 stack is the message plus frames, so none of the attached
// request objects come with it.
export function describe(err) {
  const frames = String(err.stack ?? '').split('\n').filter((line) => /^\s*at /.test(line)).slice(0, MAX_FRAMES)
  return [summarize(err), ...frames.map((frame) => redact(frame.trimEnd()))].join('\n')
}

export function render(args) {
  return redact(format(...args.map((a) => (isError(a) ? describe(a) : a))))
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
    con.error(`${new Date().toISOString()} [reactor] uncaught exception:`, err)
    proc.exit(1)
  })
  proc.on('unhandledRejection', (reason) => {
    con.error(`${new Date().toISOString()} [reactor] unhandled rejection:`, reason)
    proc.exit(1)
  })
}
