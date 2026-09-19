# Slack setup

`slack-channel-export` needs permission to read the Slack conversations that you ask it to export.

Slack provides that access through a **user token**. To get one, you create a small Slack app for yourself, install it in the workspace you want to use, and save the resulting token in your Mac's Keychain.

This is a one-time setup.

Installing the `slack-export` command itself is covered in `README.md`.

## Before you start

### macOS

This version of the tool is written for macOS.

It uses:

* macOS Keychain to store the Slack token
* `pbcopy` to put exported Markdown on your clipboard

### Workspace approval

Whether you can install the Slack app depends on the workspace.

If it is:

* **your own workspace**, you should normally be able to install it directly
* **a company or school workspace**, the workspace may require an owner or administrator to approve apps first

If approval is required, Slack will show a request-for-approval flow instead of installing the app immediately.

### Make sure exporting is allowed

The fact that Slack lets your account read a conversation does not necessarily mean your organization allows copies of that conversation to be stored elsewhere.

Exports are local files on your Mac.

If your workplace or school has policies about where Slack or company data can be stored, those policies still apply.

## What you are granting

The app uses a **Slack user token**, not a bot token.

That means it acts using your Slack account's existing access.

With the permissions requested by this project, the token can read:

* public channels
* private channels you are a member of
* your DMs
* your group DMs
* thread replies
* Slack users' display names

It cannot:

* post messages
* reply
* react
* upload files
* invite people
* modify channels
* download attachments

The tool also does not continuously monitor Slack. It makes API requests only while you are actively running `slack-export`.

The complete permission list appears later in this guide.

## 1. Create the Slack app

The repository includes:

```text
slack-export-app-manifest.yaml
```

The manifest describes the app and its requested permissions so you do not have to configure those permissions manually.

1. Go to **api.slack.com/apps** and sign in.
2. Choose **Create New App**.
3. Choose to create the app **from a manifest**.
4. Select the Slack workspace you want to use.
5. Open `slack-export-app-manifest.yaml` from this repository.
6. Copy the entire file.
7. Paste it into Slack's manifest editor, replacing anything already there.
8. Review the configuration.
9. Create the app.

### If Slack says the installation was not completed

Slack's review panel sometimes shows this after you click **Create and Install**:

```text
Installation was not completed. Click Create and Install to try again.
```

**Do not click Create and Install again.** Despite the message, the app has normally been created. Clicking again tends to produce Slack's **"You're creating apps too quickly"** warning, and can leave you with a duplicate app.

Instead:

1. Close the panel with the **X** in its top corner.
2. Look at your apps list at **api.slack.com/apps**.
3. The app should be listed there. If it is not, refresh the page and look again.
4. Open it and continue with step 2 below.

This is a quirk of Slack's website rather than a problem with these instructions or with the manifest.

If Slack does display **"You're creating apps too quickly,"** check your existing apps before trying again. The first attempt may already have succeeded.

## 2. Install the app in the workspace

Inside the app's Slack settings:

1. Open **OAuth & Permissions**.
2. Choose **Install to Workspace**.

On some Enterprise Grid workspaces, the wording may instead refer to installing to the organization.

Slack will display the permissions the app requests.

Review them before approving the installation.

**Slack may describe the DM permissions with wording such as "Read and send DMs."** That is a broad category label in Slack's permission screen; this app does not request any permission that allows it to send DMs. You can verify the exact scopes in `slack-export-app-manifest.yaml`, and the setup check will refuse the token if Slack reports any unexpected scope.

### If Slack asks for approval

Some workspaces require app installations to be approved by an owner or administrator.

If that happens, submit the request through Slack.

The **What the app can access** section below provides a plain-English explanation of the requested permissions that you can use when reviewing or requesting approval.

## 3. Copy the User OAuth Token

After the app is installed, stay on **OAuth & Permissions**.

Find:

**User OAuth Token**

It should begin with:

```text
xoxp-
```

Copy that token.

You do **not** want:

* a bot token
* an App Configuration Token

This tool uses the User OAuth Token only.

Treat the token like a password. Although the app has no write permissions, the token can read a substantial amount of Slack content that your account can already access.

## 4. Store the token in macOS Keychain

Open Terminal and run:

```sh
security add-generic-password -a "$USER" -s "SLACK_USER_TOKEN" -U -w
```

Terminal will ask you to enter the token **twice** — once to enter it, and once to confirm it:

```text
password data for new item:
retype password for new item:
```

Paste the token at both prompts.

Your typing will not appear on the screen. That is normal.

The token is stored in macOS Keychain rather than in a file inside the repository.

## 5. Verify the setup

From the `slack-channel-export` folder, run:

```sh
sh tools/check_token.sh
```

The check:

* verifies that the token works
* shows the Slack identity associated with it
* shows the permissions Slack reports for that token
* refuses the token if any unexpected permission is present

It does not attempt to post a message or perform another write action.

A successful result ends with:

```text
PASS: every scope is on the allowlist, and none of them can write.
```

If the token is invalid, copy the User OAuth Token again and repeat the Keychain step.

## 6. Leave app distribution disabled

