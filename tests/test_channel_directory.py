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

    def test_a_nickname_already_used_elsewhere_is_refused_and_nothing_changes(self):
        self.nickname("Planning Team", channel=CHAN)
        before = json.dumps(self.store, sort_keys=True)
        for clash in ("Planning Team", "planning-team", "  PLANNING team "):
            with self.subTest(clash=clash), \
                    self.assertRaises(cd.NicknameTaken) as caught:
                self.nickname(clash, channel=OTHER_CHAN)
            self.assertEqual(caught.exception.owner_id, CHAN)
        self.assertEqual(json.dumps(self.store, sort_keys=True), before)

    def test_re_saving_its_own_nickname_is_fine(self):
        self.nickname("Planning Team")
        self.assertEqual(self.nickname("planning team"), "changed")

    def test_other_workspaces_may_reuse_a_nickname(self):
        self.nickname("Planning Team", team=TEAM)
        self.assertEqual(self.nickname("Planning Team", team=OTHER_TEAM), "added")

    def test_kind_comes_from_slack_not_from_the_id(self):
        for info, kind in (({"is_im": True}, "dm"),
                           # Slack marks group DMs private too.
                           ({"is_mpim": True, "is_private": True}, "group_dm"),
                           ({"is_private": True}, "private_channel"),
                           ({}, "public_channel")):
            with self.subTest(kind=kind):
                self.assertEqual(cd.kind_of(info), kind)

    def test_set_kind_stores_it_and_refuses_unknown(self):
        cd.set_kind(self.store, TEAM, "Example", CHAN, "group_dm")
        self.assertEqual(self.entry()["kind"], "group_dm")
        with self.assertRaises(ValueError):
            cd.set_kind(self.store, TEAM, "Example", CHAN, "guess")

    def test_workspace_name_follows_the_latest_report(self):
        self.nickname("x")
        cd.set_nickname(self.store, TEAM, "Renamed Workspace", OTHER_CHAN, "y")
        self.assertEqual(self.store["workspaces"][TEAM]["name"], "Renamed Workspace")


class SymbolNicknameTests(unittest.TestCase):

    def test_different_emoji_nicknames_are_different(self):
        store = cd.empty()
        cd.set_nickname(store, TEAM, "Example", "D0000000005", "🎉")
        self.assertEqual(cd.set_nickname(store, TEAM, "Example", "D0000000006", "🚀"),
                         "added")

    def test_the_same_emoji_nickname_is_taken(self):
        store = cd.empty()
        cd.set_nickname(store, TEAM, "Example", "D0000000005", "🎉")
        with self.assertRaises(cd.NicknameTaken):
            cd.set_nickname(store, TEAM, "Example", "D0000000006", " 🎉 ")


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
        cd.set_kind(self.store, TEAM, "Example", "C0000000004", "group_dm")

    def ids(self, term=None):
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

    def test_dms_match_their_label_not_a_group_dms_handles(self):
        self.assertEqual(self.ids("ahandle"), [])
        self.assertEqual(self.ids("dm"), ["C0000000004", "D0000000003"])

    def test_exported_unnamed_group_dm_matches_its_export_name(self):
        cd.record_export(self.store, TEAM, "Example", "C0000000004", "Group-Chat", WHEN)
        self.assertEqual(self.ids("group chat"), ["C0000000004"])
        # Its label stays in the Slack name column, so it still matches.
        self.assertEqual(self.ids("dm"), ["C0000000004", "D0000000003"])

    def test_punctuation_only_search_does_not_match_everything(self):
        self.assertEqual(self.ids("!!!"), [])
        self.assertEqual(self.ids("?!"), [])
        self.assertEqual(self.ids(""), [])
        self.assertEqual(self.ids("   "), [])

    def test_symbol_or_emoji_search_matches_as_typed(self):
        cd.set_nickname(self.store, TEAM, "Example", "D0000000005", "🎉")
        cd.set_nickname(self.store, TEAM, "Example", "D0000000006", "Party 🚀")
        cd.set_nickname(self.store, TEAM, "Example", "D0000000007", "C++ help")
        self.assertEqual(self.ids("🎉"), ["D0000000005"])
        self.assertEqual(self.ids("🚀"), ["D0000000006"])
        self.assertEqual(self.ids("party"), ["D0000000006"])
        self.assertEqual(self.ids("🌮"), [])
        self.assertEqual(self.ids("++"), ["D0000000007"])

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
                               "slack_name": "lab-notes", "kind": "",
                               "export_names": ["Bench-Log", "Bench-Log-2"],
                               "latest_export_name": "Bench-Log-2",
                               "last_export_utc": LATER})

    def test_no_match_is_an_empty_list(self):
        self.assertEqual(cd.search(self.store, "nothing like this"), [])


