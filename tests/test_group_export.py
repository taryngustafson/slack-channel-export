"""End-to-end tests of `slack-export --group NAME`, against a fake Slack.

Run from the project folder:

    ./.venv/bin/python -m unittest discover -s tests -v

The real command runs from argument parsing to the last write, with the same
three things swapped out as in test_export_end_to_end.py: a FakeSlack client,
temporary folders, and no Keychain. Every ID, name and message is invented.
"""

import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_export_end_to_end import ExportTest, HERMETIC_GIT  # noqa: E402
import export_conversation as ec  # noqa: E402

ALPHA, BRAVO, CHARLIE = "C0000000001", "C0000000002", "C0000000003"
HOME_TEAM, OTHER_TEAM = "T0000000001", "T0000000009"


class GroupTest(ExportTest):
    """ExportTest plus a group in the conversation index and a Desktop folder."""

    def setUp(self):
        super().setUp()
        self.desktop = self.tmp / "Desktop"
        self.desktop.mkdir()
        self.slack.add_channel(BRAVO, "research")
        self.slack.post(BRAVO, "2000.000100", "research message")

    def make_group(self, name, *ids, team=HOME_TEAM):
        """Put `ids` in group `name`, writing the index directly."""
        store = (json.loads(self.index.read_text()) if self.index.exists()
                 else {"version": 1, "workspaces": {}})
        for conversation_id in ids:
            workspace = store["workspaces"].setdefault(
                team, {"name": "Example" if team == HOME_TEAM else "Elsewhere",
                       "channels": {}})
            workspace["channels"].setdefault(conversation_id, {})
        members = store.setdefault("groups", {}).setdefault(name, [])
        members.extend({"workspace": team, "channel": c} for c in ids)
        self.index.parent.mkdir(parents=True, exist_ok=True)
        self.index.write_text(json.dumps(store))

    def at(self, folder, name):
        return (folder / name).read_text()

    def info_calls(self, conversation_id):
        return sum(1 for name, arg in self.slack.calls
                   if name == "conversations.info" and arg == conversation_id)


# ── the ordinary run ─────────────────────────────────────────────────────────

