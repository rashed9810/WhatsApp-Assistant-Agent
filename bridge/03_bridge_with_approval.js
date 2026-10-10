// 03_bridge_with_approval.js — STEP 3
// New compared with Step 2:
//   1. Messages YOU type on your phone are reported to the brain (to learn your style later).
//   2. Every few seconds the bridge checks the brain's "outbox" for replies you approved, and sends them.
//
// Run the Step 3 brain FIRST:  uvicorn main_v3:app --reload --port 8000
// Then:                        node 03_bridge_with_approval.js
// Stop any earlier bridge file before running this one (they share one WhatsApp login).

const { Client, LocalAuth } = require('whatsapp-web.js');
const qrcode = require('qrcode-terminal');
const { ALLOWED_NUMBERS, BRAIN_URL, BRAIN_TIMEOUT_MS } = require('./config');

const BRAIN_BASE = BRAIN_URL.replace(/\/incoming$/, ''); // http://127.0.0.1:8000
const OUTBOX_EVERY_MS = 3000;

const client = new Client({
  authStrategy: new LocalAuth({ clientId: 'stanley-agent' }),
  puppeteer: { headless: true },
});

const botSentIds = new Set();      // ids of messages the BOT sent (so we don't label them as yours)
const warnedNotAllowed = new Set(); // outbox items we refused, so we only warn once

// ---------- talking to the brain ----------

async function callBrain(path, method = 'GET', body = undefined) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), BRAIN_TIMEOUT_MS);
  try {
    const res = await fetch(BRAIN_BASE + path, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined,
      signal: controller.signal,
    });
    if (!res.ok) {
      console.error(`   brain error ${res.status} on ${path}: ${await res.text()}`);
      return null;
    }
    return await res.json();
  } catch (err) {
    console.error(`   brain not reachable (${path}):`, err.name === 'AbortError' ? 'timed out' : err.message);
    return null;
  } finally {
    clearTimeout(timer);
  }
}

// Sends a message as the bot, and tells the brain it went out.
async function sendAsBot(chatId, number, text) {
  const sent = await client.sendMessage(chatId, text);
  botSentIds.add(sent.id._serialized);
  await callBrain('/outgoing', 'POST', {
    message_id: sent.id._serialized,
    chat_id: chatId,
    number,
    text,
    type: 'chat',
    timestamp: Math.floor(Date.now() / 1000),
    source: 'bot',
  });
}

function isPrivateChat(id) {
  return !id.endsWith('@g.us') && id !== 'status@broadcast';
}

// ---------- WhatsApp events ----------

client.on('qr', (qr) => {
  console.log('\nScan this with your phone: WhatsApp > Linked devices > Link a device\n');
  qrcode.generate(qr, { small: true });
});
client.on('auth_failure', (m) => console.error('Login failed:', m));
client.on('disconnected', (reason) => console.log('Disconnected:', reason));

client.on('ready', () => {
  console.log('Bridge (Step 3) connected. Approval queue is active.\n');
  setInterval(checkOutbox, OUTBOX_EVERY_MS);
});

// A) Someone writes to you
client.on('message', async (msg) => {
  if (msg.isStatus || msg.fromMe || !isPrivateChat(msg.from)) return;

  const contact = await msg.getContact();
  const name = contact.name || contact.pushname || 'Unknown';
  const number = contact.number || msg.from.split('@')[0];

  console.log(`IN  ${name} (${number}) [${msg.type}]: ${msg.body || '(no text)'}`);

  const decision = await callBrain('/incoming', 'POST', {
    message_id: msg.id._serialized,
    chat_id: msg.from,
    sender_name: name,
    sender_number: number,
    type: msg.type,
    text: msg.body || '',
    has_media: msg.hasMedia,
    timestamp: msg.timestamp,
  });

  if (!decision) return console.log('   -> no decision, not replying');

  if (decision.action === 'reply' && decision.text) {
    if (!ALLOWED_NUMBERS.includes(number)) {
      return console.log(`   -> ${number} not in ALLOWED_NUMBERS, not sending`);
    }
    await sendAsBot(msg.from, number, decision.text);
    console.log(`OUT ${name} (auto): ${decision.text}`);
  } else if (decision.action === 'hold') {
    console.log(`   -> HELD: ${decision.reason}. Approve it at ${BRAIN_BASE}/docs`);
  }
});

// B) A message LEAVES your account (you typed it on your phone, or the bot sent it)
client.on('message_create', async (msg) => {
  if (!msg.fromMe || !isPrivateChat(msg.to)) return;

  // Wait a moment: if the bot sent this, sendAsBot() will have registered its id by then.
  await new Promise((r) => setTimeout(r, 1500));
  if (botSentIds.has(msg.id._serialized)) return;

  const chat = await msg.getChat();
  const contact = await chat.getContact();
  const number = contact.number || msg.to.split('@')[0];

  await callBrain('/outgoing', 'POST', {
    message_id: msg.id._serialized,
    chat_id: msg.to,
    number,
    text: msg.body || '',
    type: msg.type,
    timestamp: msg.timestamp,
    source: 'owner_phone',
  });
  console.log(`YOU -> ${contact.name || contact.pushname || number}: ${msg.body || `(${msg.type})`}`);
});

// C) Every 3 seconds: send replies you approved
let checking = false;
async function checkOutbox() {
  if (checking) return; // don't start a second check while one is running
  checking = true;
  try {
    const items = await callBrain('/outbox');
    for (const item of items || []) {
      if (!ALLOWED_NUMBERS.includes(item.number)) {
        if (!warnedNotAllowed.has(item.id)) {
          console.log(`   approved #${item.id} is for ${item.number}, which is not in ALLOWED_NUMBERS. Not sent.`);
          warnedNotAllowed.add(item.id);
        }
        continue;
      }
      await sendAsBot(item.chat_id, item.number, item.final_text);
      await callBrain(`/outbox/${item.id}/sent`, 'POST');
      console.log(`OUT ${item.display_name || item.number} (approved #${item.id}): ${item.final_text}`);
    }
  } finally {
    checking = false;
  }
}

client.initialize();
