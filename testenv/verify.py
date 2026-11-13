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
        SEED.write_text(json.dumps(seed, indent=2))

    post = "--post" in sys.argv
    checks = []

    def expect(label, actual, wanted):
        checks.append((actual == wanted, label, actual, wanted))

    for name, c in seed["channels"].items():
        target = post and c["archived"] and name not in kept
        expect(f"channel {name}: row", rows("channels", [c["id"]]), 0 if target else 1)
        expect(f"channel {name}: files on disk", on_disk(c.get("paths", [])), 0 if target else len(c.get("paths", [])))
    for name, d in seed["dms"].items():
        target = post and d["target"]
        # Mattermost can't delete DM channels: the conversation stays, its posts must be gone
        posts = int(psql(f"SELECT count(*) FROM posts WHERE channelid = '{d['id']}' AND type = ''"))
        expect(f"DM {name}: messages", min(posts, 1), 0 if target else 1)
        expect(f"DM {name}: files on disk", on_disk(d.get("paths", [])), 0 if target else len(d.get("paths", [])))
    n = len(seed["orphans"])
    expect("orphaned FileInfo rows", rows("fileinfo", seed["orphans"]), 0 if post else n)
    expect("orphaned files on disk", on_disk(seed.get("orphan_paths", [])), 0 if post else len(seed.get("orphan_paths", [])))
    expect("unattached upload (draft): row kept", rows("fileinfo", [seed["unattached"]]), 1)
    expect("files that must stay: on disk", on_disk(seed.get("keep_paths", [])), len(seed.get("keep_paths", [])))

    for ok, label, actual, wanted in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {label}: {actual} (expected {wanted})")
    failed = sum(not c[0] for c in checks)
    print(f"\n{len(checks) - failed}/{len(checks)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
