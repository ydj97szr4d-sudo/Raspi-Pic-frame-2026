"""A one-shot backup for the library grid's rotate/crop editor.

Smaller than :mod:`picframe3.library.removed` on purpose: that journal is
the whole explanation of what left the library and why, kept forever, with
restore and purge and a trash listing.  This one answers a single question
-- "does this file have an original to go back to?" -- for a feature where
the backup exists purely so a bad crop is not permanent, not as a record
anyone is expected to browse.  A picture gets at most one backup at a time:
editing it again before undoing does not stack a second one, because there
is nowhere further back than the original, and keeping every intermediate
step would turn ordinary use into a slow leak of disk space.

Same append-only-journal-plus-lock shape as removals.jsonl, for the same
reasons: it survives losing the database, and a line can be read with
``cat`` on the Pi without this software.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import time
from typing import Any

_log = logging.getLogger(__name__)

#: Lives beside the backup files, not in the data directory, so that copying
#: the folder off the Pi copies its explanation with it.
JOURNAL_NAME = "edits.jsonl"


class EditLog:
    """Tracks the one live backup (if any) for each edited file's path."""

    def __init__(self, folder: str):
        self.folder = os.path.expanduser(folder)
        self.path = os.path.join(self.folder, JOURNAL_NAME)
        self._cache: list[dict[str, Any]] | None = None
        self._cache_key: tuple[float, int] | None = None

    @contextlib.contextmanager
    def _locked(self):
        os.makedirs(self.folder, exist_ok=True)
        with open(self.path + ".lock", "a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def backup(self, path: str, backed_up_as: str, op: str) -> None:
        """Note that *path*'s original bytes are now at *backed_up_as*."""
        entry = {
            "path": path,
            "backed_up_as": backed_up_as,
            "op": op,
            "edited_at": time.time(),
            "restored_at": None,
        }
        self._append(entry)

    def active(self, path: str) -> dict[str, Any] | None:
        """The live backup for *path*, if any -- what "Undo edit" needs."""
        for entry in reversed(self._load()):
            if entry.get("path") == path and not entry.get("restored_at"):
                return entry
        return None

    def mark_restored(self, backed_up_as: str) -> bool:
        if not os.path.exists(self.path):
            return False
        try:
            with self._locked():
                self._invalidate()
                entries = self._load()
                hit = False
                for entry in entries:
                    if (entry.get("backed_up_as") == backed_up_as
                            and not entry.get("restored_at")):
                        entry["restored_at"] = time.time()
                        hit = True
                if hit:
                    self._rewrite(entries)
        except OSError as exc:
            _log.error("cannot update the edit journal at %s: %s", self.path, exc)
            return False
        return hit

    def _append(self, entry: dict[str, Any]) -> None:
        line = json.dumps(entry, ensure_ascii=False, default=str)
        try:
            os.makedirs(self.folder, exist_ok=True)
            with self._locked(), open(self.path, "a", encoding="utf-8") as fh:
                if fh.tell() and not self._ends_with_newline():
                    fh.write("\n")
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:      # never let bookkeeping break an edit
            _log.error("cannot write the edit journal at %s: %s", self.path, exc)
            return
        self._invalidate()

    def _ends_with_newline(self) -> bool:
        try:
            with open(self.path, "rb") as fh:
                fh.seek(-1, os.SEEK_END)
                return fh.read(1) == b"\n"
        except OSError:
            return True

    def _rewrite(self, entries: list[dict[str, Any]]) -> None:
        os.makedirs(self.folder, exist_ok=True)
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                for entry in entries:
                    fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except OSError as exc:
            _log.error("cannot rewrite the edit journal at %s: %s", self.path, exc)
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            return
        self._invalidate()

    def _load(self) -> list[dict[str, Any]]:
        try:
            st = os.stat(self.path)
            key = (st.st_mtime, st.st_size)
        except OSError:
            self._cache = []
            self._cache_key = None
            return []
        if self._cache is not None and self._cache_key == key:
            return self._cache
        rows: list[dict[str, Any]] = []
        try:
            with open(self.path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        _log.warning("skipping a malformed line in %s", self.path)
        except OSError as exc:
            _log.error("cannot read the edit journal at %s: %s", self.path, exc)
        self._cache = rows
        self._cache_key = key
        return rows

    def _invalidate(self) -> None:
        self._cache = None
        self._cache_key = None
