"""Tests for the saved channel names: the rules in channel_directory.py, and how
export_conversation.py stores them.

Run from the project folder:

    ./.venv/bin/python -m unittest discover -s tests -v

Every ID and name here is invented. The real store holds real channel names and
never appears in a test, and nothing here reads or writes ~/.config - every store
is a file inside a temporary directory.
"""

import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import channel_directory as cd  # noqa: E402  (needs the sys.path line above)
import export_conversation as ec  # noqa: E402

HERMETIC_GIT = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}

TEAM, OTHER_TEAM = "T0000000001", "T0000000002"
CHAN, OTHER_CHAN = "C0000000001", "C0000000002"
WHEN, LATER = "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"


def mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


# ── the rules ────────────────────────────────────────────────────────────────

class NameTests(unittest.TestCase):
    """Nickname, Slack name and export names each have their own slot."""

    def setUp(self):
        self.store = cd.empty()

    def entry(self, channel=CHAN, team=TEAM):
        return self.store["workspaces"][team]["channels"][channel]

    def nickname(self, name, channel=CHAN, team=TEAM):
        return cd.set_nickname(self.store, team, "Example", channel, name)

    def slack_name(self, name, channel=CHAN):
        cd.set_slack_name(self.store, TEAM, "Example", channel, name)

    def export(self, stem, when=WHEN, channel=CHAN):
        cd.record_export(self.store, TEAM, "Example", channel, stem, when)

    def test_nickname_added_changed_unchanged(self):
        self.assertEqual(self.nickname("Project Planning"), "added")
        self.assertEqual(self.nickname("Project Planning"), "unchanged")
        self.assertEqual(self.nickname("Planning"), "changed")
        self.assertEqual(self.entry(), {"nickname": "Planning"})
        self.assertEqual(self.store["workspaces"][TEAM]["name"], "Example")

    def test_nickname_is_trimmed_and_must_not_be_blank(self):
        self.nickname("  Project Planning  ")
        self.assertEqual(self.entry()["nickname"], "Project Planning")
        with self.assertRaises(ValueError):
            self.nickname("   ")

    def test_the_three_names_never_overwrite_each_other(self):
        self.nickname("Mine")
        self.slack_name("project-planning")
        self.export("Some-File")
        self.assertEqual(self.entry(), {
            "nickname": "Mine", "slack_name": "project-planning",
            "exports": {"Some-File": {"last_export_utc": WHEN}}})

    def test_slack_name_follows_a_rename_and_ignores_blank(self):
        self.slack_name("old-name")
        self.slack_name("new-name")
        self.slack_name("  ")
        self.assertEqual(self.entry()["slack_name"], "new-name")

    def test_blank_slack_name_still_creates_the_entry(self):
        # A 1:1 DM has no Slack name, but an export of it must still be listed.
        self.slack_name("")
        self.assertEqual(self.entry(), {})

    def test_each_export_name_keeps_its_own_latest_time(self):
        self.export("First-Name", WHEN)
        self.export("Second-Name", WHEN)
        self.export("First-Name", LATER)
        self.assertEqual(self.entry()["exports"], {
            "First-Name": {"last_export_utc": LATER},
            "Second-Name": {"last_export_utc": WHEN}})

    def test_same_id_in_two_workspaces_stays_separate(self):
        self.nickname("Here", team=TEAM)
        self.nickname("There", team=OTHER_TEAM)
        self.assertEqual(self.entry(team=TEAM)["nickname"], "Here")
        self.assertEqual(self.entry(team=OTHER_TEAM)["nickname"], "There")

    def test_workspace_name_follows_the_latest_report(self):
        self.nickname("x")
        cd.set_nickname(self.store, TEAM, "Renamed Workspace", OTHER_CHAN, "y")
        self.assertEqual(self.store["workspaces"][TEAM]["name"], "Renamed Workspace")