class GroupExportTests(GroupTest):

    def test_every_member_is_exported_under_its_id(self):
        self.make_group("Research", ALPHA, BRAVO)
        code, out, err = self.run_export("--group", "Research")
        self.assertEqual(code, 0, err + out)
        self.assertIn("first message", self.read(f"{ALPHA}.txt"))
        self.assertIn("research message", self.read(f"{BRAVO}.md"))
        self.assertNotIn("research message", self.read(f"{ALPHA}.md"))
        self.assertIn("DONE: 2 of 2 conversations exported", out)
        self.assertIn("never exported before", out)
        self.assertEqual(self.slack.count("auth.test"), 1, "one pre-flight for all")

    def test_a_group_name_with_spaces(self):
        self.make_group("Project Planning", ALPHA)
        code, out, err = self.run_export("--group", "Project Planning")
        self.assertEqual(code, 0, err + out)
        self.assertIn("first message", self.read(f"{ALPHA}.txt"))

    def test_options_can_go_before_the_group(self):
        self.make_group("Research", ALPHA)
        code, out, err = self.run_export("--out", str(self.desktop),
                                         "--group", "Research")
        self.assertEqual(code, 0, err + out)
        self.assertTrue((self.desktop / f"{ALPHA}.md").exists())

    def test_one_failing_member_does_not_stop_the_others(self):
        self.make_group("Research", "C0000000999", ALPHA, BRAVO)
        code, out, err = self.run_export("--group", "Research")
        self.assertEqual(code, 1)
        self.assertIn("DONE: 2 of 3 conversations exported", out)
        self.assertIn("C0000000999  FAILED: conversations.info failed: "
                      "channel_not_found", out)
        self.assertIn("first message", self.read(f"{ALPHA}.txt"))
        self.assertIn("research message", self.read(f"{BRAVO}.txt"))
        self.assertFalse((self.exports / "raw" / "C0000000999.raw.json").exists())

    def test_a_member_from_another_workspace_is_skipped(self):
        self.make_group("Research", ALPHA)
        self.make_group("Research", BRAVO, team=OTHER_TEAM)
        code, out, err = self.run_export("--group", "Research")
        self.assertEqual(code, 1)
        self.assertIn("saved from another Slack workspace (Elsewhere)", out)
        self.assertIn("DONE: 1 of 2 conversations exported", out)
        self.assertEqual(self.info_calls(BRAVO), 0, "never fetched")
        self.assertFalse((self.exports / f"{BRAVO}.md").exists())
        self.assertIn("first message", self.read(f"{ALPHA}.txt"))

    def test_repeated_runs_add_only_what_is_new(self):
        self.make_group("Research", ALPHA, BRAVO)
        self.run_export("--group", "Research")
        self.slack.post(ALPHA, "1000.000300", "third message")
        code, out, err = self.run_export("--group", "Research")
        self.assertEqual(code, 0, err + out)
        for name in (f"{ALPHA}.txt", f"{ALPHA}.md"):
            text = self.read(name)
            for line in ("first message", "third message"):
                self.assertEqual(text.count(line), 1, (name, line))
        self.assertEqual(self.read(f"{BRAVO}.txt").count("research message"), 1)
        self.assertNotIn("never exported before", out)

    def test_no_threads_now_and_the_replies_arrive_next_time(self):
        self.slack.reply(ALPHA, "1000.000100", "1000.000150", "a reply")
        self.make_group("Research", ALPHA)
        code, out, err = self.run_export("--group", "Research", "--no-threads")
        self.assertEqual(code, 0, err + out)
        self.assertNotIn("a reply", self.read(f"{ALPHA}.txt"))
        code, out, err = self.run_export("--group", "Research")
        self.assertEqual(code, 0, err + out)
        self.assertEqual(self.read(f"{ALPHA}.txt").count("a reply"), 1)

    def test_last_exported_is_recorded_for_each_member(self):
        self.make_group("Research", ALPHA, BRAVO)
        self.run_export("--group", "Research")
        self.assertIn(ALPHA, self.last_exported(ALPHA))
        self.assertIn(BRAVO, self.last_exported(BRAVO))


# ── existing copies ──────────────────────────────────────────────────────────

