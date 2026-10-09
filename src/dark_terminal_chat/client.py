"""Minimal English terminal client with a 60-second reconnect window."""

import argparse
import asyncio
import contextlib
import os
import re
import ssl
import sys
import threading
import time
import uuid
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit, urlunsplit

from cryptography.exceptions import InvalidTag
from prompt_toolkit import PromptSession, prompt
from prompt_toolkit.history import DummyHistory
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from . import __version__
from .protocol import MAX_FRAME, MAX_TEXT, SESSION, decrypt, dumps, encrypt, new_code, parse, room_keys, safe_text, valid_message
from .terminal_ui import chat_prompt, chat_stdout, clear_terminal

RECONNECT_SECONDS = 60
DEFAULT_SERVER = "wss://zubayr.alwaysdata.net/dark-chat/ws"


class SessionError(Exception):
    pass


def server_url(value, allow_insecure=False):
    value = value.strip()
    if any(ord(char) < 33 for char in value):
        raise ValueError("Server address cannot contain spaces or control characters.")
    url = urlsplit(value)
    scheme = {"https": "wss", "http": "ws"}.get(url.scheme, url.scheme)
    if (scheme not in {"ws", "wss"} or not url.hostname
            or url.username is not None or url.password is not None or url.query or url.fragment):
        raise ValueError("Use a server address such as wss://ACCOUNT.alwaysdata.net/dark-chat/ws.")
    if url.port is not None and not 1 <= url.port <= 65535:
        raise ValueError("Invalid server port.")
    if scheme == "ws" and url.hostname not in {"localhost", "127.0.0.1", "::1"} and not allow_insecure:
        raise ValueError("Use wss:// over the internet. For a local network, use --allow-insecure.")
    return urlunsplit((scheme, url.netloc, url.path or "/ws", "", ""))


class Terminal:
    def __init__(self):
        self.interactive = sys.stdin.isatty() and sys.stdout.isatty()
        self.clean_requested = False

    def say(self, text):
        print(safe_text(text), flush=True)

    def ask(self, text, secret=False):
        if not self.interactive:
            raise ValueError("Use an interactive terminal, or provide --name and DARK_CHAT_CODE.")
        return prompt(text, is_password=secret, history=DummyHistory()).strip()