class SearchTests(unittest.TestCase):

    def setUp(self):
        self.store = cd.empty()
        cd.set_slack_name(self.store, TEAM, "Example", CHAN, "project-planning")
        cd.set_nickname(self.store, TEAM, "Example", CHAN, "Planning Team")
        cd.record_export(self.store, TEAM, "Example", CHAN, "Old-File-Name", WHEN)
        cd.set_slack_name(self.store, TEAM, "Example", OTHER_CHAN, "lab-notes")
        cd.record_export(self.store, TEAM, "Example", OTHER_CHAN, "Bench-Log", WHEN)
        cd.record_export(self.store, TEAM, "Example", OTHER_CHAN, "Bench-Log-2", LATER)
        cd.set_slack_name(self.store, OTHER_TEAM, "Another", "D0000000003", "")
        cd.set_nickname(self.store, OTHER_TEAM, "Another", "D0000000003", "Alex")
        cd.set_slack_name(self.store, TEAM, "Example", "C0000000004",
                          "mpdm-ahandle--bhandle-1")

    def ids(self, term=""):
        return sorted(row["channel_id"] for row in cd.search(self.store, term))

    def test_no_term_lists_everything(self):
        self.assertEqual(self.ids(), [CHAN, OTHER_CHAN, "C0000000004", "D0000000003"])

    def test_matches_slack_name_and_nickname(self):
        self.assertEqual(self.ids("lab"), [OTHER_CHAN])          # Slack name
        self.assertEqual(self.ids("team"), [CHAN])               # nickname

    def test_fallback_nickname_is_searched_because_it_is_shown(self):
        self.assertEqual(self.ids("bench log 2"), [OTHER_CHAN])

    def test_hidden_export_names_are_not_searched(self):
        # CHAN shows its own nickname, so its export name is not on the line.
        self.assertEqual(self.ids("old file"), [])

    def test_unnamed_group_dm_matches_its_label_not_its_handles(self):
        self.assertEqual(self.ids("ahandle"), [])
        self.assertEqual(self.ids("unnamed group"), ["C0000000004"])

    def test_ignores_case_spaces_and_punctuation(self):
        self.assertEqual(self.ids("PROJECT planning"), [CHAN])
        self.assertEqual(self.ids("project_planning"), [CHAN])

    def test_id_finds_names(self):
        self.assertEqual(self.ids("c0000000001"), [CHAN])
        self.assertEqual(self.ids("d000"), ["D0000000003"])

    def test_row_carries_every_field(self):
        row = cd.search(self.store, "lab")[0]
        self.assertEqual(row, {"team_id": TEAM, "team_name": "Example",
                               "channel_id": OTHER_CHAN, "nickname": "",
                               "slack_name": "lab-notes",
                               "export_names": ["Bench-Log", "Bench-Log-2"],
                               "latest_export_name": "Bench-Log-2",
                               "last_export_utc": LATER})

    def test_no_match_is_an_empty_list(self):
        self.assertEqual(cd.search(self.store, "nothing like this"), [])


def row(channel_id, slack_name="", nickname="", latest_export=""):
    return {"team_id": TEAM, "team_name": "Example", "channel_id": channel_id,
            "nickname": nickname, "slack_name": slack_name,
            "export_names": [latest_export] if latest_export else [],
            "latest_export_name": latest_export, "last_export_utc": ""}


