"""Tests for the safety guards: the scope allowlist, the git check, private file
permissions, the setup check script, conversation IDs, and install.sh.

Run from the project folder:

    ./.venv/bin/python -m unittest discover -s tests -v

Everything here is local. No Slack call is made, the Keychain is never read -
`security` and `curl` are replaced by fakes - and every git repository is a
throwaway one inside a temporary directory. The install.sh tests run against a
copy of the project with a fake python and a fake HOME, so nothing is downloaded
and no real shell startup file is ever touched.
"""

import io
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

import export_conversation as ec  # noqa: E402  (needs the sys.path line above)

GOOD_SCOPES = ("identify,channels:history,channels:read,groups:history,groups:read,"
               "im:history,im:read,mpim:history,mpim:read,users:read")

# Git behaves the same on every machine only if nobody's personal git config
# (a global excludes file, say) can leak into the test repositories.
HERMETIC_GIT = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def git(cwd, *args) -> None:
    subprocess.run(["git", "-C", str(cwd), "-c", "user.name=test",
                    "-c", "user.email=test@example.com", *args],
                   check=True, capture_output=True)


def fake_bin(folder: Path, name: str, body: str) -> None:
    script = folder / name
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(0o755)


class FakeResponse(dict):
    def __init__(self, headers):
        super().__init__(ok=True, user="user-a", user_id="U0123456789",
                         team="Example", url="https://example.slack.com/")
        self.headers = headers


class FakeClient:
    def __init__(self, headers):
        self._headers = headers

    def auth_test(self):
        return FakeResponse(self._headers)


class TempDirTest(unittest.TestCase):
    """Gives each test a fresh temporary folder, removed again afterwards."""

    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        # resolve(): on macOS /tmp is a symlink to /private/tmp, and git reports
        # real paths.
        self.tmp = Path(holder.name).resolve()
        patcher = mock.patch.dict(os.environ, HERMETIC_GIT)
        patcher.start()
        self.addCleanup(patcher.stop)


# ── scopes ───────────────────────────────────────────────────────────────────

class ScopeTests(unittest.TestCase):

    def test_allowlist_matches_the_manifest(self):
        manifest = (ROOT / "slack-export-app-manifest.yaml").read_text()
        requested = set(re.findall(r"^\s+-\s+([a-z]+:[a-z.]+)", manifest, re.M))
        self.assertEqual(requested, ec.ALLOWED_SCOPES - {"identify"})

    def test_normal_token_is_accepted(self):
        with redirect_stdout(io.StringIO()):
            granted, _ = ec.assert_read_only(FakeClient({"x-oauth-scopes": GOOD_SCOPES}))
        self.assertEqual(len(granted), 10)

    def test_token_without_identify_is_accepted(self):
        scopes = GOOD_SCOPES.replace("identify,", "")
        with redirect_stdout(io.StringIO()):
            granted, _ = ec.assert_read_only(FakeClient({"x-oauth-scopes": scopes}))
        self.assertNotIn("identify", granted)

    def test_header_name_is_case_insensitive(self):
        with redirect_stdout(io.StringIO()):
            ec.assert_read_only(FakeClient({"X-OAuth-Scopes": GOOD_SCOPES}))

    def test_missing_scope_header_is_refused(self):
        for headers in ({}, {"x-oauth-scopes": ""}, {"x-oauth-scopes": " , "}):
            with self.subTest(headers=headers), self.assertRaises(SystemExit) as caught:
                ec.assert_read_only(FakeClient(headers))
            self.assertIn("cannot be verified", str(caught.exception))

    def test_a_token_slack_rejects_is_reported_not_raised(self):
        from slack_sdk.errors import SlackApiError

        class Rejecting:
            def __init__(self):
                self.calls = []

            def auth_test(self):
                self.calls.append("auth_test")
                raise SlackApiError("the request failed",
                                    {"ok": False, "error": "invalid_auth"})

            def __getattr__(self, name):   # conversations_info, conversations_history...
                raise AssertionError(f"{name} must not be called after auth_test fails")

        client = Rejecting()
        with self.assertRaises(SystemExit) as caught:
            ec.assert_read_only(client)
        message = str(caught.exception)
        self.assertIn("FATAL:", message)
        self.assertIn("invalid_auth", message)
        self.assertIn("SLACK-SETUP.md", message)
        self.assertEqual(client.calls, ["auth_test"])

    def test_a_rejection_without_an_error_code_still_reads_sensibly(self):
        from slack_sdk.errors import SlackApiError

        class Odd:
            def auth_test(self):
                raise SlackApiError("the request failed", None)

        with self.assertRaises(SystemExit) as caught:
            ec.assert_read_only(Odd())
        self.assertIn("no error reported", str(caught.exception))

    def test_unexpected_scopes_are_refused(self):
        for extra in ("chat:write", "admin", "files:read", "*"):
            with self.subTest(extra=extra), self.assertRaises(SystemExit) as caught:
                ec.assert_read_only(FakeClient({"x-oauth-scopes": f"{GOOD_SCOPES},{extra}"}))
            self.assertIn("unexpected scope", str(caught.exception))