class ExistingCopyTests(GroupTest):

    def test_a_copy_under_another_name_keeps_its_name(self):
        self.run_export(ALPHA, "Notes")
        self.make_group("Research", ALPHA)
        self.slack.post(ALPHA, "1000.000300", "third message")
        code, out, err = self.run_export("--group", "Research")
        self.assertEqual(code, 0, err + out)
        self.assertEqual(self.read("Notes.txt").count("third message"), 1)
        self.assertFalse((self.exports / f"{ALPHA}.txt").exists())

    def test_copies_in_two_folders_are_both_updated_from_one_fetch(self):
        self.run_export(ALPHA, "Notes")
        self.run_export(ALPHA, "Notes", "--out", str(self.desktop))
        self.make_group("Research", ALPHA)
        self.slack.post(ALPHA, "1000.000300", "third message")
        self.slack.calls.clear()
        code, out, err = self.run_export("--group", "Research")
        self.assertEqual(code, 0, err + out)
        for folder in (self.exports, self.desktop):
            for name in ("Notes.txt", "Notes.md"):
                text = self.at(folder, name)
                self.assertEqual(text.count("first message"), 1, (folder, name))
                self.assertEqual(text.count("third message"), 1, (folder, name))
        self.assertEqual(self.info_calls(ALPHA), 1, "one fetch for one archive")
        self.assertEqual(self.archive("Notes")["pending"], {})

    def test_two_archive_names_are_each_brought_up_to_date(self):
        self.run_export(ALPHA)
        self.slack.post(ALPHA, "1000.000300", "third message")
        self.run_export(ALPHA, "Notes", "--out", str(self.desktop))
        self.make_group("Research", ALPHA)
        self.slack.post(ALPHA, "1000.000400", "fourth message")
        self.slack.calls.clear()
        code, out, err = self.run_export("--group", "Research")
        self.assertEqual(code, 0, err + out)
        # The ID-named copy was one message further behind than the other.
        for path in (self.exports / f"{ALPHA}.txt", self.desktop / "Notes.txt"):
            text = path.read_text()
            for line in ("first message", "third message", "fourth message"):
                self.assertEqual(text.count(line), 1, (path, line))
        self.assertEqual(self.info_calls(ALPHA), 2, "each archive fetched for itself")

    def test_out_updates_only_the_copy_in_that_folder(self):
        self.run_export(ALPHA, "Notes")
        self.run_export(ALPHA, "Notes", "--out", str(self.desktop))
        self.make_group("Research", ALPHA)
        before = self.read("Notes.txt")
        self.slack.post(ALPHA, "1000.000300", "third message")
        code, out, err = self.run_export("--group", "Research",
                                         "--out", str(self.desktop))
        self.assertEqual(code, 0, err + out)
        self.assertIn("third message", self.at(self.desktop, "Notes.txt"))
        self.assertEqual(self.read("Notes.txt"), before)
        self.assertIn("1 other copy outside", out)

    def test_out_with_no_copy_there_adds_one_named_by_id(self):
        self.run_export(ALPHA, "Notes")
        self.make_group("Research", ALPHA)
        before = self.read("Notes.txt")
        code, out, err = self.run_export("--group", "Research",
                                         "--out", str(self.desktop))
        self.assertEqual(code, 0, err + out)
        self.assertIn("first message", self.at(self.desktop, f"{ALPHA}.txt"))
        self.assertIn("no copy in this folder yet", out)
        self.assertEqual(self.read("Notes.txt"), before)

    def test_moved_copies_mean_a_fresh_one_and_the_moved_files_are_untouched(self):
        self.run_export(ALPHA, "Notes", "--out", str(self.desktop))
        moved = self.tmp / "Moved"
        shutil.move(str(self.desktop), str(moved))
        kept = (moved / "Notes.txt").read_text()
        self.make_group("Research", ALPHA)
        code, out, err = self.run_export("--group", "Research")
        self.assertEqual(code, 0, err + out)
        self.assertIn("every earlier copy was moved or deleted", out)
        self.assertIn("first message", self.read(f"{ALPHA}.txt"))
        self.assertEqual((moved / "Notes.txt").read_text(), kept)

    def test_a_missing_half_of_the_pair_is_rewritten_in_full(self):
        self.run_export(ALPHA, "Notes")
        (self.exports / "Notes.txt").unlink()
        self.make_group("Research", ALPHA)
        self.slack.post(ALPHA, "1000.000300", "third message")
        code, out, err = self.run_export("--group", "Research")
        self.assertEqual(code, 0, err + out)
        for name in ("Notes.txt", "Notes.md"):
            text = self.read(name)
            for line in ("first message", "third message"):
                self.assertEqual(text.count(line), 1, (name, line))
        self.assertFalse((self.exports / f"{ALPHA}.txt").exists())

    def test_an_unrelated_file_where_a_fresh_copy_goes_skips_that_member(self):
        (self.exports).mkdir(parents=True, exist_ok=True)
        (self.exports / f"{ALPHA}.txt").write_text("somebody else's notes\n")
        self.make_group("Research", ALPHA, BRAVO)
        code, out, err = self.run_export("--group", "Research")
        self.assertEqual(code, 1)
        self.assertIn("already exists, but no export of this conversation", out)
        self.assertEqual(self.read(f"{ALPHA}.txt"), "somebody else's notes\n")
        self.assertFalse((self.exports / f"{ALPHA}.md").exists())
        self.assertEqual(self.info_calls(ALPHA), 0)
        self.assertIn("research message", self.read(f"{BRAVO}.txt"))

    def test_a_skipped_file_holds_back_last_exported(self):
        self.run_export(ALPHA, "Notes")
        self.run_export(ALPHA, "Notes", "--out", str(self.desktop))
        # A sentinel time, so "not updated" cannot be mistaken for "updated
        # within the same second".
        store = json.loads(self.index.read_text())
        entry = store["workspaces"][HOME_TEAM]["channels"][ALPHA]
        entry["exports"]["Notes"]["last_export_utc"] = "2001-01-01T00:00:00Z"
        self.index.write_text(json.dumps(store))
        # A Desktop .txt the archive has no record of: replaced by hand.
        (self.desktop / "Notes.txt").unlink()
        (self.desktop / "Notes.txt").write_text("hand-made\n")
        archive = self.archive("Notes")
        for mapping in (archive["rendered"], archive["rendered_run"]):
            mapping.pop(str(self.desktop / "Notes.txt"))
        (self.exports / "raw" / "Notes.raw.json").write_text(json.dumps(archive))
        self.make_group("Research", ALPHA)
        code, out, err = self.run_export("--group", "Research")
        self.assertEqual(code, 0, err + out)
        self.assertIn("SKIPPED - no record of writing this file", out)
        self.assertEqual(self.last_exported(ALPHA)["Notes"]["last_export_utc"],
                         "2001-01-01T00:00:00Z")
        self.assertEqual((self.desktop / "Notes.txt").read_text(), "hand-made\n")