def row(channel_id, slack_name="", nickname="", latest_export="", kind=""):
    return {"team_id": TEAM, "team_name": "Example", "channel_id": channel_id,
            "nickname": nickname, "slack_name": slack_name, "kind": kind,
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

    def test_format_is_id_nickname_slack_name_aligned(self):
        lines = cd.format_rows([row("C00000001", "short", "Mine"),
                                row("C0000000002", "a-longer-name", "Also Mine")])
        self.assertEqual(lines, ["C00000001    Mine       short",
                                 "C0000000002  Also Mine  a-longer-name"])

    def test_header_names_the_columns_and_widens_them_if_needed(self):
        lines = cd.format_rows([row(CHAN, "chan")], header=True)
        self.assertEqual(lines, ["ID           NICKNAME  SLACK NAME",
                                 f"{CHAN}  -         chan"])

    def test_blank_cells_print_a_placeholder(self):
        # No nickname and never exported; a channel not yet named by Slack.
        lines = cd.format_rows([row(CHAN, "project-planning"),
                                row(OTHER_CHAN, "", "Mine")])
        self.assertEqual(lines, [f"{CHAN}  -     project-planning",
                                 f"{OTHER_CHAN}  Mine  -"])

    def test_every_dm_without_a_slack_name_shows_one_label(self):
        # By kind, or by the D prefix for an entry recorded before its kind.
        lines = cd.format_rows([row("D0000000003", "", "Alex", kind="dm"),
                                row("D0000000004", "", "Sam"),
                                row(CHAN, "mpdm-ahandle--bhandle-1", "Group",
                                    kind="group_dm")])
        self.assertEqual(lines, ["D0000000003  Alex   (DM)",
                                 "D0000000004  Sam    (DM)",
                                 f"{CHAN}  Group  (DM)"])

    def test_no_nickname_falls_back_to_export_name_in_brackets(self):
        # Shown even when it repeats the Slack name: '-' means never exported.
        lines = cd.format_rows(
            [row(CHAN, "project-planning", latest_export="Planning-Notes"),
             row(OTHER_CHAN, "chan", latest_export="chan"),
             row("D0000000003", latest_export="Some-Person")])
        self.assertEqual(lines, [f"{CHAN}  [Planning-Notes]  project-planning",
                                 f"{OTHER_CHAN}  [chan]            chan",
                                 "D0000000003  [Some-Person]     (DM)"])

    def test_export_named_only_by_its_id_counts_as_no_name(self):
        lines = cd.format_rows([row(CHAN, "lab-notes", latest_export=CHAN)])
        self.assertEqual(lines, [f"{CHAN}  -  lab-notes"])

    def test_nickname_beats_export_name(self):
        lines = cd.format_rows([row("D0000000003", "", "Alex",
                                    latest_export="Some-Person")])
        self.assertEqual(lines, ["D0000000003  Alex  (DM)"])

    def test_unnamed_group_dm_is_labelled_and_named_one_is_not(self):
        lines = cd.format_rows([row(CHAN, "mpdm-ahandle--bhandle-1", "Grant Group",
                                    kind="group_dm"),
                                row(OTHER_CHAN, "Named Group", kind="group_dm")])
        self.assertEqual(lines, [f"{CHAN}  Grant Group  (DM)",
                                 f"{OTHER_CHAN}  -            Named Group"])

    def test_channel_named_mpdm_is_shown_as_it_is(self):
        # Only a group DM's generated name is replaced; a channel may really be
        # called this.
        for kind in ("public_channel", "private_channel"):
            with self.subTest(kind=kind):
                lines = cd.format_rows([row(CHAN, "mpdm-roadmap", kind=kind)])
                self.assertEqual(lines, [f"{CHAN}  -  mpdm-roadmap"])

    def test_dm_sorts_among_channels_by_its_nickname(self):
        rows = [row(CHAN, "zebra"), row("D0000000003", "", "Morgan"),
                row(OTHER_CHAN, "apple"), row("D0000000004", latest_export="Kim")]
        self.assertEqual([r["channel_id"] for r in cd.sort_rows(rows)],
                         [OTHER_CHAN, "D0000000004", "D0000000003", CHAN])

    def test_unnamed_group_dm_sorts_by_its_nickname_not_its_label(self):
        rows = [row(CHAN, "mpdm-ahandle--bhandle-1", latest_export="Apple-Group",
                    kind="group_dm"),
                row(OTHER_CHAN, "banana")]
        self.assertEqual([r["channel_id"] for r in cd.sort_rows(rows)],
                         [CHAN, OTHER_CHAN])

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
                        CHAN: {"kind": 5}}}}},
                    {"version": 1, "workspaces": {TEAM: {"channels": {
                        CHAN: {"exports": ["Some-File"]}}}}}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                cd.validate(bad)


