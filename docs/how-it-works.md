# How Binday works

Binday runs on your own computer and reaches the Mattermost server over SSH. Nothing is installed on the server.

| It uses | For |
|---|---|
| **SSH** (password, key file or agent) | Running `mmctl` inside the Mattermost Docker container, measuring the data directory, and removing files over SFTP |
| **PostgreSQL** | Finding what to clean up. Reached directly, or through the SSH connection so the database stays private |
| **`mmctl`** | Mattermost's own commands for deleting channels and posts |

## What it cleans up

| Bin | What is removed | What stays |
|---|---|---|
| **Archived channels** | The channel, its messages and every file posted in it (`mmctl channel delete`) | Active channels, even empty ones |
| **Deactivated users' DMs** | Every message and file in direct messages with deactivated users (`mmctl post delete --permanent`) | The user accounts, DMs between active users, and the empty conversation itself, because Mattermost doesn't allow deleting DM channels |
| **Orphaned files** | Files whose message no longer exists, and their `FileInfo` rows | Avatars, plugins, and uploads not yet attached to a message (drafts) |

Binday finds items from the database, never by scanning the disk blindly, so system files are never in scope.

## The four steps

1. **Connect.** Checks SSH, the database, `mmctl`, whether channel deletion is enabled, and the data directory size. Each section also has its own **Test** button: Server (SSH login, data directory, Docker container, mmctl inside it), Database (connection and Mattermost's tables) and mmctl login (logs in with the URL and token, then makes a real call).
2. **Scan (dry run).** Lists every item and measures its files on disk. Read-only.
3. **Review.** Shows the space to reclaim split by bin, largest first. Untick anything you want to keep.
4. **Collect.** You confirm by typing `DELETE`, then items are removed in plan order. Results list every skipped or failed item with the cause and what to do next.

## Safety rules

- **Dry run first, always.** Scanning never changes anything.
- **You confirm what you saw.** The dialog lists the exact items and sizes, and nothing happens until you type `DELETE`.
- **Each item is checked again right before it's deleted.** If a channel was unarchived, a user reactivated or a message restored after the scan, that item is skipped.
- **Mattermost deletes first.** For channels and DMs, Binday asks Mattermost to delete, and only then removes leftover files. If Mattermost refuses, no files are touched.
- **Failures don't stop the run, and they're never hidden.** Every problem is listed with its plan number and written to the log.

`VACUUM FULL` is optional. Deleting rows frees space inside PostgreSQL, but the database file only shrinks after a vacuum, which locks the `FileInfo` table while it runs.

## Where things live

| What | Where |
|---|---|
| Saved connection profiles | `~/.binday/profiles.json` (no passwords) |
| Passwords and tokens | Your OS keychain (Windows Credential Manager, macOS Keychain, Linux Secret Service), under `binday` |
| Log of every run | `~/.binday/binday.log` (desktop app), `./binday.log` (command line) |

Binday talks only to your server. There's no telemetry and no update check.