# ── git check ────────────────────────────────────────────────────────────────

class GitCheckTests(TempDirTest):

    def repo(self, name="repo") -> Path:
        path = self.tmp / name
        path.mkdir()
        git(path, "init", "-q")
        return path

    def outputs(self, folder: Path):
        return [folder / "Foo.md", folder / "Foo.txt"]

    def test_outside_any_worktree_is_allowed(self):
        self.assertEqual(ec.committable_paths(self.outputs(self.tmp / "out")), [])

    def test_ignored_folder_is_allowed_even_before_it_exists(self):
        repo = self.repo()
        (repo / ".gitignore").write_text("transcripts/\n")
        self.assertEqual(ec.committable_paths(self.outputs(repo / "transcripts")), [])

    def test_unignored_folder_is_refused(self):
        repo = self.repo()
        paths = self.outputs(repo / "notes")
        self.assertEqual(ec.committable_paths(paths), paths)

    def test_only_the_unignored_file_is_reported(self):
        repo = self.repo()
        (repo / ".gitignore").write_text("*.md\n")
        self.assertEqual(ec.committable_paths(self.outputs(repo)), [repo / "Foo.txt"])

    def test_nested_gitignore_counts(self):
        repo = self.repo()
        (repo / "private").mkdir()
        (repo / "private" / ".gitignore").write_text("*\n")
        self.assertEqual(ec.committable_paths(self.outputs(repo / "private")), [])

    def test_already_tracked_file_is_refused_despite_ignore_rule(self):
        repo = self.repo()
        (repo / "transcripts").mkdir()
        (repo / "transcripts" / "Foo.md").write_text("committed by mistake\n")
        git(repo, "add", "-f", "transcripts/Foo.md")
        git(repo, "commit", "-q", "-m", "oops")
        (repo / ".gitignore").write_text("transcripts/\n")
        self.assertEqual(ec.committable_paths(self.outputs(repo / "transcripts")),
                         [repo / "transcripts" / "Foo.md"])

    def test_spaces_and_non_ascii_in_the_path(self):
        repo = self.repo()
        folder = repo / "Slack exports – café"
        self.assertEqual(ec.committable_paths(self.outputs(folder)), self.outputs(folder))
        (repo / ".gitignore").write_text("Slack exports – café/\n")
        self.assertEqual(ec.committable_paths(self.outputs(folder)), [])

    def test_broken_repository_fails_closed(self):
        folder = self.tmp / "broken"
        folder.mkdir()
        (folder / ".git").write_text("not a real gitdir pointer\n")
        with self.assertRaises(ec.GitCheckFailed):
            ec.committable_paths(self.outputs(folder))

    def test_missing_git_fails_closed(self):
        repo = self.repo()
        empty = self.tmp / "empty-path"
        empty.mkdir()
        with mock.patch.dict(os.environ, {"PATH": str(empty)}), \
                self.assertRaises(ec.GitCheckFailed):
            ec.committable_paths(self.outputs(repo))

    def test_git_is_not_run_outside_a_repository(self):
        with mock.patch.object(ec, "_git") as called:
            ec.committable_paths(self.outputs(self.tmp / "out"))
        called.assert_not_called()

    def test_default_exports_folder_is_allowed(self):
        paths = [ec.EXPORT_DIR / "Foo.md", ec.EXPORT_DIR / "Foo.txt",
                 ec.RAW_DIR / "Foo.raw.json"]
        self.assertEqual(ec.committable_paths(paths), [])

    def test_refusal_message(self):
        repo = self.repo()
        with self.assertRaises(SystemExit) as caught:
            ec.refuse_if_committable(self.outputs(repo / "notes"))
        self.assertIn("refusing to write inside a git repository", str(caught.exception))


