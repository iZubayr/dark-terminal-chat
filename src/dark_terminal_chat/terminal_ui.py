"""Slash completion and terminal cleanup shared by both chat modes."""

import os
import sys

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.filters import has_completions
from prompt_toolkit.history import DummyHistory
from prompt_toolkit.key_binding import KeyBindings


class CommandCompleter(Completer):
    def __init__(self, personal=False):
        self.commands = {"/clean": "Clear terminal and exit", "/exit": "Exit chat",
                         "/help": "Show commands", "/clear": "Clear screen"}
        if personal:
            self.commands.update({"/chat": "Choose a contact or ID", "/contacts": "List contacts",
                                  "/id": "Show your ID", "/history": "Show saved messages",
                                  "/backup": "Back up your encrypted identity"})
        else:
            self.commands["/who"] = "Show participants"

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if text.startswith("/") and " " not in text:
            for command, description in self.commands.items():
                if command.startswith(text):
                    yield Completion(command, start_position=-len(text), display_meta=description)


def chat_prompt(personal=False, **kwargs):
    bindings = KeyBindings()

    @bindings.add("enter", eager=True)
    def accept(event):
        buffer = event.current_buffer
        state = buffer.complete_state
        if state and state.completions:
            buffer.apply_completion(state.current_completion or state.completions[0])
        elif buffer.text == "/":
            buffer.text = "/clean"
            buffer.cursor_position = len(buffer.text)
        buffer.validate_and_handle()

    return PromptSession(history=DummyHistory(), completer=CommandCompleter(personal),
                         complete_while_typing=True, key_bindings=bindings, **kwargs)


def clear_terminal(scrollback=False):
    if not sys.stdout.isatty():
        return
    # Use the active console output implementation for legacy Windows consoles.
    from prompt_toolkit.output import create_output
    output = create_output(stdout=sys.stdout)
    output.erase_screen()
    output.cursor_goto(0, 0)
    output.flush()
    if scrollback:
        if os.name == "nt":
            # Enable VT sequences in Windows Terminal / modern conhost.
            import ctypes
            handle = ctypes.windll.kernel32.GetStdHandle(-11)
            mode = ctypes.c_ulong()
            if ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                ctypes.windll.kernel32.SetConsoleMode(handle, mode.value | 4)
        sys.stdout.write("\x1b[3J\x1b[2J\x1b[H")
        sys.stdout.flush()
