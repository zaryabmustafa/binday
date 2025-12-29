#!/usr/bin/env python3
"""
Mattermost Purge Tool - Total Clean Edition

Features:
1. Purge Archived Channels.
2. Purge DMs of Deactivated Users.
3. Purge DB Orphans (Files with no parent Post).
4. Safety: Uses Database-driven logic only.
"""

import argparse
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple, Any

# --- CONFIGURATION ---
LARGE_FILE_THRESHOLD_MB = 200
LARGE_FILE_BYTES = LARGE_FILE_THRESHOLD_MB * 1024 * 1024
# ---------------------

try:
    import paramiko
except ImportError:
    print("ERROR: This script requires paramiko. Run: pip install paramiko")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("mm_purge.log", mode='w'), logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

try:
    import psycopg2
except ImportError:
    psycopg2 = None


@dataclass
class MMCTLConfig:
    bin_path: str
    server: Optional[str]
    profile: str
    token: Optional[str]
    ssh_conn: Optional[str] = None
    ssh_password: Optional[str] = None
    _ssh_client: Any = field(default=None, repr=False)
    _sftp_client: Any = field(default=None, repr=False)


def format_bytes(size: float) -> str:
    power = 2 ** 10
    n = 0
    power_labels = {0: '', 1: 'KB', 2: 'MB', 3: 'GB', 4: 'TB'}
    while size > power:
        size /= power
        n += 1
    return f"{size:.2f} {power_labels.get(n, 'TB')}"


def resolve_binary(bin_name: str) -> str:
    if os.path.isfile(bin_name): return os.path.abspath(bin_name)
    return bin_name


def get_or_create_ssh_client(cfg: MMCTLConfig):
    if cfg._ssh_client: return cfg._ssh_client
    host_str = cfg.ssh_conn
    user, host = "root", host_str
    if "@" in host_str: user, host = host_str.split("@", 1)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(hostname=host, username=user, password=cfg.ssh_password)
    cfg._ssh_client = client
    return client


def get_sftp_client(cfg: MMCTLConfig):
    if cfg._sftp_client: return cfg._sftp_client
    ssh = get_or_create_ssh_client(cfg)
    sftp = ssh.open_sftp()
    cfg._sftp_client = sftp
    return sftp


def run_cmd(cmd: Sequence[str], cfg: Optional[MMCTLConfig] = None, allow_fail: bool = False) -> str:
    if cfg and cfg.ssh_conn:
        client = get_or_create_ssh_client(cfg)
        full_cmd_str = shlex.join(cmd)
        stdin, stdout, stderr = client.exec_command(full_cmd_str)
        exit_status = stdout.channel.recv_exit_status()
        out_str = stdout.read().decode().strip()
        err_str = stderr.read().decode().strip()
        if exit_status != 0 and not allow_fail:
            raise RuntimeError(f"Remote Command failed ({exit_status}): {full_cmd_str}\nSTDERR: {err_str}")
        return out_str
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0 and not allow_fail:
        raise RuntimeError(f"Command failed ({p.returncode}): {shlex.join(cmd)}\nSTDERR: {p.stderr.strip()}")
    return p.stdout


def get_remote_folder_size(cfg: MMCTLConfig, path: str) -> int:
    cmd = ["du", "-sb", path]
    try:
        out = run_cmd(cmd, cfg=cfg)
        return int(out.split()[0])
    except Exception:
        return 0


def mmctl_base(cfg: MMCTLConfig) -> List[str]:
    if cfg.ssh_conn: return ["docker", "exec", "-i", "mattermost", "mmctl"]
    return [cfg.bin_path]


def mmctl_login(cfg: MMCTLConfig) -> None:
    if not cfg.server or not cfg.token: return
    cmd = mmctl_base(cfg) + ["auth", "login", "--name", cfg.profile, "--server", cfg.server, "--access-token",
                             cfg.token]
    try:
        run_cmd(cmd, cfg=cfg, allow_fail=True)
    except Exception:
        pass


