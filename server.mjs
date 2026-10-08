import { readFileSync } from 'node:fs';
import { createRelay } from './lib/relay.mjs';
import { options, portNumber } from './lib/options.mjs';

try {
  const args = options({
    host: { type: 'string', default: '127.0.0.1' },
    port: { type: 'string', default: '4040' },
    cert: { type: 'string' }, key: { type: 'string' }, help: { type: 'boolean' },
  });
  if (args.help) {
    console.log('node server.mjs [--host 0.0.0.0] [--port 4040] [--cert cert.pem --key key.pem]');
  } else {
    if (Boolean(args.cert) !== Boolean(args.key)) throw new Error('--cert va --key birga berilishi kerak.');
    const relay = createRelay(args.cert ? { cert: readFileSync(args.cert), key: readFileSync(args.key), minVersion: 'TLSv1.2' } : undefined);
    relay.server.on('error', (error) => {
      console.error(`Server xatosi: ${error.code ?? error.message}`);
      process.exitCode = 1;
    });
    relay.server.listen(portNumber(args.port), args.host, () => {
      console.log(`DARK TERMINAL / relay online\nManzil: ${args.host}:${args.port}${args.cert ? ' (TLS)' : ''}\nXabar matni va kirish kodi serverga kelmaydi.\nTo‘xtatish: Ctrl+C`);
    });
    const stop = () => { relay.destroy(); relay.server.close(); };
    process.on('SIGINT', stop);
    process.on('SIGTERM', stop);
  }
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
}
