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

    out.write_text(json.dumps(seed, indent=2))
    print(f"Seeded. Users/passwords and ids written to {out}.")
    print("Expect the tool to find: 3 archived channels (2 with files), 3 DMs with deactivated users, "
          f"{len(seed['orphans'])} orphaned files. It must keep: keep-me, the DM with dave, and draft-upload.png.")


if __name__ == "__main__":
    main()
