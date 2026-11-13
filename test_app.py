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

    # sftp
    def stat(self, path):
        if path not in self.disk:
            raise IOError(path)
        class St: st_size = self.disk[path]
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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