def mmctl_get_bool(cfg: MMCTLConfig, key: str) -> Optional[bool]:
    cmd = mmctl_base(cfg) + ["config", "get", key, "--json"]
    out = run_cmd(cmd, cfg=cfg, allow_fail=True).strip()
    if not out: return None

    def parse_val(v):
        if isinstance(v, bool): return v
        if isinstance(v, str): return v.lower() == "true"
        return None

    try:
        data = json.loads(out)
        if isinstance(data, dict): return parse_val(data.get(key, data.get("value")))
    except json.JSONDecodeError:
        pass
    lines = out.splitlines()
    for line in reversed(lines):
        clean = line.strip()
        if clean.lower() == "true": return True
        if clean.lower() == "false": return False
        try:
            data = json.loads(clean)
            if isinstance(data, dict):
                res = parse_val(data.get(key, data.get("value")))
                if res is not None: return res
        except json.JSONDecodeError:
            continue
    return None


def mmctl_team_names(cfg: MMCTLConfig) -> List[str]:
    cmd = mmctl_base(cfg) + ["team", "list", "--json"]
    data = json.loads(run_cmd(cmd, cfg=cfg))
    names = []
    for t in data:
        if t.get("name"): names.append(t.get("name"))
    return names


def mmctl_archived_channels_for_team(cfg: MMCTLConfig, team_name: str) -> List[Dict]:
    cmd = mmctl_base(cfg) + ["channel", "list", team_name, "--json"]
    data = json.loads(run_cmd(cmd, cfg=cfg))
    archived = []
    for ch in data:
        delete_at = ch.get("delete_at", ch.get("deleteAt", ch.get("DeleteAt", 0)))
        try:
            val = int(delete_at or 0)
        except (TypeError, ValueError):
            val = 0
        if val != 0: archived.append(ch)
    return archived


def mmctl_delete_channel_by_id(cfg: MMCTLConfig, channel_id: str, execute: bool) -> None:
    cmd = mmctl_base(cfg) + ["channel", "delete", channel_id, "--confirm"]
    if execute: run_cmd(cmd, cfg=cfg, allow_fail=True)


def connect_db(dsn: str):
    if psycopg2 is None: raise RuntimeError("psycopg2 missing")
    return psycopg2.connect(dsn)


def detect_mm_tables(cur) -> Tuple[str, str, str, str]:
    cur.execute(
        "SELECT table_schema, table_name FROM information_schema.tables WHERE table_type='BASE TABLE' AND (lower(table_name) IN ('fileinfo','posts','channels'))")
    rows = cur.fetchall()
    by_schema: Dict[str, set] = {}
    for schema, name in rows:
        by_schema.setdefault(schema, set()).add(name)
    candidates = []
    for schema, names in by_schema.items():
        lowered = {n.lower() for n in names}
        if {"fileinfo", "posts", "channels"}.issubset(lowered): candidates.append(schema)
    if not candidates: raise RuntimeError("Could not find Mattermost tables.")
    schema = "public" if "public" in candidates else candidates[0]
    cur.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = %s AND lower(table_name) IN ('fileinfo','posts','channels')",
        (schema,))
    names = [r[0] for r in cur.fetchall()]

    def pick(target: str) -> str:
        for n in names:
            if n.lower() == target: return n
        raise RuntimeError(f"Missing {target}")

    return schema, pick("fileinfo"), pick("posts"), pick("channels")


def qname(schema: str, table: str) -> str: return f'"{schema}"."{table}"'


def get_real_col(cur, schema, table, col_name_variants):
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s",
                (schema, table))
    real_cols = [r[0] for r in cur.fetchall()]
    for v in col_name_variants:
        for r in real_cols:
            if r == v: return r
            if r.lower() == v.lower(): return r
    raise RuntimeError(f"Column '{col_name_variants[0]}' not found in {table}")


def fetch_channel_file_paths(cur, schema: str, fi: str, posts: str, channels: str, channel_id: str) -> List[str]:
    fi_t, p_t = qname(schema, fi), qname(schema, posts)
    c_postid = get_real_col(cur, schema, fi, ["PostId", "postid"])
    c_path = get_real_col(cur, schema, fi, ["Path", "path"])
    c_thumb = get_real_col(cur, schema, fi, ["ThumbnailPath", "thumbnailpath"])
    c_preview = get_real_col(cur, schema, fi, ["PreviewPath", "previewpath"])
    c_pid = get_real_col(cur, schema, posts, ["Id", "id"])
    c_p_channelid = get_real_col(cur, schema, posts, ["ChannelId", "channelid"])

    sql = f"""SELECT f."{c_path}", f."{c_thumb}", f."{c_preview}" FROM {fi_t} f JOIN {p_t} p ON p."{c_pid}" = f."{c_postid}" WHERE p."{c_p_channelid}" = %s"""
    cur.execute(sql, (channel_id,))
    paths = []
    for path, thumb, preview in cur.fetchall():
        for item in (path, thumb, preview):
            if item: paths.append(item)
    return list(set(paths))


