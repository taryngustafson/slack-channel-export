"""Work out what a conversation has gained since it was last exported.

Everything here is pure: dicts in, dicts out, no Slack client and no file writing,
so the whole delta calculation can be tested against archives already on disk
without touching the network. export_conversation.py does the fetching and the
writing; this module only decides what is new.

Three things can have changed since an earlier export:

  1. new top-level messages           - ts newer than the cutoff
  2. new replies on a known thread    - latest_reply moved past the cutoff
  3. a FIRST reply on an old message  - it had no thread at all last time

The third is the one that is easy to miss. Polling every thread that was already
known would never find it, because the message was not a thread when it was
archived. Comparing Slack's own reply_count and latest_reply against the archive
catches all three, and costs one cheap pass over the top-level history instead of
one API call per thread.
"""

# A Slack ts is a decimal string, "1787000000.123456" - epoch seconds plus a
# per-channel counter. Comparing them as STRINGS almost works and then silently
# does not: "1787000000.123456" < "999.0" is True, because string comparison is
# character by character. Always compare as float.
def ts_value(ts) -> float:
    """One Slack timestamp as a number. Missing or unparseable sorts oldest."""
    try:
        return float(ts)
    except (TypeError, ValueError):
        return 0.0


def newest_ts(messages: list) -> str:
    """The newest ts anywhere in an export - top-level messages and replies alike.

    Returned as the original string rather than a float, because it is handed
    straight back to Slack as the `oldest` parameter and float() -> str() would
    reformat it (1787000000.123456 survives, but a trailing zero would not).
    """
    newest, best = "0", 0.0
    for message in messages:
        for item in [message] + message.get("thread_replies", []):
            value = ts_value(item.get("ts"))
            if value > best:
                newest, best = item["ts"], value
    return newest


def index_by_ts(messages: list) -> dict:
    """Map ts -> message, for asking 'do we already have this one?'."""
    return {m["ts"]: m for m in messages if m.get("ts")}


def classify(archived: list, fresh: list, cutoff: str) -> dict:
    """Compare a fresh top-level history against what the archive already holds.

    `fresh` is conversations.history as it looks right now, WITHOUT replies
    expanded - reply_count and latest_reply are enough to tell which threads moved.
    Returns the new parents, the threads worth re-fetching, and the ts of anything
    already known so the caller can dedupe.
    """
    known = index_by_ts(archived)
    cutoff_value = ts_value(cutoff)

    new_top, moved_threads = [], []
    for message in fresh:
        ts = message.get("ts")
        if not ts:
            continue

        if ts not in known:
            new_top.append(message)
            # A brand new message can already have replies by the time we look.
            if message.get("reply_count"):
                moved_threads.append(ts)
            continue

        # Known message. It only needs a replies call if Slack says the thread
        # has grown - either the count changed, or the newest reply is past the
        # cutoff. Checking both means a reply added AND one deleted, which leaves
        # reply_count unchanged, is still caught by latest_reply.
        before = known[ts]
        grew = message.get("reply_count", 0) != before.get("reply_count", 0)
        recent = ts_value(message.get("latest_reply")) > cutoff_value
        if grew or recent:
            moved_threads.append(ts)

    return {
        "new_top": new_top,
        "moved_threads": moved_threads,
        "known_ts": set(known),
    }


def merge(archived: list, new_top: list, replies_by_parent: dict) -> dict:
    """Fold new messages and replies into the archived list, in place.

    The archive is the record of what was actually said, so an existing message is
    NEVER overwritten with a freshly fetched copy: if someone edited a message last
    week, Slack now returns the edited text, and replacing the stored copy would
    quietly destroy the only record of the original. Only thread metadata that is
    purely a counter - reply_count and friends - is refreshed, so the next run can
    tell whether the thread moved again.

    Returns counts for the summary.
    """
    known = index_by_ts(archived)
    added_replies, updated_threads = [], []

    for message in new_top:
        if message["ts"] not in known:
            archived.append(message)
            known[message["ts"]] = message

    for parent_ts, replies in replies_by_parent.items():
        parent = known.get(parent_ts)
        if parent is None:
            continue

        seen = {r["ts"] for r in parent.get("thread_replies", [])}
        fresh_replies = [r for r in replies if r.get("ts") not in seen]
        if fresh_replies:
            parent.setdefault("thread_replies", []).extend(fresh_replies)
            parent["thread_replies"].sort(key=lambda m: ts_value(m.get("ts")))
            added_replies += fresh_replies
            updated_threads.append(parent_ts)

        # Counters only - never text. Set from what is actually held rather than
        # from Slack's tally, so the archive stays self-consistent even when the
        # two disagree (which they do, whenever a reply was deleted).
        held = parent.get("thread_replies", [])
        if held:
            parent["reply_count"] = len(held)
            parent["latest_reply"] = held[-1]["ts"]

    archived.sort(key=lambda m: ts_value(m.get("ts")))
    return {
        "new_top": len(new_top),
        "new_replies": len(added_replies),
        "updated_threads": len(set(updated_threads)),
    }


