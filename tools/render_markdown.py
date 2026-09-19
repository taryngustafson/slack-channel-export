"""Turn a raw Slack export into a readable Markdown document.

Format decisions:
  - Threads INLINE: replies indented directly beneath their parent, in the position
    the parent occupies. Reads like the conversation actually happened.
  - Per message: timestamp, name, text, and a permalink. Nothing else - no reactions,
    no edit markers, no user IDs.

Run this file directly to see the output format rendered from synthetic messages:
    ./.venv/bin/python tools/render_markdown.py
"""

import re
from datetime import datetime

# Slack wraps links as <url|label> or <url>, channels as <#C123|name>, and users as
# <@U123>. Left alone these make the text unreadable outside Slack.
LINK_LABELLED = re.compile(r"<(https?://[^|>]+)\|([^>]+)>")
LINK_BARE = re.compile(r"<(https?://[^|>]+)>")
CHANNEL_REF = re.compile(r"<#[CG][A-Z0-9]+\|([^>]+)>")
USER_REF = re.compile(r"<@([UW][A-Z0-9]+)>")
SPECIAL_REF = re.compile(r"<!(here|channel|everyone)>")

# Closes the header block, and the seam an append run splits on to replace a stale
# header. Always the FIRST occurrence in the file, so a --- inside a message is
# never mistaken for it.
HEADER_RULE = "---"

# Unwraps [label](url) back to label. Used only for the one-line excerpts that
# point at a thread from elsewhere in the document, where a full URL would eat the
# whole line and a live link inside a quoted phrase reads as clutter.
MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")


def collect_user_ids(messages: list, info: dict = None) -> set:
    """Every user ID that appears in this export - sender, @mention, or DM partner."""
    found = set()
    if info and info.get("is_im") and info.get("user"):
        found.add(info["user"])   # they may never have posted; still name the DM
    for message in messages:
        for item in [message] + message.get("thread_replies", []):
            if item.get("user"):
                found.add(item["user"])
            found.update(USER_REF.findall(item.get("text", "")))
    return found


def build_user_map(client, user_ids) -> dict:
    """Resolve only the people who actually appear in this conversation.

    users.list would page through the entire workspace directory, which can be
    very large and is unnecessary when only the participants here are needed. One
    users.info per participant is both far cheaper and pulls nothing about anyone
    who is not already in the export.

    An unresolvable ID (deactivated account, a bot) falls back to the raw ID rather
    than failing the whole render.
    """
    users = {}
    for user_id in sorted(user_ids):
        try:
            profile = client.users_info(user=user_id)["user"]
        except Exception:
            users[user_id] = user_id
            continue
        detail = profile.get("profile", {})
        users[user_id] = (detail.get("display_name")
                          or detail.get("real_name")
                          or profile.get("real_name")
                          or profile.get("name")
                          or user_id)
    return users


def humanise(text: str, users: dict) -> str:
    """Replace Slack's markup with something readable in a plain document."""
    if not text:
        return ""
    text = USER_REF.sub(lambda m: "@" + users.get(m.group(1), m.group(1)), text)
    text = CHANNEL_REF.sub(lambda m: "#" + m.group(1), text)
    text = SPECIAL_REF.sub(lambda m: "@" + m.group(1), text)
    text = LINK_LABELLED.sub(lambda m: f"[{m.group(2)}]({m.group(1)})", text)
    text = LINK_BARE.sub(lambda m: m.group(1), text)
    return text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")


def permalink(workspace_url: str, channel_id: str, ts: str,
              parent_ts: str = None) -> str:
    """Build a Slack deep link without spending an API call per message.

    chat.getPermalink would cost one request per message; the URL is entirely
    derivable from the workspace, channel, and timestamp.
    """
    anchor = "p" + ts.replace(".", "")
    url = f"{workspace_url.rstrip('/')}/archives/{channel_id}/{anchor}"
    if parent_ts:
        url += f"?thread_ts={parent_ts}&cid={channel_id}"
    return url


def stamp(ts: str) -> str:
    """Slack timestamps are epoch seconds as a string. Render in local time."""
    return datetime.fromtimestamp(float(ts)).astimezone().strftime("%Y-%m-%d %H:%M")


def gist(text: str, users: dict, width: int = 60) -> str:
    """A one-line excerpt of a message, flattened and stripped of link syntax."""
    flat = MD_LINK.sub(lambda m: m.group(1), humanise(text, users))
    flat = " ".join(flat.split())
    return flat[:width].rstrip()


def _line(message: dict, users: dict, workspace_url: str, channel_id: str,
          indent: str = "", parent_ts: str = None) -> list:
    """One message as Markdown: timestamp, name, text, permalink."""
    who = users.get(message.get("user", ""), message.get("user", "unknown"))
    link = permalink(workspace_url, channel_id, message["ts"], parent_ts)
    body = humanise(message.get("text", ""), users)

    # A truly empty line TERMINATES a Markdown blockquote, so inside an indented
    # reply the "blank" line must still carry the quote marker or the thread loses
    # its indentation the moment it is pasted anywhere.
    blank = indent.rstrip() if indent else ""

    out = [f"{indent}**{stamp(message['ts'])} — {who}** · [↗]({link})", blank]
    for line in (body.splitlines() or [""]):
        out.append(f"{indent}{line}" if line else blank)
    out.append(blank)
    return out


