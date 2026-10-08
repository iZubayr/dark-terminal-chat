import net from 'node:net';
import tls from 'node:tls';
import { validEnvelope } from './crypto.mjs';
import { readFrames, send } from './wire.mjs';

export function createRelay(tlsOptions) {
  const rooms = new Map();
  const sockets = new Set();
  function accept(socket) {
    sockets.add(socket);
    socket.setNoDelay(true);
    socket.setKeepAlive(true, 30000);
    let room;
    let budget = 40;
    const replenish = setInterval(() => { budget = 40; }, 10000);
    const joinTimeout = setTimeout(() => socket.destroy(), 10000);
    function presence() {
      const members = rooms.get(room);
      if (!members) return;
      for (const peer of members) send(peer, { type: 'presence', count: members.size });
    }
    socket.on('error', () => {});
    socket.on('close', () => {
      clearInterval(replenish);
      clearTimeout(joinTimeout);
      sockets.delete(socket);
      const members = rooms.get(room);
      if (!members) return;
      members.delete(socket);
      if (!members.size) rooms.delete(room);
      else presence();
    });
    readFrames(socket, (frame) => {
      if (--budget < 0) return socket.destroy();
      if (!room) {
        if (frame?.type !== 'join' || typeof frame.room !== 'string' || !/^[a-f0-9]{64}$/.test(frame.room)) {
          return socket.destroy();
        }
        room = frame.room;
        clearTimeout(joinTimeout);
        if (!rooms.has(room)) rooms.set(room, new Set());
        rooms.get(room).add(socket);
        send(socket, { type: 'ready', count: rooms.get(room).size });
        presence();
        return;
      }
      if (frame?.type === 'ping') return send(socket, { type: 'pong' });
      if (!validEnvelope(frame)) return socket.destroy();
      for (const peer of rooms.get(room) ?? []) {
        if (peer !== socket) send(peer, { type: 'message', iv: frame.iv, body: frame.body, tag: frame.tag });
      }
    });
  }
  const server = tlsOptions ? tls.createServer(tlsOptions, accept) : net.createServer(accept);
  server.maxConnections = 256;
  server.on('tlsClientError', () => {});
  return { server, rooms, sockets, destroy() { for (const socket of sockets) socket.destroy(); } };
}
