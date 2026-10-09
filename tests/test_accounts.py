import asyncio
import contextlib
import copy
import io
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from prompt_toolkit.application import create_app_session
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from dark_terminal_chat.client import Chat, Terminal, argument_parser, main
from dark_terminal_chat.identity import Identity, RETENTION, auth_data, canonical, fingerprint, open_message, seal_message, verify_message
from dark_terminal_chat.personal import AccountConnection, MailError, PersonalChat
from dark_terminal_chat.protocol import dumps, new_code, parse, room_keys
from dark_terminal_chat.server import Relay
from dark_terminal_chat.storage import LocalStore, Mailbox, create_vault, read_vault
from dark_terminal_chat.terminal_ui import CommandCompleter, chat_prompt

PASSWORD = "test-only-long-passphrase"


class IdentityTests(unittest.TestCase):
    def test_sealed_unicode_signature_recipient_and_tampering(self):
        alice, bob, eve = [Identity.create(name) for name in ("alice", "bob", "eve")]
        packet = seal_message(alice, bob.public, "Salom أَهْلًا 👋")
        self.assertEqual(open_message(bob, packet)["text"], "Salom أَهْلًا 👋")
        self.assertNotIn("Salom", dumps(packet))
        self.assertNotEqual(packet["body"], seal_message(alice, bob.public, "Salom أَهْلًا 👋")["body"])
        for field, value in [("to", eve.id), ("from", eve.id), ("public", eve.public),
                             ("created", packet["created"] - 1), ("body", new_code())]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                open_message(bob, {**packet, field: value})
        with self.assertRaises(ValueError):
            open_message(eve, packet)
        with self.assertRaises(ValueError):
            verify_message(packet, now=time.time() + RETENTION + 1)
        with self.assertRaises(ValueError):
            fingerprint({**bob.public, "name": "alice"})

    def test_encrypted_vault_password_history_contacts_and_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            alice = create_vault(root, "alice", PASSWORD)
            path = root / "identity.json"
            self.assertNotIn(b"alice", path.read_bytes())
            self.assertEqual(read_vault(path, PASSWORD).id, alice.id)
            with self.assertRaisesRegex(ValueError, "Wrong password"):
                read_vault(path, PASSWORD + "wrong")
            with self.assertRaises(ValueError):
                create_vault(root, "replace", PASSWORD)
            bob = Identity.create("bob")
            store = LocalStore(root, alice)
            store.add_contact(bob.public, "my-friend")
            packet = seal_message(alice, bob.public, "private-history-marker")
            store.save(packet, {"name": "alice", "text": "private-history-marker"}, "out", True)
            self.assertFalse(store.save(packet, {"name": "alice", "text": "duplicate"}, "out", True))
            self.assertEqual(store.messages()[0]["text"], "private-history-marker")
            self.assertTrue(store.messages()[0]["pending"])
            store.sent(packet["id"])
            self.assertFalse(store.messages(pending=True))
            backup = store.backup()
            self.assertEqual(read_vault(backup, PASSWORD).id, alice.id)
            store.close()
            raw = (root / "history.db").read_bytes()
            self.assertNotIn(b"private-history-marker", raw)
            self.assertNotIn(b"my-friend", raw)
            self.assertNotIn(bytes(alice.signing), raw)
            reopened = LocalStore(root, read_vault(path, PASSWORD))
            self.assertEqual(reopened.contacts()[bob.id]["alias"], "my-friend")
            self.assertEqual(len(reopened.messages()), 1)
            reopened.close()
            with self.assertRaises(ValueError):
                LocalStore(root, bob)
            if os.name == "posix":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_mailbox_bounds_replay_and_foreign_ack(self):
        with tempfile.TemporaryDirectory() as temporary:
            mail = Mailbox(Path(temporary) / "mail.db", per_user=1)
            alice, bob, eve = [Identity.create(name) for name in ("alice", "bob", "eve")]
            for identity in (alice, bob, eve):
                mail.register(identity.public)
            packet = seal_message(alice, bob.public, "secret-text-marker")
            mail.send(alice.id, packet)
            mail.send(alice.id, packet)
            with self.assertRaisesRegex(ValueError, "full"):
                mail.send(alice.id, seal_message(alice, bob.public, "overflow"))
            self.assertIsNone(mail.fetch(eve.id))
            mail.acknowledge(eve.id, packet["id"])
            self.assertEqual(mail.fetch(bob.id), packet)
            mail.acknowledge(bob.id, packet["id"])
            mail.send(alice.id, packet)
            self.assertIsNone(mail.fetch(bob.id))
            with self.assertRaises(ValueError):
                mail.send(eve.id, packet)
            mail.close()
            self.assertNotIn(b"secret-text-marker", (Path(temporary) / "mail.db").read_bytes())

    def test_simple_help_and_password_not_a_command_line_argument(self):
        help_text = argument_parser().format_help()
        for option in ("--register", "--login", "--new", "--chat", "/clean"):
            self.assertIn(option, help_text)
        for option in ("--server", "--name", "--allow-insecure", "--ca", "--password-file"):
            self.assertNotIn(option, help_text)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            argument_parser().parse_args(["--password", "a-secret"])

    def test_clean_runs_after_chat_shutdown_without_printing_closed(self):
        from unittest.mock import AsyncMock
        terminal = MagicMock(interactive=False, clean_requested=True)
        with patch.object(sys, "argv", ["dark-chat", "--new", "--name", "alice"]), \
                patch("dark_terminal_chat.client.Terminal", return_value=terminal), \
                patch.object(Chat, "run", new_callable=AsyncMock) as run, \
                patch("dark_terminal_chat.client.clear_terminal") as clear:
            main()
            run.assert_awaited_once()
            clear.assert_called_once_with(scrollback=True)
        self.assertNotIn("Closed.", [call.args[0] for call in terminal.say.call_args_list])

    def test_account_refuses_insecure_remote_even_with_override(self):
        with patch.object(sys, "argv", ["dark-chat", "--login", "--server", "ws://192.0.2.1/ws", "--allow-insecure"]), \
                patch("dark_terminal_chat.client.personal_main") as personal, \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            main()
        personal.assert_not_called()


