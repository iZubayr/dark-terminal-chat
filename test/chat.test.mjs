import test from 'node:test';
import assert from 'node:assert/strict';
import net from 'node:net';
import { once } from 'node:events';
import { spawn } from 'node:child_process';
import { newCode, roomKeys, encrypt, decrypt, safeText } from '../lib/crypto.mjs';
import { createRelay } from '../lib/relay.mjs';
import { readFrames, send, MAX_FRAME } from '../lib/wire.mjs';

async function startRelay(t) {
  const relay = createRelay();
  relay.server.listen(0, '127.0.0.1');
  await once(relay.server, 'listening');
  t.after(async () => {
    relay.destroy();
    await new Promise((resolve) => relay.server.close(resolve));
  });
  return { ...relay, port: relay.server.address().port };
}

async function peer(t, port, keys) {
  const socket = net.createConnection({ host: '127.0.0.1', port });
  socket.on('error', () => {});
  const frames = [];
  const waiting = [];
  readFrames(socket, (frame) => {
    const index = waiting.findIndex((item) => item.type === frame.type);
    if (index < 0) frames.push(frame);
    else waiting.splice(index, 1)[0].resolve(frame);
  });
  const next = (type) => {
    const index = frames.findIndex((frame) => frame.type === type);
    if (index >= 0) return Promise.resolve(frames.splice(index, 1)[0]);
    return new Promise((resolve, reject) => {
      const item = { type, resolve: (frame) => { clearTimeout(timer); resolve(frame); } };
      const timer = setTimeout(() => {
        waiting.splice(waiting.indexOf(item), 1);
        reject(new Error(`No ${type} frame`));
      }, 3000);
      waiting.push(item);
    });
  };
  t.after(() => socket.destroy());
  await once(socket, 'connect');
  send(socket, { type: 'join', room: keys.room });
  await next('ready');
  return { socket, next, frames };
}

test('Unicode encryption, tampering, wrong keys and terminal escapes', () => {
  const keys = roomKeys(newCode());
  const message = { name: 'elliot', text: 'Salom! أَهْلًا 👋' };
  const envelope = encrypt(keys, message);
  assert.deepEqual(decrypt(keys, envelope), message);
  assert.equal(JSON.stringify(envelope).includes('Salom'), false);
  assert.notEqual(encrypt(keys, message).iv, envelope.iv);
  assert.throws(() => decrypt(roomKeys(newCode()), envelope));
  const tampered = { ...envelope, tag: (envelope.tag[0] === 'A' ? 'B' : 'A') + envelope.tag.slice(1) };
  assert.throws(() => decrypt(keys, tampered));
  assert.throws(() => roomKeys('1234'));
  assert.equal(safeText('abc\x1b]52;c;SECRET\x07\n\u202Etext'), 'abc]52;c;SECRETtext');
});

test('relay delivers both directions, isolates rooms, and forgets empty rooms', async (t) => {
  const relay = await startRelay(t);
  const keys = roomKeys(newCode());
  const a = await peer(t, relay.port, keys);
  const b = await peer(t, relay.port, keys);
  const outsider = await peer(t, relay.port, roomKeys(newCode()));
  const outbound = encrypt(keys, { name: 'elliot', text: 'Salom, Whiterose! 👋' });
  send(a.socket, outbound);
  assert.deepEqual(await b.next('message'), outbound);
  send(b.socket, encrypt(keys, { name: 'whiterose', text: 'Salom, Elliot! أَهْلًا' }));
  assert.equal(decrypt(keys, await a.next('message')).text, 'Salom, Elliot! أَهْلًا');
  send(outsider.socket, { type: 'ping' });
  await outsider.next('pong');
  assert.equal(outsider.frames.some((frame) => frame.type === 'message'), false);
  assert.equal(a.frames.some((frame) => frame.type === 'message'), false);
  a.socket.destroy();
  b.socket.destroy();
  await new Promise((resolve) => setTimeout(resolve, 50));
  assert.equal(relay.rooms.has(keys.room), false);
});

