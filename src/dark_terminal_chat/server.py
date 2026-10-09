"""Two-person WebSocket relay. Only room/session metadata lives in memory."""

import argparse
import asyncio
import logging
import os
import re
import secrets
import signal
import time
import sqlite3
from pathlib import Path
from dataclasses import dataclass, field
from http import HTTPStatus
from urllib.parse import urlsplit

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from . import __version__
from .protocol import MAX_FRAME, SESSION, dumps, new_code, parse, valid_envelope

RECONNECT_SECONDS = 60


@dataclass
class Participant:
    socket: object
    token: str = field(default_factory=new_code)
    expiry: object = None


@dataclass
class Room:
    participants: dict = field(default_factory=dict)


class Relay:
    def __init__(self, max_clients=128, reconnect_seconds=RECONNECT_SECONDS, database_path=None):
        self.rooms = {}
        self.clients = set()
        self.max_clients = max_clients
        self.reconnect_seconds = reconnect_seconds
        self.tasks = set()
        from .storage import Mailbox
        self.mailbox = Mailbox(database_path) if database_path is not None else None

    @staticmethod
    def http_request(connection, request):
        path = urlsplit(request.path).path.rstrip("/") or "/"
        if path == "/health":
            response = connection.respond(HTTPStatus.OK, '{"status":"ok"}\n')
            response.headers["Content-Type"] = "application/json"
            response.headers["Cache-Control"] = "no-store"
            return response
        if path not in {"/", "/ws"}:
            return connection.respond(HTTPStatus.NOT_FOUND, "Not found\n")
        if request.headers.get("Upgrade", "").lower() != "websocket":
            return connection.respond(HTTPStatus.OK, "Chat server is running.\n")
        return None

    async def send(self, ws, frame):
        try:
            await asyncio.wait_for(ws.send(dumps(frame)), timeout=3)
            return True
        except (ConnectionClosed, TimeoutError, asyncio.TimeoutError):
            await ws.close(code=1013, reason="Connection lost")
            return False

    async def reject(self, ws, code, message):
        await self.send(ws, {"type": "error", "code": code, "message": message})
        await ws.close(code=1013 if code == "busy" else 1008, reason=message)

    async def presence(self, room_id):
        room = self.rooms.get(room_id)
        if room:
            online = {sid: peer.socket for sid, peer in room.participants.items() if peer.socket is not None}
            frame = {"type": "presence", "sessions": list(online), "reserved": list(room.participants)}
            await asyncio.gather(*(self.send(ws, frame) for ws in online.values()))

    def expire(self, room_id, session, participant):
        room = self.rooms.get(room_id)
        if not room or room.participants.get(session) is not participant or participant.socket is not None:
            return
        room.participants.pop(session)
        if not room.participants:
            self.rooms.pop(room_id)
        else:
            task = asyncio.create_task(self.presence(room_id))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    def close(self):
        for room in self.rooms.values():
            for peer in room.participants.values():
                if peer.expiry:
                    peer.expiry.cancel()
        for task in self.tasks:
            task.cancel()
        self.rooms.clear()
        if self.mailbox:
            self.mailbox.close()
            self.mailbox = None

    async def handler(self, ws):
        if len(self.clients) >= self.max_clients:
            await self.reject(ws, "busy", "Server is busy. Try again later.")
            return
        self.clients.add(ws)
        room_id = session = participant = None
        left = False
        try:
            frame = parse(await asyncio.wait_for(ws.recv(), timeout=10))
            if frame.get("type") == "account" and frame.get("version") == 1:
                from .account_server import account_session
                await account_session(self, ws)
                return
            room_id, session, action = frame.get("room"), frame.get("session"), frame.get("type")
            if (not isinstance(action, str) or action not in {"create", "join", "resume"}
                    or not isinstance(room_id, str) or not re.fullmatch(r"[a-f0-9]{64}", room_id)
                    or not isinstance(session, str) or not SESSION.fullmatch(session)):
                raise ValueError("Invalid handshake")
            room = self.rooms.get(room_id)
            if action == "create":
                if room:
                    await self.reject(ws, "exists", "Create a new chat with a new code.")
                    return
                if len(self.rooms) >= self.max_clients:
                    await self.reject(ws, "busy", "Server is busy. Try again later.")
                    return
                room = Room()
                self.rooms[room_id] = room
            elif not room:
                await self.reject(ws, "invalid_room", "Invalid or expired code.")
                return

            if action == "resume":
                participant = room.participants.get(session)
                token = frame.get("token")
                if (not participant or not isinstance(token, str)
                        or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token)
                        or not secrets.compare_digest(token, participant.token)):
                    participant = None
                    await self.reject(ws, "invalid_resume", "Session expired. Create a new chat.")
                    return
                old_socket = participant.socket
                participant.socket = ws
                if participant.expiry:
                    participant.expiry.cancel()
                    participant.expiry = None
                if old_socket is not None and old_socket is not ws:
                    await old_socket.close(code=4001, reason="Connection replaced")
            else:
                if len(room.participants) >= 2:
                    await self.reject(ws, "full", "This chat already has two participants.")
                    return
                if session in room.participants:
                    await self.reject(ws, "invalid_resume", "Session already exists.")
                    return
                participant = Participant(ws)
                room.participants[session] = participant

            await self.send(ws, {"type": "ready", "version": 2, "token": participant.token})
            await self.presence(room_id)
            window, budget = time.monotonic(), 40
            async for raw in ws:
                if participant.socket is not ws:
                    break
                if time.monotonic() - window >= 10:
                    window, budget = time.monotonic(), 40
                budget -= 1
                if budget < 0:
                    left = True
                    await ws.close(code=1008, reason="Rate limit exceeded")
                    break
                frame = parse(raw)
                if frame.get("type") == "leave":
                    left = True
                    await ws.close(code=1000, reason="Left chat")
                    break
                message_id, receipt = frame.get("id"), frame.get("receipt", False)
                if (not valid_envelope(frame) or type(message_id) is not int
                        or not 1 <= message_id <= 2**53 - 1 or type(receipt) is not bool):
                    left = True
                    raise ValueError("Invalid message")
                room = self.rooms.get(room_id)
                target = next((peer.socket for sid, peer in room.participants.items()
                               if sid != session and peer.socket is not None), None) if room else None
                outgoing = {key: frame[key] for key in ("type", "iv", "body", "tag", "id")}
                outgoing["sender"] = session
                delivered = target is not None and await self.send(target, outgoing)
                if receipt and not delivered:
                    await self.send(ws, {"type": "not_delivered", "id": message_id})
        except ConnectionClosed as error:
            if error.rcvd and error.rcvd.code == 1008:
                left = True
        except (ValueError, TimeoutError, asyncio.TimeoutError, UnicodeError):
            left = True
            await ws.close(code=1008, reason="Invalid protocol")
        except sqlite3.Error:
            await self.reject(ws, "storage", "Storage unavailable. Try again later.")
        finally:
            self.clients.discard(ws)
            room = self.rooms.get(room_id) if isinstance(room_id, str) else None
            if room and participant and participant.socket is ws:
                participant.socket = None
                if left:
                    room.participants.pop(session, None)
                    if not room.participants:
                        self.rooms.pop(room_id, None)
                else:
                    participant.expiry = asyncio.get_running_loop().call_later(
                        self.reconnect_seconds, self.expire, room_id, session, participant)
                await self.presence(room_id)

    def serve(self, host, port, **kwargs):
        return serve(self.handler, host, port, process_request=self.http_request,
                     origins=[None], compression=None, max_size=MAX_FRAME,
                     max_queue=16, write_limit=32768, ping_interval=5,
                     ping_timeout=5, close_timeout=3, server_header=None, **kwargs)


async def run(args):
    relay = Relay(args.max_clients, database_path=args.database)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    try:
        async with relay.serve(args.host, args.port) as server:
            address = server.sockets[0].getsockname()
            print(f"Listening on {address[0]}:{address[1]}", flush=True)
            await stop.wait()
    finally:
        relay.close()


def main():
    parser = argparse.ArgumentParser(description="Encrypted chat and mailbox server.")
    parser.add_argument("--host", default=os.environ.get("IP", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=os.environ.get("PORT", "8080"))
    parser.add_argument("--max-clients", type=int, default=128)
    parser.add_argument("--database", default=os.environ.get("DARK_CHAT_DATABASE", str(Path.home() / ".dark-chat-server" / "mailbox.db")))
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not 1 <= args.max_clients <= 1024:
        parser.error("Port must be 1–65535; max-clients must be 1–1024.")
    logging.basicConfig(level=logging.WARNING)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    except OSError as error:
        parser.exit(1, f"Server error: {error.strerror or type(error).__name__}\n")


if __name__ == "__main__":
    main()
