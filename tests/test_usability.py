"""Startup, contact selection and recoverable mailbox failure regressions."""

import asyncio
import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from dark_terminal_chat.client import main
from dark_terminal_chat.identity import Identity, canonical, open_message, seal_message
from dark_terminal_chat.personal import AccountConnection, MailError, PersonalChat, SYNC_BATCH
from dark_terminal_chat.protocol import encode
from dark_terminal_chat.server import Relay
from dark_terminal_chat.storage import LocalStore, create_vault, read_vault
from dark_terminal_chat.terminal_ui import chat_prompt

PASSWORD = 'test-only-long-password'


class StartupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.terminal = MagicMock(interactive=True, clean_requested=False)

    def launch(self, arguments=()):
        with patch.dict(os.environ, {'DARK_CHAT_HOME': str(self.root), 'DARK_CHAT_CODE': ''}), \
                patch.object(sys, 'argv', ['dark-chat', *arguments]), \
                patch('dark_terminal_chat.client.Terminal', return_value=self.terminal), \
                patch('dark_terminal_chat.client.chat_stdout', contextlib.nullcontext), \
                patch.object(PersonalChat, 'run', new_callable=AsyncMock) as run:
            main()
            run.assert_awaited_once()

    def test_first_launch_reprompts_invalid_password_confirmation_and_name_then_opens_chat(self):
        self.terminal.ask.side_effect = ['short', PASSWORD, 'mismatch', PASSWORD, PASSWORD, '', 'alice']
        self.launch()
        self.assertEqual(read_vault(self.root / 'identity.json', PASSWORD).name, 'alice')
        self.assertEqual(self.terminal.ask.call_count, 7)

    def test_default_launch_retries_password_without_changing_identity_or_history(self):
        identity = create_vault(self.root, 'alice', PASSWORD)
        vault = (self.root / 'identity.json').read_bytes()
        peer = Identity.create('bob')
        with contextlib.closing(LocalStore(self.root, identity)) as store:
            packet = seal_message(identity, peer.public, 'kept history')
            store.save(packet, {'name': 'alice', 'text': 'kept history'}, 'out', True)
        self.terminal.ask.side_effect = [PASSWORD + 'wrong', PASSWORD]
        self.launch()
        self.assertEqual((self.root / 'identity.json').read_bytes(), vault)
        with contextlib.closing(LocalStore(self.root, identity)) as store:
            self.assertEqual(store.messages()[0]['text'], 'kept history')

    def test_register_on_existing_profile_opens_it_without_replacing_keys(self):
        identity = create_vault(self.root, 'alice', PASSWORD)
        self.terminal.ask.return_value = PASSWORD
        self.launch(['--register'])
        self.assertEqual(read_vault(self.root / 'identity.json', PASSWORD).id, identity.id)

    def test_three_wrong_passwords_stop_without_replacing_identity(self):
        create_vault(self.root, 'alice', PASSWORD)
        vault = (self.root / 'identity.json').read_bytes()
        self.terminal.ask.return_value = PASSWORD + 'wrong'
        with self.assertRaises(SystemExit) as error:
            self.launch()
        self.assertEqual(error.exception.code, 1)
        self.assertEqual(self.terminal.ask.call_count, 3)
        self.assertEqual((self.root / 'identity.json').read_bytes(), vault)


class ContactInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_contact_completion_needs_explicit_selection_and_refreshes_contacts(self):
        contacts = {'a' * 64: {'alias': 'friend'}, 'b' * 64: {'alias': 'fred'}}
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            session = chat_prompt(personal=True, contacts=lambda: contacts)

            async def enter(text, select=False):
                task = asyncio.create_task(session.prompt_async('> '))
                try:
                    for _ in range(200):
                        if session.app.is_running and not session.default_buffer.text:
                            break
                        await asyncio.sleep(.01)
                    pipe.send_text(text)
                    for _ in range(200):
                        if session.default_buffer.text == text and session.default_buffer.complete_state:
                            break
                        await asyncio.sleep(.01)
                    self.assertIsNotNone(session.default_buffer.complete_state)
                    if select:
                        pipe.send_text('\x1b[B')
                        for _ in range(200):
                            state = session.default_buffer.complete_state
                            if state and state.current_completion:
                                break
                            await asyncio.sleep(.01)
                        self.assertIsNotNone(state.current_completion)
                    pipe.send_text('\r')
                    return await asyncio.wait_for(task, 3)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

            self.assertEqual(await enter('/chat fr'), '/chat fr')
            self.assertEqual(await enter('/chat fr', select=True), '/chat friend')
            contacts['c' * 64] = {'alias': 'new-person'}
            self.assertEqual(await enter('/chat new', select=True), '/chat new-person')


class MailRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.alice, self.bob, self.eve = [Identity.create(name) for name in ('alice', 'bob', 'eve')]
        self.relay = Relay(database_path=self.root / 'mail.db')
        self.server = await self.relay.serve('127.0.0.1', 0)
        self.url = f'ws://127.0.0.1:{self.server.sockets[0].getsockname()[1]}/ws'
        self.store = LocalStore(self.root / 'alice', self.alice)
        self.terminal = MagicMock(interactive=False, clean_requested=False)
        self.chat = PersonalChat(self.terminal, self.store)
        for identity in (self.alice, self.bob, self.eve):
            self.relay.mailbox.register(identity.public)

    async def asyncTearDown(self):
        self.store.close()
        self.server.close()
        await self.server.wait_closed()
        self.relay.close()

    def queue(self, peer, text):
        packet = seal_message(self.alice, peer.public, text)
        self.store.save(packet, {'name': 'alice', 'text': text}, 'out', True)
        return packet

    async def test_numbered_contacts_keep_order_across_restart_and_select_offline(self):
        self.store.add_contact(self.bob.public, 'buddy')
        self.store.add_contact(self.eve.public, 'another friend')
        self.store.close()
        self.store = LocalStore(self.root / 'alice', self.alice)
        self.chat = PersonalChat(self.terminal, self.store)
        await self.chat.handle('/chat')
        await self.chat.handle('/chat 2')
        self.assertEqual(self.chat.peer, self.eve.id)
        await self.chat.handle('/chat buddy')
        self.assertEqual(self.chat.peer, self.bob.id)
        with self.assertRaises(ValueError):
            self.store.rename_contact(self.bob.id, '2')
        # Preserve access to numeric aliases created by older clients.
        self.store.add_contact(Identity.create('old').public, '9')
        await self.chat.select('9')
        self.assertEqual(self.store.contacts()[self.chat.peer]['alias'], '9')

    async def test_disconnect_during_lookup_does_not_close_input_or_change_peer(self):
        self.store.add_contact(self.bob.public, 'buddy')
        self.chat.peer = self.bob.id
        self.chat.connection = MagicMock(request=AsyncMock(side_effect=ConnectionError('disconnected')))
        with patch('sys.stdin', io.StringIO(f'/chat {self.eve.id}\n/clean\n')):
            await asyncio.wait_for(self.chat.send_input(), 3)
        self.assertTrue(self.terminal.clean_requested)
        self.assertEqual(self.chat.peer, self.bob.id)
        self.assertTrue(any('Try /chat again' in call.args[0] for call in self.terminal.say.call_args_list))

    async def test_outbox_sends_oldest_first_across_batches(self):
        for number in range(26):
            self.queue(self.bob, f'message {number}')
        async with AccountConnection(self.url, self.alice) as connection:
            self.chat.connection = connection
            for _ in range((26 + SYNC_BATCH - 1) // SYNC_BATCH):
                await self.chat.synchronize()
        received = []
        while packet := self.relay.mailbox.fetch(self.bob.id):
            received.append(open_message(self.bob, packet)['text'])
            self.relay.mailbox.acknowledge(self.bob.id, packet['id'])
        self.assertEqual(received, [f'message {n}' for n in range(26)])
        self.assertFalse(self.store.messages(pending=True))

    async def test_full_mailbox_does_not_starve_another_contact_or_incoming_mail(self):
        self.relay.mailbox.per_user = 1
        self.relay.mailbox.send(self.alice.id, seal_message(self.alice, self.bob.public, 'already full'))
        for number in range(25):
            self.queue(self.bob, f'blocked {number}')
        self.queue(self.eve, 'other contact')
        self.relay.mailbox.send(self.bob.id, seal_message(self.bob, self.alice.public, 'incoming'))
        async with AccountConnection(self.url, self.alice) as connection:
            self.chat.connection = connection
            await self.chat.synchronize()
        self.assertEqual(open_message(self.eve, self.relay.mailbox.fetch(self.eve.id))['text'], 'other contact')
        self.assertEqual(len(self.store.messages(pending=True)), 25)
        self.assertIn('incoming', [item['text'] for item in self.store.messages()])

    async def test_signed_unreadable_mail_cannot_block_a_valid_following_message(self):
        packet = seal_message(self.bob, self.alice.public, 'broken')
        packet['body'] = encode(b'X' * 64)
        packet['signature'] = self.bob.sign(b'dark-chat-message-v1:' + canonical(
            {key: value for key, value in packet.items() if key != 'signature'}))
        self.relay.mailbox.send(self.bob.id, packet)
        self.relay.mailbox.send(self.bob.id, seal_message(self.bob, self.alice.public, 'still delivered'))
        async with AccountConnection(self.url, self.alice) as connection:
            self.chat.connection = connection
            await self.chat.synchronize()
        self.assertEqual([item['text'] for item in self.store.messages()], ['still delivered'])
        self.assertIsNone(self.relay.mailbox.fetch(self.alice.id))
        self.assertTrue(any('Unreadable message' in call.args[0] for call in self.terminal.say.call_args_list))

    async def test_expired_outbox_keeps_text_without_blocking_new_messages_to_same_peer(self):
        with patch('dark_terminal_chat.identity.time.time', return_value=1):
            self.queue(self.bob, 'old unsent message')
        self.queue(self.bob, 'new message')
        async with AccountConnection(self.url, self.alice) as connection:
            self.chat.connection = connection
            await self.chat.synchronize()
        self.assertEqual(open_message(self.bob, self.relay.mailbox.fetch(self.bob.id))['text'], 'new message')
        history = self.store.messages(self.bob.id)
        self.assertEqual(history[0]['text'], 'old unsent message')
        self.assertTrue(history[0]['expired'])
        self.assertFalse(self.store.messages(pending=True))
        self.store.add_contact(self.bob.public, 'buddy')
        await self.chat.select('buddy')
        self.assertTrue(any('[unconfirmed: expired]' in call.args[0] for call in self.terminal.say.call_args_list))

    async def test_many_blocked_contacts_yield_to_others_on_the_next_sync(self):
        # Five different full mailboxes exhaust one sync's request budget.
        self.relay.mailbox.per_user = 1
        for number in range(SYNC_BATCH):
            peer = Identity.create(str(number))
            self.relay.mailbox.register(peer.public)
            self.relay.mailbox.send(self.alice.id, seal_message(self.alice, peer.public, 'full'))
            self.queue(peer, 'blocked')
        self.queue(self.bob, 'unblocked contact')
        async with AccountConnection(self.url, self.alice) as connection:
            self.chat.connection = connection
            await self.chat.synchronize()
            await self.chat.synchronize()
        self.assertEqual(open_message(self.bob, self.relay.mailbox.fetch(self.bob.id))['text'], 'unblocked contact')

    async def test_substituted_or_foreign_frame_is_never_acknowledged(self):
        foreign = seal_message(self.bob, self.eve.public, 'foreign')
        tampered = seal_message(self.bob, self.alice.public, 'tampered')
        tampered['created'] -= 1
        for packet in (foreign, tampered):
            connection = MagicMock(request=AsyncMock(return_value={'message': packet}))
            self.chat.connection = connection
            with self.assertRaises(ValueError):
                await self.chat.synchronize()
            connection.request.assert_awaited_once_with({'type': 'fetch'}, 'mail')
        self.assertFalse(self.store.messages())


if __name__ == '__main__':
    unittest.main()
