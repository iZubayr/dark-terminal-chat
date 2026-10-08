import { createHash, hkdfSync, randomBytes, createCipheriv, createDecipheriv } from 'node:crypto';

export function newCode() {
  return randomBytes(32).toString('base64url');
}

export function roomKeys(code) {
  if (!/^[A-Za-z0-9_-]{43}$/.test(code) || Buffer.from(code, 'base64url').toString('base64url') !== code) {
    throw new Error('Kirish kodi noto‘g‘ri. Yaratuvchi bergan 43 belgili kodni kiriting.');
  }
  const secret = Buffer.from(code, 'base64url');
  const room = createHash('sha256').update('dark-terminal-room-v1:').update(secret).digest('hex');
  const key = Buffer.from(hkdfSync('sha256', secret, 'dark-terminal-v1', 'message-encryption', 32));
  return { room, key };
}

export function encrypt(keys, message) {
  const iv = randomBytes(12);
  const cipher = createCipheriv('aes-256-gcm', keys.key, iv);
  cipher.setAAD(Buffer.from(keys.room));
  const body = Buffer.concat([cipher.update(JSON.stringify(message), 'utf8'), cipher.final()]);
  return { type: 'message', iv: iv.toString('base64url'), body: body.toString('base64url'), tag: cipher.getAuthTag().toString('base64url') };
}

export function validEnvelope(value) {
  return value?.type === 'message'
    && typeof value.iv === 'string' && /^[A-Za-z0-9_-]{16}$/.test(value.iv)
    && typeof value.tag === 'string' && /^[A-Za-z0-9_-]{22}$/.test(value.tag)
    && typeof value.body === 'string' && /^[A-Za-z0-9_-]{1,16000}$/.test(value.body);
}

export function decrypt(keys, envelope) {
  if (!validEnvelope(envelope)) throw new Error('Noto‘g‘ri xabar.');
  const decipher = createDecipheriv('aes-256-gcm', keys.key, Buffer.from(envelope.iv, 'base64url'));
  decipher.setAAD(Buffer.from(keys.room));
  decipher.setAuthTag(Buffer.from(envelope.tag, 'base64url'));
  return JSON.parse(Buffer.concat([decipher.update(Buffer.from(envelope.body, 'base64url')), decipher.final()]).toString('utf8'));
}

// Remote text must never inject ANSI/OSC commands into the receiving terminal.
export function safeText(value) {
  return String(value).replace(/[\p{Cc}\p{Cf}\p{Zl}\p{Zp}]/gu, '');
}
