// A Slack client with no network. STUB_THREAD is the thread (a JSON list of messages) the replies call returns.
export class WebClient {
  constructor() {
    this.auth = { test: async () => ({ user_id: 'U1', team: 'T', user: 'bot' }) }
    this.conversations = {
      history: async () => ({ messages: [{ ts: '1.1' }] }),
      replies: async () => ({ messages: JSON.parse(process.env.STUB_THREAD ?? '[]') }),
    }
    this.chat = { getPermalink: async () => ({ permalink: 'https://example.slack.com/archives/C1/p11' }) }
    this.reactions = { add: async ({ name }) => console.error(`[stub] reaction added: ${name}`) }
    this.users = { info: async ({ user }) => ({ user: { profile: { display_name: `name-of-${user}` } } }) }
  }
}
