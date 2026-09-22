"""Remember which conversation ID goes with which name, so the ID can be found
again without going back to Slack.

Everything here is pure: dicts in, dicts out, no Slack client and no file access,
so every rule can be tested without a network or a home directory.
export_conversation.py reads and writes the store; this module only decides what
goes in it and what comes back out of a search.

A conversation can have three kinds of name, and each is kept in its own place so
none ever overwrites another:

  nickname    - what the user chose with `slack-export save`. Only save sets it.
  slack_name  - what Slack calls it (conversations.info), refreshed whenever the
                tool talks to Slack about it, since a channel can be renamed.
  exports     - the file names it has been exported under, each with the time of
                its most recent export. One conversation can have several.

Conversation IDs are unique only within one workspace, so entries are grouped by
the workspace's team ID - the one auth.test returns - never by its name, which an
admin can change. With one saved token today there is one workspace in the store;
a second token later adds a second group, not a new layout.

    {
      "version": 1,
      "workspaces": {
        "T0123456789": {
          "name": "Example",
          "channels": {
            "C0123456789": {
              "nickname": "Project Planning",
              "slack_name": "project-planning",
              "exports": {
                "Project-Planning": {"last_export_utc": "2026-09-22T18:00:00Z"}
              }
            }
          }
        }
      }
    }

Every field of an entry is optional, and fields this version does not know are
kept as they are, so a later version can add one without breaking this one.

The store holds real channel names and IDs, so it lives outside every repository,
and none of it is ever written into an export.
"""

import re

VERSION = 1


def empty() -> dict:
    """A store with nothing in it - what a missing file means."""
    return {"version": VERSION, "workspaces": {}}


def validate(data) -> dict:
    """Return the store if it has the expected shape, else raise ValueError saying why.

    Checked on every load. A store that has been hand-edited into a shape this
    code does not understand must stop the run, not be quietly replaced with an
    empty one: that would look like it worked and throw away every saved name.
    """
    if not isinstance(data, dict):
        raise ValueError("the top level is not a JSON object")
    if data.get("version") != VERSION:
        raise ValueError(f"unsupported version {data.get('version')!r} "
                         f"(this tool reads version {VERSION})")
    workspaces = data.get("workspaces")
    if not isinstance(workspaces, dict):
        raise ValueError("'workspaces' is missing or not an object")
    for team_id, workspace in workspaces.items():
        if not isinstance(workspace, dict) or not isinstance(
                workspace.get("channels"), dict):
            raise ValueError(f"workspace {team_id} has no 'channels' object")
        for channel_id, entry in workspace["channels"].items():
            where = f"{channel_id} in workspace {team_id}"
            if not isinstance(entry, dict):
                raise ValueError(f"{where} is not an object")
            for field in ("nickname", "slack_name"):
                if field in entry and not isinstance(entry[field], str):
                    raise ValueError(f"{where} has a {field} that is not text")
            exports = entry.get("exports", {})
            if not isinstance(exports, dict) or not all(
                    isinstance(e, dict) for e in exports.values()):
                raise ValueError(f"{where} has an 'exports' that is not an object")
    return data


def _entry(data: dict, team_id: str, team_name: str, channel_id: str) -> dict:
    """The entry for `channel_id`, created empty if new.

    The workspace's display name is refreshed every time, since auth.test reports
    the current one and an admin can change it.
    """
    workspace = data["workspaces"].setdefault(team_id, {"channels": {}})
    workspace["name"] = team_name
    return workspace["channels"].setdefault(channel_id, {})


def set_nickname(data: dict, team_id: str, team_name: str, channel_id: str,
                 nickname: str) -> str:
    """Give `channel_id` the user's own name for it. Changes `data` in place.

    Returns "added" (no nickname before), "changed", or "unchanged", so the caller
    can say what happened.
    """
    nickname = nickname.strip()
    if not nickname:
        raise ValueError("a nickname cannot be empty")
    entry = _entry(data, team_id, team_name, channel_id)
    before = entry.get("nickname")
    entry["nickname"] = nickname
    if before is None:
        return "added"
    return "unchanged" if before == nickname else "changed"


def set_slack_name(data: dict, team_id: str, team_name: str, channel_id: str,
                   slack_name: str) -> None:
    """Record what Slack currently calls `channel_id`. Blank is ignored, never stored."""
    entry = _entry(data, team_id, team_name, channel_id)
    if slack_name and slack_name.strip():
        entry["slack_name"] = slack_name.strip()


def record_export(data: dict, team_id: str, team_name: str, channel_id: str,
                  stem: str, when_utc: str) -> None:
    """Note that `channel_id` was just exported to files named `stem`."""
    entry = _entry(data, team_id, team_name, channel_id)
    entry.setdefault("exports", {}).setdefault(stem, {})["last_export_utc"] = when_utc


def _words(text: str) -> str:
    """Text reduced to lower-case words separated by single spaces.

    So that "project planning", "Project-Planning" and "project-planning"
    all match each other: Slack names and file names use
    hyphens where a person types spaces.
    """
    return " ".join(re.split(r"[\W_]+", text.casefold())).strip()


def _latest_export(exports: dict) -> str:
    """The file name of the most recent export, or "" if there has been none."""
    if not exports:
        return ""
    return max(exports, key=lambda stem: (exports[stem].get("last_export_utc", ""),
                                          stem))


