import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import update as updater


class CIApprovalTests(unittest.TestCase):
    def test_latest_failed_retry_does_not_approve_a_previous_success(self):
        revision = "a" * 40
        runs = {"workflow_runs": [
            {"head_sha": revision, "run_number": 12, "run_attempt": 1, "conclusion": "success"},
            {"head_sha": revision, "run_number": 12, "run_attempt": 2, "conclusion": "failure"},
        ]}
        with patch.object(updater.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(runs).encode())):
            self.assertFalse(updater.ci_passed("owner/repo", revision))


@unittest.skipUnless(os.name == "posix", "AlwaysData deployment runs on Linux")
class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.upstream = self.folder / "upstream"
        self.root = self.folder / "clone"
        self.upstream.mkdir()
        self.git(self.upstream, "init", "-b", "main")
        self.git(self.upstream, "config", "user.name", "Test")
        self.git(self.upstream, "config", "user.email", "test@example.invalid")
        (self.upstream / ".gitignore").write_text(".deploy/\n")
        (self.upstream / "source.txt").write_text("original")
        self.git(self.upstream, "add", ".")
        self.git(self.upstream, "commit", "-m", "original")
        self.old = self.git(self.upstream, "rev-parse", "HEAD")
        self.git(self.folder, "clone", str(self.upstream), str(self.root))
        old_release = self.root / ".deploy/releases" / self.old
        old_release.mkdir(parents=True)
        updater.activate(old_release, self.root)
        (self.upstream / "source.txt").write_text("new version")
        self.git(self.upstream, "commit", "-am", "new version")
        self.new = self.git(self.upstream, "rev-parse", "HEAD")
        real_command = updater.command

        def command(*args, **kwargs):
            if args == ("git", "remote", "get-url", "origin"):
                return "https://github.com/owner/repo.git\n"
            return real_command(*args, **kwargs)

        self.command_patch = patch.object(updater, "command", side_effect=command)
        self.command_patch.start()
        self.addCleanup(self.command_patch.stop)

    @staticmethod
    def git(folder, *args):
        result = subprocess.run(["git", *args], cwd=folder, check=True, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return result.stdout.strip()

    def run_update(self):
        with contextlib.redirect_stdout(io.StringIO()):
            updater.update(self.root)

    def assert_original_active(self):
        self.assertEqual(self.git(self.root, "rev-parse", "HEAD"), self.old)
        self.assertEqual(updater.current_release(self.root).name, self.old)

    def test_failed_ci_preserves_active_release_and_checkout(self):
        with patch.object(updater, "ci_passed", return_value=False), patch.object(updater, "stage") as stage:
            self.run_update()
            stage.assert_not_called()
        self.assert_original_active()

    def test_failed_install_preserves_active_release_and_checkout(self):
        with patch.object(updater, "ci_passed", return_value=True), patch.object(updater, "stage", side_effect=OSError("failed install")):
            with self.assertRaises(OSError):
                self.run_update()
        self.assert_original_active()

    def test_local_edits_are_preserved_and_refuse_update(self):
        (self.root / "source.txt").write_text("local edit")
        with self.assertRaisesRegex(ValueError, "edits"):
            self.run_update()
        self.assert_original_active()
        self.assertEqual((self.root / "source.txt").read_text(), "local edit")

    def test_success_fast_forwards_and_switches_active_release(self):
        new_release = self.root / ".deploy/releases" / self.new
        new_release.mkdir()
        with patch.object(updater, "ci_passed", return_value=True), patch.object(updater, "stage", return_value=new_release) as stage:
            self.run_update()
            stage.assert_called_once_with(self.new, self.root)
        self.assertEqual(self.git(self.root, "rev-parse", "HEAD"), self.new)
        self.assertEqual(updater.current_release(self.root), new_release)
        self.assertEqual((self.root / "source.txt").read_text(), "new version")

    def test_diverged_checkout_is_never_reset(self):
        self.git(self.root, "config", "user.name", "Test")
        self.git(self.root, "config", "user.email", "test@example.invalid")
        (self.root / "source.txt").write_text("local commit")
        self.git(self.root, "commit", "-am", "local")
        diverged = self.git(self.root, "rev-parse", "HEAD")
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_update()
        self.assertEqual(self.git(self.root, "rev-parse", "HEAD"), diverged)
        self.assertEqual(updater.current_release(self.root).name, self.old)


if __name__ == "__main__":
    unittest.main()