class CompletionTests(unittest.IsolatedAsyncioTestCase):
    async def test_slash_shows_clean_and_enter_accepts_and_returns(self):
        options = list(CommandCompleter().get_completions(Document("/"), None))
        self.assertEqual(options[0].text, "/clean")
        self.assertEqual(list(CommandCompleter().get_completions(Document("hello"), None)), [])
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            session = chat_prompt()
            task = asyncio.create_task(session.prompt_async("> "))
            pipe.send_text("/")
            for _ in range(100):
                if session.default_buffer.complete_state:
                    break
                await asyncio.sleep(0.01)
            self.assertIsNotNone(session.default_buffer.complete_state)
            pipe.send_text("\r")
            self.assertEqual(await asyncio.wait_for(task, 3), "/clean")

    async def test_clean_exits_temporary_chat_and_is_never_sent(self):
        terminal = MagicMock(interactive=False, clean_requested=False)
        chat = Chat(terminal, room_keys(new_code()), "alice")
        with patch("sys.stdin", io.StringIO("/clean\n")):
            await asyncio.wait_for(chat.send_input(), 3)
        self.assertTrue(terminal.clean_requested)
        self.assertTrue(chat.finished.is_set())
        self.assertEqual(chat.seq, 0)


class AccountServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        self.alice, self.bob, self.eve = [Identity.create(name) for name in ("alice", "bob", "eve")]
        self.relay = Relay(database_path=self.root / "server" / "mail.db")
        self.server = await self.relay.serve("127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{self.port}/ws"

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()
        self.relay.close()

    async def test_offline_mail_survives_actual_server_restart_and_ack(self):
        async with AccountConnection(self.url, self.bob):
            pass
        packet = seal_message(self.alice, self.bob.public, "offline أَهْلًا 👋")
        async with AccountConnection(self.url, self.alice) as connection:
            reply = await connection.request({"type": "lookup", "id": self.bob.id}, "public")
            self.assertEqual(fingerprint(reply["public"]), self.bob.id)
            await connection.request({"type": "send", "message": packet}, "stored")
        self.server.close()
        await self.server.wait_closed()
        self.relay.close()
        self.relay = Relay(database_path=self.root / "server" / "mail.db")
        self.server = await self.relay.serve("127.0.0.1", self.port)
        async with AccountConnection(self.url, self.eve) as eve:
            self.assertIsNone((await eve.request({"type": "fetch"}, "mail"))["message"])
            await eve.request({"type": "ack", "id": packet["id"]}, "acknowledged")
            with self.assertRaises(MailError):
                await eve.request({"type": "send", "message": packet}, "stored")
        async with AccountConnection(self.url, self.bob) as bob:
            frame = (await bob.request({"type": "fetch"}, "mail"))["message"]
            self.assertEqual(open_message(self.bob, frame)["text"], "offline أَهْلًا 👋")
            await bob.request({"type": "ack", "id": packet["id"]}, "acknowledged")
            self.assertIsNone((await bob.request({"type": "fetch"}, "mail"))["message"])

    async def test_old_challenge_signature_cannot_authenticate_again(self):
        async with connect(self.url, proxy=None) as ws:
            await ws.send(dumps({"type": "account", "version": 1}))
            nonce = parse(await ws.recv())["nonce"]
            auth = {"type": "auth", "public": self.alice.public, "register": True,
                    "signature": self.alice.sign(auth_data(nonce, self.alice.public, True))}
            await ws.send(dumps(auth))
            self.assertEqual(parse(await ws.recv())["type"], "account_ready")
        async with connect(self.url, proxy=None) as ws:
            await ws.send(dumps({"type": "account", "version": 1}))
            self.assertNotEqual(parse(await ws.recv())["nonce"], nonce)
            await ws.send(dumps(auth))
            with self.assertRaises(ConnectionClosed):
                await ws.recv()

    async def test_key_substitution_refused_and_pending_saved_before_network(self):
        terminal = MagicMock(interactive=False, clean_requested=False)
        store = LocalStore(self.root / "client", self.alice)
        self.addCleanup(store.close)
        chat = PersonalChat(terminal, store)
        async with AccountConnection(self.url, self.bob):
            pass
        async with AccountConnection(self.url, self.alice) as connection:
            chat.connection = connection
            await chat.select(self.bob.id + " friend")
            with patch.object(connection, "request", return_value={"public": self.eve.public}):
                with self.assertRaisesRegex(MailError, "does not match"):
                    await chat.select(Identity.create("target").id)
            chat.connection = None
            await chat.handle("saved-offline-message")
            self.assertEqual(store.messages(pending=True)[0]["text"], "saved-offline-message")
            chat.connection = connection
            await chat.synchronize()
            self.assertFalse(store.messages(pending=True))
            await chat.handle("clean")
            self.assertTrue(terminal.clean_requested)
            self.assertTrue(chat.finished.is_set())

    async def test_duplicate_fetch_after_lost_ack_does_not_duplicate_history(self):
        terminal = MagicMock(interactive=False, clean_requested=False)
        store = LocalStore(self.root / "bob-history", self.bob)
        self.addCleanup(store.close)
        packet = seal_message(self.alice, self.bob.public, "save before ack")
        async with AccountConnection(self.url, self.bob), AccountConnection(self.url, self.alice) as alice:
            await alice.request({"type": "send", "message": packet}, "stored")
        async with AccountConnection(self.url, self.bob) as bob:
            frame = (await bob.request({"type": "fetch"}, "mail"))["message"]
            store.save(frame, open_message(self.bob, frame), "in")
            # Simulate exit after durable local save but before acknowledgment.
        async with AccountConnection(self.url, self.bob) as bob:
            chat = PersonalChat(terminal, store)
            chat.connection = bob
            await chat.synchronize()
            self.assertEqual(len(store.messages()), 1)
            self.assertIsNone(self.relay.mailbox.fetch(self.bob.id))

    async def test_permanent_client_reconnects_after_server_restart(self):
        terminal = MagicMock(interactive=False, clean_requested=False)
        store = LocalStore(self.root / "reconnecting", self.alice)
        self.addCleanup(store.close)
        chat = PersonalChat(terminal, store)
        task = asyncio.create_task(chat.network(self.url, None))

        async def until(predicate):
            for _ in range(500):
                if predicate():
                    return
                await asyncio.sleep(0.02)
            self.fail("Account did not reconnect")

        try:
            await until(lambda: chat.connection is not None)
            previous = chat.connection
            self.server.close()
            await self.server.wait_closed()
            self.relay.close()
            self.relay = Relay(database_path=self.root / "server" / "mail.db")
            self.server = await self.relay.serve("127.0.0.1", self.port)
            await until(lambda: chat.connection is not None and chat.connection is not previous)
            self.assertIsNotNone(self.relay.mailbox.lookup(self.alice.id))
            self.assertIn("Reconnected.", [call.args[0] for call in terminal.say.call_args_list])
        finally:
            chat.finished.set()
            await asyncio.wait_for(task, 5)

    async def test_actual_cli_registration_offline_delivery_history_and_clean(self):
        password_file = self.root / "password"
        password_file.write_text(PASSWORD)
        homes = {name: self.root / name for name in ("alice", "bob")}
        identities = {}
        for name in homes:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "dark_terminal_chat", "--register", "--name", name,
                "--password-file", str(password_file), "--server", self.url,
                env={**os.environ, "DARK_CHAT_HOME": str(homes[name]), "PYTHONIOENCODING": "utf-8"},
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            stdout, _ = await asyncio.wait_for(process.communicate(), 20)
            self.assertEqual(process.returncode, 0, stdout.decode())
            identities[name] = read_vault(homes[name] / "identity.json", PASSWORD)
        children, collectors, output = [], [], {}

        async def launch(name):
            child = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "dark_terminal_chat", "--login", "--password-file", str(password_file),
                "--server", self.url, env={**os.environ, "DARK_CHAT_HOME": str(homes[name]), "PYTHONIOENCODING": "utf-8"},
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            children.append(child)
            output[child.pid] = ""

            async def collect():
                async for line in child.stdout:
                    output[child.pid] += line.decode("utf-8")

            collectors.append(asyncio.create_task(collect()))
            return child

        async def until(child, text):
            for _ in range(500):
                if text in output[child.pid]:
                    return
                if child.returncode is not None:
                    break
                await asyncio.sleep(0.02)
            self.fail(f"Missing {text}: {output[child.pid]}")

        async def send(child, text):
            child.stdin.write((text + "\n").encode("utf-8"))
            await child.stdin.drain()

        try:
            alice = await launch("alice")
            await until(alice, "Connected.")
            await send(alice, "/chat " + identities["bob"].id + " friend")
            await until(alice, "Chat: ")
            await send(alice, "persisted Unicode أَهْلًا 👋")
            await until(alice, "Queued.")
            for _ in range(200):
                if self.relay.mailbox.fetch(identities["bob"].id):
                    break
                await asyncio.sleep(0.02)
            self.assertIsNotNone(self.relay.mailbox.fetch(identities["bob"].id))
            await send(alice, "/clean")
            self.assertEqual(await asyncio.wait_for(alice.wait(), 5), 0)
            bob = await launch("bob")
            await until(bob, "New message from ")
            await send(bob, "/chat " + identities["alice"].id)
            await until(bob, "alice> persisted Unicode أَهْلًا 👋")
            await send(bob, "offline reply")
            await until(bob, "Queued.")
            for _ in range(200):
                if self.relay.mailbox.fetch(identities["alice"].id):
                    break
                await asyncio.sleep(0.02)
            self.assertIsNotNone(self.relay.mailbox.fetch(identities["alice"].id))
            await send(bob, "/clean")
            self.assertEqual(await asyncio.wait_for(bob.wait(), 5), 0)
            alice2 = await launch("alice")
            await until(alice2, "New message from ")
            await send(alice2, "/chat friend")
            await until(alice2, "bob> offline reply")
            await until(alice2, "alice> persisted Unicode أَهْلًا 👋")
            await send(alice2, "clean")
            self.assertEqual(await asyncio.wait_for(alice2.wait(), 5), 0)
            await asyncio.gather(*collectors)
            for result in output.values():
                self.assertNotIn(PASSWORD, result)
                self.assertNotIn("Traceback", result)
                self.assertNotIn("Closed.", result)
        finally:
            for child in children:
                if child.returncode is None:
                    child.kill()
                    await child.wait()
            await asyncio.gather(*collectors, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