def search(data: dict, term: str = "") -> list:
    """Every entry with `term` in what `list` shows for it: ID, Slack channel name,
    or nickname - including a nickname filled in from an export name.

    Case, spaces and punctuation are ignored. An empty term matches everything.
    Matching the ID as well as the names is what makes one search work in both
    directions - a name finds its ID, and an ID (or the start of one) finds its
    names.

    Only DISPLAYED text is searched, taken from the same COLUMNS functions that
    print it, so a line can never match on something it does not show. That is
    why older export names and the handles inside an unnamed group DM's Slack
    name are not searched: nothing shows them yet.

    Returned as flat rows carrying every field, unsorted: choosing the order and
    the columns is left to sort_rows and format_rows, so a new sort order or a new
    column never means changing the search.
    """
    needle = _words(term)
    rows = []
    for team_id, workspace in data["workspaces"].items():
        for channel_id, entry in workspace["channels"].items():
            exports = entry.get("exports", {})
            times = [e.get("last_export_utc", "") for e in exports.values()]
            row = {"team_id": team_id,
                   "team_name": workspace.get("name", team_id),
                   "channel_id": channel_id,
                   "nickname": entry.get("nickname", ""),
                   "slack_name": entry.get("slack_name", ""),
                   "export_names": sorted(exports),
                   "latest_export_name": _latest_export(exports),
                   "last_export_utc": max(times, default="")}
            if any(needle in _words(COLUMNS[column](row)) for column in SEARCHED):
                rows.append(row)
    return rows


# Slack names a group DM nobody has named "mpdm-<handle>--<handle>--...-1". It
# identifies no one at a glance and makes the column very wide, so list shows a
# label instead. A group DM that has been given a name comes back without the
# prefix and is shown as it is.
UNNAMED_GROUP_DM_PREFIX = "mpdm-"
UNNAMED_GROUP_DM = "[unnamed group DM]"


# Marks a name taken from an export file rather than from Slack or the user.
FROM_EXPORT = " (from export)"


def _export_fallback(row: dict) -> str:
    """The most recent export name, marked, or "" if never exported.

    The marker is the only thing showing that neither Slack nor the user gave this
    name, so it must never be dropped. It is worked out each time and never
    written to the store: a nickname is only ever set by `save`.
    """
    return row["latest_export_name"] + FROM_EXPORT if row["latest_export_name"] else ""


def shown_slack_name(row: dict) -> str:
    """The Slack channel name as list prints it.

    A 1:1 DM has no Slack name at all, so without a fallback it would be a bare ID.
    It shows its most recent export name instead, marked "(from export)".
    """
    if row["slack_name"].startswith(UNNAMED_GROUP_DM_PREFIX):
        return UNNAMED_GROUP_DM
    return row["slack_name"] or _export_fallback(row)


def shown_nickname(row: dict) -> str:
    """The user's nickname, or else the most recent export name, marked - unless
    that only repeats what the Slack channel name column already shows.

    It repeats it when there is no Slack name (the first column already fell back
    to the same export name), or when the export name and the Slack name are the
    same words. The NAMES are compared, not the displayed text, which would always
    differ by the marker.
    """
    if row["nickname"]:
        return row["nickname"]
    if not row["slack_name"] or (_words(row["latest_export_name"])
                                 == _words(row["slack_name"])):
        return ""
    return _export_fallback(row)


# The columns `list` can print, by name, each a function from one row to its text.
COLUMNS = {
    "id": lambda row: row["channel_id"],
    "slack_name": shown_slack_name,
    "nickname": shown_nickname,
}
DEFAULT_COLUMNS = ("id", "slack_name")
WITH_NICKNAMES = ("id", "slack_name", "nickname")
# The columns a search looks in. Every one of them must be on screen whenever a
# search runs, or a line could match for a reason it does not show: list_command
# prints WITH_NICKNAMES for any search.
SEARCHED = WITH_NICKNAMES


# The orders `list` can print in. Each is a sort key over one row. Later keys
# break ties, so the output never depends on the order the store happens to be in.
SORT_KEYS = {
    "slack_name": lambda row: (_words(shown_slack_name(row) or shown_nickname(row)),
                               _words(shown_nickname(row)), row["channel_id"]),
}
DEFAULT_SORT = "slack_name"


def sort_rows(rows: list, by: str = DEFAULT_SORT) -> list:
    """The rows in the named order. An unknown name is a programming error."""
    return sorted(rows, key=SORT_KEYS[by])


# What an empty cell prints as. Never blank: with columns separated only by spaces,
# a blank cell would make the next column look as if it were in this one.
BLANK = "-"


def format_rows(rows: list, columns=DEFAULT_COLUMNS) -> list:
    """One line of text per row, the chosen columns in order.

    Every column but the last is padded to its widest value, so the columns line
    up, and no line ends in spaces. Two spaces between columns, since a name can
    itself contain single spaces. An empty cell prints as BLANK.
    """
    cells = [[COLUMNS[column](row) or BLANK for column in columns] for row in rows]
    widths = [max((len(line[i]) for line in cells), default=0)
              for i in range(len(columns))]
    return ["  ".join([cell.ljust(width) for cell, width in zip(line[:-1], widths)]
                      + line[-1:])
            for line in cells]
