"""Work out where a group export writes each conversation.

Nothing here contacts Slack or writes a file. It reads the raw archives and checks
which files still exist; export_conversation.py does the exporting.

Every export leaves an archive in exports/raw/, named after the export
(`<name>.raw.json`), and the archive records the full path of every .md and .txt
it has written, including ones sent elsewhere with --out. So a conversation's
copies are found from that record, without guessing or searching.

The rules:

  1. Every recorded copy that still exists is updated, wherever it is.
  2. With --out DIR, only DIR counts: copies already in DIR are topped up, and
     copies elsewhere are left alone.
  3. A conversation with no copy left (or none in DIR) gets a fresh one in the
     default folder (or DIR), named by its ID, as `slack-export ID` would.
  4. Two different conversations may not share a file name, even one differing
     only in capitals: they would share one archive. Refused before anything is
     fetched.
"""

import json
import os
from pathlib import Path

# Why a destination is a fresh copy rather than an existing one. Shown in that
# conversation's line of the run, so nobody wonders why a file appeared.
NEVER_EXPORTED = "never exported before"
ALL_COPIES_GONE = "every earlier copy was moved or deleted"
NONE_IN_FOLDER = "no copy in this folder yet"


class UnreadableArchive(Exception):
    """An archive in the raw folder could not be read."""

    def __init__(self, path: Path, reason: str):
        super().__init__(f"{path} is unreadable ({reason})")
        self.path = path


def _folder_key(folder: Path):
    """What makes two spellings of a folder the same folder: the folder itself on
    disk, so /tmp and /private/tmp, a symlink and its target, or Desktop and
    desktop on a disk that ignores capitals all count once."""
    info = os.stat(folder)
    return info.st_dev, info.st_ino


def recorded_copies(raw_dir: Path) -> dict:
    """Every conversation with an archive in `raw_dir`, and its copies still on disk.

    Returns {conversation_id: {"stems": [...], "copies": [(stem, folder), ...]}}.
    "stems" names every archive the conversation has; "copies" lists each folder
    still holding that stem's .md or .txt, once, in the order first recorded.

    An unreadable archive raises UnreadableArchive rather than being skipped: if it
    belonged to a member of the group, skipping it would quietly write a fresh copy
    in place of updating the ones it records.
    """
    found = {}
    for archive_path in sorted(Path(raw_dir).glob("*.raw.json")):
        try:
            archive = json.loads(archive_path.read_text(encoding="utf-8"))
            conversation_id = archive["conversation_id"]
        except (OSError, UnicodeDecodeError, json.JSONDecodeError,
                KeyError, TypeError) as exc:
            raise UnreadableArchive(archive_path, str(exc) or type(exc).__name__)
        stem = archive_path.name[:-len(".raw.json")]
        entry = found.setdefault(conversation_id, {"stems": [], "copies": []})
        entry["stems"].append(stem)
        # "pending" holds writes an interrupted run was making; a file it names
        # that exists is a copy as surely as one in "rendered".
        recorded = (list(archive.get("rendered", {}))
                    + list(archive.get("pending", {})))
        seen = set()
        for written in recorded:
            folder = Path(written).parent
            if not Path(written).exists():
                continue
            key = _folder_key(folder)
            if key not in seen:
                seen.add(key)
                entry["copies"].append((stem, folder))
    return found


def destinations(conversation_id: str, copies: dict, default_dir: Path,
                 out_dir: Path = None) -> list:
    """Where to write `conversation_id`: a list of {"stem", "folder", "fresh"}.

    `copies` is what recorded_copies() returned. "fresh" is None for a copy that
    already exists, or the reason (one of the constants above) for a new one.
    """
    entry = copies.get(conversation_id, {"stems": [], "copies": []})
    existing = entry["copies"]
    if out_dir is not None:
        try:
            wanted = _folder_key(out_dir)
        except OSError:
            # Not created yet, so nothing can be in it.
            wanted = None
        existing = [(stem, folder) for stem, folder in existing
                    if _folder_key(folder) == wanted]
    if existing:
        return [{"stem": stem, "folder": folder, "fresh": None}
                for stem, folder in existing]
    if out_dir is not None and entry["copies"]:
        reason = NONE_IN_FOLDER
    elif entry["stems"]:
        reason = ALL_COPIES_GONE
    else:
        reason = NEVER_EXPORTED
    return [{"stem": conversation_id,
             "folder": out_dir if out_dir is not None else default_dir,
             "fresh": reason}]


def clashes(plan: dict) -> list:
    """Names that two different conversations in `plan` would both write under.

    `plan` is {conversation_id: [destination, ...]} from destinations(). Returns
    [(name, [conversation_id, ...]), ...], empty when every name has one owner.
    Compared ignoring capitals, as the single-export check does, so the same
    group means the same thing on every disk.
    """
    owners = {}
    spelled = {}
    for conversation_id, places in plan.items():
        for place in places:
            key = place["stem"].casefold()
            spelled.setdefault(key, place["stem"])
            ids = owners.setdefault(key, [])
            if conversation_id not in ids:
                ids.append(conversation_id)
    return [(spelled[key], ids) for key, ids in owners.items() if len(ids) > 1]
