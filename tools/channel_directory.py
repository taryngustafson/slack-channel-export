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

Each entry also records its kind - public or private channel, DM, or group DM -
from Slack's own answer, never from the ID: a group DM's ID starts with C, like a
channel's.

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
              "kind": "private_channel",
              "exports": {
                "Project-Planning": {"last_export_utc": "2026-09-22T18:00:00Z"}
              }
            }
          }
        }
      },
      "groups": {
        "Research": [{"workspace": "T0123456789", "channel": "C0123456789"}],
        "Ideas": []
      }
    }

Groups are the user's own collections of conversations, made with `slack-export
group`. A conversation can be in any number of groups and a group can be empty,
so groups are not stored on the entries: they are one table of their own beside
the workspaces, and each member names its workspace and channel, so one group can
hold conversations from several workspaces. Group names are exact - "tests" and
"TESTS" are two groups - unlike nicknames. A store written before groups existed
simply has no "groups" key, which means no groups.

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
            for field in ("nickname", "slack_name", "kind"):
                if field in entry and not isinstance(entry[field], str):
                    raise ValueError(f"{where} has a {field} that is not text")
            exports = entry.get("exports", {})
            if not isinstance(exports, dict) or not all(
                    isinstance(e, dict) for e in exports.values()):
                raise ValueError(f"{where} has an 'exports' that is not an object")
    groups = data.get("groups", {})
    if not isinstance(groups, dict):
        raise ValueError("'groups' is not an object")
    for name, members in groups.items():
        if not isinstance(members, list) or not all(
                isinstance(m, dict) and isinstance(m.get("workspace"), str)
                and isinstance(m.get("channel"), str) for m in members):
            raise ValueError(f"group '{name}' is not a list of "
                             f"{{workspace, channel}} members")
    return data


def _entry(data: dict, team_id: str, team_name: str, channel_id: str) -> dict:
    """The entry for `channel_id`, created empty if new.

    The workspace's display name is refreshed every time, since auth.test reports
    the current one and an admin can change it.
    """
    workspace = data["workspaces"].setdefault(team_id, {"channels": {}})
    workspace["name"] = team_name
    return workspace["channels"].setdefault(channel_id, {})


class NicknameTaken(ValueError):
    """The nickname already belongs to another conversation in the same workspace."""

    def __init__(self, nickname: str, owner_id: str, owner: dict):
        super().__init__(f"the nickname '{nickname}' is already used by {owner_id}")
        self.owner_id = owner_id
        self.owner = owner


def nickname_owner(data: dict, team_id: str, nickname: str):
    """The (ID, entry) already holding `nickname` in this workspace, or None.

    Compared the way search compares, so "Project Planning" and "project-planning"
    count as the same nickname: two that differ only in case or punctuation could
    never be told apart when typed back in. A nickname with no letters or digits
    at all, such as an emoji, is compared as typed, so two different ones are not
    mistaken for the same.
    """
    wanted = _name_key(nickname)
    workspace = data["workspaces"].get(team_id, {"channels": {}})
    for channel_id, entry in workspace["channels"].items():
        if entry.get("nickname") and _name_key(entry["nickname"]) == wanted:
            return channel_id, entry
    return None


def set_nickname(data: dict, team_id: str, team_name: str, channel_id: str,
                 nickname: str) -> str:
    """Give `channel_id` the user's own name for it. Changes `data` in place.

    Returns "added" (no nickname before), "changed", or "unchanged", so the caller
    can say what happened.

    A nickname means one conversation per workspace, so that it can later stand in
    for the ID. Raises NicknameTaken, changing nothing, if another conversation in
    the workspace already has it. Conversations in different workspaces may share
    one, as their IDs are separate too.
    """
    nickname = nickname.strip()
    if not nickname:
        raise ValueError("a nickname cannot be empty")
    owner = nickname_owner(data, team_id, nickname)
    if owner and owner[0] != channel_id:
        raise NicknameTaken(nickname, *owner)
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


# What kind of conversation an entry is, as stored and as shown to a person.
KINDS = {
    "public_channel": "public channel",
    "private_channel": "private channel",
    "dm": "DM",
    "group_dm": "group DM",
}


def kind_of(info: dict) -> str:
    """The kind of conversation, from Slack's conversations.info answer.

    Order matters: Slack also marks a group DM as private, so group DM is tested
    before private channel.
    """
    if info.get("is_im"):
        return "dm"
    if info.get("is_mpim"):
        return "group_dm"
    if info.get("is_private"):
        return "private_channel"
    return "public_channel"


def set_kind(data: dict, team_id: str, team_name: str, channel_id: str,
             kind: str) -> None:
    """Record what kind of conversation `channel_id` is."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    _entry(data, team_id, team_name, channel_id)["kind"] = kind


def record_export(data: dict, team_id: str, team_name: str, channel_id: str,
                  stem: str, when_utc: str) -> None:
    """Note that `channel_id` was just exported to files named `stem`."""
    entry = _entry(data, team_id, team_name, channel_id)
    entry.setdefault("exports", {}).setdefault(stem, {})["last_export_utc"] = when_utc


class NotSaved(ValueError):
    """The conversation is not in the conversation index, so it cannot join a group."""

    def __init__(self, team_id: str, channel_id: str):
        super().__init__(f"{channel_id} is not saved")
        self.team_id = team_id
        self.channel_id = channel_id


def _group_name(name: str) -> str:
    """The group name as stored: exact, apart from spaces at either end, which
    are never meant. Case and punctuation are kept - they may be deliberate."""
    name = name.strip()
    if not name:
        raise ValueError("a group name cannot be empty")
    return name


def create_group(data: dict, name: str) -> str:
    """Make an empty group called `name`. Changes `data` in place.

    Returns "created", or "exists" when there already is one - which is then left
    exactly as it was, so running this twice can never lose or duplicate members.
    """
    name = _group_name(name)
    groups = data.setdefault("groups", {})
    if name in groups:
        return "exists"
    groups[name] = []
    return "created"


def add_to_group(data: dict, name: str, team_id: str, channel_id: str) -> str:
    """Put an indexed conversation in group `name`, making the group if it is new.
    Changes `data` in place.

    Returns "added", or "already in" when it was a member already - adding only
    ever adds, never replaces. Raises NotSaved, changing nothing, when the
    conversation is not in the conversation index: every group member is also in
    the index, so the caller must add it there first (`group` does, via Slack).
    """
    name = _group_name(name)
    if channel_id not in data["workspaces"].get(team_id, {}).get("channels", {}):
        raise NotSaved(team_id, channel_id)
    members = data.setdefault("groups", {}).setdefault(name, [])
    member = {"workspace": team_id, "channel": channel_id}
    if member in members:
        return "already in"
    members.append(member)
    return "added"


def saved_in(data: dict, channel_id: str) -> list:
    """The team IDs of every workspace where `channel_id` is saved, sorted.

    Lets `group` skip Slack for anything already indexed: the conversation index
    already knows each conversation's workspace. Usually one; none means not saved; more than one is possible, since
    IDs are unique only within a workspace, and the caller must not guess.
    """
    return sorted(team_id for team_id, workspace in data["workspaces"].items()
                  if channel_id in workspace["channels"])


def _words(text: str) -> str:
    """Text reduced to lower-case words separated by single spaces.

    So that "project planning", "Project-Planning" and "project-planning"
    all match each other: Slack names and file names use
    hyphens where a person types spaces.
    """
    return " ".join(re.split(r"[\W_]+", text.casefold())).strip()


def _name_key(text: str) -> str:
    """What two names are compared by: their words, or, when they have none
    (only punctuation or emoji), the text itself ignoring case and outer spaces.

    Without that second part every such name would reduce to "" and match all
    the others.
    """
    return _words(text) or text.casefold().strip()


def _matches(term: str, text: str) -> bool:
    """Whether a search `term` is found in displayed `text`, as search matches.

    By words when the term has any, so case, spaces and punctuation are ignored.
    A term with no letters or digits (only punctuation or emoji) is looked for as
    typed instead, ignoring case - and one with nothing at all matches nothing,
    never everything.
    """
    needle = _words(term)
    if needle:
        return needle in _words(text)
    literal = term.casefold().strip()
    return bool(literal) and literal in text.casefold()


def _latest_export(exports: dict) -> str:
    """The file name of the most recent export, or "" if there has been none."""
    if not exports:
        return ""
    return max(exports, key=lambda stem: (exports[stem].get("last_export_utc", ""),
                                          stem))


def search(data: dict, term: str = None) -> list:
    """Every entry with `term` in what `list` shows for it: ID, Slack channel name,
    or nickname - including a nickname filled in from an export name.

    Case, spaces and punctuation are ignored. Leaving the term out (None) matches
    everything; a term that is given always has to match (see _matches), even if
    it is only punctuation.
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
                   "kind": entry.get("kind", ""),
                   "export_names": sorted(exports),
                   "latest_export_name": _latest_export(exports),
                   "last_export_utc": max(times, default="")}
            if term is None or any(_matches(term, COLUMNS[column](row))
                                   for column in SEARCHED):
                rows.append(row)
    return rows


# Slack names a group DM nobody has named "mpdm-<handle>--<handle>--...-1". It
# identifies no one at a glance and makes the column very wide, so list treats it
# as no name at all - falling back as a 1:1 DM does - and shows this label only
# when there is nothing to fall back to. A group DM that has been given a name
# comes back without the prefix and is shown as it is.
UNNAMED_GROUP_DM_PREFIX = "mpdm-"
UNNAMED_GROUP_DM = "(unnamed group DM)"


def is_unnamed_group_dm(row: dict) -> bool:
    """A group DM still carrying the name Slack generated for it.

    Checked on the kind as well as the prefix: an ordinary channel may really be
    called "mpdm-roadmap", and that is a name worth showing.
    """
    return (row["kind"] == "group_dm"
            and row["slack_name"].startswith(UNNAMED_GROUP_DM_PREFIX))


def real_slack_name(row: dict) -> str:
    """The Slack name, or "" when there is none worth showing: a 1:1 DM, or a
    group DM nobody has named."""
    return "" if is_unnamed_group_dm(row) else row["slack_name"]


# Marks a name taken from an export file rather than from Slack or the user.
FROM_EXPORT = " (from export)"

# Marks the user's nickname standing in for a Slack name that does not exist.
FROM_NICKNAME = " (nickname)"


def _export_fallback(row: dict) -> str:
    """The most recent export name, marked, or "" if never exported.

    The marker is the only thing showing that neither Slack nor the user gave this
    name, so it must never be dropped. It is worked out each time and never
    written to the store: a nickname is only ever set by `save`.
    """
    return row["latest_export_name"] + FROM_EXPORT if row["latest_export_name"] else ""


def shown_slack_name(row: dict) -> str:
    """The Slack channel name as list prints it.

    A 1:1 DM has no Slack name at all, and an unnamed group DM has none worth
    showing, so without a fallback either would be a bare ID. It shows the user's
    nickname instead, marked "(nickname)", or failing that its most recent export
    name, marked "(from export)". The nickname comes first because the user chose
    it; an export name is only whatever the file was called. An unnamed group DM
    with neither is labelled as one.
    """
    if real_slack_name(row):
        return row["slack_name"]
    if row["nickname"]:
        return row["nickname"] + FROM_NICKNAME
    if row["latest_export_name"]:
        return _export_fallback(row)
    return UNNAMED_GROUP_DM if is_unnamed_group_dm(row) else ""


def list_nickname(row: dict) -> str:
    """The nickname column of `list`: the user's nickname, or else the most recent
    export name in [brackets], or "" when there is neither.

    An export given no name is saved under the conversation ID, which would only
    repeat the ID column, so it counts as no name.

    The brackets are the only thing showing that the user did not choose this
    name - it is only whatever the file was called - so they must never be
    dropped. Worked out each time and never written to the store: a nickname is
    only ever set by `save`.
    """
    if row["nickname"]:
        return row["nickname"]
    if row["latest_export_name"] and row["latest_export_name"] != row["channel_id"]:
        return f"[{row['latest_export_name']}]"
    return ""


# What `list` shows in place of a Slack name for any DM without one: a 1:1 DM, or
# a group DM nobody has named. One label for both keeps the column quiet; the
# nickname column is what tells them apart.
DM_LABEL = "(DM)"


def list_slack_name(row: dict) -> str:
    """The Slack channel name column of `list`: only a name Slack gave it.

    A DM with no name worth showing is labelled as a DM, so the column never
    suggests the name is merely unknown. A 1:1 DM is known by its D prefix as well
    as its kind, in case an entry was recorded before its kind was.
    """
    if real_slack_name(row):
        return row["slack_name"]
    if (row["kind"] == "dm" or row["channel_id"].startswith("D")
            or is_unnamed_group_dm(row)):
        return DM_LABEL
    return ""


# The columns `list` can print, by name, each a function from one row to its text.
COLUMNS = {
    "id": lambda row: row["channel_id"],
    "nickname": list_nickname,
    "slack_name": list_slack_name,
}
DEFAULT_COLUMNS = ("id", "nickname", "slack_name")
# The columns a search looks in. Every one of them must be on screen whenever a
# search runs, or a line could match for a reason it does not show.
SEARCHED = DEFAULT_COLUMNS


# The orders `list` can print in. Each is a sort key over one row. Later keys
# break ties, so the output never depends on the order the store happens to be in.
SORT_KEYS = {
    # A conversation with no real Slack name (a DM, an unnamed group DM) sorts by
    # its nickname column, so it lands among the channels by the name the user
    # sees for it rather than under "(DM)".
    "slack_name": lambda row: (_words(real_slack_name(row) or list_nickname(row)),
                               _words(list_nickname(row)), row["channel_id"]),
}
DEFAULT_SORT = "slack_name"


def sort_rows(rows: list, by: str = DEFAULT_SORT) -> list:
    """The rows in the named order. An unknown name is a programming error."""
    return sorted(rows, key=SORT_KEYS[by])


# What an empty cell prints as. Never blank: with columns separated only by spaces,
# a blank cell would make the next column look as if it were in this one.
BLANK = "-"


# The heading printed above each column. Capitals, as `ps` and `docker ps` do, so
# the header cannot be mistaken for a saved conversation.
HEADERS = {
    "id": "ID",
    "nickname": "NICKNAME",
    "slack_name": "SLACK NAME",
}


def format_rows(rows: list, columns=DEFAULT_COLUMNS, header: bool = False) -> list:
    """One line of text per row, the chosen columns in order, after a header line
    naming the columns if `header` is set.

    Every column but the last is padded to its widest value, header included, so
    the columns line up, and no line ends in spaces. Two spaces between columns,
    since a name can itself contain single spaces. An empty cell prints as BLANK.
    """
    cells = [[COLUMNS[column](row) or BLANK for column in columns] for row in rows]
    if header:
        cells.insert(0, [HEADERS[column] for column in columns])
    widths = [max((len(line[i]) for line in cells), default=0)
              for i in range(len(columns))]
    return ["  ".join([cell.ljust(width) for cell, width in zip(line[:-1], widths)]
                      + line[-1:])
            for line in cells]
