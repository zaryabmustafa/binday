#!/usr/bin/env python3
"""
Binday for Mattermost: command line and shared core

Core (used by both this CLI and gui.py):
  Session  - SSH + SFTP + Postgres (optionally tunnelled over SSH) + mmctl in Docker.
  scan()   - dry run: returns a plan, a list of items with file paths and sizes.
  apply()  - deletes exactly the items in a plan, re-checking each one first.

Item kinds:
  channel - archived channel     -> `mmctl channel delete`, then remove its files
  dm      - deactivated user's DM -> `mmctl post delete --permanent` for every post, then remove leftover files
            (Mattermost refuses to delete DM channels, so the empty conversation stays)
  orphan  - FileInfo row whose post is gone -> delete files, then the FileInfo row
"""

import argparse
import json
import logging
import shlex
import socket
import sys
import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Set

import paramiko
import psycopg2
import psycopg2.extensions

logger = logging.getLogger("binday")

KINDS = ("channel", "dm", "orphan")
KIND_LABELS = {"channel": "Archived channel", "dm": "Deactivated user DM", "orphan": "Orphaned file"}


def format_bytes(size: float) -> str:
    n = 0
    labels = ["B", "KB", "MB", "GB", "TB"]
    while size >= 1024 and n < len(labels) - 1:
        size /= 1024
        n += 1
    return f"{size:.2f} {labels[n]}"


# --------------------------------------------------------------------------- SSH tunnel