class ExporterOrderTests(TempDirTest):
    """Run the real exporter to prove the git check happens before the Keychain is
    read. The fake `security` records that it was called and then fails, so no run
    can ever get as far as Slack."""

    def run_exporter(self, *args):
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        self.marker = self.tmp / "keychain-was-read"
        fake_bin(bin_dir, "security", f'touch "{self.marker}"\nexit 1\n')
        env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
        return subprocess.run(
            [sys.executable, str(TOOLS / "export_conversation.py"), "C0123456789",
             "safety-test-not-real", *args],
            capture_output=True, text=True, env=env)

    def test_out_inside_a_repository_is_refused_before_anything_happens(self):
        repo = self.tmp / "repo"
        repo.mkdir()
        git(repo, "init", "-q")
        target = repo / "transcripts"
        result = self.run_exporter("--out", str(target))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing to write inside a git repository", result.stderr)
        self.assertFalse(self.marker.exists(), "the Keychain must not be read")
        self.assertFalse(target.exists(), "a refused --out must not leave a folder")

    def test_out_outside_any_repository_gets_past_the_check(self):
        target = self.tmp / "out"
        result = self.run_exporter("--out", str(target))
        self.assertIn("no Keychain entry", result.stderr)
        self.assertTrue(self.marker.exists())
        self.assertTrue(target.is_dir())

    def test_default_exports_folder_gets_past_the_check(self):
        result = self.run_exporter()
        self.assertIn("no Keychain entry", result.stderr)
        self.assertTrue(self.marker.exists())

    def test_copy_and_the_old_no_clipboard_are_both_accepted(self):
        # --copy is new; --no-clipboard was the old opt-out and must still parse,
        # so a habit or script that types it keeps working.
        for flag in ("--copy", "--no-clipboard"):
            with self.subTest(flag=flag):
                result = self.run_exporter(flag)
                self.assertIn("no Keychain entry", result.stderr)
                self.assertNotIn("unrecognized arguments", result.stderr)
                self.marker.unlink()
                shutil.rmtree(self.tmp / "bin")

    def test_a_rejected_token_ends_with_a_message_not_a_traceback(self):
        """The real command, end to end, with Slack stubbed out entirely.

        A stub slack_sdk on PYTHONPATH shadows the real one, so this is offline and
        deterministic: auth_test raises, and every conversation-reading method
        fails the test if it is reached.
        """
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        self.marker = self.tmp / "keychain-was-read"
        fake_bin(bin_dir, "security",
                 f'touch "{self.marker}"\necho "fake-token-for-tests"\n')

        stub = self.tmp / "stub"
        (stub / "slack_sdk" / "http_retry").mkdir(parents=True)
        (stub / "slack_sdk" / "__init__.py").write_text(
            "from .errors import SlackApiError\n"
            "class WebClient:\n"
            "    def __init__(self, *a, **k):\n"
            "        self.retry_handlers = []\n"
            "    def auth_test(self):\n"
            "        raise SlackApiError('failed', {'ok': False, 'error': 'invalid_auth'})\n"
            "    def __getattr__(self, name):\n"
            "        raise AssertionError('%s must not be called' % name)\n")
        (stub / "slack_sdk" / "errors.py").write_text(
            "class SlackApiError(Exception):\n"
            "    def __init__(self, message, response=None):\n"
            "        super().__init__(message)\n"
            "        self.response = response\n")
        (stub / "slack_sdk" / "http_retry" / "__init__.py").write_text("")
        (stub / "slack_sdk" / "http_retry" / "builtin_handlers.py").write_text(
            "class RateLimitErrorRetryHandler:\n"
            "    def __init__(self, *a, **k):\n        pass\n")

        env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                   PYTHONPATH=str(stub))
        result = subprocess.run(
            [sys.executable, str(TOOLS / "export_conversation.py"), "C0123456789",
             "safety-test-not-real", "--out", str(self.tmp / "out")],
            capture_output=True, text=True, env=env)

        self.assertNotEqual(result.returncode, 0)
        output = result.stdout + result.stderr
        self.assertIn("FATAL:", output)
        self.assertIn("invalid_auth", output)
        self.assertIn("SLACK-SETUP.md", output)
        self.assertNotIn("Traceback", output)
        self.assertTrue(self.marker.exists(), "it should get as far as the Keychain")