def parts_since(messages: list, since: str) -> dict:
    """Everything in a merged archive newer than `since`, in the two render shapes.

    Driven off the archive rather than off what this run happened to fetch, which
    is what lets a copy of the transcript that is several runs behind catch up in
    one go: the messages it is missing are already held, they simply were never
    written into that particular file.

    New top-level messages read in chronological order with their own replies
    beneath them, as in a normal export. A reply on an OLDER parent cannot go
    there - chronologically it belongs hundreds of lines up the file, beneath a
    parent already written - so it is grouped into a catch-up section instead.
    """
    floor = ts_value(since)
    fresh, catch_up = [], []

    for message in sorted(messages, key=lambda m: ts_value(m.get("ts"))):
        if ts_value(message.get("ts")) > floor:
            fresh.append(message)
            continue

        # Older parent: only the replies that arrived since are outstanding.
        late = [r for r in message.get("thread_replies", [])
                if ts_value(r.get("ts")) > floor]
        if late:
            catch_up.append({"parent": message,
                             "replies": sorted(late,
                                               key=lambda m: ts_value(m.get("ts")))})

    return {"fresh": fresh, "catch_up": catch_up}


def is_empty(parts: dict) -> bool:
    """True when there is genuinely nothing new to append."""
    return not parts["fresh"] and not parts["catch_up"]


def _self_test() -> None:
    """Prove the comparison logic against hand-built cases. No network, no files."""
    # String comparison of Slack timestamps is the trap this module exists around.
    assert "1787000000.123456" < "999.0", "string compare really is this broken"
    assert ts_value("1787000000.123456") > ts_value("999.0")
    assert ts_value(None) == 0.0 and ts_value("nonsense") == 0.0

    archived = [
        {"ts": "100.0", "text": "old, no thread"},
        {"ts": "200.0", "text": "old, 1 reply", "reply_count": 1,
         "latest_reply": "250.0", "thread_replies": [{"ts": "250.0", "text": "r1"}]},
    ]
    fresh = [
        {"ts": "100.0", "text": "old, no thread", "reply_count": 1,
         "latest_reply": "600.0"},                       # gained its FIRST reply
        {"ts": "200.0", "text": "old, 1 reply", "reply_count": 2,
         "latest_reply": "700.0"},                       # thread grew
        {"ts": "800.0", "text": "brand new"},            # new top-level
    ]
    found = classify(archived, fresh, cutoff="500.0")
    assert [m["ts"] for m in found["new_top"]] == ["800.0"], found["new_top"]
    assert sorted(found["moved_threads"]) == ["100.0", "200.0"], found["moved_threads"]

    # An untouched conversation must produce no work at all.
    quiet = classify(archived, [dict(m) for m in archived], cutoff="500.0")
    assert quiet["new_top"] == [] and quiet["moved_threads"] == [], quiet

    counts = merge(archived, found["new_top"], {
        "100.0": [{"ts": "600.0", "text": "first ever reply"}],
        "200.0": [{"ts": "250.0", "text": "r1"},          # already held - dedupe
                  {"ts": "700.0", "text": "r2"}],
    })
    assert counts["new_top"] == 1, counts
    assert counts["new_replies"] == 2, counts
    assert counts["updated_threads"] == 2, counts
    assert [m["ts"] for m in archived] == ["100.0", "200.0", "800.0"]
    assert len(archived[1]["thread_replies"]) == 2, "r1 must not be duplicated"
    assert archived[1]["reply_count"] == 2 and archived[1]["latest_reply"] == "700.0"

    # Re-running the same merge must add nothing the second time.
    again = merge(archived, [], {"200.0": [{"ts": "700.0", "text": "r2"}]})
    assert again["new_replies"] == 0, again

    parts = parts_since(archived, since="500.0")
    assert [m["ts"] for m in parts["fresh"]] == ["800.0"], parts["fresh"]
    assert [e["parent"]["ts"] for e in parts["catch_up"]] == ["100.0", "200.0"]
    assert [r["ts"] for r in parts["catch_up"][1]["replies"]] == ["700.0"], \
        "the reply already held at 250.0 must not be re-appended"
    assert not is_empty(parts)

    # A file that is several runs behind catches up on everything at once.
    behind = parts_since(archived, since="150.0")
    assert [m["ts"] for m in behind["fresh"]] == ["200.0", "800.0"], behind["fresh"]
    assert [r["ts"] for r in behind["catch_up"][0]["replies"]] == ["600.0"]

    # A file already current gets nothing.
    assert is_empty(parts_since(archived, since=newest_ts(archived)))

    assert newest_ts(archived) == "800.0", newest_ts(archived)
    assert newest_ts([]) == "0"


if __name__ == "__main__":
    _self_test()
    print("incremental: all self-tests pass")
