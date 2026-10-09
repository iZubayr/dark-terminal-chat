# Distributing dark-chat

The package name and client command are both dark-chat. Python 3.10+ is required. Version 3.0.0 connects to wss://zubayr.alwaysdata.net/dark-chat/ws by default.

The public client commands are:

~~~sh
pip install dark-chat
dark-chat --register
dark-chat --login
dark-chat --new
dark-chat --chat
~~~

The creator shares the code with the peer. Names and codes are entered at the terminal prompts. The server address is optional; --server and DARK_CHAT_SERVER support other relays.

## Build and verify

~~~sh
python -m build --outdir dist/3.0.0
python -m twine check dist/3.0.0/*
python -m pip install dist/3.0.0/dark_chat-3.0.0-py3-none-any.whl
python -m pip check
python -m unittest discover -s tests -v
dark-chat --version
~~~

Keep earlier dark-terminal-chat distribution files separate. Publish only the new dark-chat distribution. The import module remains dark_terminal_chat, so a client environment should contain one of these distributions at a time.

## PyPI publishing

Configure a pending GitHub Trusted Publisher under [PyPI account publishing](https://pypi.org/manage/account/publishing/):

| Field | Value |
| --- | --- |
| PyPI project name | dark-chat |
| GitHub owner | iZubayr |
| Repository | dark-terminal-chat |
| Workflow filename | ci.yml |
| Environment | pypi |

Then run the Tests workflow on main with the publish input enabled. It builds and installs the package, runs tests on Linux with Python 3.10/3.12 and Windows with Python 3.12, and checks the installed dark-chat command. Only after all jobs pass does the publish job upload the tested Linux 3.12 artifact to PyPI using short-lived OIDC credentials. No permanent PyPI API token is stored in the repository or on AlwaysData.

After publishing, install dark-chat from the normal PyPI index in a fresh environment. Verify --new and --chat exchange messages through the default public relay. A built wheel or successful GitHub workflow without a successful PyPI publish does not establish that pip install dark-chat works.

[PyPI Trusted Publishing documentation](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/).
