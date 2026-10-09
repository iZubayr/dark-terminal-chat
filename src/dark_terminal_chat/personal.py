"""Persistent terminal conversations with encrypted outbox and local history."""

import asyncio
import contextlib
import ssl
import sys
import threading

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from .identity import USER_ID, auth_data, fingerprint, open_message, seal_message
from .protocol import MAX_FRAME, MAX_TEXT, dumps, parse, safe_text
from .terminal_ui import chat_prompt, clear_terminal


class MailError(ValueError):
    pass


class AccountConnection:
    def __init__(self, url, identity, ca=None):
        self.url, self.identity, self.ca = url, identity, ca
        self.lock = asyncio.Lock()
        self.ws = None

    async def __aenter__(self):
        kwargs = {"ssl": ssl.create_default_context(cafile=self.ca)} if self.ca else {}
        self.ws = await connect(self.url, compression=None, max_size=MAX_FRAME, max_queue=16,
                                open_timeout=10, ping_interval=10, ping_timeout=10,
                                close_timeout=1, proxy=None, **kwargs)
        try:
            await self.ws.send(dumps({"type": "account", "version": 1}))
            challenge = parse(await asyncio.wait_for(self.ws.recv(), 10))
            if challenge.get("type") != "challenge":
                raise MailError("This server does not support accounts. Update the server first.")
            public = self.identity.public
            await self.ws.send(dumps({"type": "auth", "public": public, "register": True,
                                      "signature": self.identity.sign(auth_data(challenge.get("nonce"), public, True))}))
            ready = parse(await asyncio.wait_for(self.ws.recv(), 10))
            if ready.get("type") != "account_ready" or ready.get("id") != self.identity.id or ready.get("version") != 1:
                raise MailError("Unable to authenticate this identity.")
            return self
        except BaseException:
            await self.ws.close()
            raise

    async def __aexit__(self, *_):
        await self.ws.close()

    async def request(self, frame, expected):
        async with self.lock:
            try:
                await self.ws.send(dumps(frame))
                response = parse(await asyncio.wait_for(self.ws.recv(), 10))
            except BaseException:
                # A timed-out response must never be read as the next command's reply.
                await self.ws.close()
                raise
            if response.get("type") == "error":
                raise MailError(safe_text(str(response.get("message", "Request refused."))))
            if response.get("type") != expected:
                raise MailError("Invalid server response.")
            return response


