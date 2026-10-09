"""Keep interactive chat usable when clock_nanosleep is unavailable (iSH)."""

import asyncio
import ctypes
import os
from pathlib import Path
import platform
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from dark_terminal_chat.identity import Identity, open_message, seal_message
from dark_terminal_chat.personal import AccountConnection
from dark_terminal_chat.server import Relay
from dark_terminal_chat.storage import LocalStore, create_vault
from dark_terminal_chat.terminal_ui import chat_prompt, chat_stdout


class RecordingOutput(DummyOutput):
    def __init__(self):
        self.text = ""

    def write(self, data):
        self.text += data

    def write_raw(self, data):
        self.text += data


class OutputTests(unittest.IsolatedAsyncioTestCase):
    async def test_incoming_output_preserves_draft_and_slash_completion_without_sleep(self):
        output = RecordingOutput()
        original_stdout, original_stderr = sys.stdout, sys.stderr
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
            session = chat_prompt(personal=True)
            with patch("time.sleep", side_effect=AssertionError("clock_nanosleep unavailable")) as sleep:
                with chat_stdout():
                    task = asyncio.create_task(session.prompt_async("alice> "))
                    try:
                        pipe.send_text("unfinished draft")
                        for _ in range(200):
                            if session.default_buffer.text == "unfinished draft":
                                break
                            await asyncio.sleep(0.01)
                        print("bob> incoming encrypted message", flush=True)
                        print("second output", file=sys.stderr, flush=True)
                        for _ in range(200):
                            if "second output" in output.text:
                                break
                            await asyncio.sleep(0.01)
                        self.assertIn("bob> incoming encrypted message", output.text)
                        self.assertIn("second output", output.text)
                        self.assertEqual(session.default_buffer.text, "unfinished draft")
                        pipe.send_text("\x15/\r")
                        self.assertEqual(await asyncio.wait_for(task, 3), "/clean")
                    finally:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                sleep.assert_not_called()
        self.assertIs(sys.stdout, original_stdout)
        self.assertIs(sys.stderr, original_stderr)


def deny_clock_nanosleep():
    # Linux x86_64 syscall 230 is the same operation as iSH's x86 syscall 267.
    # Kill only the child process if it calls it, exactly like missing syscalls
    # on iSH. New threads inherit this filter; the test runner is unaffected.
    class Filter(ctypes.Structure):
        _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte),
                    ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint)]

    class Program(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ushort), ("filters", ctypes.POINTER(Filter))]

    filters = (Filter * 4)(Filter(0x20, 0, 0, 0), Filter(0x15, 0, 1, 230),
                           Filter(0x06, 0, 0, 0x80000000), Filter(0x06, 0, 0, 0x7fff0000))
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) or libc.prctl(22, 2, ctypes.byref(Program(4, filters)), 0, 0):
        raise OSError(ctypes.get_errno(), "Unable to install syscall test filter")


@unittest.skipUnless(sys.platform == "linux" and platform.machine() == "x86_64"
                     and ctypes.sizeof(ctypes.c_void_p) == 8 and sys.version_info >= (3, 11),
                     "Linux x86_64 Python 3.11+ syscall regression")
class RestrictedTerminalTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_interactive_cli_receives_sends_and_cleans_when_sleep_syscall_is_fatal(self):
        import fcntl
        import struct
        import termios

        probe = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import time; time.sleep(0.01)",
            preexec_fn=deny_clock_nanosleep,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertEqual(await asyncio.wait_for(probe.wait(), 5), -signal.SIGSYS,
                         "The filter must actually reject Python's sleep syscall")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            password = "test-only-terminal-long-passphrase"
            password_file = root / "password"
            password_file.write_text(password)
            alice = create_vault(root / "alice", "alice", password)
            bob = Identity.create("bob")
            relay = Relay(database_path=root / "relay.db")
            server = await relay.serve("127.0.0.1", 0)
            url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/ws"
            master, slave = os.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
            os.set_blocking(master, False)
            child = None
            output = bytearray()
            loop = asyncio.get_running_loop()

            def collect():
                try:
                    output.extend(os.read(master, 65536))
                except (BlockingIOError, OSError):
                    pass

            async def until(predicate):
                for _ in range(500):
                    if predicate():
                        return
                    if child and child.returncode is not None:
                        break
                    await asyncio.sleep(0.02)
                self.fail("Interactive CLI did not reach expected state: " + output.decode(errors="replace"))

            def outbox_acknowledged():
                database = sqlite3.connect((root / "alice" / "history.db").as_uri() + "?mode=ro", uri=True)
                try:
                    return database.execute("SELECT count(*) FROM history WHERE pending=1").fetchone()[0] == 0
                finally:
                    database.close()

            try:
                async with AccountConnection(url, alice), AccountConnection(url, bob) as connection:
                    await connection.request({"type": "send", "message": seal_message(
                        bob, alice.public, "offline Unicode أَهْلًا 👋")}, "stored")
                loop.add_reader(master, collect)
                child = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "dark_terminal_chat", "--login",
                    "--password-file", str(password_file), "--server", url,
                    env={**os.environ, "DARK_CHAT_HOME": str(root / "alice"),
                         "PYTHONIOENCODING": "utf-8", "PROMPT_TOOLKIT_NO_CPR": "1", "TERM": "xterm"},
                    stdin=slave, stdout=slave, stderr=slave, preexec_fn=deny_clock_nanosleep)
                os.close(slave)
                slave = None
                await until(lambda: b"New message from " in output)
                os.write(master, f"/chat {bob.id} buddy\n".encode())
                await until(lambda: "offline Unicode أَهْلًا 👋".encode() in output)
                os.write(master, b"reply from restricted terminal\n")
                await until(lambda: relay.mailbox.fetch(bob.id) is not None)
                self.assertEqual(open_message(bob, relay.mailbox.fetch(bob.id))["text"],
                                 "reply from restricted terminal")
                await until(outbox_acknowledged)
                # '/' + Enter selects /clean through the real completion UI.
                os.write(master, b"/\n")
                self.assertEqual(await asyncio.wait_for(child.wait(), 5), 0, output.decode(errors="replace"))
                collect()
                self.assertIn(b"\x1b[3J\x1b[2J\x1b[H", output)
                store = LocalStore(root / "alice", alice)
                try:
                    self.assertEqual(store.contacts()[bob.id]["alias"], "buddy")
                    self.assertEqual(len(store.messages(bob.id)), 2)
                    self.assertFalse(store.messages(pending=True))
                finally:
                    store.close()
            finally:
                if child and child.returncode is None:
                    child.kill()
                    await child.wait()
                loop.remove_reader(master)
                os.close(master)
                if slave is not None:
                    os.close(slave)
                server.close()
                await server.wait_closed()
                relay.close()


if __name__ == "__main__":
    unittest.main()
