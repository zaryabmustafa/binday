"""Seed the local test server with data the purge tool should (and should NOT) delete.

    python testenv/seed.py            (from Windows, after `wsl sh testenv/setup.sh`)

Uses the REST API on http://localhost:8065 plus mmctl/psql through `wsl docker exec`.
Generated test users/passwords and every created id go to testenv/.seed.json (gitignored),
which verify.py reads. Run once per fresh server (setup.sh --reset wipes it).
"""
import json
import os
import secrets
import struct
import subprocess
import sys
import urllib.request
import uuid
import zlib
from pathlib import Path

HERE = Path(__file__).parent
API = "http://127.0.0.1:8065/api/v4"  # not "localhost": it may resolve to ::1, and the port is bound to IPv4 only
PG = "mm-purge-test-postgres-1"


def sh(*cmd) -> str:
    return subprocess.run(["wsl", "-e", *cmd], capture_output=True, text=True, check=True).stdout.strip()


def mmctl(*args) -> str:
    return sh("docker", "exec", "mattermost", "mmctl", *args)


def psql(sql: str) -> str:
    return sh("docker", "exec", PG, "psql", "-U", "mmuser", "-d", "mattermost", "-At", "-c", sql)


def call(method, path, token=None, body=None, raw=None, ctype="application/json"):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(API + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", ctype)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r, (json.loads(r.read() or b"null"))


def login(username, password) -> str:
    r, _ = call("POST", "/users/login", body={"login_id": username, "password": password})
    return r.headers["Token"]


def png(size: int) -> bytes:
    """A valid noisy PNG (so Mattermost makes a thumbnail and preview)."""
    raw = b"".join(b"\x00" + os.urandom(size * 3) for _ in range(size))
    def chunk(t, d): return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)) + \
        chunk(b"IDAT", zlib.compress(raw, 1)) + chunk(b"IEND", b"")


def upload(token, channel_id, name, content: bytes) -> str:
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"channel_id\"\r\n\r\n{channel_id}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"{name}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n").encode() + content + f"\r\n--{boundary}--\r\n".encode()
    _, res = call("POST", "/files", token, raw=body, ctype=f"multipart/form-data; boundary={boundary}")
    return res["file_infos"][0]["id"]


def post_files(token, channel_id, message, files) -> dict:
    ids = [upload(token, channel_id, n, c) for n, c in files]
    _, p = call("POST", "/posts", token, body={"channel_id": channel_id, "message": message, "file_ids": ids})
    return {"post_id": p["id"], "file_ids": ids}


def some_files(n, tag):
    out = []
    for i in range(n):
        out.append((f"{tag}-{i}.png", png(180 + 20 * i)) if i % 2 == 0 else (f"{tag}-{i}.bin", os.urandom(300_000 + 150_000 * i)))
    return out


