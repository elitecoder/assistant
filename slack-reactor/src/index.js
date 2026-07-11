#!/usr/bin/env node
// slack-reactor — react with this machine's emoji on a Slack thread to capture
// it as a /todo. Built on Perch's bolt + Socket Mode pattern.
//
//   PER-MACHINE routing. Each machine runs its OWN Slack app (or its own
//   $TODO_EMOJI) and claims one emoji. This machine ignores reactions whose name
//   != TODO_EMOJI, so other emojis route to other machines.
//
// Transport: @slack/bolt in SOCKET MODE (botToken xoxb- + appToken xapp-). No
// public URL, no signing secret. Because Socket Mode delivers BOT events, the
// bot only receives reaction_added for channels it is a MEMBER of — so the bot
// must be /invite-d to each channel you want this to work in. (This is the
// trade we accepted vs. user-events, which would need a public Request URL.)
//
// Feedback: a :white_check_mark: reaction only. Never chat.postMessage
// (operator rule: never send Slack messages on the user's behalf).
//
// Tokens come from the environment (launcher sources ~/.zprofile); never hardcoded.
import boltPkg from '@slack/bolt'
import { WebClient } from '@slack/web-api'
import fs from 'fs'
import os from 'os'
import path from 'path'
import crypto from 'crypto'
import { addTodo, MACHINE, TODO_PATH } from './todo-store.js'

const { App } = boltPkg

// --- config from env -------------------------------------------------------
const BOT_TOKEN = process.env.SLACK_BOT_TOKEN // xoxb- — authorizes the app, reads threads, reacts back
const APP_TOKEN = process.env.SLACK_APP_TOKEN // xapp- — Socket Mode websocket (scope connections:write)
const ONLY_REACTOR = process.env.SLACK_USER_ID // optional: only THIS user's reactions create todos
const EMOJIS = new Set(
  (process.env.TODO_EMOJI ?? 'mukuls2')
    .split(',')
    .map((e) => e.trim().replace(/:/g, ''))
    .filter(Boolean),
)
const TODO_PRIORITY = process.env.TODO_PRIORITY ?? 'P2'
const TODO_AUTODISPATCH = process.env.TODO_AUTODISPATCH !== '0'

function die(msg) {
  console.error(`ERROR: ${msg}`)
  process.exit(1)
}
if (!BOT_TOKEN) die('SLACK_BOT_TOKEN is not set (xoxb- bot token; scopes reactions:read/write + *:history)')
if (!APP_TOKEN) die('SLACK_APP_TOKEN is not set (xapp- Socket Mode app-level token, scope connections:write)')
if (EMOJIS.size === 0) die('TODO_EMOJI resolved to empty')

const bot = new WebClient(BOT_TOKEN)
const app = new App({ token: BOT_TOKEN, appToken: APP_TOKEN, socketMode: true })

// The bot's own user id (filled in at boot from auth.test) so the message
// handler can skip the bot's own messages.
let BOT_USER_ID = null

// --- helpers ---------------------------------------------------------------
const userNameCache = new Map()
async function userName(uid) {
  if (!uid) return 'unknown'
  if (userNameCache.has(uid)) return userNameCache.get(uid)
  let name = uid
  try {
    const r = await bot.users.info({ user: uid })
    const p = r.user?.profile ?? {}
    name = p.display_name || r.user?.real_name || r.user?.name || uid
  } catch {
    /* fall back to id */
  }
  userNameCache.set(uid, name)
  return name
}

const clean = (t) => (t ?? '').replace(/\s+/g, ' ').trim()

// --- Keel M5 wave-2: slack-events connector feed --------------------------
// The Python slack-events connector (bin/connectors/slack-events.py) is a
// read-only PRODUCER that normalizes app_mention + DM/channel messages into
// WorldEvents. It cannot subscribe to Slack directly (that would need a second
// Socket-Mode connection), so this existing Bolt app SPOOLS each raw event
// payload into ~/.assistant/connectors/slack/spool/ and the connector consumes
// it. This is a pure SIDE FILE WRITE — it never sends a Slack message and does
// not touch the emoji→TODO reactor path (the never-postMessage rule stands).
const SPOOL_DIR = path.join(os.homedir(), '.assistant', 'connectors', 'slack', 'spool')

function spoolEvent(event) {
  // Atomic tmp+rename drop, exactly like the connector base's inbox contract,
  // so the Python consumer never reads a half-written file. Best-effort: a
  // spool failure must never crash the reactor.
  try {
    fs.mkdirSync(SPOOL_DIR, { recursive: true })
    const ts = String(event?.ts ?? event?.event_ts ?? Date.now())
    const rand = crypto.randomBytes(4).toString('hex')
    const base = `evt-${ts}-${rand}.json`
    const dst = path.join(SPOOL_DIR, base)
    const tmp = path.join(SPOOL_DIR, `.${base}.tmp`)
    // Wrap under {event} so the payload shape matches the Slack Events API and
    // the connector's slack_event_to_event() (which accepts either shape).
    fs.writeFileSync(tmp, JSON.stringify({ event }), { mode: 0o644 })
    fs.renameSync(tmp, dst)
  } catch (e) {
    console.error(`[slack-events] spool failed: ${e.message}`)
  }
}

// app_mention: the bot was @-mentioned in a channel — always a world event.
app.event('app_mention', async ({ event }) => {
  spoolEvent(event)
})