class Chat:
    def __init__(self, terminal, keys, name, reconnect_seconds=RECONNECT_SECONDS):
        self.terminal = terminal
        self.keys = keys
        self.name = name
        self.session = str(uuid.uuid4())
        self.seq = 0
        self.peers = {}
        self.online = {self.session}
        self.peer_ready = False
        self.token = None
        self.ws = None
        self.finished = asyncio.Event()
        self.pending = {}
        self.draft = ""
        self.reconnect_seconds = reconnect_seconds

    def packet(self, kind, text="", **extra):
        self.seq += 1
        envelope = encrypt(self.keys, {"kind": kind, "name": self.name, "text": text,
                                       "session": self.session, "seq": self.seq, **extra})
        envelope["id"] = self.seq
        envelope["receipt"] = kind == "chat"
        return envelope

    def fail_pending(self):
        for future in self.pending.values():
            if not future.done():
                future.set_result(False)

    async def receive(self, ws):
        async for raw in ws:
            frame = parse(raw)
            if frame.get("type") == "presence":
                online, reserved = frame.get("sessions"), frame.get("reserved")
                if (not isinstance(online, list) or not isinstance(reserved, list)
                        or not 1 <= len(reserved) <= 2 or not 1 <= len(online) <= 2
                        or not all(isinstance(sid, str) and SESSION.fullmatch(sid) for sid in reserved + online)
                        or len(set(reserved)) != len(reserved) or len(set(online)) != len(online)
                        or not set(online) <= set(reserved) or self.session not in online):
                    raise SessionError("Invalid server response.")
                new_online = set(online)
                for sid in self.online - new_online:
                    peer = self.peers.get(sid)
                    if peer and peer["online"]:
                        action = "disconnected" if sid in reserved else "left"
                        self.terminal.say(f"{peer['name']} {action}.")
                        peer["online"] = False
                    if sid not in reserved:
                        self.peers.pop(sid, None)
                if len(new_online) == 1:
                    if self.peer_ready:
                        self.fail_pending()
                    self.peer_ready = False
                    if not self.peers:
                        self.terminal.say("Waiting for peer.")
                newcomers = new_online - self.online
                self.online = new_online
                if newcomers:
                    await ws.send(dumps(self.packet("hello")))
                continue
            if frame.get("type") == "not_delivered":
                if type(frame.get("id")) is not int:
                    raise SessionError("Invalid server response.")
                future = self.pending.get(frame.get("id"))
                if future and not future.done():
                    future.set_result(False)
                self.peer_ready = False
                continue
            if frame.get("type") != "message":
                raise SessionError("Invalid server response.")
            try:
                message = decrypt(self.keys, frame)
            except (ValueError, InvalidTag, UnicodeError):
                self.terminal.say("Invalid encrypted message ignored.")
                continue
            if (not valid_message(message) or message["session"] != frame.get("sender")
                    or message["seq"] != frame.get("id")
                    or message["session"] not in self.online or message["session"] == self.session):
                continue
            sid = message["session"]
            old = self.peers.get(sid)
            if old and old["seq"] >= message["seq"]:
                continue
            name = safe_text(message["name"])
            self.peers[sid] = {"name": name, "seq": message["seq"], "online": True}
            self.peer_ready = True
            if message["kind"] == "hello":
                if not old:
                    self.terminal.say(f"{name} joined.")
                elif not old["online"]:
                    self.terminal.say(f"{name} reconnected.")
            elif message["kind"] == "chat":
                self.terminal.say(f"{name}> {safe_text(message['text'])}")
                await ws.send(dumps(self.packet("ack", ack=message["seq"])))
            elif message["kind"] == "ack":
                future = self.pending.get(message["ack"])
                if future and not future.done():
                    future.set_result(True)
        raise ConnectionError("Connection closed.")

    async def send_message(self, text):
        if self.ws is None or not self.peer_ready:
            self.terminal.say("Sending paused. Draft kept.")
            return False
        envelope = self.packet("chat", text)
        message_id = envelope["id"]
        future = asyncio.get_running_loop().create_future()
        self.pending[message_id] = future
        try:
            await self.ws.send(dumps(envelope))
            confirmed = await asyncio.wait_for(future, timeout=8)
        except (ConnectionClosed, OSError, TimeoutError, asyncio.TimeoutError):
            confirmed = False
        finally:
            self.pending.pop(message_id, None)
        if not confirmed:
            self.terminal.say("Delivery not confirmed. Draft kept.")
        return confirmed

    async def send_input(self):
        terminal = self.terminal
        if terminal.interactive:
            session = chat_prompt()

            async def read():
                return await session.prompt_async(f"{self.name}> ", default=self.draft)
        else:
            # A daemon reader allows Windows to exit while stdin is blocked.
            queue = asyncio.Queue()
            loop = asyncio.get_running_loop()
            stopped = threading.Event()

            def feed():
                for raw in sys.stdin:
                    if stopped.is_set():
                        return
                    try:
                        loop.call_soon_threadsafe(queue.put_nowait, raw.rstrip("\r\n"))
                    except RuntimeError:
                        return
                if not stopped.is_set():
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
                    text = safe_text(await read())
                except (EOFError, KeyboardInterrupt):
                    return
                command = text.strip()
                if not command:
                    self.draft = ""
                    continue
                if command in {"/exit", "/quit"}:
                    return
                if command in {"/clean", "clean"}:
                    terminal.clean_requested = True
                    return
                if command == "/help":
                    terminal.say("/who  /clear  /clean  /exit")
                elif command == "/who":
                    names = [peer["name"] for sid, peer in self.peers.items()
                             if sid in self.online and peer["online"]]
                    terminal.say(f"Participants: {', '.join([self.name, *names])}")
                elif command == "/clear":
                    if terminal.interactive:
                        clear_terminal()
                elif command.startswith("/"):
                    terminal.say("Unknown command. Use /help.")
                elif len(text) > MAX_TEXT:
                    self.draft = text
                    terminal.say("Message limit: 2000 characters.")
                else:
                    self.draft = text
                    if await self.send_message(text):
                        self.draft = ""
                        if not terminal.interactive:
                            terminal.say(f"{self.name}> {text}")
        finally:
            if not terminal.interactive:
                stopped.set()
            self.finished.set()

    async def run(self, url, create=False, code_to_show=None, ca=None):
        kwargs = {}
        if ca:
            if not url.startswith("wss://"):
                raise ValueError("--ca requires wss://.")
            kwargs["ssl"] = ssl.create_default_context(cafile=ca)
        input_task = None
        stop_task = asyncio.create_task(self.finished.wait())
        receiver = None
        deadline = None
        try:
            while not self.finished.is_set():
                if deadline is not None and time.monotonic() >= deadline:
                    raise SessionError("Reconnect window expired. Start a new chat.")
                timeout = 10 if deadline is None else min(10, max(0.1, deadline - time.monotonic()))
                try:
                    async with connect(url, compression=None, max_size=MAX_FRAME, max_queue=16,
                                       open_timeout=timeout, ping_interval=5, ping_timeout=5,
                                       close_timeout=1, proxy=None, **kwargs) as ws:
                        action = "resume" if self.token else "create" if create else "join"
                        hello = {"type": action, "room": self.keys.room, "session": self.session}
                        if self.token:
                            hello["token"] = self.token
                        await ws.send(dumps(hello))
                        remaining = 10 if deadline is None else min(10, max(0.1, deadline - time.monotonic()))
                        reply = parse(await asyncio.wait_for(ws.recv(), remaining))
                        if reply.get("type") == "error":
                            if not isinstance(reply.get("code"), str):
                                raise SessionError("Invalid server response.")
                            messages = {
                                "invalid_room": "Invalid or expired code.",
                                "invalid_resume": "Session expired. Start a new chat.",
                                "full": "This chat already has two participants.",
                                "exists": "Create a new chat with a new code.",
                                "busy": "Server is busy. Try again later.",
                            }
                            if reply.get("code") == "busy" and self.token:
                                raise ConnectionError("Server busy.")
                            raise SessionError(messages.get(reply.get("code"), "Unable to join chat."))
                        if (reply.get("type") != "ready" or reply.get("version") != 2
                                or not isinstance(reply.get("token"), str)
                                or not re.fullmatch(r"[A-Za-z0-9_-]{43}", reply["token"])):
                            raise SessionError("Incompatible server.")
                        reconnected = self.token is not None
                        self.token = reply["token"]
                        self.ws = ws
                        self.online = {self.session}
                        self.peer_ready = False
                        deadline = None
                        if reconnected:
                            self.terminal.say("Reconnected.")
                        elif code_to_show:
                            self.terminal.say(f"Code: {code_to_show}")
                        await ws.send(dumps(self.packet("hello")))
                        if input_task is None:
                            input_task = asyncio.create_task(self.send_input())
                        receiver = asyncio.create_task(self.receive(ws))
                        try:
                            done, _ = await asyncio.wait({receiver, input_task, stop_task},
                                                         return_when=asyncio.FIRST_COMPLETED)
                        except asyncio.CancelledError:
                            with contextlib.suppress(ConnectionClosed, OSError):
                                await ws.send(dumps({"type": "leave"}))
                            raise
                        if input_task in done:
                            input_task.result()
                        if self.finished.is_set():
                            with contextlib.suppress(ConnectionClosed, OSError):
                                await ws.send(dumps({"type": "leave"}))
                            return
                        receiver.result()
                except (ConnectionClosed, ConnectionError, OSError, TimeoutError, asyncio.TimeoutError, InvalidHandshake) as error:
                    if not self.token:
                        raise
                    if isinstance(error, ConnectionClosed) and error.rcvd and error.rcvd.code in {1008, 4001}:
                        raise SessionError("Session ended. Start a new chat.") from error
                    if isinstance(error, ssl.SSLCertVerificationError):
                        raise
                    if deadline is None:
                        deadline = time.monotonic() + self.reconnect_seconds
                        self.terminal.say("Disconnected. Reconnecting...")
                finally:
                    self.ws = None
                    self.peer_ready = False
                    self.online = {self.session}
                    self.fail_pending()
                    for peer in self.peers.values():
                        peer["online"] = False
                    if receiver:
                        receiver.cancel()
                        await asyncio.gather(receiver, return_exceptions=True)
                        receiver = None
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise SessionError("Reconnect window expired. Start a new chat.")
                    with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                        await asyncio.wait_for(self.finished.wait(), min(1, remaining))
        finally:
            self.finished.set()
            self.fail_pending()
            for task in (input_task, receiver, stop_task):
                if task:
                    task.cancel()
            await asyncio.gather(*(task for task in (input_task, receiver, stop_task) if task),
                                 return_exceptions=True)