class NameNoteTests(unittest.TestCase):
    """The summary says when the file name is not what was typed (D56)."""

    def test_nothing_typed_or_kept_exactly_says_nothing(self):
        self.assertIsNone(ec.name_note("", "C0123456789", "C0123456789"))
        self.assertIsNone(ec.name_note("Project-Planning", "Project-Planning",
                                       "C0123456789"))

    def test_a_changed_name_is_reported(self):
        self.assertEqual(ec.name_note("Lab & Field", "Lab-Field", "C0123456789"),
                         'Lab-Field  (from "Lab & Field": file names keep only '
                         'letters, digits and hyphens)')

    def test_spaces_alone_are_reported_too(self):
        self.assertIsNotNone(ec.name_note("Project Planning", "Project-Planning",
                                          "C0123456789"))

    def test_a_name_with_nothing_usable_falls_back_to_the_id(self):
        self.assertEqual(ec.name_note("!!!", "C0123456789", "C0123456789"),
                         'C0123456789  ("!!!" has no letters or digits, so the '
                         'conversation ID is used)')

    def test_the_note_matches_what_safe_stem_does(self):
        typed = "Notes: v1.2 / draft"
        self.assertTrue(ec.name_note(typed, ec.safe_stem(typed), "C0123456789")
                        .startswith(ec.safe_stem(typed) + "  "))


class ClipboardTests(unittest.TestCase):
    """Copying is opt-in: a long export pasted by surprise can freeze an app."""

    def test_nothing_is_copied_unless_asked(self):
        with mock.patch.object(ec.subprocess, "run") as run:
            self.assertFalse(ec.copy_to_clipboard("# a long export", False))
        run.assert_not_called()

    def test_copied_when_asked(self):
        with mock.patch.object(ec.subprocess, "run") as run:
            run.return_value.returncode = 0
            self.assertTrue(ec.copy_to_clipboard("# a long export", True))
        self.assertEqual(run.call_args.args[0], ["pbcopy"])
        self.assertEqual(run.call_args.kwargs["input"], "# a long export")

    def test_nothing_to_copy_is_not_copied(self):
        with mock.patch.object(ec.subprocess, "run") as run:
            self.assertFalse(ec.copy_to_clipboard(None, True))
        run.assert_not_called()


# ── file permissions ─────────────────────────────────────────────────────────

class PermissionTests(TempDirTest):

    def setUp(self):
        super().setUp()
        # Even with the most permissive umask, exports must come out owner-only.
        old = os.umask(0)
        self.addCleanup(os.umask, old)

    def test_new_file_is_owner_only(self):
        path = self.tmp / "Foo.md"
        ec.write_atomic(path, "hello\n")
        self.assertEqual(path.read_text(), "hello\n")
        self.assertEqual(mode(path), 0o600)

    def test_file_is_owner_only_from_the_moment_it_is_created(self):
        # With the follow-up chmod disabled, only the mode passed when the file is
        # created is left. It must already be owner-only: anyone who opened the
        # file during a readable window would keep that access after a later chmod.
        path = self.tmp / "Foo.md"
        with mock.patch.object(ec.os, "chmod"):
            ec.write_atomic(path, "hello\n")
        self.assertEqual(mode(path), 0o600)

    def test_rewriting_tightens_an_existing_file(self):
        path = self.tmp / "Foo.txt"
        path.write_text("old\n")
        path.chmod(0o644)
        ec.write_atomic(path, "new\n")
        self.assertEqual(path.read_text(), "new\n")
        self.assertEqual(mode(path), 0o600)

    def test_stale_partial_file_is_tightened_and_consumed(self):
        path = self.tmp / "Foo.raw.json"
        stale = self.tmp / "Foo.raw.json.partial"
        stale.write_text("half-written by an interrupted run")
        stale.chmod(0o644)
        ec.write_atomic(path, "{}")
        self.assertEqual(mode(path), 0o600)
        self.assertFalse(stale.exists())

    def test_write_is_still_atomic(self):
        path = self.tmp / "Foo.md"
        ec.write_atomic(path, "the only record\n")
        with mock.patch.object(ec.os, "replace", side_effect=OSError("disk full")), \
                self.assertRaises(OSError):
            ec.write_atomic(path, "replacement that never lands\n")
        self.assertEqual(path.read_text(), "the only record\n")

    def test_project_folders_are_made_private(self):
        fresh = self.tmp / "exports" / "raw"
        ec.make_private_dir(fresh)
        self.assertEqual(mode(fresh), 0o700)
        existing = self.tmp / "old-exports"
        existing.mkdir()
        existing.chmod(0o755)
        ec.make_private_dir(existing)
        self.assertEqual(mode(existing), 0o700)

    def test_out_folder_permissions_are_left_alone(self):
        existing = self.tmp / "Desktop"
        existing.mkdir()
        existing.chmod(0o755)
        ec.create_out_dir(existing)
        self.assertEqual(mode(existing), 0o755)


