// watchbot listener: links to WhatsApp as a device (like WhatsApp Web), watches ONE group,
// and saves every message + photo to output/_inbox for `python -m watchbot watch` to process.
// It only ever sends to the approval chat (your own "Message yourself" chat unless
// WATCHBOT_APPROVAL names a group): the uploader's previews, queued as files in _outbox/.
//
//   node index.mjs                       first run: scan the QR, then it lists your groups
//   node index.mjs --group "Watch Deals" pick the group to watch (remembered in config.json)

import makeWASocket, {
  Browsers, DisconnectReason, downloadMediaMessage, getContentType,
  jidNormalizedUser, normalizeMessageContent, useMultiFileAuthState,
} from '@whiskeysockets/baileys'
import { appendFileSync, existsSync, mkdirSync, readFileSync, readdirSync, unlinkSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import pino from 'pino'
import qrcode from 'qrcode-terminal'

const HERE = dirname(fileURLToPath(import.meta.url))
const INBOX = resolve(process.env.WATCHBOT_INBOX || join(HERE, '..', 'output', '_inbox'))
const LOG = join(INBOX, 'messages.jsonl')
const APPROVALS = join(INBOX, 'approvals.jsonl')   // replies in the approval chat, for the uploader
const HEARTBEAT = join(INBOX, 'heartbeat.txt')     // the watchdog restarts us if this goes stale
const OUTBOX = resolve(process.env.WATCHBOT_OUTBOX || join(INBOX, '..', '_outbox'))
const SENT = join(OUTBOX, 'sent.jsonl')
const APPROVAL = (process.env.WATCHBOT_APPROVAL || 'self').trim()
const LOGGED_OUT = 3  // exit code: tells the watchdog not to restart
const CONFIG = join(HERE, 'config.json')
const logger = pino({ level: 'warn' })

mkdirSync(INBOX, { recursive: true })
const config = existsSync(CONFIG) ? JSON.parse(readFileSync(CONFIG, 'utf8')) : {}
const argIdx = process.argv.indexOf('--group')
if (argIdx > -1 && process.argv[argIdx + 1]) {
  config.group = process.argv[argIdx + 1]
  delete config.groupId
  writeFileSync(CONFIG, JSON.stringify(config, null, 2))
}

// Message ids already saved, so reconnects and redeliveries don't duplicate lines,
// and WhatsApp's hash of each photo/video, so a forwarded or re-sent copy isn't downloaded again.
const seen = new Set()
const mediaFiles = new Map()  // WhatsApp file hash -> file already in the inbox
if (existsSync(LOG)) {
  for (const line of readFileSync(LOG, 'utf8').split('\n')) {
    try {
      const r = JSON.parse(line)
      seen.add(r.id)
      if (r.media_sha && r.file) mediaFiles.set(r.media_sha, r.file)
    } catch {}
  }
}

// WhatsApp ids of messages we sent, so our own previews aren't read back as replies.
const sentIds = new Set()
mkdirSync(OUTBOX, { recursive: true })
if (existsSync(SENT)) {
  for (const line of readFileSync(SENT, 'utf8').split('\n')) {
    try { sentIds.add(JSON.parse(line).wa_id) } catch {}
  }
}
const startedAt = Math.floor(Date.now() / 1000)

const EXT = { 'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp', 'video/mp4': '.mp4' }

function digits(jid) {
  return jid ? jidNormalizedUser(jid).replace(/@.*/, '') : ''
}

async function resolveApproval(sock, groups) {
  if (APPROVAL.toLowerCase() === 'self') {
    // "Message yourself" can arrive under the phone-number id or the LID; accept both
    return [sock.user?.id, sock.user?.lid].filter(Boolean).map(j => jidNormalizedUser(j))
  }
  const want = APPROVAL.toLowerCase()
  const g = groups.find(g => g.subject?.toLowerCase() === want) || groups.find(g => g.subject?.toLowerCase().includes(want))
  if (!g) console.log(`  ! No group matching WATCHBOT_APPROVAL="${APPROVAL}"; previews won't be sent.`)
  return g ? [g.id] : []
}

async function resolveGroup(sock, groups) {
  writeFileSync(join(HERE, 'groups.json'), JSON.stringify(groups.map(g => ({ id: g.id, name: g.subject })), null, 2))
  if (config.groupId && groups.some(g => g.id === config.groupId)) return config.groupId
  if (!config.group) {
    console.log('\nYour groups:')
    for (const g of groups) console.log(`  - ${g.subject}`)
    console.log('\nRestart with:  node index.mjs --group "<name from the list>"')
    return null
  }
  const want = config.group.toLowerCase()
  const match = groups.find(g => g.subject?.toLowerCase() === want)
    || groups.find(g => g.subject?.toLowerCase().includes(want))
  if (!match) {
    console.log(`\nNo group matching "${config.group}". Your groups:`)
    for (const g of groups) console.log(`  - ${g.subject}`)
    return null
  }
  config.groupId = match.id
  writeFileSync(CONFIG, JSON.stringify(config, null, 2))
  return match.id
}

async function save(sock, m) {
  const content = normalizeMessageContent(m.message)  // unwraps view-once / disappearing wrappers
  if (!content) return
  const type = getContentType(content)
  const rec = {
    id: m.key.id,
    ts: Number(m.messageTimestamp) || Math.floor(Date.now() / 1000),
    sender: digits(m.key.participantAlt?.endsWith('@s.whatsapp.net') ? m.key.participantAlt : m.key.participant),
    sender_lid: digits(m.key.participant),
    sender_name: m.pushName || '',
    type: 'text',
    text: '',
    file: null,
    quoted_id: null,
  }
  const body = content[type]
  rec.quoted_id = body?.contextInfo?.stanzaId || null

  if (type === 'conversation') rec.text = content.conversation
  else if (type === 'extendedTextMessage') rec.text = body.text || ''
  else if (type === 'imageMessage' || type === 'videoMessage') {
    rec.type = type === 'imageMessage' ? 'image' : 'video'
    rec.text = body.caption || ''
    rec.media_sha = body.fileSha256 ? Buffer.from(body.fileSha256).toString('base64') : null
    const known = rec.media_sha && mediaFiles.get(rec.media_sha)
    if (known && existsSync(join(INBOX, known))) {
      rec.file = known  // same photo as an earlier message: point at that file instead of downloading
    } else {
      const day = new Date(rec.ts * 1000).toISOString().slice(0, 10).replaceAll('-', '')
      const dir = join(INBOX, day)
      mkdirSync(dir, { recursive: true })
      const ext = EXT[(body.mimetype || '').split(';')[0]] || (rec.type === 'image' ? '.jpg' : '.mp4')
      try {
        const buf = await downloadMediaMessage(m, 'buffer', {}, { logger, reuploadRequest: sock.updateMediaMessage })
        const name = `${rec.id}${ext}`
        writeFileSync(join(dir, name), buf)
        rec.file = `${day}/${name}`
        if (rec.media_sha) mediaFiles.set(rec.media_sha, rec.file)
      } catch (e) {
        console.log(`  ! could not download ${rec.type} ${rec.id}: ${e.message}`)
      }
    }
  } else {
    return  // stickers, audio, polls, reactions, etc.
  }

  appendFileSync(LOG, JSON.stringify(rec) + '\n')
  seen.add(rec.id)
  const who = rec.sender_name || rec.sender
  const preview = rec.text ? `: ${rec.text.split('\n')[0].slice(0, 50)}` : ''
  console.log(`  ${new Date(rec.ts * 1000).toLocaleTimeString()}  ${who}  ${rec.type}${preview}`)
}

function textOf(m) {
  const content = normalizeMessageContent(m.message)
  if (!content) return { text: '', quoted: null }
  const type = getContentType(content)
  const body = content[type]
  const text = type === 'conversation' ? content.conversation : (body?.text || body?.caption || '')
  return { text, quoted: body?.contextInfo?.stanzaId || null }
}

function saveApproval(m) {
  const { text, quoted } = textOf(m)
  if (!text) return
  const rec = { id: m.key.id, ts: Number(m.messageTimestamp) || startedAt, text, quoted_id: quoted, from_me: !!m.key.fromMe }
  appendFileSync(APPROVALS, JSON.stringify(rec) + '\n')
  console.log(`  approval chat: ${text.split('\n')[0].slice(0, 50)}`)
}

let sending = false
async function sendOutbox(sock, jid) {
  if (sending || !jid) return
  sending = true
  try {
    const files = readdirSync(OUTBOX).filter(f => f.endsWith('.json')).sort()
    for (const f of files) {
      const path = join(OUTBOX, f)
      let msg
      try { msg = JSON.parse(readFileSync(path, 'utf8')) } catch { continue }
      const content = msg.image && existsSync(msg.image)
        ? { image: readFileSync(msg.image), caption: msg.text }
        : { text: msg.text }
      const sent = await sock.sendMessage(jid, content)
      sentIds.add(sent.key.id)
      appendFileSync(SENT, JSON.stringify({ outbox_id: msg.id, wa_id: sent.key.id, ts: Math.floor(Date.now() / 1000) }) + '\n')
      unlinkSync(path)
      console.log(`  sent: ${msg.text.split('\n').find(l => l.trim() && !l.startsWith('*New')) || ''}`)
      await new Promise(r => setTimeout(r, 1500))  // don't burst messages
    }
  } catch (e) {
    console.log(`  ! could not send: ${e.message}`)
  } finally {
    sending = false
  }
}

let timers = []
async function start() {
  for (const t of timers) clearInterval(t)
  timers = []
  const { state, saveCreds } = await useMultiFileAuthState(join(HERE, 'auth'))
  const sock = makeWASocket({
    auth: state,
    logger,
    browser: Browsers.windows('Desktop'),
    markOnlineOnConnect: false,   // don't show as "online" or mark chats read on your phone
    syncFullHistory: false,
  })
  sock.ev.on('creds.update', saveCreds)

  let groupId = null
  let approvalJids = []
  sock.ev.on('connection.update', async ({ connection, lastDisconnect, qr }) => {
    if (qr) {
      console.log('\nOn the spare phone: WhatsApp > Settings > Linked devices > Link a device, then scan:\n')
      qrcode.generate(qr, { small: true })
    }
    if (connection === 'open') {
      console.log('Connected to WhatsApp.')
      const groups = Object.values(await sock.groupFetchAllParticipating())
      groupId = await resolveGroup(sock, groups)
      approvalJids = await resolveApproval(sock, groups)
      if (groupId) console.log(`Watching "${config.group}". Saving to ${INBOX}\n(leave this window open; Ctrl+C to stop)\n`)
      if (approvalJids.length) console.log(`Previews go to ${APPROVAL.toLowerCase() === 'self' ? 'your own "Message yourself" chat' : `"${APPROVAL}"`}.`)
      writeFileSync(HEARTBEAT, String(Date.now()))
      timers.push(setInterval(() => writeFileSync(HEARTBEAT, String(Date.now())), 30_000))
      timers.push(setInterval(() => sendOutbox(sock, approvalJids[0]), 3_000))
    }
    if (connection === 'close') {
      const code = lastDisconnect?.error?.output?.statusCode
      if (code === DisconnectReason.loggedOut) {
        console.log('Logged out from the phone. Delete the "auth" folder and run again to re-link.')
        process.exit(LOGGED_OUT)
      }
      for (const t of timers) clearInterval(t)
      timers = []
      console.log(`Connection closed (${code ?? 'unknown'}), reconnecting in 5s...`)
      setTimeout(start, 5000)
    }
  })

  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    for (const m of messages) {
      const chat = m.key.remoteJid ? jidNormalizedUser(m.key.remoteJid) : ''
      if (approvalJids.includes(chat) && !sentIds.has(m.key.id) && !seen.has(m.key.id)
          && (Number(m.messageTimestamp) || 0) >= startedAt - 60) {
        // In a shared group only replies to a preview count; in your own chat any message does.
        if (chat !== groupId || sentIds.has(textOf(m).quoted)) {
          seen.add(m.key.id)
          saveApproval(m)
          continue
        }
      }
      if (type !== 'notify' || !groupId) continue
      if (m.key.remoteJid !== groupId || m.key.fromMe || seen.has(m.key.id)) continue
      seen.add(m.key.id)  // before the download, so a redelivery arriving meanwhile is skipped
      try { await save(sock, m) } catch (e) {
        seen.delete(m.key.id)
        console.log(`  ! failed to save ${m.key.id}: ${e.message}`)
      }
    }
  })
}

start()
