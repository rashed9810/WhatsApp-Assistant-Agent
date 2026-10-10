// config.js — settings shared by both bridge files.
// Edit ONLY this file to change who the bot is allowed to reply to.

module.exports = {
  // SAFETY LIST: the bot will only ever send replies to these numbers.
  // Format: country code + number, no "+", no spaces, no dashes.
  // Bangladesh example: '8801712345678'
  // Put your test phone here (a friend who agreed to help, or your second SIM).
  ALLOWED_NUMBERS: ['8801XXXXXXXXX'],

  // Where the Python brain is running (Step 2). Leave as is unless you change the port.
  BRAIN_URL: 'http://127.0.0.1:8000/incoming',

  // How long to wait for the brain before giving up (milliseconds).
  BRAIN_TIMEOUT_MS: 15000,
};