def get_deactivated_users_dms(cur, schema: str, channels_table: str, users_table: str = "Users") -> List[Dict]:
    cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=%s AND lower(table_name)='users'",
                (schema,))
    res = cur.fetchone()
    u_table = res[0] if res else "Users"
    ch_t = qname(schema, channels_table)
    u_t = qname(schema, u_table)
    c_id = get_real_col(cur, schema, u_table, ["Id", "id"])
    c_deleteat = get_real_col(cur, schema, u_table, ["DeleteAt", "deleteat"])
    c_username = get_real_col(cur, schema, u_table, ["Username", "username"])
    c_ch_id = get_real_col(cur, schema, channels_table, ["Id", "id"])
    c_ch_name = get_real_col(cur, schema, channels_table, ["Name", "name"])
    c_ch_type = get_real_col(cur, schema, channels_table, ["Type", "type"])
    sql_users = f"""SELECT "{c_id}", "{c_username}" FROM {u_t} WHERE "{c_deleteat}" > 0"""
    cur.execute(sql_users)
    deactivated_users = cur.fetchall()
    unique_dms = {}
    if not deactivated_users: return []
    print(f"\n  -> Found {len(deactivated_users)} deactivated users. Searching their DMs...")
    for user_id, username in deactivated_users:
        sql_ch = f"""SELECT "{c_ch_id}", "{c_ch_name}" FROM {ch_t} WHERE "{c_ch_type}" = 'D' AND "{c_ch_name}" LIKE %s"""
        cur.execute(sql_ch, (f"%{user_id}%",))
        dms = cur.fetchall()
        for ch_id, ch_name in dms:
            if ch_id not in unique_dms:
                unique_dms[ch_id] = {"id": ch_id, "name": ch_name, "user_id": user_id, "username": username,
                                     "type": "DM"}
    return list(unique_dms.values())


# --- NEW: DB Orphan Logic ---
def get_db_orphans(cur, schema: str, fi_table: str, posts_table: str) -> List[Dict]:
    """Finds files in FileInfo where PostId points to a non-existent Post."""
    logger.info("Scanning for DB Orphans (Left Join PostId is NULL)...")

    fi_t = qname(schema, fi_table)
    p_t = qname(schema, posts_table)

    c_fi_id = get_real_col(cur, schema, fi_table, ["Id", "id"])
    c_fi_postid = get_real_col(cur, schema, fi_table, ["PostId", "postid"])
    c_fi_path = get_real_col(cur, schema, fi_table, ["Path", "path"])
    c_fi_thumb = get_real_col(cur, schema, fi_table, ["ThumbnailPath", "thumbnailpath"])
    c_fi_preview = get_real_col(cur, schema, fi_table, ["PreviewPath", "previewpath"])

    c_p_id = get_real_col(cur, schema, posts_table, ["Id", "id"])

    # The Logic from your Screenshot:
    sql = f"""
        SELECT fi."{c_fi_id}", fi."{c_fi_path}", fi."{c_fi_thumb}", fi."{c_fi_preview}"
        FROM {fi_t} fi
        LEFT JOIN {p_t} p ON p."{c_p_id}" = fi."{c_fi_postid}"
        WHERE p."{c_p_id}" IS NULL
    """

    cur.execute(sql)
    rows = cur.fetchall()

    orphans = []
    for r in rows:
        paths = []
        if r[1]: paths.append(r[1])
        if r[2]: paths.append(r[2])
        if r[3]: paths.append(r[3])

        if paths:
            orphans.append({
                "fi_id": r[0],
                "paths": paths
            })

    return orphans


