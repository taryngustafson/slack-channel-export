"""Turn a raw Slack export into a plain text transcript.

The Markdown renderer produces a document; this produces something you can just
read. No markup, no permalinks, no reactions - a timestamp, a name, and what was
said. Oldest first, top to bottom, a blank line between messages, and thread
replies indented four spaces so it is obvious they hang off the message above
rather than standing alone.

Two ways in:
    written automatically alongside the .md on every export run, and
    ./.venv/bin/python tools/render_text.py exports/SOMETHING.raw.json
        to convert an export you already have, without re-downloading it.

Run it with no arguments to see the format rendered from synthetic messages:
    ./.venv/bin/python tools/render_text.py
"""

import sys
from pathlib import Path

# Same directory as this script, so a plain import works when run as a script.
from render_markdown import (CHANNEL_REF, LINK_BARE, LINK_LABELLED, SPECIAL_REF,
                             USER_REF, gist, stamp)

# Four spaces, not a tab: tabs render at a different width in every editor, and
# this file's whole job is to look the same wherever it is opened.
INDENT = "    "

# The line that closes the header block. Also the seam an append run splits on to
# swap a stale header for a current one, so it must stay a full-width unique line.
HEADER_RULE = "=" * 70


def plain(text: str, users: dict) -> str:
    """Strip Slack's markup down to readable prose.

    Deliberately different from render_markdown.humanise, which emits Markdown
    link syntax. Here a labelled link becomes "label (url)" - the label alone
    would silently drop the destination, and Markdown brackets are exactly the
    noise this format exists to avoid.
    """
    if not text:
        return ""
    text = USER_REF.sub(lambda m: "@" + users.get(m.group(1), m.group(1)), text)
    text = CHANNEL_REF.sub(lambda m: "#" + m.group(1), text)
    text = SPECIAL_REF.sub(lambda m: "@" + m.group(1), text)
    text = LINK_LABELLED.sub(
        lambda m: m.group(1) if m.group(2) == m.group(1)
        else f"{m.group(2)} ({m.group(1)})", text)
    text = LINK_BARE.sub(lambda m: m.group(1), text)
    return text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")


def _block(message: dict, users: dict, indent: str = "") -> list:
    """One message: a header line, then its text, every line at the same indent."""
    who = users.get(message.get("user", ""), message.get("user", "unknown"))
    body = plain(message.get("text", ""), users).rstrip()

    # A file share or a bare attachment arrives with no text at all. Saying so
    # beats an unexplained gap in the transcript.
    if not body:
        body = "(no text - probably a file or an attachment)"

    lines = [f"{indent}{stamp(message['ts'])}  {who}"]
    lines += [f"{indent}{line}".rstrip() for line in body.splitlines()]
    lines.append("")
    return lines


def conversation_label(export: dict, users: dict) -> str:
    """The human name for this conversation - a DM is named after the person."""
    label = export.get("conversation_label", export["conversation_id"])
    info = export.get("conversation_info", {})
    if info.get("is_im") and info.get("user"):
        label = f"DM with {users.get(info['user'], info['user'])}"
    return label


def header(export: dict, users: dict, updated: str = None) -> str:
    """The block at the top of the transcript, ending in HEADER_RULE.

    Regenerated in full on every append, so the counts describe the file as it
    stands rather than as it was on the day it was first written.
    """
    total = export.get("total_message_count", 0)
    replies = export.get("reply_count", 0)
    lines = [
        conversation_label(export, users),
        f"{total} messages ({replies} of them thread replies)",
    ]
    if updated:
        lines.append(f"last updated {updated}")
    lines += [HEADER_RULE, ""]
    return "\n".join(lines)


def render(export: dict, users: dict, updated: str = None) -> str:
    """Render a raw export dict as a plain text transcript."""
    # conversations.history returns newest-first; a transcript reads forwards.
    messages = sorted(export["messages"], key=lambda m: float(m["ts"]))
    lines = [header(export, users, updated)]

    for message in messages:
        # A thread_broadcast copy already appears inside its own thread; rendering
        # it here as well would show the same message twice.
        if message.get("_also_in_thread"):
            continue

        lines += _block(message, users)
        for reply in sorted(message.get("thread_replies", []),
                            key=lambda m: float(m["ts"])):
            lines += _block(reply, users, indent=INDENT)

    return "\n".join(lines)


