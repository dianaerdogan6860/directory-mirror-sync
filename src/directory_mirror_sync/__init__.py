"""Directory Mirror Sync: mirror a source tree into a destination tree.

Mirroring is destructive by nature: files in the destination that have no
counterpart in the source are removed. ``MirrorSync`` performs a one-way
sweep, so source-side edits overwrite destination-side edits and source-side
deletions are propagated. It is intentionally not a two-way sync.
"""

from .core import MirrorSync, SyncResult

__all__ = ["MirrorSync", "SyncResult"]
