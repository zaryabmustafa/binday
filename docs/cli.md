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
  --purge-archived-channels --purge-deactivated-users --purge-db-orphans
```

It prints a report of every item it would remove, with file counts and sizes, and the space in use now.

## Delete

Add `--execute` once the report looks right. Add `--vacuum` to shrink the database file afterwards. It locks the `FileInfo` table while it runs, so use a maintenance window.

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
| `--purge-archived-channels` | Delete archived channels and their files. Needs `ServiceSettings.EnableAPIChannelDeletion = true`. |
| `--purge-deactivated-users` | Empty DMs with deactivated users: every message and file goes, the empty conversation stays. |
| `--purge-db-orphans` | Delete files whose message no longer exists, and their `FileInfo` rows. |
| `--vacuum` | Run `VACUUM FULL` on `FileInfo` after deleting (only with `--execute`). |
| `--execute` | Actually delete. Without it, nothing changes. |
| `--limit N` | Stop after N items (for trying it on a small batch). |

At least one `--purge-*` option or `--vacuum` is required.

On Git Bash for Windows, prefix the command with `MSYS_NO_PATHCONV=1`, or it rewrites `/opt/...` into a Windows path.