You do not need to distribute this Slack app.

Leave **Manage Distribution** alone.

This project is designed for an app you create for your own use in a workspace. Enabling external distribution can also change how Slack applies API rate limits, so it is unnecessary for this tool.

## What the app can access

### User token, not bot token

The app uses a user token beginning with:

```text
xoxp-
```

The token operates with your Slack identity.

It does not give the exporter access to conversations that your Slack account itself is not permitted to read.

### Conversations

With the permissions requested by the manifest, the token can read:

* public channels in the workspace
* private channels you belong to
* your DMs
* your group DMs
* thread replies in those conversations

The exporter itself reads only the conversation you explicitly provide when you run the command.

There is no "export the entire workspace" command.

### User information

The exporter uses `users:read` to translate Slack user IDs into readable names.

The manifest does not request permission to read users' email addresses.

### Attachments

The app does not request `files:read`.

Attachments are therefore represented as links rather than downloaded files.

## Requested permissions

| Slack scope        | Why it is needed                                |
| ------------------ | ----------------------------------------------- |
| `channels:history` | Read messages in public channels                |
| `channels:read`    | Look up public-channel information              |
| `groups:history`   | Read messages in private channels you belong to |
| `groups:read`      | Look up private-channel information             |
| `im:history`       | Read direct messages                            |
| `im:read`          | Look up direct-message information              |
| `mpim:history`     | Read group direct messages                      |
| `mpim:read`        | Look up group-DM information                    |
| `users:read`       | Convert Slack user IDs into readable names      |

Slack may also report the legacy `identify` scope depending on the history of the app or token.

The tool accepts that scope, but the manifest does not request it.

The authoritative requested permission list is also visible directly in:

```text
slack-export-app-manifest.yaml
```

## Safety checks

The project includes several safeguards around Slack access.

### Unexpected permissions are refused

Every export asks Slack which scopes are associated with the token.

The exporter compares those scopes against its explicit allowlist.

If anything unexpected appears, the exporter stops before reading the conversation.

That includes write permissions as well as unrelated read permissions the tool was not designed to use.

### The token remains in Keychain

The token is not stored:

* in the repository
* in the exporter configuration
* in the exported conversation files

It is read from macOS Keychain when the tool runs.

### The exporter reads one conversation at a time

Although the user token may be capable of reading many conversations available to your Slack account, the program fetches only the conversation you explicitly provide.

It does not enumerate the workspace and does not run automatically.

## Test your setup

At any time, you can re-run:

```sh
cd ~/slack-channel-export
sh tools/check_token.sh
```

Use this if:

* an export unexpectedly stops working
* you reinstall the Slack app
* you change the app's permissions
* you replace the saved token
* you want to confirm which workspace/account the token belongs to

## Replace the saved token

To overwrite the existing Keychain entry:

```sh
security add-generic-password -a "$USER" -s "SLACK_USER_TOKEN" -U -w
```

Then paste the new `xoxp-` token when prompted.

## Remove the saved token

To delete the token from your Mac:

```sh
security delete-generic-password -a "$USER" -s "SLACK_USER_TOKEN"
```

## Revoke Slack access

Removing the token from your Keychain prevents this local tool from using it, but the Slack authorization itself still exists.

To revoke that authorization, use your workspace's Slack app-management page and either:

* revoke your authorization for `slack-channel-export`, or
* remove the app from the workspace

Deleting the app through Slack's developer app management also invalidates its access.

## Troubleshooting

### `no Keychain entry 'SLACK_USER_TOKEN'`

The token has not been stored, or the Keychain entry was removed.

Repeat the **Store the token in macOS Keychain** step.

### `invalid_auth`

Slack rejected the saved token.

Copy the User OAuth Token from **OAuth & Permissions** again and replace the Keychain entry.

### `token carries unexpected scope(s)`

Slack reports a permission that the tool does not allow.

Do not ignore the warning.

Check the app's OAuth permissions and compare them with:

```text
slack-export-app-manifest.yaml
```

You can also run:

```sh
sh tools/check_token.sh
```

to see the reported scope list.

### `channel_not_found`

Possible causes include:

* an incorrect conversation ID
* a private channel you do not belong to
* a conversation in a different workspace than the saved token

The exporter prints the workspace associated with the token during its pre-flight checks.

### `not_in_channel`

The token cannot access the requested conversation.

If it is a public channel, verify that the stored token is the expected `xoxp-` user token.

### Exports are unexpectedly slow

Check that you did not enable Slack app distribution.

Slack applies different API rate limits to different categories of apps, and this tool is intended to remain a private app created for your own workspace access.

## Next

Once the setup check passes, you are ready to use the tool.

Start by picking a real conversation to export. In Slack, open a channel, click its name, open **About**, and copy the **Channel ID** at the bottom. Copying the channel's link works too.

Then run the command with that ID:

```sh
slack-export YOUR-CHANNEL-ID
```

`C0123456789` appears throughout these documents as an example only. Running it as written reports `channel_not_found`, because it is not a real conversation in your workspace.

See **`USAGE.md`** for names, options, and updating an export later.
