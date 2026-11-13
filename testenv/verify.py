"""Check the test server's real state against what seed.py created.

    python testenv/verify.py --pre                 before purging: everything must still exist
    python testenv/verify.py --post [--kept NAME]  after purging: targets gone, keepers intact
                                                   (--kept payroll-2023 if you unticked it in the app)

Checks rows in Postgres and files on disk (inside the SSH stand-in). Exits 1 on any failure.
"""
import json
import subprocess
import sys
from pathlib import Path

SEED = Path(__file__).parent / ".seed.json"
DATA = "/opt/mattermost/data"


def sh(*cmd) -> subprocess.CompletedProcess:
    return subprocess.run(["wsl", "-e", *cmd], capture_output=True, text=True, stdin=subprocess.DEVNULL)


def psql(sql) -> str:
    return sh("docker", "exec", "mm-purge-test-postgres-1", "psql", "-U", "mmuser", "-d", "mattermost", "-At", "-c", sql).stdout.strip()


def rows(table, ids) -> int:
    if not ids:
        return 0
    return int(psql(f"SELECT count(*) FROM {table} WHERE id IN ({','.join(repr(i) for i in ids)})"))


def on_disk(paths) -> int:
    if not paths:
        return 0
    script = "n=0; for p in \"$@\"; do [ -e \"" + DATA + "/$p\" ] && n=$((n+1)); done; echo $n"
    return int(sh("docker", "exec", "mm-purge-test-server-1", "sh", "-c", script, "_", *paths).stdout.strip())


def paths_for(file_ids) -> list:
    if not file_ids:
        return []
    out = psql("SELECT concat_ws(chr(10), path, nullif(thumbnailpath,''), nullif(previewpath,'')) FROM fileinfo "
               f"WHERE id IN ({','.join(repr(i) for i in file_ids)})")
    return [p for p in out.splitlines() if p]


def main():
    seed = json.loads(SEED.read_text())
    kept = set(sys.argv[sys.argv.index("--kept") + 1:]) if "--kept" in sys.argv else set()
    if "--pre" in sys.argv:  # remember file paths now; rows are gone after the purge
        for c in list(seed["channels"].values()) + list(seed["dms"].values()):
            c["paths"] = paths_for(c["file_ids"])
        seed["orphan_paths"] = paths_for(seed["orphans"])
        seed["keep_paths"] = paths_for(seed["keep_files"])
        if "deleted_post" in seed:
            seed["deleted_paths"] = paths_for(seed["deleted_post"]["file_ids"])
        SEED.write_text(json.dumps(seed, indent=2))

    post = "--post" in sys.argv
    checks = []
    keep = set(seed.get("keep_paths", []))  # files a kept row still uses (shared copies) must stay even in a purged bin

    def gone(paths):
        return [p for p in paths if p not in keep]

    def expect(label, actual, wanted):
        checks.append((actual == wanted, label, actual, wanted))

    for name, c in seed["channels"].items():
        target = post and c["archived"] and name not in kept
        expect(f"channel {name}: row", rows("channels", [c["id"]]), 0 if target else 1)
        expect(f"channel {name}: files on disk", on_disk(gone(c.get("paths", []))), 0 if target else len(gone(c.get("paths", []))))
    for name, d in seed["dms"].items():
        target = post and d["target"]
        # Mattermost can't delete DM channels: the conversation stays, its posts must be gone
        posts = int(psql(f"SELECT count(*) FROM posts WHERE channelid = '{d['id']}' AND type = ''"))
        expect(f"DM {name}: messages", min(posts, 1), 0 if target else 1)
        expect(f"DM {name}: files on disk", on_disk(d.get("paths", [])), 0 if target else len(d.get("paths", [])))
    n = len(seed["orphans"])
    expect("orphaned FileInfo rows", rows("fileinfo", seed["orphans"]), 0 if post else n)
    expect("orphaned files on disk", on_disk(gone(seed.get("orphan_paths", []))), 0 if post else len(gone(seed.get("orphan_paths", []))))
    expect("unattached upload (draft): row kept", rows("fileinfo", [seed["unattached"]]), 1)
    expect("files that must stay: on disk", on_disk(seed.get("keep_paths", [])), len(seed.get("keep_paths", [])))
    expect("files that must stay: rows", rows("fileinfo", seed["keep_files"]), len(seed["keep_files"]))

    if "deleted_post" in seed:  # the newer bins (seed.py from this version on)
        d = seed["deleted_post"]
        expect("deleted message: row", rows("posts", [d["post_id"]]), 0 if post else 1)
        expect("deleted message: edit history", int(psql(f"SELECT count(*) FROM posts WHERE originalid = '{d['post_id']}'")), 0 if post else 1)
        expect("deleted message: files on disk", on_disk(seed.get("deleted_paths", [])), 0 if post else len(seed.get("deleted_paths", [])))
        expect("edited post: still live", int(psql(f"SELECT count(*) FROM posts WHERE id = '{seed['edited_post']['post_id']}' AND deleteat = 0")), 1)
        up = seed["uploads"]
        expect("abandoned upload: row", rows("uploadsessions", [up["abandoned"]["id"]]), 0 if post else 1)
        expect("abandoned upload: file", on_disk([up["abandoned"]["path"]]), 0 if post else 1)
        expect("in-progress upload: row and file", rows("uploadsessions", [up["in-progress"]["id"]]) + on_disk([up["in-progress"]["path"]]), 2)
        expect("untracked old file", on_disk([seed["untracked"]["old"]]), 0 if post else 1)
        expect("untracked fresh file (kept)", on_disk([seed["untracked"]["fresh"]]), 1)
        expect("deleted emoji image", on_disk([seed["emoji_deleted"]]), 0 if post else 1)
        cutoff = "(extract(epoch from now()) * 1000)::bigint - 30 * 86400000::bigint"
        old_jobs = int(psql(f"SELECT count(*) FROM jobs WHERE createat < {cutoff} AND status NOT IN ('in_progress', 'pending')"))
        expect("old finished jobs", min(old_jobs, 1), 0 if post else 1)
        expect("old running job (kept)", int(psql("SELECT count(*) FROM jobs WHERE type = 'seed_running'")), 1)
        expect("link previews (old, recent)",
               psql("SELECT string_agg(hash::text, ',' ORDER BY hash) FROM linkmetadata WHERE hash IN (101, 102)"),
               "102" if post else "101,102")
        expect("lingering session of a finished upload (kept)", rows("uploadsessions", [seed["lingering_session"]]), 1)
        avatars = sh("docker", "exec", "mm-purge-test-server-1", "sh", "-c", f"ls {DATA}/users/*/profile.png | wc -l").stdout.strip()
        expect("profile images untouched", int(avatars or 0) > 0, True)

    for ok, label, actual, wanted in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {label}: {actual} (expected {wanted})")
    failed = sum(not c[0] for c in checks)
    print(f"\n{len(checks) - failed}/{len(checks)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
