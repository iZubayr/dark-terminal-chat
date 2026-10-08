"""Fast-forward the server clone only after CI and a staged install pass."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import urllib.error
import urllib.request
import venv

ROOT = Path(__file__).resolve().parents[1]


def command(*args, cwd=ROOT, capture=False):
    return subprocess.run(args, cwd=cwd, check=True, text=True,
                          stdout=subprocess.PIPE if capture else None).stdout


def ci_passed(repository, revision):
    url = (f"https://api.github.com/repos/{repository}/actions/workflows/ci.yml/runs"
           f"?branch=main&event=push&head_sha={revision}&per_page=10")
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                                  "User-Agent": "dark-terminal-chat-updater"})
    with urllib.request.urlopen(request, timeout=30) as response:
        runs = json.load(response)["workflow_runs"]
    matches = [run for run in runs if run["head_sha"] == revision]
    return bool(matches and max(matches, key=lambda run: (run["run_number"], run.get("run_attempt", 1)))["conclusion"] == "success")


def current_release(root=ROOT):
    current = root / ".deploy/current"
    if not current.exists():
        return None
    target = current.resolve(strict=True)
    target.relative_to((root / ".deploy/releases").resolve())
    if not re.fullmatch(r"[0-9a-f]{40}", target.name):
        raise ValueError("Invalid active release")
    return target


def activate(release, root=ROOT):
    current = root / ".deploy/current"
    pending = root / ".deploy/current.next"
    with contextlib.suppress(FileNotFoundError):
        pending.unlink()
    pending.symlink_to(release, target_is_directory=True)
    os.replace(pending, current)


def stage(revision, root=ROOT):
    release = root / ".deploy/releases" / revision
    # A failed previous install may be retried in the same isolated directory.
    release.mkdir(parents=True, exist_ok=True)
    source = release / "code"
    source.mkdir(exist_ok=True)
    archive = subprocess.run(["git", "archive", revision], cwd=root, check=True,
                             stdout=subprocess.PIPE).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tree:
        for member in tree.getmembers():
            if member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
                raise ValueError("Unexpected file in release archive")
            (source / member.name).resolve().relative_to(source.resolve())
        if hasattr(tarfile, "data_filter"):
            tree.extractall(source, filter="data")
        else:
            # Older Python 3.10 versions use the path/type checks above.
            tree.extractall(source)
    environment = release / "venv"
    venv.EnvBuilder(with_pip=True).create(environment)
    python = environment / "bin/python"
    # AlwaysData defaults pip to --user; this release has its own environment.
    command(str(python), "-m", "pip", "--isolated", "install", "--no-user", "--timeout", "120", str(source), cwd=source)
    command(str(python), "-m", "pip", "check", cwd=source)
    command(str(python), "-m", "unittest", "discover", "-s", "tests", "-v", cwd=source)
    return release


def update(root=ROOT):
    if os.name != "posix":
        raise ValueError("Run the updater on the Linux hosting server")
    import fcntl
    state = root / ".deploy"
    state.mkdir(exist_ok=True)
    with (state / "update.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another update is running.")
            return
        if command("git", "status", "--porcelain", cwd=root, capture=True).strip():
            raise ValueError("Local checkout has edits; update refused")
        origin = command("git", "remote", "get-url", "origin", cwd=root, capture=True).strip()
        match = re.fullmatch(r"https://github\.com/([\w.-]+/[\w.-]+?)(?:\.git)?", origin)
        if not match:
            raise ValueError("Origin must be a public GitHub HTTPS repository")
        command("git", "fetch", "origin", "main", cwd=root)
        revision = command("git", "rev-parse", "origin/main", cwd=root, capture=True).strip()
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("Invalid upstream revision")
        previous = current_release(root)
        if previous and previous.name == revision:
            print(f"Already current: {revision}")
            return
        command("git", "merge-base", "--is-ancestor", "HEAD", revision, cwd=root)
        if not ci_passed(match.group(1), revision):
            print(f"Waiting for successful GitHub tests: {revision}")
            return
        release = stage(revision, root)
        command("git", "merge", "--ff-only", revision, cwd=root)
        activate(release, root)
        print(f"Activated: {revision}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        update()
    except (OSError, ValueError, subprocess.CalledProcessError, urllib.error.URLError) as error:
        raise SystemExit(f"Update failed: {error}") from None


if __name__ == "__main__":
    main()
