"""Synthetic server for `python gui.py --demo`: replaces app.py's server calls with fake data.

Nothing connects anywhere. Data is illustrative (matches mockup/index.html). One DM is
"reactivated after the scan" (skipped) and one orphaned file fails, so every state shows.
"""
import random
import time

import app

SPEED = 1.0  # install(fast=True) sets 0: no artificial delays (README screenshots)

MB = 1024 ** 2


class DemoSession:
    def __init__(self, settings):
        time.sleep(0.8 * SPEED)
        self.s, self.schema, self.errors = settings, "public", []

    def error(self, msg):
        self.errors.append(msg)
        app.logger.error(msg)

    def close(self):
        pass


def _items():
    rnd = random.Random(7)
    chans = [("release-2023-q2", "engineering", 418, 612), ("design-assets-old", "design", 203, 355),
             ("payroll-2023", "finance", 31, 214), ("incident-2022-11-db", "operations", 57, 98),
             ("vendor-onboarding", "sales", 22, 64), ("hackathon-2022", "engineering", 40, 41),
             ("q3-planning", "operations", 12, 18), ("intern-cohort-21", "people", 9, 11),
             ("old-website", "marketing", 30, 9), ("trial-acme", "sales", 6, 4), ("offsite-2022", "people", 14, 3),
             ("legacy-alerts", "operations", 10, 3)]
    out = [{"kind": "channel", "id": f"ch{i}", "label": n, "group": g, "paths": [f"c{i}/{k}" for k in range(f)],
            "bytes": mb * MB, "missing": 3 if n.startswith("incident") else 0} for i, (n, g, f, mb) in enumerate(chans)]
    people = ["j.okafor", "m.lindqvist", "a.reyes", "s.haddad", "t.nguyen"] + [f"user{n:02d}" for n in range(41)]
    sizes = [181, 122, 87, 9, 81] + [rnd.randint(1, 12) for _ in range(41)]
    for i, (p, mb) in enumerate(zip(people, sizes)):
        out.append({"kind": "dm", "id": f"dm{i}", "user_id": f"u{i}", "label": f"DM with {p}", "group": "Direct messages",
                    "paths": [f"d{i}/{k}" for k in range(max(1, mb // 2))], "bytes": mb * MB, "missing": 0})
    for i in range(312):
        out.append({"kind": "deleted", "id": f"p{i}", "label": "in Town Square", "group": "Deleted messages",
                    "paths": [f"p{i}/a"] if i % 3 else [], "bytes": (rnd.randint(1, 9) * MB) if i % 3 else 0, "missing": 0})
    for i in range(954):
        out.append({"kind": "orphan", "id": f"fi{i}", "label": f"{rnd.getrandbits(32):08x}.png", "group": "Orphaned files",
                    "paths": [f"o{i}/a", f"o{i}/t", f"o{i}/p"], "bytes": 2_100_000, "missing": 0})
    for i in range(7):
        out.append({"kind": "upload", "id": f"up{i}", "label": f"recording-{i}.mp4", "group": "Abandoned uploads",
                    "paths": [f"u{i}"], "bytes": rnd.randint(40, 300) * MB, "missing": 0})
    for i in range(23):
        out.append({"kind": "untracked", "id": f"x{i}", "label": f"{rnd.getrandbits(32):08x}.bin", "group": "Untracked files",
                    "paths": [f"x{i}"], "bytes": rnd.randint(1, 30) * MB, "missing": 0})
    for i, name in enumerate(("partyparrot", "old-logo", "shipit")):
        out.append({"kind": "emoji", "id": f"e{i}", "label": f":{name}:", "group": "Deleted emoji",
                    "paths": [f"emoji/e{i}/image_deleted"], "bytes": 180_000, "missing": 0})
    for kind, rows, db in (("history", 18_420, 61 * MB), ("jobs", 9_310, 14 * MB), ("links", 52_800, 220 * MB)):
        out.append({"kind": kind, "id": kind, "label": f"{rows:,} rows", "group": "Database", "paths": [],
                    "bytes": 0, "db_bytes": db, "missing": 0, "rows": rows, "at": 0})
    return out


def scan(sess, kinds, progress=lambda m: None, limit=0, on_item=None):
    items = []
    for it in _items():
        if it["kind"] not in kinds:
            continue
        progress(f"Measuring {it['label']}")
        time.sleep((0.004 if it["kind"] in ("orphan", "deleted") else 0.12) * SPEED)
        items.append(it)
        if on_item:
            on_item(it)
    return items


def apply(sess, items, progress=lambda i, n, l: None, on_item=None):
    deleted = skipped = freed = 0
    for i, it in enumerate(items):
        progress(i, len(items), it["label"])
        time.sleep((0.002 if it["kind"] in ("orphan", "deleted") else 0.15) * SPEED)
        errs = []
        if it["label"] == "DM with t.nguyen":
            status, got = "skipped", 0
        elif it["kind"] == "orphan" and it["id"] == "fi17":
            sess.error(f"Failed to remove /opt/mattermost/data/20231014/{it['label']}: Permission denied")
            errs, status, got = sess.errors[-1:], "failed", 0
        else:
            status, got = "deleted", it["bytes"]
            if "rows" in it:
                it["rows_deleted"] = it["rows"]
        freed += got
        deleted += status == "deleted"
        skipped += status == "skipped"
        if on_item:
            on_item(it, status, got, errs)
    progress(len(items), len(items), "")
    return {"deleted": deleted, "skipped": skipped, "bytes": freed}


_usage = [int(48.6 * 1024 ** 3)]


def storage_usage(sess):
    return _usage[0]


def install(fast: bool = False):
    global SPEED
    SPEED = 0.0 if fast else 1.0
    real_apply = apply

    def apply_and_shrink(sess, items, progress=lambda i, n, l: None, on_item=None):
        res = real_apply(sess, items, progress, on_item)
        _usage[0] -= res["bytes"]
        return res

    app.test_server = lambda s: (time.sleep(0.6 * SPEED), [
        (True, "SSH connected", f"{s.ssh} · key from agent"), (True, "Data directory found", "48.60 GB in use"),
        (True, "Docker container running", s.container), (True, "mmctl works", "4 teams · Version: 12.1.0")])[1]
    app.test_database = lambda s: (time.sleep(0.6 * SPEED), [
        (True, "Database reachable", f"{s.pg.get('dbname')} · schema public · via SSH"),
        (True, "Mattermost tables found", "214 users")])[1]
    app.test_mmctl = lambda s: (time.sleep(0.6 * SPEED), [
        (True, "No login needed", "mmctl in the container already works"), (True, "mmctl works", "4 teams"),
        (True, "Channel deletion is on", "EnableAPIChannelDeletion = true")])[1]
    app.Session = DemoSession
    app.mmctl_team_names = lambda sess: ["engineering", "design", "operations", "sales"]
    app.channel_deletion_enabled = lambda sess: True
    app.storage_usage = storage_usage
    app.scan = scan
    app.apply = apply_and_shrink
    app.vacuum = lambda sess, tables=(): time.sleep(1.5 * SPEED)
    app.post_deletion_problem = lambda sess: None