class GroupTests(unittest.TestCase):
    """Groups are a table of their own, beside the workspaces (D43)."""

    def setUp(self):
        self.store = cd.empty()
        cd.set_slack_name(self.store, TEAM, "Example", CHAN, "lab-notes")
        cd.set_slack_name(self.store, TEAM, "Example", OTHER_CHAN, "planning")
        cd.set_slack_name(self.store, OTHER_TEAM, "Another", CHAN, "same-id-elsewhere")

    def groups(self):
        return self.store.get("groups", {})

    def test_a_store_from_before_groups_has_none_and_is_valid(self):
        self.assertNotIn("groups", self.store)
        self.assertIs(cd.validate(self.store), self.store)

    def test_create_makes_an_empty_group(self):
        self.assertEqual(cd.create_group(self.store, "Ideas"), "created")
        self.assertEqual(self.groups(), {"Ideas": []})

    def test_creating_an_existing_group_changes_nothing(self):
        cd.add_to_group(self.store, "Research", TEAM, CHAN)
        before = json.dumps(self.store)
        self.assertEqual(cd.create_group(self.store, "Research"), "exists")
        self.assertEqual(json.dumps(self.store), before)

    def test_adding_creates_the_group_if_new(self):
        self.assertEqual(cd.add_to_group(self.store, "Research", TEAM, CHAN), "added")
        self.assertEqual(self.groups(),
                         {"Research": [{"workspace": TEAM, "channel": CHAN}]})

    def test_adding_only_adds_never_replaces(self):
        cd.add_to_group(self.store, "Research", TEAM, CHAN)
        cd.add_to_group(self.store, "Research", TEAM, OTHER_CHAN)
        self.assertEqual(self.groups()["Research"],
                         [{"workspace": TEAM, "channel": CHAN},
                          {"workspace": TEAM, "channel": OTHER_CHAN}])

    def test_adding_a_member_twice_keeps_one(self):
        cd.add_to_group(self.store, "Research", TEAM, CHAN)
        self.assertEqual(cd.add_to_group(self.store, "Research", TEAM, CHAN),
                         "already in")
        self.assertEqual(len(self.groups()["Research"]), 1)

    def test_one_conversation_in_many_groups(self):
        for name in ("Research", "Marketing", "Ideas"):
            cd.add_to_group(self.store, name, TEAM, CHAN)
        self.assertEqual(sorted(self.groups()), ["Ideas", "Marketing", "Research"])

    def test_a_group_can_span_workspaces(self):
        # The same channel ID in two workspaces is two different members.
        cd.add_to_group(self.store, "Research", TEAM, CHAN)
        self.assertEqual(cd.add_to_group(self.store, "Research", OTHER_TEAM, CHAN),
                         "added")
        self.assertEqual(self.groups()["Research"],
                         [{"workspace": TEAM, "channel": CHAN},
                          {"workspace": OTHER_TEAM, "channel": CHAN}])

    def test_names_are_exact_apart_from_outer_spaces(self):
        cd.create_group(self.store, "tests")
        self.assertEqual(cd.create_group(self.store, "TESTS"), "created")
        self.assertEqual(cd.create_group(self.store, "  tests "), "exists")
        self.assertEqual(sorted(self.groups()), ["TESTS", "tests"])

    def test_an_empty_name_is_refused(self):
        for name in ("", "   "):
            with self.subTest(name=name), self.assertRaises(ValueError):
                cd.create_group(self.store, name)
        self.assertNotIn("groups", self.store)

    def test_an_unsaved_conversation_is_refused_and_nothing_changes(self):
        with self.assertRaises(cd.NotSaved) as caught:
            cd.add_to_group(self.store, "Research", TEAM, "C0000000009")
        self.assertEqual(caught.exception.channel_id, "C0000000009")
        self.assertNotIn("groups", self.store)   # not even an empty group

    def test_saved_in_another_workspace_is_not_saved_in_this_one(self):
        with self.assertRaises(cd.NotSaved):
            cd.add_to_group(self.store, "Research", OTHER_TEAM, OTHER_CHAN)

    def test_a_store_with_groups_is_valid(self):
        cd.create_group(self.store, "Ideas")
        cd.add_to_group(self.store, "Research", TEAM, CHAN)
        self.assertIs(cd.validate(self.store), self.store)

    def test_wrong_group_shapes_are_refused(self):
        base = {"version": 1, "workspaces": {}}
        for bad in ([], {"Research": {}}, {"Research": ["C0000000001"]},
                    {"Research": [{"channel": CHAN}]},
                    {"Research": [{"workspace": TEAM, "channel": 5}]}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                cd.validate({**base, "groups": bad})


# ── storing it ───────────────────────────────────────────────────────────────

class TempStoreTest(unittest.TestCase):
    """A store path inside a fresh temporary folder, with hermetic git and a
    permissive umask. Holds no tests, so subclasses don't run each other's."""

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


class StoreFileTests(TempStoreTest):

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

class GroupBrowseTests(unittest.TestCase):
    """The helpers behind `list --groups`."""

    def setUp(self):
        self.store = cd.empty()
        for channel in (CHAN, OTHER_CHAN):
            cd.set_slack_name(self.store, TEAM, "Example", channel, channel.lower())
        cd.add_to_group(self.store, "Research", TEAM, CHAN)
        cd.add_to_group(self.store, "Research", TEAM, OTHER_CHAN)
        cd.add_to_group(self.store, "Work", TEAM, CHAN)
        cd.create_group(self.store, "ideas")

    def test_sizes_are_counted_and_sorted_ignoring_case(self):
        self.assertEqual(cd.group_sizes(self.store),
                         [("ideas", 0), ("Research", 2), ("Work", 1)])

    def test_case_twins_sort_next_to_each_other_in_a_fixed_order(self):
        cd.create_group(self.store, "IDEAS")
        self.assertEqual([n for n, _ in cd.group_sizes(self.store)][:2],
                         ["IDEAS", "ideas"])

    def test_no_groups_key_means_no_groups(self):
        self.assertEqual(cd.group_sizes(cd.empty()), [])

    def test_members_of_several_groups_count_once(self):
        self.assertEqual(cd.group_members(self.store, ["Research", "Work"]),
                         {(TEAM, CHAN), (TEAM, OTHER_CHAN)})

    def test_similar_names_differ_only_in_case(self):
        self.assertEqual(cd.similar_group_names(self.store, "research"), ["Research"])
        self.assertEqual(cd.similar_group_names(self.store, "Research"), [])
        self.assertEqual(cd.similar_group_names(self.store, "Reserch"), [])


class GroupFileTests(TempStoreTest):

    def test_groups_survive_a_save_and_load(self):
        store = cd.empty()
        cd.set_slack_name(store, TEAM, "Example", CHAN, "lab-notes")
        cd.create_group(store, "Ideas")
        cd.add_to_group(store, "Research", TEAM, CHAN)
        ec.save_directory(store, self.path)
        self.assertEqual(ec.load_directory(self.path)["groups"],
                         {"Ideas": [],
                          "Research": [{"workspace": TEAM, "channel": CHAN}]})


class CommandTest(TempStoreTest):
    """Runs slack-export's main() against the temporary store. Holds no tests."""

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



class ListCommandTests(CommandTest):

    def test_lists_id_nickname_and_slack_name(self):
        self.fill()
        code, out, _ = self.run_main("list")
        self.assertEqual(code, 0)
        self.assertEqual(out, "ID           NICKNAME       SLACK NAME\n"
                              f"{OTHER_CHAN}  -              lab-notes\n"
                              f"{CHAN}  Planning Team  project-planning\n")

    def test_retired_nicknames_flag_is_still_accepted(self):
        self.fill()
        _, plain, _ = self.run_main("list")
        for flag in ("-n", "--nicknames"):
            with self.subTest(flag=flag):
                code, out, _ = self.run_main("list", flag)
                self.assertEqual((code, out), (0, plain))

    def test_a_search_shows_the_nickname_that_matched(self):
        self.fill()
        _, out, _ = self.run_main("list", "team")
        self.assertEqual(out, "ID           NICKNAME       SLACK NAME\n"
                              f"{CHAN}  Planning Team  project-planning\n")

    def test_search_words_are_joined_without_quotes(self):
        self.fill()
        _, out, _ = self.run_main("list", "project", "PLAN")
        self.assertEqual(out, "ID           NICKNAME       SLACK NAME\n"
                              f"{CHAN}  Planning Team  project-planning\n")

    def test_search_by_id(self):
        self.fill()
        _, out, _ = self.run_main("list", OTHER_CHAN)
        self.assertEqual(out, "ID           NICKNAME  SLACK NAME\n"
                              f"{OTHER_CHAN}  -         lab-notes\n")

    def test_no_match_exits_1_like_grep(self):
        self.fill()
        code, out, err = self.run_main("list", "nothing like this")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("No conversation in your conversation index matches 'nothing like this'", err)

    def test_punctuation_only_search_is_not_match_all(self):
        self.fill()
        code, out, err = self.run_main("list", "!!!")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("No conversation in your conversation index matches '!!!'", err)

    def test_empty_store_says_so_and_creates_nothing(self):
        code, out, _ = self.run_main("list")
        self.assertEqual(code, 0)
        self.assertIn("Your conversation index is empty", out)
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




# ── the save command ─────────────────────────────────────────────────────────

GOOD_SCOPES = ("identify,channels:history,channels:read,groups:history,groups:read,"
               "im:history,im:read,mpim:history,mpim:read,users:read")


class ListGroupsCommandTests(CommandTest):
    """`list --groups` and `list --search`. run_main fails the test on any Keychain
    or Slack use, so every test here also proves the command stayed offline."""

    def fill_groups(self):
        store = cd.empty()
        cd.set_slack_name(store, TEAM, "Example", CHAN, "project-planning")
        cd.set_nickname(store, TEAM, "Example", CHAN, "Planning Team")
        cd.set_slack_name(store, TEAM, "Example", OTHER_CHAN, "lab-notes")
        cd.set_nickname(store, TEAM, "Example", "D0000000003", "Alex")
        cd.set_kind(store, TEAM, "Example", "D0000000003", "dm")
        for channel in (CHAN, OTHER_CHAN, "D0000000003"):
            cd.add_to_group(store, "Research", TEAM, channel)
        cd.add_to_group(store, "Work", TEAM, CHAN)
        cd.add_to_group(store, "Lab Notes", TEAM, OTHER_CHAN)
        cd.create_group(store, "Ideas")
        ec.save_directory(store, self.path)

    def test_all_groups_sorted_with_counts(self):
        self.fill_groups()
        code, out, _ = self.run_main("list", "--groups")
        self.assertEqual(code, 0)
        self.assertEqual(out, "GROUP      CONVERSATIONS\n"
                              "Ideas      0\n"
                              "Lab Notes  1\n"
                              "Research   3\n"
                              "Work       1\n")

    def test_no_groups_says_how_to_make_one(self):
        self.fill()
        code, out, _ = self.run_main("list", "--groups")
        self.assertEqual((code, out), (0, "You have no groups yet.\n"
                                          "To make one: slack-export group <name>\n"))

    def test_one_groups_members_use_lists_table_and_order(self):
        self.fill_groups()
        code, out, _ = self.run_main("list", "--groups", "Research")
        self.assertEqual(code, 0)
        # The same lines plain `list` prints for these three, in the same order.
        _, everything, _ = self.run_main("list")
        self.assertEqual(out, everything)
        self.assertEqual(out, "ID           NICKNAME       SLACK NAME\n"
                              "D0000000003  Alex           (DM)\n"
                              f"{OTHER_CHAN}  -              lab-notes\n"
                              f"{CHAN}  Planning Team  project-planning\n")

    def test_members_only_from_the_named_group(self):
        self.fill_groups()
        _, out, _ = self.run_main("list", "--groups", "Work")
        self.assertEqual(out, "ID           NICKNAME       SLACK NAME\n"
                              f"{CHAN}  Planning Team  project-planning\n")

    def test_a_name_with_spaces_works_when_quoted(self):
        self.fill_groups()
        code, out, _ = self.run_main("list", "--groups", "Lab Notes")
        self.assertEqual(code, 0)
        self.assertIn(OTHER_CHAN, out)

    def test_several_groups_list_each_member_once(self):
        self.fill_groups()
        code, out, _ = self.run_main("list", "--groups", "Work", "Research")
        self.assertEqual(code, 0)
        self.assertEqual(out.count(CHAN), 1)
        self.assertEqual(len(out.splitlines()), 4)   # header + 3 conversations

    def test_several_groups_show_which_each_conversation_is_in(self):
        # Every member of EITHER group is listed (not only those in both), with a
        # GROUPS column naming the requested groups it is in, in the order typed.
        self.fill_groups()
        code, out, _ = self.run_main("list", "--groups", "Work", "Lab Notes")
        self.assertEqual(code, 0)
        self.assertEqual(out, "ID           NICKNAME       SLACK NAME        GROUPS\n"
                              f"{OTHER_CHAN}  -              lab-notes         Lab Notes\n"
                              f"{CHAN}  Planning Team  project-planning  Work\n")
        _, out, _ = self.run_main("list", "--groups", "Work", "Research")
        self.assertIn(f"{CHAN}  Planning Team  project-planning  Work, Research\n", out)
        self.assertIn("D0000000003  Alex           (DM)              Research\n", out)

    def test_one_group_has_no_groups_column(self):
        self.fill_groups()
        _, out, _ = self.run_main("list", "--groups", "Research")
        self.assertNotIn("GROUPS", out)

    def test_empty_group(self):
        self.fill_groups()
        code, out, _ = self.run_main("list", "--groups", "Ideas")
        self.assertEqual((code, out), (0, "Ideas has no conversations yet.\n"))

    def test_unknown_group_is_an_error(self):
        self.fill_groups()
        code, out, err = self.run_main("list", "--groups", "Nope")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "No group named 'Nope'.")

    def test_names_are_case_sensitive_with_a_hint(self):
        self.fill_groups()
        code, _, err = self.run_main("list", "--groups", "research")
        self.assertEqual(code, 1)
        self.assertEqual(err, "No group named 'research'.\n"
                              "Did you mean 'Research'? Group names are exact.")

    def test_unknown_among_several_points_to_search(self):
        # `--groups Research planning` reads "planning" as a group: say how to
        # search instead.
        self.fill_groups()
        code, _, err = self.run_main("list", "--groups", "Research", "planning")
        self.assertEqual(code, 1)
        self.assertIn("No group named 'planning'.", err)
        self.assertIn("use --search", err)

    def test_search_within_a_group(self):
        self.fill_groups()
        code, out, _ = self.run_main("list", "--groups", "Research",
                                     "--search", "planning")
        self.assertEqual(code, 0)
        self.assertEqual(out, "ID           NICKNAME       SLACK NAME\n"
                              f"{CHAN}  Planning Team  project-planning\n")

    def test_search_within_a_group_with_no_match(self):
        self.fill_groups()
        code, _, err = self.run_main("list", "--groups", "Work", "--search", "lab")
        self.assertEqual(code, 1)
        self.assertEqual(err, "No conversation in Work matches 'lab'.")

    def test_search_flag_matches_positional_search(self):
        self.fill_groups()
        _, positional, _ = self.run_main("list", "planning")
        code, flag, _ = self.run_main("list", "--search", "planning")
        self.assertEqual(code, 0)
        self.assertEqual(flag, positional)
        _, two, _ = self.run_main("list", "--search", "project", "PLAN")
        self.assertEqual(two, positional)

    def test_positional_search_still_works_unchanged(self):
        self.fill()
        code, out, _ = self.run_main("list", "planning")
        self.assertEqual(code, 0)
        self.assertEqual(out, "ID           NICKNAME       SLACK NAME\n"
                              f"{CHAN}  Planning Team  project-planning\n")

    def test_positional_search_with_groups_is_refused_with_the_fix(self):
        self.fill_groups()
        for argv in (("planning", "--groups", "Research"),
                     ("--groups", "Lab Notes", "--nicknames", "planning")):
            with self.subTest(argv=argv):
                code, out, err = self.run_main("list", *argv)
                self.assertEqual((code, out), (1, ""))
                self.assertTrue(err.startswith("When using --groups, use --search "
                                               "to search within the results:\n"))
        _, _, err = self.run_main("list", "planning", "--groups", "Lab Notes")
        self.assertIn('slack-export list --groups "Lab Notes" --search planning', err)

    def test_both_kinds_of_search_at_once_is_refused(self):
        self.fill()
        code, _, err = self.run_main("list", "lab", "--search", "planning")
        self.assertEqual(code, 1)
        self.assertIn("not both", err)

    def test_search_needs_a_group_name_with_groups(self):
        self.fill_groups()
        code, _, err = self.run_main("list", "--groups", "--search", "planning")
        self.assertEqual(code, 1)
        self.assertIn("needs a group name", err)


