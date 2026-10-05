# Using slack-channel-export

This guide covers everyday use of `slack-export`.

If you have not installed the tool yet, start with the installation section in `README.md`.

If you still need to create the Slack app or save your Slack token, follow `SLACK-SETUP.md` first.

## Export a conversation

There are three basic steps.

### 1. Get the conversation

You can give `slack-export` either a Slack conversation ID or a Slack link.

#### Channel ID

In Slack:

1. Open the channel.
2. Click the channel name at the top.
3. Open **About**.
4. Scroll to the bottom.
5. Copy the **Channel ID**.

It will look something like:

```text
C0123456789
```

#### DM ID

For a DM, open it in Slack in your browser and look at the URL:

```text
https://example.slack.com/archives/D0123456789
                                    ^^^^^^^^^^^
```

The part beginning with `D` is the DM conversation ID.

#### Or just copy a Slack link

You usually do not need to find the ID manually.

You can paste:

* a channel link
* a link to any message in that channel or DM
* a copied Slack channel mention containing its ID

`slack-export` extracts the conversation ID automatically.

### 2. Run the exporter

Open Terminal and run:

```sh
slack-export C0123456789
```

Or paste a Slack link:

```sh
slack-export https://example.slack.com/archives/C0123456789
```

If the link contains `?` (message links usually do), wrap it in quotes:

```sh
slack-export "https://example.slack.com/archives/C0123456789/p1787000000000100?thread_ts=1787000000.000100&cid=C0123456789"
```

Press Return.

The exporter prints its progress while it works. Large conversations with many threads can take longer because thread replies have to be fetched separately.

### 3. Paste

When the export finishes, the Markdown version of the conversation is already on your clipboard.

Just paste it wherever you need it.

The files are also saved locally so you can use them again later.

## Give the export a name

Add a name after the conversation ID:

```sh
slack-export C0123456789 Project Planning
```

This creates files such as:

```text
Project-Planning.txt
Project-Planning.md
Project-Planning.raw.json
```

Spaces and punctuation are converted to hyphens so the name works safely as a filename.

You can also use quotes if you prefer:

```sh
slack-export C0123456789 "Project Planning"
```

If you do not provide a name, the conversation ID becomes the filename.

## Where the files go

By default, the readable files are saved in:

```text
~/slack-channel-export/exports/
```

Open that folder with:

```sh
open ~/slack-channel-export/exports
```

The raw archive is kept separately in:

```text
~/slack-channel-export/exports/raw/
```

Each export has three files.

### `.txt`

Example:

```text
Project-Planning.txt
```

This is the simplest version to read.

Messages appear oldest first, with:

* timestamp
* speaker name
* message text
* blank lines between messages
* thread replies indented underneath their parent

It does not include reactions or Slack permalinks.

### `.md`

Example:

```text
Project-Planning.md
```

This contains the same conversation as Markdown.

Each message includes a permalink back to the original Slack message.

This is the version copied to your clipboard.

### `.raw.json`

Example:

```text
exports/raw/Project-Planning.raw.json
```

This is Slack's raw response data.

You normally do not need to open it.

The exporter keeps it so that:

* later runs know what has already been downloaded
* new messages can be fetched incrementally
* the conversation can be rendered into another format later without downloading everything from Slack again

## Update an existing export

To bring an existing export up to date, **run the same command again**.

For example:

```sh
slack-export C0123456789 Project Planning
```

If that command created the export previously, running it again updates those same files.

The exporter checks the raw archive, downloads the new material, and appends it to the existing export.

Your clipboard contains **only the newly fetched messages**.

A new section looks like:

```text
======================================================================
new as of 2026-08-30 13:20
======================================================================

2026-08-30 12:58  user-a
Picked up the parking passes this morning - see everyone at the trailhead.
```

### Use the same name and output folder

The exporter identifies an existing output by its name and location.

For example:

```sh
slack-export C0123456789 Project Planning --out ~/Desktop
```

Run that same command again later to update the same export:

```sh
slack-export C0123456789 Project Planning --out ~/Desktop
```

Changing the name or `--out` location creates a separate export instead.

### Old thread replies

Sometimes a new reply is added to a thread whose original message is far up the existing transcript.

The exporter does not rewrite hundreds of lines just to insert that reply into its original chronological position.

Instead, new replies to old threads are appended in a separate **new replies to earlier threads** section.

### Existing messages are not rewritten

Previously exported messages stay as they were when you captured them.

If someone later edits an older Slack message, your previous export is not silently changed.

The export is therefore a record of what was captured at that time rather than a live mirror of Slack.

### Do not reuse a name for another conversation

One export name maps to one Slack conversation.

If you already have:

```text
Team-Notes.md
```

for one channel and try to use `Team Notes` for another channel, the exporter refuses rather than combining the two conversations.

## Find a conversation again

Conversation IDs are not very memorable, so `slack-export` keeps a conversation index: a list of conversations you have exported, saved, or added to a group.

After an export, the last lines show whether the conversation index was updated:

```text
  conversation index: C0123456789  project-planning
```

If the conversation index cannot be updated, the export itself is still complete. The command tells you what went wrong separately.

### See what is saved

```sh
slack-export list
```

This shows each conversation in your conversation index in three columns: its ID, your nickname for it, and its Slack channel name.

```text
ID           NICKNAME               SLACK NAME
D0123456789  Alex                   (DM)
C0234567890  Bench Work             lab-notes
C0123456789  -                      project-planning
D0987654321  [Sam-Lee]              (DM)
C0345678901  [Trip-Planning-Group]  (DM)
```

The output is sorted by Slack channel name. A conversation without one, such as a DM, is sorted by its nickname instead.

`list` is completely local. It reads the saved file only and does not contact Slack or read your Slack token, so it also works offline.

### What the columns show

The nickname column shows:

| Shown as | Meaning |
| --- | --- |
| `Alex` | a nickname you gave the conversation |
| `[Sam-Lee]` | no nickname, so the name of its most recent export, in brackets |
| `-` | no nickname, and never exported with a name |

The Slack channel name column shows the name Slack gives the conversation. DMs do not have Slack channel names, so a DM shows `(DM)`. A group DM technically has an internal Slack name beginning with `mpdm-`, but that is not very useful to a person, so it also shows as `(DM)` unless someone has given the group DM a name. A normal Slack channel whose actual name starts with `mpdm-` is still shown normally.

### Search the conversation index

Add a search after `list`:

```sh
slack-export list planning
```

You can search by conversation ID or by the names `list` shows.

```text
ID           NICKNAME               SLACK NAME
C0123456789  -                      project-planning
C0345678901  [Trip-Planning-Group]  (DM)
```

For normal words, search ignores case and differences such as spaces or punctuation. For example, these both find the same name:

```text
project planning
Project-Planning
```

A search containing only symbols or emoji is searched literally instead, rather than being treated as an empty search.

You can also search with part of an ID:

```sh
slack-export list C0123
```

### Give a conversation a nickname

```sh
slack-export save D0123456789 Alex
```

A nickname is your own name for a conversation. It is especially useful for DMs, which do not have Slack channel names.

Quotes are optional, just like they are when naming an export:

```sh
slack-export save D0123456789 "Alex Kim"
```

Within the same Slack workspace, a nickname can belong to only one conversation in your conversation index.

Saving the same conversation again with a new nickname changes it. Saving it again without a nickname leaves the existing nickname alone.

### Save a conversation without exporting it

You can add a conversation to your conversation index without downloading its messages:

```sh
slack-export save C0123456789
```

Unlike `list`, `save` does contact Slack. It uses the same read-only access as the exporter to make sure the conversation exists and to get its current name and type.

