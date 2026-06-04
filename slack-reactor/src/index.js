#!/usr/bin/env node
// slack-reactor — react with this machine's emoji on ANY Slack thread to capture
// it as a /todo. Built on Perch's bolt pattern, but tuned for the two hard
// requirements:
//
//   1. NO per-channel bot invites. We subscribe to `reaction_added` as a USER
//      event (oauth_config.scopes.user + settings.event_subscriptions.user_events),
//      authorized by your user token. User events fire for every channel YOU can
//      see — public, private, DMs — with no bot member in the channel. Bot events
//      would only fire for channels the bot was invited to; that's the invite pain.
//
//   2. PER-MACHINE routing. Each machine runs its OWN Slack app (one app = one
//      Events Request URL = one machine) and claims one emoji via $TODO_EMOJI.
//      This machine ignores reactions whose name != TODO_EMOJI, so other emojis
//      on other machines' apps route there instead.
//
// Transport: bolt in HTTP mode (ExpressReceiver) behind the existing cloudflared
// tunnel. Socket Mode is NOT used — it does not deliver user-token events.
//
// Feedback: a :white_check_mark: reaction only. We never chat.postMessage as the
// user (operator rule: never send Slack messages on the user's behalf).
//
// Tokens come from the environment (launcher sources ~/.zprofile); never hardcoded.
import boltPkg from '@slack/bolt'
import { WebClient } from '@slack/web-api'
import { addTodo, MACHINE, TODO_PATH } from './todo-store.js'

const { App, ExpressReceiver } = boltPkg

// --- config from env -------------------------------------------------------
const USER_TOKEN = process.env.SLACK_USER_TOKEN // xoxp- — runs API calls AS YOU
const BOT_TOKEN = process.env.SLACK_BOT_TOKEN // xoxb- — used only for the ✅ react-back
const SIGNING_SECRET = process.env.SLACK_SIGNING_SECRET // verifies Slack's POSTs
const PORT = parseInt(process.env.SLACK_REACTOR_PORT ?? '3737', 10)
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
if (!USER_TOKEN) die('SLACK_USER_TOKEN is not set (xoxp- user token with user scope reactions:read + *:history)')
if (!SIGNING_SECRET) die('SLACK_SIGNING_SECRET is not set (Basic Information → Signing Secret)')
if (EMOJIS.size === 0) die('TODO_EMOJI resolved to empty')

// API calls run as the USER so we can read any channel the user can see without
// a bot being present. The bot token is optional and only used for react-back.
const userClient = new WebClient(USER_TOKEN)
const botClient = BOT_TOKEN ? new WebClient(BOT_TOKEN) : null

// We act ONLY on the authorizing user's own reactions. user_events deliver every
// reaction the user can *see*, including other people's — so gate on user id.
let ME = null

const receiver = new ExpressReceiver({ signingSecret: SIGNING_SECRET, endpoints: '/slack/events' })
// HTTP-mode app; no socketMode, no appToken. `token` left unset — we pass the
// right client per call (user for reads, bot for react-back).
const app = new App({ receiver, token: BOT_TOKEN ?? USER_TOKEN })

// --- helpers ---------------------------------------------------------------
const userNameCache = new Map()
async function userName(uid) {
  if (!uid) return 'unknown'
  if (userNameCache.has(uid)) return userNameCache.get(uid)
  let name = uid
  try {
    const r = await userClient.users.info({ user: uid })
    const p = r.user?.profile ?? {}
    name = p.display_name || r.user?.real_name || r.user?.name || uid
  } catch {
    /* fall back to id */
  }
  userNameCache.set(uid, name)
  return name
}

const clean = (t) => (t ?? '').replace(/\s+/g, ' ').trim()

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
  if (ME && event.user !== ME) return // only the operator's own reactions
  if (event.item?.type !== 'message') return

  const channel = event.item.channel
  const ts = event.item.ts

  // Resolve the thread root, then pull the whole thread — all as the USER, so
  // no bot membership is required in `channel`.
  let threadTs = ts
  try {
    const hist = await userClient.conversations.history({ channel, latest: ts, oldest: ts, inclusive: true, limit: 1 })
    threadTs = hist.messages?.[0]?.thread_ts ?? ts
  } catch (e) {
    console.error(`[reactor] history lookup failed (${channel}/${ts}): ${e.data?.error ?? e.message}`)
  }

  let messages = []
  try {
    const rep = await userClient.conversations.replies({ channel, ts: threadTs, limit: 200 })
    messages = rep.messages ?? []
  } catch (e) {
    console.error(`[reactor] replies fetch failed (${channel}/${threadTs}): ${e.data?.error ?? e.message}`)
    return
  }
  if (messages.length === 0) return

  let link = ''
  try {
    link = (await userClient.chat.getPermalink({ channel, message_ts: threadTs })).permalink ?? ''
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

  // Feedback = a reaction only. Never a message (never post as the user).
  const reactClient = botClient ?? userClient
  try {
    await reactClient.reactions.add({ channel, timestamp: ts, name: 'white_check_mark' })
  } catch (e) {
    if (e.data?.error !== 'already_reacted') {
      console.error(`[reactor] react-back failed: ${e.data?.error ?? e.message}`)
    }
  }
})

// --- boot ------------------------------------------------------------------
;(async () => {
  try {
    const auth = await userClient.auth.test()
    ME = auth.user_id
    console.error(
      `slack-reactor up\n` +
        `  machine:  ${MACHINE}\n` +
        `  team:     ${auth.team}\n` +
        `  user:     ${auth.user} (${ME})\n` +
        `  emoji(s): ${[...EMOJIS].join(', ')}\n` +
        `  priority: ${TODO_PRIORITY}  autoDispatch=${TODO_AUTODISPATCH}\n` +
        `  todo:     ${TODO_PATH}\n` +
        `  react-back via: ${botClient ? 'bot token' : 'user token'}\n` +
        `  port:     ${PORT}  endpoint: /slack/events`,
    )
  } catch (e) {
    die(`auth.test failed for SLACK_USER_TOKEN: ${e.data?.error ?? e.message}`)
  }
  await app.start(PORT)
  console.error(`[reactor] listening on :${PORT} — waiting for reactions…`)
})()
