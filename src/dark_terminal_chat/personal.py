"""Persistent terminal conversations with encrypted outbox and local history."""

import asyncio
import contextlib
import ssl
import sys
import threading
import time

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from .identity import RETENTION, USER_ID, auth_data, fingerprint, open_message, seal_message, verify_message
from .protocol import MAX_FRAME, MAX_TEXT, dumps, parse, safe_text
from .terminal_ui import chat_prompt, clear_terminal

# Up to 15 mailbox requests per second, below the relay's 240/10s limit.
SYNC_BATCH = 5


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
        self.retry_after = {}

    def contacts(self):
        contacts = self.store.contacts()
        for number, (uid, contact) in enumerate(contacts.items(), 1):
            label = contact['alias']
            self.terminal.say(f"{number}. {uid}" if label == uid else f"{number}. {label}: {uid}")
        if not contacts:
            self.terminal.say("No contacts. Share your ID, then use /chat PEER_ID name.")
        else:
            self.terminal.say("Open with /chat number or /chat name.")
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
            elif value.isdecimal() and len(value) < 10 and 1 <= int(value) <= len(contacts):
                uid = list(contacts)[int(value) - 1]
            elif USER_ID.fullmatch(value):
                if value == self.identity.id:
                    raise MailError("Enter your peer's ID, not your own.")
                if self.connection is None:
                    raise MailError("Connect before adding a new contact.")
                try:
                    reply = await self.connection.request({"type": "lookup", "id": value}, "public")
                except (ConnectionClosed, OSError, TimeoutError, InvalidHandshake) as error:
                    raise MailError("Connection interrupted. Try /chat again after reconnecting.") from error
                public = reply.get("public")
                if public is None:
                    raise MailError("Peer not found. They must register first.")
                if fingerprint(public) != value:
                    raise MailError("Peer key does not match the shared ID. Connection refused.")
                uid = self.store.add_contact(public, value)
            else:
                raise MailError("Use /chat to list contacts, or /chat PEER_ID name to add one.")
        if alias is not None:
            self.store.rename_contact(uid, alias)
        self.peer = uid
        label = self.store.contacts()[uid]['alias']
        self.terminal.say(f"Chat: {uid}" if label == uid else f"Chat: {label} ({uid})")
        self.history()

    def history(self):
        if not self.peer or self.peer not in self.store.contacts():
            self.terminal.say("Use /chat followed by a peer ID.")
            return
        for item in self.store.messages(self.peer, limit=50):
            suffix = " [unconfirmed: expired]" if item.get("expired") else " [pending]" if item["pending"] else ""
            self.terminal.say(f"{item['name']}> {item['text']}{suffix}")

    async def synchronize(self):
        self.retry_after = {uid: until for uid, until in self.retry_after.items() if until > time.monotonic()}
        blocked = set(self.retry_after)
        for _ in range(SYNC_BATCH):
            pending = self.store.messages(pending=True, limit=1, exclude_peers=blocked)
            if not pending:
                break
            item = pending[0]
            if item['envelope']['created'] < int(time.time()) - RETENTION:
                self.store.expire(item['id'])
                self.warnings.discard(item['id'])
                self.terminal.say("An unconfirmed message expired. Text kept in /history; send it again if needed.")
                continue
            try:
                result = await self.connection.request({"type": "send", "message": item["envelope"]}, "stored")
                if result.get("id") != item["id"]:
                    raise MailError("Invalid storage receipt.")
                self.store.sent(item["id"])
                self.warnings.discard(item["id"])
            except MailError as error:
                blocked.add(item['peer'])
                self.retry_after[item['peer']] = time.monotonic() + 5
                if item["id"] not in self.warnings:
                    self.terminal.say(str(error))
                    self.warnings.add(item["id"])
                # Keep each conversation in order; a full mailbox must not
                # starve other contacts or prevent incoming mail.
        for _ in range(SYNC_BATCH):
            reply = await self.connection.request({"type": "fetch"}, "mail")
            frame = reply.get("message")
            if frame is None:
                break
            # Only discard unreadable content after authenticating the envelope
            # and recipient. Never acknowledge a substituted or foreign message.
            verify_message(frame)
            if frame['to'] != self.identity.id:
                raise MailError("Message is addressed to another identity.")
            try:
                content = open_message(self.identity, frame)
            except ValueError:
                self.terminal.say(f"Unreadable message from {frame['from']} skipped.")
                is_new = False
            else:
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
        offline_notice = False
        while not self.finished.is_set():
            try:
                async with AccountConnection(url, self.identity, ca) as connection:
                    self.connection = connection
                    self.terminal.say("Connected." if not was_online else "Reconnected.")
                    was_online = True
                    offline_notice = False
                    while not self.finished.is_set():
                        await self.synchronize()
                        with contextlib.suppress(asyncio.TimeoutError):
                            await asyncio.wait_for(self.finished.wait(), 1)
            except ssl.SSLCertVerificationError:
                raise
            except (ConnectionClosed, OSError, TimeoutError, InvalidHandshake):
                if self.finished.is_set():
                    return
                if not offline_notice:
                    self.terminal.say("Offline. Saved outgoing messages will retry after reconnecting.")
                    offline_notice = True
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
        elif command == "/chat":
            self.contacts()
        elif command == "/help":
            self.terminal.say("/chat ID [name]  /chat name  /chat number  /contacts  /id  /history  /backup  /clean  /exit")
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
            session = chat_prompt(personal=True, contacts=self.store.contacts)

            async def read():
                contact = self.store.contacts().get(self.peer)
                label = contact['alias'] if contact else ''
                if label == self.peer:
                    label = label[:12]
                target = f" [{label}]" if label else ''
                return await session.prompt_async(f"{self.identity.name}{target}> ", default=self.draft)
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
