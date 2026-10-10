# AlwaysData deployment

Deploy this chat as a separate site, directory, and virtual environment beside MediaHub. The applications share the account's resource allowance. The hosting supervisor limits the relay to 32 connections.

AlwaysData supports [WebSocket sites](https://help.alwaysdata.com/en/blog/2023-03-14-hold-on-to-your-socks-high-speed-data-stream-hosting-with-websockets/). External clients use HTTPS/WSS, while the relay listens on the site's internal IP/port.

## Deployed instance

The zubayr account serves the chat at wss://zubayr.alwaysdata.net/dark-chat/ws. Health: https://zubayr.alwaysdata.net/dark-chat/health. Its clone lives at /home/zubayr/dark-chat. The separate Dark Terminal site is 1084884; scheduled task 34302 checks for updates every five minutes. MediaHub remains the root site.

On 2026-10-08, the hosting environment passed all 22 tests. Two clients installed from the GitHub release exchanged messages through the public WSS endpoint. The same check verified encrypted Unicode messages, refusal of a third participant, session resumption, and rejection of an old invite after both participants left. MediaHub's /health returned HTTP 200 before and after the check.

## Install

In the AlwaysData SSH terminal (replace ACCOUNT with the account name):

~~~sh
cd /home/ACCOUNT
git clone --branch deploy https://github.com/iZubayr/dark-terminal-chat.git dark-chat
cd dark-chat
python3 scripts/update.py
.deploy/current/venv/bin/dark-chat-server --version
~~~

Python 3.10+ is required. GitHub Actions advances the deploy branch only after every test job passes. The updater installs that approved commit and tests it again in its own environment. The deploy branch becomes available after the first successful Tests workflow.

## Add a site

In the AlwaysData panel, choose Web → Sites → Add a site. See [User program configuration](https://help.alwaysdata.com/en/docs/web-hosting/sites/http-servers/user-program/).

| Field | Value |
| --- | --- |
| Name | Dark Terminal |
| Addresses | ACCOUNT.alwaysdata.net/dark-chat |
| Type | User program |
| Command | python3 /home/ACCOUNT/dark-chat/scripts/run_server.py |
| Working directory | /home/ACCOUNT/dark-chat |
| Environment | IP and PORT: the values assigned to this new site in the panel |
| Trim path | Enabled |
| Idle time | 0 |

Replace ACCOUNT with your actual account name. If IP/PORT are not supplied automatically, set them in Environment or pass --host PANEL_IP --port PANEL_PORT in Command. Use the new site's assigned port.

Trim path maps external /dark-chat/ws to internal /ws, and /dark-chat/health to /health. AlwaysData handles external TLS. Start/restart the new chat site.

## Verify

~~~sh
curl --fail https://ACCOUNT.alwaysdata.net/dark-chat/health
~~~

Expected response:

~~~json
{"status":"ok"}
~~~

Then use two computers:

~~~sh
dark-chat --server wss://ACCOUNT.alwaysdata.net/dark-chat/ws --new --name elliot
dark-chat --server wss://ACCOUNT.alwaysdata.net/dark-chat/ws --chat --name whiterose
~~~

The creator shares the invite code with the peer. Confirm messages arrive in both directions, a third client is refused, and MediaHub still responds normally. Local tests do not prove production behavior.

## Restarts and upgrades

Version 3 supports version 2 temporary clients and adds permanent accounts. Permanent accounts require version 3 clients and server. Version 1 remains incompatible.

Run one relay process. Rooms are in memory, so a server restart ends existing sessions and invalidates join codes. Clients handle ordinary connection drops for 60 seconds; they cannot reconstruct rooms after a server restart.

Permanent accounts and pending encrypted messages live in `/home/ACCOUNT/.dark-chat-server/mailbox.db` by default, outside the clone and generated release environments. The default path is the same whether an old or new supervisor starts the release. `DARK_CHAT_DATABASE` can override it; always use an absolute persistent path. Do not store it under `.deploy/releases`. Back up this database separately. Local client keys/history live on the users' devices and are not part of server backups. Pending ciphertext expires after 30 days and is removed after the recipient's acknowledgment.

For a 503 response, check this site's startup log, command, environment, and IP/PORT. If health works but chat doesn't, check the public WSS URL, Trim path, TLS, and site settings affecting WebSocket connections.

## Automatic updates

Add a separate AlwaysData scheduled task:

| Field | Value |
| --- | --- |
| Type | Command |
| Frequency | Every 5 minutes |
| Command | See the bootstrap command below |
| Working directory | /home/ACCOUNT/dark-chat |
| Annotation | Dark Terminal updates |

Scheduled task command (replace ACCOUNT):

~~~sh
/bin/bash -c 'set -eu; cd /home/ACCOUNT/dark-chat; git fetch origin main deploy; mkdir -p .deploy; git show origin/deploy:scripts/update.py > .deploy/update-current.py; exec python3 .deploy/update-current.py'
~~~

The bootstrap uses the approved upstream updater, so fixes to deployment itself can take effect. The updater fetches the deploy branch published by GitHub Tests, verifies that its commit belongs to main, and builds a separate release environment. Before building, it removes inactive generated release environments inside this clone to fit the hosting disk quota; the current release is preserved. It runs the tests there before fast-forwarding the clone and atomically changing .deploy/current. Pip runs in isolated mode with --no-user and --no-cache-dir. The supervisor then restarts only the chat child process. No GitHub SSH private key, API token, or unauthenticated GitHub API call is needed on the server.

Failed tests, failed installs, local edits, or diverged commits leave the active release unchanged. A lock prevents overlapping updates. The current release and one candidate are retained; older environments can be rebuilt from Git if needed. A successful upgrade ends temporary rooms. Permanent clients reconnect, and their mailbox database survives the release switch.

For a manual update, run python3 scripts/update.py from the clone. The scheduled task uses the same path. To roll back, point .deploy/current at a known previous release; the supervisor notices the change. Disable the scheduled task while investigating so it does not immediately reapply main.