def header(export: dict, users: dict, updated: str = None) -> str:
    """The document header, ending in HEADER_RULE.

    Regenerated on every append so the counts describe the document as it now
    stands, not as it was the day it was first written.
    """
    channel_id = export["conversation_id"]
    label = export.get("conversation_label", channel_id)

    # A DM has no name, so the export labelled it with the other person's raw user ID.
    # By now that ID has been resolved along with everyone else - use the name.
    info = export.get("conversation_info", {})
    if info.get("is_im") and info.get("user"):
        label = f"DM with {users.get(info['user'], info['user'])}"

    provenance = (f"`{channel_id}` · {export.get('conversation_type', '')} · "
                  f"exported {export['exported_at_utc']}")
    if updated:
        provenance += f" · last updated {updated}"

    return "\n".join([
        f"# {label}",
        "",
        provenance,
        "",
        f"{export.get('top_level_count', 0)} top-level messages and "
        f"{export.get('reply_count', 0)} thread replies "
        f"({export.get('total_message_count', 0)} total).",
        "",
        HEADER_RULE,
        "",
    ])


def render(export: dict, users: dict, workspace_url: str,
           updated: str = None) -> str:
    """Render a raw export dict to a Markdown document, threads inline."""
    channel_id = export["conversation_id"]

    # conversations.history returns newest-first; a document should read forwards.
    messages = sorted(export["messages"], key=lambda m: float(m["ts"]))
    lines = [header(export, users, updated)]

    for message in messages:
        # A thread_broadcast copy already appears inside its own thread. Rendering it
        # here too would duplicate it; the export flagged it so this can skip it.
        if message.get("_also_in_thread"):
            continue

        lines += _line(message, users, workspace_url, channel_id)

        replies = sorted(message.get("thread_replies", []),
                         key=lambda m: float(m["ts"]))
        for reply in replies:
            lines += _line(reply, users, workspace_url, channel_id,
                           indent="> ", parent_ts=message["ts"])
        if replies:
            lines.append("")

    return "\n".join(lines)


def render_append(parts: dict, users: dict, workspace_url: str,
                  channel_id: str, when: str) -> str:
    """The section added to the bottom of an existing document.

    New messages read in place, chronologically. Replies that arrived on an older
    message cannot: they belong far up the document, beneath a parent already
    written. Rewriting that parent's section would mean re-emitting text Slack may
    have edited since, so they go here instead, each under a link back to the
    thread it answers.
    """
    lines = ["", HEADER_RULE, "", f"## New as of {when}", ""]

    for message in parts["fresh"]:
        if message.get("_also_in_thread"):
            continue
        lines += _line(message, users, workspace_url, channel_id)
        replies = sorted(message.get("thread_replies", []),
                         key=lambda m: float(m["ts"]))
        for reply in replies:
            lines += _line(reply, users, workspace_url, channel_id,
                           indent="> ", parent_ts=message["ts"])
        if replies:
            lines.append("")

    if parts["catch_up"]:
        lines += ["### New replies to earlier threads", ""]
        for entry in parts["catch_up"]:
            parent = entry["parent"]
            who = users.get(parent.get("user", ""), parent.get("user", "unknown"))
            excerpt = gist(parent.get("text", ""), users)
            link = permalink(workspace_url, channel_id, parent["ts"])
            lines += [f"*In reply to [{stamp(parent['ts'])} — {who}]({link})*"
                      + (f" — “{excerpt}…”" if excerpt else ""), ""]
            for reply in entry["replies"]:
                lines += _line(reply, users, workspace_url, channel_id,
                               indent="> ", parent_ts=parent["ts"])
            lines.append("")

    return "\n".join(lines)


def _demo() -> None:
    """Print the output format using synthetic messages - no real data involved."""
    users = {"U01": "user-a", "U02": "user-b", "U03": "user-c"}
    export = {
        "conversation_id": "C0EXAMPLE99",
        "conversation_label": "example-channel",
        "conversation_type": "private channel",
        "exported_at_utc": "20260826T000000Z",
        "top_level_count": 2, "reply_count": 2, "total_message_count": 4,
        "messages": [
            {"ts": "1787000000.000100", "user": "U01",
             "text": "Has anyone checked the <http://example.com/trail|trail map> "
                     "since the bridge reopened?",
             "reply_count": 2,
             "thread_replies": [
                 {"ts": "1787000060.000200", "user": "U02",
                  "text": "Yes - the loop is open again. "
                          "<@U03> found two picnic spots."},
                 {"ts": "1787000900.000300", "user": "U03",
                  "text": "The overlook and the creek. "
                          "Both have tables, only one has shade."},
             ]},
            {"ts": "1787003600.000400", "user": "U02",
             "text": "Reminder for @here: carpool leaves Saturday at 8."},
        ],
    }
    print(render(export, users, "https://example.slack.com/"))


if __name__ == "__main__":
    _demo()