def render_append(parts: dict, users: dict, when: str) -> str:
    """The chunk added to the bottom of an existing transcript.

    Two sections, because new messages and new replies to OLD messages cannot be
    presented the same way. A brand new message reads normally, in order, with its
    own replies beneath it. A reply that landed on a message from three weeks ago
    belongs hundreds of lines up the file, under a parent that is already written
    and must not be rewritten - so it is shown here with a short quotation of the
    thread it answers, enough to place it without scrolling back.
    """
    lines = ["", HEADER_RULE, f"new as of {when}", HEADER_RULE, ""]

    for message in parts["fresh"]:
        if message.get("_also_in_thread"):
            continue
        lines += _block(message, users)
        for reply in sorted(message.get("thread_replies", []),
                            key=lambda m: float(m["ts"])):
            lines += _block(reply, users, indent=INDENT)

    if parts["catch_up"]:
        lines += ["-" * 70, "new replies to earlier threads", "-" * 70, ""]
        for entry in parts["catch_up"]:
            parent = entry["parent"]
            who = users.get(parent.get("user", ""), parent.get("user", "unknown"))
            excerpt = gist(parent.get("text", ""), users)
            lines.append(f"re: {stamp(parent['ts'])}  {who}"
                         f"{f'  -  {excerpt}...' if excerpt else ''}")
            lines.append("")
            for reply in entry["replies"]:
                lines += _block(reply, users, indent=INDENT)

    return "\n".join(lines)


def _demo() -> None:
    """Print the output format using synthetic messages - no real data involved."""
    users = {"U01": "user-a", "U02": "user-b", "U03": "user-c"}
    export = {
        "conversation_id": "C0EXAMPLE99",
        "conversation_label": "example-channel",
        "total_message_count": 4, "reply_count": 2,
        "messages": [
            {"ts": "1787000000.000100", "user": "U01",
             "text": "Has anyone checked the <http://example.com/trail|trail map> "
                     "since the bridge reopened?",
             "thread_replies": [
                 {"ts": "1787000060.000200", "user": "U02",
                  "text": "Yes - the loop is open again. "
                          "<@U03> found two picnic spots."},
                 {"ts": "1787000900.000300", "user": "U03",
                  "text": "The overlook and the creek.\n"
                          "Both have tables, only one has shade."},
             ]},
            {"ts": "1787003600.000400", "user": "U02",
             "text": "Reminder for <!here>: carpool leaves Saturday at 8."},
        ],
    }
    print(render(export, users))


def _convert(raw_path: Path) -> None:
    """Re-render an export already on disk, resolving names from Slack.

    The .raw.json stores user IDs, not names, so this needs the token - but it
    re-reads nothing from the conversation itself. That is the whole point of
    keeping the raw file: a format change never means downloading it again.
    """
    import json

    from slack_sdk import WebClient

    import export_conversation
    import render_markdown

    # foo.raw.json -> foo.txt, sitting beside the file it came from.
    out_path = raw_path.with_name(raw_path.name.replace(".raw.json", "") + ".txt")
    # Same guard as a normal export, and before the Keychain or Slack is touched.
    export_conversation.refuse_if_committable([out_path])

    export = json.loads(raw_path.read_text())
    client = WebClient(token=export_conversation.keychain_token())
    participants = render_markdown.collect_user_ids(
        export["messages"], export.get("conversation_info", {}))
    print(f"  resolving {len(participants)} participant names ...", flush=True)
    users = render_markdown.build_user_map(client, participants)

    # Owner-only and atomic, exactly like the files a normal export writes.
    export_conversation.write_atomic(out_path, render(export, users))
    print(f"  wrote {out_path}   ({out_path.stat().st_size / 1024:.1f} KB)")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        _convert(Path(sys.argv[1]))
    else:
        _demo()