# ── stopped before Slack is contacted ────────────────────────────────────────

class RefusedGroupTests(GroupTest):

    def files(self):
        return {path: path.read_bytes() for path in self.tmp.rglob("*")
                if path.is_file()}

    def assert_refused(self, argv, message):
        before = self.files()
        self.slack.calls.clear()
        code, out, err = self.run_export(*argv)
        self.assertNotEqual(code, 0)
        self.assertIn(message, str(code) + err + out)
        self.assertEqual(self.slack.calls, [], "Slack must not be contacted")
        self.assertEqual(self.files(), before, "nothing written or changed")

    def test_a_group_that_does_not_exist(self):
        self.make_group("Research", ALPHA)
        self.assert_refused(("--group", "research"),
                            "No group named 'research'.\nDid you mean 'Research'?")

    def test_an_empty_group_does_nothing_and_succeeds(self):
        store = {"version": 1, "workspaces": {}, "groups": {"Research": []}}
        self.index.parent.mkdir(parents=True)
        self.index.write_text(json.dumps(store))
        code, out, err = self.run_export("--group", "Research")
        self.assertEqual(code, 0)
        self.assertIn("has no conversations in it", out)
        self.assertEqual(self.slack.calls, [])

    def test_group_with_conversation_ids(self):
        self.make_group("Research", ALPHA)
        self.assert_refused(("--group", "Research", BRAVO),
                            "cannot be combined with conversation IDs")
        self.assert_refused((BRAVO, "--group", "Research"),
                            "cannot be combined with conversation IDs")

    def test_group_with_copy(self):
        self.make_group("Research", ALPHA)
        self.assert_refused(("--group", "Research", "--copy"),
                            "--copy works with one conversation at a time")

    def test_two_groups(self):
        self.make_group("Research", ALPHA)
        self.make_group("Planning", BRAVO)
        self.assert_refused(("--group", "Research", "--group", "Planning"),
                            "one group at a time")

    def test_an_unquoted_name_with_spaces(self):
        self.make_group("Project Planning", ALPHA)
        self.assert_refused(("--group", "Project", "Planning"),
                            '--group "Project Planning"')

    def test_group_without_a_name(self):
        code, out, err = self.run_export("--group")
        self.assertEqual(code, 2)
        self.assertIn("expected one argument", err)

    def test_an_archive_holding_another_conversation_is_refused(self):
        # BRAVO's fresh copy would be named BRAVO, whose archive holds ALPHA.
        self.run_export(ALPHA, "Notes")
        raw = self.exports / "raw"
        (raw / "Notes.raw.json").rename(raw / f"{BRAVO}.raw.json")
        self.make_group("Research", BRAVO)
        self.assert_refused(("--group", "Research"), "already holds a different "
                                                     "conversation")

    def test_two_members_under_one_name_are_refused(self):
        self.run_export(ALPHA, "Notes")
        raw = self.exports / "raw"
        (raw / "Notes.raw.json").rename(raw / f"{BRAVO}.raw.json")
        self.make_group("Research", ALPHA, BRAVO)
        self.assert_refused(("--group", "Research"),
                            "two conversations in this group would share files")

    def test_an_unreadable_archive_stops_everything(self):
        self.make_group("Research", ALPHA)
        raw = self.exports / "raw"
        raw.mkdir(parents=True)
        (raw / "Broken.raw.json").write_text("{not json")
        self.assert_refused(("--group", "Research"), "Broken.raw.json is unreadable")

    def test_a_copy_git_could_commit_stops_everything(self):
        repo = self.tmp / "repo"
        subprocess.run(["git", "init", "-q", str(repo)], check=True,
                       env={**os.environ, **HERMETIC_GIT})
        (repo / ".gitignore").write_text("*.md\n*.txt\n*.partial\n")
        self.run_export(ALPHA, "Notes", "--out", str(repo))
        (repo / ".gitignore").write_text("")
        self.make_group("Research", ALPHA, BRAVO)
        self.slack.calls.clear()
        before = (repo / "Notes.txt").read_text()
        code, out, err = self.run_export("--group", "Research")
        self.assertNotEqual(code, 0)
        self.assertIn("git", str(code))
        self.assertEqual(self.slack.calls, [])
        self.assertEqual((repo / "Notes.txt").read_text(), before)
        self.assertFalse((self.exports / f"{BRAVO}.md").exists())

    def test_a_stray_temporary_file_stops_everything(self):
        self.make_group("Research", ALPHA, BRAVO)
        self.exports.mkdir(parents=True)
        (self.exports / f"{BRAVO}.md.partial").write_text("left over")
        self.assert_refused(("--group", "Research"),
                            "something is already where a temporary file goes")