def delete_db_orphan_row(cur, schema: str, fi_table: str, fi_id: str, execute: bool):
    """Deletes the FileInfo row to clean the DB."""
    if not execute: return
    fi_t = qname(schema, fi_table)
    c_id = get_real_col(cur, schema, fi_table, ["Id", "id"])
    sql = f"""DELETE FROM {fi_t} WHERE "{c_id}" = %s"""
    try:
        cur.execute(sql, (fi_id,))
    except Exception as e:
        logger.error(f"Failed to delete FileInfo row {fi_id}: {e}")


def delete_remote_files(sftp, remote_base: str, rel_paths: List[str], execute: bool) -> Tuple[
    int, int, int, List[Tuple[str, int]]]:
    deleted, missing, total_bytes = 0, 0, 0
    large_files = []

    def remote_join(base, rel):
        return base.rstrip('/') + '/' + rel.lstrip('/')

    for rel in rel_paths:
        full_path = remote_join(remote_base, rel)
        try:
            stat = sftp.stat(full_path)
            fsize = stat.st_size
            total_bytes += fsize
            if fsize > LARGE_FILE_BYTES: large_files.append((os.path.basename(full_path), fsize))
        except (FileNotFoundError, IOError):
            missing += 1
            continue
        if execute:
            try:
                sftp.remove(full_path); deleted += 1
            except Exception:
                pass
    return deleted, missing, total_bytes, large_files