class FakeSlack:
    """Stands in for WebClient: auth.test and conversations.info, nothing else."""

    def __init__(self, conversations, scopes=GOOD_SCOPES):
        self.conversations, self.scopes = conversations, scopes
        self.calls = []

    def auth_test(self):
        self.calls.append("auth.test")
        response = mock.MagicMock()
        response.headers = {"x-oauth-scopes": self.scopes}
        fields = {"team_id": TEAM, "team": "Example", "user": "someone",
                  "user_id": "U0000000001", "url": "https://example.slack.com/"}
        response.__getitem__.side_effect = fields.__getitem__
        return response

    def conversations_info(self, channel):
        self.calls.append(f"conversations.info {channel}")
        if channel not in self.conversations:
            raise ec.SlackApiError("not found", {"error": "channel_not_found"})
        return {"channel": self.conversations[channel]}


class GroupCommandTests(CommandTest):
    """`slack-export group`. run_main fails the test on any Keychain or Slack use,
    so a test that does not patch `connect` also proves the run stayed local."""

    CONVERSATIONS = {
        "C0000000005": {"id": "C0000000005", "name": "field-notes", "is_private": False},
        "D0000000003": {"id": "D0000000003", "is_im": True, "user": "U0000000002"},
        "D0000000004": {"id": "D0000000004", "is_im": True, "user": "U0000000003"},
    }
    GONE = "C0000000009"   # Slack answers channel_not_found

    def run_group(self, *argv, slack=None):
        """Run `group` with a fake Slack available, for IDs not yet indexed."""
        self.slack = slack or FakeSlack(self.CONVERSATIONS)
        with mock.patch.object(ec, "connect", return_value=self.slack):
            return self.run_main("group", *argv)

    def stored(self):
        return json.loads(self.path.read_text())

    def members(self, name="Research"):
        return [m["channel"] for m in self.stored()["groups"][name]]

    # -- staying local --------------------------------------------------------

    def test_name_alone_makes_an_empty_group_locally(self):
        self.fill()
        code, out, _ = self.run_main("group", "Research")
        self.assertEqual((code, out), (0, "Created group Research (empty).\n"))
        self.assertEqual(self.stored()["groups"], {"Research": []})

    def test_making_an_existing_group_changes_nothing(self):
        self.fill()
        self.run_main("group", "Research", CHAN)
        before = self.path.read_text()
        code, out, _ = self.run_main("group", "Research")
        self.assertEqual((code, out), (0, "Group Research already exists "
                                          "(1 conversation). Nothing was changed.\n"))
        self.assertEqual(self.path.read_text(), before)

    def test_indexed_conversations_are_added_without_slack(self):
        self.fill()
        code, out, _ = self.run_main("group", "Research", CHAN, OTHER_CHAN)
        self.assertEqual(code, 0)
        self.assertEqual(out, "Added to Research:\n"
                              f"  {CHAN}  Planning Team  project-planning\n"
                              f"  {OTHER_CHAN}  -              lab-notes\n"
                              "\n"
                              "Created group Research. Research now has "
                              "2 conversations.\n")
        self.assertEqual(self.members(), [CHAN, OTHER_CHAN])

    def test_everything_already_in_the_group_says_no_changes(self):
        self.fill()
        self.run_main("group", "Research", CHAN, OTHER_CHAN)
        before = self.path.read_text()
        code, out, _ = self.run_main("group", "Research", CHAN, OTHER_CHAN)
        self.assertEqual(code, 0)
        self.assertEqual(out, "Already in Research:\n"
                              f"  {CHAN}  Planning Team  project-planning\n"
                              f"  {OTHER_CHAN}  -              lab-notes\n"
                              "\n"
                              "No changes - Research still has 2 conversations.\n")
        self.assertEqual(self.path.read_text(), before)

    def test_adding_never_removes_members(self):
        self.fill()
        self.run_main("group", "Research", CHAN)
        self.run_main("group", "Research", OTHER_CHAN)
        self.assertEqual(self.members(), [CHAN, OTHER_CHAN])

    def test_links_work_and_a_repeat_is_handled_once(self):
        self.fill()
        link = f"https://example.slack.com/archives/{CHAN}"
        code, out, _ = self.run_main("group", "Research", link, CHAN)
        self.assertEqual(code, 0)
        self.assertEqual(out.count(CHAN), 1)
        self.assertEqual(self.members(), [CHAN])

    def test_a_word_that_is_not_an_id_cancels_everything_locally(self):
        # D48: usually a group name with a space, typed without quotes. Checked
        # before the Keychain or Slack: GONE would need Slack, and none is patched.
        self.fill()
        before = self.path.read_text()
        code, _, err = self.run_main("group", "Lab", "Notes", CHAN, self.GONE)
        self.assertEqual(code, 1)
        self.assertEqual(err, "Nothing was changed: Notes is not a conversation ID.\n"
                              'A group name with spaces needs quotes: '
                              'slack-export group "Lab Notes" C0123456789')
        self.assertEqual(self.path.read_text(), before)

    def test_several_non_ids_are_named_together(self):
        self.fill()
        _, _, err = self.run_main("group", "Big", "Lab", "Notes", CHAN)
        self.assertTrue(err.startswith("Nothing was changed: Lab, Notes are not "
                                       "conversation IDs.\n"))

    def test_an_id_where_the_name_goes_is_refused(self):
        self.fill()
        code, _, err = self.run_main("group", CHAN, OTHER_CHAN)
        self.assertEqual(code, 1)
        self.assertIn("is a conversation ID, not a group name", err)
        self.assertNotIn("groups", self.stored())

    def test_names_are_exact(self):
        self.fill()
        self.run_main("group", "tests")
        _, out, _ = self.run_main("group", "TESTS")
        self.assertEqual(out, "Created group TESTS (empty).\n")
        self.assertEqual(sorted(self.stored()["groups"]), ["TESTS", "tests"])

    def test_indexed_in_two_workspaces_is_not_guessed(self):
        store = cd.empty()
        cd.set_slack_name(store, TEAM, "Example", CHAN, "one")
        cd.set_slack_name(store, OTHER_TEAM, "Another", CHAN, "two")
        ec.save_directory(store, self.path)
        code, out, _ = self.run_main("group", "Research", CHAN)
        self.assertEqual(code, 1)
        self.assertIn(f"Could not add:\n  {CHAN}  in the conversation index for "
                      "more than one workspace", out)
        self.assertNotIn("groups", self.stored())

    def test_list_does_not_show_groups(self):
        self.fill()
        _, before, _ = self.run_main("list")
        self.run_main("group", "Research", CHAN)
        _, after, _ = self.run_main("list")
        self.assertEqual(after, before)

    # -- looking up what is not indexed --------------------------------------

    def test_unindexed_is_looked_up_indexed_and_added(self):
        self.fill()
        code, out, _ = self.run_group("Research", "C0000000005")
        self.assertEqual(code, 0)
        self.assertEqual(self.slack.calls,
                         ["auth.test", "conversations.info C0000000005"])
        entry = self.stored()["workspaces"][TEAM]["channels"]["C0000000005"]
        self.assertEqual(entry, {"slack_name": "field-notes",
                                 "kind": "public_channel"})
        self.assertEqual(self.members(), ["C0000000005"])
        self.assertEqual(out, "Added to Research:\n"
                              "  C0000000005  -  field-notes\n"
                              "\n"
                              "Created group Research. Research now has "
                              "1 conversation.\n")

    def test_works_with_an_empty_index(self):
        code, _, _ = self.run_group("Research", "C0000000005")
        self.assertEqual(code, 0)
        self.assertEqual(self.members(), ["C0000000005"])

    def test_a_new_dm_gets_no_invented_nickname(self):
        self.run_group("Research", "D0000000003")
        entry = self.stored()["workspaces"][TEAM]["channels"]["D0000000003"]
        self.assertEqual(entry, {"kind": "dm"})

    def test_several_new_ones_share_one_slack_check(self):
        code, _, _ = self.run_group("Research", "D0000000003", "D0000000004",
                                    "C0000000005")
        self.assertEqual(code, 0)
        self.assertEqual(self.slack.calls.count("auth.test"), 1)
        self.assertEqual(self.members(),
                         ["D0000000003", "D0000000004", "C0000000005"])

    def test_indexed_ones_are_not_looked_up_again(self):
        self.fill()
        self.run_group("Research", CHAN, "C0000000005")
        self.assertEqual(self.slack.calls,
                         ["auth.test", "conversations.info C0000000005"])

    def test_not_found_is_added_nowhere(self):
        self.fill()
        before = self.path.read_text()
        code, out, _ = self.run_group("Research", self.GONE)
        self.assertEqual(code, 1)
        self.assertEqual(out, "Could not add:\n"
                              f"  {self.GONE}  conversation not found or not "
                              "accessible\n"
                              "\n"
                              "No changes - group Research was not created.\n")
        self.assertEqual(self.path.read_text(), before)

    def test_mixed_new_already_and_not_found(self):
        # Partial success (D45): what can be added is, and stays added.
        self.fill()
        self.run_main("group", "Research", CHAN)
        code, out, _ = self.run_group("Research", "D0000000003", CHAN, self.GONE)
        self.assertEqual(code, 1)
        self.assertEqual(out, "Added to Research:\n"
                              "  D0000000003  -              (DM)\n"
                              "\n"
                              "Already in Research:\n"
                              f"  {CHAN}  Planning Team  project-planning\n"
                              "\n"
                              "Could not add:\n"
                              f"  {self.GONE}  conversation not found or not "
                              "accessible\n"
                              "\n"
                              "Research now has 2 conversations.\n")
        self.assertEqual(self.members(), [CHAN, "D0000000003"])
        self.assertNotIn(self.GONE, self.stored()["workspaces"][TEAM]["channels"])

    def test_other_slack_errors_are_reported_as_they_are(self):
        slack = FakeSlack(self.CONVERSATIONS)
        slack.conversations_info = mock.Mock(side_effect=ec.SlackApiError(
            "x", {"error": "ratelimited"}))
        code, out, _ = self.run_group("Research", "C0000000005", slack=slack)
        self.assertEqual(code, 1)
        self.assertIn("C0000000005  Slack could not look it up (ratelimited)", out)

    def test_read_only_check_still_guards_group(self):
        slack = FakeSlack(self.CONVERSATIONS, scopes=GOOD_SCOPES + ",chat:write")
        code, _, err = self.run_group("Research", "C0000000005", slack=slack)
        self.assertEqual(code, 1)
        self.assertIn("unexpected scope", err)
        self.assertEqual(slack.calls, ["auth.test"])
        self.assertFalse(self.path.exists())

    def test_a_failed_write_leaves_neither_index_nor_group_changed(self):
        # The new index entry and its group membership are one write: if it
        # fails, the file on disk is exactly as it was.
        self.fill()
        before = self.path.read_text()
        with mock.patch.object(ec, "write_atomic", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.run_group("Research", "C0000000005")
        self.assertEqual(self.path.read_text(), before)


class SaveCommandTests(CommandTest):

    CONVERSATIONS = {
        CHAN: {"id": CHAN, "name": "project-planning", "is_private": True},
        "D0000000003": {"id": "D0000000003", "is_im": True, "user": "U0000000002"},
        "C0000000004": {"id": "C0000000004", "name": "mpdm-ahandle--bhandle-1",
                        "is_mpim": True, "is_private": True},
    }

    def run_save(self, *argv, slack=None):
        self.slack = slack or FakeSlack(self.CONVERSATIONS)
        with mock.patch.object(ec, "connect", return_value=self.slack):
            return self.run_main("save", *argv)

    def stored(self, channel=CHAN):
        return json.loads(self.path.read_text())["workspaces"][TEAM]["channels"][channel]

    def test_saves_slack_name_and_kind_quietly(self):
        code, out, _ = self.run_save(CHAN)
        self.assertEqual(code, 0)
        self.assertEqual(out, f"{CHAN}  project-planning  (private channel)\n")
        self.assertEqual(self.stored(), {"slack_name": "project-planning",
                                         "kind": "private_channel"})
        self.assertEqual(mode(self.path), 0o600)

    def test_nickname_words_are_joined_and_reported(self):
        _, out, _ = self.run_save(CHAN, "Planning", "Team")
        self.assertEqual(out, f"{CHAN}  project-planning  (private channel)\n"
                              f"  nickname: Planning Team  (added)\n")
        self.assertEqual(self.stored()["nickname"], "Planning Team")

    def test_bare_save_keeps_the_existing_nickname(self):
        self.run_save(CHAN, "Planning Team")
        _, out, _ = self.run_save(CHAN)
        self.assertEqual(out, f"{CHAN}  project-planning  (private channel)\n"
                              f"  nickname: Planning Team  (unchanged)\n")
        self.assertEqual(self.stored()["nickname"], "Planning Team")

    def test_new_nickname_replaces_the_old(self):
        self.run_save(CHAN, "Old")
        _, out, _ = self.run_save(CHAN, "New")
        self.assertIn("nickname: New  (changed)", out)

    def test_duplicate_nickname_is_refused_and_nothing_is_saved(self):
        self.run_save(CHAN, "Planning Team")
        before = self.path.read_text()
        code, _, err = self.run_save("D0000000003", "planning-team")
        self.assertEqual(code, 1)
        self.assertIn(f"already used by {CHAN} (project-planning)", err)
        self.assertEqual(self.path.read_text(), before)

    def test_bare_dm_is_saved_with_a_hint(self):
        _, out, _ = self.run_save("D0000000003")
        self.assertEqual(self.stored("D0000000003"), {"kind": "dm"})
        self.assertIn("D0000000003  -  (DM)", out)
        self.assertIn("slack-export save D0000000003 <nickname>", out)

    def test_named_group_dm_gets_no_nickname_hint(self):
        conversations = dict(self.CONVERSATIONS)
        conversations["C0000000004"] = dict(conversations["C0000000004"],
                                            name="Trip Planning")
        _, out, _ = self.run_save("C0000000004", slack=FakeSlack(conversations))
        self.assertEqual(out, "C0000000004  Trip Planning  (group DM)\n")

    def test_group_dm_kind_and_label(self):
        _, out, _ = self.run_save("C0000000004")
        self.assertEqual(out, "C0000000004  (unnamed group DM)  (group DM)\n"
                              "  No nickname yet. To give it one:  "
                              "slack-export save C0000000004 <nickname>\n")
        self.assertEqual(self.stored("C0000000004")["kind"], "group_dm")

    def test_exported_before_but_no_nickname(self):
        # An export name is only ever a fallback for display; save leaves it alone.
        store = cd.empty()
        cd.record_export(store, TEAM, "Example", "D0000000003", "Some-Person", WHEN)
        ec.save_directory(store, self.path)
        _, out, _ = self.run_save("D0000000003")
        self.assertEqual(out, "D0000000003  Some-Person (from export)  (DM)\n"
                              "  No nickname yet. To give it one:  "
                              "slack-export save D0000000003 <nickname>\n")
        self.assertNotIn("nickname", self.stored("D0000000003"))
        self.assertEqual(self.stored("D0000000003")["exports"],
                         {"Some-Person": {"last_export_utc": WHEN}})

    def test_dm_with_nickname_shows_it_in_place_of_a_slack_name(self):
        _, out, _ = self.run_save("D0000000003", "Alex")
        self.assertEqual(out, "D0000000003  Alex (nickname)  (DM)\n"
                              "  nickname: Alex  (added)\n")

    def test_bad_id_fails_before_slack_or_the_store(self):
        code, _, err = self.run_save("not-an-id")
        self.assertEqual(code, 1)
        self.assertIn("is not a Slack conversation ID", err)
        self.assertEqual(self.slack.calls, [])
        self.assertFalse(self.path.exists())

    def test_unknown_conversation_saves_nothing(self):
        code, _, err = self.run_save("C0000000009")
        self.assertEqual(code, 1)
        self.assertIn("could not find C0000000009 (channel_not_found)", err)
        self.assertFalse(self.path.exists())

    def test_read_only_check_still_guards_save(self):
        slack = FakeSlack(self.CONVERSATIONS, scopes=GOOD_SCOPES + ",chat:write")
        code, _, err = self.run_save(CHAN, slack=slack)
        self.assertEqual(code, 1)
        self.assertIn("unexpected scope", err)
        self.assertEqual(slack.calls, ["auth.test"])
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()


# ── recording an export ──────────────────────────────────────────────────────

class RecordExportTests(TempStoreTest):

    AUTH = {"team_id": TEAM, "team": "Example"}
    CHANNEL = {"id": CHAN, "name": "project-planning", "is_private": True}
    DM = {"id": "D0000000003", "is_im": True, "user": "U0000000002"}

    def record(self, info, stem, when=WHEN):
        return ec.record_in_directory(self.AUTH, info, info["id"], stem, when,
                                      self.path)

    def stored(self, channel=CHAN):
        return json.loads(self.path.read_text())["workspaces"][TEAM]["channels"][channel]

    def test_named_export_records_name_kind_and_file(self):
        line = self.record(self.CHANNEL, "Planning")
        self.assertEqual(line, f"{CHAN}  project-planning")
        self.assertEqual(self.stored(), {
            "slack_name": "project-planning", "kind": "private_channel",
            "exports": {"Planning": {"last_export_utc": WHEN}}})
        self.assertEqual(mode(self.path), 0o600)

    def test_unnamed_export_is_recorded_under_its_id(self):
        line = self.record(self.DM, "D0000000003")
        self.assertEqual(line, "D0000000003  D0000000003 (from export)")
        self.assertEqual(self.stored("D0000000003")["exports"],
                         {"D0000000003": {"last_export_utc": WHEN}})

    def test_nickname_is_kept_and_shown(self):
        store = cd.empty()
        cd.set_nickname(store, TEAM, "Example", "D0000000003", "Alex")
        ec.save_directory(store, self.path)
        line = self.record(self.DM, "Some-Person")
        self.assertEqual(line, "D0000000003  Alex (nickname)")
        self.assertEqual(self.stored("D0000000003")["nickname"], "Alex")

    def test_a_top_up_moves_the_time_forward(self):
        self.record(self.CHANNEL, "Planning")
        self.record(self.CHANNEL, "Planning", LATER)
        self.assertEqual(self.stored()["exports"],
                         {"Planning": {"last_export_utc": LATER}})

    def test_damaged_store_is_a_warning_and_is_left_alone(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{ not json")
        line = self.record(self.CHANNEL, "Planning")
        self.assertTrue(line.startswith("NOT updated - the export itself is fine."))
        self.assertIn("is unreadable", line)
        self.assertNotIn("FATAL", line)
        self.assertEqual(self.path.read_text(), "{ not json")

    def test_store_git_could_commit_is_a_warning_and_not_written(self):
        subprocess.run(["git", "init", "-q", str(self.tmp)], check=True)
        line = self.record(self.CHANNEL, "Planning")
        self.assertTrue(line.startswith("NOT updated"))
        self.assertIn("inside a git repository", line)
        self.assertFalse(self.path.exists())
