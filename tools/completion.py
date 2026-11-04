"""Tab completion for slack-export, through the argcomplete package.

On every Tab the shell runs slack-export itself, with the line typed so far in
COMP_LINE and _ARGCOMPLETE set. export_conversation.main() hands that run to
complete() before doing anything else, and argcomplete prints the candidates and
exits. Nothing here writes a file, reads the Keychain or contacts Slack: the only
thing read is the conversation index.

Only what the tool accepts in that spot is offered: the command words, flags,
group names, folders for --out, conversation IDs already in the conversation
index, and, as the first word of an export only, saved nicknames (the one place
a nickname can stand in for an ID). Slack names are never offered. Matching is
case-sensitive, as `cd` is. argcomplete quotes what it inserts, so a nickname
with spaces arrives as one word.

slack-export has no single parser: the first word picks one (see main()), so
complete() does the same, then tells argcomplete to start reading one word later.

argcomplete is optional. Only this module imports it, and only on a Tab, so
without it slack-export works the same and Tab simply completes nothing.
install.sh installs it and registers the completion with the shell.
"""

import os
import re

import argcomplete
from argcomplete.completers import DirectoriesCompleter

# The parser builder for each command word, by name in export_conversation.
COMMAND_PARSERS = {"list": "_list_parser", "save": "_save_parser",
                   "group": "_group_parser"}


def complete(tool) -> None:
    """Answer one Tab and exit. `tool` is the export_conversation module, passed
    in because it runs as __main__ and would otherwise be imported twice."""
    line = os.environ.get("COMP_LINE", "")
    point = int(os.environ.get("COMP_POINT", len(line)))
    # The finished words before the one being typed; the first is slack-export.
    words = argcomplete.split_line(line, point)[3]
    typed = words[int(os.environ.get("_ARGCOMPLETE", "1")):]

    removing = typed[:1] == ["group"] and "--remove" in typed
    if removing:
        # The tool parses `group` intermixed, which argcomplete cannot: on Python
        # 3.9 plain parsing fills the IDs slot (empty) as soon as the name is
        # read, so after --remove no slot is left to complete. So --remove is
        # taken off the line argcomplete sees, and remembered here instead.
        before = re.sub(r"(?<!\S)--remove(?!\S)", "", line[:point], count=1)
        os.environ["COMP_LINE"] = before + line[point:]
        os.environ["COMP_POINT"] = str(len(before))
    if typed and typed[0] in COMMAND_PARSERS:
        parser = getattr(tool, COMMAND_PARSERS[typed[0]])()
        os.environ["_ARGCOMPLETE"] = str(int(os.environ["_ARGCOMPLETE"]) + 1)
    elif tool._wants_group_export(typed):
        parser = tool._group_export_parser()
    else:
        parser = tool._export_parser()
        # --group is its own parser (group_export), so the export parser does
        # not know it; added here so that `--gr` completes to it.
        parser.add_argument("--group", metavar="name",
                            help="export every conversation in a group")
    _attach_completers(parser, tool, removing)
    argcomplete.autocomplete(parser, always_complete_options=False,
                             default_completer=_nothing)


def _attach_completers(parser, tool, removing: bool) -> None:
    """Give each argument that takes a known kind of value its completer. Any
    other free text - a file name, a nickname, a search - completes nothing."""
    index = _Index(tool)
    completers = {
        "conversation_id": index.ids,
        "name": index.ids,          # an export's name slot also takes the next ID
        "group": index.group_names,
        "groups": index.group_names,
        "out": DirectoriesCompleter(),
    }
    if parser.prog == "slack-export group":
        completers.update(name=index.group_names,
                          conversations=index.in_group if removing
                          else index.not_in_group)
    elif parser.prog == "slack-export" and any(
            a.dest == "conversation_id" for a in parser._actions):
        completers["conversation_id"] = index.first_words
    for action in parser._actions:
        if action.dest in completers:
            action.completer = completers[action.dest]


def _nothing(**_) -> list:
    """For free text: offer nothing, rather than argcomplete's default of files."""
    return []


class _Index:
    """The conversation index, read once and only if a completer needs it. A
    missing or damaged index completes nothing instead of failing."""

    def __init__(self, tool):
        self._tool, self._store = tool, None

    def _read(self) -> dict:
        if self._store is None:
            try:
                self._store = self._tool.load_directory()
            except SystemExit:
                self._store = {"workspaces": {}, "groups": {}}
        return self._store

    def ids(self, **_) -> list:
        return sorted({channel for workspace in self._read()["workspaces"].values()
                       for channel in workspace.get("channels", {})})

    def group_names(self, **_) -> list:
        return sorted(self._read().get("groups", {}))

    def nicknames(self) -> list:
        """Saved nicknames that `slack-export NICKNAME` can use: read from the
        index on every Tab, so a renamed or removed one is gone at once. Every
        workspace's, since which one the token is for is only known by asking
        Slack, and Tab never does."""
        usable = {entry["nickname"]
                  for workspace in self._read()["workspaces"].values()
                  for entry in workspace.get("channels", {}).values()
                  if entry.get("nickname")
                  and not self._tool.nickname_problem(entry["nickname"])}
        return sorted(usable)

    def first_words(self, **_) -> list:
        """The first word of a command line: a command, or the conversation to
        export, by ID or by nickname."""
        return list(COMMAND_PARSERS) + self.ids() + self.nicknames()

    def _members(self, name) -> set:
        return {m["channel"] for m in self._read().get("groups", {}).get(name, [])}

    def in_group(self, parsed_args, **_) -> list:
        """What `group NAME --remove ID ...` can take: the group's own members."""
        return sorted(self._members(parsed_args.name or ""))

    def not_in_group(self, parsed_args, **_) -> list:
        """What `group NAME ID ...` can add: every indexed ID not in it yet.
        Nothing before the name: argcomplete offers both slots there, since the
        name is optional."""
        if not parsed_args.name:
            return []
        members = self._members(parsed_args.name)
        return [i for i in self.ids() if i not in members]
