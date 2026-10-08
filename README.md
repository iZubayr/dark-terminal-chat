# Dark Terminal Chat

A plain, English terminal chat for two people. No banner, logo, animation, or forced color. Python 3.10 or newer is required. The client and WebSocket server install from the same Python package.

Version 2 uses a new session protocol. Upgrade both the client and server together. Source: [GitHub](https://github.com/iZubayr/dark-terminal-chat). The package has not been published to PyPI.

## Install

From the GitHub release (no Git required):

~~~sh
python -m pip install https://github.com/iZubayr/dark-terminal-chat/releases/download/v2.0.0/dark_terminal_chat-2.0.0-py3-none-any.whl
~~~

From GitHub source (requires Git):

~~~sh
python -m pip install "git+https://github.com/iZubayr/dark-terminal-chat.git@v2.0.0"
~~~

From this folder on Windows:

~~~powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install .
~~~

On Linux or macOS:

~~~sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
~~~

From the built wheel, inside your Python environment:

~~~sh
python -m pip install ./dark_terminal_chat-2.0.0-py3-none-any.whl
~~~

The package provides two commands: dark-chat and dark-chat-server. You can also use python -m dark_terminal_chat and python -m dark_terminal_chat.server.

## Local use

Open three terminals with the Python environment activated.

~~~sh
# Server
dark-chat-server

# Creator
dark-chat --new --name elliot

# Peer, in another terminal
dark-chat --name whiterose
~~~

The creator receives a line starting with "Code:". The peer chooses "j" at "Create or join? [c/j]:" and pastes that code. The code is hidden while entering it. If no name is supplied, the program asks "Name:".

The interface contains only prompts, messages, and relevant connection/error notices. Example:

~~~text
Code: <invite code>
Waiting for peer.
whiterose joined.
whiterose> Hello.
elliot>
~~~

On Windows, chat.cmd and server.cmd use this folder's .venv when available.

## Session rules

- Each chat has two participant slots. A third participant is refused.
- Leaving with /exit or Ctrl+C releases the participant's slot immediately while connected.
- When both participants leave, the room is removed. Joining with its old code fails; create a new chat for a new code.
- A dropped connection reserves the same participant's slot for 60 seconds. Only that participant's in-memory resume token can reclaim it.
- The client tries to reconnect for up to 60 seconds. When a server restarts, room state is lost and a new chat is needed.
- Sending is paused while the peer is offline. The draft remains editable.
- Messages are not queued or automatically resent. If receipt isn't confirmed, the draft is kept and a short notice is shown. A lost receipt can mean the peer already saw the message; check before manually resending.
- Messages and drafts are not written to files. Old messages are not replayed after reconnecting.

## Internet use

The deployed server is available at wss://zubayr.alwaysdata.net/dark-chat/ws:

~~~sh
dark-chat --server wss://zubayr.alwaysdata.net/dark-chat/ws --new --name elliot
dark-chat --server wss://zubayr.alwaysdata.net/dark-chat/ws --name whiterose
~~~

Share the server address and code with the peer. The peer chooses j and enters the code. Use wss:// for internet connections. TLS certificates are checked; --ca ca.pem supports a private certificate authority.

You may set DARK_CHAT_SERVER to avoid repeating --server. DARK_CHAT_CODE is supported for non-interactive clients; interactive clients ask for the code instead.

For local Wi-Fi, use --host 0.0.0.0 on the server and --server ws://LAN_IP:8080/ws --allow-insecure on clients.

[AlwaysData setup beside MediaHub](docs/alwaysdata.md).

## Commands

| Command | Action |
| --- | --- |
| /help | List commands |
| /who | Show the connected participants whose names are known |
| /clear | Clear the screen |
| /exit or Ctrl+C | Leave |

## Privacy

Messages and names are encrypted on the client with AES-256-GCM. Keys are derived from a random 32-byte invite code with HKDF-SHA256. The invite code is never sent to the relay. The relay sees opaque room/session identifiers, IP addresses, message sizes, timing, and delivery-control metadata.

An invite code is shared access, and a name is not verified identity. Keep the code private. The terminal scrollback may retain displayed messages and the creator's code; /clear is not secure erasure. There is no forward secrecy, anonymity guarantee, or independent security audit.

The relay holds only session metadata and temporary network buffers. There is no message history or application-level message queue. The default server limit is 128 connections/rooms, with two participant slots per room and a 40-packet limit per connection per 10 seconds. Run one relay process; separate instances do not share rooms.

## Build and check

~~~sh
python -m pip install -e . build twine
python -m unittest discover -s tests -v
python -m build
python -m twine check dist/*
~~~

[Package distribution](docs/distribution.md). A local wheel is installable now; a short pip package-name command requires a real PyPI release.

GitHub Actions tests the installed wheel on Linux with Python 3.10/3.12 and on Windows with Python 3.12. After all jobs pass, it advances the deploy branch. AlwaysData checks that branch every five minutes and tests the candidate again before changing the running version. See [automatic updates](docs/alwaysdata.md#automatic-updates).

The earlier Node.js TCP experiment remains as reference source only. It does not support the current session protocol. The Python client/server is the application.
