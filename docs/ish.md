# iPhone / iSH

Use Python 3.10+ and dark-chat 3.0.1 or later. The default Alpine 3.14 / Python 3.9 environment cannot install this package.

iSH emulates a subset of Linux system calls. Installing the package alone does not prove that every feature works on a physical iPhone. Version 3.0.0 could stop after the chat prompt with `Bad system call`; `dmesg` showed `missing syscall 267` (`clock_nanosleep`). Version 3.0.1 removes the terminal output worker's timed sleep. Linux tests reject that syscall with SIGSYS while exercising real interactive login, offline delivery, sending, history and slash completion. Physical iSH testing remains a separate check.

## Existing installation

~~~sh
. ~/darkchat/bin/activate
python3 -m pip install --upgrade dark-chat
dark-chat --version
dark-chat
~~~

Enter the password chosen when registering. The existing identity remains in `~/.dark-chat`; upgrading the package does not replace it. Do not register again after closing iSH.

On subsequent launches:

~~~sh
. ~/darkchat/bin/activate
dark-chat
~~~

Version 3.1.0 opens the saved identity automatically. Inside the chat, use `/chat FULL_PEER_ID friend` once, then `/chat friend` or `/chat 1`. `/chat` lists contacts. Use `/backup` to create an encrypted identity backup, and keep it outside iSH in case the app or filesystem is removed. `/clean` clears the supported terminal screen/scrollback and exits the chat; it preserves the identity and saved history.

To launch without activating the environment each time, use `~/darkchat/bin/dark-chat`. This still reads the same identity and history.

## New environment

The official Alpine 3.23 x86 repositories supply compatible Python, cryptography and PyNaCl packages. Newer Alpine releases can still encounter unrelated unsupported iSH system calls. Import a separate x86 mini root filesystem through iSH Settings > Filesystems > Import > Boot From This Filesystem, keeping the previous filesystem available for recovery. See the [iSH import instructions](https://github.com/ish-app/ish/wiki/Install-%26-Activate-Alternate-Filesystems) and [Alpine downloads](https://alpinelinux.org/downloads/).

In the imported environment:

~~~sh
apk update
apk add python3 py3-pip py3-cryptography py3-pynacl ca-certificates
python3 --version
python3 -m venv --without-pip --system-site-packages ~/darkchat
. ~/darkchat/bin/activate
python3 -m pip install dark-chat
dark-chat
~~~

`--without-pip` skips slow pip bootstrapping under emulation. The system pip remains visible through `--system-site-packages`; always invoke it with the environment's `python3 -m pip`.

If pip needs to compile a newer cffi and reports `No such file or directory: 'cc'`, install its build requirements and retry:

~~~sh
apk add build-base python3-dev libffi-dev pkgconf
python3 -m pip install dark-chat
~~~

Compilation can take considerable time under emulation. Keep iSH in the foreground while installing and chatting. If another `Bad system call` occurs, retain the error and the output of `dmesg | tail -20`; do not reset the identity or delete the profile to troubleshoot it.