def print_summary_table(data: List[Dict], mode: str, total_reclaimed: int, total_storage_size: int):
    print("\n" + "=" * 80)
    print(f" MATTERMOST PURGE REPORT ({mode})")
    print(f" Showing files larger than {LARGE_FILE_THRESHOLD_MB} MB only.")
    print("=" * 80)
    print(f"{'CHANNEL / USER DM / ORPHAN':<50} | {'FILES':<8} | {'SIZE':<10}")
    print("-" * 80)
    for entry in data:
        if entry.get('type') == 'DM':
            display = f"DM: {entry['username']} ({entry['channel'][:8]}...)"
        elif entry.get('type') == 'Orphan':
            display = f"DB Orphan (FileInfo ID: {entry['channel'][:8]}...)"
        else:
            display = f"{entry['team']}:{entry['channel']}"
        if len(display) > 48: display = display[:45] + "..."
        print(f"{display:<50} | {str(entry['file_count']):<8} | {entry['readable_reclaimed']:<10}")
        large_files = entry.get('large_files', [])
        if large_files:
            print(f"   [!] Large Files (> {LARGE_FILE_THRESHOLD_MB} MB):")
            for fname, fsize in large_files:
                print(f"       - {fname} ({format_bytes(fsize)})")
            print("")
    print("-" * 80)
    print(f"{'RECLAIMED SPACE':<50} | {'':<8} | {format_bytes(total_reclaimed):<10}")
    print("-" * 80)
    print(f"{'CURRENT TOTAL STORAGE USAGE':<50} | {'':<8} | {format_bytes(total_storage_size):<10}")
    print("=" * 80 + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mmctl", default="mmctl")
    ap.add_argument("--mm-server", default=os.getenv("MM_SERVER"))
    ap.add_argument("--mm-token", default=os.getenv("MM_TOKEN"))
    ap.add_argument("--mm-profile", default="local")
    ap.add_argument("--ssh", default=None, required=True)
    ap.add_argument("--ssh-password", default=None)
    ap.add_argument("--pg-dsn", default=os.getenv("MM_PG_DSN"), required=True)
    ap.add_argument("--remote-data-dir", required=True)

    # Modes
    ap.add_argument("--purge-deactivated-users", action="store_true")
    ap.add_argument("--purge-db-orphans", action="store_true", help="Delete files where Post no longer exists")

    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    mode = "EXECUTE" if args.execute else "DRY-RUN"
    print(f"Starting... Mode: {mode}")

    cfg = MMCTLConfig("mmctl", args.mm_server, args.mm_profile, args.mm_token, args.ssh, args.ssh_password)
    mmctl_login(cfg)

    if mmctl_get_bool(cfg, "ServiceSettings.EnableAPIChannelDeletion") is not True:
        logger.error("ServiceSettings.EnableAPIChannelDeletion is false.")
        if args.execute: sys.exit(1)

    conn = connect_db(args.pg_dsn)
    # Enable AutoCommit for DELETE operations to work smoothly
    conn.autocommit = True
    cur = conn.cursor()
    schema, fi_table, posts_table, channels_table = detect_mm_tables(cur)
    sftp = get_sftp_client(cfg)

    processed_count = 0
    grand_total_bytes = 0
    report_data = []

    # 1. Archived Channels
    teams = mmctl_team_names(cfg)
    print(f"Scanning Teams: {teams}")
    for team in teams:
        archived = mmctl_archived_channels_for_team(cfg, team)
        if not archived: continue
        for ch in archived:
            print(".", end="", flush=True)
            ch_name, ch_id = ch.get("name"), ch.get("id")
            paths = []
            try:
                paths = fetch_channel_file_paths(cur, schema, fi_table, posts_table, channels_table, ch_id)
            except Exception:
                pass

            bytes_cleaned = 0
            l_files = []
            if paths:
                deleted, missing, bytes_cleaned, l_files = delete_remote_files(sftp, args.remote_data_dir, paths,
                                                                               execute=args.execute)
                grand_total_bytes += bytes_cleaned

            report_data.append({"team": team, "channel": ch_name, "type": "Channel", "file_count": len(paths),
                                "readable_reclaimed": format_bytes(bytes_cleaned), "large_files": l_files})
            mmctl_delete_channel_by_id(cfg, ch_id, execute=args.execute)
            processed_count += 1
            if args.limit and processed_count >= args.limit: break
        if args.limit and processed_count >= args.limit: break

    # 2. Deactivated Users
    if args.purge_deactivated_users:
        print("\nScanning Deactivated Users DMs...")
        try:
            dm_channels = get_deactivated_users_dms(cur, schema, channels_table)
            print(f"  -> Found {len(dm_channels)} unique DM channels to process.")
            for dm in dm_channels:
                print("x", end="", flush=True)
                paths = []
                try:
                    paths = fetch_channel_file_paths(cur, schema, fi_table, posts_table, channels_table, dm['id'])
                except Exception:
                    pass
                bytes_cleaned = 0
                l_files = []
                if paths:
                    deleted, missing, bytes_cleaned, l_files = delete_remote_files(sftp, args.remote_data_dir, paths,
                                                                                   execute=args.execute)
                    grand_total_bytes += bytes_cleaned
                report_data.append(
                    {"team": "Direct Message", "channel": dm['id'], "username": dm['username'], "type": "DM",
                     "file_count": len(paths), "readable_reclaimed": format_bytes(bytes_cleaned),
                     "large_files": l_files})
                mmctl_delete_channel_by_id(cfg, dm['id'], execute=args.execute)
                processed_count += 1
                if args.limit and processed_count >= args.limit: break
        except Exception as e:
            logger.error(f"Failed to scan deactivated users: {e}")

    # 3. DB Orphans
    if args.purge_db_orphans:
        print("\nScanning DB Orphans (Files with no parent Post)...")
        orphans = get_db_orphans(cur, schema, fi_table, posts_table)
        print(f"  -> Found {len(orphans)} orphans.")

        for orphan in orphans:
            print("o", end="", flush=True)
            bytes_cleaned = 0
            l_files = []
            if orphan['paths']:
                deleted, missing, bytes_cleaned, l_files = delete_remote_files(sftp, args.remote_data_dir,
                                                                               orphan['paths'], execute=args.execute)
                grand_total_bytes += bytes_cleaned

            report_data.append({
                "team": "Orphan Cleanup",
                "channel": orphan['fi_id'],  # Use ID as placeholder
                "type": "Orphan",
                "file_count": len(orphan['paths']),
                "readable_reclaimed": format_bytes(bytes_cleaned),
                "large_files": l_files
            })

            # ALSO DELETE THE DB ROW
            delete_db_orphan_row(cur, schema, fi_table, orphan['fi_id'], execute=args.execute)

            processed_count += 1
            if args.limit and processed_count >= args.limit: break

    print("\nCalculating total storage usage...")
    total_usage = get_remote_folder_size(cfg, args.remote_data_dir)
    print("Scan Complete.")
    cur.close();
    conn.close()
    if cfg._sftp_client: cfg._sftp_client.close()
    if cfg._ssh_client: cfg._ssh_client.close()
    print_summary_table(report_data, mode, grand_total_bytes, total_usage)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)