# Dark Terminal Chat

Plain English terminal chat. No banner, logo, animation, or forced color. Python 3.10+.

~~~sh
pip install --upgrade dark-chat
~~~

## Temporary chat

~~~sh
dark-chat --new
dark-chat --chat
~~~

The creator gets a secret invite code. Share it privately with the other person; they enter it at the prompt. Both commands use the hosted relay automatically. Each room has two slots. Temporary messages are never saved or queued. A disconnected participant has 60 seconds to reconnect. When both participants leave, or the server restarts, the old invite stops working.

## Permanent ID and offline messages

Both people register once on their own devices:

~~~sh
dark-chat --register
~~~

Choose a strong password of at least 12 characters and a display name. Registration creates private keys on this device and publishes only the public keys. Your 64-character ID is a fingerprint of those keys. Share and compare the full ID with your friend through a trusted channel. A display name alone is not proof of identity.

Open your account:

~~~sh
dark-chat --login
~~~

Enter your password. Inside the chat, select your friend and optionally save a local name:

~~~text
/chat <friend's full ID> alice
Hello.
~~~

Next time, `/chat alice` opens that conversation. The friend must have registered, but need not be online. Incoming messages from other contacts produce a notice with their full ID; select that contact to read the conversation.

Outgoing messages are saved encrypted on your device before sending. `Queued.` means the local outbox accepted the message; it is not a read receipt. The outbox retries automatically. The relay keeps encrypted messages until the recipient has saved and acknowledged them, for at most 30 days. Undelivered messages older than 30 days need to be sent again as a new message. The last 50 locally saved messages appear when opening a conversation. `/history` shows them again.

The password unlocks this device's identity; it is never sent to the server. Logging in on another computer requires your encrypted identity backup as well as the password. Use one active device per identity; multi-device history synchronization is not provided.

## Commands

Type `/` to display the command menu. Choose with the arrow keys and press Enter. `/clean` is the first option; `/` followed by Enter selects it. Typing `clean` also works.

| Command | Action |
| --- | --- |
| `/clean` or `clean` | Close the chat and clear the terminal screen and supported scrollback |
| `/exit` or Ctrl+C | Close the chat |
| `/clear` | Clear the screen and stay in the chat |
| `/help` | Show commands |
| `/who` | Show temporary-room participants |
| `/chat ID [name]` | Open a permanent conversation; optionally name the contact locally |
| `/chat name` | Open a saved contact |
| `/contacts` | List saved contact IDs and local names |
| `/id` | Show your permanent ID |
| `/history` | Show the selected conversation's recent saved messages |
| `/backup` | Create an encrypted identity backup and print its path |

`/clean` exits dark-chat and returns to the shell. It does not close the shell window or delete saved conversations, backups, terminal recordings, screenshots, or operating-system memory. Scrollback clearing depends on the terminal; it is not secure forensic erasure.

## Identity backup and recovery

While logged in, run `/backup`. Move the resulting encrypted file to a safe location on another device. It contains private keys, encrypted with your existing password. It does not contain conversation history. Anyone with both the backup and password can use your identity and decrypt messages addressed to it.

On a new device:

~~~sh
dark-chat --restore /path/to/identity-backup.json
dark-chat --login
~~~

Restore refuses to overwrite an existing identity. There is no server-side password reset or private-key recovery. For a complete local backup, close all clients and copy the whole `~/.dark-chat` directory. History is encrypted and tied to the identity. `DARK_CHAT_HOME` selects a different profile directory.

## Security model

Permanent messages use [libsodium sealed boxes through PyNaCl](https://pynacl.readthedocs.io/en/latest/public/#nacl-public-sealedbox) and Ed25519 signatures. Clients verify the sender's key fingerprint and signature, the intended recipient, and the full peer ID before accepting a retrieved public key. Authentication signs a fresh server challenge; old responses cannot log in again. Message IDs and durable acknowledgments suppress retries and replay. The server never receives private keys, account passwords, contact aliases, or decrypted message text.

The identity file uses AES-256-GCM with a random salt/nonce and scrypt (`N=131072, r=8, p=1`) to protect against offline password guessing. Local message bodies and contact names use AES-256-GCM under an independent random storage key kept inside the encrypted identity. Files are owner-only on Unix; Windows uses the profile directory's inherited access permissions, plus encryption. Names/text received from peers are stripped of terminal control characters.

Temporary rooms continue to use AES-256-GCM with keys derived from their random 32-byte invite codes. A shared invite grants room access; it is not a permanent identity. Version 3 supports version 2 temporary clients. Permanent accounts require a version 3 server.

Public Internet connections require verified WSS/TLS. Account mode refuses insecure remote/LAN connections even if `--allow-insecure` is supplied. Local loopback WS is available for development.

The relay sees IP addresses, public IDs/keys, sender/recipient relationships, timing, sizes, and delivery metadata. The local database exposes contact IDs and message metadata, although bodies and names are encrypted. There is no anonymity guarantee, forward secrecy against later compromise of a recipient's long-term private key, independent security audit, or protection from malware on an unlocked device. Sealed messages are signed, so they are not deniable. Terminal clearing does not change these limits.

## Hosting

The default relay is `wss://zubayr.alwaysdata.net/dark-chat/ws`. Advanced options remain available but are hidden from the short help: `--server`, `--name`, `--ca`, `--allow-insecure`, `--password-file`, and `--restore`. `DARK_CHAT_SERVER` overrides the relay; `DARK_CHAT_CODE` provides a temporary invite for automation. `--password-file` reads a protected local file for automation; do not put passwords directly on a command line.

~~~sh
dark-chat-server --database /persistent/private/path/mailbox.db
dark-chat --server ws://127.0.0.1:8080/ws --new
~~~

Run one relay process. Its default database is `~/.dark-chat-server/mailbox.db`, outside release directories; `DARK_CHAT_DATABASE` overrides it. Never put it in `.deploy/releases`. The server keeps ciphertext only and bounds storage to 1,000 identities, 1,000 pending messages overall, 100 pending messages per recipient, and 10,000 pending-message/delivery-receipt rows. A full mailbox returns an error while the encrypted outbox stays on the sender's device. Delivered ciphertext is deleted after acknowledgment; replay receipts expire after 30 days. Pending mail and public identities survive normal restarts and upgrades. Availability and backups of the server database remain the operator's responsibility.

[AlwaysData setup and automatic updates](docs/alwaysdata.md). [Package distribution](docs/distribution.md).

## Development

~~~sh
python -m venv .venv
python -m pip install -e . build twine
python -m unittest discover -s tests -v
python -m build
python -m twine check dist/*
~~~

Activate the virtual environment before installing. GitHub Actions tests installed wheels on Linux (Python 3.10 and 3.12) and Windows (Python 3.12). Only passing commits advance the deployment branch; AlwaysData tests the candidate again before activation. Tests cover offline delivery, database persistence, signatures, challenge replay, recipient isolation, key substitution, local encryption, CLI flows, and slash completion. A local test run does not establish live production behavior.

The earlier Node.js experiment is reference source only. The Python package is the application.
