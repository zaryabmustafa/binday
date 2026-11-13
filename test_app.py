"""Self-check for app.py's core: run `python test_app.py`. No server needed (fakes stand in)."""
import socket
import threading

import app


def test_format_bytes():
    assert app.format_bytes(0) == "0.00 B"
    assert app.format_bytes(1024) == "1.00 KB"
    assert app.format_bytes(3.9 * 1024 ** 3) == "3.90 GB"


def test_tunnel_roundtrip():
    """start_tunnel forwards bytes both ways through whatever the transport's channel connects to."""
    echo = socket.socket()
    echo.bind(("127.0.0.1", 0))
    echo.listen(1)

    def serve():
        c, _ = echo.accept()
        c.sendall(c.recv(1024).upper())
        c.close()

    threading.Thread(target=serve, daemon=True).start()

    class FakeTransport:  # stands in for paramiko: the "SSH channel" is a plain socket to the echo server
        def open_channel(self, kind, dest, src):
            assert kind == "direct-tcpip" and dest == ("db.internal", 5432)
            return socket.create_connection(echo.getsockname())

    srv = app.start_tunnel(FakeTransport(), "db.internal", 5432)
    with socket.create_connection(srv.getsockname(), timeout=5) as c:
        c.sendall(b"select 1")
        assert c.recv(1024) == b"SELECT 1"
    srv.close()


class FakeSession:
    """Minimal Session: an in-memory disk and DB, records what apply() deletes."""
    def __init__(self, eligible, fail_mmctl_for=()):
        self.eligible, self.fail_mmctl_for = eligible, set(fail_mmctl_for)
        self.disk = {}                       # path -> size
        self.removed, self.mmctl_calls, self.sql = [], [], []
        self.errors = []
        self.s = app.Settings(ssh="x", remote_data_dir="/data", pg={})
        self.sftp = self
        self.cur = self
        self._rows = []
        self.rowcount = 0

    # sftp
    def stat(self, path):
        if path not in self.disk:
            raise IOError(path)
        class St: st_size, st_mtime = self.disk[path], 0
        return St()

    def remove(self, path):
        del self.disk[path]
        self.removed.append(path)

    # session helpers used by apply()
    def error(self, msg):
        self.errors.append(msg)

    def mmctl(self, *args, **kw):
        self.mmctl_calls.append(args)
        if any(a in self.fail_mmctl_for for a in args):
            raise RuntimeError("mmctl failed")
        return ""

    def q(self, t): return t
    def col(self, t, c): return c

    # cursor
    def execute(self, sql, params=()):
        self.sql.append((sql, params))
        self._last = params
        self._rows = [("post-of-" + params[0],)] if sql.startswith("SELECT Id FROM posts") else []

    def fetchone(self):
        return (1,) if self._last and self._last[0] in self.eligible else None

    def fetchall(self):
        return self._rows


def test_apply_deletes_only_eligible_plan_items():
    items = [
        {"kind": "channel", "id": "ch1", "label": "c", "paths": ["a/1"]},
        {"kind": "dm", "id": "dm1", "user_id": "u1", "label": "d", "paths": ["b/1", "b/2"]},
        {"kind": "orphan", "id": "fi1", "label": "o", "paths": ["c/1"]},
        {"kind": "channel", "id": "gone", "label": "x", "paths": ["z/1"]},  # changed since scan
    ]
    sess = FakeSession(eligible={"ch1", "dm1", "fi1"})
    sess.disk = {"/data/a/1": 100, "/data/b/1": 100, "/data/b/2": 100, "/data/c/1": 100, "/data/z/1": 100}
    seen = []
    res = app.apply(sess, items, on_item=lambda it, st, freed, errs: seen.append((it["id"], st, freed)))
    assert res == {"deleted": 3, "skipped": 1, "bytes": 400}, res
    assert seen == [("ch1", "deleted", 100), ("dm1", "deleted", 200), ("fi1", "deleted", 100), ("gone", "skipped", 0)]
    assert sess.disk == {"/data/z/1": 100}                                  # only the skipped item's file is left
    assert ("channel", "delete", "ch1", "--confirm") in sess.mmctl_calls      # channels: mmctl channel delete
    assert ("post", "delete", "post-of-dm1", "--permanent", "--confirm") in sess.mmctl_calls  # DMs: posts, not channel
    assert not any(c[:2] == ("channel", "delete") and c[2] == "dm1" for c in sess.mmctl_calls)
    assert any(s.startswith("DELETE FROM fileinfo") and p == ("fi1",) for s, p in sess.sql)
    assert not sess.errors