def main():
    out = HERE / ".seed.json"
    if out.exists() and "--force" not in sys.argv:
        sys.exit(f"{out} exists: this server is already seeded (reset with `sh testenv/setup.sh --reset`, or pass --force)")

    users = {u: secrets.token_urlsafe(12) + "aA1!" for u in ("purgeadmin", "alice", "dave", "bob", "carol")}
    for u, pw in users.items():
        mmctl("user", "create", "--email", f"{u}@test.local", "--username", u, "--password", pw,
              *(["--system-admin"] if u == "purgeadmin" else []))
    mmctl("team", "create", "--name", "eng", "--display-name", "Engineering")
    mmctl("team", "create", "--name", "ops", "--display-name", "Operations")
    for team in ("eng", "ops"):
        mmctl("team", "users", "add", team, *users)

    admin, alice = login("purgeadmin", users["purgeadmin"]), login("alice", users["alice"])
    _, me = call("GET", "/users/me", admin)
    ids = {u: call("GET", f"/users/username/{u}", admin)[1]["id"] for u in users}
    team_id = {t: call("GET", f"/teams/name/{t}", admin)[1]["id"] for t in ("eng", "ops")}

    def channel(team, name):
        _, c = call("POST", "/channels", admin, body={"team_id": team_id[team], "name": name, "display_name": name, "type": "O"})
        return c["id"]

    seed = {"users": users, "user_ids": ids, "channels": {}, "dms": {}, "orphans": [], "unattached": None, "keep_files": []}

    # channels: archived ones are purge targets; active ones must survive
    for team, name, n, archive in (("eng", "old-project", 4, True), ("eng", "payroll-2023", 3, True),
                                   ("ops", "empty-archive", 0, True), ("eng", "keep-me", 3, False)):
        cid = channel(team, name)
        files = post_files(admin, cid, f"files for {name}", some_files(n, name)) if n else {"file_ids": []}
        if archive:
            call("DELETE", f"/channels/{cid}", admin)  # archive (soft delete)
        else:
            seed["keep_files"] += files["file_ids"]
        seed["channels"][name] = {"id": cid, "team": team, "archived": archive, "file_ids": files["file_ids"]}

    # DMs: with bob/carol (to be deactivated) are targets; with dave (active) must survive
    for owner_token, me_name, other, n in ((admin, "purgeadmin", "bob", 3), (admin, "purgeadmin", "carol", 2),
                                           (alice, "alice", "bob", 2), (admin, "purgeadmin", "dave", 2)):
        _, dm = call("POST", "/channels/direct", owner_token, body=[ids[me_name], ids[other]])
        files = post_files(owner_token, dm["id"], f"dm {me_name}-{other}", some_files(n, f"dm-{me_name}-{other}"))
        seed["dms"][f"{me_name}-{other}"] = {"id": dm["id"], "target": other in ("bob", "carol"), "file_ids": files["file_ids"]}
        if other == "dave":
            seed["keep_files"] += files["file_ids"]
    for u in ("bob", "carol"):
        mmctl("user", "deactivate", u)

    # orphans: posts removed straight from the DB leave their FileInfo rows and files behind
    town = call("GET", f"/teams/{team_id['eng']}/channels/name/town-square", admin)[1]["id"]
    for i in range(2):
        p = post_files(admin, town, f"soon orphaned {i}", some_files(2, f"orphan{i}"))
        psql(f"DELETE FROM posts WHERE id = '{p['post_id']}'")
        seed["orphans"] += p["file_ids"]
    # unattached upload (like a draft): PostId is empty, must NOT be treated as an orphan
    seed["unattached"] = upload(admin, town, "draft-upload.png", png(200))
    seed["keep_files"] += [seed["unattached"]]

    # ---- the newer bins (each mirrors Mattermost 12.1 behaviour checked in its source)
    day = 86_400_000
    now = int(psql("SELECT (extract(epoch from now()) * 1000)::bigint"))
    # deleted message, edited once first: Mattermost keeps the row, its files and its edit history
    d = post_files(admin, town, "to be deleted v1", some_files(2, "deleted"))
    call("PUT", f"/posts/{d['post_id']}/patch", admin, body={"message": "to be deleted v2"})
    call("DELETE", f"/posts/{d['post_id']}", admin)
    seed["deleted_post"] = d
    # an edit that drops an attachment: the dropped file is only marked deleted, and restoring the
    # older version brings it back, so it must survive while that history exists
    e = post_files(admin, town, "edited", some_files(2, "edited"))
    call("PUT", f"/posts/{e['post_id']}/patch", admin, body={"file_ids": e["file_ids"][:1]})
    seed["edited_post"] = e
    seed["keep_files"] += e["file_ids"]
    # a thread in a DM with a deactivated user: the reply's file must go too (replies are deleted first)
    dm = seed["dms"]["purgeadmin-carol"]["id"]
    root = call("POST", "/posts", admin, body={"channel_id": dm, "message": "thread root"})[1]
    rf = upload(admin, dm, "dm-reply.png", png(150))
    call("POST", "/posts", admin, body={"channel_id": dm, "message": "reply", "root_id": root["id"], "file_ids": [rf]})
    seed["dms"]["purgeadmin-carol"]["file_ids"].append(rf)
    # uploads: one abandoned 8 days ago (goes), one started now (stays)
    uploads = {}
    for name, age in (("abandoned", 8 * day), ("in-progress", 0)):
        _, us = call("POST", "/uploads", admin, body={"channel_id": town, "filename": f"{name}.bin", "file_size": 12_000_000})
        call("POST", f"/uploads/{us['id']}", admin, raw=b"x" * 5_300_000, ctype="application/octet-stream")
        psql(f"UPDATE uploadsessions SET createat = {now - age} WHERE id = '{us['id']}'")
        uploads[name] = {"id": us["id"], "path": psql(f"SELECT path FROM uploadsessions WHERE id = '{us['id']}'")}
    seed["uploads"] = uploads
    # untracked files in a dated folder: one untouched for days (goes), one fresh (stays)
    sh("docker", "exec", "mm-purge-test-server-1", "sh", "-c",
       "d=/opt/mattermost/data/20200101/teams/stray && mkdir -p $d && head -c 120000 /dev/urandom > $d/old.bin && "
       "touch -d '2020-01-02 00:00' $d/old.bin && head -c 1000 /dev/urandom > $d/fresh.bin")
    seed["untracked"] = {"old": "20200101/teams/stray/old.bin", "fresh": "20200101/teams/stray/fresh.bin"}
    # a deleted custom emoji: Mattermost keeps its image as image_deleted
    mmctl("config", "set", "ServiceSettings.EnableCustomEmoji", "true")
    b = uuid.uuid4().hex
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"emoji\"\r\n\r\n"
            + json.dumps({"creator_id": me["id"], "name": "seedemoji"})
            + f"\r\n--{b}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"e.png\"\r\n"
            + "Content-Type: image/png\r\n\r\n").encode() + png(64) + f"\r\n--{b}--\r\n".encode()
    _, emoji = call("POST", "/emoji", admin, raw=body, ctype=f"multipart/form-data; boundary={b}")
    call("DELETE", f"/emoji/{emoji['id']}", admin)
    seed["emoji_deleted"] = f"emoji/{emoji['id']}/image_deleted"
    # old finished job records and link previews (40 days) go; recent ones stay
    psql(f"INSERT INTO jobs (id, type, priority, createat, startat, lastactivityat, status, progress, data) VALUES "
         f"('{uuid.uuid4().hex[:26]}', 'seed_old', 0, {now - 40 * day}, 0, 0, 'success', 100, '{{}}'), "
         f"('{uuid.uuid4().hex[:26]}', 'seed_running', 0, {now - 40 * day}, 0, 0, 'in_progress', 50, '{{}}')")
    psql(f"INSERT INTO linkmetadata (hash, url, timestamp, type, data) VALUES "
         f"(101, 'https://example.com/old', {now - 40 * day}, 'opengraph', '{{}}'), "
         f"(102, 'https://example.com/new', {now}, 'opengraph', '{{}}')")

    # files shared by two FileInfo rows (Mattermost's CopyFileInfos, used by plugins, keeps the paths):
    # a live copy of an archived-channel file and of an orphan must keep those files on disk
    holder = call("POST", "/posts", admin, body={"channel_id": seed["channels"]["keep-me"]["id"], "message": "copies"})[1]["id"]
    seed["shared_copies"] = []
    for src in (seed["channels"]["old-project"]["file_ids"][0], seed["orphans"][0]):
        new_id = uuid.uuid4().hex[:26]
        psql(f"INSERT INTO fileinfo SELECT (jsonb_populate_record(NULL::fileinfo, to_jsonb(f) || "
             f"jsonb_build_object('id', '{new_id}', 'postid', '{holder}'))).* FROM fileinfo f WHERE id = '{src}'")
        seed["shared_copies"].append(new_id)
        seed["keep_files"].append(new_id)
    # a finished upload whose session row lingered (Mattermost only logs if deleting it fails)
    live = seed["channels"]["keep-me"]["file_ids"][0]
    seed["lingering_session"] = uuid.uuid4().hex[:26]
    psql(f"INSERT INTO uploadsessions (id, type, createat, userid, channelid, filename, path, filesize, fileoffset) "
         f"SELECT '{seed['lingering_session']}', 'attachment', {now - 10 * day}, creatorid, '{seed['channels']['keep-me']['id']}', "
         f"name, path, size, size FROM fileinfo WHERE id = '{live}'")

    out.write_text(json.dumps(seed, indent=2))
    print(f"Seeded. Users/passwords and ids written to {out}.")
    print("Expect the tool to find: 3 archived channels (2 with files), 3 DMs with deactivated users, "
          f"{len(seed['orphans'])} orphaned files, 1 deleted message, 1 abandoned upload, 1 untracked file, "
          "1 deleted emoji image, old jobs and link previews. It must keep: keep-me, the DM with dave, draft-upload.png, "
          "both files of the edited post, the in-progress upload and the fresh untracked file.")


if __name__ == "__main__":
    main()
