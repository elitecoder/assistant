// Replace token-shaped strings with <redacted> so a secret can't reach a log.
// Every pattern is bounded and linear: a hostile megabyte line stays fast.
// Keep in step with src/assistant/redact.py (the Python twin).
export const PLACEHOLDER = '<redacted>'

const NOT_ALNUM = '(?<![A-Za-z0-9])'
const VALUE = '[^\\s&"\',;\\\\]{1,512}'

const RULES = [
  // Header values: the whole value, with or without a scheme word.
  [new RegExp(`(authorization["']?[ \\t]{0,8}[:=][ \\t]{0,8}["']?)(?:(?:bearer|basic|token)[ \\t]{1,8})?${VALUE}`, 'gi'), `$1${PLACEHOLDER}`],
  [/\b(bearer[ \t]{1,8})[A-Za-z0-9._~+/=-]{8,512}/gi, `$1${PLACEHOLDER}`],
  // Slack bot, user, app and legacy tokens.
  [new RegExp(`${NOT_ALNUM}(xox[a-z]-|xapp-)[A-Za-z0-9-]{6,300}`, 'g'), `$1${PLACEHOLDER}`],
  // Named fields in form bodies, JSON and Node's object dumps. A bare
  // "token: word" in prose stays.
  [new RegExp(`${NOT_ALNUM}((?:access_|refresh_|id_)?token|client_secret)(["']?[ \\t]{0,8}=[ \\t]{0,8}["']?|["']?[ \\t]{0,8}:[ \\t]{0,8}["'])${VALUE}`, 'g'), `$1$2${PLACEHOLDER}`],
]

export function redact(text) {
  try {
    let out = typeof text === 'string' ? text : String(text)
    for (const [pattern, replacement] of RULES) out = out.replace(pattern, replacement)
    return out
  } catch {
    return '<log text withheld: redaction failed>'
  }
}
