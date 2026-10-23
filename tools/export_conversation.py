"""Export named Slack conversations - channels or DMs - with every thread expanded.

Fetches the top-level history, then makes one conversations.replies call per message
that has replies, so nothing stays hidden inside a collapsed thread. Resolves the
participants' names, renders threads inline, and writes three files: a plain text
transcript, a Markdown document, and Slack's raw response. With --copy, the
Markdown is also copied to the clipboard.

The two readable files land in exports/ unless --out sends them somewhere else. The
raw response always stays in exports/raw/: it is the archive the renderers replay to
reformat an old export without asking Slack for the history again, and the record of
how much of the conversation is already held.

Run it again on the same conversation under the same name and it tops the existing
files up instead of writing new ones - only what arrived since is fetched, and it is
appended under a dated rule. Text already on disk is never rewritten, so a message
someone edited last week keeps the wording it was archived with.

Read-only by construction: it refuses to start if Slack reports any scope on the
token beyond the read scopes it was built for, and it exports only the
conversations named on the command line - it never enumerates the workspace.

    slack-export C0123456789
    slack-export C0123456789 Project Planning   # name the output file
    slack-export C0123456789 D0123456789        # several, one after another
    slack-export C0123456789 Planning D0123456789 Alice   # ...each with a name
    slack-export D0123456789 --copy                # also copy the Markdown
    slack-export C0123456789 --out ~/Desktop        # put the .md and .txt there
    slack-export C0123456789                        # again later: append what is new

The conversation index remembers conversations, so an ID can be found again without
going back to Slack. After an export, slack-export also updates the conversation
index; `save` is for adding one without exporting it, or for giving it a nickname:

    slack-export save C0123456789                   # remember it
    slack-export save C0123456789 Planning Team     # ...with your own nickname
    slack-export list                   # each ID, your nickname, its Slack name
    slack-export list planning          # only those with this in an ID or a name
    slack-export list --sort-by export  # most recently exported first
    slack-export list --sort-by export --reverse    # ...or longest ago first
    slack-export list --sort-by nickname            # A to Z by your nicknames

Groups collect conversations under a name of your choosing. A conversation can be in
any number of groups, or none. One not yet in the conversation index is looked up in
Slack and added to it first:

    slack-export group Research                     # make an empty group
    slack-export group Research C0123456789 D0123456789   # add conversations to it
    slack-export group Research --remove C0123456789      # take one out again
    slack-export group Research --delete            # delete the group itself
    slack-export group Research --list              # the conversations in it
    slack-export group --list                       # your groups, with their sizes
    slack-export list --groups                      # your groups, with their sizes
    slack-export list --groups Research             # the conversations in one
    slack-export list --groups Research --search planning   # ...searched
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

# Same directory as this script, so a plain import works when run as a script.
import channel_directory
import destinations
import incremental
import render_markdown
import render_text

# Slack caps this at 1000 for internal apps; 200 is Slack's own recommendation.
PAGE_SIZE = 200

# A conversation ID: a channel (C), a DM (D), or a legacy private channel or group
# DM (G), followed by uppercase letters and digits. Slack never uses lower case.
CONVERSATION_ID = re.compile(r"^[CDG][A-Z0-9]{7,}$")
# The same ID inside a link: .../archives/C0123456789, with or without a message
# or thread on the end, and the slack://channel?...&id=C0123456789 form.
ID_IN_LINK = re.compile(r"(?:archives/|[?&]id=)([CDG][A-Z0-9]{7,})")
# What Slack puts on the clipboard when a channel mention is copied.
ID_IN_MARKUP = re.compile(r"^<#([CDG][A-Z0-9]{7,})(?:\|[^>]*)?>$")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EXPORT_DIR = PROJECT_ROOT / "exports"
# The raw responses live one level down so the folder a person actually browses
# shows only the two readable formats. Kept inside the project, and never moved by
# --out, so a re-run can always find what was exported before.
RAW_DIR = EXPORT_DIR / "raw"

# Long enough for a real channel name, short enough that Finder still shows the
# timestamp that follows it.
NAME_MAX = 60

# Exports hold private conversations, so every file this tool writes is readable by
# its owner only, and so are the project's own export folders. A directory chosen
# with --out belongs to the user and its permissions are left exactly as they are.
PRIVATE_FILE = 0o600
PRIVATE_DIR = 0o700

# The remembered ID <-> name pairs. State about the user and their workspaces, not
# output, so it lives with the user's settings rather than in exports/ - where it
# would split in two under --out and vanish with the project folder. Outside the
# project, so git never sees it; save_directory still checks, for the people who
# keep ~/.config itself in a dotfiles repository.
DIRECTORY_PATH = Path.home() / ".config" / "slack-export" / "channels.json"


def keychain_token(service: str = "SLACK_USER_TOKEN") -> str:
    """Read the Slack user token out of the macOS Keychain."""
    result = subprocess.run(
        ["security", "find-generic-password",
         "-a", os.environ["USER"], "-s", service, "-w"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        sys.exit(f"FATAL: no Keychain entry '{service}' for account {os.environ['USER']}")
    return result.stdout.strip()


def parse_conversation_id(raw):
    """The conversation ID in whatever was typed, or None if there isn't one.

    Accepts the ID on its own, a Slack link to a channel or to a message inside
    one, and the <#C0123456789|name> form Slack copies for a channel mention -
    all easier to lay hands on than the ID buried in the About tab.

    Checked before the Keychain is read or Slack is called, so a mistyped ID costs
    nothing. It also catches the classic slip of leaving the command's own name
    among the arguments, which would otherwise be sent to Slack as a channel.
    """
    text = (raw or "").strip()
    if not text:
        return None
    markup = ID_IN_MARKUP.match(text)
    if markup:
        return markup.group(1)
    if CONVERSATION_ID.match(text):
        return text
    link = ID_IN_LINK.search(text)
    return link.group(1) if link else None


def safe_stem(name: str) -> str:
    """Turn a name typed on the command line into a safe filename stem.

    Anything that is not a letter or a digit becomes a hyphen - spaces, slashes,
    colons, emoji - so the result is safe on any filesystem and still readable:
    "Project Planning" -> "Project-Planning". Returns "" if nothing usable
    is left, which tells the caller to fall back to the conversation ID.
    """
    return re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-")[:NAME_MAX].strip("-")


def name_note(typed: str, stem: str, conversation_id: str):
    """What the export's summary says about the file name, or None when it is
    exactly what was typed (or nothing was typed). A file name keeps only letters,
    digits and hyphens, so "Lab & Field" becomes Lab-Field - said outright, since
    the name a person typed is easy to mistake for the conversation's name (D56).
    """
    typed = typed.strip()
    if not typed or stem == typed:
        return None
    if stem == conversation_id:
        return (f'{stem}  ("{typed}" has no letters or digits, so the conversation '
                f'ID is used)')
    return f'{stem}  (from "{typed}": file names keep only letters, digits and hyphens)'


def display_path(path: Path) -> str:
    """Render a path for the summary: short when it is somewhere familiar.

    Path.relative_to RAISES when the path is not under the base - it does not fall
    back to an absolute path - so printing the summary would crash after a perfectly
    good export if --out pointed outside the project. Try the project first, then the
    home directory, then give up and print it in full.
    """
    for base, prefix in ((PROJECT_ROOT, ""), (Path.home(), "~/")):
        try:
            return prefix + str(path.relative_to(base))
        except ValueError:
            continue
    return str(path)


def resolve_out_dir(raw_value) -> Path:
    """Turn --out into an absolute directory path, or exit saying why it is not one.

    Checked BEFORE any Slack call: a typo in the path should cost nothing, not
    surface after a few minutes of fetching. expanduser() matters because a quoted
    "~/Desktop" reaches Python as a literal tilde - the shell only expands it bare.

    Nothing is created here. create_out_dir does that once the git check has
    passed, so a refused --out leaves no empty folder behind.
    """
    if raw_value is None:
        return EXPORT_DIR

    out_dir = Path(raw_value).expanduser().resolve()
    if out_dir.exists() and not out_dir.is_dir():
        sys.exit(f"FATAL: --out {out_dir} exists but is not a directory.")
    return out_dir


def create_out_dir(out_dir: Path) -> None:
    """Make sure an --out directory exists and is writable. Its permissions are
    the user's and are not changed."""
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        sys.exit(f"FATAL: cannot use --out {out_dir}: {exc.strerror}")
    if not os.access(out_dir, os.W_OK):
        sys.exit(f"FATAL: --out {out_dir} is not writable.")


def make_private_dir(path: Path) -> None:
    """Create one of the project's own export folders, or tighten an existing one.

    Only ever called on exports/ and exports/raw/ - never on an --out directory.
    """
    path.mkdir(mode=PRIVATE_DIR, parents=True, exist_ok=True)
    os.chmod(path, PRIVATE_DIR)


class GitCheckFailed(Exception):
    """A git repository is present, but git could not say whether a path is ignored."""


def _git(cwd: Path, *args) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", "-C", str(cwd), *args],
                              capture_output=True, text=True)
    except OSError as exc:
        raise GitCheckFailed(f"git could not be run: {exc}") from exc


def committable_paths(paths) -> list:
    """Return the paths git could commit: inside a work tree and not ignored.

    Git itself decides, rather than a reimplementation of .gitignore rules, so
    nested .gitignore files, global excludes, and files that are already tracked
    all count exactly as git counts them. A path that does not exist yet is fine -
    it is checked against the nearest folder above it that does.

    Git runs only when a .git exists somewhere above the path. A copy of this tool
    that is not a git repository never needs git, and on a Mac without developer
    tools installed never triggers the prompt to install them.

    Raises GitCheckFailed when a repository is present but git cannot answer: an
    unanswered question is not a safe answer.
    """
    exposed = []
    for path in paths:
        # The folder is resolved, so /tmp and /private/tmp are one place to git,
        # but not the file itself: write_atomic replaces a symlink where it sits
        # rather than writing through it, so the link's own location is what git
        # would see.
        path = Path(path)
        path = Path(os.path.abspath(path)).parent.resolve() / path.name
        folder = next(p for p in path.parents if p.is_dir())
        if not any((p / ".git").exists() for p in (folder, *folder.parents)):
            continue

        inside = _git(folder, "rev-parse", "--is-inside-work-tree")
        if inside.returncode != 0:
            raise GitCheckFailed(inside.stderr.strip() or "git rev-parse failed")
        if inside.stdout.strip() != "true":
            continue            # inside a .git directory itself, which git never tracks

        # check-ignore exits 0 when ignored, 1 when not, anything else on error.
        ignored = _git(folder, "check-ignore", "-q", "--", str(path))
        if ignored.returncode == 1:
            exposed.append(path)
        elif ignored.returncode != 0:
            raise GitCheckFailed(ignored.stderr.strip() or "git check-ignore failed")
    return exposed


def temp_path(path: Path) -> Path:
    """Where write_atomic puts a file's contents before renaming it into place."""
    return path.with_name(path.name + ".partial")


def with_temp_paths(paths) -> list:
    """Each path, followed by its temporary file: everything a write touches.

    The temporary file holds the same private text as the finished one, and a run
    that dies mid-write can leave it behind, so git must ignore it too. A rule
    such as *.md does not cover C0123456789.md.partial.
    """
    return [p for path in paths for p in (Path(path), temp_path(Path(path)))]