// message: DMs (channel_type==='im') and plain channel messages. Skip the
// bot's own messages and non-user subtypes (edits, joins, bot posts) so the
// connector only ever sees genuine human messages.
app.event('message', async ({ event }) => {
  if (!event || event.subtype || event.bot_id) return
  if (event.user && BOT_USER_ID && event.user === BOT_USER_ID) return
  spoolEvent(event)
})

async function buildTodo(messages, link) {
  const root = clean(messages[0]?.text ?? '')
  const title = root.length > 80 ? root.slice(0, 78) + '…' : root || '(no text)'
  const lines = []
  for (const m of messages) {
    const body = clean(m.text ?? '')
    if (body) lines.push(`*${await userName(m.user)}:* ${body}`)
  }
  const detail = `Captured from Slack thread on ${MACHINE}.\n\n${lines.join('\n')}\n\n${link}`.trim()
  return { title, detail }
}

// --- event handler ---------------------------------------------------------
app.event('reaction_added', async ({ event }) => {
  const reaction = (event.reaction ?? '').split('::')[0] // strip skin tone
  if (!EMOJIS.has(reaction)) return // routed to a different machine/emoji
  if (ONLY_REACTOR && event.user !== ONLY_REACTOR) return // someone else reacted
  if (event.item?.type !== 'message') return

  const channel = event.item.channel
  const ts = event.item.ts

  // Resolve thread root, then pull the whole thread. Reads use the bot token —
  // the bot is a member of `channel` (it just received a bot event there).
  let threadTs = ts
  try {
    const hist = await bot.conversations.history({ channel, latest: ts, oldest: ts, inclusive: true, limit: 1 })
    threadTs = hist.messages?.[0]?.thread_ts ?? ts
  } catch (e) {
    console.error(`[reactor] history lookup failed (${channel}/${ts}): ${e.data?.error ?? e.message}`)
  }

  let messages = []
  try {
    const rep = await bot.conversations.replies({ channel, ts: threadTs, limit: 200 })
    messages = rep.messages ?? []
  } catch (e) {
    console.error(`[reactor] replies fetch failed (${channel}/${threadTs}): ${e.data?.error ?? e.message}`)
    return
  }
  if (messages.length === 0) return

  let link = ''
  try {
    link = (await bot.chat.getPermalink({ channel, message_ts: threadTs })).permalink ?? ''
  } catch {
    /* permalink optional */
  }

  const { title, detail } = await buildTodo(messages, link)
  const source = `slack-react:${channel}:${threadTs}`
  let result
  try {
    result = addTodo({ title, detail, url: link, source, priority: TODO_PRIORITY, autoDispatch: TODO_AUTODISPATCH })
  } catch (e) {
    console.error(`[reactor] todo write failed: ${e.message}`)
    return
  }

  console.error(
    `[${new Date().toISOString()}] :${reaction}: → ${result.id} ` +
      `(${result.created ? 'new' : 'existing'}) on ${MACHINE}: ${title}`,
  )

  // Feedback = a reaction only. Never a message.
  try {
    await bot.reactions.add({ channel, timestamp: ts, name: 'white_check_mark' })
  } catch (e) {
    if (e.data?.error !== 'already_reacted') {
      console.error(`[reactor] react-back failed: ${e.data?.error ?? e.message}`)
    }
  }
})

// --- boot ------------------------------------------------------------------
;(async () => {
  try {
    const auth = await bot.auth.test()
    BOT_USER_ID = auth.user_id ?? null
    console.error(
      `slack-reactor up (Socket Mode)\n` +
        `  machine:  ${MACHINE}\n` +
        `  team:     ${auth.team}\n` +
        `  bot:      ${auth.user} (${auth.user_id})\n` +
        `  emoji(s): ${[...EMOJIS].join(', ')}\n` +
        `  reactor:  ${ONLY_REACTOR ?? 'anyone in the bot\'s channels'}\n` +
        `  priority: ${TODO_PRIORITY}  autoDispatch=${TODO_AUTODISPATCH}\n` +
        `  todo:     ${TODO_PATH}\n` +
        `  NOTE: bot only sees reactions in channels it has been /invite-d to.`,
    )
  } catch (e) {
    die(`auth.test failed for SLACK_BOT_TOKEN: ${e.data?.error ?? e.message}`)
  }
  // Exit on WebSocket disconnect so launchd KeepAlive restarts us cleanly.
  app.receiver.client.on('disconnected', () => {
    console.error('[reactor] Socket Mode disconnected — exiting for launchd restart')
    process.exit(1)
  })

  // Liveness watchdog: if the socket goes zombie (pong timeouts but no crash,
  // so launchd never restarts), exit after 10 minutes of silence so launchd
  // KeepAlive kicks a fresh connection. Reset on every received Slack event.
  const WATCHDOG_MS = 10 * 60 * 1000
  let watchdog = setTimeout(() => {
    console.error('[reactor] watchdog: no Slack events in 10 min — exiting for launchd restart')
    process.exit(1)
  }, WATCHDOG_MS)
  watchdog.unref()
  app.receiver.client.on('slack_event', () => {
    clearTimeout(watchdog)
    watchdog = setTimeout(() => {
      console.error('[reactor] watchdog: no Slack events in 10 min — exiting for launchd restart')
      process.exit(1)
    }, WATCHDOG_MS)
    watchdog.unref()
  })

  await app.start()
  console.error('[reactor] Socket Mode connected — waiting for reactions…')
})()
