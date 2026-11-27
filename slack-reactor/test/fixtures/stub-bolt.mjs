import { EventEmitter } from 'node:events'
import { leakyError } from './leaky-error.mjs'

class App {
  constructor() {
    this.receiver = { client: new EventEmitter() }
    this.handlers = {}
  }
  event(name, handler) {
    this.handlers[name] = handler
  }
  async start() {
    if (process.env.STUB_START === 'reject') throw leakyError()
    if (process.env.STUB_START === 'log') console.error('Bolt logger:', leakyError())
    // STUB_REACTION: one reaction_added event, delivered to the real handler once the app is up.
    if (process.env.STUB_REACTION) await this.handlers.reaction_added({ event: JSON.parse(process.env.STUB_REACTION) })
  }
}

export default { App }
