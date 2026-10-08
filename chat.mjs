import net from 'node:net';
import tls from 'node:tls';
import { readFileSync } from 'node:fs';
import { randomUUID } from 'node:crypto';
import { createInterface, clearLine, cursorTo } from 'node:readline';
import { newCode, roomKeys, encrypt, decrypt, safeText } from './lib/crypto.mjs';
import { readFrames, send } from './lib/wire.mjs';
import { options, portNumber } from './lib/options.mjs';

const output = process.stdout;
const interactive = Boolean(process.stdin.isTTY && output.isTTY);
const color = interactive && !Object.hasOwn(process.env, 'NO_COLOR');
const green = (text) => color ? `\x1b[32m${text}\x1b[0m` : text;
const dim = (text) => color ? `\x1b[2;32m${text}\x1b[0m` : text;
let rl;
let socket;
let heartbeat;
let connectTimeout;
let closing = false;
let ready = false;
let keys;
let name;
let seq = 0;
const session = randomUUID();
const seen = new Map();

function line(text) {
  if (interactive && ready && rl && !closing) {
    clearLine(output, 0);
    cursorTo(output, 0);
  }
  output.write(`${text}\n`);
  if (interactive && ready && rl && !closing) rl.prompt(true);
}

function packet(kind, text) {
  return encrypt(keys, { kind, name, text, session, seq: ++seq });
}

function stop(message, failed = false) {
  if (closing) return;
  closing = true;
  clearInterval(heartbeat);
  clearTimeout(connectTimeout);
  if (ready && socket && !socket.destroyed) send(socket, packet('bye', ''));
  rl?.close();
  if (!interactive && rl) process.stdin.destroy();
  socket?.end();
  const forceClose = setTimeout(() => socket?.destroy(), 1000);
  forceClose.unref();
  if (message) line(dim(message));
  if (failed) process.exitCode = 1;
}

