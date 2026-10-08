# AlwaysData deployment

Deploy this chat as a separate site, directory, and virtual environment beside MediaHub. The applications share the account's resource allowance. The hosting supervisor limits the relay to 32 connections.

AlwaysData supports [WebSocket sites](https://help.alwaysdata.com/en/blog/2023-03-14-hold-on-to-your-socks-high-speed-data-stream-hosting-with-websockets/). External clients use HTTPS/WSS, while the relay listens on the site's internal IP/port.

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
dark-chat --server wss://ACCOUNT.alwaysdata.net/dark-chat/ws --name whiterose
~~~

The creator shares the invite code with the peer. Confirm messages arrive in both directions, a third client is refused, and MediaHub still responds normally. Local tests do not prove production behavior.

## Restarts and upgrades

Use version 2 clients with a version 2 server. The session protocol is incompatible with version 1.

Run one relay process. Rooms are in memory, so a server restart ends existing sessions and invalidates join codes. Clients handle ordinary connection drops for 60 seconds; they cannot reconstruct rooms after a server restart.

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

The bootstrap uses the approved upstream updater, so fixes to deployment itself can take effect. The updater fetches the deploy branch published by GitHub Tests, verifies that its commit belongs to main, and builds a separate release environment. It runs the tests there before fast-forwarding the clone and atomically changing .deploy/current. Pip runs in isolated mode with --no-user because AlwaysData otherwise defaults to user installs. The supervisor then restarts only the chat child process. No GitHub SSH private key, API token, or unauthenticated GitHub API call is needed on the server.

Failed tests, failed installs, local edits, or diverged commits leave the active release unchanged. A lock prevents overlapping updates. Previous release environments remain available in .deploy/releases for recovery. A successful upgrade ends any current chats because rooms exist only in memory.

For a manual update, run python3 scripts/update.py from the clone. The scheduled task uses the same path. To roll back, point .deploy/current at a known previous release; the supervisor notices the change. Disable the scheduled task while investigating so it does not immediately reapply main.
