# Mattermost Data Purge Tool

A robust Python utility to reclaim disk space on self-hosted Mattermost servers. It safely cleans up old data that Mattermost's built-in tools often miss, specifically targeting deactivated users and orphaned files.

> [!WARNING]
> **USE AT YOUR OWN RISK.**
> This tool performs **permanent, irreversible deletions** from your database and file system.
> * **Backup:** Always backup your database and `/data` directory before running this tool.
> * **Dry Run:** Always run without the `--execute` flag first to review what will be deleted.
> * **Liability:** The author assumes no liability for data loss, corruption, or downtime.

## 🚀 Features

* **Purge Deactivated Users:** Deletes all Direct Messages (DMs) and associated files for users who have been deactivated.
* **Purge DB Orphans:** Identifies and deletes files recorded in the database where the parent Message (Post) no longer exists (zombie files).
* **Vacuum Support:** Runs `VACUUM FULL` on the `FileInfo` table to physically reclaim disk space from PostgreSQL after deletion.
* **Safety First:** Uses database-driven logic. Does not perform "blind" disk scans, protecting system assets like avatars and plugins.
* **Zero Latency Mode:** Designed to run directly on the Mattermost server for maximum performance.

## 📋 Prerequisites

* **OS:** Linux (Recommended)
* **Python:** 3.8+
* **Database:** PostgreSQL
* **Access:** Root/Sudo access to the server (required to read/delete files in the local data directory).

### Dependencies

Install the required Python packages:

```bash
pip3 install paramiko psycopg2-binary

## 🛠️ Usage

It is highly recommended to run this script **locally** on your Mattermost server to avoid network latency.

### 1. Dry Run (Safe Mode)

By default, the script runs in **Dry Run** mode. It will calculate what _would_ be deleted and show you a summary report without touching any data.

Bash

```
python3 app.py \
  --ssh "root@127.0.0.1" \
  --ssh-password "YOUR_ROOT_PASSWORD" \
  --pg-dsn "host=127.0.0.1 dbname=mattermost user=mmuser password=YOUR_DB_PASS" \
  --remote-data-dir "/mnt/user/mattermost/data" \
  --purge-deactivated-users \
  --purge-db-orphans

```

### 2. Execute (Destructive Mode)

Add the `--execute` flag to actually delete the data.

Bash

```
python3 app.py \
  --ssh "root@127.0.0.1" \
  --ssh-password "YOUR_ROOT_PASSWORD" \
  --pg-dsn "host=127.0.0.1 dbname=mattermost user=mmuser password=YOUR_DB_PASS" \
  --remote-data-dir "/mnt/user/mattermost/data" \
  --purge-deactivated-users \
  --purge-db-orphans \
  --vacuum \
  --execute

```

## ⚙️ Command Line Arguments

**Argument**

**Description**

`--ssh`

SSH Connection string (e.g., `root@127.0.0.1`). Required for file operations.

`--ssh-password`

Password for the SSH user.

`--pg-dsn`

PostgreSQL connection string (DSN).

`--remote-data-dir`

Absolute path to the Mattermost `data` directory (e.g., `/opt/mattermost/data`).

`--purge-deactivated-users`

Flag to enable scanning/deleting DMs of deactivated users.

`--purge-db-orphans`

Flag to enable scanning/deleting files with no parent Post.

`--vacuum`

**CAUTION:** Runs `VACUUM FULL` on the `FileInfo` table. This reclaims disk space but **will lock your database** table. Run only during maintenance windows.

`--execute`

Commits changes. If omitted, the script runs in Dry Run mode.

`--limit`

(Optional) Stop after processing X items. Useful for testing small batches.

## 🔍 How it Works

1.  **Orphan Check:** Queries the Postgres `FileInfo` table for entries where the `PostId` points to a non-existent message.
    
2.  **Deactivated User Check:** Finds users with `DeleteAt > 0`, identifies their Direct Message channels, and targets all files within those channels.
    
3.  **Verification:** Before deleting, it uses SFTP/SSH to confirm the file actually exists on the disk.
    
4.  **Deletion:** Removes the file from the disk (SFTP) and then removes the row from the database (SQL).
    
5.  **Vacuum:** (Optional) Rewrites the database table to release bloat back to the OS.
    

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.