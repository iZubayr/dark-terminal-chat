"""Authenticated mailbox commands on the existing WebSocket endpoint."""

import asyncio
import sqlite3
import time

from .identity import MESSAGE_ID, USER_ID, auth_data, fingerprint, verify_signature
from .protocol import new_code, parse


async def account_session(relay, ws):
    mailbox = relay.mailbox
    if mailbox is None:
        await relay.reject(ws, "unavailable", "Accounts are not enabled on this server.")
        return
    nonce = new_code()
    await relay.send(ws, {"type": "challenge", "nonce": nonce})
    response = parse(await asyncio.wait_for(ws.recv(), 10))
    public, register = response.get("public"), response.get("register")
    if response.get("type") != "auth":
        raise ValueError("Invalid authentication.")
    uid = fingerprint(public)
    verify_signature(public, auth_data(nonce, public, register), response.get("signature"))
    if register:
        mailbox.register(public)
    elif not mailbox.lookup(uid):
        await relay.reject(ws, "unknown", "Identity is not registered on this server.")
        return
    await relay.send(ws, {"type": "account_ready", "id": uid, "version": 1})
    window, count = time.monotonic(), 0
    async for raw in ws:
        if time.monotonic() - window >= 10:
            window, count = time.monotonic(), 0
        count += 1
        if count > 240:
            await ws.close(code=1008, reason="Rate limit exceeded")
            return
        frame = parse(raw)
        action = frame.get("type")
        try:
            if action == "lookup":
                peer = frame.get("id")
                if not isinstance(peer, str) or not USER_ID.fullmatch(peer):
                    raise ValueError("Enter a full 64-character peer ID.")
                result = {"type": "public", "public": mailbox.lookup(peer)}
            elif action == "send":
                mailbox.send(uid, frame.get("message"))
                result = {"type": "stored", "id": frame["message"]["id"]}
            elif action == "fetch":
                result = {"type": "mail", "message": mailbox.fetch(uid)}
            elif action == "ack":
                mid = frame.get("id")
                if not isinstance(mid, str) or not MESSAGE_ID.fullmatch(mid):
                    raise ValueError("Invalid message ID.")
                mailbox.acknowledge(uid, mid)
                result = {"type": "acknowledged", "id": mid}
            elif action == "leave":
                await ws.close(code=1000, reason="Logged out")
                return
            else:
                raise ValueError("Unknown mailbox command.")
        except ValueError as error:
            result = {"type": "error", "message": str(error)}
        except sqlite3.Error:
            result = {"type": "error", "message": "Storage unavailable. Message kept on your device."}
        if not await relay.send(ws, result):
            return
