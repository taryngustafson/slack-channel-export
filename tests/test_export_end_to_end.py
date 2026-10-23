"""End-to-end tests of a whole export, against a fake Slack.

Run from the project folder:

    ./.venv/bin/python -m unittest discover -s tests -v

These run the real `slack-export` command - argument parsing, the checks made
before Slack is contacted, fetching, merging, rendering, writing and the
conversation index - with only three things swapped out: the Slack client is a
FakeSlack holding invented conversations, every folder the tool writes to is a
temporary one, and the Keychain is never read. No network, no real token.

Every ID, name and message here is invented.
"""

import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import export_conversation as ec  # noqa: E402  (needs the sys.path line above)
from slack_sdk.errors import SlackApiError  # noqa: E402

GOOD_SCOPES = ("identify,channels:history,channels:read,groups:history,groups:read,"
               "im:history,im:read,mpim:history,mpim:read,users:read")
HERMETIC_GIT = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}

TEAM = {"ok": True, "user": "user-a", "user_id": "U0000000001", "team": "Example",
        "team_id": "T0000000001", "url": "https://example.slack.com/"}
PEOPLE = {"U0000000001": "Alice", "U0000000002": "Bob"}


class FakeResponse(dict):
    """What slack_sdk hands back: a dict that also carries the HTTP headers."""

    def __init__(self, data, headers=None):
        super().__init__(data)
        self.headers = headers or {}


class FakeSlack:
    """A stand-in for slack_sdk.WebClient, holding a few invented conversations.

    Only the read calls the exporter makes exist. Each one returns fresh copies,
    as Slack does, so the exporter can never reach back into the fake's own data,
    and every call is logged so a test can count them. Pages are tiny, so paging
    is exercised by even a short conversation.
    """

    PAGE = 2

    def __init__(self):
        self.retry_handlers = []
        self.conversations = {}
        self.calls = []

    # -- building the invented workspace --------------------------------------

    def add_channel(self, channel_id, name):
        self.conversations[channel_id] = {
            "info": {"id": channel_id, "name": name, "is_channel": True,
                     "is_private": False},
            "messages": {}, "replies": {}}

    def post(self, channel_id, ts, text, user="U0000000001"):
        self.conversations[channel_id]["messages"][ts] = {
            "type": "message", "ts": ts, "user": user, "text": text}

    def reply(self, channel_id, parent_ts, ts, text, user="U0000000002"):
        self.conversations[channel_id]["replies"].setdefault(parent_ts, {})[ts] = {
            "type": "message", "ts": ts, "user": user, "text": text,
            "thread_ts": parent_ts}

    # -- the Slack API ---------------------------------------------------------

    def _page(self, items, cursor):
        start = int(cursor or 0)
        page = items[start:start + self.PAGE]
        more = start + self.PAGE < len(items)
        return {"ok": True, "messages": copy.deepcopy(page),
                "response_metadata": {"next_cursor": str(start + self.PAGE)
                                      if more else ""}}

    def _conversation(self, channel):
        if channel not in self.conversations:
            raise SlackApiError("failed", {"ok": False, "error": "channel_not_found"})
        return self.conversations[channel]

    def auth_test(self):
        self.calls.append(("auth.test", None))
        return FakeResponse(TEAM, {"x-oauth-scopes": GOOD_SCOPES})

    def conversations_info(self, channel):
        self.calls.append(("conversations.info", channel))
        return {"ok": True,
                "channel": copy.deepcopy(self._conversation(channel)["info"])}

    def conversations_history(self, channel, limit=None, cursor=None):
        self.calls.append(("conversations.history", channel))
        convo = self._conversation(channel)
        top = []
        for ts in sorted(convo["messages"], key=float, reverse=True):
            message = dict(convo["messages"][ts])
            replies = convo["replies"].get(ts)
            if replies:
                message["thread_ts"] = ts
                message["reply_count"] = len(replies)
                message["latest_reply"] = max(replies, key=float)
            top.append(message)
        return self._page(top, cursor)

    def conversations_replies(self, channel, ts, limit=None, cursor=None):
        self.calls.append(("conversations.replies", (channel, ts)))
        convo = self._conversation(channel)
        thread = [convo["messages"][ts]] + [
            convo["replies"][ts][r] for r in sorted(convo["replies"].get(ts, {}),
                                                    key=float)]
        return self._page(thread, cursor)

    def users_info(self, user):
        self.calls.append(("users.info", user))
        return {"ok": True, "user": {"name": user,
                                     "profile": {"display_name": PEOPLE.get(user, user)}}}

    def count(self, method):
        return sum(1 for name, _ in self.calls if name == method)


