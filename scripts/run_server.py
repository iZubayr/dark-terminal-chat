"""AlwaysData supervises this process; it supervises the active chat release."""
import os
from pathlib import Path
import signal
import subprocess
import threading

ROOT = Path(__file__).resolve().parents[1]


def release_target():
    release = (ROOT / ".deploy/current").resolve(strict=True)
    release.relative_to((ROOT / ".deploy/releases").resolve())
    if not (release / "venv/bin/python").is_file():
        raise ValueError("Active release has no Python runtime")
    return release


def stop_child(child):
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


def main():
    stopped = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopped.set())
    child = None
    try:
        while not stopped.is_set():
            release = release_target()
            environment = dict(os.environ, PYTHONUNBUFFERED="1")
            child = subprocess.Popen(
                [str(release / "venv/bin/python"), "-m", "dark_terminal_chat.server",
                 "--max-clients", "32"], cwd=release, env=environment,
            )
            print(f"Running release: {release.name}", flush=True)
            while not stopped.wait(2):
                status = child.poll()
                if status is not None:
                    raise SystemExit(status or 1)
                if release_target() != release:
                    break
            stop_child(child)
            child = None
    finally:
        if child is not None:
            stop_child(child)


if __name__ == "__main__":
    main()