Running `save` again later also refreshes the Slack channel name if it has changed.

### Where the conversation index lives

```text
~/.config/slack-export/channels.json
```

The file stores lookup information such as:

- conversation IDs and Slack names
- conversation type
- your nicknames
- workspace ID/name
- export names and times
- your groups, and which conversations are in each

It does **not** contain Slack message text or attachments.

The folder and file are created with owner-only permissions.

The conversation index normally lives outside the `slack-channel-export` project. If its location is inside another Git repository — for example, because you keep `~/.config` in a dotfiles repo — `slack-export` checks whether Git could track `channels.json` and refuses to write it unless that file is ignored.

## Group conversations

A group is your own named collection of conversations — for example, everything related to one project. Groups are optional: a conversation can be in any number of groups, or none.

### Make a group

```sh
slack-export group Research
```

```text
Created group Research (empty).
```

Making a group that already exists changes nothing.

### Add conversations to a group

Put one or more conversation IDs or Slack links after the group's name:

```sh
slack-export group Research C0123456789 D0123456789
```

```text
Added to Research:
  C0123456789  -     project-planning
  D0123456789  Alex  (DM)

Research now has 2 conversations.
```

If the group does not exist yet, it is created first, so you do not need to make it separately.

Adding never removes anything. Run the command again with more IDs to add more conversations to the same group:

```sh
slack-export group Research D0987654321 D0123456789 C0999999999
```

```text
Added to Research:
  D0987654321  -     (DM)

Already in Research:
  D0123456789  Alex  (DM)

Could not add:
  C0999999999  conversation not found or not accessible

Research now has 3 conversations.
```

Each conversation is reported once, under what happened to it. Conversations that could be added stay added even when others could not.

### Conversations not yet in your conversation index

A conversation does not need to be saved or exported before you add it to a group.

