# Command line

`app.py` is the same engine as the desktop app, for scripts and servers without a screen. It does a **dry run by default**: nothing changes until you add `--execute`.

```bash
pip install paramiko psycopg2-binary
```

## Dry run

```bash
python app.py \
  --ssh root@chat.example.org \
  --pg-dsn "host=127.0.0.1 dbname=mattermost user=mmuser password=YOUR_DB_PASSWORD" --db-via-ssh \
  --remote-data-dir /opt/mattermost/data \
  --purge-archived-channels --purge-deactivated-users --purge-deleted-posts --purge-db-orphans
```

It prints a report of every item it would remove, with file counts and sizes, and the space in use now.

## Delete

Add `--execute` once the report looks right. Add `--vacuum` to shrink the database file afterwards. It rewrites only the tables the run deleted from and locks each while it runs, so use a maintenance window.

`--purge-deactivated-users` and `--purge-deleted-posts` need `ServiceSettings.EnableAPIPostDeletion = true` unless `mmctl` runs in local mode; `--execute` stops before deleting anything if it's off.

```bash
python app.py ... --purge-archived-channels --purge-deactivated-users --purge-db-orphans --vacuum --execute
```

The exit code is 1 if anything failed; details are in `binday.log` in the current folder.

## Options

| Option | What it does |
|---|---|
| `--ssh user@host[:port]` | SSH login for the server. Required. The user defaults to `root`. |
| `--ssh-password PW` | SSH password. Leave it out to use your SSH key or agent. |
| `--ssh-key FILE` | A private key file, if it isn't in `~/.ssh` or your agent. |
| `--pg-dsn DSN` | PostgreSQL connection string. Required. |
| `--db-via-ssh` | Reach PostgreSQL through the SSH connection. The DSN's host and port are then as seen from the server, so the database doesn't need to be exposed. |
| `--remote-data-dir PATH` | Mattermost's data directory on the server, e.g. `/opt/mattermost/data`. Required. |
| `--mm-container NAME` | The Docker container running Mattermost (default `mattermost`). `mmctl` runs inside it. |
| `--mm-server URL`, `--mm-token TOKEN` | Log `mmctl` in, if it isn't already logged in inside the container. |
| `--purge-archived-channels` | Delete archived channels and their files. Needs `ServiceSettings.EnableAPIChannelDeletion = true` unless `mmctl` local mode works. |
| `--purge-deactivated-users` | Empty DMs with deactivated users (not disabled bots): every message and file goes, from both sides; the empty conversation stays. |
| `--purge-deleted-posts` | Permanently delete messages people deleted (Mattermost only hides them) and their files. |
| `--purge-db-orphans` | Delete files whose message no longer exists, or that an edit removed and no version of the message still uses, and their `FileInfo` rows. |
| `--purge-abandoned-uploads` | Delete uploads that never finished, started over a week ago and not written to since, and their `UploadSessions` rows. |
| `--purge-untracked-files` | Delete files in the dated attachment folders that no database row points to (untouched for a day). Refuses if most files look untracked. |
| `--purge-deleted-emoji` | Delete the images Mattermost keeps (`image_deleted`) after a custom emoji is deleted. |
| `--tidy-edit-history` | Delete earlier versions of edited messages. Users lose "Show edit history" and restore for them. |
| `--tidy-jobs` | Delete finished job records older than 30 days (the same query as Mattermost's own, off-by-default, cleanup). |
| `--tidy-link-previews` | Delete the link-preview cache for messages older than 30 days; Mattermost fetches a preview again when one is viewed. |
| `--vacuum` | Run `VACUUM FULL` (only with `--execute`): with `--purge-*`/`--tidy-*` options, on the tables those bins delete from; on its own, on every table Binday cleans. A table busy for 10 seconds is skipped rather than waited for. |
| `--execute` | Actually delete. Without it, nothing changes. |
| `--limit N` | Stop the scan after N items (for trying it on a small batch). Deleted messages inside a channel or DM in the plan go with it and don't count. |

At least one `--purge-*`/`--tidy-*` option or `--vacuum` is required. If a required setting is off (see above), `--execute` stops before deleting anything at all.

On Git Bash for Windows, prefix the command with `MSYS_NO_PATHCONV=1`, or it rewrites `/opt/...` into a Windows path.