class SortAndFormatTests(unittest.TestCase):

    def test_default_sort_is_by_slack_name_ignoring_case(self):
        rows = [row(CHAN, "beta"), row(OTHER_CHAN, "Alpha"), row("C0000000003", "gamma")]
        self.assertEqual([r["slack_name"] for r in cd.sort_rows(rows)],
                         ["Alpha", "beta", "gamma"])

    def test_no_slack_name_sorts_by_nickname(self):
        rows = [row(CHAN, "carrot"), row("D0000000003", nickname="Banana")]
        self.assertEqual([r["channel_id"] for r in cd.sort_rows(rows)],
                         ["D0000000003", CHAN])

    def test_ties_are_ordered_by_id_not_by_store_order(self):
        rows = [row(OTHER_CHAN, "same"), row(CHAN, "same")]
        self.assertEqual([r["channel_id"] for r in cd.sort_rows(rows)],
                         [CHAN, OTHER_CHAN])

    def test_every_sort_order_and_column_works_on_a_search_row(self):
        # Guards the tables themselves: a new entry must accept what search returns.
        store = cd.empty()
        cd.set_nickname(store, TEAM, "Example", CHAN, "x")
        rows = cd.search(store)
        for by in cd.SORT_KEYS:
            cd.sort_rows(rows, by)
        cd.format_rows(rows, tuple(cd.COLUMNS))

    def test_default_format_is_id_and_slack_name_aligned(self):
        lines = cd.format_rows([row("C00000001", "short", "Mine"),
                                row("C0000000002", "a-longer-name", "Also Mine")])
        self.assertEqual(lines, ["C00000001    short",
                                 "C0000000002  a-longer-name"])

    def test_nickname_column_is_added_on_request(self):
        lines = cd.format_rows([row("C00000001", "short", "Mine"),
                                row("C0000000002", "a-longer-name", "Also Mine")],
                               cd.WITH_NICKNAMES)
        self.assertEqual(lines, ["C00000001    short          Mine",
                                 "C0000000002  a-longer-name  Also Mine"])

    def test_blank_cells_print_a_placeholder(self):
        lines = cd.format_rows([row(CHAN, "project-planning"),
                                row("D0000000003", "", "Alex")], cd.WITH_NICKNAMES)
        self.assertEqual(lines, [f"{CHAN}  project-planning  -",
                                 "D0000000003  -                 Alex"])

    def test_unnamed_group_dm_is_labelled_and_named_one_is_not(self):
        lines = cd.format_rows([row(CHAN, "mpdm-ahandle--bhandle-1"),
                                row(OTHER_CHAN, "Named Group")])
        self.assertEqual(lines, [f"{CHAN}  [unnamed group DM]",
                                 f"{OTHER_CHAN}  Named Group"])

    def test_no_slack_name_falls_back_to_marked_export_name(self):
        lines = cd.format_rows([row("D0000000003", latest_export="Some-Person"),
                                row("D0000000004")])
        self.assertEqual(lines, ["D0000000003  Some-Person (from export)",
                                 "D0000000004  -"])

    def test_nickname_column_falls_back_to_a_different_export_name(self):
        lines = cd.format_rows(
            [row(CHAN, "mpdm-ahandle--bhandle-1", latest_export="Group-Chat"),
             row(OTHER_CHAN, "chan", "Mine", latest_export="Ignored")],
            cd.WITH_NICKNAMES)
        self.assertEqual(lines, [f"{CHAN}  [unnamed group DM]  Group-Chat (from export)",
                                 f"{OTHER_CHAN}  chan                Mine"])

    def test_fallback_that_repeats_the_first_column_is_hidden(self):
        # Same words, different case and hyphens: still a repeat.
        lines = cd.format_rows(
            [row(CHAN, "rl--abundant-trace-review",
                 latest_export="RL-Abundant-Trace-Review"),
             row("D0000000003", latest_export="Some-Person")],
            cd.WITH_NICKNAMES)
        self.assertEqual(lines, [f"{CHAN}  rl--abundant-trace-review  -",
                                 "D0000000003  Some-Person (from export)  -"])

    def test_latest_export_is_the_most_recent_not_the_last_written(self):
        store = cd.empty()
        cd.record_export(store, TEAM, "Example", CHAN, "Newer", LATER)
        cd.record_export(store, TEAM, "Example", CHAN, "Older", WHEN)
        self.assertEqual(cd.search(store)[0]["latest_export_name"], "Newer")

    def test_fallback_never_changes_the_store(self):
        store = cd.empty()
        cd.record_export(store, TEAM, "Example", CHAN, "Some-File", WHEN)
        cd.format_rows(cd.search(store))
        self.assertNotIn("nickname", store["workspaces"][TEAM]["channels"][CHAN])

    def test_nothing_to_format(self):
        self.assertEqual(cd.format_rows([]), [])