- If it is **already in your conversation index**, `group` works entirely locally. It does not read your Slack token or contact Slack.
- If it is **not in your conversation index yet**, `group` looks it up in Slack, read-only, adds it to the conversation index, and then adds it to the group. It is added without a nickname; to give it one, use `slack-export save <ID> <nickname>`.
- If Slack **cannot find or access it**, it is added to neither the conversation index nor the group, and appears under `Could not add`. See [`channel_not_found`](#channel_not_found) for common causes. "Not accessible" can also mean the conversation exists but the saved token cannot see it.

### Group names

Group names are exact: `Research` and `research` are two different groups.

A name with spaces needs quotes:

```sh
slack-export group "Lab Notes" C0123456789
```

Without the quotes, `slack-export` would read `Lab` as the group's name and `Notes` as a conversation. Because `Notes` is not a conversation ID, the whole command is cancelled before anything changes:

```text
Nothing was changed: Notes is not a conversation ID.
A group name with spaces needs quotes: slack-export group "Lab Notes" C0123456789
```

A conversation ID in the name's place, such as `slack-export group C0123456789`, is refused, so a group is never accidentally named after a conversation.

Groups are not shown by `slack-export list`, and are stored in the conversation index file alongside everything else.

## Options

Most exports only need the basic command:

```sh
slack-export C0123456789
```

The following options are available when you need them.

### Choose another output folder

```sh
slack-export C0123456789 --out ~/Desktop
```

The `.txt` and `.md` files are written to that folder.

The `.raw.json` archive remains in:

```text
~/slack-channel-export/exports/raw/
```

If the output folder does not exist, the exporter creates it.

Before anything is downloaded, the exporter also checks whether the destination is inside a Git repository. If the generated files would be trackable by Git, it refuses to write them there.

This is intended to prevent private Slack conversations from being committed accidentally.

### Leave the clipboard alone

```sh
slack-export C0123456789 --no-clipboard
```

The files are still created, but your current clipboard contents are preserved.

You can combine this with a name:

```sh
slack-export C0123456789 Project Planning --no-clipboard
```

### Skip thread replies

```sh
slack-export C0123456789 --no-threads
```

This exports only top-level messages.

It is faster, but any conversation inside threads will be missing.

## Re-render an existing raw export

If you already have a `.raw.json` archive and want to create the text version again:

```sh
cd ~/slack-channel-export
./.venv/bin/python tools/render_text.py exports/raw/SOMETHING.raw.json
```

The text file is rendered from the saved Slack data, so the conversation does not have to be downloaded again. It still needs your stored token, to turn user IDs into names.

## If `slack-export` is not found

If Terminal says:

```text
command not found: slack-export
```

there are two common causes.

### The installer has not been run

From the project folder:

```sh
cd ~/slack-channel-export
sh install.sh
```

### The current terminal was already open during installation

Open a new terminal tab and try again.

You can also run the exporter directly by its full path:

```sh
~/slack-channel-export/slack-export C0123456789
```

Or, while you are inside the project folder:

```sh
./slack-export C0123456789
```

## Troubleshooting

### `is not a Slack conversation ID`

The value you provided does not contain a valid Slack conversation ID.

Try copying the channel ID or Slack link again.

### `no Keychain entry 'SLACK_USER_TOKEN'`

The Slack token is not currently stored in your Keychain.

Follow the token-storage step in `SLACK-SETUP.md`.

### `channel_not_found`

Common causes include:

* the conversation ID is wrong
* the private channel is not one you belong to
* the conversation belongs to a different Slack workspace than the saved token

The exporter prints the workspace associated with the token during its pre-flight checks.

### `not_in_channel`

The saved token cannot access that conversation.

A properly scoped Slack user token can read public channels whether or not you have joined them, so first check that the saved token is the expected `xoxp-` user token.

### `token carries unexpected scope(s)`

The Slack token has a permission that this exporter does not expect.

Do not continue until you understand why.

Run the setup check:

```sh
cd ~/slack-channel-export
sh tools/check_token.sh
```

See `SLACK-SETUP.md` for the permission model.

### `refusing to write inside a git repository`

The requested output would create files that Git could track.

Choose:

* a location outside the repository, or
* a location the repository already ignores

For example, the project's normal `exports/` directory is already configured appropriately.

### `conversation index: NOT updated - the export itself is fine`

The export worked, but it could not be added to the conversation index. The line below it gives the reason. Your exported files are complete.

Once the problem is fixed, running the same export again adds it.

### `channels.json is unreadable`

The conversation index is not valid JSON, or has a structure this version does not understand. `slack-export` stops rather than quietly starting a new, empty index, which would lose every nickname and name in it.

Fix the file, or move it aside to start the conversation index over:

```sh
mv ~/.config/slack-export/channels.json ~/.config/slack-export/channels.json.old
```

### `refusing to save channel names to ~/.config/slack-export/channels.json`

The conversation index is inside a Git repository that does not ignore it, so its names and IDs could be committed. Add `channels.json` to that repository's `.gitignore`.

### `conversation not found or not accessible`

`slack-export group` asked Slack about a conversation that is not in your conversation index, and Slack could not return it. It was not added anywhere. The causes are the same as for [`channel_not_found`](#channel_not_found).

### `Nothing was changed: ... is not a conversation ID`

Something after the group's name is not a conversation ID or Slack link. Most often, a group name with a space was typed without quotes. Nothing was changed; put the name in quotes and run the command again.

### `the nickname '...' is already used by`

Within a workspace, each nickname can belong to only one conversation. The message names the conversation that already has it. Choose another nickname, or give that conversation a different one first.

## Good to know

* The exporter is **read-only**.
* It does not run in the background.
* It reads only the conversation you explicitly provide.
* Attachments are exported as links rather than downloaded files.
* Exported conversations can contain private information. Treat the resulting files accordingly.

For Slack-app permissions and token setup, see **`SLACK-SETUP.md`**.
