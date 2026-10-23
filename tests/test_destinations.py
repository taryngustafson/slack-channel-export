"""Tests for destinations.py: where a group export writes each conversation.

Run from the project folder:

    ./.venv/bin/python -m unittest discover -s tests -v

Each test builds a temporary exports folder with invented raw archives and
transcripts, so nothing real is read. Every ID and name here is invented.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import destinations as dest  # noqa: E402  (needs the sys.path line above)

ALPHA = "C0000000001"
BRAVO = "C0000000002"


class DestinationTest(unittest.TestCase):
    """A temporary home with exports/, exports/raw/ and a Desktop folder."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # Resolved, so a folder compares equal to the one the code finds.
        self.home = Path(self._tmp.name).resolve()
        self.exports = self.home / "exports"
        self.raw = self.exports / "raw"
        self.desktop = self.home / "Desktop"
        for folder in (self.raw, self.desktop):
            folder.mkdir(parents=True)

    def archive(self, stem, conversation_id, rendered=(), pending=()):
        """Write raw/<stem>.raw.json recording `rendered` and `pending` paths."""
        record = {"conversation_id": conversation_id,
                  "rendered": {str(p): "1787000000.000100" for p in rendered},
                  "pending": {str(p): {"through": "1787000000.000100", "run": 2,
                                       "sha256": "0" * 64} for p in pending},
                  "messages": []}
        (self.raw / f"{stem}.raw.json").write_text(json.dumps(record))

    def transcripts(self, folder, stem, kinds=(".md", ".txt")):
        """Create <folder>/<stem>.md and .txt; return both paths."""
        paths = [folder / f"{stem}{kind}" for kind in (".md", ".txt")]
        for path in paths:
            if path.suffix in kinds:
                path.write_text("transcript\n")
        return paths

    def plan(self, conversation_id, out_dir=None):
        copies = dest.recorded_copies(self.raw)
        return dest.destinations(conversation_id, copies, self.exports, out_dir)

    def places(self, conversation_id, out_dir=None):
        """The plan as (stem, folder, fresh) tuples, for short assertions."""
        return [(d["stem"], d["folder"], d["fresh"])
                for d in self.plan(conversation_id, out_dir)]


class ExistingCopyTests(DestinationTest):

    def test_a_copy_in_the_default_folder_is_updated_there(self):
        self.archive(ALPHA, ALPHA, self.transcripts(self.exports, ALPHA))
        self.assertEqual(self.places(ALPHA), [(ALPHA, self.exports, None)])

    def test_a_copy_saved_with_out_is_updated_where_it_is(self):
        self.archive("Notes", ALPHA, self.transcripts(self.desktop, "Notes"))
        self.assertEqual(self.places(ALPHA), [("Notes", self.desktop, None)])

    def test_copies_in_two_folders_are_both_updated(self):
        self.archive("Notes", ALPHA,
                     self.transcripts(self.exports, "Notes")
                     + self.transcripts(self.desktop, "Notes"))
        self.assertEqual(self.places(ALPHA), [("Notes", self.exports, None),
                                              ("Notes", self.desktop, None)])

    def test_copies_under_two_names_are_both_updated(self):
        self.archive(ALPHA, ALPHA, self.transcripts(self.exports, ALPHA))
        self.archive("Notes", ALPHA, self.transcripts(self.desktop, "Notes"))
        self.assertCountEqual(self.places(ALPHA), [(ALPHA, self.exports, None),
                                                   ("Notes", self.desktop, None)])

    def test_one_surviving_file_of_the_pair_still_counts(self):
        md, txt = self.transcripts(self.desktop, "Notes", kinds=(".txt",))
        self.archive("Notes", ALPHA, [md, txt])
        self.assertEqual(self.places(ALPHA), [("Notes", self.desktop, None)])

    def test_only_the_surviving_folder_is_used(self):
        self.archive("Notes", ALPHA,
                     [self.home / "Moved" / "Notes.md", self.home / "Moved" / "Notes.txt"]
                     + self.transcripts(self.desktop, "Notes"))
        self.assertEqual(self.places(ALPHA), [("Notes", self.desktop, None)])

    def test_a_file_from_an_interrupted_run_counts(self):
        self.archive("Notes", ALPHA,
                     pending=self.transcripts(self.desktop, "Notes"))
        self.assertEqual(self.places(ALPHA), [("Notes", self.desktop, None)])

    def test_one_folder_recorded_under_two_spellings_counts_once(self):
        link = self.home / "Shortcut"
        link.symlink_to(self.desktop)
        real = self.transcripts(self.desktop, "Notes")
        self.archive("Notes", ALPHA, real + [link / "Notes.md"])
        self.assertEqual(len(self.places(ALPHA)), 1)

    def test_other_conversations_do_not_leak_in(self):
        self.archive(ALPHA, ALPHA, self.transcripts(self.exports, ALPHA))
        self.archive("Notes", BRAVO, self.transcripts(self.desktop, "Notes"))
        self.assertEqual(self.places(ALPHA), [(ALPHA, self.exports, None)])