# ── setup check script ───────────────────────────────────────────────────────

class SetupScriptTests(TempDirTest):

    def run_script(self, headers: str, body: str):
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        self.curl_log = self.tmp / "curl-urls"
        fake_bin(bin_dir, "security", 'echo "fake-token-for-tests"\n')
        # Records each URL requested, then writes the canned response where the
        # script asked for the headers (-D) and the body (-o).
        fake_bin(bin_dir, "curl", """
D=""; O=""
while [ $# -gt 0 ]; do
  case "$1" in -D) D="$2"; shift ;; -o) O="$2"; shift ;; esac
  shift
done
grep '^url' >> "$FAKE_CURL_LOG"
[ -n "$D" ] && printf '%s' "$FAKE_HEADERS" > "$D"
[ -n "$O" ] && printf '%s' "$FAKE_BODY" > "$O"
exit 0
""")
        env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                   FAKE_CURL_LOG=str(self.curl_log), FAKE_HEADERS=headers,
                   FAKE_BODY=body)
        return subprocess.run(["sh", str(TOOLS / "check_token.sh")],
                              capture_output=True, text=True, env=env, cwd=self.tmp)

    OK_BODY = '{"ok": true, "team": "Example", "user": "user-a", "user_id": "U0123456789"}'

    def headers(self, scopes=None):
        lines = ["HTTP/2 200", "content-type: application/json"]
        if scopes is not None:
            lines.append(f"x-oauth-scopes: {scopes}")
        return "\r\n".join(lines) + "\r\n\r\n"

    def test_allowed_scopes_pass_and_only_auth_test_is_called(self):
        result = self.run_script(self.headers(GOOD_SCOPES), self.OK_BODY)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS", result.stdout)
        urls = self.curl_log.read_text().splitlines()
        self.assertEqual(len(urls), 1)
        self.assertIn("auth.test", urls[0])

    def test_unexpected_scope_exits_nonzero(self):
        result = self.run_script(self.headers(GOOD_SCOPES + ",chat:write"), self.OK_BODY)
        self.assertEqual(result.returncode, 1)
        self.assertIn("UNEXPECTED", result.stdout)

    def test_scope_is_never_expanded_as_a_glob(self):
        (self.tmp / "a-file-a-glob-would-match").write_text("")
        result = self.run_script(self.headers(GOOD_SCOPES + ",*"), self.OK_BODY)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("a-file-a-glob-would-match", result.stdout)

    def test_missing_scope_header_exits_nonzero(self):
        result = self.run_script(self.headers(None), self.OK_BODY)
        self.assertEqual(result.returncode, 1)
        self.assertIn("cannot be verified", result.stdout)

    def test_invalid_token_stops_before_the_scope_check(self):
        result = self.run_script(self.headers(GOOD_SCOPES),
                                 '{"ok": false, "error": "invalid_auth"}')
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("TEST 2", result.stdout)

    def test_script_never_attempts_a_write(self):
        source = (TOOLS / "check_token.sh").read_text()
        self.assertNotIn("postMessage", source)
        self.assertNotIn(".slack_uid", source)


# ── conversation IDs ─────────────────────────────────────────────────────────

