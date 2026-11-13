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
  deleted - message a user deleted (Mattermost only marks it) -> `mmctl post delete --permanent`
            (Mattermost removes its files and FileInfo rows itself)
  orphan  - FileInfo row whose post is gone, or an attachment removed by an edit that no
            version of the message still uses -> delete files, then the FileInfo row
  upload  - resumable upload abandoned for over a day -> delete the partial file and its UploadSessions row
  untracked - file in an attachment folder that no FileInfo or upload row points at -> delete it
  emoji   - image Mattermost kept (renamed image_deleted) when a custom emoji was deleted -> delete it
Database tidy (no files; the space comes back with VACUUM):
  history - old versions of edited messages (Posts rows with an OriginalId)
  jobs    - finished Jobs rows older than 30 days (the same query as Mattermost's own, off-by-default, cleanup)
  links   - link-preview cache (LinkMetadata) for messages older than 30 days; Mattermost refetches on a miss
"""

import argparse
import json
import logging
import shlex
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Set

import paramiko
import psycopg2
import psycopg2.extensions

logger = logging.getLogger("binday")

KINDS = ("channel", "dm", "deleted", "orphan", "upload", "untracked", "emoji", "history", "jobs", "links")
KIND_LABELS = {"channel": "Archived channel", "dm": "Deactivated user DM", "deleted": "Deleted message",
               "orphan": "Orphaned file", "upload": "Abandoned upload", "untracked": "Untracked file",
               "emoji": "Deleted emoji image", "history": "Edit history", "jobs": "Old job records",
               "links": "Link preview cache"}
TIDY = {"history", "jobs", "links"}  # database rows only, no files
DAY_MS = 86_400_000
STALE_MS = DAY_MS        # uploads and untracked files younger than this may still be in flight
KEEP_MS = 30 * DAY_MS    # job records and link previews newer than this are kept
OPTIONAL_TABLES = ("uploadsessions", "emoji", "jobs", "linkmetadata")


def _now_ms() -> int:
    return int(time.time() * 1000)


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
    def __init__(self, settings: Settings, parts=("ssh", "db", "mmctl")):
        """Connect the given parts. A DB reached through SSH connects SSH too."""
        self.s = settings
        self.errors: List[str] = []
        self.ssh = self.sftp = self.conn = self.cur = self._tunnel = None
        self._cols: Dict[str, List[str]] = {}
        try:
            if "ssh" in parts or "mmctl" in parts or ("db" in parts and settings.db_via_ssh):
                self._connect_ssh()
            if "db" in parts:
                self._connect_db()
            if "mmctl" in parts and settings.mm_server and settings.mm_token:
                self.login_mmctl(allow_fail=True)
        except Exception:
            self.close()
            raise

    def _connect_ssh(self):
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

    def _connect_db(self):
        s = self.s
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

    def login_mmctl(self, allow_fail=False):
        s = self.s
        # mmctl takes the server URL as a positional argument (there is no --server flag)
        return self.mmctl("auth", "login", s.mm_server, "--name", s.mm_profile,
                          "--access-token", s.mm_token, allow_fail=allow_fail)

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
                "WHERE table_type='BASE TABLE' AND lower(table_name) IN %s", (tuple(wanted) + OPTIONAL_TABLES,))
    by_schema: Dict[str, Dict[str, str]] = {}
    for schema, name in cur.fetchall():
        by_schema.setdefault(schema, {})[name.lower()] = name
    candidates = [sch for sch, t in by_schema.items() if wanted.issubset(t)]
    if not candidates:
        raise RuntimeError("Could not find Mattermost tables (FileInfo, Posts, Channels, Users).")
    schema = "public" if "public" in candidates else candidates[0]
    return schema, by_schema[schema]


# --------------------------------------------------------------------------- connection tests
# Each returns check rows (ok, title, detail): ok is True, False (failed) or None (works, with a warning).

def _short(e: Exception) -> str:
    msg = str(e).strip() or type(e).__name__
    if "FATAL:" in msg:  # PostgreSQL puts the reason after FATAL:, behind a long "connection to ... failed" prefix
        return msg.split("FATAL:", 1)[1].strip().splitlines()[0]
    lines = [l.strip() for l in msg.splitlines() if l.strip()]
    for l in lines:  # prefer mmctl's / the remote command's own message over the command line
        if l.startswith(("Error:", "STDERR:", "FATAL:", "*")):
            return l.split(":", 1)[-1].strip() if l.startswith("STDERR:") else l
    return lines[0]


def test_server(s: Settings) -> List[tuple]:
    """SSH login, data directory, Docker container and mmctl inside it."""
    try:
        sess = Session(s, parts=("ssh",))
    except Exception as e:
        return [(False, "SSH failed", _short(e))]
    rows = [(True, "SSH connected", f"{s.ssh} · {'password' if s.ssh_password else 'key file' if s.ssh_key else 'key from agent'}")]
    try:
        found = sess.run(["sh", "-c", f"test -d {shlex.quote(s.remote_data_dir)} && echo yes"], allow_fail=True) == "yes"
        rows.append((True, "Data directory found", f"{format_bytes(storage_usage(sess))} in use") if found else
                    (False, "Data directory not found", s.remote_data_dir))
        running = sess.run(["docker", "inspect", "-f", "{{.State.Running}}", s.container], allow_fail=True)
        if running != "true":
            rows.append((False, f"Container “{s.container}” isn't running",
                         "Check the name with `docker ps` on the server" if not running else running))
        else:
            rows.append((True, "Docker container running", s.container))
            version = sess.mmctl("version", allow_fail=True)
            vline = next((l.strip() for l in version.splitlines() if "Version" in l), version.splitlines()[0] if version else "")
            if not version:
                rows.append((False, "mmctl not found", f"No mmctl inside “{s.container}”"))
            else:
                try:  # installed isn't enough: it must be able to talk to Mattermost (local mode or a saved login)
                    rows.append((True, "mmctl works", f"{len(mmctl_team_names(sess))} teams · {' '.join(vline.split())[:60]}"))
                except Exception:
                    rows.append((None, "mmctl installed but not logged in",
                                 "Fill in mmctl login and test it, or turn on local mode in the container"))
    except Exception as e:
        rows.append((False, "Server check failed", _short(e)))
    finally:
        sess.close()
    return rows


def test_database(s: Settings) -> List[tuple]:
    """PostgreSQL connection (through SSH when set) and that it holds Mattermost's tables."""
    try:
        sess = Session(s, parts=("db",))
    except Exception as e:
        where = " through SSH" if s.db_via_ssh else ""
        return [(False, f"Couldn't reach the database{where}", _short(e))]
    try:
        sess.cur.execute(f"SELECT count(*) FROM {sess.q('users')}")
        users = sess.cur.fetchone()[0]
        return [(True, "Database reachable", f"{s.pg.get('dbname')} · schema {sess.schema}" + (" · via SSH" if s.db_via_ssh else "")),
                (True, "Mattermost tables found", f"{users:,} users")]
    except Exception as e:
        return [(False, "Database check failed", _short(e))]
    finally:
        sess.close()


def test_mmctl(s: Settings) -> List[tuple]:
    """Log mmctl in with the URL and token (if given), then make a real call."""
    try:
        sess = Session(s, parts=("ssh",))
    except Exception as e:
        return [(False, "SSH failed", _short(e))]
    rows = []
    try:
        if s.mm_server and s.mm_token:
            try:
                sess.login_mmctl()
                rows.append((True, "mmctl logged in", s.mm_server))
            except Exception as e:
                return [(False, "mmctl login failed", _short(e))]
        try:
            teams = mmctl_team_names(sess)
        except Exception as e:
            hint = "Enter the server URL and an access token" if not (s.mm_server and s.mm_token) else _short(e)
            return rows + [(False, "mmctl can't talk to Mattermost", hint)]
        if not rows:
            rows.append((True, "No login needed", "mmctl in the container already works"))
        rows.append((True, "mmctl works", f"{len(teams)} teams"))
        enabled = channel_deletion_enabled(sess)
        rows.append((True, "Channel deletion is on", "EnableAPIChannelDeletion = true") if enabled else
                    (None, "Channel deletion is off", "Archived channels can be scanned but not deleted"))
    finally:
        sess.close()
    return rows


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
    # Replies first: permanently deleting a thread root also drops its replies' rows but not their files.
    sess.cur.execute(f"SELECT {sess.col(p, 'Id')} FROM {sess.q(p)} WHERE {sess.col(p, 'ChannelId')} = %s "
                     f"ORDER BY {sess.col(p, 'RootId')} = '' , {sess.col(p, 'CreateAt')}", (channel_id,))
    return [r[0] for r in sess.cur.fetchall()]


def _orphan_sql(sess: Session) -> str:
    f, p = "fileinfo", "posts"
    pid, pq, pcol = sess.col(f, "PostId"), sess.q(p), lambda c: sess.col(p, c)
    fid = f"fi.{sess.col(f, 'Id')}"
    # Empty/NULL PostId = uploaded but not yet attached to a post (e.g. a draft); not an orphan.
    # An attachment removed by an edit is only marked deleted, and restoring an older version of the
    # message brings it back, so it counts only once no version (live or history) lists it any more.
    return (f"fi.{pid} IS NOT NULL AND fi.{pid} <> '' AND ("
            f"NOT EXISTS (SELECT 1 FROM {pq} p WHERE p.{pcol('Id')} = fi.{pid}) OR ("
            f"fi.{sess.col(f, 'DeleteAt')} > 0 "
            f"AND EXISTS (SELECT 1 FROM {pq} p WHERE p.{pcol('Id')} = fi.{pid} AND p.{pcol('DeleteAt')} = 0 "
            f"AND position({fid} in p.{pcol('FileIds')}) = 0) "
            f"AND NOT EXISTS (SELECT 1 FROM {pq} h WHERE h.{pcol('OriginalId')} = fi.{pid} "
            f"AND position({fid} in h.{pcol('FileIds')}) > 0)))")


def db_orphans(sess: Session) -> List[Dict]:
    f = "fileinfo"
    sess.cur.execute(
        f"SELECT fi.{sess.col(f, 'Id')}, fi.{sess.col(f, 'Name')}, fi.{sess.col(f, 'Path')}, "
        f"fi.{sess.col(f, 'ThumbnailPath')}, fi.{sess.col(f, 'PreviewPath')} "
        f"FROM {sess.q(f)} fi WHERE {_orphan_sql(sess)}")
    return [{"id": r[0], "name": r[1], "paths": [x for x in r[2:] if x]} for r in sess.cur.fetchall()]


def _deleted_post_sql(sess: Session) -> str:
    p = "posts"
    return f"p.{sess.col(p, 'DeleteAt')} > 0 AND coalesce(p.{sess.col(p, 'OriginalId')}, '') = ''"


def deleted_posts(sess: Session, skip_channels: Set[str]) -> List[Dict]:
    """Messages users deleted (Mattermost keeps the row and every file). Not edit-history rows.
    Replies come before roots, so a root's permanent delete never takes a reply's row first."""
    p, f, c = "posts", "fileinfo", "channels"
    sess.cur.execute(
        f"SELECT p.{sess.col(p, 'Id')}, p.{sess.col(p, 'ChannelId')}, coalesce(ch.{sess.col(c, 'DisplayName')}, ''), "
        f"fi.{sess.col(f, 'Path')}, fi.{sess.col(f, 'ThumbnailPath')}, fi.{sess.col(f, 'PreviewPath')} "
        f"FROM {sess.q(p)} p LEFT JOIN {sess.q(f)} fi ON fi.{sess.col(f, 'PostId')} = p.{sess.col(p, 'Id')} "
        f"LEFT JOIN {sess.q(c)} ch ON ch.{sess.col(c, 'Id')} = p.{sess.col(p, 'ChannelId')} "
        f"WHERE {_deleted_post_sql(sess)} "
        f"ORDER BY p.{sess.col(p, 'RootId')} = '', p.{sess.col(p, 'CreateAt')}")
    posts: Dict[str, Dict] = {}
    for post_id, channel_id, channel, *paths in sess.cur.fetchall():
        if channel_id in skip_channels:
            continue
        it = posts.setdefault(post_id, {"id": post_id, "channel": channel or "Direct message", "paths": []})
        it["paths"] += [x for x in paths if x]
    return list(posts.values())


def abandoned_uploads(sess: Session) -> List[Dict]:
    """Resumable attachment uploads never finished; Mattermost never removes these."""
    if "uploadsessions" not in sess.t:
        return []
    u = "uploadsessions"
    sess.cur.execute(f"SELECT {sess.col(u, 'Id')}, {sess.col(u, 'Filename')}, {sess.col(u, 'Path')} FROM {sess.q(u)} "
                     f"WHERE {sess.col(u, 'Type')} = 'attachment' AND {sess.col(u, 'CreateAt')} < %s",
                     (_now_ms() - STALE_MS,))
    out = []
    for upload_id, name, path in sess.cur.fetchall():
        if path:  # incomplete parts may be written as <path>.tmp (model.IncompleteUploadSuffix): use whichever exists
            on_disk = list(existing_sizes(sess, [path, path + ".tmp"])) or [path]
            out.append({"id": upload_id, "name": name, "paths": on_disk})
    return out


def _known_paths(sess: Session) -> Set[str]:
    f = "fileinfo"
    sess.cur.execute(f"SELECT {sess.col(f, 'Path')}, {sess.col(f, 'ThumbnailPath')}, {sess.col(f, 'PreviewPath')} FROM {sess.q(f)}")
    known = {x for row in sess.cur.fetchall() for x in row if x}
    if "uploadsessions" in sess.t:
        u = "uploadsessions"
        sess.cur.execute(f"SELECT {sess.col(u, 'Path')} FROM {sess.q(u)}")
        for (path,) in sess.cur.fetchall():
            if path:
                known |= {path, path + ".tmp"}
    return known


def untracked_files(sess: Session, progress: Callable[[str], None]) -> List[Dict]:
    """Files in the dated attachment folders (YYYYMMDD/...) that no FileInfo or UploadSessions row
    points at, untouched for a day. Other top-level folders (users, teams, brand, emoji, plugins,
    bookmark, export, import) belong to Mattermost and are never looked at."""
    import stat as st
    known, cutoff, out = _known_paths(sess), time.time() - STALE_MS / 1000, []
    root = sess.s.remote_data_dir.rstrip("/")

    def walk(rel):
        for a in sess.sftp.listdir_attr(f"{root}/{rel}"):
            child = f"{rel}/{a.filename}"
            if st.S_ISDIR(a.st_mode):
                walk(child)
            elif st.S_ISREG(a.st_mode) and child not in known and (a.st_mtime or 0) < cutoff:
                out.append({"id": child, "name": a.filename, "paths": [child], "size": a.st_size})

    for a in sorted(sess.sftp.listdir_attr(root), key=lambda a: a.filename):
        if st.S_ISDIR(a.st_mode) and len(a.filename) == 8 and a.filename.isdigit():
            progress(f"Checking {a.filename}/ for untracked files...")
            walk(a.filename)
    return out


def deleted_emoji_images(sess: Session) -> List[Dict]:
    if "emoji" not in sess.t:
        return []
    e = "emoji"
    sess.cur.execute(f"SELECT {sess.col(e, 'Id')}, {sess.col(e, 'Name')} FROM {sess.q(e)} WHERE {sess.col(e, 'DeleteAt')} > 0")
    return [{"id": r[0], "name": r[1], "paths": [f"emoji/{r[0]}/image_deleted"]} for r in sess.cur.fetchall()]


def _tidy_where(sess: Session, kind: str, now: int):
    """(logical table, WHERE clause, params) for a database-tidy kind, fixed at scan time `now`."""
    if kind == "history":
        p = "posts"
        return p, (f"coalesce({sess.col(p, 'OriginalId')}, '') <> '' AND {sess.col(p, 'DeleteAt')} > 0 "
                   f"AND {sess.col(p, 'DeleteAt')} <= %s"), (now,)
    if kind == "jobs":  # Mattermost's own SqlJobStore.Cleanup query
        j = "jobs"
        return j, f"{sess.col(j, 'CreateAt')} < %s AND {sess.col(j, 'Status')} NOT IN ('in_progress', 'pending')", (now - KEEP_MS,)
    lm = "linkmetadata"
    return lm, f"{sess.col(lm, 'Timestamp')} < %s", (now - KEEP_MS,)


def tidy_item(sess: Session, kind: str) -> Optional[Dict]:
    """One plan item per database tidy: how many rows, and roughly how much table space they hold."""
    now = _now_ms()
    table, where, params = _tidy_where(sess, kind, now)
    if table not in sess.t:
        return None
    sess.cur.execute(f"SELECT count(*) FROM {sess.q(table)} WHERE {where}", params)
    rows = sess.cur.fetchone()[0]
    if not rows:
        return None
    sess.cur.execute(f"SELECT pg_total_relation_size(%s::regclass), greatest(count(*), 1) FROM {sess.q(table)}", (sess.q(table),))
    size, total = sess.cur.fetchone()
    return {"kind": kind, "id": kind, "label": f"{rows:,} rows", "group": f"{sess.t[table]} table", "paths": [],
            "bytes": 0, "db_bytes": int(size * rows / total), "missing": 0, "rows": rows, "at": now}


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
    kind = item["kind"]
    if kind == "deleted":
        sess.cur.execute(f"SELECT 1 FROM {sess.q('posts')} p WHERE p.{sess.col('posts', 'Id')} = %s AND {_deleted_post_sql(sess)}",
                         (item["id"],))
        return sess.cur.fetchone() is not None
    if kind == "upload":
        u = "uploadsessions"
        sess.cur.execute(f"SELECT 1 FROM {sess.q(u)} WHERE {sess.col(u, 'Id')} = %s AND {sess.col(u, 'Type')} = 'attachment' "
                         f"AND {sess.col(u, 'CreateAt')} < %s", (item["id"], _now_ms() - STALE_MS))
        return sess.cur.fetchone() is not None
    if kind == "untracked":
        if item["id"] in _known_paths(sess):
            return False
        try:
            return (sess.sftp.stat(_remote(sess, item["id"])).st_mtime or 0) < time.time() - STALE_MS / 1000
        except IOError:
            return False
    if kind == "emoji":
        e = "emoji"
        sess.cur.execute(f"SELECT 1 FROM {sess.q(e)} WHERE {sess.col(e, 'Id')} = %s AND {sess.col(e, 'DeleteAt')} > 0", (item["id"],))
        return sess.cur.fetchone() is not None
    if kind in TIDY:
        return _tidy_where(sess, kind, item["at"])[0] in sess.t
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
    covered: Set[str] = set()  # channel ids whose posts are already in the plan

    def full() -> bool:
        return bool(limit) and len(items) >= limit

    def add(kind, item_id, label, group, paths, size=None, **extra):
        size, missing = (size, 0) if size is not None else measure_files(sess, paths)
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
                covered.add(ch["id"])

    if "dm" in kinds and not full():
        progress("Finding DMs of deactivated users...")
        for dm in deactivated_user_dms(sess):
            if full(): break
            if not channel_post_ids(sess, dm["id"]):
                continue  # already emptied: Mattermost keeps the empty DM channel, nothing left to collect
            label = f"DM {dm['username']} & {dm['other']}" if dm["other"] != dm["username"] else f"DM {dm['username']} (self)"
            progress(label)
            add("dm", dm["id"], label, "Direct messages", channel_file_paths(sess, dm["id"]), user_id=dm["user_id"])
            covered.add(dm["id"])

    if "deleted" in kinds and not full():
        progress("Finding deleted messages...")
        for d in deleted_posts(sess, covered):  # posts in channels/DMs picked above go with them
            if full(): break
            add("deleted", d["id"], f"in {d['channel']}", "Deleted messages", d["paths"])

    if "orphan" in kinds and not full():
        progress("Finding orphaned files...")
        for o in db_orphans(sess):
            if full(): break
            add("orphan", o["id"], o["name"] or o["id"], "Orphaned files", o["paths"])

    if "upload" in kinds and not full():
        progress("Finding abandoned uploads...")
        for u in abandoned_uploads(sess):
            if full(): break
            add("upload", u["id"], u["name"] or u["id"], "Abandoned uploads", u["paths"])

    if "untracked" in kinds and not full():
        progress("Finding untracked files...")
        for u in untracked_files(sess, progress):
            if full(): break
            add("untracked", u["id"], u["name"], "Untracked files", u["paths"], size=u["size"])

    if "emoji" in kinds and not full():
        progress("Finding deleted emoji images...")
        for e in deleted_emoji_images(sess):
            if full(): break
            size, missing = measure_files(sess, e["paths"])
            if size:  # only deletions that left an image behind
                add("emoji", e["id"], f":{e['name']}:", "Deleted emoji", e["paths"], size=size)

    for kind in ("history", "jobs", "links"):
        if kind in kinds and not full():
            progress(f"Counting {KIND_LABELS[kind].lower()}...")
            it = tidy_item(sess, kind)
            if it:
                items.append(it)
                if on_item:
                    on_item(it)

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
            elif it["kind"] == "upload":
                item_freed = delete_files(sess, it["paths"])
                u = "uploadsessions"
                sess.cur.execute(f"DELETE FROM {sess.q(u)} WHERE {sess.col(u, 'Id')} = %s", (it["id"],))
            elif it["kind"] in ("untracked", "emoji"):
                item_freed = delete_files(sess, it["paths"])
            elif it["kind"] in TIDY:
                table, where, params = _tidy_where(sess, it["kind"], it["at"])
                sess.cur.execute(f"DELETE FROM {sess.q(table)} WHERE {where}", params)
                it["rows_deleted"] = sess.cur.rowcount
                item_freed = 0  # table space only comes back with VACUUM
            else:
                # Mattermost's own delete runs first: if it fails, nothing of ours is gone yet.
                before = existing_sizes(sess, it["paths"])
                if it["kind"] == "channel":
                    sess.mmctl("channel", "delete", it["id"], "--confirm")
                elif it["kind"] == "deleted":
                    # ponytail: one mmctl call per message; batch ids if servers with many thousands get slow
                    sess.mmctl("post", "delete", it["id"], "--permanent", "--confirm")
                else:  # Mattermost can't delete DM channels; permanently delete every post (and its files) instead
                    posts = channel_post_ids(sess, it["id"])
                    for i in range(0, len(posts), 100):
                        sess.mmctl("post", "delete", *posts[i:i + 100], "--permanent", "--confirm")
                delete_files(sess, it["paths"])  # whatever Mattermost left on disk
                if it["kind"] == "channel" and it["paths"]:  # Mattermost's channel delete keeps the FileInfo rows
                    f = "fileinfo"
                    sess.cur.execute(f"DELETE FROM {sess.q(f)} fi WHERE fi.{sess.col(f, 'Path')} = ANY(%s) "
                                     f"AND {_orphan_sql(sess)}", (it["paths"],))
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


VACUUM_TABLES = ("fileinfo", "posts", "uploadsessions", "jobs", "linkmetadata")


def vacuum(sess: Session) -> None:
    """VACUUM FULL on the tables Binday deletes from. Takes exclusive locks; maintenance windows only."""
    for table in VACUUM_TABLES:
        if table in sess.t:
            try:
                sess.cur.execute(f"VACUUM FULL ANALYZE {sess.q(table)}")
            except Exception as e:
                sess.error(f"Vacuum of {sess.t[table]} failed: {e}")


# --------------------------------------------------------------------------- CLI

def print_summary_table(items: List[Dict], mode: str, reclaimed: int, total_storage: int):
    print("\n" + "=" * 80)
    print(f" BINDAY REPORT ({mode})")
    print("=" * 80)
    print(f"{'ITEM':<50} | {'FILES':<8} | {'SIZE':<10}")
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
    ap.add_argument("--purge-deleted-posts", action="store_true", help="messages users deleted, and their files")
    ap.add_argument("--purge-abandoned-uploads", action="store_true", help="unfinished uploads older than a day")
    ap.add_argument("--purge-untracked-files", action="store_true", help="attachment files no database row points at")
    ap.add_argument("--purge-deleted-emoji", action="store_true", help="images left by deleted custom emoji")
    ap.add_argument("--tidy-edit-history", action="store_true", help="old versions of edited messages")
    ap.add_argument("--tidy-jobs", action="store_true", help="finished job records older than 30 days")
    ap.add_argument("--tidy-link-previews", action="store_true", help="link preview cache for messages older than 30 days")
    ap.add_argument("--vacuum", action="store_true", help="VACUUM FULL the tables Binday deletes from (requires --execute)")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    flags = {"channel": "purge_archived_channels", "dm": "purge_deactivated_users", "deleted": "purge_deleted_posts",
             "orphan": "purge_db_orphans", "upload": "purge_abandoned_uploads", "untracked": "purge_untracked_files",
             "emoji": "purge_deleted_emoji", "history": "tidy_edit_history", "jobs": "tidy_jobs", "links": "tidy_link_previews"}
    kinds = {k for k, a in flags.items() if getattr(args, a)}
    if not kinds and not args.vacuum:
        ap.error("nothing to do: pass at least one --purge-*/--tidy-* flag or --vacuum")

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
                print("\n[Maintenance] Running VACUUM FULL... (this locks the tables)")
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