class ValidateTests(unittest.TestCase):

    def test_a_filled_store_is_valid(self):
        store = cd.empty()
        cd.set_nickname(store, TEAM, "Example", CHAN, "x")
        cd.set_slack_name(store, TEAM, "Example", CHAN, "y")
        cd.record_export(store, TEAM, "Example", CHAN, "z", WHEN)
        self.assertIs(cd.validate(store), store)

    def test_unknown_fields_are_kept_for_later_versions(self):
        store = {"version": 1, "workspaces": {TEAM: {"channels": {
            CHAN: {"nickname": "x", "added_later": True}}}}}
        self.assertIs(cd.validate(store), store)

    def test_wrong_shapes_are_refused(self):
        for bad in ([], {"workspaces": {}}, {"version": 99, "workspaces": {}},
                    {"version": 1},
                    {"version": 1, "workspaces": {TEAM: {}}},
                    {"version": 1, "workspaces": {TEAM: {"channels": {CHAN: []}}}},
                    {"version": 1, "workspaces": {TEAM: {"channels": {
                        CHAN: {"nickname": 5}}}}},
                    {"version": 1, "workspaces": {TEAM: {"channels": {
                        CHAN: {"exports": ["Some-File"]}}}}}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                cd.validate(bad)


# ── storing it ───────────────────────────────────────────────────────────────

class StoreFileTests(unittest.TestCase):

    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name).resolve()
        self.path = self.tmp / "config" / "slack-export" / "channels.json"
        patcher = mock.patch.dict(os.environ, HERMETIC_GIT)
        patcher.start()
        self.addCleanup(patcher.stop)
        old = os.umask(0)
        self.addCleanup(os.umask, old)

    def saved_store(self):
        store = cd.empty()
        cd.set_nickname(store, TEAM, "Example", CHAN, "Project Planning")
        return store

    def test_default_location_is_outside_the_project(self):
        self.assertEqual(ec.DIRECTORY_PATH,
                         Path.home() / ".config" / "slack-export" / "channels.json")
        self.assertNotIn(ec.PROJECT_ROOT, ec.DIRECTORY_PATH.parents)

    def test_missing_file_is_an_empty_store(self):
        self.assertEqual(ec.load_directory(self.path), cd.empty())
        self.assertFalse(self.path.exists())     # loading never creates anything

    def test_round_trip(self):
        store = self.saved_store()
        ec.save_directory(store, self.path)
        self.assertEqual(ec.load_directory(self.path), store)

    def test_file_and_folder_are_owner_only(self):
        ec.save_directory(self.saved_store(), self.path)
        self.assertEqual(mode(self.path), 0o600)
        self.assertEqual(mode(self.path.parent), 0o700)

    def test_damaged_file_stops_the_run_and_is_left_alone(self):
        self.path.parent.mkdir(parents=True)
        for text in ("{ not json", '{"version": 99, "workspaces": {}}'):
            with self.subTest(text=text):
                self.path.write_text(text)
                with self.assertRaises(SystemExit) as caught:
                    ec.load_directory(self.path)
                self.assertIn("unreadable", str(caught.exception))
                self.assertEqual(self.path.read_text(), text)

    def test_refuses_to_save_where_git_could_commit_it(self):
        # Some people keep ~/.config itself in a dotfiles repository.
        subprocess.run(["git", "init", "-q", str(self.tmp)], check=True)
        with self.assertRaises(SystemExit) as caught:
            ec.save_directory(self.saved_store(), self.path)
        self.assertIn("could be committed", str(caught.exception))
        self.assertFalse(self.path.exists())

    def test_saves_inside_a_repository_that_ignores_it(self):
        subprocess.run(["git", "init", "-q", str(self.tmp)], check=True)
        (self.tmp / ".gitignore").write_text("config/\n")
        ec.save_directory(self.saved_store(), self.path)
        self.assertTrue(self.path.exists())