async function main() {
  const args = options({
    host: { type: 'string', default: '127.0.0.1' },
    port: { type: 'string', default: '4040' },
    name: { type: 'string' }, new: { type: 'boolean' },
    tls: { type: 'boolean' }, ca: { type: 'string' }, help: { type: 'boolean' },
  });
  if (args.help) {
    console.log('node chat.mjs [--host SERVER] [--port 4040] [--name LAQAB] [--new] [--tls --ca ca.pem]\nBuyruqlar: /help /clear /who /exit\nKirish kodi terminalda so‘raladi.');
    return;
  }
  const port = portNumber(args.port);
  if (args.ca && !args.tls) throw new Error('--ca faqat --tls bilan ishlaydi.');
  line(green('\n  D A R K   T E R M I N A L\n  ──────────────────────────────'));
  line(dim('  Kod bilan kirish. Jonli suhbat.\n'));
  if (!interactive && (!args.name || (!args.new && !process.env.DARK_CHAT_CODE))) {
    throw new Error('Interaktiv terminalni oching yoki --name va DARK_CHAT_CODE muhit o‘zgaruvchisini bering.');
  }
  rl = createInterface({ input: process.stdin, output, terminal: interactive, historySize: 0 });
  // Piped clients must keep stdin paused until the handshake completes.
  if (!interactive) rl.pause();
  rl.on('SIGINT', () => stop('\nAloqa yopildi.'));
  const ask = (prompt) => new Promise((resolve, reject) => {
    const onClose = () => reject(new Error('Kirish bekor qilindi.'));
    rl.once('close', onClose);
    rl.question(green(prompt), (answer) => { rl.off('close', onClose); resolve(answer.trim()); });
  });
  name = safeText(args.name ?? await ask('  Laqabingiz: ')).trim();
  if (!name || [...name].length > 24) throw new Error('Laqab 1–24 belgidan iborat bo‘lsin.');
  let create = args.new;
  if (!create && !process.env.DARK_CHAT_CODE) {
    create = (await ask('  [1] Yangi suhbat  [2] Kod bilan kirish (2): ')) === '1';
  }
  const code = create ? newCode() : (process.env.DARK_CHAT_CODE?.trim() ?? await ask('  Kirish kodi: '));
  keys = roomKeys(code);
  // Do not leave the invite in the child process environment after startup.
  delete process.env.DARK_CHAT_CODE;
  line(dim(`\n  Ulanmoqda: ${safeText(args.host)}:${port} ...`));
  const tlsOptions = args.tls ? {
    minVersion: 'TLSv1.2',
    ...(args.ca ? { ca: readFileSync(args.ca) } : {}),
    ...(net.isIP(args.host) ? {} : { servername: args.host }),
  } : undefined;
  socket = args.tls ? tls.connect({ host: args.host, port, ...tlsOptions }) : net.createConnection({ host: args.host, port });
  socket.setNoDelay(true);
  socket.setKeepAlive(true, 30000);
  connectTimeout = setTimeout(() => { socket.destroy(); stop('Ulanish vaqti tugadi.', true); }, 10000);
  socket.once(args.tls ? 'secureConnect' : 'connect', () => send(socket, { type: 'join', room: keys.room }));
  socket.on('error', (error) => stop(`Ulanish xatosi: ${safeText(error.code ?? error.message)}. Server manzilini tekshiring.`, true));
  socket.on('close', () => { if (!closing) stop('Server bilan aloqa uzildi.', true); });
  let lastPong = Date.now();
  readFrames(socket, (frame) => {
    if (closing) return;
    if (frame?.type === 'ready' && !ready) {
      clearTimeout(connectTimeout);
      if (create) {
        line(green(`\n  KIRISH KODI: ${code}`));
        line(dim('  Sherigingizga shu kod va server manzilini yuboring.\n'));
      }
      line(green(`  ACCESS GRANTED / ${name}`));
      line(dim('  Xabarni yozing va Enter bosing. /help — buyruqlar.'));
      line(dim('  Xabar tarixi saqlanmaydi. /exit — chiqish.\n'));
      ready = true;
      send(socket, packet('hello', ''));
      heartbeat = setInterval(() => {
        if (Date.now() - lastPong > 45000) { socket.destroy(); stop('Server javob bermayapti.', true); }
        else send(socket, { type: 'ping' });
      }, 15000);
      rl.setPrompt(green(`${name}> `));
      rl.on('line', (input) => {
        if (closing) return;
        const text = safeText(input).trim();
        if (!text) return interactive && rl.prompt();
        if (text === '/exit' || text === '/quit') return stop('Aloqa yopildi.');
        if (text === '/help') return line(dim('/who — tanishgan ishtirokchilar; /clear — ekranni tozalash; /exit — chiqish'));
        if (text === '/clear') {
          if (interactive) output.write('\x1b[2J\x1b[H');
          return line(dim('  DARK TERMINAL / suhbat davom etmoqda'));
        }
        if (text === '/who') {
          const peers = [...seen.values()].filter((peer) => peer.online).map((peer) => peer.name);
          return line(dim(`Ishtirokchilar: ${[name, ...peers].join(', ')}`));
        }
        if (text.startsWith('/')) return line(dim('Noma’lum buyruq. /help ni yozing.'));
        if ([...text].length > 2000) return line(dim('Xabar 2000 belgidan oshmasin.'));
        send(socket, packet('chat', text));
        if (!interactive) line(green(`${name}> ${text}`));
        if (interactive) rl.prompt();
      });
      rl.on('close', () => stop('Aloqa yopildi.'));
      rl.resume();
      if (interactive) rl.prompt();
      return;
    }
    if (frame?.type === 'pong') { lastPong = Date.now(); return; }
    if (!ready) return socket.destroy();
    if (frame?.type === 'presence') {
      if (!Number.isInteger(frame.count) || frame.count < 1 || frame.count > 256) return socket.destroy();
      line(dim(frame.count === 1 ? '  Sherigingiz kutilmoqda...' : `  Ulangan terminallar: ${frame.count}`));
      if (frame.count === 1) { for (const peer of seen.values()) peer.online = false; }
      send(socket, packet('hello', ''));
      return;
    }
    if (frame?.type !== 'message') return socket.destroy();
    let message;
    try { message = decrypt(keys, frame); }
    catch { line(dim('  Yaroqsiz shifrlangan xabar rad etildi.')); return; }
    if (!message || typeof message.name !== 'string' || [...message.name].length > 24
      || typeof message.session !== 'string' || !/^[a-f0-9-]{36}$/.test(message.session)
      || !Number.isSafeInteger(message.seq) || message.seq < 1
      || !['hello', 'chat', 'bye'].includes(message.kind)
      || typeof message.text !== 'string' || [...message.text].length > 2000) return;
    if (message.session === session) return;
    const old = seen.get(message.session);
    if (old && old.seq >= message.seq) return;
    if (seen.size >= 512 && !old) seen.delete(seen.keys().next().value);
    const peerName = safeText(message.name);
    seen.set(message.session, { name: peerName, seq: message.seq, online: message.kind !== 'bye' });
    if (message.kind === 'chat') line(green(`${peerName}> ${safeText(message.text)}`));
    else if (message.kind === 'bye') line(dim(`  ${peerName} chiqdi.`));
    else if (!old || !old.online) line(dim(`  ${peerName} suhbatga kirdi.`));
  });
}

process.on('SIGTERM', () => stop('Aloqa yopildi.'));
process.on('SIGINT', () => stop('\nAloqa yopildi.'));
main().catch((error) => stop(safeText(error.message), true));
