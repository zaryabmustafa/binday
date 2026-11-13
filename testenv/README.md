# Local test server (WSL + Docker)

A real Mattermost Team Edition 12.1 server to test the purge tool against, without touching anything real. Everything runs in Docker inside WSL; nothing needs sudo.

| Service | Where | Notes |
|---|---|---|
| Mattermost | http://127.0.0.1:8065 | container named `mattermost`, mmctl in local mode, channel deletion on |
| SSH "server" | `root@127.0.0.1:2222` | key auth only (`testenv/.keys/id_ed25519`); shares Mattermost's data at `/opt/mattermost/data` and the Docker socket |
| PostgreSQL | not published | reach it through SSH as host `postgres`, port 5432, db/user `mattermost`/`mmuser`; password in `testenv/.env` |

Test secrets (`.env`, `.keys/`, `.seed.json` with the seeded users' passwords) are generated on first run and gitignored.

## Use it

From the repo root, in PowerShell or Git Bash:

```bash
wsl -e sh testenv/setup.sh --reset
```
```bash
.venv/Scripts/python testenv/seed.py
```
```bash
.venv/Scripts/python testenv/verify.py --pre
```

Then purge with the app (`python gui.py`) or the CLI, and check:

```bash
.venv/Scripts/python testenv/verify.py --post
```

Add `--kept payroll-2023` to `--post` if you unticked that channel in Review.

App settings: SSH login `root@127.0.0.1:2222`, SSH key file `<repo>\testenv\.keys\id_ed25519`, data directory `/opt/mattermost/data`, database host `postgres` through SSH, user `mmuser`, password from `testenv/.env`.

CLI (Git Bash needs `MSYS_NO_PATHCONV=1`, or it rewrites `/opt/...` into a Windows path):

```bash
cd testenv && MSYS_NO_PATHCONV=1 ../.venv/Scripts/python ../app.py --ssh root@127.0.0.1:2222 --ssh-key .keys/id_ed25519 --db-via-ssh --pg-dsn "host=postgres port=5432 dbname=mattermost user=mmuser password=$(grep POSTGRES_PASSWORD .env | cut -d= -f2)" --remote-data-dir /opt/mattermost/data --purge-archived-channels --purge-deactivated-users --purge-db-orphans
```

## What the seed creates

- **Purge targets:** archived channels `old-project` (eng, files), `payroll-2023` (eng, files) and `empty-archive` (ops, no files); DMs with deactivated users bob and carol (3 conversations); 4 orphaned files (their posts were deleted straight from the database).
- **Must survive:** active channel `keep-me`, the DM with active user dave, and `draft-upload.png`, an upload never attached to a post.

## Notes

- WSL shuts its VM down when no WSL process is running, which stops the containers. They restart with Docker (`restart: unless-stopped`); keep a WSL terminal open while testing.
- `setup.sh --reset` deletes all test data (containers and volumes) and starts fresh.
- To stop everything: `wsl -e sh -c "cd testenv && docker compose down"` (add `-v` to delete the data).