class ExportTest(unittest.TestCase):
    """Runs the real command in a temporary folder against a FakeSlack."""

    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name).resolve()
        self.exports = self.tmp / "exports"
        self.index = self.tmp / "config" / "channels.json"
        for patcher in (
                mock.patch.dict(os.environ, HERMETIC_GIT),
                mock.patch.object(ec, "EXPORT_DIR", self.exports),
                mock.patch.object(ec, "RAW_DIR", self.exports / "raw"),
                mock.patch.object(ec, "DIRECTORY_PATH", self.index),
                # Its display_path check expects the real exports/ folder.
                mock.patch.object(ec, "_self_test", lambda: None)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.slack = FakeSlack()
        self.slack.add_channel("C0000000001", "planning")
        self.slack.post("C0000000001", "1000.000100", "first message")
        self.slack.post("C0000000001", "1000.000200", "second message")

    def run_export(self, *argv):
        """The command as typed. Returns (exit code, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["slack-export", *argv]), \
                mock.patch.object(ec, "connect", return_value=self.slack), \
                redirect_stdout(out), redirect_stderr(err):
            try:
                ec.main()
                code = 0
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def read(self, name):
        return (self.exports / name).read_text()

    def archive(self, stem):
        return json.loads((self.exports / "raw" / f"{stem}.raw.json").read_text())

    def last_exported(self, channel_id):
        store = json.loads(self.index.read_text())
        entry = store["workspaces"]["T0000000001"]["channels"][channel_id]
        return entry.get("exports", {})


# ── whole exports ────────────────────────────────────────────────────────────

class ExportTests(ExportTest):

    def test_a_single_conversation_is_exported_in_full(self):
        self.slack.reply("C0000000001", "1000.000100", "1000.000150", "a reply")
        code, out, err = self.run_export("C0000000001")
        self.assertEqual(code, 0, err)
        for name in ("C0000000001.txt", "C0000000001.md"):
            text = self.read(name)
            for line in ("first message", "a reply", "second message"):
                self.assertIn(line, text, name)
        held = self.archive("C0000000001")
        self.assertEqual(held["conversation_id"], "C0000000001")
        self.assertEqual(held["total_message_count"], 3)
        self.assertIn("C0000000001", self.last_exported("C0000000001"))

    def test_several_conversations_each_get_their_own_files(self):
        self.slack.add_channel("C0000000002", "research")
        self.slack.post("C0000000002", "2000.000100", "research message")
        code, out, err = self.run_export("C0000000001", "Planning", "C0000000002")
        self.assertEqual(code, 0, err)
        self.assertIn("first message", self.read("Planning.txt"))
        self.assertNotIn("research message", self.read("Planning.txt"))
        self.assertIn("research message", self.read("C0000000002.md"))
        self.assertIn("DONE: 2 of 2 exported", out)
        self.assertEqual(self.slack.count("auth.test"), 1, "one pre-flight for all")

    def test_one_refused_conversation_does_not_stop_the_others(self):
        code, out, err = self.run_export("C0000000999", "C0000000001")
        self.assertEqual(code, 1)
        self.assertIn("C0000000999  FAILED: conversations.info failed: "
                      "channel_not_found", out)
        self.assertIn("first message", self.read("C0000000001.txt"))
        self.assertFalse((self.exports / "C0000000999.txt").exists())
        self.assertFalse((self.exports / "raw" / "C0000000999.raw.json").exists())


    def test_a_top_up_appends_only_what_is_new(self):
        self.run_export("C0000000001")
        self.slack.post("C0000000001", "1000.000300", "third message")
        code, out, err = self.run_export("C0000000001")
        self.assertEqual(code, 0, err)
        for name in ("C0000000001.txt", "C0000000001.md"):
            text = self.read(name)
            self.assertEqual(text.count("first message"), 1, name)
            self.assertEqual(text.count("third message"), 1, name)
        self.assertIn("appended (+1)", out)
        held = self.archive("C0000000001")
        self.assertEqual(held["top_level_count"], 3)
        self.assertEqual(held["pending"], {})

    def test_a_quiet_conversation_costs_no_thread_calls_and_changes_nothing(self):
        self.slack.reply("C0000000001", "1000.000100", "1000.000150", "a reply")
        self.run_export("C0000000001")
        before = {n: self.read(n) for n in ("C0000000001.txt", "C0000000001.md")}
        self.slack.calls.clear()
        code, out, err = self.run_export("C0000000001")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.slack.count("conversations.replies"), 0)
        self.assertEqual(out.count("already up to date"), 2)
        # The header's "last updated" line moves; nothing else may.
        for name, text in before.items():
            self.assertEqual(self.read(name).split("a reply")[1:],
                             text.split("a reply")[1:], name)


# ── refused before Slack is contacted ────────────────────────────────────────

class RefusedTargetTests(ExportTest):
    """Every one of these must stop before the first Slack call and write nothing."""

    def assert_refused(self, argv, message):
        code, out, err = self.run_export(*argv)
        self.assertNotEqual(code, 0)
        self.assertIn(message, str(code) + err)
        self.assertEqual(self.slack.calls, [], "Slack must not be contacted")
        self.assertFalse(any(self.exports.glob("**/*.raw.json")),
                         "no archive may be written")

    def test_names_differing_only_in_capitals_are_refused(self):
        self.slack.add_channel("C0000000002", "research")
        self.assert_refused(("C0000000001", "Notes", "C0000000002", "notes"),
                            'under the file names "Notes" and "notes"')

    def test_names_that_become_the_same_file_name_are_refused(self):
        self.slack.add_channel("C0000000002", "research")
        for argv in (("C0000000001", "Lab", "Notes", "C0000000002", "Lab-Notes"),
                     ("C0000000001", "A&B", "C0000000002", "a b")):
            with self.subTest(argv=argv):
                self.assert_refused(argv, "would both be saved")

    def test_a_name_too_long_for_a_file_is_compared_after_cutting(self):
        self.slack.add_channel("C0000000002", "research")
        long = "x" * ec.NAME_MAX
        self.assert_refused(("C0000000001", long + "a", "C0000000002", long + "b"),
                            "would both be saved")

    def test_a_name_whose_archive_holds_another_conversation_is_refused(self):
        self.run_export("C0000000001", "Notes")
        self.slack.add_channel("C0000000002", "research")
        self.slack.calls.clear()
        code, out, err = self.run_export("C0000000002", "Notes")
        self.assertNotEqual(code, 0)
        self.assertIn("already holds a different conversation", str(code))
        self.assertEqual(self.slack.calls, [])
        self.assertEqual(self.archive("Notes")["conversation_id"], "C0000000001")

    def test_out_inside_a_git_repository_is_refused(self):
        repo = self.tmp / "repo"
        repo.mkdir()
        import subprocess
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        self.assert_refused(("C0000000001", "--out", str(repo / "transcripts")),
                            "refusing to write inside a git repository")
        self.assertFalse((repo / "transcripts").exists())

    def test_a_first_word_that_is_not_an_id_is_refused(self):
        self.assert_refused(("Planning", "C0000000001"),
                            "is not a Slack conversation ID")


@unittest.skipUnless(
    os.path.exists(str(Path(tempfile.gettempdir()).resolve()).swapcase()),
    "needs a disk that ignores capitals, as macOS normally does")
class CaseInsensitiveDiskTests(ExportTest):
    """On a disk where Notes.md and notes.md are one file."""

    def test_the_same_conversation_under_other_capitals_is_topped_up(self):
        self.run_export("C0000000001", "Notes")
        self.slack.post("C0000000001", "1000.000300", "third message")
        code, out, err = self.run_export("C0000000001", "notes")
        self.assertEqual(code, 0, err)
        self.assertNotIn("SKIPPED", out)
        self.assertEqual(self.read("Notes.txt").count("third message"), 1)
        self.assertEqual(len(self.archive("Notes")["rendered"]), 2,
                         "one record per file, not one per spelling")

    def test_another_conversation_under_other_capitals_is_refused(self):
        self.run_export("C0000000001", "Notes")
        self.slack.add_channel("C0000000002", "research")
        self.slack.calls.clear()
        code, out, err = self.run_export("C0000000002", "notes")
        self.assertIn("already holds a different conversation", str(code))
        self.assertEqual(self.slack.calls, [])
        self.assertEqual(self.archive("Notes")["conversation_id"], "C0000000001")


# ── where flags can go ───────────────────────────────────────────────────────

class ArgumentOrderTests(ExportTest):

    def setUp(self):
        super().setUp()
        self.slack.add_channel("C0000000002", "research")
        self.slack.post("C0000000002", "2000.000100", "research message")
        self.out = self.tmp / "desk"

    def test_out_works_before_between_and_after_the_targets(self):
        arrangements = (
            ("--out", "{out}", "C0000000001", "Lab", "C0000000002", "Meeting"),
            ("C0000000001", "Lab", "--out", "{out}", "C0000000002", "Meeting"),
            ("C0000000001", "Lab", "C0000000002", "Meeting", "--out", "{out}"),
            ("C0000000001", "--out", "{out}", "Lab", "C0000000002", "Meeting"),
        )
        for index, arrangement in enumerate(arrangements):
            with self.subTest(argv=arrangement):
                out = self.out / str(index)
                argv = [str(out) if a == "{out}" else a for a in arrangement]
                code, stdout, err = self.run_export(*argv)
                self.assertEqual(code, 0, err)
                self.assertIn("first message", (out / "Lab.md").read_text())
                self.assertIn("research message", (out / "Meeting.md").read_text())

    def test_a_flag_between_an_id_and_its_name_keeps_the_name_together(self):
        code, out, err = self.run_export("C0000000001", "Project", "--no-threads",
                                         "Planning")
        self.assertEqual(code, 0, err)
        self.assertTrue((self.exports / "Project-Planning.md").exists())

    def test_an_unknown_flag_is_still_an_error(self):
        code, out, err = self.run_export("C0000000001", "--bogus")
        self.assertEqual(code, 2)
        self.assertIn("--bogus", err)
        self.assertEqual(self.slack.calls, [])


# ── threads skipped once, then wanted ────────────────────────────────────────

class ThreadBackfillTests(ExportTest):
    """An export made with --no-threads, then topped up normally, must end up with
    the replies the first run skipped - in the archive AND in the readable files."""

    def setUp(self):
        super().setUp()
        self.slack.reply("C0000000001", "1000.000100", "1000.000150", "old reply")

    def parent(self, ts="1000.000100"):
        return next(m for m in self.archive("C0000000001")["messages"]
                    if m["ts"] == ts)

    def test_replies_skipped_by_no_threads_arrive_on_a_normal_run(self):
        self.run_export("C0000000001", "--no-threads")
        self.assertNotIn("old reply", self.read("C0000000001.txt"))
        self.assertFalse(self.archive("C0000000001")["threads_expanded"])
        # A later top-level message moves the archive's cutoff past the old reply.
        self.slack.post("C0000000001", "1000.000300", "later message")

        code, out, err = self.run_export("C0000000001")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.slack.count("conversations.replies"), 1)
        self.assertEqual([r["text"] for r in self.parent()["thread_replies"]],
                         ["old reply"])
        self.assertTrue(self.archive("C0000000001")["threads_expanded"])
        for name in ("C0000000001.txt", "C0000000001.md"):
            self.assertEqual(self.read(name).count("old reply"), 1, name)
            self.assertEqual(self.read(name).count("later message"), 1, name)

    def test_once_caught_up_later_runs_fetch_and_add_nothing_more(self):
        self.run_export("C0000000001", "--no-threads")
        self.slack.post("C0000000001", "1000.000300", "later message")
        self.run_export("C0000000001")
        self.slack.calls.clear()
        for _ in range(2):
            code, out, err = self.run_export("C0000000001")
            self.assertEqual(code, 0, err)
        self.assertEqual(self.slack.count("conversations.replies"), 0)
        for name in ("C0000000001.txt", "C0000000001.md"):
            self.assertEqual(self.read(name).count("old reply"), 1, name)

    def test_a_thread_started_during_no_threads_top_ups_is_caught_up_too(self):
        self.run_export("C0000000001", "--no-threads")
        self.slack.post("C0000000001", "1000.000300", "new parent")
        self.slack.reply("C0000000001", "1000.000300", "1000.000350", "new reply")
        self.run_export("C0000000001", "--no-threads")
        self.slack.post("C0000000001", "1000.000400", "latest message")
        code, out, err = self.run_export("C0000000001")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.slack.count("conversations.replies"), 2)
        for name in ("C0000000001.txt", "C0000000001.md"):
            for text in ("old reply", "new reply", "latest message"):
                self.assertEqual(self.read(name).count(text), 1, (name, text))

    def test_no_threads_again_does_not_fetch_them(self):
        self.run_export("C0000000001", "--no-threads")
        self.slack.post("C0000000001", "1000.000300", "later message")
        self.run_export("C0000000001", "--no-threads")
        self.assertEqual(self.slack.count("conversations.replies"), 0)
        self.assertNotIn("old reply", self.read("C0000000001.txt"))

    def test_a_failed_thread_fetch_is_not_marked_as_fetched(self):
        self.run_export("C0000000001", "--no-threads")
        self.slack.post("C0000000001", "1000.000300", "later message")
        with mock.patch.object(FakeSlack, "conversations_replies",
                               side_effect=SlackApiError(
                                   "failed", {"ok": False, "error": "ratelimited"})):
            code, out, err = self.run_export("C0000000001")
        self.assertEqual(code, "FATAL: ratelimited")
        self.assertNotIn("thread_replies", self.parent())
        code, out, err = self.run_export("C0000000001")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.read("C0000000001.txt").count("old reply"), 1)


# ── interrupted writes ───────────────────────────────────────────────────────

class InterruptedWriteTests(ExportTest):
    """A run that dies partway through writing must leave things a retry can
    finish: nothing duplicated, nothing lost, nobody else's file touched.

    A top-up writes, in order: the archive (with what each file will become),
    the .txt, the .md, then the archive again. Each test fails one of them."""

    FILES = ("C0000000001.txt", "C0000000001.md")

    def failing_writes(self, fail_on):
        """write_atomic, except that the writes numbered in `fail_on` (1 = the
        first write of the run) raise as a full disk would."""
        real = ec.write_atomic
        count = [0]

        def write(path, text):
            count[0] += 1
            if count[0] in fail_on:
                raise OSError(28, "No space left on device")
            real(path, text)
        return mock.patch.object(ec, "write_atomic", side_effect=write)

    def assert_whole(self, *texts):
        for name in self.FILES:
            for text in texts:
                self.assertEqual(self.read(name).count(text), 1, (name, text))
        held = self.archive("C0000000001")
        self.assertEqual(held["pending"], {})
        self.assertEqual(set(Path(k).name for k in held["rendered"]), set(self.FILES))
        self.assertEqual(held["rendered"][str(self.exports / self.FILES[0])],
                         held["last_ts"])

    def interrupted_top_up(self, fail_on):
        self.run_export("C0000000001")
        self.slack.post("C0000000001", "1000.000300", "new message")
        with self.failing_writes(fail_on):
            code, out, err = self.run_export("C0000000001")
        self.assertIn("could not write", str(code))
        self.assertIn("Run the same command again", str(code))
        return code

    def test_failure_writing_the_archive_before_any_file(self):
        self.interrupted_top_up({1})
        self.assertEqual(self.read("C0000000001.txt").count("new message"), 0)
        self.assertEqual(self.run_export("C0000000001")[0], 0)
        self.assert_whole("first message", "new message")

    def test_failure_writing_the_text_file(self):
        self.interrupted_top_up({2})
        self.assertEqual(self.run_export("C0000000001")[0], 0)
        self.assert_whole("first message", "new message")

    def test_failure_after_the_text_file_was_updated(self):
        self.interrupted_top_up({3})
        self.assertEqual(self.read("C0000000001.txt").count("new message"), 1)
        self.assertEqual(self.read("C0000000001.md").count("new message"), 0)
        self.assertEqual(self.run_export("C0000000001")[0], 0)
        self.assert_whole("first message", "new message")

    def test_failure_after_both_files_but_before_the_archive_caught_up(self):
        self.interrupted_top_up({4})
        self.assertEqual(self.run_export("C0000000001")[0], 0)
        self.assert_whole("first message", "new message")

    def test_a_retry_that_fails_too_is_still_recoverable(self):
        self.interrupted_top_up({3})
        self.slack.post("C0000000001", "1000.000400", "newer message")
        with self.failing_writes({2}):
            code, out, err = self.run_export("C0000000001")
        self.assertIn("could not write", str(code))
        self.assertEqual(self.run_export("C0000000001")[0], 0)
        self.assert_whole("first message", "new message", "newer message")

    def test_a_first_export_interrupted_after_the_text_file(self):
        with self.failing_writes({3}):
            self.run_export("C0000000001")
        self.assertEqual(self.run_export("C0000000001")[0], 0)
        self.assert_whole("first message", "second message")

    def test_backfilled_replies_survive_an_interruption(self):
        self.slack.reply("C0000000001", "1000.000100", "1000.000150", "old reply")
        self.run_export("C0000000001", "--no-threads")
        self.slack.post("C0000000001", "1000.000300", "new message")
        with self.failing_writes({3}):
            self.run_export("C0000000001")
        self.assertEqual(self.run_export("C0000000001")[0], 0)
        self.assert_whole("old reply", "new message")

    def test_a_file_edited_after_a_failure_is_not_mistaken_for_written(self):
        self.interrupted_top_up({3})
        text = self.exports / "C0000000001.md"
        text.write_text(text.read_text() + "\nmy own note\n")
        self.assertEqual(self.run_export("C0000000001")[0], 0)
        self.assertIn("my own note", self.read("C0000000001.md"))
        self.assert_whole("new message")


# ── what the run reports, and LAST EXPORTED ──────────────────────────────────

class StatusTests(ExportTest):

    def test_a_file_the_tool_did_not_write_is_left_alone(self):
        self.exports.mkdir(parents=True)
        mine = self.exports / "C0000000001.md"
        mine.write_text("somebody else's notes\n")
        code, out, err = self.run_export("C0000000001")
        self.assertEqual(code, 0, err)
        self.assertEqual(mine.read_text(), "somebody else's notes\n")
        self.assertIn("SKIPPED - no record of writing this file", out)
        self.assertIn("LAST EXPORTED not updated", out)
        self.assertEqual(self.last_exported("C0000000001"), {})
        self.assertIn("first message", self.read("C0000000001.txt"))

    def test_last_exported_moves_only_when_every_file_is_current(self):
        self.run_export("C0000000001")
        # A time no run could write, so any update is visible.
        store = json.loads(self.index.read_text())
        exports = store["workspaces"]["T0000000001"]["channels"]["C0000000001"]["exports"]
        exports["C0000000001"]["last_export_utc"] = "2000-01-01T00:00:00Z"
        self.index.write_text(json.dumps(store))

        (self.exports / "C0000000001.md").rename(self.exports / "moved.md")
        (self.exports / "C0000000001.md").write_text("not ours\n")
        self.slack.post("C0000000001", "1000.000300", "third message")
        code, out, err = self.run_export("C0000000001")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.read("C0000000001.txt").count("third message"), 1)
        self.assertEqual(self.last_exported("C0000000001")["C0000000001"],
                         {"last_export_utc": "2000-01-01T00:00:00Z"})

        # Once every file is current again, it moves.
        (self.exports / "C0000000001.md").unlink()
        code, out, err = self.run_export("C0000000001")
        self.assertEqual(code, 0, err)
        self.assertNotEqual(
            self.last_exported("C0000000001")["C0000000001"]["last_export_utc"],
            "2000-01-01T00:00:00Z")

    def test_a_failed_export_records_nothing(self):
        code, out, err = self.run_export("C0000000999")
        self.assertNotEqual(code, 0)
        self.assertFalse(self.index.exists())


# ── temporary files and git ──────────────────────────────────────────────────

class GitTempFileTests(ExportTest):
    """Every file is written as NAME.partial first and then renamed. That
    temporary file holds the same private text, so git must ignore it as surely
    as the finished file - checked before Slack is contacted - and a failed write
    must not leave it lying around."""

    def make_repo(self, name, ignore):
        repo = self.tmp / name
        repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / ".gitignore").write_text(ignore)
        return repo

    def visible_to_git(self, repo):
        """Every path git would offer to add, apart from the .gitignore itself."""
        status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain",
                                 "-uall"], capture_output=True, text=True, check=True)
        return [line[3:] for line in status.stdout.splitlines()
                if line[3:] != ".gitignore"]

    def failing_rename(self, suffix):
        """os.replace, failing as a full disk would for files ending in `suffix`."""
        real = os.replace

        def replace(source, destination):
            if str(destination).endswith(suffix):
                raise OSError(28, "No space left on device")
            real(source, destination)
        return mock.patch.object(ec.os, "replace", side_effect=replace)

    # A. The finished files are ignored, their temporary files are not.
    def test_ignoring_only_the_finished_files_is_refused_before_slack(self):
        repo = self.make_repo("repo", "*.txt\n*.md\n")
        code, out, err = self.run_export("C0000000001", "--out", str(repo / "out"))
        self.assertNotEqual(code, 0)
        self.assertIn("C0000000001.md.partial", str(code))
        self.assertIn("add *.partial to its .gitignore", str(code))
        self.assertEqual(self.slack.calls, [], "Slack must not be contacted")
        self.assertFalse((repo / "out").exists(), "nothing may be written")
        self.assertEqual(self.visible_to_git(repo), [])

    # B. Both ignored: an ordinary export.
    def test_ignoring_the_temporary_files_too_lets_the_export_run(self):
        repo = self.make_repo("repo", "*.txt\n*.md\n*.partial\n")
        code, out, err = self.run_export("C0000000001", "--out", str(repo / "out"))
        self.assertEqual(code, 0, err)
        self.assertIn("first message", (repo / "out" / "C0000000001.md").read_text())
        self.assertEqual(self.visible_to_git(repo), [])
        self.assertEqual(list((repo / "out").glob("*.partial")), [])

    # C. A write that fails partway leaves no temporary file behind.
    def test_a_failed_rename_removes_its_temporary_file(self):
        repo = self.make_repo("repo", "*.txt\n*.md\n*.partial\n")
        with self.failing_rename(".md"):
            code, out, err = self.run_export("C0000000001", "--out", str(repo / "out"))
        self.assertIn("could not write", str(code))
        self.assertEqual(list((repo / "out").glob("*.partial")), [])
        self.assertEqual(list(self.exports.glob("**/*.partial")), [])
        self.assertEqual(self.visible_to_git(repo), [])
        # ...and the retry still finishes the export.
        code, out, err = self.run_export("C0000000001", "--out", str(repo / "out"))
        self.assertEqual(code, 0, err)
        self.assertEqual((repo / "out" / "C0000000001.md").read_text().count(
            "first message"), 1)

    def test_a_failed_write_removes_its_temporary_file(self):
        repo = self.make_repo("repo", "*.txt\n*.md\n*.partial\n")
        real_fdopen = os.fdopen

        class FullDisk:
            def __init__(self, handle):
                self.handle = handle

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.handle.close()

            def write(self, text):
                self.handle.write(text[:10])      # some private text lands first
                self.handle.flush()
                raise OSError(28, "No space left on device")

        with mock.patch.object(ec.os, "fdopen",
                               side_effect=lambda *a, **k: FullDisk(real_fdopen(*a, **k))):
            code, out, err = self.run_export("C0000000001", "--out", str(repo / "out"))
        self.assertIn("could not write", str(code))
        self.assertEqual(list(self.tmp.glob("**/*.partial")), [])

    def test_a_temporary_file_left_by_a_crash_stays_invisible_to_git(self):
        # A crash (power loss, kill -9) gives no chance to clean up. Simulated by
        # a removal that fails: the file stays, and git must still ignore it.
        repo = self.make_repo("repo", "*.txt\n*.md\n*.partial\n")
        with self.failing_rename(".md"), \
                mock.patch.object(ec.Path, "unlink", side_effect=OSError("crash")):
            self.run_export("C0000000001", "--out", str(repo / "out"))
        leftover = repo / "out" / "C0000000001.md.partial"
        self.assertTrue(leftover.exists())
        self.assertIn("first message", leftover.read_text())
        self.assertEqual(self.visible_to_git(repo), [])

    # D. The raw archive and the conversation index are written the same way.
    def test_a_raw_archive_folder_that_ignores_only_archives_is_refused(self):
        repo = self.make_repo("project", "*.raw.json\n*.txt\n*.md\n")
        with mock.patch.object(ec, "RAW_DIR", repo / "raw"):
            code, out, err = self.run_export("C0000000001", "--out", str(self.tmp / "out"))
        self.assertIn("C0000000001.raw.json.partial", str(code))
        self.assertEqual(self.slack.calls, [])
        self.assertFalse((repo / "raw").exists())

    def test_an_index_whose_temporary_file_is_not_ignored_is_never_written(self):
        repo = self.make_repo("dotfiles", "channels.json\n")
        index = repo / "slack-export" / "channels.json"
        with self.assertRaises(SystemExit) as caught:
            ec.save_directory({"workspaces": {}}, index)
        self.assertIn("channels.json.partial", str(caught.exception.code))
        self.assertIn("add *.partial", str(caught.exception.code))
        self.assertFalse(index.parent.exists())
        # During an export it is the index that is skipped, not the export.
        with mock.patch.object(ec, "DIRECTORY_PATH", index):
            code, out, err = self.run_export("C0000000001")
        self.assertEqual(code, 0, err)
        self.assertIn("conversation index: NOT updated", out)
        self.assertEqual(self.visible_to_git(repo), [])

    def test_the_rerender_tool_checks_its_temporary_file_too(self):
        repo = self.make_repo("repo", "*.txt\n")
        with self.assertRaises(SystemExit) as caught:
            ec.refuse_if_committable([repo / "C0000000001.txt"])
        self.assertIn("C0000000001.txt.partial", str(caught.exception.code))

    # E. Outside any repository nothing changes; the old protections stand.
    def test_outside_any_repository_the_export_runs_as_before(self):
        code, out, err = self.run_export("C0000000001", "--out", str(self.tmp / "out"))
        self.assertEqual(code, 0, err)
        self.assertTrue((self.tmp / "out" / "C0000000001.md").exists())
        self.assertEqual(list(self.tmp.glob("**/*.partial")), [])

    def test_a_repository_that_ignores_nothing_is_still_refused(self):
        repo = self.make_repo("repo", "")
        code, out, err = self.run_export("C0000000001", "--out", str(repo / "out"))
        self.assertIn("C0000000001.md", str(code))
        self.assertEqual(self.slack.calls, [])

    # Other places a temporary file could slip through.
    def test_a_nested_repository_is_judged_by_its_own_rules(self):
        outer = self.make_repo("outer", "*.partial\n")
        inner = outer / "inner"
        inner.mkdir()
        subprocess.run(["git", "init", "-q", str(inner)], check=True)
        (inner / ".gitignore").write_text("*.txt\n*.md\n")
        code, out, err = self.run_export("C0000000001", "--out", str(inner / "out"))
        self.assertIn("C0000000001.md.partial", str(code))
        self.assertEqual(self.slack.calls, [])

    def test_a_tracked_temporary_file_counts_despite_an_ignore_rule(self):
        repo = self.make_repo("repo", "")
        (repo / "out").mkdir()
        tracked = repo / "out" / "C0000000001.md.partial"
        tracked.write_text("placeholder\n")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        (repo / ".gitignore").write_text("*.txt\n*.md\n*.partial\n")
        code, out, err = self.run_export("C0000000001", "--out", str(repo / "out"))
        self.assertIn("C0000000001.md.partial", str(code))
        self.assertEqual(self.slack.calls, [])
        self.assertEqual(tracked.read_text(), "placeholder\n")

    def test_a_folder_reached_through_a_symlink_is_checked_where_it_really_is(self):
        repo = self.make_repo("repo", "*.txt\n*.md\n")
        (repo / "out").mkdir()
        link = self.tmp / "shortcut"
        link.symlink_to(repo / "out")
        code, out, err = self.run_export("C0000000001", "--out", str(link))
        self.assertIn(".partial", str(code))
        self.assertEqual(self.slack.calls, [])

    # Something already at the temporary file's path: a link to a tracked file,
    # or a stray file. It must never receive the conversation, be truncated, or
    # be deleted.
    def tracked_file_and_out_folder(self):
        repo = self.make_repo("repo", "")
        tracked = repo / "tracked.txt"
        tracked.write_bytes(b"ORIGINAL CONTENT\n")
        git = ["git", "-C", str(repo), "-c", "user.name=test",
               "-c", "user.email=test@example.com"]
        subprocess.run(git + ["add", "tracked.txt"], check=True)
        subprocess.run(git + ["commit", "-qm", "tracked"], check=True)
        (repo / ".gitignore").write_text("out/\n*.partial\n")
        (repo / "out").mkdir()
        return repo, tracked

    def assert_nothing_written_past(self, code, repo, tracked):
        self.assertIn("something is already where a temporary file goes", str(code))
        self.assertIn("will not write through it or delete it", str(code))
        self.assertEqual(self.slack.calls, [], "Slack must not be contacted")
        self.assertEqual(tracked.read_bytes(), b"ORIGINAL CONTENT\n")
        self.assertEqual(self.visible_to_git(repo), [])

    # A.
    def test_a_temporary_path_that_is_a_symlink_is_refused(self):
        repo, tracked = self.tracked_file_and_out_folder()
        link = repo / "out" / "C0000000001.md.partial"
        link.symlink_to(tracked)
        code, out, err = self.run_export("C0000000001", "--out", str(repo / "out"))
        self.assert_nothing_written_past(code, repo, tracked)
        self.assertTrue(link.is_symlink(), "the link itself is left alone")

    # B.
    def test_a_temporary_path_that_is_a_hard_link_is_refused(self):
        repo, tracked = self.tracked_file_and_out_folder()
        link = repo / "out" / "C0000000001.txt.partial"
        os.link(tracked, link)
        code, out, err = self.run_export("C0000000001", "--out", str(repo / "out"))
        self.assert_nothing_written_past(code, repo, tracked)
        self.assertEqual(os.stat(tracked).st_nlink, 2, "the hard link is left alone")

    # C.
    def test_a_stray_temporary_file_is_refused_and_kept(self):
        self.exports.mkdir(parents=True)
        stray = self.exports / "C0000000001.md.partial"
        stray.write_bytes(b"someone's half-finished file\n")
        code, out, err = self.run_export("C0000000001")
        self.assertIn("something is already where a temporary file goes", str(code))
        self.assertEqual(stray.read_bytes(), b"someone's half-finished file\n")
        self.assertFalse((self.exports / "C0000000001.md").exists())
        # Once the person has moved it, the same command works.
        stray.unlink()
        code, out, err = self.run_export("C0000000001")
        self.assertEqual(code, 0, err)

    def test_a_raw_archive_temporary_path_is_checked_too(self):
        (self.exports / "raw").mkdir(parents=True)
        stray = self.exports / "raw" / "C0000000001.raw.json.partial"
        stray.write_bytes(b"keep me\n")
        code, out, err = self.run_export("C0000000001")
        self.assertIn("C0000000001.raw.json.partial", str(code))
        self.assertEqual(stray.read_bytes(), b"keep me\n")

    # The early check is a courtesy; write_atomic's exclusive open is the guard,
    # with no gap between checking the path and opening it.
    def test_write_atomic_never_writes_through_a_link_appearing_later(self):
        repo, tracked = self.tracked_file_and_out_folder()
        for make in (lambda link: link.symlink_to(tracked),
                     lambda link: os.link(tracked, link)):
            final = repo / "out" / "C0000000001.md"
            link = repo / "out" / "C0000000001.md.partial"
            make(link)
            with self.subTest(link="symlink" if link.is_symlink() else "hard link"):
                with self.assertRaises(FileExistsError) as caught:
                    ec.write_atomic(final, "private transcript\n")
                self.assertIn("is in the way", str(caught.exception))
                self.assertEqual(tracked.read_bytes(), b"ORIGINAL CONTENT\n")
                self.assertTrue(os.path.lexists(link), "not deleted either")
                self.assertFalse(final.exists())
            link.unlink()

    def test_the_index_is_not_written_through_a_link_either(self):
        repo, tracked = self.tracked_file_and_out_folder()
        index = repo / "out" / "channels.json"
        (repo / "out" / "channels.json.partial").symlink_to(tracked)
        with mock.patch.object(ec, "DIRECTORY_PATH", index):
            code, out, err = self.run_export("C0000000001")
        self.assertEqual(code, 0, err)
        self.assertIn("conversation index: NOT updated", out)
        self.assertIn("channels.json.partial is in the way", out)
        self.assertEqual(tracked.read_bytes(), b"ORIGINAL CONTENT\n")

    def test_a_symlinked_file_is_checked_where_the_link_sits(self):
        # write_atomic replaces a symlink where it sits, so an ignored target
        # does not make an unignored link location safe.
        repo = self.make_repo("repo", "elsewhere/\n")
        (repo / "elsewhere").mkdir()
        (repo / "out").mkdir()
        (repo / "out" / "C0000000001.md").symlink_to(
            repo / "elsewhere" / "C0000000001.md")
        exposed = ec.committable_paths([repo / "out" / "C0000000001.md"])
        self.assertEqual([p.name for p in exposed], ["C0000000001.md"])


if __name__ == "__main__":
    unittest.main()
