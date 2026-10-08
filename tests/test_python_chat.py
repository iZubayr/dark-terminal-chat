import asyncio
import contextlib
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from prompt_toolkit import PromptSession
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from dark_terminal_chat.client import Chat, SessionError, server_url
from dark_terminal_chat.protocol import decrypt, dumps, encrypt, new_code, parse, room_keys, safe_text, valid_message
from dark_terminal_chat.server import Relay

ROOT = Path(__file__).resolve().parents[1]


class ProtocolTests(unittest.TestCase):
    def test_crypto_unicode_wrong_key_tampering_and_escapes(self):
        keys = room_keys(new_code())
        message = {"text": "Salom! أَهْلًا 👋"}
        envelope = encrypt(keys, message)
        self.assertEqual(decrypt(keys, envelope), message)
        self.assertNotIn("Salom", dumps(envelope))
        self.assertNotEqual(envelope["iv"], encrypt(keys, message)["iv"])
        with self.assertRaises(InvalidTag):
            decrypt(room_keys(new_code()), envelope)
        envelope["tag"] = ("A" if envelope["tag"][0] != "A" else "B") + envelope["tag"][1:]
        with self.assertRaises(InvalidTag):
            decrypt(keys, envelope)
        with self.assertRaises(ValueError):
            room_keys("1234")
        self.assertEqual(safe_text("abc\x1b]52;c;SECRET\x07\n\u202Etext"), "abc]52;c;SECRETtext")
        self.assertFalse(valid_message({"kind": []}))
        with self.assertRaises(ValueError):
            parse("[" * 10000)

    def test_secure_server_urls(self):
        self.assertEqual(server_url("https://account.alwaysdata.net/dark-chat/ws"),
                         "wss://account.alwaysdata.net/dark-chat/ws")
        self.assertEqual(server_url("ws://127.0.0.1:8080"), "ws://127.0.0.1:8080/ws")
        for value in ["ws://remote.example/ws", "wss://example.com/ws?code=secret",
                      "wss://user:password@example.com", "file:///tmp/file", "wss://example.com\n"]:
            with self.subTest(value=value):
                if value.endswith("\n"):
                    # Outer whitespace from a copied address is harmless.
                    self.assertEqual(server_url(value), "wss://example.com/ws")
                else:
                    with self.assertRaises(ValueError):
                        server_url(value)

    @unittest.skipUnless(shutil.which("node") and (ROOT / "lib" / "crypto.mjs").exists(), "Original Node runtime/source absent")
    def test_crypto_interoperates_with_original_node_implementation(self):
        code = new_code()
        keys = room_keys(code)
        message = {"text": "O‘zbekcha va عَرَبِيّ 👋"}
        source = """
            import {roomKeys, encrypt, decrypt} from './lib/crypto.mjs';
            let input = '';
            for await (const chunk of process.stdin) input += chunk;
            const data = JSON.parse(input), keys = roomKeys(data.code);
            process.stdout.write(JSON.stringify({room: keys.room,
              decoded: decrypt(keys, data.envelope), envelope: encrypt(keys, data.message)}));
        """
        result = subprocess.run(["node", "--input-type=module", "-e", source], cwd=ROOT,
                                input=dumps({"code": code, "message": message,
                                             "envelope": encrypt(keys, message)}),
                                text=True, encoding="utf-8", capture_output=True, check=True, timeout=10)
        reply = json.loads(result.stdout)
        self.assertEqual(reply["room"], keys.room)
        self.assertEqual(reply["decoded"], message)
        self.assertEqual(decrypt(keys, reply["envelope"]), message)


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.relay = Relay()
        self.server = await self.relay.serve("127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{self.port}/ws"
        self.clients = []
        self.tokens = {}

    async def asyncTearDown(self):
        for client in self.clients:
            await client.close()
        self.server.close()
        await self.server.wait_closed()
        self.relay.close()

    async def peer(self, keys, url=None, action="create", session=None, token=None):
        ws = await connect(url or self.url, proxy=None)
        self.clients.append(ws)
        session = session or str(uuid.uuid4())
        await ws.send(dumps({"type": action, "room": keys.room, "session": session, "token": token}))
        reply = await self.next(ws, "ready")
        self.tokens[ws] = reply["token"]
        self.assertEqual(reply["version"], 2)
        return ws, session

    async def until(self, predicate):
        for _ in range(200):
            if predicate():
                return
            await asyncio.sleep(0.01)
        self.fail("Condition not reached")

    async def leave(self, ws):
        await ws.send(dumps({"type": "leave"}))
        await ws.wait_closed()

    def envelope(self, keys, message, message_id=1, receipt=False):
        return {**encrypt(keys, message), "id": message_id, "receipt": receipt}

    async def next(self, ws, kind):
        async def read():
            while True:
                frame = parse(await ws.recv())
                if frame["type"] == kind:
                    return frame
        return await asyncio.wait_for(read(), 4)

    async def rejected(self, keys, action="join", session=None, token=None):
        ws = await connect(self.url, proxy=None)
        self.clients.append(ws)
        await ws.send(dumps({"type": action, "room": keys.room,
                             "session": session or str(uuid.uuid4()), "token": token}))
        reply = await self.next(ws, "error")
        await ws.wait_closed()
        return reply["code"]

    async def test_two_slots_and_closed_code_cannot_open_a_new_chat(self):
        keys = room_keys(new_code())
        a, _ = await self.peer(keys)
        b, _ = await self.peer(keys, action="join")
        self.assertEqual(await self.rejected(keys), "full")
        self.assertEqual(len(self.relay.rooms[keys.room].participants), 2)
        await self.leave(a)
        await self.leave(b)
        await self.until(lambda: keys.room not in self.relay.rooms)
        self.assertEqual(await self.rejected(keys), "invalid_room")
        self.assertEqual(await self.rejected(room_keys(new_code())), "invalid_room")

    async def test_offline_slot_is_reserved_and_only_its_token_can_resume(self):
        self.assertEqual(self.relay.reconnect_seconds, 60)
        keys = room_keys(new_code())
        a, sid_a = await self.peer(keys)
        b, _ = await self.peer(keys, action="join")
        token = self.tokens[a]
        a.transport.abort()
        await self.until(lambda: self.relay.rooms[keys.room].participants[sid_a].socket is None)
        self.assertEqual(await self.rejected(keys), "full")
        self.assertEqual(await self.rejected(keys, "resume", sid_a, new_code()), "invalid_resume")
        resumed, same_sid = await self.peer(keys, action="resume", session=sid_a, token=token)
        self.assertEqual(same_sid, sid_a)
        self.assertIsNone(self.relay.rooms[keys.room].participants[sid_a].expiry)
        self.assertEqual(len(self.relay.rooms[keys.room].participants), 2)
        await b.send(dumps(self.envelope(keys, {"text": "same conversation"})))
        self.assertEqual(decrypt(keys, await self.next(resumed, "message"))["text"], "same conversation")

    async def test_resume_replaces_a_stale_connection_without_freeing_its_slot(self):
        keys = room_keys(new_code())
        a, sid = await self.peer(keys)
        await self.peer(keys, action="join")
        resumed, _ = await self.peer(keys, action="resume", session=sid, token=self.tokens[a])
        await a.wait_closed()
        await self.until(lambda: resumed.state.name == "OPEN")
        self.assertIsNotNone(self.relay.rooms[keys.room].participants[sid].socket)
        self.assertEqual(await self.rejected(keys), "full")

    async def test_both_disconnected_sessions_expire_and_old_code_is_rejected(self):
        self.relay.reconnect_seconds = 0.2
        keys = room_keys(new_code())
        a, sid_a = await self.peer(keys)
        b, _ = await self.peer(keys, action="join")
        token = self.tokens[a]
        a.transport.abort()
        b.transport.abort()
        await self.until(lambda: keys.room in self.relay.rooms and
                         all(peer.socket is None for peer in self.relay.rooms[keys.room].participants.values()))
        self.assertEqual(len(self.relay.rooms[keys.room].participants), 2)
        await self.until(lambda: keys.room not in self.relay.rooms)
        self.assertEqual(await self.rejected(keys, "resume", sid_a, token), "invalid_room")
        self.assertEqual(await self.rejected(keys), "invalid_room")

    async def test_peer_offline_refuses_messages_and_does_not_queue_them(self):
        keys = room_keys(new_code())
        a, _ = await self.peer(keys)
        b, sid_b = await self.peer(keys, action="join")
        token = self.tokens[b]
        b.transport.abort()
        await self.until(lambda: self.relay.rooms[keys.room].participants[sid_b].socket is None)
        await a.send(dumps(self.envelope(keys, {"text": "must not queue"}, receipt=True)))
        self.assertEqual((await self.next(a, "not_delivered"))["id"], 1)
        resumed, _ = await self.peer(keys, action="resume", session=sid_b, token=token)
        await self.next(resumed, "presence")
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(resumed.recv(), 0.1)
        await a.send(dumps(self.envelope(keys, {"text": "new message"}, message_id=2)))
        self.assertEqual(decrypt(keys, await self.next(resumed, "message"))["text"], "new message")

    async def test_reconnect_attempts_end_when_the_budget_expires(self):
        class QuietTerminal:
            interactive = False
            def say(self, text):
                pass

        class QuietChat(Chat):
            async def send_input(self):
                await self.finished.wait()

        chat = QuietChat(QuietTerminal(), room_keys(new_code()), "elliot", reconnect_seconds=0.2)
        task = asyncio.create_task(chat.run(self.url, create=True))
        try:
            await self.until(lambda: chat.token is not None)
            room = self.relay.rooms[chat.keys.room]
            self.relay.max_clients = 0
            room.participants[chat.session].socket.transport.abort()
            with self.assertRaisesRegex(SessionError, "Reconnect window expired"):
                await asyncio.wait_for(task, 4)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_cancelled_client_leaves_without_reserving_a_slot(self):
        class QuietTerminal:
            interactive = False
            def say(self, text):
                pass

        class QuietChat(Chat):
            async def send_input(self):
                await self.finished.wait()

        chat = QuietChat(QuietTerminal(), room_keys(new_code()), "elliot")
        task = asyncio.create_task(chat.run(self.url, create=True))
        await self.until(lambda: chat.ws is not None)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await self.until(lambda: chat.keys.room not in self.relay.rooms)

    async def test_real_input_buffer_keeps_a_draft_while_peer_is_offline(self):
        class InputTerminal:
            interactive = True
            def __init__(self):
                self.lines = []
            def say(self, text):
                self.lines.append(text)

        terminal = InputTerminal()
        chat = Chat(terminal, room_keys(new_code()), "elliot")
        sessions = []
        def make_session(*args, **kwargs):
            session = PromptSession(*args, **kwargs)
            sessions.append(session)
            return session

        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            with patch("dark_terminal_chat.client.PromptSession", side_effect=make_session):
                task = asyncio.create_task(chat.send_input())
                try:
                    await self.until(lambda: len(sessions) == 1)
                    pipe.send_text("keep this draft\n")
                    await self.until(lambda: "Sending paused. Draft kept." in terminal.lines)
                    await self.until(lambda: sessions[0].default_buffer.text == "keep this draft")
                    self.assertEqual(chat.draft, "keep this draft")
                    self.assertEqual(chat.pending, {})
                    # Text remains editable after the blocked send.
                    pipe.send_text(" edited")
                    await self.until(lambda: sessions[0].default_buffer.text == "keep this draft edited")
                    pipe.send_text("\x15/exit\n")
                    await asyncio.wait_for(task, 3)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_two_way_unicode_room_isolation_and_empty_room_cleanup(self):
        keys = room_keys(new_code())
        a, sid_a = await self.peer(keys)
        b, sid_b = await self.peer(keys, action="join")
        outsider, _ = await self.peer(room_keys(new_code()))
        await self.next(outsider, "presence")
        await a.send(dumps(self.envelope(keys, {"text": "Salom! أَهْلًا 👋"})))
        frame = await self.next(b, "message")
        self.assertEqual(frame["sender"], sid_a)
        self.assertEqual(decrypt(keys, frame)["text"], "Salom! أَهْلًا 👋")
        await b.send(dumps(self.envelope(keys, {"text": "Javob"})))
        frame = await self.next(a, "message")
        self.assertEqual(frame["sender"], sid_b)
        self.assertEqual(decrypt(keys, frame)["text"], "Javob")
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(outsider.recv(), 0.1)
        await self.leave(a)
        await self.leave(b)
        await self.until(lambda: keys.room not in self.relay.rooms)
        self.assertNotIn(keys.room, self.relay.rooms)

    async def test_invalid_clients_cannot_crash_relay(self):
        for payload in ["null", "not-json", '{"type":"join","room":"bad"}', "[" * 10000,
                        '{"type":"join","room":[],"session":null}']:
            ws = await connect(self.url, proxy=None)
            await ws.send(payload)
            with self.assertRaises(ConnectionClosed):
                await asyncio.wait_for(ws.recv(), 3)
            await ws.close()
        ws, _ = await self.peer(room_keys(new_code()))
        await ws.send("x" * 20001)
        with self.assertRaises(ConnectionClosed):
            while True:
                await asyncio.wait_for(ws.recv(), 3)
        ws, sid = await self.peer(room_keys(new_code()))
        self.assertEqual((await self.next(ws, "presence"))["sessions"], [sid])

    async def test_health_http_paths_origin_and_rate_limit(self):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write(b"GET /health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        await writer.drain()
        response = await asyncio.wait_for(reader.read(), 3)
        writer.close()
        await writer.wait_closed()
        self.assertIn(b"200 OK", response)
        self.assertIn(b'{"status":"ok"}', response)
        with self.assertRaises(InvalidStatus):
            async with connect(f"ws://127.0.0.1:{self.port}/missing", proxy=None):
                pass
        with self.assertRaises(InvalidStatus):
            async with connect(self.url, origin="https://unknown.example", proxy=None):
                pass
        keys = room_keys(new_code())
        ws, _ = await self.peer(keys)
        for _ in range(41):
            await ws.send(dumps(self.envelope(keys, {"text": "burst"})))
        with self.assertRaises(ConnectionClosed):
            while True:
                await asyncio.wait_for(ws.recv(), 3)

    async def test_real_cli_processes_exchange_messages_and_exit(self):
        children = []
        outputs = {}
        async def launch(name, code=None, create=False):
            arguments = [sys.executable, "-m", "dark_terminal_chat", "--server", self.url, "--name", name]
            if create:
                arguments.append("--new")
            child = await asyncio.create_subprocess_exec(
                *arguments,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                env={**os.environ, "DARK_CHAT_CODE": code or "", "PYTHONIOENCODING": "utf-8"})
            children.append(child)
            outputs[child.pid] = ""
            async def collect():
                async for raw in child.stdout:
                    outputs[child.pid] += raw.decode("utf-8")
            return child, asyncio.create_task(collect())
        async def until(child, text):
            for _ in range(250):
                if text in outputs[child.pid]:
                    return
                await asyncio.sleep(0.02)
            self.fail(f"Missing {text}: {outputs[child.pid]}")
        try:
            a, output_a = await launch("elliot", create=True)
            await until(a, "Code: ")
            code = outputs[a.pid].split("Code: ", 1)[1].splitlines()[0]
            keys = room_keys(code)
            a.stdin.write(b"unsent draft\n")
            await a.stdin.drain()
            await until(a, "Sending paused. Draft kept.")
            b, output_b = await launch("whiterose", code)
            await asyncio.gather(until(a, "whiterose joined."), until(b, "elliot joined."))
            self.assertNotIn("unsent draft", outputs[b.pid])
            a.stdin.write("Salom! أَهْلًا 👋\n".encode())
            await a.stdin.drain()
            await until(b, "elliot> Salom! أَهْلًا 👋")
            b.stdin.write(b"Javob\n")
            await b.stdin.drain()
            await until(a, "whiterose> Javob")
            a.stdin.write(b"/who\n")
            await a.stdin.drain()
            await until(a, "Participants: elliot, whiterose")
            # Lose the actual TCP connection and let the installed client resume.
            room = self.relay.rooms[keys.room]
            sid = next(sid for sid, peer in room.participants.items() if peer.socket is not None)
            room.participants[sid].socket.transport.abort()
            await until(a, "Disconnected. Reconnecting...")
            await until(a, "Reconnected.")
            await until(b, "elliot reconnected.")
            a.stdin.write(b"after reconnect\n")
            await a.stdin.drain()
            await until(b, "elliot> after reconnect")
            a.stdin.write(b"/exit\n")
            await a.stdin.drain()
            self.assertEqual(await asyncio.wait_for(a.wait(), 5), 0)
            await until(b, "elliot left.")
            b.stdin.write(b"/who\n")
            await b.stdin.drain()
            await until(b, "Participants: whiterose")
            b.stdin.write(b"/exit\n")
            await b.stdin.drain()
            self.assertEqual(await asyncio.wait_for(b.wait(), 5), 0)
            await asyncio.gather(output_a, output_b)
            self.assertNotIn(code, outputs[b.pid])
            for output in outputs.values():
                self.assertNotIn("DARK", output)
                self.assertNotIn("ACCESS", output)
                self.assertNotIn("\x1b", output)
            await self.until(lambda: keys.room not in self.relay.rooms)
        finally:
            for child in children:
                if child.returncode is None:
                    child.kill()
                    await child.wait()

    async def test_tls_verifies_certificate_and_hostname(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
            now = datetime.now(timezone.utc)
            cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                    .public_key(key.public_key()).serial_number(x509.random_serial_number())
                    .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
                    .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
                    .sign(key, hashes.SHA256()))
            cert_path, key_path = folder / "cert.pem", folder / "key.pem"
            cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                   serialization.NoEncryption()))
            server_ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_ssl.load_cert_chain(cert_path, key_path)
            client_ssl = ssl.create_default_context(cafile=str(cert_path))
            async with self.relay.serve("127.0.0.1", 0, ssl=server_ssl) as tls_server:
                port = tls_server.sockets[0].getsockname()[1]
                with self.assertRaises(ssl.SSLCertVerificationError):
                    async with connect(f"wss://localhost:{port}/ws", proxy=None):
                        pass
                with self.assertRaises(ssl.SSLCertVerificationError):
                    async with connect(f"wss://127.0.0.1:{port}/ws", ssl=client_ssl, proxy=None):
                        pass
                async with connect(f"wss://localhost:{port}/ws", ssl=client_ssl, proxy=None) as ws:
                    await ws.send(dumps({"type": "create", "room": room_keys(new_code()).room,
                                         "session": str(uuid.uuid4())}))
                    self.assertEqual((await self.next(ws, "ready"))["type"], "ready")


if __name__ == "__main__":
    unittest.main()
