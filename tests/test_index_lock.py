"""Tests for the lock on the conversation index: two runs changing it at the same
moment must both keep their change.

Run from the project folder:

    ./.venv/bin/python -m unittest discover -s tests -v

Every ID and name here is invented, and every store is a file inside a temporary
directory.
"""

import fcntl
import io
import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

import channel_directory as cd  # noqa: E402  (needs the sys.path lines above)
import export_conversation as ec  # noqa: E402
from test_channel_directory import (  # noqa: E402
    CHAN, OTHER_CHAN, TEAM, WHEN, CommandTest, FakeSlack, TempStoreTest)

AUTH = {"team_id": TEAM, "team": "Example"}
NEW_CHAN = "C0000000005"   # not in the index until `group` looks it up in Slack


def hold_lock(path: Path) -> int:
    """Take the index lock the way another slack-export would, and return the
    open folder; closing it releases the lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path.parent, os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


class LockTests(TempStoreTest):
    """directory_lock itself, and the export's index update that uses it."""

    def record(self, channel=CHAN, stem=None):
        return ec.record_in_directory(AUTH, {"name": "project-planning"}, channel,
                                      stem or channel, WHEN, self.path)

    def exports(self, channel=CHAN):
        stored = json.loads(self.path.read_text())
        return set(stored["workspaces"][TEAM]["channels"][channel]["exports"])

    def test_a_busy_index_is_waited_for_then_updated(self):
        fd = hold_lock(self.path)
        done = threading.Event()
        worker = threading.Thread(target=lambda: (self.record(), done.set()))
        worker.start()
        self.assertFalse(done.wait(0.3), "updated the index while it was locked")
        os.close(fd)
        self.assertTrue(done.wait(5), "never updated the index after it was freed")
        worker.join()
        self.assertEqual(self.exports(), {CHAN})

    def test_waiting_is_said_and_a_long_wait_gives_up_with_nothing_changed(self):
        self.record(stem="first")
        before = self.path.read_text()
        fd = hold_lock(self.path)
        self.addCleanup(os.close, fd)
        err = io.StringIO()
        with mock.patch.object(ec, "INDEX_LOCK_NOTICE_SECONDS", 0), \
                mock.patch.object(ec, "INDEX_LOCK_WAIT_SECONDS", 0.2), \
                redirect_stderr(err):
            line = self.record(stem="second")
        self.assertIn("Waiting for another slack-export", err.getvalue())
        self.assertIn("NOT updated - the export itself is fine.", line)
        self.assertIn("another slack-export has been changing the conversation "
                      "index", line)
        self.assertEqual(self.path.read_text(), before)

    def test_a_short_wait_is_not_mentioned(self):
        fd = hold_lock(self.path)
        threading.Timer(0.1, os.close, [fd]).start()
        err = io.StringIO()
        with redirect_stderr(err):
            self.record()
        self.assertEqual(err.getvalue(), "")

    def test_a_run_killed_while_holding_the_lock_leaves_none_behind(self):
        self.path.parent.mkdir(parents=True)
        holder = subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent(f"""
                import fcntl, os, sys, time
                fd = os.open({str(self.path.parent)!r}, os.O_RDONLY)
                fcntl.flock(fd, fcntl.LOCK_EX)
                print("locked", flush=True)
                time.sleep(60)
            """)], stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.wait)
        self.assertEqual(holder.stdout.readline().strip(), "locked")
        holder.send_signal(signal.SIGKILL)
        holder.wait()
        holder.stdout.close()
        started = time.monotonic()
        self.record()
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(self.exports(), {CHAN})

    def test_nothing_new_is_left_beside_the_index(self):
        self.record()
        self.assertEqual(os.listdir(self.path.parent), ["channels.json"])

    def test_runs_finishing_together_all_keep_their_change(self):
        """The race the lock exists for: several processes each recording exports
        as fast as they can. Without it, some of these updates are lost."""
        script = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(ROOT / "tools")!r})
            from pathlib import Path
            import export_conversation as ec
            channel = sys.argv[1]
            for n in range(15):
                line = ec.record_in_directory(
                    {AUTH!r}, {{"name": channel}}, channel, f"{{channel}}-{{n}}",
                    {WHEN!r}, Path({str(self.path)!r}))
                if "NOT updated" in line:
                    sys.exit(line)
        """)
        channels = [f"C00000001{n:02d}" for n in range(6)]
        runs = [subprocess.Popen([sys.executable, "-c", script, channel],
                                 stderr=subprocess.PIPE, text=True)
                for channel in channels]
        for run in runs:
            _, err = run.communicate(timeout=60)
            self.assertEqual(run.returncode, 0, err)
        for channel in channels:
            self.assertEqual(self.exports(channel),
                             {f"{channel}-{n}" for n in range(15)})


class SlowSlack(FakeSlack):
    """A Slack whose conversations.info answer arrives after another run has
    changed the index."""

    def __init__(self, conversations, meanwhile):
        super().__init__(conversations)
        self.meanwhile = meanwhile

    def conversations_info(self, channel):
        self.meanwhile()
        return super().conversations_info(channel)


class CommandsKeepOtherRunsChangesTests(CommandTest):
    """`save` and `group` ask Slack before changing the index. What another run
    saved while Slack was answering must survive their own save."""

    CONVERSATIONS = {
        CHAN: {"id": CHAN, "name": "project-planning", "is_private": True},
        OTHER_CHAN: {"id": OTHER_CHAN, "name": "lab-notes", "is_private": False},
    }

    def another_run_records_an_export(self):
        ec.record_in_directory(AUTH, {"name": "lab-notes"}, OTHER_CHAN, "lab-notes",
                               WHEN, self.path)

    def run_with(self, *argv):
        slack = SlowSlack(self.CONVERSATIONS, self.another_run_records_an_export)
        with mock.patch.object(ec, "connect", return_value=slack):
            return self.run_main(*argv)

    def stored(self):
        return json.loads(self.path.read_text())

    def test_save_keeps_an_export_recorded_while_slack_answered(self):
        code, _, err = self.run_with("save", CHAN, "Planning")
        self.assertEqual(code, 0, err)
        channels = self.stored()["workspaces"][TEAM]["channels"]
        self.assertEqual(channels[CHAN]["nickname"], "Planning")
        self.assertIn("lab-notes", channels[OTHER_CHAN]["exports"])

    def test_group_keeps_an_export_recorded_while_slack_answered(self):
        code, _, err = self.run_with("group", "Research", CHAN)
        self.assertEqual(code, 0, err)
        stored = self.stored()
        self.assertEqual([m["channel"] for m in stored["groups"]["Research"]], [CHAN])
        self.assertIn("lab-notes",
                      stored["workspaces"][TEAM]["channels"][OTHER_CHAN]["exports"])

    def test_a_nickname_taken_while_slack_answered_is_still_refused(self):
        def another_run_takes_the_nickname():
            store = ec.load_directory(self.path)
            cd.set_nickname(store, TEAM, "Example", OTHER_CHAN, "Planning")
            ec.save_directory(store, self.path)
        slack = SlowSlack(self.CONVERSATIONS, another_run_takes_the_nickname)
        with mock.patch.object(ec, "connect", return_value=slack):
            code, _, err = self.run_main("save", CHAN, "Planning")
        self.assertEqual(code, 1)
        self.assertIn("already used by", err)
        self.assertNotIn(CHAN, self.stored()["workspaces"][TEAM]["channels"])

    def test_a_group_deleted_while_slack_answered_is_not_recreated(self):
        self.fill()
        self.run_main("group", "Research", CHAN)
        before = self.path.read_text()

        def another_run_deletes_the_group():
            store = ec.load_directory(self.path)
            cd.delete_group(store, "Research")
            ec.save_directory(store, self.path)
        conversations = {**self.CONVERSATIONS, NEW_CHAN: {
            "id": NEW_CHAN, "name": "field-notes", "is_private": False}}
        slack = SlowSlack(conversations, another_run_deletes_the_group)
        with mock.patch.object(ec, "connect", return_value=slack):
            code, out, err = self.run_main("group", "Research", NEW_CHAN)
        self.assertEqual(code, 1)
        self.assertIn("Group Research was deleted while this command was running",
                      err)
        stored = self.stored()
        self.assertNotIn("Research", stored["groups"])
        # Nothing is indexed without also being added.
        self.assertNotIn(NEW_CHAN, stored["workspaces"][TEAM]["channels"])
        self.assertNotEqual(self.path.read_text(), before)   # only the deletion

    def test_adding_to_a_new_group_still_creates_it(self):
        conversations = {**self.CONVERSATIONS, NEW_CHAN: {
            "id": NEW_CHAN, "name": "field-notes", "is_private": False}}
        slack = SlowSlack(conversations, lambda: None)
        with mock.patch.object(ec, "connect", return_value=slack):
            code, out, err = self.run_main("group", "Fieldwork", NEW_CHAN)
        self.assertEqual(code, 0, err)
        self.assertIn("Created group Fieldwork.", out)
        self.assertEqual([m["channel"] for m in self.stored()["groups"]["Fieldwork"]],
                         [NEW_CHAN])

    def test_a_group_deleted_meanwhile_is_reported_not_recreated(self):
        self.fill()
        self.run_main("group", "Research", CHAN)
        real_load = ec.load_directory
        calls = []

        def load_then_delete(path=None):
            store = real_load(path)
            if not calls:     # the first read, before the lock is taken
                calls.append(1)
                other = real_load(path)
                cd.delete_group(other, "Research")
                ec.save_directory(other, path)
            return store
        with mock.patch.object(ec, "load_directory", load_then_delete):
            code, _, err = self.run_main("group", "Research", "--remove", CHAN)
        self.assertEqual(code, 1)
        self.assertIn("No group named 'Research'", err)
        self.assertNotIn("Research", self.stored()["groups"])


if __name__ == "__main__":
    unittest.main()
