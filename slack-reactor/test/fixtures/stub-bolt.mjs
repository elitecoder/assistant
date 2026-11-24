import { EventEmitter } from 'node:events'
import { leakyError } from './leaky-error.mjs'

class App {
  constructor() {
    this.receiver = { client: new EventEmitter() }
  }
  event() {}
  async start() {
    if (process.env.STUB_START === 'reject') throw leakyError()
    if (process.env.STUB_START === 'log') console.error('Bolt logger:', leakyError())
  }
}

export default { App }
