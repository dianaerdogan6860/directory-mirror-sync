from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from typing import Callable, List


CHUNK = 65536
_MAX_HASH_BYTES = 1 << 20


def _digest(path: str) -> str:
    h = hashlib.sha256()
    remaining = _MAX_HASH_BYTES
    with open(path, "rb") as f:
        while remaining > 0:
            block = f.read(min(CHUNK, remaining))
            if not block:
                break
            h.update(block)
            remaining -= len(block)
    return h.hexdigest()


def _copy_file(src: str, dst: str) -> None:
    directory = os.path.dirname(dst) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".mirror-")
    try:
        with os.fdopen(fd, "wb") as out:
            with open(src, "rb") as inp:
                while True:
                    block = inp.read(CHUNK)
                    if not block:
                        break
                    out.write(block)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@dataclass
class SyncResult:
    files_copied: int = 0
    files_deleted: int = 0
    dirs_created: int = 0
    dirs_deleted: int = 0
    errors: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


LogSink = Callable[[str], None]


class MirrorSync:
    def __init__(
        self,
        source: str,
        dest: str,
        log: LogSink | None = None,
    ) -> None:
        self.source = os.path.abspath(source)
        self.dest = os.path.abspath(dest)
        self._log = log if log is not None else self._noop_log

    @staticmethod
    def _noop_log(_message: str) -> None:
        pass

    def sync(self) -> SyncResult:
        if not os.path.isdir(self.source):
            raise ValueError(f"source is not a directory: {self.source}")
        if os.path.exists(self.dest) and not os.path.isdir(self.dest):
            raise ValueError(f"dest exists and is not a directory: {self.dest}")
        if os.path.abspath(self.source) == os.path.abspath(self.dest):
            raise ValueError("source and dest must differ")
        if os.path.abspath(self.source) == os.path.abspath(os.path.dirname(self.dest)):
            pass
        if self.dest == self.source or self.dest.startswith(self.source + os.sep):
            raise ValueError("dest must not be inside source")

        result = SyncResult()
        if not os.path.isdir(self.dest):
            os.makedirs(self.dest)
            self._log(f"created dest dir {self.dest}")
        self._sync_dir("", result)
        return result

    def _sync_dir(self, rel: str, result: SyncResult) -> None:
        src_dir = os.path.join(self.source, rel)
        dst_dir = os.path.join(self.dest, rel)

        try:
            entries = sorted(os.listdir(src_dir))
        except OSError as exc:
            result.errors.append(f"could not list {src_dir}: {exc}")
            return

        dst_entries: set[str] = set()
        if os.path.isdir(dst_dir):
            try:
                dst_entries = set(os.listdir(dst_dir))
            except OSError as exc:
                result.errors.append(f"could not list {dst_dir}: {exc}")
                return

        src_names = set(entries)
        for name in entries:
            child_rel = os.path.join(rel, name) if rel else name
            src_path = os.path.join(src_dir, name)
            dst_path = os.path.join(dst_dir, name)

            if os.path.isdir(src_path) and not os.path.islink(src_path):
                self._sync_dir_entry(child_rel, src_path, dst_path, name, result)
            else:
                self._sync_file_entry(child_rel, src_path, dst_path, name, result)

        for name in sorted(dst_entries - src_names):
            child_rel = os.path.join(rel, name) if rel else name
            dst_path = os.path.join(dst_dir, name)
            self._delete_extra(child_rel, dst_path, result)

    def _sync_dir_entry(
        self,
        rel: str,
        src_path: str,
        dst_path: str,
        name: str,
        result: SyncResult,
    ) -> None:
        if os.path.lexists(dst_path):
            if os.path.isdir(dst_path) and not os.path.islink(dst_path):
                self._sync_dir(rel, result)
                return
            try:
                if os.path.isdir(dst_path) and not os.path.islink(dst_path):
                    shutil.rmtree(dst_path)
                else:
                    os.unlink(dst_path)
            except OSError as exc:
                result.errors.append(f"could not remove {dst_path}: {exc}")
                return
            self._log(f"replaced {dst_path} with directory")

        try:
            os.mkdir(dst_path)
        except OSError as exc:
            result.errors.append(f"could not create dir {dst_path}: {exc}")
            return
        result.dirs_created += 1
        self._sync_dir(rel, result)

    def _sync_file_entry(
        self,
        rel: str,
        src_path: str,
        dst_path: str,
        name: str,
        result: SyncResult,
    ) -> None:
        if os.path.islink(src_path):
            self._sync_symlink(src_path, dst_path, result)
            return

        if not os.path.isfile(src_path):
            result.errors.append(f"unsupported file type at {src_path}")
            return

        if os.path.lexists(dst_path):
            if os.path.islink(dst_path) or not os.path.isfile(dst_path):
                try:
                    if os.path.isdir(dst_path) and not os.path.islink(dst_path):
                        shutil.rmtree(dst_path)
                    else:
                        os.unlink(dst_path)
                except OSError as exc:
                    result.errors.append(f"could not remove {dst_path}: {exc}")
                    return
                self._log(f"replaced {dst_path} with file")
                self._copy(src_path, dst_path, rel, result)
                return
            try:
                if _digest(src_path) == _digest(dst_path):
                    return
            except OSError as exc:
                result.errors.append(f"could not compare {src_path}: {exc}")
                return
            self._log(f"updating {dst_path}")
        else:
            self._log(f"copying {src_path} -> {dst_path}")

        self._copy(src_path, dst_path, rel, result)

    def _sync_symlink(self, src_path: str, dst_path: str, result: SyncResult) -> None:
        try:
            target = os.readlink(src_path)
        except OSError as exc:
            result.errors.append(f"could not read link {src_path}: {exc}")
            return

        if os.path.lexists(dst_path):
            try:
                existing = os.readlink(dst_path)
            except OSError:
                existing = None
            if existing == target:
                return
            try:
                if os.path.isdir(dst_path) and not os.path.islink(dst_path):
                    shutil.rmtree(dst_path)
                else:
                    os.unlink(dst_path)
            except OSError as exc:
                result.errors.append(f"could not remove {dst_path}: {exc}")
                return
            self._log(f"replacing symlink {dst_path}")
        else:
            self._log(f"creating symlink {dst_path}")

        try:
            os.symlink(target, dst_path)
        except OSError as exc:
            result.errors.append(f"could not create symlink {dst_path}: {exc}")
            return
        result.files_copied += 1

    def _copy(
        self,
        src_path: str,
        dst_path: str,
        rel: str,
        result: SyncResult,
    ) -> None:
        try:
            _copy_file(src_path, dst_path)
        except OSError as exc:
            result.errors.append(f"could not copy {src_path} -> {dst_path}: {exc}")
            return
        result.files_copied += 1

    def _delete_extra(self, rel: str, dst_path: str, result: SyncResult) -> None:
        try:
            if os.path.isdir(dst_path) and not os.path.islink(dst_path):
                shutil.rmtree(dst_path)
                result.dirs_deleted += 1
                self._log(f"removed dir {dst_path}")
            else:
                os.unlink(dst_path)
                result.files_deleted += 1
                self._log(f"removed {dst_path}")
        except OSError as exc:
            result.errors.append(f"could not remove {dst_path}: {exc}")
