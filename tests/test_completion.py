"""Tests for tab completion: what slack-export offers when the shell asks.

Run from the project folder:

    ./.venv/bin/python -m unittest discover -s tests -v

Each test runs the real tool the way the shell does on a Tab: with the line
typed so far in COMP_LINE and _ARGCOMPLETE set, reading the candidates from the
file named by _ARGCOMPLETE_STDOUT_FILENAME. HOME is a temporary folder holding
an invented conversation index, so the real one is never read.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "export_conversation.py"

INDEX = {
    "version": 1,
    "workspaces": {"T0000000001": {"name": "Example", "channels": {
        "C0000000001": {"slack_name": "project-planning", "nickname": "Planning"},
        "C0000000002": {"slack_name": "lab-notes"},
        "D0000000003": {"kind": "dm"}}}},
    "groups": {
        "Contacts": [{"workspace": "T0000000001", "channel": "C0000000001"}],
        "Lab Notes": [],
        "Research": [{"workspace": "T0000000001", "channel": "C0000000002"}]},
}
ALL_IDS = ["C0000000001", "C0000000002", "D0000000003"]
GROUPS = ["Contacts", "Lab\\ Notes", "Research"]   # as bash is given them


class CompletionTest(unittest.TestCase):
    """A temporary HOME with the invented index. Holds no tests."""

    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name).resolve()
        self.home = self.tmp / "home"
        self.index = self.home / ".config" / "slack-export" / "channels.json"
        self.index.parent.mkdir(parents=True)
        self.index.write_text(json.dumps(INDEX))
        self.work = self.tmp / "work"
        (self.work / "Reports").mkdir(parents=True)

    def env(self, line):
        return dict(os.environ, HOME=str(self.home), COMP_LINE=line,
                    COMP_POINT=str(len(line)), _ARGCOMPLETE="1",
                    _ARGCOMPLETE_SHELL="bash",
                    _ARGCOMPLETE_STDOUT_FILENAME=str(self.tmp / "offered"))

    def tab(self, line, python=None):
        """What pressing Tab after `line` offers, as a list."""
        offered = self.tmp / "offered"
        offered.write_text("")
        run = subprocess.run(python or [sys.executable, str(TOOL)],
                             env=self.env(line), cwd=self.work,
                             capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        return [w.strip() for w in offered.read_text().split("\013") if w.strip()]


class WhatTabOffersTests(CompletionTest):

    def test_first_word_is_a_command_an_id_or_a_nickname(self):
        self.assertEqual(self.tab("slack-export "),
                         ["list", "save", "group"] + ALL_IDS + ["Planning"])

    def test_commands_complete_from_their_start(self):
        self.assertEqual(self.tab("slack-export g"), ["group"])
        self.assertEqual(self.tab("slack-export l"), ["list"])

    def test_matching_is_case_sensitive(self):
        self.assertEqual(self.tab("slack-export G"), [])
        self.assertEqual(self.tab("slack-export --group con"), [])

    def test_flags_only_once_a_dash_is_typed(self):
        self.assertEqual(sorted(self.tab("slack-export --")),
                         ["--copy", "--group", "--help", "--no-threads", "--out"])
        self.assertEqual(self.tab("slack-export --gr"), ["--group"])

    def test_hidden_flags_are_never_offered(self):
        self.assertNotIn("--no-clipboard", self.tab("slack-export --"))
        self.assertNotIn("--nicknames", self.tab("slack-export list --"))

    def test_group_names_after_group_flag(self):
        self.assertEqual(self.tab("slack-export --group "), GROUPS)
        self.assertEqual(self.tab("slack-export --group Con"), ["Contacts"])
        self.assertEqual(self.tab("slack-export --group L"), ["Lab\\ Notes"])

    def test_group_command(self):
        self.assertEqual(self.tab("slack-export group "), GROUPS)
        self.assertEqual(self.tab("slack-export group --li"), ["--list"])
        self.assertEqual(self.tab("slack-export group --list "), GROUPS)

    def test_adding_offers_ids_not_in_the_group_yet(self):
        self.assertEqual(self.tab("slack-export group Research "),
                         ["C0000000001", "D0000000003"])
        self.assertEqual(self.tab("slack-export group 'Lab Notes' "), ALL_IDS)

    def test_removing_offers_only_the_groups_members(self):
        self.assertEqual(self.tab("slack-export group Research --remove "),
                         ["C0000000002"])
        self.assertEqual(self.tab("slack-export group Research --remove C"),
                         ["C0000000002"])
        self.assertEqual(self.tab("slack-export group Research --rem"), ["--remove"])

    def test_list_groups_and_sort_choices(self):
        self.assertEqual(self.tab("slack-export list --groups "), GROUPS)
        self.assertEqual(self.tab("slack-export list --sort-by "),
                         ["name", "id", "nickname", "export"])

    def test_ids_where_an_id_goes(self):
        self.assertEqual(self.tab("slack-export save "), ALL_IDS)
        self.assertEqual(self.tab("slack-export save C"),
                         ["C0000000001", "C0000000002"])
        # After an ID, the next word may be another ID to export.
        self.assertEqual(self.tab("slack-export C0000000001 "), ALL_IDS)

    def test_free_text_offers_nothing(self):
        """A new nickname, a file name or a search: nothing valid to offer,
        and never the files in the current folder."""
        self.assertEqual(self.tab("slack-export save C0000000001 "), [])
        self.assertEqual(self.tab("slack-export list "), [])
        self.assertEqual(self.tab("slack-export list Pla"), [])

    def test_out_offers_folders(self):
        self.assertEqual(self.tab("slack-export C0000000001 --out "), ["Reports/"])
        self.assertEqual(self.tab("slack-export --group Research --out R"),
                         ["Reports/"])

    def test_no_index_still_completes_commands(self):
        self.index.unlink()
        self.assertEqual(self.tab("slack-export "), ["list", "save", "group"])
        self.assertEqual(self.tab("slack-export --group "), [])

    def test_damaged_index_still_completes_commands(self):
        self.index.write_text("{ not json")
        self.assertEqual(self.tab("slack-export "), ["list", "save", "group"])

    def test_a_tab_never_runs_the_command(self):
        """A completed export line must not export: nothing written anywhere."""
        before = sorted(p.name for p in self.work.iterdir())
        self.tab("slack-export C0000000001 ")
        self.assertEqual(sorted(p.name for p in self.work.iterdir()), before)
        self.assertEqual(json.loads(self.index.read_text()), INDEX)


# Nicknames: two workspaces, a nickname with a space, one with an apostrophe, one
# shaped like an ID in lower case (usable), and one saved by hand as a real ID,
# which slack-export could never read as a nickname.
NICKNAME_INDEX = {
    "version": 1,
    "workspaces": {
        "T0000000001": {"name": "Example", "channels": {
            "C0000000001": {"nickname": "Planning"},
            "C0000000002": {"nickname": "C0000000009"},
            "C0000000005": {"nickname": "Gardening"},
            "D0000000003": {"nickname": "Alice Smith"}}},
        "T0000000002": {"name": "Other", "channels": {
            "D0000000004": {"nickname": "Bob's DMs"}}}},
    "groups": {"Research": [{"workspace": "T0000000001", "channel": "C0000000001"}]},
}
NICKNAME_IDS = ["C0000000001", "C0000000002", "C0000000005", "D0000000003",
                "D0000000004"]


class NicknameTabTests(CompletionTest):
    """A nickname is offered only where the export accepts one: its first word."""

    def setUp(self):
        super().setUp()
        self.index.write_text(json.dumps(NICKNAME_INDEX))

    def test_first_word_offers_usable_nicknames_quoted_for_the_shell(self):
        self.assertEqual(
            self.tab("slack-export "),
            ["list", "save", "group"] + NICKNAME_IDS
            + ["Alice\\ Smith", "Bob\\'s\\ DMs", "Gardening", "Planning"])

    def test_a_nickname_with_spaces_completes_as_one_word(self):
        self.assertEqual(self.tab("slack-export Al"), ["Alice\\ Smith"])
        self.assertEqual(self.tab("slack-export Bo"), ["Bob\\'s\\ DMs"])
        # Inside a quote the shell closes it, so the name comes back as saved.
        self.assertEqual(self.tab('slack-export "Al'), ["Alice Smith"])

    def test_a_reserved_nickname_is_not_offered(self):
        self.assertNotIn("C0000000009", self.tab("slack-export "))
        self.assertEqual(self.tab("slack-export C0000000009"), [])
        self.assertEqual(self.tab("slack-export G"), ["Gardening"])

    def test_case_sensitive_like_the_export(self):
        """D81: "A" and "a" are two nicknames; Tab keeps them apart too."""
        index = json.loads(json.dumps(NICKNAME_INDEX))
        channels = index["workspaces"]["T0000000001"]["channels"]
        channels["C0000000006"] = {"nickname": "A"}
        channels["C0000000007"] = {"nickname": "a"}
        self.index.write_text(json.dumps(index))
        self.assertEqual(self.tab("slack-export A"), ["A", "Alice\\ Smith"])
        self.assertEqual(self.tab("slack-export a"), ["a"])
        self.assertEqual(self.tab("slack-export plan"), [])

    def test_nowhere_but_the_first_word(self):
        # After the first word comes a file name or another ID: IDs only.
        self.assertEqual(self.tab("slack-export Planning "), NICKNAME_IDS)
        self.assertEqual(self.tab("slack-export C0000000001 Al"), [])
        self.assertEqual(self.tab("slack-export save "), NICKNAME_IDS)
        self.assertEqual(self.tab("slack-export save C0000000001 "), [])
        self.assertEqual(self.tab("slack-export group Research "),
                         ["C0000000002", "C0000000005", "D0000000003",
                          "D0000000004"])
        self.assertEqual(self.tab("slack-export --group "), ["Research"])

    def test_renamed_and_removed_nicknames_change_at_once(self):
        self.assertEqual(self.tab("slack-export Pl"), ["Planning"])
        index = json.loads(json.dumps(NICKNAME_INDEX))
        channels = index["workspaces"]["T0000000001"]["channels"]
        channels["C0000000001"]["nickname"] = "Roadmap"
        del channels["D0000000003"]["nickname"]
        self.index.write_text(json.dumps(index))
        self.assertEqual(self.tab("slack-export Pl"), [])
        self.assertEqual(self.tab("slack-export Ro"), ["Roadmap"])
        self.assertEqual(self.tab("slack-export Al"), [])


# Runs the tool as __main__ with `import argcomplete` made to fail, as in a
# .venv set up before tab completion existed.
WITHOUT_ARGCOMPLETE = [
    sys.executable, "-c",
    "import runpy, sys; sys.modules['argcomplete'] = None; "
    f"sys.path.insert(0, {str(TOOL.parent)!r}); sys.argv[0] = {str(TOOL)!r}; "
    f"runpy.run_path({str(TOOL)!r}, run_name='__main__')"]


class WithoutArgcompleteTests(CompletionTest):

    def test_the_tool_still_works(self):
        run = subprocess.run(WITHOUT_ARGCOMPLETE + ["list"], cwd=self.work,
                             env=dict(os.environ, HOME=str(self.home)),
                             capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("project-planning", run.stdout)

    def test_a_tab_completes_nothing_and_runs_nothing(self):
        run = subprocess.run(WITHOUT_ARGCOMPLETE, env=self.env("slack-export C0000000001 "),
                             cwd=self.work, capture_output=True, text=True)
        self.assertEqual(run.returncode, 1)
        self.assertEqual((run.stdout, run.stderr), ("", ""))
        self.assertEqual(sorted(p.name for p in self.work.iterdir()), ["Reports"])


REGISTER = Path(sys.executable).parent / "register-python-argcomplete"


@unittest.skipUnless(REGISTER.exists(), "argcomplete is not installed here")
class RealShellTests(CompletionTest):
    """The shell code install.sh writes, loaded by real shells."""

    def setUp(self):
        super().setUp()
        install = (ROOT / "install.sh").read_text()
        header = install.split("cat <<'EOF'\n", 1)[1].split("\nEOF\n", 1)[0]
        shellcode = subprocess.run([str(REGISTER), "slack-export"], check=True,
                                   capture_output=True, text=True).stdout
        self.completion = self.tmp / "slack-export-completion.sh"
        self.completion.write_text(header + "\n" + shellcode)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "slack-export").symlink_to(ROOT / "slack-export")
        self.path = f"{bin_dir}:/usr/bin:/bin"

    @unittest.skipUnless(shutil.which("bash"), "bash is not installed")
    def test_bash_completes_through_the_installed_command(self):
        script = f"""
            . "{self.completion}"
            COMP_LINE="slack-export --group Con"; COMP_POINT=${{#COMP_LINE}}
            COMP_WORDS=(slack-export --group Con); COMP_CWORD=2
            _python_argcomplete slack-export Con --group
            echo "${{COMPREPLY[*]}}"
        """
        run = subprocess.run(["bash", "--norc", "--noprofile", "-c", script],
                             env={"HOME": str(self.home), "PATH": self.path},
                             cwd=self.work, capture_output=True, text=True)
        self.assertEqual((run.stdout.strip(), run.stderr), ("Contacts", ""))

    @unittest.skipUnless(shutil.which("bash"), "bash is not installed")
    def test_bash_completes_a_nickname_with_a_space_as_one_word(self):
        self.index.write_text(json.dumps(NICKNAME_INDEX))
        script = f"""
            . "{self.completion}"
            COMP_LINE="slack-export Ali"; COMP_POINT=${{#COMP_LINE}}
            COMP_WORDS=(slack-export Ali); COMP_CWORD=1
            _python_argcomplete slack-export Ali slack-export
            printf '%s\\n' "${{COMPREPLY[@]}}"
        """
        run = subprocess.run(["bash", "--norc", "--noprofile", "-c", script],
                             env={"HOME": str(self.home), "PATH": self.path},
                             cwd=self.work, capture_output=True, text=True)
        # One word, escaped, then the space that moves on to the next word.
        self.assertEqual((run.stdout, run.stderr), ("Alice\\ Smith \n", ""))

    @unittest.skipUnless(shutil.which("zsh"), "zsh is not installed")
    def test_zsh_without_compinit_loads_it_without_errors(self):
        """A new Mac's ~/.zshrc never starts zsh's completion system, and
        argcomplete's compdef needs it: the file must start it itself."""
        script = f'. "{self.completion}" && whence -w compdef'
        run = subprocess.run(["zsh", "-f", "-c", script],
                             env={"HOME": str(self.home), "PATH": self.path},
                             cwd=self.work, capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stderr, "")
        self.assertIn("compdef: function", run.stdout)


if __name__ == "__main__":
    unittest.main()