class PersonalChat:
    def __init__(self, terminal, store, peer=""):
        self.terminal, self.store, self.identity = terminal, store, store.identity
        self.peer = peer
        self.connection = None
        self.finished = asyncio.Event()
        self.draft = ""
        self.warnings = set()
        self.last_inbox = None

    def contacts(self):
        contacts = self.store.contacts()
        for uid, contact in contacts.items():
            self.terminal.say(f"{contact['alias']}: {uid}")
        if not contacts:
            self.terminal.say("No contacts. Use /chat followed by a peer ID.")
        return contacts

    async def select(self, value):
        alias = None
        if len(value) > 65 and USER_ID.fullmatch(value[:64]) and value[64] == " ":
            value, alias = value[:64], value[65:].strip()
        contacts = self.store.contacts()
        if value in contacts:
            uid = value
        else:
            matches = [uid for uid, contact in contacts.items() if contact["alias"] == value]
            if matches:
                uid = matches[0]
            elif USER_ID.fullmatch(value):
                if value == self.identity.id:
                    raise MailError("Enter your peer's ID, not your own.")
                if self.connection is None:
                    raise MailError("Connect before adding a new contact.")
                reply = await self.connection.request({"type": "lookup", "id": value}, "public")
                public = reply.get("public")
                if public is None:
                    raise MailError("Peer not found. They must register first.")
                if fingerprint(public) != value:
                    raise MailError("Peer key does not match the shared ID. Connection refused.")
                uid = self.store.add_contact(public, value)
            else:
                raise MailError("Enter a saved contact name or a full 64-character peer ID.")
        if alias is not None:
            self.store.rename_contact(uid, alias)
        self.peer = uid
        self.terminal.say(f"Chat: {uid}")
        self.history()

    def history(self):
        if not self.peer or self.peer not in self.store.contacts():
            self.terminal.say("Use /chat followed by a peer ID.")
            return
        for item in self.store.messages(self.peer, limit=50):
            suffix = " [pending]" if item["pending"] else ""
            self.terminal.say(f"{item['name']}> {item['text']}{suffix}")

    async def synchronize(self):
        for item in self.store.messages(pending=True, limit=20):
            try:
                result = await self.connection.request({"type": "send", "message": item["envelope"]}, "stored")
                if result.get("id") != item["id"]:
                    raise MailError("Invalid storage receipt.")
                self.store.sent(item["id"])
                self.warnings.discard(item["id"])
            except MailError as error:
                if item["id"] not in self.warnings:
                    self.terminal.say(str(error))
                    self.warnings.add(item["id"])
                # One full recipient mailbox must not block incoming mail.
        for _ in range(20):
            reply = await self.connection.request({"type": "fetch"}, "mail")
            frame = reply.get("message")
            if frame is None:
                break
            content = open_message(self.identity, frame)
            is_new = self.store.save(frame, content, "in")
            # Unknown senders are identified by their full fingerprint, never a claimed name.
            self.store.add_contact(frame["public"], frame["from"])
            result = await self.connection.request({"type": "ack", "id": frame["id"]}, "acknowledged")
            if result.get("id") != frame["id"]:
                raise MailError("Invalid delivery receipt.")
            if is_new:
                if frame["from"] == self.peer:
                    self.terminal.say(f"{content['name']}> {content['text']}")
                else:
                    self.terminal.say(f"New message from {frame['from']}. Use /chat {frame['from']}")

    async def network(self, url, ca):
        was_online = False
        while not self.finished.is_set():
            try:
                async with AccountConnection(url, self.identity, ca) as connection:
                    self.connection = connection
                    self.terminal.say("Connected." if not was_online else "Reconnected.")
                    was_online = True
                    if self.peer:
                        try:
                            await self.select(self.peer)
                        except MailError as error:
                            self.terminal.say(str(error))
                            self.peer = ""
                    while not self.finished.is_set():
                        await self.synchronize()
                        with contextlib.suppress(asyncio.TimeoutError):
                            await asyncio.wait_for(self.finished.wait(), 1)
            except ssl.SSLCertVerificationError:
                raise
            except (ConnectionClosed, OSError, TimeoutError, InvalidHandshake):
                if self.finished.is_set():
                    return
                self.terminal.say("Offline. Saved outgoing messages will retry after reconnecting.")
            finally:
                self.connection = None
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.finished.wait(), 3)

    async def handle(self, text):
        command = text.strip()
        if not command:
            return
        if command in {"/clean", "clean"}:
            self.terminal.clean_requested = True
            self.finished.set()
        elif command in {"/exit", "/quit"}:
            self.finished.set()
        elif command == "/clear":
            clear_terminal()
        elif command == "/id":
            self.terminal.say(f"ID: {self.identity.id}")
        elif command == "/contacts":
            self.contacts()
        elif command == "/history":
            self.history()
        elif command == "/backup":
            self.terminal.say(f"Encrypted identity backup: {self.store.backup()}")
        elif command.startswith("/chat "):
            await self.select(command[6:].strip())
        elif command in {"/chat", "/help"}:
            self.terminal.say("/chat ID [name]  /contacts  /id  /history  /backup  /clean  /exit")
        elif command.startswith("/"):
            self.terminal.say("Unknown command. Use /help.")
        elif len(text) > MAX_TEXT:
            self.draft = text
            self.terminal.say("Message limit: 2000 characters.")
        else:
            contact = self.store.contacts().get(self.peer)
            if not contact:
                self.draft = text
                raise MailError("Choose a contact with /chat ID first.")
            frame = seal_message(self.identity, contact["public"], text)
            self.store.save(frame, {"name": self.identity.name, "text": text}, "out", pending=True)
            self.draft = ""
            self.terminal.say("Queued.")

    async def send_input(self):
        stopped = threading.Event()
        if self.terminal.interactive:
            session = chat_prompt(personal=True)

            async def read():
                return await session.prompt_async(f"{self.identity.name}> ", default=self.draft)
        else:
            queue, loop = asyncio.Queue(), asyncio.get_running_loop()

            def feed():
                for raw in sys.stdin:
                    if stopped.is_set():
                        return
                    with contextlib.suppress(RuntimeError):
                        loop.call_soon_threadsafe(queue.put_nowait, raw.rstrip("\r\n"))
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(queue.put_nowait, None)

            threading.Thread(target=feed, daemon=True).start()

            async def read():
                value = await queue.get()
                if value is None:
                    raise EOFError
                return value
        try:
            while not self.finished.is_set():
                try:
                    await self.handle(safe_text(await read()))
                except (EOFError, KeyboardInterrupt):
                    return
                except (MailError, ValueError) as error:
                    self.terminal.say(str(error))
        finally:
            stopped.set()
            self.finished.set()

    async def run(self, url, ca=None):
        self.terminal.say(f"ID: {self.identity.id}")
        self.contacts()
        self.terminal.say("Use /chat ID to open a conversation. Type / for commands.")
        network = asyncio.create_task(self.network(url, ca))
        reader = asyncio.create_task(self.send_input())
        try:
            done, _ = await asyncio.wait({network, reader}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            self.finished.set()
            network.cancel()
            reader.cancel()
            await asyncio.gather(network, reader, return_exceptions=True)