def argument_parser():
    parser = argparse.ArgumentParser(prog="dark-chat", description="Encrypted terminal chat.", allow_abbrev=False,
                                     epilog="Inside chat: type / for commands. /clean clears the terminal and exits.")
    parser.add_argument("--server", default=os.environ.get("DARK_CHAT_SERVER", DEFAULT_SERVER),
                        help=argparse.SUPPRESS)
    parser.add_argument("--name", help=argparse.SUPPRESS)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--new", action="store_true", help="Create a temporary chat")
    mode.add_argument("--chat", action="store_true", help="Join with an invite code")
    mode.add_argument("--register", action="store_true", help="Create your permanent ID")
    mode.add_argument("--login", action="store_true", help="Open your saved chats")
    mode.add_argument("--restore", metavar="FILE", help=argparse.SUPPRESS)
    parser.add_argument("--allow-insecure", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--ca", help=argparse.SUPPRESS)
    parser.add_argument("--password-file", help=argparse.SUPPRESS)
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def personal_main(args, terminal, url):
    from .personal import AccountConnection, PersonalChat
    from .storage import LocalStore, create_vault, default_home, private_directory, private_write, read_vault
    folder = default_home()
    if args.register and (folder / "identity.json").exists():
        raise ValueError("An identity already exists. Use --login.")
    if args.login and not (folder / "identity.json").exists():
        raise ValueError("No identity on this device. Use --register or restore your encrypted backup.")
    if args.password_file:
        password_path = Path(args.password_file)
        if password_path.stat().st_size > 4096:
            raise ValueError("Invalid password file.")
        password = password_path.read_text(encoding="utf-8").rstrip("\r\n")
    else:
        password = terminal.ask("Password: ", secret=True)
    if args.register:
        if not args.password_file and password != terminal.ask("Confirm password: ", secret=True):
            raise ValueError("Passwords do not match.")
        name = safe_text(args.name if args.name is not None else terminal.ask("Name: ")).strip()
        identity = create_vault(folder, name, password)
        terminal.say("Identity saved on this device. Keep your password and an encrypted backup.")
        terminal.say(f"ID: {identity.id}")

        async def register():
            async with AccountConnection(url, identity, args.ca):
                pass

        asyncio.run(register())
        terminal.say("Registered. Use --login to chat.")
        return
    if args.restore:
        read_vault(args.restore, password)
        private_directory(folder)
        if (folder / "identity.json").exists():
            raise ValueError("An identity already exists. Restore into a separate DARK_CHAT_HOME directory.")
        private_write(folder / "identity.json", Path(args.restore).read_bytes())
        terminal.say("Identity restored. Use --login. History is not included in identity backups.")
        return
    identity = read_vault(folder / "identity.json", password)
    del password
    store = LocalStore(folder, identity)
    try:
        stdout_context = chat_stdout() if terminal.interactive else contextlib.nullcontext()
        with stdout_context:
            asyncio.run(PersonalChat(terminal, store).run(url, args.ca))
    finally:
        store.close()


def main():
    parser = argument_parser()
    args = parser.parse_args()
    terminal = Terminal()
    try:
        url = server_url(args.server, args.allow_insecure)
        if args.register or args.login or args.restore:
            # Permanent identities may never authenticate over plaintext Internet/LAN links.
            url = server_url(args.server)
            if args.ca and not url.startswith("wss://"):
                raise ValueError("--ca requires wss://.")
            personal_main(args, terminal, url)
            return
        name = safe_text(args.name if args.name is not None else terminal.ask("Name: ")).strip()
        if not 1 <= len(name) <= 24:
            raise ValueError("Name must be 1-24 characters.")
        code = os.environ.pop("DARK_CHAT_CODE", "").strip()
        create = args.new
        if not create and not args.chat and not code:
            choice = terminal.ask("Create or join? [c/j]: ").lower()
            if choice not in {"c", "j", ""}:
                raise ValueError("Enter c to create or j to join.")
            create = choice == "c"
        if create:
            code = new_code()
        elif not code:
            code = terminal.ask("Code: ", secret=True)
        keys = room_keys(code)
        stdout_context = chat_stdout() if terminal.interactive else contextlib.nullcontext()
        with stdout_context:
            asyncio.run(Chat(terminal, keys, name).run(url, create, code if create else None, args.ca))
        if terminal.clean_requested is not True:
            terminal.say("Closed.")
    except (KeyboardInterrupt, EOFError):
        terminal.say("Closed.")
    except SessionError as error:
        terminal.say(str(error))
        raise SystemExit(1) from None
    except (InvalidTag, sqlite3.Error):
        terminal.say("Unable to read encrypted local storage. Check your identity backup.")
        raise SystemExit(1) from None
    except (ValueError, OSError, ConnectionClosed, ConnectionError, TimeoutError, asyncio.TimeoutError, InvalidHandshake) as error:
        detail = str(error) if isinstance(error, ValueError) else type(error).__name__
        terminal.say(f"Error: {detail}")
        raise SystemExit(1) from None
    finally:
        if terminal.clean_requested is True:
            clear_terminal(scrollback=True)


if __name__ == "__main__":
    main()
