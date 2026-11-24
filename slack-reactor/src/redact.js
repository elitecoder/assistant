// Replace token-shaped strings with <redacted> so a secret can't reach a log.
// Every pattern is bounded and linear: a hostile megabyte line stays fast.
// The rules are the same as src/assistant/redact.py (the Python twin): a test
// compares the patterns and runs one shared list of cases through both.
export const PLACEHOLDER = '<redacted>'
const R = String.raw

const NA = R`(?<![A-Za-z0-9])`
const VAL = R`[^ \t\r\n\f\v&\"',;\\<]`
// Something that looks like a credential, not a word: 16+ characters holding a digit, or a known prefix.
const CRED = R`(?:(?=[A-Za-z0-9._~+/=-]{0,512}[0-9])[A-Za-z0-9._~+/=-]{16,512}` +
  R`|(?:xox[a-z]-|xapp-|gh[pousr]_|github_pat_|ya29\.|eyJ|sk-|AKIA|ASIA)[A-Za-z0-9._~+/=%-]{4,512})`
// After a header or field name: an optional (escaped) quote, the separator, an optional array bracket and quote.
const HEADER = R`[ \t]{0,8}[:=][ \t]{0,8}(?:\[[ \t]{0,8})?\\?[\"']?)`
const NAMES = R`(?:(?:access_|refresh_|id_)?token|client_secret|password|passwd|api[_-]?key)`

// [pattern, flags, replacement]. \1 and \2 keep the text that says what the value was.
export const SPECS = [
  // Headers.
  [R`(authorization\\?[\"']?` + HEADER + R`(?:(?:bearer|basic|token|digest)[ \t]{1,8}[^ \t\r\n\f\v\"',;\\\]]{1,512}|` + CRED + ')',
    'i', '\\1<redacted>'],
  [R`(?<![A-Za-z0-9_])(bearer[ \t]{1,8})` + CRED, 'i', '\\1<redacted>'],
  [R`(?<![A-Za-z0-9_])((?:set-)?cookie\\?[\"']?` + HEADER + R`(?=[A-Za-z0-9_.-]{1,64}=)[^\r\n\"'\\\]]{1,1024}`, 'i', '\\1<redacted>'],
  // https://user:password@host keeps the host.
  [R`(?<![A-Za-z0-9_])([a-z][a-z0-9+.-]{1,20}://)[^ \t\r\n\f\v/:@\"']{1,256}:[^ \t\r\n\f\v/@\"']{1,256}@`, 'i', '\\1<redacted>@'],
  // Slack incoming webhook: the last part is the secret.
  [R`(hooks\.slack\.com/services/[A-Za-z0-9]{1,24}/[A-Za-z0-9]{1,24}/)[A-Za-z0-9]{8,64}`, '', '\\1<redacted>'],
  // Named fields in query strings, form bodies, JSON (also escaped JSON) and Node's object dumps.
  // A quoted value runs to its closing quote, spaces and all.
  [NA + '(' + NAMES + R`)(\\?[\"']?[ \t]{0,8}[=:][ \t]{0,8}\\?[\"'])[^\"'\\\r\n]{1,512}`, 'i', '\\1\\2<redacted>'],
  // Unquoted. A bare `token` keeps short values (token=12345 in a model log line is a count).
  [NA + R`((?:access_|refresh_)token|client_secret|password|passwd|api[_-]?key)(\\?[\"']?[ \t]{0,8}=[ \t]{0,8})` + VAL + '{1,512}',
    'i', '\\1\\2<redacted>'],
  [NA + R`((?:id_)?token)(\\?[\"']?[ \t]{0,8}=[ \t]{0,8})` + VAL + '{8,512}', 'i', '\\1\\2<redacted>'],
  [NA + R`(client_secret|api[_-]?key)(\\?[\"']?[ \t]{0,8}:[ \t]{0,8})` + VAL + '{1,512}', 'i', '\\1\\2<redacted>'],
  // Environment dumps: SOME_TOKEN=value.
  [R`(?<![A-Za-z0-9_])([A-Z][A-Z0-9_]{0,60}_(?:TOKEN|SECRET|PASSWORD|API_KEY)=)[^ \t\r\n\f\v<\"'][^ \t\r\n\f\v<]{0,511}`, '', '\\1<redacted>'],
  // Token shapes, keeping the prefix.
  [NA + R`(xox[a-z](?:-|%2[dD])|xapp(?:-|%2[dD]))(?:[A-Za-z0-9-]|%[0-9A-Fa-f]{2}){6,300}`, '', '\\1<redacted>'],
  [NA + R`(gh[pousr]_)[A-Za-z0-9]{20,255}`, '', '\\1<redacted>'],
  [NA + R`(github_pat_)[A-Za-z0-9_]{20,255}`, '', '\\1<redacted>'],
  [NA + R`(ya29\.)[A-Za-z0-9_.-]{20,2048}`, '', '\\1<redacted>'],
  [R`(?<![A-Za-z0-9/:.])(1//)[A-Za-z0-9_-]{20,512}`, '', '\\1<redacted>'],
  [NA + R`(AKIA|ASIA)[A-Z0-9]{16}(?![A-Za-z0-9])`, '', '\\1<redacted>'],
  [NA + R`(GOCSPX-)[A-Za-z0-9_-]{16,64}`, '', '\\1<redacted>'],
  // Model keys: not after a letter, digit, hyphen, slash, dot or underscore (task-sk-..., a path), and a real key holds a digit.
  [R`(?<![A-Za-z0-9/._-])(sk-ant-)[A-Za-z0-9_-]{16,300}`, '', '\\1<redacted>'],
  [R`(?<![A-Za-z0-9/._-])(sk-)(?=[A-Za-z0-9_-]{0,300}[0-9])[A-Za-z0-9_-]{32,300}`, '', '\\1<redacted>'],
  // A JWT on its own (Microsoft Graph access tokens are JWTs). The lookbehind keeps a long run
  // from being rescanned from every eyJ inside it.
  [R`(?<![A-Za-z0-9_-])(eyJ)[A-Za-z0-9_-]{8,2048}\.[A-Za-z0-9_-]{4,4096}\.[A-Za-z0-9_-]{4,2048}`, '', '\\1<redacted>'],
]

const RULES = SPECS.map(([pattern, flags, template]) => [
  new RegExp(pattern, `g${flags}`),
  (...match) => template.replace(/\\(\d)/g, (_, n) => match[Number(n)]),
])

export function redact(text) {
  try {
    let out = typeof text === 'string' ? text : String(text)
    for (const [pattern, replacer] of RULES) out = out.replace(pattern, replacer)
    return out
  } catch {
    return '<log text withheld: redaction failed>'
  }
}
