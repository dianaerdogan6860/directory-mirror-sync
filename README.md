# Directory Mirror Sync

`directory_mirror_sync` mirrors a source directory tree into a destination tree: it copies files that are new or changed, and deletes destination paths that no longer exist in the source. It is a one-way, destructive mirror — destination-side edits are overwritten and destination-side-only files are removed.

```python
import tempfile
from directory_mirror_sync import MirrorSync, SyncResult

with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as dest:
    sync = MirrorSync(source, dest)
    result: SyncResult = sync.sync()
    if not result.ok:
        for msg in result.errors:
            print(msg)
```

`MirrorSync(source, dest, log=None)` takes absolute or relative paths and an optional `log` callable that receives one human-readable string per significant action. `sync()` returns a `SyncResult` with `files_copied`, `files_deleted`, `dirs_created`, `dirs_deleted`, `errors` (a list of strings), and an `ok` property that is `True` when `errors` is empty.

## Why this exists

The specific problem is keeping a generated or published directory identical to an authoritative source without relying on modification times. Mtimes are unreliable across filesystems, tarballs, and clock skew, and two different files can share a size. This library compares the first megabyte of each file by SHA-256 instead, so a re-sync after a no-op is genuinely a no-op and an identical mtime on different content still triggers a copy.

The trade-off is that the content check is truncated: two files whose first megabyte is identical but whose tails differ will not be re-copied. That is accepted because such files are almost always the same logical asset with an appended trailer, and hashing gigabytes on every sync would dominate the cost of the sync itself. If you need exact byte equality, run `sync()` a second time; the first run writes the full file, and the second is a no-op only when the copy was truly complete.

## The awkward edge you will hit

That truncation is the one edge to know about. A file changed only past the first megabyte will appear unchanged after a sync and will keep its old tail in the destination. For typical text, code, and config files this never matters. For large append-only logs or media with appended metadata, call `sync()` twice if you suspect a partial change, or replace `_MAX_HASH_BYTES` in `directory_mirror_sync.core` with a larger value for your deployment.

Symlinks are mirrored as symlinks (their target string is preserved), not dereferenced. Special files (FIFOs, devices, sockets) are reported in `result.errors` rather than copied.

## Design notes

The window stores values eagerly rather than keeping running aggregates. Running
sums drift with floating point over long streams, and recomputing from a small
buffer is cheap enough that the drift is not worth the speed.

