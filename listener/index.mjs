// watchbot listener: links to WhatsApp as a device (like WhatsApp Web), watches ONE group,
// and saves every message + photo to output/_inbox for `python -m watchbot watch` to process.
// It never sends anything.
//
//   node index.mjs                       first run: scan the QR, then it lists your groups
//   node index.mjs --group "Watch Deals" pick the group to watch (remembered in config.json)

import makeWASocket, {
  Browsers, DisconnectReason, downloadMediaMessage, getContentType,
  jidNormalizedUser, normalizeMessageContent, useMultiFileAuthState,
} from '@whiskeysockets/baileys'
import { appendFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import pino from 'pino'
import qrcode from 'qrcode-terminal'

const HERE = dirname(fileURLToPath(import.meta.url))
const INBOX = resolve(process.env.WATCHBOT_INBOX || join(HERE, '..', 'output', '_inbox'))
const LOG = join(INBOX, 'messages.jsonl')
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

// Message ids already saved, so reconnects and redeliveries don't duplicate lines.
const seen = new Set()
if (existsSync(LOG)) {
  for (const line of readFileSync(LOG, 'utf8').split('\n')) {
    try { seen.add(JSON.parse(line).id) } catch {}
  }
}

const EXT = { 'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp', 'video/mp4': '.mp4' }

function digits(jid) {
  return jid ? jidNormalizedUser(jid).replace(/@.*/, '') : ''
}

async function resolveGroup(sock) {
  const groups = Object.values(await sock.groupFetchAllParticipating())
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
    const day = new Date(rec.ts * 1000).toISOString().slice(0, 10).replaceAll('-', '')
    const dir = join(INBOX, day)
    mkdirSync(dir, { recursive: true })
    const ext = EXT[(body.mimetype || '').split(';')[0]] || (rec.type === 'image' ? '.jpg' : '.mp4')
    try {
      const buf = await downloadMediaMessage(m, 'buffer', {}, { logger, reuploadRequest: sock.updateMediaMessage })
      const name = `${rec.id}${ext}`
      writeFileSync(join(dir, name), buf)
      rec.file = `${day}/${name}`
    } catch (e) {
      console.log(`  ! could not download ${rec.type} ${rec.id}: ${e.message}`)
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

async function start() {
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
  sock.ev.on('connection.update', async ({ connection, lastDisconnect, qr }) => {
    if (qr) {
      console.log('\nOn the spare phone: WhatsApp > Settings > Linked devices > Link a device, then scan:\n')
      qrcode.generate(qr, { small: true })
    }
    if (connection === 'open') {
      console.log('Connected to WhatsApp.')
      groupId = await resolveGroup(sock)
      if (groupId) console.log(`Watching "${config.group}". Saving to ${INBOX}\n(leave this window open; Ctrl+C to stop)\n`)
    }
    if (connection === 'close') {
      const code = lastDisconnect?.error?.output?.statusCode
      if (code === DisconnectReason.loggedOut) {
        console.log('Logged out from the phone. Delete the "auth" folder and run again to re-link.')
        process.exit(1)
      }
      console.log(`Connection closed (${code ?? 'unknown'}), reconnecting in 5s...`)
      setTimeout(start, 5000)
    }
  })

  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    if (type !== 'notify' || !groupId) return
    for (const m of messages) {
      if (m.key.remoteJid !== groupId || m.key.fromMe || seen.has(m.key.id)) continue
      try { await save(sock, m) } catch (e) { console.log(`  ! failed to save ${m.key.id}: ${e.message}`) }
    }
  })
}

start()