def _temp_file_hint(exposed) -> str:
    """The extra advice when what git would see includes temporary files."""
    if not any(p.name.endswith(".partial") for p in exposed):
        return ""
    return ("\n       Files ending in .partial hold the same text while it is being "
            "saved, and an\n       interrupted save can leave one behind. If the "
            "repository ignores the\n       finished files, add *.partial to its "
            ".gitignore too.")


def refuse_if_committable(paths) -> None:
    """Exit before anything is written or fetched if an export could be committed:
    the finished files or the temporary files they are written through."""
    try:
        exposed = committable_paths(with_temp_paths(paths))
    except GitCheckFailed as exc:
        sys.exit(f"FATAL: could not check whether the export would be ignored by git.\n"
                 f"       git said: {exc}\n"
                 f"       Use --out with a directory outside any git repository.")
    if exposed:
        listing = "\n".join(f"         {display_path(p)}" for p in exposed)
        sys.exit(f"FATAL: refusing to write inside a git repository where these would "
                 f"not be ignored:\n{listing}\n"
                 f"       Exports hold private conversations and must not be "
                 f"committable. Use --out with\n"
                 f"       a directory outside the repository, or one it already "
                 f"ignores.{_temp_file_hint(exposed)}")


def refuse_if_temp_paths_taken(paths) -> None:
    """Exit before Slack is contacted if a temporary file's path is already taken.

    write_atomic refuses such a path anyway, in the same step as creating the file;
    this only says so up front, before anything is fetched. Nothing found is
    removed: it may be a leftover from a crash, or a link to something else.
    """
    taken = [temp_path(Path(p)) for p in paths if os.path.lexists(temp_path(Path(p)))]
    if taken:
        listing = "\n".join(f"         {display_path(p)}" for p in taken)
        sys.exit(f"FATAL: something is already where a temporary file goes:\n"
                 f"{listing}\n"
                 f"       It may be left over from an export that crashed, or a "
                 f"link to another\n"
                 f"       file. slack-export will not write through it or delete "
                 f"it. Look at it,\n"
                 f"       move or delete it yourself, then run the same command "
                 f"again.")


def write_atomic(path: Path, text: str) -> None:
    """Write a file by replacing it whole, never by truncating it first.

    There is now ONE archive per conversation instead of a fresh timestamped copy
    each run, so a crash midway through a write would destroy the only record.
    Writing a neighbouring temp file and renaming it means the old file survives
    intact until the new one is complete; os.replace is atomic on the same
    filesystem, which is why the temp file sits in the destination directory
    rather than in /tmp.

    The temp file is created owner-only, so there is no moment when the
    conversation is readable by anyone else, and the rename carries that mode onto
    the finished file. The fchmod covers a umask that would loosen it.

    The temp file is created EXCLUSIVELY: if anything is already at that path -
    a .partial left by a crash, a symlink, a hard link to some other file - the
    open fails rather than following the link or truncating what is there, so no
    other file can ever receive the conversation through it. Checking first and
    opening after would leave a gap between the two; O_EXCL is a single step.
    Whatever was found is left exactly as it was, for the person to look at.

    If the write or the rename fails, the temporary file this call created is
    removed rather than left holding a copy of the conversation. Callers check
    beforehand that git ignores it anyway (with_temp_paths), because a crash
    leaves no chance to clean up.
    """
    temp = temp_path(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(temp, flags, PRIVATE_FILE)
    except FileExistsError as exc:
        raise FileExistsError(exc.errno, f"{temp.name} is in the way", str(temp)) \
            from exc
    try:
        os.fchmod(fd, PRIVATE_FILE)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temp, path)
    except BaseException:
        # Only ever the file created just above: the open was exclusive, so
        # nothing that existed before this call can be at risk here.
        try:
            temp.unlink()
        except OSError:
            pass
        raise


def load_archive(path: Path):
    """Read a previous export, or None if this conversation is new to us.

    A damaged archive is fatal rather than ignored: silently starting over would
    look like it worked and quietly abandon the history the file was holding.
    """
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        sys.exit(f"FATAL: {display_path(path)} is unreadable ({exc}).\n"
                 f"       Move it aside to start this conversation over.")


def load_directory(path: Path = None) -> dict:
    """Read the saved channel names, or an empty store if none have been saved.

    The path defaults to DIRECTORY_PATH looked up at call time, not at import, so
    tests can point it at a temporary file.

    Damaged is fatal, for the same reason as load_archive: starting over quietly
    would discard every name that was saved.
    """
    path = path or DIRECTORY_PATH
    if not path.exists():
        return channel_directory.empty()
    try:
        return channel_directory.validate(json.loads(path.read_text()))
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        sys.exit(f"FATAL: {display_path(path)} is unreadable ({exc}).\n"
                 f"       Fix it by hand, or move it aside to start the "
                 f"conversation index over.")


