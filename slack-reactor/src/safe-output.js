// Keep tokens out of this process's stdout and stderr.
// Node prints a whole HTTP error object (headers, request body, raw request) when
// something logs or throws one; that is how the bot token reached the launchd
// log. Three layers: console.* renders an error as a summary plus a few stack frames and redacts
// everything; the stream writes redact whatever else gets through; the process
// handlers print a summary of a crash instead of Node's full dump.
import { format } from 'node:util'
import { redact } from './redact.js'

const METHODS = ['log', 'info', 'debug', 'warn', 'error']

const CRASH_FRAMES = 8
const CONSOLE_FRAMES = 3
const MAX_LINE = 2000
const UNPRINTABLE = '<unprintable error>'
// Cut before cleaning: a 5 MB message must not be written whole.
const clip = (text) => (text.length > MAX_LINE ? `${text.slice(0, MAX_LINE)}…` : text)
const oneLine = (text) => clip(String(text ?? '')).replace(/\s+/g, ' ')
const isError = (v) => v instanceof Error || (v !== null && typeof v === 'object' && typeof v.message === 'string' && 'stack' in v)

// A safe one-line view of an error: name, message, code, HTTP status, Slack's
// data.error, the real reason a request failed (original.code, such as ENOTFOUND
// or ETIMEDOUT) and one level of cause. Never the attached request or headers.
export function summarize(err, nested = false) {
  const parts = [`${err.name ?? 'Error'}: ${oneLine(err.message)}`]
  if (err.code !== undefined) parts.push(`code=${oneLine(err.code)}`)
  const status = err.statusCode ?? err.status
  if (status !== undefined) parts.push(`status=${oneLine(status)}`)
  if (err.data?.error) parts.push(`data.error=${oneLine(err.data.error)}`)
  const reason = err.original?.code ?? err.original?.message
  if (reason !== undefined) parts.push(`original=${oneLine(reason)}`)
  if (!nested && isError(err.cause)) parts.push(`cause=[${summarize(err.cause, true)}]`)
  if (!nested && err instanceof AggregateError) {
    const first = err.errors[0]
    parts.push(`errors=${err.errors.length} first=[${isError(first) ? summarize(first, true) : oneLine(first)}]`)
  }
  return redact(clip(parts.join(' ')))
}

// The summary, then up to `frames` stack frames (the "at ..." lines): enough to
// find the failing line. A V8 stack is the message plus frames, so none of the
// attached request objects come with it. If anything about the error refuses to
// print (a Symbol code, a getter that throws), say so rather than lose the line
// or let Node dump the object.
export function describe(err, frames = CRASH_FRAMES) {
  try {
    const at = String(err.stack ?? '').split('\n').filter((line) => /^\s*at /.test(line)).slice(0, frames)
    return [summarize(err), ...at.map((frame) => redact(clip(frame.trimEnd())))].join('\n')
  } catch {
    return UNPRINTABLE
  }
}

// console.* gets the summary and a few frames: Bolt may log handled errors often.
export function render(args) {
  try {
    return redact(format(...args.map((a) => {
      try {
        return isError(a) ? describe(a, CONSOLE_FRAMES) : a
      } catch {
        return UNPRINTABLE
      }
    })))
  } catch {
    return UNPRINTABLE
  }
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
  // Exit non-zero so launchd restarts the service, as it did before, even if printing fails.
  const crash = (label, value) => {
    let text
    try {
      text = isError(value) ? describe(value) : render([value])
    } catch {
      text = UNPRINTABLE
    }
    try {
      con.error(`${new Date().toISOString()} [reactor] ${label}: ${text}`)
    } catch {
      /* the exit below is what matters */
    }
    proc.exit(1)
  }
  proc.on('uncaughtException', (err) => crash('uncaught exception', err))
  proc.on('unhandledRejection', (reason) => crash('unhandled rejection', reason))
}
