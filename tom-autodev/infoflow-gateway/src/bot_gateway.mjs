#!/usr/bin/env node
// Infoflow bot gateway for tom-autodev.
//
// Both directions of an approval belong to the bot app (zhaotao02_Bot): a personal
// account cannot hold the inbound WebSocket, so a request sent as anything else
// could never be answered automatically. Credentials are resolved here — env first,
// then ~/.infoflow_config — so no shell has to export a token before a run.
//
// The gateway journals inbound messages and sends outbound markdown. It authorizes
// nothing: `clients.infoflow_reply_client` decides whether a journalled reply is a
// decision for a specific approval.
import { createServer } from 'node:http'
import { appendFileSync, mkdirSync, readFileSync, existsSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { dirname } from 'node:path'
import { homedir } from 'node:os'
import { WSClient, LogLevel, Client } from '@baidu/infoflow-sdk-nodejs'

const DEFAULT_HOST = '127.0.0.1'
const DEFAULT_PORT = 18791
const DEFAULT_BASE_URL = 'https://api.im.baidu.com'
const INTERNAL_WS_GATEWAY = 'infoflow-open-gateway.weiyun.baidu.com'
const INTERNAL_WS_CONNECT_DOMAIN = 'infoflow-open-gateway.weiyun.baidu.com:8869'

function nowIso() {
  return new Date().toISOString()
}

function log(message, fields = {}) {
  console.log(JSON.stringify({ ts: nowIso(), message, ...fields }, null, 0))
}

function isRecord(value) {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function readString(value) {
  return value == null ? '' : String(value)
}

function readConfigFile() {
  const path = process.env.INFOFLOW_CONFIG || `${homedir()}/.infoflow_config`
  if (!existsSync(path)) return {}
  const result = {}
  for (const rawLine of readFileSync(path, 'utf8').split(/\r?\n/)) {
    const line = rawLine.trim()
    if (!line || line.startsWith('#') || !line.includes('=')) continue
    const [key, ...rest] = line.split('=')
    result[key.trim()] = rest.join('=').trim().replace(/^['"]|['"]$/g, '')
  }
  return result
}

function credential(fileConfig, ...names) {
  for (const name of names) {
    if (process.env[name]) return process.env[name]
    if (fileConfig[name]) return fileConfig[name]
  }
  return ''
}

function journalPath() {
  return process.env.TOM_AUTODEV_INFOFLOW_REPLY_JOURNAL
    || `${homedir()}/.tom-autodev/infoflow-replies.jsonl`
}

function baseUrl() {
  return (process.env.INFOFLOW_BASE_URL || DEFAULT_BASE_URL).replace(/\/$/, '')
}

const fileConfig = readConfigFile()
const appKey = credential(fileConfig, 'INFOFLOW_APP_KEY', 'INFOFLOW_AK', 'INFOFLOW_APP_ID')
const appSecret = credential(fileConfig, 'INFOFLOW_APP_SECRET', 'INFOFLOW_SK')
if (!appKey || !appSecret) {
  throw new Error(
    'missing bot credentials: set INFOFLOW_APP_KEY/INFOFLOW_APP_SECRET or write '
    + `INFOFLOW_AK/INFOFLOW_SK into ${process.env.INFOFLOW_CONFIG || `${homedir()}/.infoflow_config`}`,
  )
}

let cachedToken = null

async function accessToken(force = false) {
  if (!force && cachedToken && cachedToken.expiresAt > Date.now() + 60_000) return cachedToken.value
  const response = await fetch(`${baseUrl()}/api/v1/auth/app_access_token`, {
    method: 'POST',
    headers: { 'content-type': 'application/json; charset=utf-8' },
    body: JSON.stringify({
      app_key: appKey,
      // The endpoint expects the secret pre-hashed, exactly as get_token.sh does.
      app_secret: createHash('md5').update(appSecret).digest('hex'),
    }),
  })
  const payload = await response.json().catch(() => null)
  const data = isRecord(payload) && isRecord(payload.data) ? payload.data : payload
  const token = isRecord(data) ? readString(data.app_access_token) : ''
  if (!token) throw new Error('token request rejected')
  const expire = isRecord(data) && Number.isFinite(Number(data.expire)) ? Number(data.expire) : 7200
  cachedToken = { value: `Bearer-${token}`, expiresAt: Date.now() + expire * 1000 }
  return cachedToken.value
}

function sendReceipt(payload) {
  const outer = isRecord(payload) && isRecord(payload.data) ? payload.data : payload
  // A group send answers `{code, data:{errcode, data:{messageid,…}}}`, a single send
  // puts `msgkey` one level up, so both envelopes are inspected.
  const data = isRecord(outer) && isRecord(outer.data) ? outer.data : outer
  for (const envelope of [payload, outer, data]) {
    if (!isRecord(envelope)) continue
    const code = envelope.errcode
    if (code !== undefined && code !== null && !['0', '200'].includes(String(code))) {
      throw new Error(`message rejected: ${code}`)
    }
  }
  const identity = [outer, data].reduce(
    (found, envelope) =>
      found ||
      (isRecord(envelope)
        ? readString(
            envelope.msgkey ||
              envelope.messageid ||
              envelope.messageId ||
              envelope.msgId ||
              envelope.message_id,
          )
        : ''),
    '',
  )
  if (!identity) throw new Error(`message response invalid: ${JSON.stringify(payload).slice(0, 400)}`)
  return identity
}

async function sendMarkdown(recipients, content) {
  const touser = recipients.join('|')
  const body = JSON.stringify({ touser, msgtype: 'md', md: { content } })
  return await authorizedPost('/api/v1/app/message/send', body, sendReceipt)
}

// Group creation and group sends live here for the same reason single sends do:
// the scripts in `infoflow-message-group` need INFOFLOW_TOKEN in their
// environment, and tom-autodev's CLI boundary refuses to carry secrets.
async function authorizedPost(path, body, read) {
  let token = await accessToken()
  for (const attempt of [0, 1]) {
    const response = await fetch(`${baseUrl()}${path}`, {
      method: 'POST',
      headers: {
        authorization: token,
        'content-type': 'application/json; charset=utf-8',
        LOGID: `${Date.now()}`,
      },
      body,
    })
    const payload = await response.json().catch(() => null)
    if (response.status === 401 && attempt === 0) {
      token = await accessToken(true)
      continue
    }
    return read(payload)
  }
  throw new Error('request failed')
}

function agentId(payload) {
  // `/imRobot/detail` nests the robot one level deeper than the other endpoints
  // (`{code, data:{code, data:{agentId}}}`), so both depths are inspected. Reading
  // only the first one silently yielded null, which used to create the collaboration
  // group without its own bot in `robotList`.
  const outer = isRecord(payload) && isRecord(payload.data) ? payload.data : payload
  const data = isRecord(outer) && isRecord(outer.data) ? outer.data : outer
  for (const envelope of [data, outer]) {
    const value = isRecord(envelope) ? Number(envelope.agentId ?? envelope.agent_id) : NaN
    if (Number.isFinite(value) && value > 0) return value
  }
  return null
}

async function botAgentId() {
  try {
    return await authorizedPost('/api/v1/imRobot/detail', '{}', agentId)
  } catch {
    return null
  }
}

function groupReceipt(payload) {
  const data = isRecord(payload) && isRecord(payload.data) ? payload.data : payload
  const identity = isRecord(data)
    // The create endpoint answers with `groupid`; the other group APIs use `groupId`.
    ? readString(data.groupid || data.groupId || data.group_id || data.chatId)
    : ''
  if (!identity) {
    throw new Error(`group create rejected: ${JSON.stringify(payload)}`)
  }
  const failed = isRecord(data) && Array.isArray(data.failMembers) ? data.failMembers : []
  if (failed.length) {
    throw new Error(`group members rejected: ${JSON.stringify(failed)}`)
  }
  const bot = isRecord(data) ? readString(data.botId || data.bot_id) : ''
  return bot ? { group_id: identity, bot_id: bot } : { group_id: identity }
}

async function createGroup(request) {
  const self = await botAgentId()
  const body = JSON.stringify({
    groupName: request.group_name,
    groupOwner: request.owner,
    memberList: request.members,
    robotList: self === null ? [] : [self],
    friendlyLevel: request.friendly_level,
    searchAbility: 0,
    managers: [],
    robotManagers: [],
  })
  return await authorizedPost('/api/v1/robot/group/create', body, groupReceipt)
}

async function sendGroupMarkdown(groupId, content, atUsers) {
  const blocks = atUsers.length
    ? [{ type: 'AT', atall: false, atuserids: atUsers }, { type: 'MD', content }]
    : [{ type: 'MD', content }]
  const body = JSON.stringify({
    message: {
      header: {
        toid: Number(groupId),
        totype: 'GROUP',
        msgtype: 'MD',
        clientmsgid: Date.now(),
        role: 'robot',
      },
      body: blocks,
    },
  })
  return await authorizedPost('/api/v1/robot/msg/groupmsgsend', body, sendReceipt)
}

const seen = new Set()

// A gate used to be answered by typing `APPROVE <32 hex>`, which on a phone means
// pasting the id back out of the card. The bubble workflow card turns that into one
// tap. The approval id is encoded into the button's own `eventKey` rather than kept
// in gateway memory, so a restart between sending the card and the tap still yields a
// readable decision.
const CARD_CALLER = 'tom-autodev'
const DECISION_BY_BUTTON = { approve: 'APPROVE', reject: 'REJECT' }

function buttonEventKey(button, approvalId) {
  return `${button}.${approvalId}`
}

function parseDecision(eventKey) {
  const raw = readString(eventKey).trim()
  const gate = /^(approve|reject)\.([0-9a-f]{32})$/.exec(raw)
  if (gate) return { text: `${DECISION_BY_BUTTON[gate[1]]} ${gate[2]}`, subject: gate[2] }
  // A run also has to ask questions that are not gates ("continue into PLAN?"). Those
  // arrive as an opaque decision id plus the chosen option, so the asking side can
  // recognise the answer without the gateway knowing what the options meant.
  const choice = /^choice\.([0-9a-f]{16})\.([a-z0-9_-]{1,32})$/.exec(raw)
  if (choice) return { text: `CHOICE ${choice[1]} ${choice[2]}`, subject: choice[1] }
  return null
}

let cardApi = null

async function interactiveCards() {
  if (cardApi) return cardApi
  // The card relay is reached through the full SDK client, which needs the bot's
  // agentId; the REST calls above only need the app token.
  const agent = await botAgentId()
  if (agent === null) throw new Error('bot agentId unavailable, cannot render cards')
  cardApi = new Client({
    appKey,
    appSecret,
    agentId: String(agent),
    baseUrl: `${baseUrl()}/api/v1`,
    loggerLevel: LogLevel.info,
  }).im.bubbleWorkflowCard
  return cardApi
}

async function sendApprovalCard(request) {
  return await renderCard({
    card_id: `approval-${request.approval_id}`,
    request_id: request.approval_id,
    target_type: request.target_type,
    target_id: request.target_id,
    title: request.title,
    question: request.question,
    lines: request.lines,
    buttons: [
      { eventKey: buttonEventKey('approve', request.approval_id), text: '同意' },
      { eventKey: buttonEventKey('reject', request.approval_id), text: '驳回' },
    ],
  })
}

async function sendChoiceCard(request) {
  return await renderCard({
    card_id: `choice-${request.decision_id}`,
    request_id: request.decision_id,
    target_type: request.target_type,
    target_id: request.target_id,
    title: request.title,
    question: request.question,
    lines: request.lines,
    buttons: request.options.map((option) => ({
      eventKey: `choice.${request.decision_id}.${option.key}`,
      text: option.text,
    })),
  })
}

async function renderCard(request) {
  const cards = await interactiveCards()
  const components = request.lines
    .slice(0, 8)
    .map((text, index) => ({ componentId: `line-${index}`, type: 'text', text }))
  const result = await cards.render({
    // One card per subject: resending refreshes the same bubble instead of stacking a
    // second one that could still be tapped after the first was answered.
    cardId: request.card_id,
    caller: { callerName: CARD_CALLER, requestId: request.request_id },
    question: request.question,
    receiver: { type: request.target_type, id: request.target_id },
    card: { title: request.title, components, buttons: request.buttons },
    // Left off deliberately. The platform's own submitting state replaces the card
    // with a "补充说明 + 提交" form, which reads as a second thing to fill in and
    // races the terminal refresh below — that refresh then fails with
    // `刷新外层气泡卡失败`. Answering the card ourselves is both clearer and the only
    // one of the two that actually lands.
    refreshDefaultIntermediateState: false,
  })
  if (!isRecord(result) || result.ok !== true) {
    throw new Error(`card rejected: ${JSON.stringify(result).slice(0, 400)}`)
  }
  const info = isRecord(result.cardInfo) ? result.cardInfo : {}
  if (request.buttons?.length) {
    // Kept so the answered card can name the option the person chose. It is a nicety:
    // a gateway restart loses it and `answeredCard` falls back to what the submit
    // event itself carries.
    live.set(request.card_id, {
      request_id: request.request_id,
      target_type: request.target_type,
      target_id: request.target_id,
      title: request.title,
      question: request.question,
      lines: [...request.lines],
      labels: Object.fromEntries(request.buttons.map((button) => [button.eventKey, button.text])),
    })
  }
  return {
    card_id: readString(info.cardId) || request.card_id,
    created: result.created === true,
    revision: Number.isFinite(Number(info.revision)) ? Number(info.revision) : null,
  }
}

const live = new Map()

async function answerCard(data, eventKey, sender) {
  const cardId = readString(data.cardId)
  if (!cardId) return
  const known = live.get(cardId)
  const receiver = isRecord(data.receiver) ? data.receiver : {}
  const targetType = known?.target_type || readString(receiver.type)
  const targetId = known?.target_id || readString(receiver.id)
  if (!['group', 'user'].includes(targetType) || !targetId) return
  const submit = isRecord(data.submit) ? data.submit : {}
  // The submit event echoes the text components back as `label`, so the card can be
  // rebuilt even when this process never sent it.
  const echoed = Array.isArray(submit.components)
    ? submit.components
        .filter((item) => isRecord(item) && readString(item.type) === 'text')
        .map((item) => readString(item.label))
        .filter(Boolean)
    : []
  const lines = known?.lines?.length ? known.lines : echoed
  const label = known?.labels?.[readString(eventKey).trim()] || readString(eventKey).trim()
  await renderCard({
    card_id: cardId,
    request_id: known?.request_id || readString(isRecord(data.caller) ? data.caller.requestId : ''),
    target_type: targetType,
    target_id: targetId,
    title: known?.title || '',
    question: `已由 ${sender} 处理：${label}`,
    // No buttons: a terminal card must not offer a tap that would run anything again.
    buttons: [],
    // The answered card appends one line of its own, so the caller's lines are capped
    // one short of the platform's eight-component ceiling.
    lines: [...lines.slice(0, 7), `已选择「${label}」· ${sender} · ${nowIso()}`],
  })
  live.delete(cardId)
}

function journalInteraction(data) {
  if (!isRecord(data)) return
  const submit = isRecord(data.submit) ? data.submit : {}
  const parsed = parseDecision(submit.eventKey ?? submit.event_key)
  if (parsed === null) {
    log('interaction submit ignored', { reason: 'no decision button' })
    return
  }
  const sender = uuapSender(data.userId)
  if (!sender) {
    // Only a uuap prefix can be matched against an approval's member policy. A
    // numeric submitter would authorize nothing, and inventing a mapping here would
    // be worse than leaving the typed reply as the way in.
    log('interaction submit ignored', {
      reason: 'submitter not a uuap',
      subject: parsed.subject,
    })
    return
  }
  const groupId = readString(data.groupId)
  journal(
    // `eventId` is the platform's delivery idempotency id, so a redelivered tap
    // lands on the same journal id instead of looking like a second decision.
    { msgkey: readString(data.eventId) },
    {
      chat_type: groupId ? 'group' : 'single',
      sender,
      group_id: groupId,
      text: parsed.text,
    },
  )
  // The decision is already recorded, so a refresh that fails must only be logged: a
  // card stuck on its open state is a worse outcome than losing the answer would be,
  // but not worth dropping the answer for.
  answerCard(data, submit.eventKey ?? submit.event_key, sender).catch((err) => {
    log('answered card refresh failed', {
      subject: parsed.subject,
      error: err instanceof Error ? err.message : String(err),
    })
  })
}

function journal(raw, normalized) {
  if (!normalized.sender || !normalized.text) return
  const entry = {
    message_id: messageId(raw, JSON.stringify(raw)),
    chat_type: normalized.chat_type,
    sender: normalized.sender,
    text: normalized.text,
    received_at: nowIso(),
  }
  // A group reply must be answerable only in the group the run belongs to, so the
  // consumer needs to know where it was typed.
  if (normalized.group_id) entry.group_id = normalized.group_id
  if (seen.has(entry.message_id)) return
  seen.add(entry.message_id)
  const path = journalPath()
  mkdirSync(dirname(path), { recursive: true })
  appendFileSync(path, `${JSON.stringify(entry)}\n`, 'utf8')
  log('reply journalled', { message_id: entry.message_id, chat_type: entry.chat_type })
}

function messageId(raw, fallback) {
  const known = readString(raw.MsgId ?? raw.msgid ?? raw.MsgID ?? raw.msgkey ?? raw.msgId)
  if (known) return known
  // Duplicate deliveries of the same payload must land on the same id, otherwise a
  // relay restart would look like a second decision.
  return `sha256-${createHash('sha256').update(fallback).digest('hex').slice(0, 32)}`
}

function extractPrivateText(raw) {
  return readString(raw.Content ?? raw.content ?? raw.Text ?? raw.text).trim()
}

function extractGroupText(raw) {
  const message = isRecord(raw.message) ? raw.message : {}
  const blocks = Array.isArray(message.body) ? message.body : []
  return blocks
    .filter((block) => isRecord(block) && readString(block.type).toUpperCase() !== 'AT')
    .map((block) => readString(block.content ?? block.label ?? block.url))
    .join('')
    .trim()
}

function normalizePrivateMessage(raw) {
  return {
    chat_type: 'single',
    sender: readString(raw.FromUserId ?? raw.FromUserName),
    text: extractPrivateText(raw),
  }
}

function normalizeGroupMessage(raw) {
  const message = isRecord(raw.message) ? raw.message : {}
  const header = isRecord(message.header) ? message.header : {}
  return {
    chat_type: 'group',
    // Only a uuap prefix can be mapped back onto an approval's member policy. A
    // message from a robot carries no `fromuserid` and `raw.fromid` is then a numeric
    // robot id, which authorizes nothing — journalling it would only look like a
    // sender, so such a message is dropped by `journal` instead.
    sender: uuapSender(header.fromuserid ?? header.fromUserId),
    group_id: readString(raw.groupid ?? raw.groupId),
    text: extractGroupText(raw),
  }
}

function uuapSender(value) {
  const sender = readString(value).trim()
  return /^[0-9]+$/.test(sender) ? '' : sender
}

function parseJsonBody(req) {
  return new Promise((resolve, reject) => {
    let body = ''
    req.setEncoding('utf8')
    req.on('data', (chunk) => {
      body += chunk
      if (body.length > 512 * 1024) {
        reject(new Error('request body too large'))
        req.destroy()
      }
    })
    req.on('end', () => {
      if (!body.trim()) {
        resolve({})
        return
      }
      try {
        const parsed = JSON.parse(body)
        resolve(isRecord(parsed) ? parsed : {})
      } catch (err) {
        reject(err)
      }
    })
    req.on('error', reject)
  })
}

function sendJson(res, statusCode, payload) {
  const data = JSON.stringify(payload)
  res.writeHead(statusCode, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': Buffer.byteLength(data),
  })
  res.end(data)
}

const internal = process.env.INFOFLOW_NETWORK === 'internal'
const wsClient = new WSClient({
  appId: appKey,
  appSecret,
  wsGateway: process.env.INFOFLOW_WS_GATEWAY || (internal ? INTERNAL_WS_GATEWAY : undefined),
  wsConnectDomain: process.env.INFOFLOW_WS_CONNECT_DOMAIN
    || (internal ? INTERNAL_WS_CONNECT_DOMAIN : undefined),
  loggerLevel: LogLevel.info,
})

wsClient.on('private.*', (event) => {
  const raw = event?.data?.raw
  if (!isRecord(raw) || !raw.MsgType) return
  if (readString(raw.MsgType).toLowerCase() === 'event') return
  journal(raw, normalizePrivateMessage(raw))
})

wsClient.on('group.*', (event) => {
  const raw = event?.data?.raw
  if (!isRecord(raw)) return
  if (readString(raw.eventtype ?? raw.eventType) !== 'MESSAGE_RECEIVE') return
  journal(raw, normalizeGroupMessage(raw))
})

wsClient.on('event.interaction_submit', (event) => {
  journalInteraction(event?.data)
})

await wsClient.connect()
log('bot websocket connected', { journal: journalPath() })

const host = process.env.TOM_AUTODEV_INFOFLOW_GATEWAY_HOST || DEFAULT_HOST
const port = Number(process.env.TOM_AUTODEV_INFOFLOW_GATEWAY_PORT || DEFAULT_PORT)
const server = createServer(async (req, res) => {
  try {
    const url = new URL(req.url || '/', `http://${host}:${port}`)
    if (req.method === 'GET' && url.pathname === '/health') {
      sendJson(res, 200, { ok: true, app_key: appKey, journal: journalPath() })
      return
    }
    if (req.method === 'POST' && url.pathname === '/notify') {
      const body = await parseJsonBody(req)
      const recipients = Array.isArray(body.recipients)
        ? body.recipients.map(readString).filter(Boolean)
        : []
      const content = readString(body.content)
      if (!recipients.length || !content) {
        sendJson(res, 400, { ok: false, error: 'recipients and content are required' })
        return
      }
      const messageKey = await sendMarkdown(recipients, content)
      sendJson(res, 200, { ok: true, message_key: messageKey, recipients })
      return
    }
    if (req.method === 'POST' && url.pathname === '/group/create') {
      const body = await parseJsonBody(req)
      const members = Array.isArray(body.members)
        ? body.members.map(readString).filter(Boolean)
        : []
      const groupName = readString(body.group_name)
      const owner = readString(body.owner)
      if (!groupName || !owner || !members.length || body.friendly_level !== 3) {
        sendJson(res, 400, { ok: false, error: 'group_name, owner, members and friendly_level 3 are required' })
        return
      }
      const receipt = await createGroup({
        group_name: groupName, owner, members, friendly_level: 3,
      })
      sendJson(res, 200, { ok: true, ...receipt, group_name: groupName })
      return
    }
    if (req.method === 'POST' && url.pathname === '/group/members') {
      const body = await parseJsonBody(req)
      const groupId = readString(body.group_id)
      if (!/^\d+$/.test(groupId)) {
        sendJson(res, 400, { ok: false, error: 'numeric group_id is required' })
        return
      }
      const members = await authorizedPost(
        '/api/v1/robot/group/memberList',
        JSON.stringify({ groupId: Number(groupId), recallType: 0 }),
        (payload) => payload,
      )
      sendJson(res, 200, { ok: true, group_id: groupId, response: members })
      return
    }
    if (req.method === 'POST' && url.pathname === '/group/disband') {
      const body = await parseJsonBody(req)
      const groupId = readString(body.group_id)
      const owner = readString(body.owner)
      if (!/^\d+$/.test(groupId) || !owner) {
        sendJson(res, 400, { ok: false, error: 'numeric group_id and owner are required' })
        return
      }
      await authorizedPost(
        '/api/v1/robot/group/disband',
        JSON.stringify({ groupId: Number(groupId), groupOwner: owner }),
        (payload) => {
          const data = isRecord(payload) && isRecord(payload.data) ? payload.data : payload
          const code = isRecord(data) ? data.errcode : undefined
          if (code !== undefined && code !== null && String(code) !== '0') {
            throw new Error(`group disband rejected: ${JSON.stringify(payload)}`)
          }
          return true
        },
      )
      sendJson(res, 200, { ok: true, group_id: groupId })
      return
    }
    if (req.method === 'POST' && url.pathname === '/group/message') {
      const body = await parseJsonBody(req)
      const groupId = readString(body.group_id)
      const content = readString(body.content)
      const atUsers = Array.isArray(body.at_users)
        ? body.at_users.map(readString).filter(Boolean)
        : []
      if (!/^\d+$/.test(groupId) || !content) {
        sendJson(res, 400, { ok: false, error: 'numeric group_id and content are required' })
        return
      }
      const messageKey = await sendGroupMarkdown(groupId, content, atUsers)
      sendJson(res, 200, { ok: true, message_key: messageKey, group_id: groupId })
      return
    }
    if (req.method === 'POST' && url.pathname === '/card/approval') {
      const body = await parseJsonBody(req)
      const approvalId = readString(body.approval_id).trim()
      const targetType = readString(body.target_type).trim()
      const targetId = readString(body.target_id).trim()
      const title = readString(body.title).trim()
      const question = readString(body.question).trim()
      const lines = Array.isArray(body.lines)
        ? body.lines.map((line) => readString(line).trim()).filter(Boolean)
        : []
      if (
        !/^[0-9a-f]{32}$/.test(approvalId)
        || !['group', 'user'].includes(targetType)
        || !targetId
        || !title
      ) {
        sendJson(res, 400, {
          ok: false,
          error: 'approval_id (32 hex), target_type (group|user), target_id and title are required',
        })
        return
      }
      const receipt = await sendApprovalCard({
        approval_id: approvalId,
        target_type: targetType,
        target_id: targetId,
        title,
        question,
        lines,
      })
      sendJson(res, 200, { ok: true, approval_id: approvalId, ...receipt })
      return
    }
    if (req.method === 'POST' && url.pathname === '/card/choice') {
      const body = await parseJsonBody(req)
      const decisionId = readString(body.decision_id).trim()
      const targetType = readString(body.target_type).trim()
      const targetId = readString(body.target_id).trim()
      const title = readString(body.title).trim()
      const options = Array.isArray(body.options)
        ? body.options
            .filter(isRecord)
            .map((option) => ({
              key: readString(option.key).trim(),
              text: readString(option.text).trim(),
            }))
            .filter((option) => /^[a-z0-9_-]{1,32}$/.test(option.key) && option.text)
        : []
      const lines = Array.isArray(body.lines)
        ? body.lines.map((line) => readString(line).trim()).filter(Boolean)
        : []
      if (
        !/^[0-9a-f]{16}$/.test(decisionId)
        || !['group', 'user'].includes(targetType)
        || !targetId
        || !title
        // Six is the card's own button ceiling, and one option is not a question.
        || options.length < 2
        || options.length > 6
      ) {
        sendJson(res, 400, {
          ok: false,
          error: 'decision_id (16 hex), target_type (group|user), target_id, title and 2-6 options are required',
        })
        return
      }
      const receipt = await sendChoiceCard({
        decision_id: decisionId,
        target_type: targetType,
        target_id: targetId,
        title,
        question: readString(body.question).trim(),
        lines,
        options,
      })
      sendJson(res, 200, { ok: true, decision_id: decisionId, ...receipt })
      return
    }
    sendJson(res, 404, { ok: false, error: 'not found' })
  } catch (err) {
    sendJson(res, 500, { ok: false, error: err instanceof Error ? err.message : String(err) })
  }
})

server.listen(port, host, () => {
  log('tom-autodev infoflow bot gateway listening', { url: `http://${host}:${port}` })
})
