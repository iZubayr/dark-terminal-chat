# Distributing the Python package

Installing the package and hosting the chat relay are separate steps. The client connects to the WebSocket address supplied by the user.

## Wheel file

Build the release:

~~~sh
python -m pip install build twine
python -m build
python -m twine check dist/dark_terminal_chat-2.0.0*
~~~

Release files:

~~~text
dist/dark_terminal_chat-2.0.0-py3-none-any.whl
dist/dark_terminal_chat-2.0.0.tar.gz
~~~

Share the wheel with the peer. Inside a Python environment:

~~~sh
python -m pip install ./dark_terminal_chat-2.0.0-py3-none-any.whl
dark-chat --server wss://ACCOUNT.alwaysdata.net/dark-chat/ws
~~~

Python and internet access are required to install dependencies. The project's wheel is platform-independent; pip selects platform-specific dependencies where needed. Windows has been tested locally; GitHub Actions also checks Linux. Physical macOS terminals need their own validation.

To host the wheel yourself, add a separate Static files site, for example ACCOUNT.alwaysdata.net/downloads, containing the release file. Once that actual URL exists:

~~~sh
python -m pip install https://ACCOUNT.alwaysdata.net/downloads/dark_terminal_chat-2.0.0-py3-none-any.whl
~~~

The chat relay does not serve download files.

## GitHub

Source repository: [iZubayr/dark-terminal-chat](https://github.com/iZubayr/dark-terminal-chat).

~~~sh
python -m pip install "git+https://github.com/iZubayr/dark-terminal-chat.git@v2.0.0"
~~~

This method requires Git as well as Python. PyPI publication is separate from the GitHub repository.

## PyPI

Follow the [official Python packaging guide](https://packaging.python.org/en/latest/tutorials/packaging-projects/).

1. Choose an available package name. The local metadata name is dark-terminal-chat; PyPI availability has not been verified.
2. Set up a PyPI account, required 2FA, and an API token or trusted publisher.
3. Add a license and real project URLs to pyproject.toml. Update the version/name as needed and rebuild.
4. Optionally test on TestPyPI:

   ~~~sh
   python -m twine upload --repository testpypi dist/dark_terminal_chat-2.0.0*
   ~~~

5. Publish the intended version to PyPI:

   ~~~sh
   python -m twine upload dist/dark_terminal_chat-2.0.0*
   ~~~

Older local version 1 artifacts are retained in dist; upload only the intended release. Do not put API tokens in source or shell command text. Use Twine's credentials mechanism.

This release has not been uploaded. Only after a successful release under the confirmed name:

~~~sh
python -m pip install CONFIRMED_PACKAGE_NAME
dark-chat --server wss://ACCOUNT.alwaysdata.net/dark-chat/ws
~~~

[pip also supports local files, HTTPS files, and repository URLs](https://pip.pypa.io/en/stable/getting-started/).