test('stream framing survives split Unicode and coalesced packets; oversized input disconnects', async (t) => {
  const relay = await startRelay(t);
  const keys = roomKeys(newCode());
  const a = await peer(t, relay.port, keys);
  const b = await peer(t, relay.port, keys);
  const message = encrypt(keys, { text: 'عربي 🙂' });
  const frame = Buffer.from(`${JSON.stringify(message)}\n`);
  a.socket.write(frame.subarray(0, 17));
  a.socket.write(Buffer.concat([frame.subarray(17), Buffer.from('{"type":"ping"}\n')]));
  assert.equal(decrypt(keys, await b.next('message')).text, 'عربي 🙂');
  await a.next('pong');
  const closed = once(a.socket, 'close');
  a.socket.write('x'.repeat(MAX_FRAME + 1));
  await closed;
});

test('malformed handshake and JSON do not crash the relay', async (t) => {
  const relay = await startRelay(t);
  for (const payload of ['not-json\n', '{"type":"join","room":"bad"}\n', 'null\n']) {
    const socket = net.createConnection({ host: '127.0.0.1', port: relay.port });
    socket.on('error', () => {});
    await once(socket, 'connect');
    const closed = once(socket, 'close');
    socket.write(payload);
    await closed;
  }
  const valid = await peer(t, relay.port, roomKeys(newCode()));
  send(valid.socket, { type: 'ping' });
  await valid.next('pong');
});

function runClient(t, port, name, code) {
  const child = spawn(process.execPath, ['chat.mjs', '--name', name, '--port', String(port)], {
    cwd: new URL('..', import.meta.url),
    env: { ...process.env, DARK_CHAT_CODE: code, NO_COLOR: '1' },
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  let output = '';
  child.stdout.on('data', (chunk) => { output += chunk.toString(); });
  child.stderr.on('data', (chunk) => { output += chunk.toString(); });
  t.after(() => { if (child.exitCode === null) child.kill(); });
  const until = async (text) => {
    const deadline = Date.now() + 5000;
    while (!output.includes(text)) {
      if (Date.now() > deadline || child.exitCode !== null) throw new Error(`Missing ${text}: ${output}`);
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
  };
  return { child, until, output: () => output };
}

test('two real CLI processes exchange Uzbek/Arabic messages and exit cleanly', async (t) => {
  const relay = await startRelay(t);
  const code = newCode();
  const elliot = runClient(t, relay.port, 'elliot', code);
  const whiterose = runClient(t, relay.port, 'whiterose', code);
  await Promise.all([elliot.until('whiterose suhbatga kirdi'), whiterose.until('elliot suhbatga kirdi')]);
  elliot.child.stdin.write('Salom, Whiterose! أَهْلًا 👋\n');
  await whiterose.until('elliot> Salom, Whiterose! أَهْلًا 👋');
  whiterose.child.stdin.write('Salom, Elliot!\n');
  await elliot.until('whiterose> Salom, Elliot!');
  elliot.child.stdin.write('/who\n');
  await elliot.until('Ishtirokchilar: elliot, whiterose');
  assert.equal(elliot.output().includes(code), false);
  assert.equal(whiterose.output().includes(code), false);
  const exitedA = once(elliot.child, 'exit');
  elliot.child.stdin.write('/exit\n');
  assert.equal((await Promise.race([exitedA, new Promise((_, reject) => {
    const timer = setTimeout(() => reject(new Error(`Elliot did not exit: ${elliot.output()}`)), 3000);
    timer.unref();
  })]))[0], 0);
  await whiterose.until('elliot chiqdi.');
  const exitedB = once(whiterose.child, 'exit');
  whiterose.child.stdin.write('/exit\n');
  assert.equal((await Promise.race([exitedB, new Promise((_, reject) => {
    const timer = setTimeout(() => reject(new Error(`Whiterose did not exit: ${whiterose.output()}`)), 3000);
    timer.unref();
  })]))[0], 0);
});