# ── the list command ─────────────────────────────────────────────────────────

class ListCommandTests(StoreFileTests):

    def run_main(self, *argv):
        """Run slack-export with these arguments against the temporary store.

        The Keychain and Slack are replaced by stand-ins that fail the test if
        touched: list must work offline and never read the token.
        """
        out, err = io.StringIO(), io.StringIO()
        forbidden = mock.Mock(side_effect=AssertionError("list touched the network"))
        with mock.patch.object(ec, "DIRECTORY_PATH", self.path), \
                mock.patch.object(ec, "keychain_token", forbidden), \
                mock.patch.object(ec, "WebClient", forbidden), \
                mock.patch.object(sys, "argv", ["slack-export", *argv]), \
                redirect_stdout(out), redirect_stderr(err):
            try:
                ec.main()
                code = 0
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
                err.write(str(exc.code) if not isinstance(exc.code, int) else "")
        return code, out.getvalue(), err.getvalue()

    def fill(self):
        store = cd.empty()
        cd.set_slack_name(store, TEAM, "Example", CHAN, "project-planning")
        cd.set_nickname(store, TEAM, "Example", CHAN, "Planning Team")
        cd.set_slack_name(store, TEAM, "Example", OTHER_CHAN, "lab-notes")
        ec.save_directory(store, self.path)

    def test_lists_ids_and_slack_names_by_default(self):
        self.fill()
        code, out, _ = self.run_main("list")
        self.assertEqual(code, 0)
        self.assertEqual(out, f"{OTHER_CHAN}  lab-notes\n"
                              f"{CHAN}  project-planning\n")

    def test_nicknames_flag_adds_the_column(self):
        self.fill()
        for flag in ("-n", "--nicknames"):
            with self.subTest(flag=flag):
                _, out, _ = self.run_main("list", flag)
                self.assertEqual(out, f"{OTHER_CHAN}  lab-notes         -\n"
                                      f"{CHAN}  project-planning  Planning Team\n")

    def test_a_search_always_shows_the_nickname_that_matched(self):
        self.fill()
        _, out, _ = self.run_main("list", "team")
        self.assertEqual(out, f"{CHAN}  project-planning  Planning Team\n")

    def test_search_words_are_joined_without_quotes(self):
        self.fill()
        _, out, _ = self.run_main("list", "project", "PLAN")
        self.assertEqual(out, f"{CHAN}  project-planning  Planning Team\n")

    def test_search_by_id(self):
        self.fill()
        _, out, _ = self.run_main("list", OTHER_CHAN)
        self.assertEqual(out, f"{OTHER_CHAN}  lab-notes  -\n")   # search: 3 columns

    def test_no_match_exits_1_like_grep(self):
        self.fill()
        code, out, err = self.run_main("list", "nothing like this")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("No saved conversation matches 'nothing like this'", err)

    def test_empty_store_says_so_and_creates_nothing(self):
        code, out, _ = self.run_main("list")
        self.assertEqual(code, 0)
        self.assertIn("No saved conversations yet", out)
        self.assertFalse(self.path.exists())

    def test_an_id_is_still_an_export_not_a_command(self):
        # The export path must still be reached: here it stops at the Keychain.
        with self.assertRaisesRegex(AssertionError, "reached export"):
            with mock.patch.object(ec, "refuse_if_committable"), \
                    mock.patch.object(ec, "load_archive", return_value=None), \
                    mock.patch.object(ec, "keychain_token",
                                      side_effect=AssertionError("reached export")), \
                    mock.patch.object(sys, "argv", ["slack-export", CHAN]):
                ec.main()



if __name__ == "__main__":
    unittest.main()