# ── a run that dies partway ──────────────────────────────────────────────────

class InterruptedGroupTests(GroupTest):

    def failing_writes(self, fail_on):
        real = ec.write_atomic
        count = [0]

        def write(path, text):
            count[0] += 1
            if count[0] in fail_on:
                raise OSError(28, "No space left on device")
            real(path, text)
        return mock.patch.object(ec, "write_atomic", side_effect=write)

    def test_a_retry_finishes_every_folder_without_duplicates(self):
        self.run_export(ALPHA, "Notes")
        self.run_export(ALPHA, "Notes", "--out", str(self.desktop))
        self.make_group("Research", ALPHA, BRAVO)
        # ALPHA's writes, in order: archive, exports .txt, exports .md, Desktop
        # .txt, Desktop .md, archive again. Each pass fails one of them, after a
        # new message, so there is always something to write.
        sent = ["first message"]
        for fail_on in range(1, 7):
            with self.subTest(fail_on=fail_on):
                sent.append(f"message {fail_on}")
                self.slack.post(ALPHA, f"1000.00{fail_on + 3}00", sent[-1])
                with self.failing_writes({fail_on}):
                    code, out, err = self.run_export("--group", "Research")
                self.assertEqual(code, 1)
                self.assertIn("could not write", out)
                code, out, err = self.run_export("--group", "Research")
                self.assertEqual(code, 0, err + out)
                for folder in (self.exports, self.desktop):
                    for name in ("Notes.txt", "Notes.md"):
                        text = self.at(folder, name)
                        for line in sent:
                            self.assertEqual(text.count(line), 1,
                                             (fail_on, folder, name, line))
                self.assertEqual(self.archive("Notes")["pending"], {})
                self.assertIn("research message", self.read(f"{BRAVO}.txt"))

    def test_a_failed_member_does_not_stop_the_next(self):
        self.make_group("Research", ALPHA, BRAVO)
        with self.failing_writes({1}):
            code, out, err = self.run_export("--group", "Research")
        self.assertEqual(code, 1)
        self.assertIn(f"{ALPHA}  FAILED: could not write", out)
        self.assertIn("research message", self.read(f"{BRAVO}.txt"))


if __name__ == "__main__":
    unittest.main()
