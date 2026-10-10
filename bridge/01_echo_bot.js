// 01_echo_bot.js — STEP 1
// Goal: prove we can log in to WhatsApp, READ messages, and SEND a reply.
// It prints every incoming message, but replies "got it" ONLY to numbers in config.js.
//
// Run:  npm run step1

const { Client, LocalAuth } = require('whatsapp-web.js');
const qrcode = require('qrcode-terminal');
const { ALLOWED_NUMBERS } = require('./config');

const client = new Client({
  // LocalAuth saves your login in a ".wwebjs_auth" folder,
  // so you only scan the QR code the first time.
  authStrategy: new LocalAuth({ clientId: 'stanley-agent' }),
  puppeteer: { headless: true }, // runs a hidden Chrome in the background
});

// 1) WhatsApp gives us a QR code -> print it in the terminal so you can scan it.
client.on('qr', (qr) => {
  console.log('\nScan this with your phone: WhatsApp > Linked devices > Link a device\n');
  qrcode.generate(qr, { small: true });
});

client.on('authenticated', () => console.log('Logged in, loading chats...'));
client.on('ready', () => console.log('Connected to WhatsApp. Waiting for messages...\n'));
client.on('auth_failure', (m) => console.error('Login failed:', m));
client.on('disconnected', (reason) => console.log('Disconnected:', reason));

// 2) Every time someone sends you a message, this function runs.
client.on('message', async (msg) => {
  // Skip things we don't want to touch yet: status updates, groups, our own messages.
  if (msg.isStatus || msg.fromMe || msg.from.endsWith('@g.us')) return;

  const contact = await msg.getContact();
  const name = contact.name || contact.pushname || 'Unknown';
  const number = contact.number || msg.from.split('@')[0];
  const time = new Date(msg.timestamp * 1000).toLocaleTimeString();

  console.log(`[${time}] ${name} (${number}) [${msg.type}]: ${msg.body || '(no text)'}`);

  // 3) SAFETY: only reply to people on the allowed list.
  if (!ALLOWED_NUMBERS.includes(number)) return;

  await client.sendMessage(msg.from, 'got it (test bot)');
  console.log(`   -> replied to ${name}`);
});

client.initialize();