def save_directory(data: dict, path: Path = None) -> None:
    """Write the saved channel names, owner-only, never where git could commit them.

    Uses committable_paths directly rather than refuse_if_committable, whose advice
    (use --out) is about exports and would be wrong here.
    """
    path = path or DIRECTORY_PATH
    try:
        exposed = committable_paths(with_temp_paths([path]))
    except GitCheckFailed as exc:
        sys.exit(f"FATAL: could not check whether {display_path(path)} is ignored "
                 f"by git.\n       git said: {exc}")
    if exposed:
        names = " and ".join(p.name for p in exposed)
        sys.exit(f"FATAL: refusing to save channel names to {display_path(path)}:\n"
                 f"       it is inside a git repository that does not ignore "
                 f"{names},\n"
                 f"       so real channel names and IDs could be committed. Add "
                 f"{'them' if len(exposed) > 1 else 'it'} to that repository's "
                 f".gitignore.{_temp_file_hint(exposed)}")
    make_private_dir(path.parent)
    write_atomic(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def record_in_directory(auth: dict, info: dict, conversation_id: str, stem: str,
                        when_utc: str, path: Path = None,
                        complete: bool = True) -> str:
    """Add this export to the conversation index, and say how it went in one line.

    Records the Slack channel name, the kind and the export name; a nickname is
    only ever set by `save`, so it is left alone. Runs after the export files are
    written, so a failed export records nothing. LAST EXPORTED moves only when
    `complete`: the archive and every readable file of that export name are up to
    date. A run that had to skip a file still records the names, but not the time.

    Never fails the export: the export is what was asked for, and the list can be
    fixed afterwards. Every problem - a damaged store, a git refusal, a write
    error - comes back as the line to print instead, which is why this catches the
    SystemExit that load_directory and save_directory use to stop `save` and `list`.
    """
    try:
        store = load_directory(path)
        team_id, team_name = auth["team_id"], auth["team"]
        channel_directory.set_slack_name(store, team_id, team_name, conversation_id,
                                         info.get("name") or "")
        channel_directory.set_kind(store, team_id, team_name, conversation_id,
                                   channel_directory.kind_of(info))
        if complete:
            channel_directory.record_export(store, team_id, team_name,
                                            conversation_id, stem, when_utc)
        save_directory(store, path)
    except (SystemExit, OSError) as exc:
        reason = str(exc.code if isinstance(exc, SystemExit) else exc)
        return (f"NOT updated - the export itself is fine.\n"
                f"              {reason.removeprefix('FATAL: ')}")
    row = next(r for r in channel_directory.search(store, conversation_id)
               if r["team_id"] == team_id and r["channel_id"] == conversation_id)
    # The same columns `list` prints - ID, nickname, Slack name - minus the header.
    line = channel_directory.format_rows([row])[0]
    if not complete:
        line += "\n              (LAST EXPORTED not updated: a file was skipped)"
    return line


def swap_header(existing: str, new_header: str, marker: str):
    """Replace a rendered file's header block, keeping every message below it.

    Splits on the FIRST marker only - the rule that closes the header - so a
    matching line inside somebody's message is never mistaken for the seam.
    Returns None when the marker is absent, which means the file was not written
    by this tool and must not be appended to.
    """
    index = existing.find(marker)
    if index == -1:
        return None
    return new_header + existing[index + len(marker):]


def parse_scopes(raw) -> list:
    """Normalise Slack's x-oauth-scopes header into a list of scope names.

    Slack sends it as one comma-separated string. Beware: iterating a Python string
    yields single CHARACTERS, so 'a,b'[0] is 'a' and ','.join('a,b') is 'a,",",b' -
    both silently produce nonsense rather than raising. Some HTTP layers hand headers
    back as a list instead, so accept either.
    """
    if isinstance(raw, (list, tuple)):
        raw = ",".join(raw)
    return sorted({s.strip() for s in str(raw).split(",") if s.strip()})


# The only scopes this tool will run with: the nine read scopes requested in
# slack-export-app-manifest.yaml, plus identify - a legacy scope that Slack may
# report on a user token, depending on the app's history, without the manifest
# asking for it. An allowlist rather than a search for ":write", so a scope whose
# name does not look like a write - "admin", or one Slack invents later - is still
# refused. Adding a scope to the manifest means adding it here too, deliberately.
ALLOWED_SCOPES = frozenset({
    "channels:history", "channels:read",
    "groups:history", "groups:read",
    "im:history", "im:read",
    "mpim:history", "mpim:read",
    "users:read",
    "identify",
})


def unexpected_scopes(scopes) -> list:
    """Return any granted scope that is not on the allowlist."""
    return [s for s in scopes if s not in ALLOWED_SCOPES]


def reported_scopes(headers):
    """The scopes Slack reported for this token, or None if it reported none.

    None is deliberately not an empty list. A missing or empty header means the
    token's permissions are unknown, and unknown must never pass as "nothing to
    worry about". HTTP header names are case-insensitive, so they are matched
    that way.
    """
    for name, value in (headers or {}).items():
        if name.lower() == "x-oauth-scopes":
            return parse_scopes(value) or None
    return None


def _self_test() -> None:
    """Prove the guard fires. Runs on every invocation - it costs nothing, and this
    is the one check the project's 'never a bot' promise actually rests on."""
    real = "identify,channels:history,groups:history,im:history,mpim:history," \
           "channels:read,groups:read,im:read,mpim:read,users:read"
    assert len(parse_scopes(real)) == 10, parse_scopes(real)
    assert parse_scopes(real) == parse_scopes(real.split(",")), "list form must match"
    assert unexpected_scopes(parse_scopes(real)) == []
    assert unexpected_scopes(parse_scopes(real + ",chat:write")) == ["chat:write"]
    assert unexpected_scopes(parse_scopes(real + ",files:write")) == ["files:write"]
    # The reason for an allowlist: scopes that never say "write" are refused too.
    assert unexpected_scopes(parse_scopes(real + ",admin")) == ["admin"]
    assert unexpected_scopes(parse_scopes(real + ",files:read")) == ["files:read"]
    # Holding fewer scopes than the manifest grants is not a risk, just less reach.
    assert unexpected_scopes(parse_scopes("channels:history,identify")) == []

    # Unknown is not safe: no scope header must never read as "no scopes".
    assert reported_scopes({}) is None
    assert reported_scopes(None) is None
    assert reported_scopes({"x-oauth-scopes": ""}) is None
    assert reported_scopes({"x-oauth-scopes": " , "}) is None
    assert reported_scopes({"X-OAuth-Scopes": real}) == parse_scopes(real)

    # An ID is checked before anything is fetched, in every form people paste.
    assert parse_conversation_id("C0123456789") == "C0123456789"
    assert parse_conversation_id("  D0123456789  ") == "D0123456789"
    assert parse_conversation_id(
        "https://example.slack.com/archives/C0123456789") == "C0123456789"
    assert parse_conversation_id(
        "https://example.slack.com/archives/C0123456789/p1787000000000100"
    ) == "C0123456789"
    assert parse_conversation_id("<#C0123456789|general>") == "C0123456789"
    assert parse_conversation_id("slack-export") is None, "the command's own name"
    assert parse_conversation_id("c0123456789") is None, "Slack IDs are upper case"
    assert parse_conversation_id("") is None and parse_conversation_id(None) is None

    assert safe_stem("Project Planning") == "Project-Planning"
    assert safe_stem("  slashes/and : colons  ") == "slashes-and-colons"
    assert safe_stem("v1.2 planning") == "v1-2-planning"
    assert safe_stem("!!!") == "", "a name with no letters must fall back to the ID"
    assert "." not in safe_stem("notes.md"), "a dot would break the .raw.json suffix"
    assert name_note("Project-Planning", "Project-Planning", "C0123456789") is None
    assert name_note("", "C0123456789", "C0123456789") is None
    assert name_note("Lab & Field", "Lab-Field", "C0123456789").startswith("Lab-Field")

    # relative_to raises instead of falling back, so prove all three branches.
    assert display_path(EXPORT_DIR / "a.md") == "exports/a.md"
    assert display_path(Path.home() / "Desktop/a.md") == "~/Desktop/a.md"
    assert display_path(Path("/tmp/a.md")) == "/tmp/a.md"

    # Header swapping is how an append keeps the counts at the top honest. It must
    # split on the FIRST rule only - a message quoting one must not be treated as
    # the seam, which would silently delete everything above it.
    body = "OLD HEAD\n===\n\nmessage one\nquoting === inside a message\n"
    assert swap_header(body, "NEW HEAD\n===", "===") == \
        "NEW HEAD\n===\n\nmessage one\nquoting === inside a message\n"
    assert swap_header("no rule here at all", "NEW", "===") is None


def assert_read_only(client: WebClient, quiet: bool = False):
    """Refuse to run unless Slack reports the token's scopes, all in ALLOWED_SCOPES.

    Returns the granted scopes and Slack's auth.test response, which also says
    whose token it is and which workspace - the team ID that keys saved names.
    `quiet` skips the identity lines; a refusal is always printed.

    A token that Slack rejects - revoked, expired, mistyped - is an ordinary thing
    to hit, not a bug, so it gets the same one-line FATAL treatment as every other
    Slack error rather than a traceback. Only SlackApiError is caught here: a
    programming mistake must still surface as itself.
    """
    try:
        response = client.auth_test()
    except SlackApiError as exc:
        reported = getattr(exc, "response", None) or {}
        sys.exit(f"FATAL: Slack rejected the saved token "
                 f"({reported.get('error') or 'no error reported'}).\n"
                 f"       Store a valid User OAuth Token and try again. "
                 f"See SLACK-SETUP.md.")

    granted = reported_scopes(response.headers)
    if granted is None:
        sys.exit("FATAL: Slack did not report this token's scopes, so its permissions "
                 "cannot be verified.\n"
                 "       Refusing to run.")

    suspicious = unexpected_scopes(granted)
    if suspicious:
        sys.exit(f"FATAL: token carries unexpected scope(s) {suspicious}. "
                 f"Refusing to run.\n"
                 f"       Only the read scopes in slack-export-app-manifest.yaml "
                 f"are allowed.")

    if not quiet:
        print(f"  identity   : {response['user']} ({response['user_id']}) "
              f"on {response['team']}")
        print(f"  scopes     : {len(granted)} granted, all read-only")
    return granted, response


def fetch_history(client: WebClient, conversation_id: str) -> list:
    """Page through the entire top-level history of one conversation, newest first.

    Slack paginates with an opaque 'cursor': each response carries the cursor for the
    next page, and the absence of one means you have reached the end.
    """
    messages, cursor, page = [], None, 0

    while True:
        page += 1
        response = client.conversations_history(
            channel=conversation_id, limit=PAGE_SIZE, cursor=cursor,
        )
        messages.extend(response["messages"])
        print(f"  page {page:>3}  +{len(response['messages']):>4} messages  "
              f"(running total {len(messages)})", flush=True)

        cursor = response.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            return messages


def fetch_thread_replies(client: WebClient, conversation_id: str,
                         parent_ts: str) -> list:
    """Fetch every reply in one thread, excluding the parent message itself.

    conversations.replies includes the PARENT alongside the replies, so it is
    filtered out by timestamp here - otherwise every thread starter would appear
    twice in the merged output.
    """
    replies, cursor = [], None

    while True:
        response = client.conversations_replies(
            channel=conversation_id, ts=parent_ts, limit=PAGE_SIZE, cursor=cursor,
        )
        replies.extend(m for m in response["messages"] if m.get("ts") != parent_ts)

        cursor = response.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            return replies


def expand_threads(client: WebClient, conversation_id: str, messages: list) -> dict:
    """Attach replies to every parent that has them. Mutates `messages` in place.

    Returns integrity counters so the caller can report whether what Slack actually
    handed back matches what it claimed in reply_count.
    """
    parents = [m for m in messages if m.get("reply_count")]
    claimed = sum(m["reply_count"] for m in parents)
    fetched = 0
    mismatches = []

    print(f"  {len(parents)} threads to expand "
          f"({claimed} replies claimed by reply_count)", flush=True)

    for index, parent in enumerate(parents, start=1):
        replies = fetch_thread_replies(client, conversation_id, parent["ts"])
        parent["thread_replies"] = replies
        fetched += len(replies)

        # reply_count is Slack's own tally; a mismatch usually means a reply was
        # deleted after the parent's counter was last written. Worth surfacing
        # rather than silently trusting either number.
        if len(replies) != parent["reply_count"]:
            mismatches.append((parent["ts"], parent["reply_count"], len(replies)))

        if index % 10 == 0 or index == len(parents):
            print(f"  thread {index:>4}/{len(parents)}  "
                  f"{fetched} replies fetched", flush=True)

    return {"threads": len(parents), "claimed": claimed,
            "fetched": fetched, "mismatches": mismatches}


def flag_broadcast_duplicates(messages: list) -> int:
    """Mark top-level messages that ALSO appear inside a thread.

    When someone replies in a thread and ticks "Also send to #channel", Slack returns
    that one message twice: once from conversations.history with
    subtype='thread_broadcast', and again from conversations.replies. Same ts, same
    content, both legitimate.

    Nothing is deleted here - capture stays faithful. The flag lets the renderers
    show it once (in its thread, where it has context) instead of twice.
    """
    reply_ts = {r["ts"] for m in messages for r in m.get("thread_replies", [])}
    flagged = 0
    for message in messages:
        if message["ts"] in reply_ts:
            message["_also_in_thread"] = True
            flagged += 1
    return flagged


class ExportFailed(Exception):
    """One conversation could not be exported: Slack would not hand it over (a
    wrong ID, no access, an error partway through the fetch), or a file could not
    be written. Either way a retry is safe, and the other conversations in the
    same run can still go ahead."""


def connect() -> WebClient:
    """A Slack client using the Keychain token, retrying politely on HTTP 429."""
    client = WebClient(token=keychain_token())
    client.retry_handlers.append(RateLimitErrorRetryHandler(max_retry_count=3))
    return client


def require_conversation_id(raw) -> str:
    """The conversation ID in what was typed, or exit explaining what one looks like.

    Runs before the Keychain is read or Slack is called, so a typo costs nothing.
    """
    conversation_id = parse_conversation_id(raw)
    if conversation_id is None:
        sys.exit(
            f"FATAL: '{raw}' is not a Slack conversation ID.\n"
            f"       Expected C0123456789 (a channel), D0123456789 (a DM), or a "
            f"Slack link\n"
            f"       such as https://example.slack.com/archives/C0123456789\n"
            f"       In Slack: click the channel name -> About -> Channel ID, at "
            f"the bottom.")
    return conversation_id


def split_targets(first: str, rest: list) -> list:
    """The conversations to export, each with the name typed for its files, as
    (conversation ID, name) pairs in the order typed.

    The words after an ID, up to the next ID, are that conversation's file name;
    an ID with no words after it gets "" and so keeps its ID as the name:

        slack-export C0123456789 Project Planning          # one, named
        slack-export C0123456789 Planning D0123456789      # two, the first named

    A word counts as an ID if parse_conversation_id finds one in it, so a name that
    looks like an ID starts a new conversation instead - rare enough to accept.
    The same conversation typed twice under the same name is exported once; under
    two different names it is refused, as more likely a slip than a wish.
    """
    targets = [(require_conversation_id(first), [])]
    for word in rest:
        found = parse_conversation_id(word)
        if found:
            targets.append((found, []))
        else:
            targets[-1][1].append(word)

    named = {}
    for conversation_id, words in targets:
        typed = " ".join(words)
        earlier = named.setdefault(conversation_id, typed)
        if earlier != typed:
            sys.exit(
                f"FATAL: {conversation_id} is typed twice, as "
                f"{_quote(earlier) or '(no name)'} and {_quote(typed) or '(no name)'}.\n"
                f"       Type it once, with the name you want for its files.")
    # A dict keeps the order its keys were first added, so this is the order typed.
    return list(named.items())


def _quote(word: str) -> str:
    """A word as it would need to be typed: in quotes if it contains a space."""
    return f'"{word}"' if " " in word else word


# What can be typed after --sort-by, and the order in channel_directory.SORT_KEYS
# each one asks for. One word per order, with no second spellings.
SORT_ORDERS = {"name": "slack_name", "id": "id", "nickname": "nickname",
               "export": "export"}
DEFAULT_SORT_ORDER = "name"


class _NoSuchFlag(argparse.Action):
    """A flag that does not exist, registered only so that typing it is an error
    naming the real one. argparse accepts any unambiguous start of a flag, so
    without this `--sort` would quietly work as short for `--sort-by`."""

    def __call__(self, parser, namespace, values, option_string=None):
        parser.error(f"there is no {option_string}; use {self.const}")
SORT_NEEDS_A_GROUP = ("FATAL: --sort-by and --reverse order the conversations in a "
                      "group, so they need a group name:\n"
                      "  slack-export list --groups <group> --sort-by export")


class _HelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Keeps the description as written, and also the line breaks in any flag's
    help that has them, such as --sort-by's table. Help without line breaks is
    wrapped as usual."""

    def _split_lines(self, text, width):
        if "\n" in text:
            return text.splitlines()
        return super()._split_lines(text, width)


def _add_sort_flags(parser) -> None:
    """--sort-by and --reverse, the same on every command that prints the
    conversation table."""
    parser.add_argument("--sort-by", choices=tuple(SORT_ORDERS),
                        default=DEFAULT_SORT_ORDER, metavar="{name,id,nickname,export}",
                        help="changes conversation list order:\n"
                             "  name      sorts alphabetically by SLACK NAME "
                             "(default)\n"
                             "  id        sorts alphanumerically by ID\n"
                             "  nickname  sorts alphabetically by NICKNAME\n"
                             "  export    sorts in order of recency by LAST "
                             "EXPORTED")
    parser.add_argument("--sort", action=_NoSuchFlag, nargs="?", const="--sort-by",
                        help=argparse.SUPPRESS)
    parser.add_argument("--reverse", action="store_true",
                        help="list conversations in the opposite order")


def _sort_was_asked_for(args) -> bool:
    """Whether the command line asked for an order other than the default one."""
    return args.sort_by != DEFAULT_SORT_ORDER or args.reverse


def _sorted(rows: list, sort_by: str, reverse: bool) -> list:
    """The rows in the order --sort-by and --reverse asked for."""
    return channel_directory.sort_rows(rows, SORT_ORDERS[sort_by], reverse)


def list_command(argv) -> None:
    """`slack-export list [search ...]`, `list --search WORD ...` and
    `list --groups [GROUP ...] [--search WORD ...]`: print conversations or
    groups from the conversation index. Offline.

    Filter, sort and format are separate steps in channel_directory, so a new
    sort order or column is a table entry there and a choice or flag here.
    """
    parser = argparse.ArgumentParser(
        prog="slack-export list",
        usage="slack-export list [search ...]\n"
              "       slack-export list --search WORD [WORD ...]\n"
              "       slack-export list --groups [GROUP ...] "
              "[--search WORD [WORD ...]]\n"
              "       slack-export list ... [--sort-by {name,id,nickname,export}] [--reverse]",
        # Line breaks written out, since RawDescriptionHelpFormatter keeps them as
        # they are - it is what lets the paragraphs stay separate.
        description="Print every conversation in your conversation index, one per\n"
                    "line, under a header naming the four columns: its ID, your\n"
                    "nickname for it, its Slack channel name, and how long ago\n"
                    "you last exported it. Sorted by Slack\n"
                    "channel name, or by nickname for a conversation Slack gives\n"
                    "no name, such as a 1:1 DM.\n\n"
                    "With no nickname, the nickname column shows the name of its\n"
                    "most recent export in [brackets] - a name you did not choose -\n"
                    "unless that export was given no name. A DM has no Slack\n"
                    "channel name, so it shows as (DM) - unless it is a group DM\n"
                    "someone has named.\n"
                    "LAST EXPORTED is when an export last ran, under any file name\n"
                    "- not whether Slack has anything newer. It is not searched.\n"
                    "A '-' marks an empty column, such as a conversation never\n"
                    "exported.\n\n"
                    "--groups alone lists your groups and how many conversations\n"
                    "each has. With group names, it lists the conversations in\n"
                    "those groups instead; add --search to search within them.\n"
                    "With several names, every conversation in any of them is\n"
                    "listed once, and a GROUPS column says which it is in.\n\n"
                    "--sort-by changes the order: name (the default, described\n"
                    "above) sorts alphabetically by SLACK NAME; id sorts\n"
                    "alphanumerically by ID; nickname sorts alphabetically by\n"
                    "NICKNAME, those with none last; export sorts in order of\n"
                    "recency by LAST EXPORTED, most recent first and the never\n"
                    "exported last. --reverse turns any order back to front,\n"
                    "so with --sort-by export the ones never exported or\n"
                    "exported longest ago come first.\n\n"
                    "Reads only the conversation index - never the Keychain or "
                    "Slack.",
        formatter_class=_HelpFormatter)
    # The nickname column used to be opt-in. It is always shown now, but -n is
    # still accepted, silently, so a habit or a script that types it keeps working.
    parser.add_argument("-n", "--nicknames", action="store_true",
                        help=argparse.SUPPRESS)
    # Every word is joined, like an export's name, so quotes are optional.
    parser.add_argument("search", nargs="*",
                        help="show only conversations whose ID, nickname, or "
                             "Slack channel name contains this - ignoring case, "
                             "spaces and punctuation")
    parser.add_argument("--search", dest="search_flag", nargs="+", metavar="WORD",
                        help="the same search, spelled out; needed with --groups")
    # Any number of names, so `--groups Research Work` already works. None when
    # absent, [] when given alone, so the two can be told apart.
    parser.add_argument("--groups", nargs="*", metavar="GROUP",
                        help="with no names, list your groups; with names, list "
                             "the conversations in those groups (names are "
                             "exact; quote a name with spaces)")
    _add_sort_flags(parser)
    args = parser.parse_args(argv)

    if args.search and args.search_flag:
        sys.exit("FATAL: give the search either as words after `list` or with "
                 "--search, not both.")
    # Words after `list` are never read as search when --groups is used: where
    # they fell on the line would decide their meaning (D53).
    if args.search and args.groups is not None:
        names = " ".join(_quote(n) for n in args.groups) or "<group>"
        sys.exit(f"When using --groups, use --search to search within the "
                 f"results:\n"
                 f"  slack-export list --groups {names} --search "
                 f"{' '.join(args.search)}")
    words = args.search_flag or args.search
    # None when no search was typed, so only that lists everything: a typed term
    # has to match, even one that is only punctuation.
    term = " ".join(words) if words else None

    store = load_directory()
    if args.groups is not None:
        if not args.groups and _sort_was_asked_for(args):
            sys.exit(SORT_NEEDS_A_GROUP)
        _list_groups(store, args.groups, term, args.sort_by, args.reverse)
        return

    rows = channel_directory.search(store, term)
    if not rows:
        if channel_directory.search(store):
            # Exit 1 on no match, as grep does, so a script can tell.
            sys.exit(f"No conversation in your conversation index matches '{term}'.")
        print("Your conversation index is empty.")
        return
    for line in channel_directory.format_rows(_sorted(rows, args.sort_by, args.reverse),
                                              channel_directory.LIST_COLUMNS,
                                              header=True):
        print(line)


def _no_such_group(store: dict, name: str) -> str:
    """The message for a group name that does not exist, with a hint when one
    differs only in capitalization - the likely slip, since names are exact."""
    message = f"No group named '{name}'."
    similar = channel_directory.similar_group_names(store, name)
    if similar:
        message += ("\nDid you mean " + " or ".join(f"'{s}'" for s in similar)
                    + "? Group names are exact.")
    return message


def _list_groups(store: dict, names: list, term,
                 sort_by: str = DEFAULT_SORT_ORDER, reverse: bool = False) -> None:
    """`list --groups`: every group with its size, or the members of the named
    groups, searched like `list` if a term is given and in the order asked for."""
    if not names:
        if term is not None:
            sys.exit("FATAL: --search searches the conversations in a group, so "
                     "it needs a group name:\n"
                     f"  slack-export list --groups <group> --search {term}")
        sizes = channel_directory.group_sizes(store)
        if not sizes:
            print("You have no groups yet.\n"
                  "To make one: slack-export group <name>")
            return
        cells = [["GROUP", "CONVERSATIONS"]]
        cells += [[name, str(size)] for name, size in sizes]
        for line in channel_directory.align(cells):
            print(line)
        return

    names = list(dict.fromkeys(name.strip() for name in names))
    groups = store.get("groups", {})
    unknown = [name for name in names if name not in groups]
    if unknown:
        lines = [_no_such_group(store, name) for name in unknown]
        if len(names) > 1:
            lines.append("(To search within groups, use --search: "
                         "slack-export list --groups <group> --search <words>)")
        sys.exit("\n".join(lines))

    members = channel_directory.group_members(store, names)
    if not members:
        print(f"{names[0]} has no conversations yet." if len(names) == 1
              else "None of these groups has any conversations yet.")
        return
    rows = [row for row in channel_directory.search(store, term)
            if (row["team_id"], row["channel_id"]) in members]
    if not rows:
        where = names[0] if len(names) == 1 else "these groups"
        sys.exit(f"No conversation in {where} matches '{term}'.")
    columns = channel_directory.LIST_COLUMNS
    if len(names) > 1:
        # Every conversation in ANY of the groups is listed once; this column says
        # which of the named groups it is in, in the order they were typed.
        each = {name: channel_directory.group_members(store, [name])
                for name in names}
        for row in rows:
            key = (row["team_id"], row["channel_id"])
            row["groups"] = ", ".join(n for n in names if key in each[n])
        columns += ("groups",)
    for line in channel_directory.format_rows(_sorted(rows, sort_by, reverse),
                                              columns, header=True):
        print(line)


def save_command(argv) -> None:
    """`slack-export save <ID> [nickname ...]`: remember a conversation.

    Asks Slack which workspace the token belongs to (auth.test, which is also the
    read-only check) and what the conversation is (conversations.info), so the
    entry is keyed correctly and the ID is known to exist before it is saved.
    """
    parser = argparse.ArgumentParser(
        prog="slack-export save",
        description="Save a conversation to your conversation index, so "
                    "`slack-export list` can find its ID again. Records its Slack channel name and kind, and your "
                    "nickname for it if you give one. Saving again refreshes the "
                    "Slack channel name; a nickname is only changed by giving a "
                    "new one. Contacts Slack, read-only.")
    parser.add_argument("conversation_id",
                        help="Slack conversation ID - C... (channel) or D... (DM) "
                             "- or a Slack link containing one")
    # Every leftover word is the nickname, so quotes are optional, as for an export.
    parser.add_argument("nickname", nargs="*",
                        help="your own name for it (optional); must not already "
                             "belong to another conversation in your index")
    args = parser.parse_args(argv)
    conversation_id = require_conversation_id(args.conversation_id)
    nickname = " ".join(args.nickname).strip()

    # Read before Slack is contacted: a damaged store should cost nothing.
    store = load_directory()
    client = connect()
    _, auth = assert_read_only(client, quiet=True)
    team_id, team_name = auth["team_id"], auth["team"]

    try:
        info = client.conversations_info(channel=conversation_id)["channel"]
    except SlackApiError as exc:
        error = exc.response["error"]
        hint = ("\n       Check the ID, and that you are a member of it in "
                "this workspace." if error == "channel_not_found" else "")
        sys.exit(f"FATAL: Slack could not find {conversation_id} ({error}).{hint}")

    kind = channel_directory.kind_of(info)
    if nickname:
        try:
            outcome = channel_directory.set_nickname(store, team_id, team_name,
                                                     conversation_id, nickname)
        except channel_directory.NicknameTaken as exc:
            owner = exc.owner.get("slack_name") or "no Slack channel name"
            sys.exit(f"FATAL: the nickname '{nickname}' is already used by "
                     f"{exc.owner_id} ({owner}).\n"
                     f"       Pick another, or give that one a different "
                     f"nickname first.")
    channel_directory.set_slack_name(store, team_id, team_name, conversation_id,
                                     info.get("name") or "")
    channel_directory.set_kind(store, team_id, team_name, conversation_id, kind)
    save_directory(store)

    row = next(r for r in channel_directory.search(store, conversation_id)
               if r["team_id"] == team_id and r["channel_id"] == conversation_id)
    # The row as `list` prints it, then the kind - except for a 1:1 DM, whose
    # Slack-name column already says (DM).
    line = channel_directory.format_rows([row])[0]
    if kind != "dm":
        line += f"  ({channel_directory.KINDS[kind]})"
    print(line)
    if row["nickname"]:
        print(f"  nickname: {row['nickname']}  "
              f"({outcome if nickname else 'unchanged'})")
    elif not channel_directory.real_slack_name(row):
        # Slack gives it no name, so only a nickname identifies it for good.
        print(f"  No nickname yet. To give it one:  "
              f"slack-export save {conversation_id} <nickname>")


def _count(n: int) -> str:
    return f"{n} conversation" + ("" if n == 1 else "s")


def _index_from_slack(store: dict, client, auth: dict, conversation_id: str):
    """Look `conversation_id` up in Slack and add it to the conversation index, as
    `save` does but with no nickname. Returns None, or why it could not be added.

    Slack's "not found" also covers a conversation this token simply cannot see,
    so it is never reported as proof that the ID is wrong.
    """
    try:
        info = client.conversations_info(channel=conversation_id)["channel"]
    except SlackApiError as exc:
        error = exc.response["error"]
        if error == "channel_not_found":
            return "conversation not found or not accessible"
        return f"Slack could not look it up ({error})"
    team_id, team_name = auth["team_id"], auth["team"]
    channel_directory.set_slack_name(store, team_id, team_name, conversation_id,
                                     info.get("name") or "")
    channel_directory.set_kind(store, team_id, team_name, conversation_id,
                               channel_directory.kind_of(info))
    return None


def _remove_from_group(store: dict, name: str, raw_ids: list) -> None:
    """`group NAME --remove ID ...`: take each conversation out of the group and
    report what happened to it. Partial success: one that was not a member does
    not undo the ones that were. Local only."""
    removed, absent = [], []
    for conversation_id in dict.fromkeys(parse_conversation_id(raw)
                                         for raw in raw_ids):
        teams = channel_directory.remove_from_group(store, name, conversation_id)
        # Shown as `list` would show it, from the index alone; an ID the index
        # does not know is shown bare rather than looked up in Slack.
        rows = [r for r in channel_directory.search(store, conversation_id)
                if r["channel_id"] == conversation_id
                and (not teams or r["team_id"] in teams)]
        (removed if teams else absent).append(
            rows[0] if rows else channel_directory.bare_row(conversation_id))
    if removed:
        save_directory(store)

    lines = channel_directory.format_rows(removed + absent)
    sections = []
    if removed:
        sections.append([f"Removed from {name}:"]
                        + [f"  {line}" for line in lines[:len(removed)]])
    if absent:
        sections.append([f"Not in {name}:"]
                        + [f"  {line}" for line in lines[len(removed):]])
    size = _count(len(store["groups"][name]))
    sections.append([f"{name} now has {size}." if removed
                     else f"No changes - {name} still has {size}."])
    print("\n\n".join("\n".join(section) for section in sections))


def _list_word_slip(parser, argv, args):
    """Whether `group` was typed with list as a plain word - `group list`,
    `group list Research`, `group Research list` - which is almost always --list
    with its dashes left off. Only list is caught: --list changes nothing, while
    --remove and --delete are never run on a guess.

    Returns None when list was not typed as a word, when a flag was typed as
    well, or when a group named list already exists (then it means that group).
    Otherwise (as_name, meant, shown): as_name says list is in the group name's
    place, so it could be a new group's name; meant is the argument list with
    the dashes added and shown is how to print it, both None when adding them
    still makes no working command.
    """
    if args.remove or args.delete or args.list:
        return None
    if "list" not in [args.name, *args.conversations]:
        return None
    as_name = args.name == "list"
    if as_name and "list" in load_directory().get("groups", {}):
        return None

    meant = list(argv)
    meant[meant.index("list")] = "--list"
    m = parser.parse_intermixed_args(meant)
    has_name = m.name is not None and not parse_conversation_id(m.name)
    works = not m.conversations and (
        has_name or (m.name is None and not _sort_was_asked_for(m)))
    if not works:
        # With nothing to suggest, a misplaced word is left to the usual
        # "not a conversation ID" refusal.
        return (True, None, None) if as_name else None
    shown = " ".join(["slack-export", "group"]
                     + ([_quote(m.name)] if m.name is not None else [])
                     + ["--list"]
                     + (["--sort-by", m.sort_by]
                        if m.sort_by != DEFAULT_SORT_ORDER else [])
                     + (["--reverse"] if m.reverse else []))
    return as_name, meant, shown


def _ask() -> "str | None":
    """One line typed at the terminal, or None when nobody is there to type it
    (input from a pipe or a script), or when it is cut off with Ctrl-C or Ctrl-D."""
    if not sys.stdin.isatty():
        return None
    try:
        return input("> ")
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)
        return None


def _confirm_list_word(as_name, meant, shown):
    """Ask whether list typed as a plain word was meant as --list. Returns the
    argument list to run instead, or None to go ahead as typed; exits, changing
    nothing, when the answer is anything else or there is nobody to answer.

    Enter runs the command with --list; y goes ahead as typed, making (or adding
    to) a group named list - offered only when list is in the name's place."""
    if as_name:
        lines = ['There is no group named "list".']
    else:
        lines = ['"list" is not a conversation ID.']
    if meant is not None:
        lines += ["It looks like you meant:", f"  {shown}"]
    else:
        lines.append("Did you mean --list? Lists go:  slack-export group --list  "
                     "or  slack-export group <name> --list")
    choices = []
    if meant is not None:
        choices.append("press Enter to run that")
    if as_name:
        choices.append('type y to make a group named "list"')
    choice = " or ".join(choices)
    lines.append(choice[0].upper() + choice[1:] + "; anything else cancels.")
    print("\n".join(lines), file=sys.stderr)

    answer = _ask()
    if answer is None:
        sys.exit("Nothing was changed: there is no one at the terminal to "
                 "answer.")
    answer = answer.strip().lower()
    if answer == "" and meant is not None:
        return meant
    if answer == "y" and as_name:
        return None
    sys.exit("Cancelled. Nothing was changed.")


def group_command(argv) -> None:
    """`slack-export group NAME [ID ...]`: make a group, or add conversations to
    one. `--remove ID ...` takes conversations out of it; `--delete` deletes it;
    `--list` prints its conversations, as `list --groups NAME` does - or, with no
    name, every group and its size, as `list --groups` does. Removing, deleting
    and listing are always local.

    Conversations already in the conversation index are handled locally. One that
    is not is looked up in Slack, read-only, and added to the index first - so the
    Keychain and Slack are touched only when some ID actually needs it. Adds what
    it can and reports the rest; exits 1 if anything could not be added, but only
    after saving what was.
    """
    parser = argparse.ArgumentParser(
        prog="slack-export group",
        usage="slack-export group name [ID ...]\n"
              "       slack-export group name --remove ID [ID ...]\n"
              "       slack-export group name --delete\n"
              "       slack-export group name --list "
              "[--sort-by {name,id,nickname,export}] [--reverse]\n"
              "       slack-export group --list",
        # Each paragraph wrapped on its own, so the blank lines between them
        # survive (the default formatter would run them together).
        description="\n\n".join(textwrap.fill(paragraph, 78) for paragraph in (
            "Make a group of conversations, or add conversations to one. With "
            "only a name, makes an empty group. With IDs, adds them, making the "
            "group first if it is new; adding never removes anything already in "
            "it.",
            "Conversations already in your conversation index are handled "
            "locally. If an ID is not in it yet, slack-export looks it up in "
            "Slack (read-only) and adds it to the conversation index before "
            "adding it to the group. Conversations Slack cannot find or access "
            "are not added.",
            "--remove takes the given conversations out of the group; an "
            "emptied group is kept. --delete deletes the group itself. Neither "
            "removes anything from the conversation index, and both work without "
            "Slack.",
            "--list prints the conversations in the group and changes nothing. "
            "It is the same as `slack-export list --groups name`, which can "
            "also search within a group and show several groups at once. With "
            "no name, --list prints all your groups and how many conversations "
            "each has, as `slack-export list --groups` does. With a name, "
            "--sort-by and --reverse choose the order, as they do for "
            "`slack-export list`.",
            "Group names are exact: 'Research' and 'research' are two groups. "
            "Put a name with spaces in quotes.")),
        formatter_class=_HelpFormatter)
    # Optional only so that `group --list` can list every group; everything
    # else still needs it, checked below.
    parser.add_argument("name", nargs="?",
                        help="the group's name (only --list works without it)")
    parser.add_argument("conversations", nargs="*", metavar="ID",
                        help="conversation IDs - C... or D... - or Slack links "
                             "containing them")
    parser.add_argument("--remove", action="store_true",
                        help="remove these conversations from the group instead "
                             "of adding them")
    parser.add_argument("--delete", action="store_true",
                        help="delete the group itself (its conversations stay in "
                             "the conversation index)")
    parser.add_argument("--list", action="store_true",
                        help="list the conversations in the group - or, with "
                             "no name, all your groups - changing nothing")
    _add_sort_flags(parser)
    # Intermixed, so `group Research --remove C0123 D0456` parses on every Python
    # this supports: plain parse_args on 3.9 takes the IDs' slot as filled (empty)
    # before it reaches --remove, and then rejects the IDs.
    args = parser.parse_intermixed_args(argv)
    slip = _list_word_slip(parser, argv, args)
    if slip:
        meant = _confirm_list_word(*slip)
        if meant is not None:
            group_command(meant)
            return
    if args.remove + args.delete + args.list > 1:
        parser.error("use only one of --remove, --delete and --list")
    if _sort_was_asked_for(args) and not args.list:
        parser.error("--sort-by and --reverse order what --list prints, so they "
                     "need --list")
    if args.name is None:
        if _sort_was_asked_for(args):
            sys.exit(SORT_NEEDS_A_GROUP.replace("list --groups <group>",
                                                "group <group> --list"))
        if not args.list:
            parser.error("a group name is needed (only --list works without "
                         "one, to list all your groups)")
        _list_groups(load_directory(), [], None)
        return
    if args.delete and args.conversations:
        parser.error("--delete deletes the whole group, so it takes no "
                     "conversation IDs")
    if args.list and args.conversations:
        parser.error("--list lists the whole group, so it takes no "
                     "conversation IDs")
    if args.remove and not args.conversations:
        parser.error("--remove needs the conversation IDs to remove")
    name = args.name.strip()
    if not name:
        sys.exit("FATAL: a group name cannot be empty.")
    # The easiest slip is leaving the name out, which would make a group named
    # after the first conversation.
    if parse_conversation_id(name):
        sys.exit(f"FATAL: '{name}' is a conversation ID, not a group name.\n"
                 f"       Give the group's name first:  "
                 f"slack-export group <name> {name} ...")
    # A word that is not a conversation ID is a slip in the command itself -
    # usually a group name with a space, typed without quotes - so it cancels the
    # whole command (D48), before the Keychain or Slack is touched.
    not_ids = list(dict.fromkeys(raw for raw in args.conversations
                                 if parse_conversation_id(raw) is None))
    if not_ids:
        verb = ("is not a conversation ID" if len(not_ids) == 1
                else "are not conversation IDs")
        sys.exit(f"Nothing was changed: {', '.join(not_ids)} {verb}.\n"
                 f'A group name with spaces needs quotes: '
                 f'slack-export group "Lab Notes" C0123456789')

    store = load_directory()
    existed = name in store.get("groups", {})
    if args.remove or args.delete or args.list:
        # Only an existing group can be listed, lose members or be deleted; none
        # of these ever creates one, and none needs the Keychain or Slack.
        if not existed:
            sys.exit(_no_such_group(store, name))
        if args.list:
            # One code path with `list --groups NAME`, so the two cannot drift.
            _list_groups(store, [name], None, args.sort_by, args.reverse)
        elif args.delete:
            size = channel_directory.delete_group(store, name)
            save_directory(store)
            print(f"Deleted group {name}" + (f" ({_count(size)})." if size else ".")
                  + "\nIts conversations are still in your conversation index.")
        else:
            _remove_from_group(store, name, args.conversations)
        return

    if not args.conversations:
        if channel_directory.create_group(store, name) == "exists":
            size = len(store["groups"][name])
            print(f"Group {name} already exists ({_count(size)}). "
                  f"Nothing was changed.")
            return
        save_directory(store)
        print(f"Created group {name} (empty).")
        return

    # The same conversation typed twice (say, as an ID and as a link) is handled
    # once, in the order first typed.
    wanted = list(dict.fromkeys(parse_conversation_id(raw)
                                for raw in args.conversations))
    # Slack is contacted only if something is missing from the index, and then
    # once, before anything changes: a missing or over-scoped token stops the run
    # with nothing written.
    client = auth = None
    if any(not channel_directory.saved_in(store, cid) for cid in wanted):
        client = connect()
        _, auth = assert_read_only(client, quiet=True)

    added, already, failed = [], [], []
    for conversation_id in wanted:
        teams = channel_directory.saved_in(store, conversation_id)
        if not teams:
            reason = _index_from_slack(store, client, auth, conversation_id)
            if reason:
                failed.append((conversation_id, reason))
                continue
            teams = [auth["team_id"]]
        if len(teams) > 1:
            failed.append((conversation_id,
                           "in the conversation index for more than one "
                           "workspace (not supported yet)"))
            continue
        outcome = channel_directory.add_to_group(store, name, teams[0],
                                                 conversation_id)
        row = next(r for r in channel_directory.search(store, conversation_id)
                   if r["team_id"] == teams[0] and r["channel_id"] == conversation_id)
        (added if outcome == "added" else already).append(row)
    # One write, so a conversation new to the index and its place in the group are
    # saved together or not at all. Nothing is indexed without also being added.
    if added:
        save_directory(store)

    # Sections by outcome; added and already-in rows share one set of columns.
    lines = channel_directory.format_rows(added + already)
    sections = []
    if added:
        sections.append([f"Added to {name}:"]
                        + [f"  {line}" for line in lines[:len(added)]])
    if already:
        sections.append([f"Already in {name}:"]
                        + [f"  {line}" for line in lines[len(added):]])
    if failed:
        width = max(len(cid) for cid, _ in failed)
        sections.append(["Could not add:"]
                        + [f"  {cid.ljust(width)}  {why}" for cid, why in failed])
    if name in store.get("groups", {}):
        size = _count(len(store["groups"][name]))
        if added:
            summary = f"{name} now has {size}."
            if not existed:
                summary = f"Created group {name}. {summary}"
        else:
            summary = f"No changes - {name} still has {size}."
    else:
        summary = f"No changes - group {name} was not created."
    sections.append([summary])
    print("\n\n".join("\n".join(section) for section in sections))
    if failed:
        sys.exit(1)


# Words that, as the first argument, run something other than an export. A
# conversation ID can never be one of them: parse_conversation_id accepts only
# ID-shaped text and Slack links.
COMMANDS = {
    "list": list_command,
    "save": save_command,
    "group": group_command,
}


def copy_to_clipboard(document, wanted: bool) -> bool:
    """Put `document` on the clipboard, only if `wanted` (--copy) and there is
    something to copy. Returns whether it was copied."""
    if not (wanted and document):
        return False
    return subprocess.run(["pbcopy"], input=document, text=True).returncode == 0


def _fingerprint(text: str) -> str:
    """A file's contents, reduced to compare against the file on disk later."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _recorded_key(mapping: dict, path: Path):
    """The key under which `mapping` records `path`, or None.

    Usually the path itself. On a disk that ignores capitals (the macOS default)
    the same file can also have been recorded as Notes.md and now be asked for as
    notes.md, so a recorded path that is the very same file counts too.
    """
    if str(path) in mapping:
        return str(path)
    for key in mapping:
        try:
            if os.path.samefile(key, path):
                return key
        except OSError:
            continue
    return None


def settle_pending(archive, raw: dict) -> None:
    """Carry an interrupted run's finished writes into `raw`'s record of files.

    A run that died between its archive writes left "pending" entries: what each
    file was about to become, as a fingerprint. Where the file on disk matches,
    that write happened, so the file is recorded as written that far; where it
    does not, it never happened, and the file is still where the record says.
    Every entry is settled, not only this run's files, so a retry that writes
    elsewhere (another --out folder) loses track of nothing.
    """
    for key, entry in (archive or {}).get("pending", {}).items():
        try:
            on_disk = Path(key).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if _fingerprint(on_disk) == entry["sha256"]:
            _record_written(raw, Path(key), entry["through"], entry["run"])


def file_progress(raw: dict, path: Path):
    """How far a readable file has been written: (newest ts in it, run that wrote
    it), or None when the archive has no record of writing it."""
    key = _recorded_key(raw["rendered"], path)
    if key is None:
        return None
    return raw["rendered"][key], raw["rendered_run"].get(key, 0)


def _record_written(raw: dict, path: Path, through: str, run: int) -> None:
    """Record `path` as written through `through` by `run`, replacing any older
    record of the same file under another spelling."""
    for mapping in (raw["rendered"], raw["rendered_run"]):
        alias = _recorded_key(mapping, path)
        if alias is not None and alias != str(path):
            del mapping[alias]
    raw["rendered"][str(path)] = through
    raw["rendered_run"][str(path)] = run


def export_one(client: WebClient, job: dict, args, granted: list, auth: dict,
               position: int, total: int, compact: bool = False) -> None:
    """Fetch one conversation and write or top up its archive and transcripts.

    `job` is what was worked out before Slack was contacted: the conversation,
    the file stem, the archive path and the archive already on disk, if any, and
    `outputs`, the (.md, .txt) pair in each folder to write. Usually one pair; a
    group export passes every folder that holds a copy under this stem, so one
    fetch serves them all. `position` and `total` only label the progress banner
    when several are exported. `compact` prints a few lines instead of the full
    report, for a group export.

    Raises ExportFailed when Slack refuses the conversation (before any file for
    it is written) or a write fails (leaving what a retry needs to finish without
    repeating anything); the caller decides whether that ends the run.
    """
    conversation_id = job["conversation_id"]
    stem, archive = job["stem"], job["archive"]
    raw_path, outputs = job["raw_path"], job["outputs"]
    info_url = auth["url"]

    if not compact:
        print()
        print("=" * 63)
        of = f"  ({position} of {total})" if total > 1 else ""
        print(f" FETCHING {conversation_id}{of}")
        print("=" * 63)

    try:
        info = client.conversations_info(channel=conversation_id)["channel"]
    except SlackApiError as exc:
        raise ExportFailed(f"conversations.info failed: {exc.response['error']}")

    kind = "DM" if info.get("is_im") else ("private channel" if info.get("is_private")
                                           else "public channel")
    label = info.get("name") or f"DM with {info.get('user', 'unknown')}"
    if compact:
        print(f"  {kind}: {label}")
    else:
        print(f"  type       : {kind}")
        print(f"  label      : {label}")
        print()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    # Counts the runs that have written this archive; see settle_pending() and
    # incremental.parts_since().
    run = (archive or {}).get("run", 0) + 1

    try:
        if archive is None:
            messages = fetch_history(client, conversation_id)
            if args.no_threads:
                stats = {"threads": 0, "claimed": 0, "fetched": 0, "mismatches": []}
            else:
                print()
                stats = expand_threads(client, conversation_id, messages)
            added = {"new_top": len(messages), "new_replies": stats["fetched"],
                     "updated_threads": stats["threads"]}
        else:
            messages = archive["messages"]
            cutoff = archive.get("last_ts") or incremental.newest_ts(messages)
            if not compact:
                print(f"  topping up an export of {len(messages)} top-level "
                      f"messages")
                print(f"  last one seen {render_markdown.stamp(cutoff)}")
                print()

            # The top-level history is cheap - a few pages - and carries Slack's
            # own reply_count and latest_reply for every message. Comparing those
            # against the archive finds the threads worth re-reading, instead of
            # re-reading all of them.
            fresh = fetch_history(client, conversation_id)
            found = incremental.classify(messages, fresh, cutoff)

            replies_by_parent = {}
            if args.no_threads:
                found["moved_threads"] = []
            total_threads = len(found["moved_threads"])
            if total_threads:
                print(f"\n  {total_threads} thread(s) have moved since then",
                      flush=True)
            for index, parent_ts in enumerate(found["moved_threads"], start=1):
                replies_by_parent[parent_ts] = fetch_thread_replies(
                    client, conversation_id, parent_ts)
                if index % 10 == 0 or index == total_threads:
                    print(f"  thread {index:>4}/{total_threads} re-read", flush=True)

            added = incremental.merge(messages, found["new_top"], replies_by_parent,
                                      cutoff=cutoff, run=run)
            stats = {"threads": added["updated_threads"], "claimed": 0,
                     "fetched": added["new_replies"], "mismatches": []}
    except SlackApiError as exc:
        raise ExportFailed(exc.response["error"])

    broadcasts = flag_broadcast_duplicates(messages)
    reply_total = sum(len(m.get("thread_replies", [])) for m in messages)

    # The project's own folders are made private. An --out directory is not touched.
    make_private_dir(EXPORT_DIR)
    make_private_dir(RAW_DIR)
    raw = {
        "exported_at_utc": (archive or {}).get("exported_at_utc", stamp),
        "last_updated_utc": stamp,
        "last_ts": incremental.newest_ts(messages),
        "conversation_id": conversation_id,
        "conversation_type": kind,
        "conversation_label": label,
        "conversation_info": info,
        "granted_scopes": granted,
        # True only when every thread has actually been fetched, however many
        # runs that took; an earlier --no-threads run is caught up on later.
        "threads_expanded": all("thread_replies" in m for m in messages
                                if m.get("reply_count")),
        "top_level_count": len(messages),
        "reply_count": reply_total,
        "total_message_count": len(messages) + reply_total,
        "thread_broadcast_duplicates": broadcasts,
        # Per output file, the newest ts it has actually been written up to. Kept
        # per PATH because the same conversation can be rendered into several
        # folders, each of them a different number of runs behind.
        "rendered": dict((archive or {}).get("rendered", {})),
        # Per output file, the run that last wrote it; with "run", this is how a
        # reply backfilled into an old thread reaches files written before it.
        "run": run,
        "rendered_run": dict((archive or {}).get("rendered_run", {})),
        # Between the two archive writes below: what each file is being turned
        # into. Empty once a run has finished.
        "pending": {},
        "messages": messages,
    }

    participants = render_markdown.collect_user_ids(messages, info)
    if not compact:
        print()
        print(f"  resolving {len(participants)} participant names ...", flush=True)
    users = render_markdown.build_user_map(client, participants)
    when = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")

    settle_pending(archive, raw)

    # Every file's new contents are worked out before anything is written, so a
    # failure while rendering leaves every file exactly as it was.
    document = None      # what goes on the clipboard
    written = []         # (path, what happened, messages added) for the summary
    planned = []         # (path, new contents, what, count) still to be written
    current = []         # files already up to date
    files = [(path, module) for md_path, txt_path in outputs
             for path, module in ((txt_path, render_text),
                                  (md_path, render_markdown))]
    for path, module in files:
        is_md = module is render_markdown

        if not path.exists():
            # Nothing here yet - write the whole conversation, however much of it
            # arrived on earlier runs.
            full = (module.render(raw, users, info_url, when) if is_md
                    else module.render(raw, users, when))
            planned.append((path, full, "written in full", None))
            if is_md:
                document = full
            continue

        progress = file_progress(raw, path)
        if progress is None:
            # The archive tracks progress per absolute path, so a file that was
            # renamed or moved is indistinguishable from somebody else's file of
            # the same name. Appending blind could duplicate or lose messages.
            written.append((path, "SKIPPED - no record of writing this file", None))
            continue

        through, done_run = progress
        parts = incremental.parts_since(messages, through, done_run)
        if incremental.is_empty(parts):
            written.append((path, "already up to date", None))
            current.append(path)
            continue

        chunk = (module.render_append(parts, users, info_url,
                                      conversation_id, when) if is_md
                 else module.render_append(parts, users, when))
        body = swap_header(path.read_text(encoding="utf-8"),
                           module.header(raw, users, when), module.HEADER_RULE)
        if body is None:
            written.append((path, "SKIPPED - no header found, not ours", None))
            continue

        count = len(parts["fresh"]) + sum(len(e["replies"])
                                          for e in parts["catch_up"])
        planned.append((path, body.rstrip("\n") + "\n" + chunk, "appended", count))
        if is_md:
            document = chunk

    # Three steps, because several files cannot be replaced as one: (1) the
    # archive, holding everything fetched and a fingerprint of what each file is
    # about to become; (2) the files; (3) the archive again, recording them as
    # written. Each single write is atomic. A run that dies anywhere in between
    # leaves a retry able to tell, file by file, whether its write happened (see
    # settle_pending), so nothing is appended twice and nothing fetched is lost.
    for path in current:
        _record_written(raw, path, raw["last_ts"], run)
    raw["pending"] = {str(path): {"through": raw["last_ts"], "run": run,
                                  "sha256": _fingerprint(text)}
                      for path, text, _, _ in planned}
    target = raw_path
    try:
        write_atomic(raw_path, json.dumps(raw, indent=2, ensure_ascii=False))
        for target, text, what, count in planned:
            write_atomic(target, text)
            written.append((target, what, count))
        for path, _, _, _ in planned:
            _record_written(raw, path, raw["last_ts"], run)
        raw["pending"] = {}
        target = raw_path
        write_atomic(raw_path, json.dumps(raw, indent=2, ensure_ascii=False))
    except OSError as exc:
        raise ExportFailed(
            f"could not write {display_path(target)} ({exc.strerror or exc}).\n"
            f"       Run the same command again once that is fixed: it carries on "
            f"from where it stopped, without repeating anything.") from exc

    skipped = any(what.startswith("SKIPPED") for _, what, _ in written)
    # Only now that every file is safely written, and only if none was skipped:
    # LAST EXPORTED means the archive AND every readable file are up to date. In
    # the store's own time format, not the archive's compact stamp, so export
    # times always sort together.
    listed = record_in_directory(
        auth, info, conversation_id, stem,
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        complete=not skipped)

    copied = copy_to_clipboard(document, args.copy)

    if compact:
        if archive is None:
            print(f"  new export : {len(messages)} top-level, {reply_total} replies")
        else:
            print(f"  new since  : {added['new_top']} top-level, "
                  f"{added['new_replies']} replies")
        for path, what, count in written:
            detail = f"{what} (+{count})" if count else what
            print(f"  {path.suffix.lstrip('.').upper():<9}: {display_path(path)}"
                  f"   [{detail}]")
        if listed.startswith("NOT updated"):
            print(f"  conversation index: {listed}")
        if skipped:
            print("  A file was left alone (no record of writing it); LAST EXPORTED "
                  "is not\n  updated for this one until it is moved aside and "
                  "re-exported.")
        return

    print()
    print("=" * 63)
    print(" RESULT")
    print("=" * 63)
    if archive is None:
        print(f"  new export : {len(messages)} top-level, {reply_total} replies")
    else:
        print(f"  new since  : {added['new_top']} top-level message(s), "
              f"{added['new_replies']} reply/replies "
              f"across {added['updated_threads']} thread(s)")
    if stats["mismatches"]:
        print(f"  MISMATCHES : {len(stats['mismatches'])} thread(s) returned a "
              f"different number of replies than reply_count claimed")
        for ts, claimed, got in stats["mismatches"][:5]:
            print(f"               ts={ts}  claimed={claimed}  got={got}")
    if broadcasts:
        print(f"  broadcasts : {broadcasts} thread reply/replies were also "
              f"posted to the channel; flagged _also_in_thread, not deduped")
    print(f"  TOTAL      : {len(messages) + reply_total} messages held "
          f"({reply_total} of them thread replies)")
    print(f"  people     : {len(users)} names resolved")
    print()
    note = name_note(job["typed"], stem, conversation_id)
    if note:
        print(f"  file name: {note}")
    for path, what, count in written:
        size = f"{path.stat().st_size / 1024:.1f} KB" if path.exists() else "-"
        detail = f"{what} (+{count})" if count else what
        print(f"  {path.suffix.lstrip('.').upper():<9}: {display_path(path)}"
              f"   [{detail}, {size}]")
    print(f"  ARCHIVE  : {display_path(raw_path)}"
          f"   ({raw_path.stat().st_size / 1024:.1f} KB)")
    if copied:
        print("  clipboard: copied - ready to paste")
    elif args.copy:
        print("  clipboard: not copied (nothing new, or pbcopy failed)")
    else:
        print("  clipboard: not copied (add --copy to copy the Markdown)")
    print(f"  conversation index: {listed}")

    if skipped:
        print()
        print("  A file was left alone: either this tool did not write it, or it "
              "was\n  moved or renamed since, so there is no record of how far it "
              "got.\n  Move it aside and re-run to write a fresh copy. LAST "
              "EXPORTED is not\n  updated until every file is.")



def _wants_group_export(argv) -> bool:
    """True when the command line asks to export a group: --group anywhere."""
    return any(word == "--group" or word.startswith("--group=") for word in argv)


def _member_label(store: dict, team_id: str, conversation_id: str) -> str:
    """What the conversation index calls a conversation: nickname, else Slack name."""
    entry = store["workspaces"].get(team_id, {}).get("channels", {}).get(
        conversation_id, {})
    return entry.get("nickname") or entry.get("slack_name") or ""


def _group_jobs(conversation_id: str, places: list) -> list:
    """One job per archive name (stem), holding every folder written under it.

    A stem is one archive with one incremental history, so it is fetched once
    and every folder holding a copy under it is written from that fetch. Two
    stems are two archives, each a different number of runs behind, so each is
    fetched for itself: one fetch cannot be trusted to bring both up to date.
    """
    jobs = {}
    for place in places:
        stem, folder = place["stem"], place["folder"]
        job = jobs.setdefault(stem, {
            "conversation_id": conversation_id, "typed": "", "stem": stem,
            "raw_path": RAW_DIR / f"{stem}.raw.json", "outputs": [],
            "fresh": place["fresh"]})
        job["outputs"].append((folder / f"{stem}.md", folder / f"{stem}.txt"))
    return list(jobs.values())


def group_export(argv) -> None:
    """`slack-export --group NAME`: export or top up every conversation in a group.

    Works out every destination first, offline (see destinations.py): each copy
    that still exists is topped up where it is, under its own name; one with no
    copy left gets a fresh one named by its ID. Anything that could overwrite or
    mix conversations - two members under one name, an archive holding another
    conversation, a path git could commit, a stray temporary file - stops the
    whole run before Slack is contacted. After that, a member that fails is
    reported and skipped, and the rest carry on; the exit code is 1 if any failed.
    """
    parser = argparse.ArgumentParser(
        prog="slack-export",
        usage="slack-export --group name [--out DIR] [--no-threads]",
        description="Export every conversation in a group, one after another. "
                    "Each copy already exported is topped up where it is; a "
                    "conversation with no copy left gets a new one named by its "
                    "ID. Put a name with spaces in quotes.")
    parser.add_argument("--group", action="append", required=True, metavar="name",
                        help="the group to export")
    parser.add_argument("--out", metavar="DIR",
                        help="only update copies in DIR, adding new ones there")
    parser.add_argument("--no-threads", action="store_true",
                        help="top-level messages only")
    # Accepted only to be refused with a reason, rather than argparse's own error.
    parser.add_argument("--copy", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--no-clipboard", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("words", nargs="*", help=argparse.SUPPRESS)
    args = parser.parse_intermixed_args(argv)

    if len(args.group) > 1:
        sys.exit("FATAL: one group at a time. Run slack-export --group once for "
                 "each.")
    if args.copy:
        sys.exit("FATAL: --copy works with one conversation at a time, not with "
                 "--group.")
    if args.words:
        if any(parse_conversation_id(word) for word in args.words):
            sys.exit("FATAL: --group cannot be combined with conversation IDs.\n"
                     "       Export the group and the other conversations in "
                     "separate commands.")
        sys.exit(f"FATAL: --group takes one name; {_quote(args.words[0])} was left "
                 f"over.\n       Put a name with spaces in quotes: "
                 f"--group \"{args.group[0]} {' '.join(args.words)}\"")

    _self_test()
    incremental._self_test()

    out_dir = resolve_out_dir(args.out) if args.out is not None else None
    store = load_directory()
    name = args.group[0].strip()
    if name not in store.get("groups", {}):
        sys.exit(_no_such_group(store, name))
    members = []
    for member in store["groups"][name]:
        pair = (member["workspace"], member["channel"])
        if pair not in members:
            members.append(pair)
    if not members:
        print(f"Group '{name}' has no conversations in it, so there is nothing to "
              f"export.\nAdd some with: slack-export group {_quote(name)} ID ...")
        return

    # Where every member goes, worked out before anything is written.
    try:
        copies = destinations.recorded_copies(RAW_DIR)
    except destinations.UnreadableArchive as exc:
        sys.exit(f"FATAL: {display_path(exc.path)} is unreadable ({exc.reason}).\n"
                 f"       Every archive is read to find where each conversation's "
                 f"files are, so\n       nothing is exported until it is fixed or "
                 f"moved out of {display_path(RAW_DIR)}.")
    plan = {conversation_id: destinations.destinations(
                conversation_id, copies, EXPORT_DIR, out_dir)
            for _, conversation_id in members}

    clash = destinations.clashes(plan)
    if clash:
        lines = [f"       {', '.join(ids)} would all be saved under \"{stem}\""
                 for stem, ids in clash]
        sys.exit("FATAL: two conversations in this group would share files:\n"
                 + "\n".join(lines)
                 + "\n       (names that differ only in capitals count as the "
                   "same on most Macs).\n       Nothing was exported.")

    jobs = {conversation_id: _group_jobs(conversation_id, places)
            for conversation_id, places in plan.items()}
    every_job = [job for member_jobs in jobs.values() for job in member_jobs]

    # A fresh copy's archive (named by its ID) must not belong to anything else:
    # topping it up would mix two conversations in one file.
    owner = {stem.casefold(): conversation_id
             for conversation_id, entry in copies.items()
             for stem in entry["stems"]}
    for job in every_job:
        held = owner.get(job["stem"].casefold(), job["conversation_id"])
        if held != job["conversation_id"]:
            sys.exit(f"FATAL: {display_path(job['raw_path'])} already holds a "
                     f"different conversation.\n"
                     f"       held : {held}\n"
                     f"       asked: {job['conversation_id']}\n"
                     f"       Move that archive aside. Nothing was exported.")

    paths = [path for job in every_job
             for pair in job["outputs"] for path in pair] + [
             job["raw_path"] for job in every_job]
    refuse_if_committable(paths)
    refuse_if_temp_paths_taken(paths)

    # A fresh copy goes where there is no record of a copy; a file already
    # there was not written by any export of this conversation, so it would be
    # left alone and the pair half-written. That member is skipped instead.
    failed = {}
    for conversation_id, member_jobs in jobs.items():
        for job in member_jobs:
            if not job["fresh"]:
                continue
            taken = [path for pair in job["outputs"] for path in pair
                     if path.exists()]
            if taken:
                failed[conversation_id] = (
                    f"{display_path(taken[0])} already exists, but no export of "
                    f"this conversation\n               wrote it. Move it aside "
                    f"to export this one.")

    print("=" * 63)
    print(f" GROUP {_quote(name)}: {_count(len(members))}")
    print("=" * 63)
    print("  each place is a .md and .txt pair")
    for team_id, conversation_id in members:
        label = _member_label(store, team_id, conversation_id)
        places = plan[conversation_id]
        existing = sum(1 for place in places if not place["fresh"])
        several = f"   ({existing} copies, each updated)" if existing > 1 else ""
        print(f"  {conversation_id:<13}{label}{several}".rstrip())
        for place in places:
            where = display_path(place["folder"] / place["stem"])
            why = f"   (new: {place['fresh']})" if place["fresh"] else ""
            print(f"      -> {where}{why}")
        if out_dir is not None:
            elsewhere = len(copies.get(conversation_id, {}).get("copies", [])) - sum(
                1 for p in places if not p["fresh"])
            if elsewhere:
                print(f"      ({elsewhere} other "
                      f"{'copy' if elsewhere == 1 else 'copies'} outside "
                      f"{display_path(out_dir)} left alone)")
    print()

    if out_dir is not None:
        create_out_dir(out_dir)

    client = connect()
    print("=" * 63)
    print(" PRE-FLIGHT")
    print("=" * 63)
    granted, auth = assert_read_only(client)

    for team_id, conversation_id in members:
        if team_id != auth["team_id"] and conversation_id not in failed:
            saved_in = store["workspaces"].get(team_id, {}).get("name") or team_id
            failed[conversation_id] = (
                f"saved from another Slack workspace ({saved_in}); this token "
                f"is for {auth['team']}")

    # One after another: Slack rate-limits per token.
    for position, (team_id, conversation_id) in enumerate(members, start=1):
        print()
        label = _member_label(store, team_id, conversation_id)
        print(f"[{position}/{len(members)}] {conversation_id}  {label}".rstrip())
        if conversation_id in failed:
            print(f"  SKIPPED    : {failed[conversation_id]}")
            continue
        for job in jobs[conversation_id]:
            archive = load_archive(job["raw_path"])
            try:
                if archive and archive.get("conversation_id") != conversation_id:
                    raise ExportFailed(f"{display_path(job['raw_path'])} now holds "
                                       f"a different conversation")
                job["archive"] = archive
                export_one(client, job, args, granted, auth, position,
                           len(members), compact=True)
            except ExportFailed as exc:
                failed[conversation_id] = str(exc)
                print(f"  FAILED     : {exc} - carrying on")
                break

    done = len(members) - len(failed)
    print()
    print("=" * 63)
    print(f" DONE: {done} of {_count(len(members))} exported "
          f"(group {_quote(name)})")
    print("=" * 63)
    for _, conversation_id in members:
        reason = failed.get(conversation_id)
        if reason:
            print(f"  {conversation_id:<13}FAILED: {reason}")
            continue
        wrote = [display_path(md) for job in jobs[conversation_id]
                 for md, _ in job["outputs"]]
        print(f"  {conversation_id:<13}{wrote[0]}")
        for more in wrote[1:]:
            print(f"  {'':<13}{more}")
    if failed:
        sys.exit(1)


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] in COMMANDS:
        COMMANDS[argv[0]](argv[1:])
        return
    if _wants_group_export(argv):
        group_export(argv)
        return

    parser = argparse.ArgumentParser(
        prog="slack-export", description=__doc__,
        # Spelled out rather than left to argparse, which lists the flags first.
        # What someone needs to type first is the conversation, so that comes
        # first; the flags are optional and can go in any order after it.
        usage="slack-export conversation_id [name ...] "
              "[--out DIR] [--no-threads] [--copy]\n"
              "       slack-export conversation_id [name ...] "
              "conversation_id [name ...] ... [--out DIR] [--no-threads]\n"
              "       slack-export --group name [--out DIR] [--no-threads]\n"
              "       slack-export save conversation_id [nickname ...]\n"
              "       slack-export list [search ...]\n"
              "       slack-export list --groups [group ...] [--search word ...]\n"
              "       slack-export group name [ID ...]\n"
              "       slack-export group name --remove ID [ID ...]\n"
              "       slack-export group name --delete\n"
              "       slack-export group name --list\n"
              "       slack-export group --list",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    # No default, and nargs is not '?' - the target is REQUIRED. This tool never
    # enumerates the workspace or guesses what to export.
    parser.add_argument("conversation_id",
                        help="Slack conversation ID - C... (channel) or D... (DM) "
                             "- or a Slack link containing one")
    # Optional, and the LAST positional: every leftover word is joined into the file
    # name, so quotes are welcome but not required -
    #   slack-export C0123456789 Project Planning
    # - up to the next conversation ID, which starts another export; see
    # split_targets.
    parser.add_argument("name", nargs="*",
                        help="what to call the files of the ID before it "
                             "(default: that ID)")
    parser.add_argument("--no-threads", action="store_true",
                        help="top-level messages only, skipping thread replies "
                             "(much faster, but most of a busy channel is in "
                             "the threads)")
    # Off by default: a long export pasted by surprise can flood or freeze
    # whatever it lands in.
    parser.add_argument("--copy", action="store_true",
                        help="also copy the Markdown to the clipboard "
                             "(default: do not)")
    # The old opt-out, from when copying was automatic. Still accepted, silently,
    # so a habit or script that types it keeps working; it changes nothing now.
    parser.add_argument("--no-clipboard", action="store_true",
                        help=argparse.SUPPRESS)
    parser.add_argument("--out", metavar="DIR",
                        help="where to put the .md and .txt "
                             "(default: exports/; the .raw.json archive always "
                             "stays in exports/raw/)")
    # Intermixed, so a flag can sit between targets as well as after them -
    # `ID Lab --out DIR ID Meeting` - and every word that is not a flag still
    # reaches split_targets in the order it was typed.
    args = parser.parse_intermixed_args()

    _self_test()
    incremental._self_test()

    # Before anything else: is each of those actually a conversation ID?
    targets = split_targets(args.conversation_id, args.name)
    if args.copy and len(targets) > 1:
        sys.exit("FATAL: --copy works with one conversation at a time.\n"
                 "       Leave it off to export several, or export the one you "
                 "want copied by itself.")
    # Resolved up front: a bad path should fail now, not after the fetch.
    out_dir = resolve_out_dir(args.out)

    # The stem carries no timestamp, so a re-run under the same name lands on the
    # same three files and tops them up rather than making a fourth copy.
    jobs = []
    for conversation_id, typed in targets:
        stem = safe_stem(typed) or conversation_id
        md_path, txt_path = out_dir / f"{stem}.md", out_dir / f"{stem}.txt"
        jobs.append({"conversation_id": conversation_id, "typed": typed,
                     "stem": stem, "raw_path": RAW_DIR / f"{stem}.raw.json",
                     "md_path": md_path, "txt_path": txt_path,
                     "outputs": [(md_path, txt_path)]})

    # Two conversations under one name would share one set of files: the second
    # would overwrite the first's archive and be refused by its transcripts. Names
    # that differ only in punctuation ("A&B", "A B") meet here too, as A-B, and so
    # do names that differ only in capitals: macOS and Windows disks normally treat
    # Notes.md and notes.md as one file. Compared that way everywhere, so the same
    # command never means different things on different disks.
    owner = {}
    for job in jobs:
        other = owner.setdefault(job["stem"].casefold(), job)
        if other["conversation_id"] == job["conversation_id"]:
            continue
        if other["stem"] == job["stem"]:
            where = f"under the file name \"{job['stem']}\""
        else:
            where = (f"under the file names \"{other['stem']}\" and "
                     f"\"{job['stem']}\",\n       which most Macs treat as the "
                     f"same file")
        sys.exit(f"FATAL: {other['conversation_id']} and {job['conversation_id']} "
                 f"would both be saved {where}.\n"
                 f"       Give each conversation its own name.")

    # Before any folder is created, the Keychain is read, or Slack is contacted:
    # a private conversation is never written where git could commit it. Every
    # conversation is checked first, so a problem with the last one does not
    # turn up after the others have been fetched.
    refuse_if_committable([job[key] for job in jobs
                           for key in ("md_path", "txt_path", "raw_path")])
    refuse_if_temp_paths_taken([job[key] for job in jobs
                                for key in ("md_path", "txt_path", "raw_path")])
    if args.out is not None:
        create_out_dir(out_dir)

    for job in jobs:
        archive = load_archive(job["raw_path"])
        if archive and archive.get("conversation_id") != job["conversation_id"]:
            # Without a timestamp in the name, two different conversations called
            # the same thing would silently merge into one transcript. Refuse.
            sys.exit(
                f"FATAL: {display_path(job['raw_path'])} already holds a different "
                f"conversation.\n"
                f"       held : {archive['conversation_id']} "
                f"({archive.get('conversation_label', '?')})\n"
                f"       asked: {job['conversation_id']}\n"
                f"       Pick another name, or move the existing files aside.")
        job["archive"] = archive

    client = connect()

    print("=" * 63)
    print(" PRE-FLIGHT")
    print("=" * 63)
    granted, auth = assert_read_only(client)

    # One after another, not in parallel: Slack rate-limits per token. One that
    # Slack refuses is skipped and the rest still go ahead, so the order they were
    # typed in never decides what gets saved.
    failed = {}
    for position, job in enumerate(jobs, start=1):
        try:
            export_one(client, job, args, granted, auth, position, len(jobs))
        except ExportFailed as exc:
            if len(jobs) == 1:
                sys.exit(f"FATAL: {exc}")
            failed[job["conversation_id"]] = str(exc)
            print(f"  FAILED     : {exc} - skipped, carrying on")

    if len(jobs) > 1:
        print()
        print("=" * 63)
        print(f" DONE: {len(jobs) - len(failed)} of {len(jobs)} exported")
        print("=" * 63)
        for job in jobs:
            reason = failed.get(job["conversation_id"])
            where = (f"FAILED: {reason}" if reason
                     else display_path(job["md_path"]))
            print(f"  {job['conversation_id']:<13}{where}")
    # Still an error to the shell, so a script can tell a partial run from a
    # complete one. The summary above has already said which failed.
    if failed:
        sys.exit(1)


# Python runs a file top to bottom on import, so this guard means main() fires only
# when the file is executed directly - not if something later imports it as a module.
if __name__ == "__main__":
    main()
