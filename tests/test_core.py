from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from directory_mirror_sync import MirrorSync, SyncResult


class _Harness(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        self.source = os.path.join(self._tmp, "src")
        self.dest = os.path.join(self._tmp, "dst")
        os.makedirs(self.source)
        self.messages: list[str] = []

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def make_sync(self) -> MirrorSync:
        return MirrorSync(self.source, self.dest, log=self.messages.append)

    def write(self, path: str, content: bytes) -> None:
        full = os.path.join(self.source, path)
        os.makedirs(os.path.dirname(full) or self.source, exist_ok=True)
        with open(full, "wb") as f:
            f.write(content)

    def dst_read(self, path: str) -> bytes:
        with open(os.path.join(self.dest, path), "rb") as f:
            return f.read()

    def dst_exists(self, path: str) -> bool:
        return os.path.lexists(os.path.join(self.dest, path))


class TestInitialSync(_Harness):
    def test_empty_source_produces_empty_dest(self) -> None:
        result = self.make_sync().sync()
        self.assertTrue(result.ok)
        self.assertEqual(
            (result.files_copied, result.files_deleted,
             result.dirs_created, result.dirs_deleted),
            (0, 0, 0, 0),
        )
        self.assertEqual(os.listdir(self.dest), [])

    def test_copies_new_files_and_dirs(self) -> None:
        self.write("a.txt", b"alpha")
        self.write("sub/b.txt", b"beta")
        result = self.make_sync().sync()
        self.assertTrue(result.ok)
        self.assertEqual(result.files_copied, 2)
        self.assertEqual(result.dirs_created, 1)  # only 'sub'; dest root created via os.makedirs
        self.assertEqual(self.dst_read("a.txt"), b"alpha")
        self.assertEqual(self.dst_read("sub/b.txt"), b"beta")


class TestIdempotence(_Harness):
    def test_second_sync_is_noop_when_unchanged(self) -> None:
        self.write("a.txt", b"alpha")
        sync = self.make_sync()
        first = sync.sync()
        second = sync.sync()
        self.assertTrue(first.ok and second.ok)
        self.assertGreater(first.files_copied, 0)
        self.assertEqual(second.files_copied, 0)
        self.assertEqual(second.dirs_created, 0)
        self.assertEqual(second.files_deleted, 0)
        self.assertEqual(second.dirs_deleted, 0)

    def test_changed_file_is_recopied(self) -> None:
        self.write("a.txt", b"alpha")
        sync = self.make_sync()
        sync.sync()
        self.write("a.txt", b"ALPHA")
        result = sync.sync()
        self.assertEqual(result.files_copied, 1)
        self.assertEqual(self.dst_read("a.txt"), b"ALPHA")

    def test_identical_prefix_but_different_tail_is_not_recopied(self) -> None:
        from directory_mirror_sync.core import _MAX_HASH_BYTES

        head = b"x" * _MAX_HASH_BYTES
        self.write("a.txt", head + b"tail-one")
        sync = self.make_sync()
        sync.sync()
        self.write("a.txt", head + b"tail-two")
        result = sync.sync()
        self.assertEqual(result.files_copied, 0)
        self.assertEqual(self.dst_read("a.txt"), head + b"tail-one")


class TestDeletions(_Harness):
    def test_removes_files_absent_from_source(self) -> None:
        self.write("keep.txt", b"keep")
        self.write("drop.txt", b"drop")
        sync = self.make_sync()
        sync.sync()
        os.unlink(os.path.join(self.source, "drop.txt"))
        result = sync.sync()
        self.assertEqual(result.files_deleted, 1)
        self.assertTrue(self.dst_exists("keep.txt"))
        self.assertFalse(self.dst_exists("drop.txt"))

    def test_removes_directories_absent_from_source(self) -> None:
        self.write("keep/a.txt", b"keep")
        self.write("gone/a.txt", b"gone")
        sync = self.make_sync()
        sync.sync()
        shutil.rmtree(os.path.join(self.source, "gone"))
        result = sync.sync()
        self.assertEqual(result.dirs_deleted, 1)
        self.assertTrue(self.dst_exists("keep/a.txt"))
        self.assertFalse(self.dst_exists("gone"))


class TestTypeTransitions(_Harness):
    def test_file_replaced_by_directory(self) -> None:
        self.write("p", b"file")
        sync = self.make_sync()
        sync.sync()
        os.unlink(os.path.join(self.source, "p"))
        os.makedirs(os.path.join(self.source, "p"))
        self.write("p/child", b"child")
        result = sync.sync()
        self.assertTrue(result.ok)
        self.assertTrue(os.path.isdir(os.path.join(self.dest, "p")))
        self.assertEqual(self.dst_read("p/child"), b"child")

    def test_directory_replaced_by_file(self) -> None:
        self.write("p/child", b"child")
        sync = self.make_sync()
        sync.sync()
        shutil.rmtree(os.path.join(self.source, "p"))
        self.write("p", b"file")
        result = sync.sync()
        self.assertTrue(result.ok)
        self.assertTrue(os.path.isfile(os.path.join(self.dest, "p")))
        self.assertEqual(self.dst_read("p"), b"file")


class TestSymlinks(_Harness):
    def test_symlink_is_preserved_as_symlink(self) -> None:
        self.write("target", b"target")
        os.symlink("target", os.path.join(self.source, "link"))
        result = self.make_sync().sync()
        self.assertTrue(result.ok)
        dst_link = os.path.join(self.dest, "link")
        self.assertTrue(os.path.islink(dst_link))
        self.assertEqual(os.readlink(dst_link), "target")

    def test_symlink_target_change_updates_destination(self) -> None:
        self.write("t1", b"one")
        self.write("t2", b"two")
        os.symlink("t1", os.path.join(self.source, "link"))
        sync = self.make_sync()
        sync.sync()
        os.unlink(os.path.join(self.source, "link"))
        os.symlink("t2", os.path.join(self.source, "link"))
        sync.sync()
        self.assertEqual(os.readlink(os.path.join(self.dest, "link")), "t2")


class TestRobustness(_Harness):
    def test_dest_not_directory_raises(self) -> None:
        with open(self.dest, "w") as f:
            f.write("not a dir")
        with self.assertRaises(ValueError):
            self.make_sync().sync()

    def test_source_must_exist(self) -> None:
        sync = MirrorSync(os.path.join(self._tmp, "missing"), self.dest)
        with self.assertRaises(ValueError):
            sync.sync()

    def test_source_equal_dest_raises(self) -> None:
        sync = MirrorSync(self.source, self.source)
        with self.assertRaises(ValueError):
            sync.sync()

    def test_nested_destination_under_source_is_refused(self) -> None:
        nested = os.path.join(self.source, "nested")
        sync = MirrorSync(self.source, nested)
        with self.assertRaises(ValueError):
            sync.sync()

    def test_large_file_copies_in_full(self) -> None:
        payload = os.urandom(4 * 1024 * 1024)
        self.write("big.bin", payload)
        result = self.make_sync().sync()
        self.assertTrue(result.ok)
        self.assertEqual(result.files_copied, 1)
        self.assertEqual(self.dst_read("big.bin"), payload)

    def test_noop_sync_emits_no_log_messages(self) -> None:
        self.write("a.txt", b"alpha")
        sync = self.make_sync()
        sync.sync()
        self.messages.clear()
        sync.sync()
        self.assertEqual(self.messages, [])


class TestResultShape(_Harness):
    def test_result_defaults(self) -> None:
        r = SyncResult()
        self.assertTrue(r.ok)
        self.assertEqual(r.errors, [])


if __name__ == "__main__":
    unittest.main()