class ConversationIdTests(unittest.TestCase):

    def test_plain_ids(self):
        for text in ("C0123456789", "D0123456789", "G0123456789", " C0123456789 "):
            with self.subTest(text=text):
                self.assertEqual(ec.parse_conversation_id(text), text.strip())

    def test_links(self):
        cases = [
            "https://example.slack.com/archives/C0123456789",
            "https://example.slack.com/archives/C0123456789/p1787000000000100",
            "https://example.slack.com/archives/C0123456789/p1787000000000100"
            "?thread_ts=1787000000.000100&cid=C0123456789",
            "slack://channel?team=T0123456789&id=C0123456789",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(ec.parse_conversation_id(text), "C0123456789")

    def test_channel_mention_markup(self):
        self.assertEqual(ec.parse_conversation_id("<#C0123456789|general>"),
                         "C0123456789")
        self.assertEqual(ec.parse_conversation_id("<#C0123456789>"), "C0123456789")

    def test_rejected(self):
        for text in ("slack-export", "c0123456789", "C012", "U0123456789",
                     "multimodal-object-detection", "", "   ", None):
            with self.subTest(text=text):
                self.assertIsNone(ec.parse_conversation_id(text))


# ── install.sh ───────────────────────────────────────────────────────────────

class InstallScriptTests(TempDirTest):
    """Runs the real install.sh against a copy of the project.

    The copy gets a fake .venv/bin/python, so nothing is downloaded, and HOME is
    pointed at the temporary folder, so a PATH line can never land in the real
    ~/.zshrc.
    """

    def setUp(self):
        super().setUp()
        self.project = self.tmp / "slack-channel-export"
        (self.project / "tools").mkdir(parents=True)
        for name in ("install.sh", "slack-export", "requirements.txt"):
            (self.project / name).write_bytes((ROOT / name).read_bytes())
            (self.project / name).chmod((ROOT / name).stat().st_mode & 0o777)
        (self.project / "tools" / "export_conversation.py").write_text("")
        venv_bin = self.project / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        fake_bin(venv_bin, "python",
                 'if [ "$1" = "-m" ]; then exit 0; fi\necho "PYTHON-RAN $*"\n')
        self.bin_dir = self.tmp / "target-bin"
        self.home = self.tmp / "home"
        self.home.mkdir()

    def install(self, *args, shell="sh", path=None):
        env = dict(os.environ, HOME=str(self.home),
                   PATH=path if path is not None else os.environ["PATH"])
        return subprocess.run([shell, str(self.project / "install.sh"), *args],
                              capture_output=True, text=True, env=env, cwd=self.tmp)

    def test_installs_a_working_command(self):
        result = self.install("--to", str(self.bin_dir))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        link = self.bin_dir / "slack-export"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), str(self.project / "slack-export"))
        # The command must work THROUGH the symlink: the wrapper has to follow the
        # link back to the project, not look for .venv beside the link.
        ran = subprocess.run([str(link), "C0123456789"], capture_output=True, text=True)
        self.assertIn("PYTHON-RAN", ran.stdout)
        self.assertIn("export_conversation.py C0123456789", ran.stdout)

    def test_works_when_run_with_zsh_and_from_another_folder(self):
        result = self.install("--to", str(self.bin_dir), shell="zsh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.bin_dir / "slack-export").is_symlink())

    def test_running_it_again_is_harmless(self):
        self.install("--to", str(self.bin_dir))
        result = self.install("--to", str(self.bin_dir))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.bin_dir / "slack-export").is_symlink())

    def test_adds_a_path_line_only_when_needed(self):
        zshrc = self.home / ".zshrc"
        zshrc.write_text("# existing content\n")
        # Target not on PATH -> one line added, and the user is told to open a tab.
        self.install("--to", str(self.bin_dir), path="/usr/bin:/bin")
        self.assertIn(f'export PATH="{self.bin_dir}:$PATH"', zshrc.read_text())
        self.assertIn("# existing content", zshrc.read_text())
        # Already on PATH -> nothing added.
        before = zshrc.read_text()
        self.install("--to", str(self.bin_dir), path=f"{self.bin_dir}:/usr/bin:/bin")
        self.assertEqual(zshrc.read_text(), before)

    def test_uninstall_removes_the_link_and_the_path_line(self):
        zshrc = self.home / ".zshrc"
        zshrc.write_text("# existing content\n")
        self.install("--to", str(self.bin_dir), path="/usr/bin:/bin")
        result = self.install("--to", str(self.bin_dir), "--uninstall")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.bin_dir / "slack-export").exists())
        self.assertNotIn("export PATH", zshrc.read_text())
        self.assertIn("# existing content", zshrc.read_text())

    def test_uninstall_leaves_another_project_alone(self):
        self.bin_dir.mkdir()
        someone_else = self.tmp / "someone-elses-slack-export"
        someone_else.write_text("#!/bin/sh\necho not ours\n")
        link = self.bin_dir / "slack-export"
        link.symlink_to(someone_else)
        self.install("--to", str(self.bin_dir), "--uninstall")
        self.assertTrue(link.is_symlink(), "a link to another copy must be left alone")

    def test_wrapper_says_what_is_wrong_when_the_venv_is_missing(self):
        import shutil
        shutil.rmtree(self.project / ".venv")
        ran = subprocess.run([str(self.project / "slack-export"), "C0123456789"],
                             capture_output=True, text=True)
        self.assertEqual(ran.returncode, 1)
        self.assertIn("install.sh", ran.stderr)


if __name__ == "__main__":
    unittest.main()