def start_tunnel(transport: paramiko.Transport, remote_host: str, remote_port: int) -> socket.socket:
    """Forward a local ephemeral port to remote_host:remote_port as seen from the SSH server.
    Returns the listening socket; close it to stop accepting. Port = sock.getsockname()[1]."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)

    def pump(src, dst):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except (OSError, EOFError):
            pass
        finally:
            for s in (src, dst):
                try:
                    s.close()
                except Exception:
                    pass

    def accept_loop():
        while True:
            try:
                sock, peer = srv.accept()
            except OSError:
                return  # listener closed
            try:
                chan = transport.open_channel("direct-tcpip", (remote_host, remote_port), peer)
            except Exception as e:
                logger.error(f"SSH tunnel to {remote_host}:{remote_port} failed: {e}")
                sock.close()
                continue
            threading.Thread(target=pump, args=(sock, chan), daemon=True).start()
            threading.Thread(target=pump, args=(chan, sock), daemon=True).start()

    threading.Thread(target=accept_loop, daemon=True).start()
    return srv


# --------------------------------------------------------------------------- Session

@dataclass
class Settings:
    ssh: str                      # user@host (user defaults to root)
    remote_data_dir: str          # Mattermost data dir on the server
    pg: Dict[str, str]            # psycopg2 connect kwargs: host, port, dbname, user, password
    ssh_password: Optional[str] = None   # None/empty -> SSH key / agent
    ssh_key: Optional[str] = None        # private key file; None -> ~/.ssh defaults and agent
    db_via_ssh: bool = False      # tunnel Postgres through SSH; pg host/port are then as seen from the server
    container: str = "mattermost"
    mm_server: Optional[str] = None
    mm_token: Optional[str] = None
    mm_profile: str = "local"


class Session:
    def __init__(self, settings: Settings):
        self.s = settings
        self.errors: List[str] = []
        self.ssh = self.sftp = self.conn = self.cur = self._tunnel = None
        self._cols: Dict[str, List[str]] = {}
        try:
            self._connect()
        except Exception:
            self.close()
            raise

    def _connect(self):
        s = self.s
        user, host = ("root", s.ssh)
        if "@" in s.ssh:
            user, host = s.ssh.split("@", 1)
        port = 22
        if ":" in host:
            host, p = host.rsplit(":", 1)
            port = int(p)
        self.ssh = paramiko.SSHClient()
        self.ssh.load_system_host_keys()  # a changed known key still raises BadHostKeyException
        self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.ssh.connect(hostname=host, port=port, username=user, password=s.ssh_password or None,
                         key_filename=s.ssh_key or None, timeout=15)
        self.sftp = self.ssh.open_sftp()

        pg = {k: v for k, v in s.pg.items() if v not in (None, "")}
        if s.db_via_ssh:
            self._tunnel = start_tunnel(self.ssh.get_transport(), pg.get("host", "127.0.0.1"),
                                        int(pg.get("port", 5432)))
            pg["host"], pg["port"] = "127.0.0.1", self._tunnel.getsockname()[1]
        pg.setdefault("connect_timeout", 15)
        self.conn = psycopg2.connect(**pg)
        self.conn.autocommit = True  # needed for VACUUM; every DELETE commits immediately
        self.cur = self.conn.cursor()
        self.schema, self.t = detect_mm_tables(self.cur)

        if s.mm_server and s.mm_token:
            self.mmctl("auth", "login", "--name", s.mm_profile, "--server", s.mm_server,
                       "--access-token", s.mm_token, allow_fail=True)

    # -- helpers
    def error(self, msg: str) -> None:
        self.errors.append(msg)
        logger.error(msg)

    def run(self, cmd: Sequence[str], allow_fail: bool = False) -> str:
        full = shlex.join(cmd)
        _, stdout, stderr = self.ssh.exec_command(full)
        status = stdout.channel.recv_exit_status()
        out, err = stdout.read().decode().strip(), stderr.read().decode().strip()
        if status != 0 and not allow_fail:
            raise RuntimeError(f"Remote command failed ({status}): {full}\nSTDERR: {err}")
        return out

    def mmctl(self, *args: str, allow_fail: bool = False) -> str:
        return self.run(["docker", "exec", "-i", self.s.container, "mmctl", *args], allow_fail=allow_fail)

    def q(self, table: str) -> str:
        """Quoted schema.table for a logical table name (fileinfo/posts/channels/users)."""
        return f'"{self.schema}"."{self.t[table]}"'

    def col(self, table: str, name: str) -> str:
        """Real (quoted) column name, matched case-insensitively."""
        real = self.t[table]
        if real not in self._cols:
            self.cur.execute("SELECT column_name FROM information_schema.columns "
                             "WHERE table_schema=%s AND table_name=%s", (self.schema, real))
            self._cols[real] = [r[0] for r in self.cur.fetchall()]
        for c in self._cols[real]:
            if c.lower() == name.lower():
                return f'"{c}"'
        raise RuntimeError(f"Column {name} not found in {real}")

    def close(self):
        for obj in (self.cur, self.conn, self._tunnel, self.sftp, self.ssh):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass


def detect_mm_tables(cur):
    wanted = {"fileinfo", "posts", "channels", "users"}
    cur.execute("SELECT table_schema, table_name FROM information_schema.tables "
                "WHERE table_type='BASE TABLE' AND lower(table_name) IN ('fileinfo','posts','channels','users')")
    by_schema: Dict[str, Dict[str, str]] = {}
    for schema, name in cur.fetchall():
        by_schema.setdefault(schema, {})[name.lower()] = name
    candidates = [sch for sch, t in by_schema.items() if wanted.issubset(t)]
    if not candidates:
        raise RuntimeError("Could not find Mattermost tables (FileInfo, Posts, Channels, Users).")
    schema = "public" if "public" in candidates else candidates[0]
    return schema, by_schema[schema]


# --------------------------------------------------------------------------- mmctl queries

def channel_deletion_enabled(sess: Session) -> Optional[bool]:
    key = "ServiceSettings.EnableAPIChannelDeletion"
    out = sess.mmctl("config", "get", key, "--json", allow_fail=True).strip()
    for line in [out] + list(reversed(out.splitlines())):
        line = line.strip()
        if line.lower() in ("true", "false"):
            return line.lower() == "true"
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        val = data.get(key, data.get("value")) if isinstance(data, dict) else data
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.lower() == "true"
    return None


def mmctl_team_names(sess: Session) -> List[str]:
    return [t["name"] for t in (json.loads(sess.mmctl("team", "list", "--json") or "[]") or []) if t.get("name")]


def mmctl_archived_channels(sess: Session, team: str) -> List[Dict]:
    out = []
    for ch in (json.loads(sess.mmctl("channel", "list", team, "--json") or "[]") or []):
        try:
            if int(ch.get("delete_at", ch.get("deleteAt", ch.get("DeleteAt", 0))) or 0) != 0:
                out.append(ch)
        except (TypeError, ValueError):
            pass
    return out


def storage_usage(sess: Session) -> int:
    try:
        return int(sess.run(["du", "-sb", sess.s.remote_data_dir]).split()[0])
    except Exception:
        return 0


# --------------------------------------------------------------------------- DB queries

def channel_file_paths(sess: Session, channel_id: str) -> List[str]:
    f, p = "fileinfo", "posts"
    sess.cur.execute(
        f"SELECT fi.{sess.col(f, 'Path')}, fi.{sess.col(f, 'ThumbnailPath')}, fi.{sess.col(f, 'PreviewPath')} "
        f"FROM {sess.q(f)} fi JOIN {sess.q(p)} p ON p.{sess.col(p, 'Id')} = fi.{sess.col(f, 'PostId')} "
        f"WHERE p.{sess.col(p, 'ChannelId')} = %s", (channel_id,))
    return sorted({x for row in sess.cur.fetchall() for x in row if x})


def deactivated_user_dms(sess: Session) -> List[Dict]:
    u, c = "users", "channels"
    sess.cur.execute(f"SELECT {sess.col(u, 'Id')}, {sess.col(u, 'Username')} FROM {sess.q(u)} "
                     f"WHERE {sess.col(u, 'DeleteAt')} > 0")
    dms: Dict[str, Dict] = {}
    for user_id, username in sess.cur.fetchall():
        # DM channel names are "<userA>__<userB>"
        sess.cur.execute(f"SELECT {sess.col(c, 'Id')}, {sess.col(c, 'Name')} FROM {sess.q(c)} "
                         f"WHERE {sess.col(c, 'Type')} = 'D' AND {sess.col(c, 'Name')} LIKE %s", (f"%{user_id}%",))
        for ch_id, name in sess.cur.fetchall():
            if ch_id not in dms:
                other_id = next((x for x in name.split("__") if x != user_id), user_id)
                dms[ch_id] = {"id": ch_id, "user_id": user_id, "username": username, "other_id": other_id}
    for dm in dms.values():  # name the other person too: "DM with bob" alone is ambiguous
        sess.cur.execute(f"SELECT {sess.col(u, 'Username')} FROM {sess.q(u)} WHERE {sess.col(u, 'Id')} = %s", (dm["other_id"],))
        row = sess.cur.fetchone()
        dm["other"] = row[0] if row else "unknown user"
    return list(dms.values())


def channel_post_ids(sess: Session, channel_id: str) -> List[str]:
    p = "posts"
    sess.cur.execute(f"SELECT {sess.col(p, 'Id')} FROM {sess.q(p)} WHERE {sess.col(p, 'ChannelId')} = %s", (channel_id,))
    return [r[0] for r in sess.cur.fetchall()]


def _orphan_sql(sess: Session) -> str:
    f, p = "fileinfo", "posts"
    pid = sess.col(f, "PostId")
    # Empty/NULL PostId = uploaded but not yet attached to a post (e.g. a draft); not an orphan.
    return (f"fi.{pid} IS NOT NULL AND fi.{pid} <> '' AND NOT EXISTS "
            f"(SELECT 1 FROM {sess.q(p)} p WHERE p.{sess.col(p, 'Id')} = fi.{pid})")


def db_orphans(sess: Session) -> List[Dict]:
    f = "fileinfo"
    sess.cur.execute(
        f"SELECT fi.{sess.col(f, 'Id')}, fi.{sess.col(f, 'Name')}, fi.{sess.col(f, 'Path')}, "
        f"fi.{sess.col(f, 'ThumbnailPath')}, fi.{sess.col(f, 'PreviewPath')} "
        f"FROM {sess.q(f)} fi WHERE {_orphan_sql(sess)}")
    return [{"id": r[0], "name": r[1], "paths": [x for x in r[2:] if x]} for r in sess.cur.fetchall()]


def still_eligible(sess: Session, item: Dict) -> bool:
    """Re-check an item right before deleting it, so apply() never acts on stale scan data."""
    c, u, f = "channels", "users", "fileinfo"
    if item["kind"] == "channel":
        sess.cur.execute(f"SELECT {sess.col(c, 'DeleteAt')} FROM {sess.q(c)} WHERE {sess.col(c, 'Id')} = %s",
                         (item["id"],))
        row = sess.cur.fetchone()
        return bool(row and row[0] and int(row[0]) > 0)
    if item["kind"] == "dm":
        sess.cur.execute(
            f"SELECT 1 FROM {sess.q(c)} ch, {sess.q(u)} us WHERE ch.{sess.col(c, 'Id')} = %s "
            f"AND us.{sess.col(u, 'Id')} = %s AND us.{sess.col(u, 'DeleteAt')} > 0", (item["id"], item["user_id"]))
        return sess.cur.fetchone() is not None
    sess.cur.execute(f"SELECT 1 FROM {sess.q(f)} fi WHERE fi.{sess.col(f, 'Id')} = %s AND {_orphan_sql(sess)}",
                     (item["id"],))
    return sess.cur.fetchone() is not None


# --------------------------------------------------------------------------- files

def _remote(sess: Session, rel: str) -> str:
    return sess.s.remote_data_dir.rstrip("/") + "/" + rel.lstrip("/")


def measure_files(sess: Session, paths: List[str]):
    """-> (bytes on disk, number of paths missing on disk)"""
    total, missing = 0, 0
    for rel in paths:
        try:
            total += sess.sftp.stat(_remote(sess, rel)).st_size
        except IOError:
            missing += 1
    return total, missing


def existing_sizes(sess: Session, paths: List[str]) -> Dict[str, int]:
    """{path: bytes} for the paths that currently exist on disk."""
    out = {}
    for rel in paths:
        try:
            out[rel] = sess.sftp.stat(_remote(sess, rel)).st_size
        except IOError:
            pass
    return out


def delete_files(sess: Session, paths: List[str]) -> int:
    """Delete files; returns bytes freed. Missing files are ignored, other failures recorded."""
    freed = 0
    for rel in paths:
        full = _remote(sess, rel)
        try:
            size = sess.sftp.stat(full).st_size
        except IOError:
            continue
        try:
            sess.sftp.remove(full)
            freed += size
        except Exception as e:
            sess.error(f"Failed to remove {full}: {e}")
    return freed


# --------------------------------------------------------------------------- scan / apply

def scan(sess: Session, kinds: Set[str], progress: Callable[[str], None] = lambda msg: None,
         limit: int = 0, on_item: Optional[Callable[[Dict], None]] = None) -> List[Dict]:
    """Dry run. Returns plan items: {kind, id, label, group, paths, bytes, missing, [user_id]}.
    on_item(item) is called as each item is measured. Raise from progress/on_item to stop early."""
    items: List[Dict] = []

    def full() -> bool:
        return bool(limit) and len(items) >= limit

    def add(kind, item_id, label, group, paths, **extra):
        size, missing = measure_files(sess, paths)
        items.append({"kind": kind, "id": item_id, "label": label, "group": group, "paths": paths,
                      "bytes": size, "missing": missing, **extra})
        if on_item:
            on_item(items[-1])

    if "channel" in kinds:
        progress("Listing teams...")
        for team in mmctl_team_names(sess):
            for ch in mmctl_archived_channels(sess, team):
                if full(): break
                label = ch.get("display_name") or ch.get("name") or ch["id"]
                progress(f"Archived channel {team}: {label}")
                add("channel", ch["id"], label, team, channel_file_paths(sess, ch["id"]))

    if "dm" in kinds and not full():
        progress("Finding DMs of deactivated users...")
        for dm in deactivated_user_dms(sess):
            if full(): break
            label = f"DM {dm['username']} & {dm['other']}" if dm["other"] != dm["username"] else f"DM {dm['username']} (self)"
            progress(label)
            add("dm", dm["id"], label, "Direct messages", channel_file_paths(sess, dm["id"]), user_id=dm["user_id"])

    if "orphan" in kinds and not full():
        progress("Finding orphaned files...")
        for o in db_orphans(sess):
            if full(): break
            add("orphan", o["id"], o["name"] or o["id"], "Orphaned files", o["paths"])

    progress(f"Scan complete: {len(items)} items.")
    return items


def apply(sess: Session, items: List[Dict],
          progress: Callable[[int, int, str], None] = lambda i, n, label: None,
          on_item: Optional[Callable[[Dict, str, int, List[str]], None]] = None) -> Dict:
    """Delete exactly these plan items, in order. Items that changed since the scan are skipped.
    on_item(item, status, bytes_freed, new_errors) reports each item; status is
    'deleted', 'skipped' or 'failed' (failed = any error while handling it, even if some files went)."""
    deleted, skipped, freed = 0, 0, 0
    n = len(items)
    for i, it in enumerate(items):
        progress(i, n, it["label"])
        errs_before, item_freed, status = len(sess.errors), 0, "deleted"
        try:
            if not still_eligible(sess, it):
                status = "skipped"
                logger.warning(f"Skipped {it['kind']} {it['id']}: changed since scan")
            elif it["kind"] == "orphan":
                item_freed = delete_files(sess, it["paths"])
                f = "fileinfo"
                sess.cur.execute(f"DELETE FROM {sess.q(f)} fi WHERE fi.{sess.col(f, 'Id')} = %s "
                                 f"AND {_orphan_sql(sess)}", (it["id"],))
            else:
                # Mattermost's own delete runs first: if it fails, nothing of ours is gone yet.
                before = existing_sizes(sess, it["paths"])
                if it["kind"] == "channel":
                    sess.mmctl("channel", "delete", it["id"], "--confirm")
                else:  # Mattermost can't delete DM channels; permanently delete every post (and its files) instead
                    posts = channel_post_ids(sess, it["id"])
                    for i in range(0, len(posts), 100):
                        sess.mmctl("post", "delete", *posts[i:i + 100], "--permanent", "--confirm")
                delete_files(sess, it["paths"])  # whatever Mattermost left on disk
                after = existing_sizes(sess, list(before))
                item_freed = sum(size for path, size in before.items() if path not in after)
        except Exception as e:
            sess.error(f"Failed to purge {it['kind']} {it['label']} ({it['id']}): {e}")
        new_errors = sess.errors[errs_before:]
        if status != "skipped" and new_errors:
            status = "failed"
        freed += item_freed
        deleted += status == "deleted"
        skipped += status == "skipped"
        if on_item:
            on_item(it, status, item_freed, new_errors)
    progress(n, n, "")
    return {"deleted": deleted, "skipped": skipped, "bytes": freed}


def vacuum(sess: Session) -> None:
    """VACUUM FULL on FileInfo. Takes an exclusive lock; maintenance windows only."""
    try:
        sess.cur.execute(f"VACUUM FULL ANALYZE {sess.q('fileinfo')}")
    except Exception as e:
        sess.error(f"Vacuum failed: {e}")


# --------------------------------------------------------------------------- CLI

def print_summary_table(items: List[Dict], mode: str, reclaimed: int, total_storage: int):
    print("\n" + "=" * 80)
    print(f" BINDAY REPORT ({mode})")
    print("=" * 80)
    print(f"{'CHANNEL / USER DM / ORPHAN':<50} | {'FILES':<8} | {'SIZE':<10}")
    print("-" * 80)
    for it in items:
        display = f"{it['group']}: {it['label']}" if it["kind"] == "channel" else f"{KIND_LABELS[it['kind']]}: {it['label']}"
        if len(display) > 48: display = display[:45] + "..."
        print(f"{display:<50} | {len(it['paths']):<8} | {format_bytes(it['bytes']):<10}")
    print("-" * 80)
    print(f"{'RECLAIMED SPACE' if mode == 'EXECUTE' else 'RECLAIMABLE SPACE':<50} | {'':<8} | {format_bytes(reclaimed):<10}")
    print("-" * 80)
    print(f"{'CURRENT TOTAL STORAGE USAGE':<50} | {'':<8} | {format_bytes(total_storage):<10}")
    print("=" * 80 + "\n")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        handlers=[logging.FileHandler("binday.log", mode="w"), logging.StreamHandler(sys.stdout)])
    ap = argparse.ArgumentParser()
    ap.add_argument("--mm-server", default=None)
    ap.add_argument("--mm-token", default=None)
    ap.add_argument("--mm-profile", default="local")
    ap.add_argument("--mm-container", default="mattermost", help="Docker container running Mattermost/mmctl")
    ap.add_argument("--ssh", required=True, help="user@host[:port]")
    ap.add_argument("--ssh-password", default=None, help="omit to use SSH keys / agent")
    ap.add_argument("--ssh-key", default=None, help="private key file (default: ~/.ssh keys and agent)")
    ap.add_argument("--pg-dsn", required=True)
    ap.add_argument("--db-via-ssh", action="store_true", help="tunnel the Postgres connection through SSH")
    ap.add_argument("--remote-data-dir", required=True)
    ap.add_argument("--purge-archived-channels", action="store_true")
    ap.add_argument("--purge-deactivated-users", action="store_true")
    ap.add_argument("--purge-db-orphans", action="store_true")
    ap.add_argument("--vacuum", action="store_true", help="Run VACUUM FULL on FileInfo table (requires --execute)")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    kinds = {k for k, on in (("channel", args.purge_archived_channels), ("dm", args.purge_deactivated_users),
                             ("orphan", args.purge_db_orphans)) if on}
    if not kinds and not args.vacuum:
        ap.error("nothing to do: pass at least one of --purge-archived-channels, --purge-deactivated-users, "
                 "--purge-db-orphans, --vacuum")

    mode = "EXECUTE" if args.execute else "DRY-RUN"
    print(f"Starting... Mode: {mode}")
    sess = Session(Settings(ssh=args.ssh, ssh_password=args.ssh_password, ssh_key=args.ssh_key,
                            pg=psycopg2.extensions.parse_dsn(args.pg_dsn), db_via_ssh=args.db_via_ssh,
                            remote_data_dir=args.remote_data_dir, container=args.mm_container,
                            mm_server=args.mm_server, mm_token=args.mm_token, mm_profile=args.mm_profile))
    try:
        if "channel" in kinds and channel_deletion_enabled(sess) is not True:
            logger.error("ServiceSettings.EnableAPIChannelDeletion is false.")
            if args.execute: sys.exit(1)

        items = scan(sess, kinds, progress=lambda m: print(".", end="", flush=True), limit=args.limit)
        reclaimed = sum(it["bytes"] for it in items)
        if args.execute:
            reclaimed = apply(sess, items)["bytes"]
        if args.vacuum:
            if args.execute:
                print("\n[Maintenance] Running VACUUM FULL on FileInfo... (this locks the table)")
                vacuum(sess)
            else:
                print("\n[Maintenance] Dry-Run: Skipping VACUUM FULL.")
        print("\nCalculating total storage usage...")
        total = storage_usage(sess)
    finally:
        sess.close()
    print_summary_table(items, mode, reclaimed, total)
    if sess.errors:
        print(f"{len(sess.errors)} error(s) occurred - see binday.log")
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
