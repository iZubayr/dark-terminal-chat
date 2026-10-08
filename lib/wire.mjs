export const MAX_FRAME = 20000;

export function send(socket, value) {
  if (socket.destroyed || !socket.writable) return false;
  if (socket.writableLength > MAX_FRAME * 16) {
    socket.destroy();
    return false;
  }
  socket.write(`${JSON.stringify(value)}\n`);
  return true;
}

export function readFrames(socket, onFrame) {
  let pending = Buffer.alloc(0);
  socket.on('data', (chunk) => {
    pending = Buffer.concat([pending, chunk]);
    let end;
    while ((end = pending.indexOf(10)) !== -1) {
      if (end > MAX_FRAME) return socket.destroy();
      const line = pending.subarray(0, end);
      pending = pending.subarray(end + 1);
      let frame;
      try { frame = JSON.parse(line.toString('utf8')); }
      catch { return socket.destroy(); }
      onFrame(frame);
      if (socket.destroyed) return;
    }
    if (pending.length > MAX_FRAME) socket.destroy();
  });
}
