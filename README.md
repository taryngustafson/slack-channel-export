# slack-channel-export

Export a Slack channel or DM, including all thread replies, to readable text and Markdown.

`slack-channel-export` is a small macOS command-line tool for conversations that are hard to capture cleanly from Slack itself. Give it a channel or DM, and it:

* downloads the conversation in chronological order
* expands thread replies
* copies the Markdown version to your clipboard
* saves readable `.txt` and `.md` files
* keeps a raw archive so later runs only fetch what is new

It is **read-only**. It cannot post, reply, react, upload files, or send DMs. Nothing runs in the background: you run the command, it exports the conversation, and it exits.

## Quick start

Already installed and connected to Slack?

```sh
slack-export C0123456789
```

Or give the export a readable name:

```sh
slack-export C0123456789 Project Planning
```

When it finishes, the Markdown version is already on your clipboard.

Your saved exports are in:

```sh
open ~/slack-channel-export/exports
```

You can also paste a Slack channel or message link instead of typing the conversation ID.

## First-time setup

There are two parts:

1. **Install `slack-channel-export` on your Mac**
2. **Create a read-only Slack app and save its token**

### Install the tool

Put the project somewhere permanent. The examples in these docs assume:

```text
~/slack-channel-export
```

Then:

```sh
cd ~/slack-channel-export
sh install.sh
```

The installer creates the tool's Python environment and makes the `slack-export` command available from any folder.

If it tells you to open a new terminal tab, do that before continuing.

To uninstall later:

```sh
sh install.sh --uninstall
```

### Connect it to Slack

Slack requires a one-time app setup so the exporter can read conversations using your own Slack account.

See **[SLACK-SETUP.md](SLACK-SETUP.md)** for the complete walkthrough, including:

* creating the Slack app
* requested permissions
* workspace approval
* getting the `xoxp-` user token
* storing the token in macOS Keychain
* verifying the token before exporting anything

Once that is done, see **[USAGE.md](USAGE.md)** for the full usage guide.

## Basic usage

### Export a channel or DM

```sh
slack-export C0123456789
```

Channel IDs normally start with `C`. DM IDs start with `D`.

You can also paste:

* a Slack channel link
* a link to a message in that conversation
* a copied Slack channel mention containing the conversation ID

The exporter extracts the conversation ID automatically.

### Name the export

```sh
slack-export C0123456789 Project Planning
```

This creates files such as:

```text
Project-Planning.txt
Project-Planning.md
Project-Planning.raw.json
```

If you do not provide a name, the conversation ID is used instead.

### Common options

```sh
slack-export C0123456789 --no-clipboard
```

Write the files without replacing your clipboard.

```sh
slack-export C0123456789 --no-threads
```

Export only top-level messages.

```sh
slack-export C0123456789 --out ~/Desktop
```

Put the readable `.txt` and `.md` files in another folder.

Options can be combined with a name:

```sh
slack-export C0123456789 Project Planning --no-clipboard
```

### Find a conversation again

`slack-export` keeps a small conversation index so you do not have to remember conversation IDs or go back to Slack to find them again.

```sh
slack-export list
```

```text
ID           NICKNAME  SLACK NAME
D0123456789  Alex      (DM)
C0123456789  -         project-planning
```

You can search your conversation index by name or ID:

```sh
slack-export list planning
```

You can give any conversation your own nickname:

```sh
slack-export save D0123456789 Alex
```

You can also use `save` to remember a conversation without exporting it first.

You can collect conversations into groups of your own, such as one per project:

```sh
slack-export group Research C0123456789 D0123456789
```

A conversation can be in any number of groups, or none. If a conversation is not in your conversation index yet, `group` looks it up in Slack and adds it to the index first.

For more about the conversation index, groups, re-running existing exports, and other options, see **[USAGE.md](USAGE.md)**.

## What gets saved

Each export has three parts:

| File        | Location       | Purpose                                                      |
| ----------- | -------------- | ------------------------------------------------------------ |
| `.txt`      | `exports/`     | Plain-text conversation with thread replies indented         |
| `.md`       | `exports/`     | Markdown version with Slack permalinks                       |
| `.raw.json` | `exports/raw/` | Raw Slack data used for incremental updates and re-rendering |

The Markdown version is what gets copied to your clipboard.

The conversation index is kept separately at `~/.config/slack-export/channels.json`. It stores the information needed to find conversations again, such as conversation and workspace IDs/names, nicknames, conversation type, export names/times, and your groups. It does **not** contain Slack message text or attachments.

Example text output:

```text
2026-08-17 13:53  user-a
Has anyone checked the trail map (http://example.com/trail) since the bridge reopened?

    2026-08-17 13:54  user-b
    Yes - the loop is open again. @user-c found two picnic spots.

2026-08-17 14:53  user-b
Reminder for @here: carpool leaves Saturday at 8.
```

Example Markdown output:

```markdown
**2026-08-17 13:53 — user-a** · [↗](https://example.slack.com/archives/...)

Has anyone checked the [trail map](http://example.com/trail) since the bridge reopened?

> **2026-08-17 13:54 — user-b** · [↗](https://example.slack.com/archives/...)
>
> Yes - the loop is open again. @user-c found two picnic spots.
```

The `↗` links back to that exact message in Slack.

All example conversations, names, IDs, and URLs in this repository are synthetic and were created for demonstration and testing.

## Re-running an export

Run the same command again later:

```sh
slack-export C0123456789 Project Planning
```

The exporter checks the existing raw archive, downloads what has appeared since the previous run, and appends only the new material.

Your clipboard contains only the newly fetched messages.

The existing exported text is not rewritten. If an older Slack message has since been edited, your previous export keeps the version that was captured at the time.

More details are in **[USAGE.md](USAGE.md)**.

## Privacy and safety

Slack exports can contain private conversations, so the project includes several safeguards:

* the Slack app requests only the permissions the exporter needs
* the exporter refuses to run if Slack reports any unexpected permission
* the token is stored in macOS Keychain rather than in the project files
* generated export files and the conversation index are created with owner-only permissions
* `exports/` is ignored by Git, and the conversation index is kept outside the project folder
* if generated files or the conversation index are inside a Git repository, the exporter refuses to write them if Git could track them
* the tool reads only the conversation you explicitly provide
* attachments are exported as links rather than downloaded

The full permission model is explained in **[SLACK-SETUP.md](SLACK-SETUP.md)** and is also visible directly in `slack-export-app-manifest.yaml`.

## Platform

This version is written for **macOS**.

It uses:

* macOS Keychain via `security`
* `pbcopy` for clipboard output
* Python and `slack_sdk` for Slack API access

## Documentation

* **[SLACK-SETUP.md](SLACK-SETUP.md)** — one-time Slack app and token setup
* **[USAGE.md](USAGE.md)** — detailed day-to-day usage, options, incremental updates, the conversation index, groups, and troubleshooting

## License

MIT License. See `LICENSE`.
