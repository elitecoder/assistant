// The shape of the error Slack's HTTP client throws: the request config (with
// the Authorization header and the form body) and the raw request ride along.
// Built from parts so GitHub's secret scanning doesn't take this made-up value for a real token.
export const BOT_TOKEN = ['xoxb', '1111111111', '2222222222', 'AbCdEfGhIjKlMnOpQrStUvWx'].join('-')
export const APP_TOKEN = 'xapp-1-A0123456789-9876543210-0123456789abcdef0123456789abcdef'

export function leakyError(message = 'A request error occurred') {
  const err = new Error(message)
  err.code = 'slack_webapi_request_error'
  err.original = {
    config: {
      headers: { Authorization: `Bearer ${BOT_TOKEN}`, 'Content-Type': 'application/x-www-form-urlencoded' },
      data: `token=${BOT_TOKEN}&channel=C1`,
    },
    request: { _header: `POST /api/auth.test HTTP/1.1\r\nAuthorization: Bearer ${BOT_TOKEN}\r\nHost: slack.com\r\n\r\n` },
  }
  err.config = err.original.config
  err.request = err.original.request
  return err
}
