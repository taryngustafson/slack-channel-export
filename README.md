# slack-channel-export

Export a Slack channel or DM, including all thread replies, to readable text and Markdown.

`slack-channel-export` is a small macOS command-line tool for conversations that are hard to capture cleanly from Slack itself. Give it a channel or DM, and it:

* downloads the conversation in chronological order
* expands thread replies
* can copy the Markdown version to your clipboard, if you ask
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

Add `--copy` if you also want the Markdown version copied to your clipboard. Without it, your clipboard is left alone.

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
slack-export C0123456789 --copy
```

Also copy the Markdown version to your clipboard. Without `--copy`, your clipboard is not touched.

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
slack-export C0123456789 Project Planning --copy
```

### Find a conversation again

`slack-export` keeps a small saved list so you do not have to remember conversation IDs or go back to Slack to find them again.

```sh
slack-export list
```

```text
ID           NICKNAME  SLACK NAME
D0123456789  Alex      (DM)
C0123456789  -         project-planning
```

You can search the list by name or ID:

```sh
slack-export list planning
```

You can give any conversation your own nickname:

```sh
slack-export save D0123456789 Alex
```

You can also use `save` to remember a conversation without exporting it first.

For more about the saved list, re-running existing exports, and other options, see **[USAGE.md](USAGE.md)**.

## What gets saved

Each export has three parts:

| File        | Location       | Purpose                                                      |
| ----------- | -------------- | ------------------------------------------------------------ |
| `.txt`      | `exports/`     | Plain-text conversation with thread replies indented         |
| `.md`       | `exports/`     | Markdown version with Slack permalinks                       |
| `.raw.json` | `exports/raw/` | Raw Slack data used for incremental updates and re-rendering |

The Markdown version is what `--copy` copies to your clipboard.

The saved list is kept separately at `~/.config/slack-export/channels.json`. It stores the information needed to find conversations again, such as conversation and workspace IDs/names, nicknames, conversation type, and export names/times. It does **not** contain Slack message text or attachments.

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

With `--copy`, only the newly fetched messages are copied.

The existing exported text is not rewritten. If an older Slack message has since been edited, your previous export keeps the version that was captured at the time.

More details are in **[USAGE.md](USAGE.md)**.

## Privacy and safety

Slack exports can contain private conversations, so the project includes several safeguards:

* the Slack app requests only the permissions the exporter needs
* the exporter refuses to run if Slack reports any unexpected permission
* the token is stored in macOS Keychain rather than in the project files
* generated export files and the saved list are created with owner-only permissions
* `exports/` is ignored by Git, and the saved list is kept outside the project folder
* if generated files or the saved list are inside a Git repository, the exporter refuses to write them if Git could track them
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
* **[USAGE.md](USAGE.md)** — detailed day-to-day usage, options, incremental updates, the saved list, and troubleshooting

## License

MIT License. See `LICENSE`.