def test_failed_mmctl_leaves_files_alone():
    """If Mattermost's own delete fails, none of the item's files may be removed."""
    sess = FakeSession(eligible={"ch1"}, fail_mmctl_for={"ch1"})
    sess.disk = {"/data/a/1": 100}
    seen = []
    res = app.apply(sess, [{"kind": "channel", "id": "ch1", "label": "c", "paths": ["a/1"]}],
                    on_item=lambda it, st, freed, errs: seen.append((st, freed, len(errs))))
    assert res["bytes"] == 0 and seen == [("failed", 0, 1)], (res, seen)
    assert sess.disk == {"/data/a/1": 100} and not sess.removed


def test_new_bins_apply():
    """Deleted posts go through mmctl; uploads drop their row; untracked/emoji only lose files; tidies only run SQL."""
    items = [
        {"kind": "deleted", "id": "p1", "label": "in Town", "paths": ["d/1"]},
        {"kind": "upload", "id": "u1", "label": "big.bin", "paths": ["u/1"]},
        {"kind": "untracked", "id": "x/1", "label": "stray", "paths": ["x/1"]},
        {"kind": "emoji", "id": "e1", "label": ":gone:", "paths": ["emoji/e1/image_deleted"]},
        {"kind": "jobs", "id": "jobs", "label": "5 rows", "paths": [], "bytes": 0, "at": 10 ** 13},
    ]
    sess = FakeSession(eligible={"p1", "u1", "e1"})
    sess.t = {k: k for k in ("posts", "fileinfo", "uploadsessions", "emoji", "jobs")}
    sess.disk = {"/data/d/1": 10, "/data/u/1": 20, "/data/x/1": 30, "/data/emoji/e1/image_deleted": 40}
    real, app._path_known = app._path_known, lambda s, path: False  # nothing points at x/1
    seen = []
    try:
        res = app.apply(sess, items, on_item=lambda it, st, freed, errs: seen.append((it["id"], st)))
    finally:
        app._path_known = real
    assert [s for _, s in seen] == ["deleted"] * 5, seen
    assert ("post", "delete", "p1", "--permanent", "--confirm") in sess.mmctl_calls
    assert sess.disk == {}, sess.disk
    assert any(s.startswith("DELETE FROM uploadsessions") and p == ("u1",) for s, p in sess.sql)
    assert any(s.startswith("DELETE FROM jobs") for s, _ in sess.sql)
    assert res["bytes"] == 100 and not sess.errors
    assert any(s.startswith("DELETE FROM posts WHERE OriginalId") and p == ("p1",) for s, p in sess.sql)  # its edit history


def test_deleted_posts_go_in_one_batch():
    """A run of deleted messages is one mmctl call, in plan order; a failed batch retries each alone."""
    items = [{"kind": "deleted", "id": f"p{i}", "label": "m", "paths": []} for i in range(3)]
    sess = FakeSession(eligible={"p0", "p1", "p2"})
    app.apply(sess, items)
    assert [c for c in sess.mmctl_calls if c[:2] == ("post", "delete")] == [("post", "delete", "p0", "p1", "p2", "--permanent", "--confirm")]
    sess = FakeSession(eligible={"p0", "p1", "p2"}, fail_mmctl_for={"p1"})
    seen = []
    app.apply(sess, items, on_item=lambda it, st, freed, errs: seen.append((it["id"], st)))
    assert seen == [("p0", "deleted"), ("p1", "failed"), ("p2", "deleted")], seen


def test_vacuum_only_touched_tables():
    assert app.vacuum_tables([{"kind": "orphan"}, {"kind": "untracked"}]) == ["fileinfo"]
    assert app.vacuum_tables([{"kind": "jobs"}, {"kind": "deleted"}]) == ["fileinfo", "jobs", "posts"]


def test_untracked_sanity_check():
    app.check_untracked_sane(100, 95, 5)  # a few strays: fine
    try:
        app.check_untracked_sane(100, 3, 97)  # nearly nothing matches the database: wrong folder
    except RuntimeError:
        return
    raise AssertionError("expected the untracked scan to refuse")


def test_collectable_keeps_nested_unless_parent_kept():
    items = [{"kind": "channel", "id": "c1"}, {"kind": "deleted", "id": "p1", "inside": "c1"}, {"kind": "deleted", "id": "p2"}]
    assert [i["id"] for i in app.collectable(items)] == ["c1", "p2"]
    assert [i["id"] for i in app.collectable(items, {"c1"})] == ["c1", "p1", "p2"]


def test_tidy_never_counts_as_disk():
    """Database tidies free no disk until VACUUM: their bytes are 0, the estimate lives in db_bytes."""
    class Cur(FakeSession):
        def execute(self, sql, params=()):
            self._one = (1200, 120) if "pg_class" in sql else (12,)
        def fetchone(self):
            return self._one
    sess = Cur(eligible=set())
    sess.t = {"jobs": "Jobs"}
    it = app.tidy_item(sess, "jobs")
    assert it["bytes"] == 0 and it["rows"] == 12 and it["db_bytes"] == 120, it


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