class FreshCopyTests(DestinationTest):

    def test_never_exported_gets_a_fresh_copy_named_by_its_id(self):
        self.assertEqual(self.places(ALPHA),
                         [(ALPHA, self.exports, dest.NEVER_EXPORTED)])

    def test_every_copy_gone_gets_a_fresh_copy_in_the_default_folder(self):
        gone = self.home / "Deleted"
        self.archive("Notes", ALPHA, [gone / "Notes.md", gone / "Notes.txt"])
        self.assertEqual(self.places(ALPHA),
                         [(ALPHA, self.exports, dest.ALL_COPIES_GONE)])

    def test_a_raw_folder_that_does_not_exist_means_nothing_was_exported(self):
        copies = dest.recorded_copies(self.home / "nowhere" / "raw")
        self.assertEqual(copies, {})


class OutFolderTests(DestinationTest):

    def test_out_tops_up_the_copy_already_there_and_leaves_others(self):
        self.archive("Notes", ALPHA,
                     self.transcripts(self.exports, "Notes")
                     + self.transcripts(self.desktop, "Notes"))
        self.assertEqual(self.places(ALPHA, out_dir=self.desktop),
                         [("Notes", self.desktop, None)])

    def test_out_with_copies_only_elsewhere_gets_a_fresh_copy_there(self):
        self.archive("Notes", ALPHA, self.transcripts(self.exports, "Notes"))
        self.assertEqual(self.places(ALPHA, out_dir=self.desktop),
                         [(ALPHA, self.desktop, dest.NONE_IN_FOLDER)])

    def test_out_for_a_never_exported_conversation(self):
        self.assertEqual(self.places(ALPHA, out_dir=self.desktop),
                         [(ALPHA, self.desktop, dest.NEVER_EXPORTED)])

    def test_out_folder_that_does_not_exist_yet(self):
        new = self.home / "New Folder"
        self.archive("Notes", ALPHA, self.transcripts(self.exports, "Notes"))
        self.assertEqual(self.places(ALPHA, out_dir=new),
                         [(ALPHA, new, dest.NONE_IN_FOLDER)])
        self.assertFalse(new.exists())

    def test_out_reached_through_a_symlink_finds_the_copy(self):
        link = self.home / "Shortcut"
        link.symlink_to(self.desktop)
        self.archive("Notes", ALPHA, self.transcripts(self.desktop, "Notes"))
        self.assertEqual(self.places(ALPHA, out_dir=link),
                         [("Notes", self.desktop, None)])


class ArchiveReadingTests(DestinationTest):

    def test_an_unreadable_archive_stops_the_plan(self):
        (self.raw / "Broken.raw.json").write_text("{not json")
        with self.assertRaises(dest.UnreadableArchive) as caught:
            dest.recorded_copies(self.raw)
        self.assertEqual(caught.exception.path.name, "Broken.raw.json")

    def test_an_archive_without_a_conversation_id_stops_the_plan(self):
        (self.raw / "Odd.raw.json").write_text(json.dumps({"messages": []}))
        with self.assertRaises(dest.UnreadableArchive):
            dest.recorded_copies(self.raw)

    def test_an_archive_that_is_not_an_object_stops_the_plan(self):
        (self.raw / "List.raw.json").write_text("[]")
        with self.assertRaises(dest.UnreadableArchive):
            dest.recorded_copies(self.raw)

    def test_temporary_files_in_the_raw_folder_are_not_archives(self):
        (self.raw / "Notes.raw.json.partial").write_text("{half writ")
        self.assertEqual(dest.recorded_copies(self.raw), {})

    def test_an_archive_from_before_files_were_recorded(self):
        (self.raw / "Old.raw.json").write_text(
            json.dumps({"conversation_id": ALPHA, "messages": []}))
        self.assertEqual(self.places(ALPHA),
                         [(ALPHA, self.exports, dest.ALL_COPIES_GONE)])

    def test_nothing_is_written(self):
        self.archive("Notes", ALPHA, self.transcripts(self.desktop, "Notes"))
        before = sorted(os.walk(self.home))
        self.plan(ALPHA, out_dir=self.home / "New Folder")
        self.plan(BRAVO)
        self.assertEqual(sorted(os.walk(self.home)), before)


class ClashTests(unittest.TestCase):

    @staticmethod
    def at(stem, folder="exports"):
        return {"stem": stem, "folder": Path(folder), "fresh": None}

    def test_different_names_do_not_clash(self):
        self.assertEqual(dest.clashes({ALPHA: [self.at("Notes")],
                                       BRAVO: [self.at("Planning")]}), [])

    def test_two_conversations_under_one_name_clash(self):
        self.assertEqual(dest.clashes({ALPHA: [self.at("Notes")],
                                       BRAVO: [self.at("Notes", "Desktop")]}),
                         [("Notes", [ALPHA, BRAVO])])

    def test_names_differing_only_in_capitals_clash(self):
        found = dest.clashes({ALPHA: [self.at("Notes")],
                              BRAVO: [self.at("notes")]})
        self.assertEqual([ids for _, ids in found], [[ALPHA, BRAVO]])

    def test_one_conversation_in_two_folders_is_not_a_clash(self):
        self.assertEqual(dest.clashes({ALPHA: [self.at("Notes"),
                                               self.at("Notes", "Desktop")]}), [])


if __name__ == "__main__":
    unittest.main()
