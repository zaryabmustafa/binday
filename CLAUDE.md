# CLAUDE.md

Python tool that reclaims disk space on a self-hosted Mattermost (Team Edition) server by permanently deleting archived channels, deactivated users' DMs and orphaned file attachments, then optionally running `VACUUM FULL`. A CLI (`app.py`) and a Flet desktop app (`gui.py`) share one core. User docs: `README.md`. Product truth: `PRODUCT.md`. Design system: `DESIGN.md` (+ `.impeccable/design.json`); the approved screens are `mockup/index.html`.

**Deletions are irreversible.** Treat any change to deletion logic, SQL, eligibility re-checks or path handling as high-risk.

## Files

- `app.py`: core plus CLI. `Session` (SSH, SFTP, Postgres, mmctl in Docker), `scan()` (dry run, returns plan items), `apply()` (deletes exactly the given items), `vacuum()`, `start_tunnel()` (Postgres over SSH), `main()` (CLI).
- `gui.py`: Flet UI only, no deletion logic. Flow: Connect → Scan → Review → Confirm dialog → Collecting → Results.
- `demo.py`: `python gui.py --demo` swaps the core's server calls for synthetic data (one skipped DM, one failed orphan). Nothing connects anywhere.
- `pyproject.toml` + `assets/` (icon, bundled Public Sans font) + `.github/workflows/build.yml`: release builds via `flet build windows|macos|linux` (module `gui`; testenv/mockup/tests excluded). The `portable` job also makes a single-file `Binday-portable.exe` with `flet pack` (PyInstaller, `assets/icon.ico`, `--add-data assets:assets`); locally: `pip install pyinstaller` then the same `flet pack` line. On this Windows machine the user folder has a space, which breaks Flutter's Dart hooks, so local builds need `PATH=/d/tools/flutter/3.47.9/bin:$PATH` and `PUB_CACHE='D:\tools\pub-cache'`.
- `docs/` (cli, how-it-works, development), `SECURITY.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `.github/` issue forms + PR template + dependabot: the public repo setup, modelled on the owner's Ferry and Headroom repos. Keep README claims true to the code; README screenshots come from `assets/readme/capture.ps1` (demo autoplay: `gui.py --demo --demo-screen=review|results|connect --theme=light|dark`), never with real server data or full home paths.
- `testenv/`: a real Mattermost 12.1 test server in WSL Docker (setup.sh, seed.py, verify.py); see testenv/README.md. Use it for any change to deletion logic: `--reset`, seed, `verify --pre`, purge, `verify --post`.
- `test_app.py`: `python test_app.py` self-check with fakes (format_bytes, tunnel round trip, apply only deletes eligible items in order, per-item callbacks).

## Run

```bash
python -m venv .venv && .venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python test_app.py
.venv/Scripts/python gui.py --demo          # desktop window, synthetic data
.venv/Scripts/python gui.py --demo --web    # browser at http://127.0.0.1:8550 (.claude/launch.json "purge-demo-web")
python app.py --ssh root@HOST[:port] [--ssh-password PW] --pg-dsn "host=... dbname=mattermost user=mmuser password=..." [--db-via-ssh] --remote-data-dir /path/to/data [--mm-container mattermost] [--purge-archived-channels] [--purge-deactivated-users] [--purge-db-orphans] [--vacuum] [--limit N] [--execute]
```

- The CLI needs at least one `--purge-*`/`--vacuum` flag; the default is a dry run. Never run `--execute`, or confirm a delete in the GUI, against a real server unless the user explicitly asks.
- Python 3.15 is the only interpreter on this machine; all deps (Flet 1.1.2, paramiko 5, psycopg2-binary, keyring) work on it. `flet[desktop]` and `flet-web` are separate packages in Flet 1.x.
- `.vscode/launch.json` is tracked and has placeholder credentials; never put real ones there.
- Logs: the CLI overwrites `./binday.log`; the GUI writes `~/.binday/binday.log`. Profiles are in `~/.binday/profiles.json`, and secrets are in the OS keychain (service `binday`).

## Core behaviour (app.py)

- `Session` connects SSH (key/agent when there is no password; system known_hosts loaded, so a changed key still fails), SFTP, then Postgres (`autocommit=True`: needed for VACUUM, so every DELETE commits at once). With `db_via_ssh` the pg host/port are as seen from the server.
- `detect_mm_tables` finds the schema holding fileinfo/posts/channels/users; always use `sess.q(table)` and `sess.col(table, "Name")` (case-insensitive) in SQL, with values as `%s` params.
- Plan item: `{kind: channel|dm|orphan, id, label, group, paths, bytes, missing, [user_id], [plan]}`. The GUI adds `plan` numbers sorted by size; orphans share one plan number (one row).
- `apply()` runs items in the given order. For each: `still_eligible()` re-checks it (channel still archived / DM user still deactivated / file still orphaned with a non-empty PostId). Channels: `mmctl channel delete` first, then remove files. DMs: Mattermost refuses to delete DM channels ("Unable to delete direct or group message channels"), so every post is removed with `mmctl post delete --permanent` and the empty DM stays; then leftover files are removed. Mattermost's delete always runs before ours, so a failed mmctl touches no files. Orphans: files first, then the FileInfo row.
- `mmctl ... --json` prints `null` (not `[]`) for empty lists. `on_item(item, status, freed, new_errors)` gets `deleted|skipped|failed`. An item with any error counts as failed, even if some files went.
- Errors go through `sess.error()` (collected and logged). The CLI exits 1 if any occurred.
- Empty or NULL `PostId` = an upload not yet attached to a post. It is never treated as an orphan.

## GUI conventions (gui.py)

- Follow DESIGN.md. Colours come from `LIGHT`/`DARK` mapped onto spare Material `ColorScheme` roles (e.g. `secondary` = teal channel bin, `inverse_surface` = rail navy, `primary_fixed` = ok green), so light/dark follow the OS. Use the module constants (`INK`, `RAIL`, `KIND[...]`), never raw hex in widgets.
- Mattermost blue `#1C58D9` is for interaction only. Bin colours (teal/yellow/brown) appear only in bin chips, legends and the load bar. Red only on the delete action. Failed items use an ink cross-hatch, a `cancel` icon and the word "Failed".
- Screens are rebuilt by `paint()`. Worker threads call `paint(force=False)`, which is throttled; blocking work goes in `page.run_thread`. The rail is locked while collecting.
- Layout must hold at about 1,000 logical px (a 1280 window at 125% scaling). Check with the demo in the browser pane after UI changes.
