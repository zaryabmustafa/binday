# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

Desktop app built with Flet 1.x (Python, Flutter-rendered Material 3), shipped for Windows, macOS and Linux via `flet build`. The CLI (`app.py`) and the desktop UI (`gui.py`) share one core. Design mockups are authored as HTML and must be buildable with Flet/Flutter Material 3 controls. (Platform is recorded as `web` because the schema has no desktop value; there is no browser deployment.)

## Users

Administrators of self-hosted Mattermost Team Edition servers, who run cleanup occasionally (e.g. when the disk fills up) from their own workstation. They are comfortable with SSH, Docker and PostgreSQL but are about to do something irreversible on a production chat server, usually with no undo besides a backup.

## Product Purpose

Reclaims disk space on a self-hosted Mattermost server by permanently deleting archived channels, direct messages of deactivated users, and orphaned file attachments (files whose post no longer exists), then optionally running `VACUUM FULL` on the FileInfo table. Success: the admin sees exactly what will be removed and how much space it frees, deletes only what they chose, and gets an honest report of what happened.

## Positioning

Mattermost's built-in data retention is Enterprise-only, and Team Edition has no supported way to purge this data. This tool fills that gap with a database-driven, dry-run-first workflow: scan reads only, the admin reviews and ticks items, apply deletes exactly the reviewed plan and re-checks each item before deleting it.

## Operating Context

- Runs on the admin's workstation; reaches the server over SSH (password or key/agent), runs `mmctl` inside the Mattermost Docker container, reads/writes PostgreSQL (directly or through an SSH tunnel), and deletes files over SFTP.
- Flow: Connect → Scan (dry run) → Review → Confirm → Results.
- Saved connection profiles live in `~/.binday/profiles.json`; passwords in the OS keychain.
- Requires `ServiceSettings.EnableAPIChannelDeletion = true` on the server to delete archived channels. DMs are emptied by permanently deleting their posts (Mattermost can't delete DM channels).

## Capabilities and Constraints

- Deletions are permanent. Default is always dry run; deletion requires reviewing the breakdown and typing DELETE to confirm.
- Items that changed since the scan are skipped, not deleted.
- `VACUUM FULL` locks the FileInfo table; maintenance windows only.
- Errors never stop a run; they are collected and shown at the end and written to `~/.binday/binday.log`.
- Orphaned files may number in the thousands; the review shows them as one group.

## Brand Commitments

Name: **Binday**, always written in full as "Binday for Mattermost" where people first meet it (title, README, release page); "Binday" alone inside the app. Mark: a calendar day (bin day) holding the three bin bars; see DESIGN.md "Brand". Voice: calm, plain and exact about consequences. Independent, unofficial open-source tool. The user chose Mattermost's blue (#1C58D9 actions, #1E325C navy rail) so the tool feels at home next to Mattermost; no Mattermost logo, wordmark or implied endorsement.

## Evidence on Hand

No real usage data, screenshots, testimonials or benchmarks exist. Mockups use synthetic, clearly illustrative data (team/channel names, sizes); never present it as real.

## Product Principles

1. Nothing destructive happens by accident: dry run by default, explicit review, confirmation that proves the admin read the numbers.
2. Show the real consequence: counts, sizes and locations before deleting; honest results after.
3. Never hide failure: every skipped or failed item is visible.
4. The admin stays in control of scope: they can untick anything.

## Accessibility & Inclusion

Must work in both light and dark OS themes and with keyboard navigation. Destructive state must never be conveyed by colour alone.
