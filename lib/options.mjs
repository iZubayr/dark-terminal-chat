import { parseArgs } from 'node:util';

export function options(definitions) {
  return parseArgs({ options: definitions, strict: true }).values;
}

export function portNumber(value) {
  if (!/^\d+$/.test(value) || Number(value) < 1 || Number(value) > 65535) {
    throw new Error('Port 1–65535 orasida bo‘lishi kerak.');
  }
  return Number(value);
}
