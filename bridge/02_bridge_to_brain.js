// 02_bridge_to_brain.js — STEP 2
// Goal: connect the "ears and mouth" (this file) to the "brain" (Python FastAPI).
// Flow: message arrives -> send it to the brain -> brain answers reply / hold / ignore -> we act.
//
// Run the brain FIRST (see README), then:  npm run step2
// Note: stop 01_echo_bot.js before running this. Both use the same WhatsApp login.

const { Client, LocalAuth } = require('whatsapp-web.js');
const qrcode = require('qrcode-terminal');
const { ALLOWED_NUMBERS, BRAIN_URL, BRAIN_TIMEOUT_MS } = require('./config');

const client = new Client({
  authStrategy: new LocalAuth({ clientId: 'stanley-agent' }), // same login as Step 1
  puppeteer: { headless: true },
});

client.on('qr', (qr) => {
  console.log('\nScan this with your phone: WhatsApp > Linked devices > Link a device\n');
  qrcode.generate(qr, { small: true });
});
client.on('ready', () => console.log('Bridge connected. Forwarding messages to the brain...\n'));
client.on('auth_failure', (m) => console.error('Login failed:', m));
client.on('disconnected', (reason) => console.log('Disconnected:', reason));

// Sends one message to the Python brain and returns its decision.
// Returns null if the brain is off or too slow. In that case we do NOTHING (never guess).
async function askBrain(payload) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), BRAIN_TIMEOUT_MS);
  try {
    const res = await fetch(BRAIN_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
    if (!res.ok) {
      console.error(`   brain error ${res.status}: ${await res.text()}`);
      return null;
    }
    return await res.json();
  } catch (err) {
    console.error('   brain not reachable:', err.name === 'AbortError' ? 'timed out' : err.message);
    return null;
  } finally {
    clearTimeout(timer);
  }
}

client.on('message', async (msg) => {
  if (msg.isStatus || msg.fromMe || msg.from.endsWith('@g.us')) return;

  const contact = await msg.getContact();
  const name = contact.name || contact.pushname || 'Unknown';
  const number = contact.number || msg.from.split('@')[0];

  // Everything the brain needs to know about this message.
  const payload = {
    message_id: msg.id._serialized,
    chat_id: msg.from,
    sender_name: name,
    sender_number: number,
    type: msg.type,            // 'chat' = text, 'ptt' = voice note, 'image', ...
    text: msg.body || '',
    has_media: msg.hasMedia,
    timestamp: msg.timestamp,  // seconds since 1970
  };

  console.log(`IN  ${name} (${number}) [${msg.type}]: ${payload.text || '(no text)'}`);

  const decision = await askBrain(payload);
  if (!decision) {
    console.log('   -> no decision, not replying');
    return;
  }

  if (decision.action === 'reply' && decision.text) {
    if (!ALLOWED_NUMBERS.includes(number)) {
      console.log(`   -> brain wanted to reply, but ${number} is not in ALLOWED_NUMBERS. Held.`);
      return;
    }
    await client.sendMessage(msg.from, decision.text);
    console.log(`OUT ${name}: ${decision.text}`);
  } else if (decision.action === 'hold') {
    console.log(`   -> HELD for Stanley (${decision.reason || 'no reason given'})`);
  } else {
    console.log('   -> ignored');
  }
});

client.initialize();
