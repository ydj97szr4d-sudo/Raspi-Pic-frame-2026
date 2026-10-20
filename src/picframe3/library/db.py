"""The photo index: a single SQLite file in WAL mode.

``picframe`` also used SQLite, and that was one of its better decisions -- it
is the right database for a device that has to survive being unplugged.  What
is new here:

* **WAL journalling**, so a scan writing in the background never blocks the
  render loop's reads, and a power cut mid-scan cannot corrupt the index.
* **FTS5 full-text search** over titles, captions, tags and paths, which is
  what makes "show me photos from Lisbon" a query instead of a table scan.
* **Normalised tags**, so filtering by keyword is an index lookup rather than
  a ``LIKE '%...%'`` over a comma-joined string.
* Play counts and last-played timestamps, so the shuffle can actually avoid
  repeating itself across restarts.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from ..media.metadata import PhotoMeta
from ..media.phash import hamming as _hamming64

_log = logging.getLogger(__name__)

SCHEMA_VERSION = 6

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS files (
    id            INTEGER PRIMARY KEY,
    path          TEXT    NOT NULL UNIQUE,
    folder        TEXT    NOT NULL,
    basename      TEXT    NOT NULL,
    ext           TEXT    NOT NULL,
    mtime         REAL    NOT NULL,
    size          INTEGER NOT NULL,
    is_video      INTEGER NOT NULL DEFAULT 0,
    width         INTEGER,
    height        INTEGER,
    orientation   INTEGER DEFAULT 1,
    is_portrait   INTEGER NOT NULL DEFAULT 0,
    taken_at      REAL,
    make          TEXT,
    model         TEXT,
    lens          TEXT,
    f_number      REAL,
    exposure_time TEXT,
    iso           INTEGER,
    focal_length  REAL,
    latitude      REAL,
    longitude     REAL,
    location      TEXT,
    title         TEXT,
    caption       TEXT,
    rating        INTEGER,
    duration      REAL,
    play_count    INTEGER NOT NULL DEFAULT 0,
    last_played   REAL,
    -- The shuffle round in which this picture was last shown.  A round covers
    -- every picture exactly once, so "has it been shown yet this round?" is a
    -- single indexed comparison, and it survives reboots, rescans and new
    -- files arriving over Samba in the middle of a round.
    play_round    INTEGER NOT NULL DEFAULT 0,
    -- Unreadable, so not offered: a truncated download, a HEIC on a frame
    -- without the plugin.  Cleared the moment the file changes on disk.
    hidden        INTEGER NOT NULL DEFAULT 0,
    -- Deliberately kept out of the playlist because this picture was removed
    -- once and has come back.  Nothing clears it but somebody saying so.
    held          INTEGER NOT NULL DEFAULT 0,
    -- SHA-256, filled in only for files that matched something in `holds`.
    digest        TEXT,
    -- The region a "cover" crop must not cut into, to keep a detected face
    -- (and its hair) on screen -- (face_x0, face_y0)-(face_x1, face_y1),
    -- each a 0..1 fraction of the image. NULL until face_checked is set;
    -- still NULL after that means detection ran and found nothing (or is
    -- switched off), which is exactly the state that should fall back to
    -- the frame's ordinary fixed centering, not be retried on every scan.
    face_x0       REAL,
    face_y0       REAL,
    face_x1       REAL,
    face_y1       REAL,
    face_checked  INTEGER NOT NULL DEFAULT 0,
    -- A difference-hash of the picture, for "find similar" -- see
    -- media.phash.  NULL until phash_checked is set; still NULL after that
    -- means the file could not be decoded (or is a video), the same
    -- "looked, found nothing" distinction face_checked makes.
    phash         INTEGER,
    phash_checked INTEGER NOT NULL DEFAULT 0,
    indexed_at    REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS files_folder  ON files(folder);
CREATE INDEX IF NOT EXISTS files_taken   ON files(taken_at);
CREATE INDEX IF NOT EXISTS files_played  ON files(last_played);

CREATE TABLE IF NOT EXISTS tags (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE
);
CREATE TABLE IF NOT EXISTS file_tags (
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    tag_id  INTEGER NOT NULL REFERENCES tags(id)  ON DELETE CASCADE,
    PRIMARY KEY (file_id, tag_id)
);
CREATE INDEX IF NOT EXISTS file_tags_tag ON file_tags(tag_id);

CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(
    path, title, caption, tags, location, tokenize='unicode61 remove_diacritics 2'
);

-- Pictures that were removed, remembered by their content rather than by
-- their path.  A removal has to outlive the file coming back: Syncthing
-- re-copies it, a backup is restored over the folder, the same photograph
-- turns up again under another name.  Matching on `size` first is what keeps
-- this cheap -- the size comes free with the scan's own stat, and only a file
-- whose size is in this table is ever hashed.
--
-- A row is deleted when, and only when, somebody says the picture may be
-- shown again.
CREATE TABLE IF NOT EXISTS holds (
    digest     TEXT    PRIMARY KEY,
    size       INTEGER NOT NULL,
    path       TEXT    NOT NULL,
    basename   TEXT    NOT NULL,
    stored_as  TEXT    NOT NULL DEFAULT '',
    held_at    REAL    NOT NULL,
    seen_at    REAL,
    seen_path  TEXT    NOT NULL DEFAULT '',
    seen_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS holds_size ON holds(size);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

def like_escape(text: str) -> str:
    """Literal text as a LIKE pattern fragment.

    ``%`` and ``_`` are wildcards to LIKE, and a folder called ``2024_Italien``
    therefore matched ``2024xItalien`` as well -- rarely, confusingly, and only
    for the people whose folders are named that way.  The backslash has to be
    escaped first, and every query using this must say ``ESCAPE '\\'``.
    """
    return (str(text or "").replace("\\", "\\\\")
            .replace("%", "\\%").replace("_", "\\_"))


def fts_query(text: str) -> str:
    """An FTS5 MATCH expression for whatever somebody typed into a search box.

    Every word becomes a quoted prefix term, so "lis" still finds Lisbon.  The
    quoting is the part that matters: a double quote in the typed text would
    otherwise close the string literal FTS5 is parsing and leave it with an
    unterminated one.  That is an ``OperationalError``, not an empty result --
    and because the frame persists its filters, the error came back on every
    single start afterwards.  Doubling the quote inside the term is the escape
    FTS5 understands, so ``dog "`` is simply a search for a dog and a quote.
    """
    terms = [t.replace('"', '""') for t in str(text or "").split()]
    return " ".join(f'"{t}"*' for t in terms if t.strip('"'))


_COLUMNS = (
    "id", "path", "folder", "basename", "ext", "mtime", "size", "is_video",
    "width", "height", "orientation", "is_portrait", "taken_at", "make", "model",
    "lens", "f_number", "exposure_time", "iso", "focal_length", "latitude",
    "longitude", "location", "title", "caption", "rating", "duration",
    "play_count", "last_played", "hidden", "held", "digest",
    "face_x0", "face_y0", "face_x1", "face_y1", "face_checked",
    "phash", "indexed_at",
)


@dataclass
class Record:
    """One row of ``files``, with the metadata view the renderer needs."""

    id: int
    path: str
    folder: str
    basename: str
    ext: str
    mtime: float
    size: int
    is_video: bool
    width: int | None
    height: int | None
    orientation: int
    is_portrait: bool
    taken_at: float | None
    make: str | None
    model: str | None
    lens: str | None
    f_number: float | None
    exposure_time: str | None
    iso: int | None
    focal_length: float | None
    latitude: float | None
    longitude: float | None
    location: str | None
    title: str | None
    caption: str | None
    rating: int | None
    duration: float | None
    play_count: int
    last_played: float | None
    hidden: bool
    held: bool
    digest: str | None
    face_x0: float | None
    face_y0: float | None
    face_x1: float | None
    face_y1: float | None
    face_checked: bool
    phash: int | None
    indexed_at: float
    tags: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.tags is None:
            self.tags = []

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Record:
        return cls(**{k: row[k] for k in _COLUMNS})

    def as_meta(self) -> PhotoMeta:
        return PhotoMeta(
            path=self.path,
            width=self.width or 0,
            height=self.height or 0,
            orientation=self.orientation or 1,
            taken_at=self.taken_at,
            make=self.make,
            model=self.model,
            lens=self.lens,
            f_number=self.f_number,
            exposure_time=self.exposure_time,
            iso=self.iso,
            focal_length=self.focal_length,
            latitude=self.latitude,
            longitude=self.longitude,
            title=self.title,
            caption=self.caption,
            tags=list(self.tags or []),
            rating=self.rating,
            is_video=bool(self.is_video),
            duration=self.duration,
            face_box=(
                (self.face_x0, self.face_y0, self.face_x1, self.face_y1)
                if None not in (self.face_x0, self.face_y0, self.face_x1, self.face_y1)
                else None
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        d = {k: getattr(self, k) for k in _COLUMNS}
        d["tags"] = list(self.tags or [])
        return d


class Library:
    def __init__(self, path: str):
        self.path = os.path.expanduser(path)
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._local = threading.local()
        #: Every connection handed out, so that close() can close them all and
        #: not just the one belonging to whichever thread happens to call it.
        self._conns: list[sqlite3.Connection] = []
        self._conns_lock = threading.Lock()
        #: What the last refused prune looked like, so the same refusal twice
        #: in a row is taken as a real deletion rather than a mount that keeps
        #: failing.  See prune_missing().
        self._prune_refused: tuple[int, int] | None = None
        # Not wrapped in transaction(): the schema script sets journal_mode,
        # which SQLite refuses inside a transaction, and executescript commits
        # whatever is open before it runs anyway.
        conn = self.connect()
        conn.executescript(_SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self._migrate()

    # -- connections -------------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        """One connection per thread; SQLite objects are not thread-safe."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            # check_same_thread=False is safe here *because* of the one
            # connection per thread rule above -- no two threads ever touch the
            # same object.  It is switched off only so that close() may close a
            # connection that belongs to a thread which has already finished.
            conn = sqlite3.connect(self.path, timeout=15.0, isolation_level=None,
                                   check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=15000")
            conn.create_function("hamming64", 2, _hamming64, deterministic=True)
            self._local.conn = conn
            with self._conns_lock:
                self._conns.append(conn)
        return conn

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """A real transaction -- which ``with conn:`` is not on this connection.

        The connection is opened in autocommit mode (``isolation_level=None``),
        and sqlite3's own connection context manager commits the implicit
        transaction it manages, of which there is none in that mode.  Every
        statement was therefore its own transaction: a crash between writing the
        row and writing its tags and search entry left a file indexed with
        neither, and ``needs_reindex()`` compares mtime and size only, so that
        half-written row was never repaired.

        ``BEGIN IMMEDIATE`` takes the write lock up front instead of upgrading
        halfway through, so a scan and the render loop queue rather than one of
        them failing with SQLITE_BUSY after having written half its work.

        Nested uses join the transaction already open rather than starting a
        second one -- that is what lets the scanner wrap a whole batch of
        upserts, each of which opens one itself, into a single commit.
        """
        conn = self.connect()
        depth = getattr(self._local, "depth", 0)
        if depth:
            self._local.depth = depth + 1
            try:
                yield conn
            finally:
                self._local.depth = depth
            return
        conn.execute("BEGIN IMMEDIATE")
        self._local.depth = 1
        try:
            yield conn
        except BaseException:
            self._local.depth = 0
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise
        else:
            self._local.depth = 0
            conn.execute("COMMIT")

    def _migrate(self) -> None:
        conn = self.connect()
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        version = int(row["value"]) if row else 0
        if version > SCHEMA_VERSION:
            _log.warning("index was written by a newer picframe3 (v%d); proceeding read-only-ish",
                         version)
        # v2 added files.play_round.  An index written by v1 has every picture
        # at round 0, which means "not yet shown in the round we are in now" --
        # exactly the right starting state, so there is nothing to backfill.
        # The column is added here rather than in the schema script because
        # CREATE TABLE IF NOT EXISTS will not alter a table that already
        # exists, and the index on it would then fail on every old library.
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(files)")}
        if "play_round" not in columns:
            with self.transaction():
                conn.execute("ALTER TABLE files ADD COLUMN "
                             "play_round INTEGER NOT NULL DEFAULT 0")
            _log.info("index upgraded to schema v2 (added play_round)")
        # v3 added the held-out list: files.held / files.digest and the holds
        # table.  An older index has nothing held, which is the right starting
        # state -- every removal it knew about took its row with it.  Same
        # reason as above for doing it here: CREATE TABLE IF NOT EXISTS does
        # not alter a table that is already there.
        if "held" not in columns:
            with self.transaction():
                conn.execute("ALTER TABLE files ADD COLUMN "
                             "held INTEGER NOT NULL DEFAULT 0")
                conn.execute("ALTER TABLE files ADD COLUMN digest TEXT")
            _log.info("index upgraded to schema v3 (added held, digest)")
        # v4 added face-aware cropping: files.face_cx/face_cy (the anchor) and
        # files.face_checked (has detection run on this file at all).  An
        # older index has every row at face_checked=0, which is exactly
        # "not looked at yet" -- the right starting state for a backfill to
        # work through, same as locations_missing() does for geocoding.
        if "face_checked" not in columns:
            with self.transaction():
                conn.execute("ALTER TABLE files ADD COLUMN face_cx REAL")
                conn.execute("ALTER TABLE files ADD COLUMN face_cy REAL")
                conn.execute("ALTER TABLE files ADD COLUMN "
                             "face_checked INTEGER NOT NULL DEFAULT 0")
            _log.info("index upgraded to schema v4 (added face_cx, face_cy, face_checked)")
        # v5 replaced the single face_cx/face_cy anchor with the whole
        # bounding box (face_x0, face_y0, face_x1, face_y1): knowing just
        # where a face's centre is cannot say whether the ordinary fixed
        # crop would already have kept the whole thing in, which is what
        # media.prepare now checks before it moves the crop at all. The old
        # cx/cy columns are left in place (SQLite ALTER cannot drop a column
        # before a dedicated statement for it, and nothing reads them any
        # more) rather than risk a destructive migration for two unused
        # columns. Every row's face_checked is reset so the backfill looks
        # again and actually fills in the box this time -- a no-op for
        # anyone who had not turned viewer.detect_faces on yet.
        if "face_x0" not in columns:
            with self.transaction():
                conn.execute("ALTER TABLE files ADD COLUMN face_x0 REAL")
                conn.execute("ALTER TABLE files ADD COLUMN face_y0 REAL")
                conn.execute("ALTER TABLE files ADD COLUMN face_x1 REAL")
                conn.execute("ALTER TABLE files ADD COLUMN face_y1 REAL")
                conn.execute("UPDATE files SET face_checked=0")
            _log.info("index upgraded to schema v5 (face box replaces face anchor point)")
        # v6 added the similarity hash: files.phash / files.phash_checked, on
        # the same "not yet looked at" starting state as face_checked above --
        # an older index backfills it the first time it is opened with this
        # version, a few pictures at a time, the same way face detection and
        # geocoding caught up on a library that predated them.
        if "phash_checked" not in columns:
            with self.transaction():
                conn.execute("ALTER TABLE files ADD COLUMN phash INTEGER")
                conn.execute("ALTER TABLE files ADD COLUMN "
                             "phash_checked INTEGER NOT NULL DEFAULT 0")
            _log.info("index upgraded to schema v6 (added phash, phash_checked)")
        with self.transaction():
            conn.execute("CREATE INDEX IF NOT EXISTS files_round ON files(play_round)")
            # Unconditional (unlike the ALTERs above) so a fresh database --
            # which already has face_checked from _SCHEMA and never takes the
            # v4 branch above -- gets the index too.
            conn.execute("CREATE INDEX IF NOT EXISTS files_face_checked "
                         "ON files(face_checked)")
            conn.execute("CREATE INDEX IF NOT EXISTS files_phash_checked "
                         "ON files(phash_checked)")
            # The indices below are the ones the playlist's ORDER BY clauses
            # actually use.  files_hidden and files_portrait never were: both
            # columns hold two values over the whole library, so SQLite reads
            # the table anyway and the index is pure write cost on every scan.
            # Every statement here is idempotent, so this runs on each start
            # and repairs an index that was built by an older version.
            conn.execute("DROP INDEX IF EXISTS files_hidden")
            conn.execute("DROP INDEX IF EXISTS files_portrait")
            conn.execute("CREATE INDEX IF NOT EXISTS files_play_count "
                         "ON files(play_count, last_played)")
            conn.execute("CREATE INDEX IF NOT EXISTS files_basename "
                         "ON files(basename COLLATE NOCASE)")
            conn.execute("CREATE INDEX IF NOT EXISTS files_recent "
                         "ON files(COALESCE(taken_at, mtime))")
        if version < SCHEMA_VERSION:
            with self.transaction():
                conn.execute("INSERT OR REPLACE INTO meta(key, value) "
                             "VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))

    # -- writing -----------------------------------------------------------
    def file_state(self, path: str) -> tuple[float, int] | None:
        """``(mtime, size)`` of an indexed file, or None if it is not indexed.

        The whole of what a scan needs to know about a file it has seen before.
        Asking ``by_path()`` instead costs a full row plus a join over the tags
        only to throw both away, and a scan asks this once per file -- on a
        library of twenty thousand photographs that difference is minutes.
        """
        row = self.connect().execute(
            "SELECT mtime, size FROM files WHERE path=?", (path,)
        ).fetchone()
        return (row["mtime"], row["size"]) if row is not None else None

    @staticmethod
    def is_current(state: tuple[float, int] | None, mtime: float, size: int) -> bool:
        """Whether an indexed file still matches what is on disk."""
        if state is None:
            return False
        return abs(state[0] - mtime) <= 0.5 and state[1] == size

    def needs_reindex(self, path: str, mtime: float, size: int) -> bool:
        return not self.is_current(self.file_state(path), mtime, size)

    def upsert(self, meta: PhotoMeta, *, mtime: float, size: int,
               location: str | None = None, digest: str | None = None,
               held: bool = False) -> int:
        folder = os.path.dirname(meta.path)
        basename = os.path.basename(meta.path)
        ext = os.path.splitext(basename)[1].lower()
        dw, dh = meta.display_size
        values = (
            meta.path, folder, basename, ext, mtime, size, int(meta.is_video),
            meta.width, meta.height, meta.orientation, int(dh > dw),
            meta.taken_at, meta.make, meta.model, meta.lens, meta.f_number,
            meta.exposure_time, meta.iso, meta.focal_length, meta.latitude,
            meta.longitude, location, meta.title, meta.caption, meta.rating,
            meta.duration, int(bool(held)), digest, time.time(),
        )
        # One transaction for the row, its tags and its search entry: a file
        # that is in the index but in neither of the other two is invisible to
        # every tag filter and every search, and nothing would ever notice.
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO files (
                    path, folder, basename, ext, mtime, size, is_video,
                    width, height, orientation, is_portrait, taken_at, make, model,
                    lens, f_number, exposure_time, iso, focal_length, latitude,
                    longitude, location, title, caption, rating, duration,
                    held, digest, indexed_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(path) DO UPDATE SET
                    -- This row only runs for a file that is new or whose bytes
                    -- have changed (see upsert()'s callers), so whatever face
                    -- box was cached for the old bytes is stale -- cleared
                    -- here, the same way `hidden` is, so the backfill gives it
                    -- a fresh look rather than keeping a crop shaped around a
                    -- photograph this file no longer is.
                    face_x0=NULL, face_y0=NULL, face_x1=NULL, face_y1=NULL, face_checked=0,
                    -- Same reasoning, same pattern: new bytes means whatever
                    -- was there before (if anything) is a hash of a picture
                    -- this file no longer is.
                    phash=NULL, phash_checked=0,
                    folder=excluded.folder, basename=excluded.basename, ext=excluded.ext,
                    mtime=excluded.mtime, size=excluded.size, is_video=excluded.is_video,
                    width=excluded.width, height=excluded.height,
                    orientation=excluded.orientation, is_portrait=excluded.is_portrait,
                    taken_at=excluded.taken_at, make=excluded.make, model=excluded.model,
                    lens=excluded.lens, f_number=excluded.f_number,
                    exposure_time=excluded.exposure_time, iso=excluded.iso,
                    focal_length=excluded.focal_length, latitude=excluded.latitude,
                    longitude=excluded.longitude,
                    location=COALESCE(excluded.location, files.location),
                    title=excluded.title, caption=excluded.caption,
                    rating=excluded.rating, duration=excluded.duration,
                    held=excluded.held,
                    digest=COALESCE(excluded.digest, files.digest),
                    -- This statement only runs for a file that is new or whose
                    -- bytes have changed, so a picture hidden for being
                    -- unreadable gets another chance the moment somebody
                    -- replaces it -- which is exactly when the HEIC plugin has
                    -- just been installed or the truncated download finished.
                    hidden=0,
                    indexed_at=excluded.indexed_at
                """,
                values,
            )
            file_id = conn.execute("SELECT id FROM files WHERE path=?", (meta.path,)).fetchone()["id"]
            self._set_tags(conn, file_id, meta.tags)
            self._index_search(conn, file_id, meta, location)
        return file_id

    @staticmethod
    def _set_tags(conn: sqlite3.Connection, file_id: int, tags: Sequence[str]) -> None:
        conn.execute("DELETE FROM file_tags WHERE file_id=?", (file_id,))
        for tag in {t.strip() for t in tags if t and t.strip()}:
            conn.execute("INSERT OR IGNORE INTO tags(name) VALUES (?)", (tag,))
            row = conn.execute("SELECT id FROM tags WHERE name=?", (tag,)).fetchone()
            conn.execute(
                "INSERT OR IGNORE INTO file_tags(file_id, tag_id) VALUES (?,?)",
                (file_id, row["id"]),
            )

    @staticmethod
    def _index_search(conn: sqlite3.Connection, file_id: int, meta: PhotoMeta,
                      location: str | None) -> None:
        conn.execute("DELETE FROM search WHERE rowid=?", (file_id,))
        conn.execute(
            "INSERT INTO search(rowid, path, title, caption, tags, location) VALUES (?,?,?,?,?,?)",
            (file_id, meta.path, meta.title or "", meta.caption or "",
             " ".join(meta.tags or ()), location or ""),
        )

    def set_location(self, file_id: int, location: str | None) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE files SET location=? WHERE id=?", (location, file_id))
            conn.execute("UPDATE search SET location=? WHERE rowid=?", (location or "", file_id))

    def mark_played(self, file_id: int, play_round: int | None = None) -> None:
        """Record that a picture has just been shown.

        ``play_round`` is the shuffle round it was shown in; passing it is what
        lets the playlist answer "which pictures are still owed a turn?" from
        the index rather than from an in-memory cursor that a reboot or a
        rescan would throw away.
        """
        with self.transaction() as conn:
            if play_round is None:
                conn.execute(
                    "UPDATE files SET play_count=play_count+1, last_played=? WHERE id=?",
                    (time.time(), file_id),
                )
            else:
                conn.execute(
                    "UPDATE files SET play_count=play_count+1, last_played=?, "
                    "play_round=? WHERE id=?",
                    (time.time(), play_round, file_id),
                )

    def mark_round(self, file_ids: Iterable[int], play_round: int) -> None:
        """Stamp the pictures the playlist has just handed out for this round.

        Separate from :meth:`mark_played` on purpose: this is bookkeeping for
        the shuffle and must happen exactly when the playlist advances, while
        play_count is about what a viewer actually saw.
        """
        ids = list(file_ids)
        if not ids:
            return
        with self.transaction() as conn:
            conn.executemany("UPDATE files SET play_round=? WHERE id=?",
                             [(play_round, i) for i in ids])

    def play_stats(self) -> dict[str, float]:
        """How evenly the library is actually being shown."""
        row = self.connect().execute(
            "SELECT COUNT(*) AS n, MIN(play_count) AS lo, MAX(play_count) AS hi, "
            "AVG(play_count) AS avg, SUM(play_count = 0) AS never "
            "FROM files WHERE hidden = 0 AND held = 0"
        ).fetchone()
        return {
            "files": row["n"] or 0,
            "min": row["lo"] or 0,
            "max": row["hi"] or 0,
            "average": round(row["avg"] or 0.0, 2),
            "never_shown": row["never"] or 0,
        }

    def set_hidden(self, file_id: int, hidden: bool = True) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE files SET hidden=? WHERE id=?", (int(hidden), file_id))

    # -- held-out pictures -------------------------------------------------
    # A removal that only deletes a row is undone by any copy of the file
    # landing back in the folder, and on a frame fed by Syncthing, a backup or
    # a USB stick that happens.  So a removal is also remembered by the
    # picture's content, and nothing but an explicit "show this again" clears
    # it.  See :mod:`picframe3.library.digest` for why matching starts at the
    # size rather than at the hash.

    def hold(self, digest: str, *, size: int, path: str, stored_as: str = "",
             when: float | None = None) -> None:
        """Keep this picture out of the playlist until somebody says otherwise.

        Idempotent, and deliberately not an ``INSERT OR REPLACE``: removing the
        same photograph twice must not lose the sightings recorded against the
        first removal.
        """
        if not digest:
            return
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO holds (digest, size, path, basename, stored_as, held_at) "
                "VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(digest) DO UPDATE SET "
                "  size=excluded.size, path=excluded.path, "
                "  basename=excluded.basename, stored_as=excluded.stored_as, "
                "  held_at=excluded.held_at",
                (digest, int(size), path, os.path.basename(path), stored_as or "",
                 when if when is not None else time.time()),
            )
            # Any copy already in the index goes out of the playlist now,
            # rather than at the next scan.  A second copy of the same
            # photograph under another name is exactly the case this exists
            # for, and it is already indexed.
            conn.execute("UPDATE files SET held=1 WHERE digest=?", (digest,))

    def release(self, digest: str) -> bool:
        """Let it be shown again.  The one thing that clears a hold."""
        if not digest:
            return False
        with self.transaction() as conn:
            cur = conn.execute("DELETE FROM holds WHERE digest=?", (digest,))
            conn.execute("UPDATE files SET held=0 WHERE digest=?", (digest,))
        return cur.rowcount > 0

    def files_sized(self, size: int) -> list[tuple[int, str]]:
        """Indexed files that weigh exactly this much.

        For the case of two copies of one photograph in the library: removing
        one has to take the other off the wall too, and waiting for the next
        scan to notice would leave the picture you just removed on the wall for
        an hour.  Sized first, as everywhere else, so the caller hashes a
        couple of files rather than the library.
        """
        return [(r["id"], r["path"]) for r in self.connect().execute(
            "SELECT id, path FROM files WHERE size=? AND held=0", (int(size),))]

    def mark_held(self, file_id: int, digest: str) -> None:
        with self.transaction() as conn:
            conn.execute("UPDATE files SET held=1, digest=? WHERE id=?",
                         (digest, file_id))

    def held_sizes(self) -> set[int]:
        """The file sizes worth hashing.  Read once per scan, not per file."""
        return {r["size"] for r in self.connect().execute("SELECT size FROM holds")}

    def hold_for(self, digest: str) -> sqlite3.Row | None:
        if not digest:
            return None
        return self.connect().execute(
            "SELECT * FROM holds WHERE digest=?", (digest,)).fetchone()

    def hold_by_stored_as(self, stored_as: str) -> sqlite3.Row | None:
        """The hold that belongs to one line of the removal journal."""
        if not stored_as:
            return None
        return self.connect().execute(
            "SELECT * FROM holds WHERE stored_as=? ORDER BY held_at DESC LIMIT 1",
            (stored_as,)).fetchone()

    def note_seen(self, digest: str, path: str, when: float | None = None) -> None:
        """Record that a held-out picture has turned up on disk again."""
        with self.transaction() as conn:
            conn.execute(
                "UPDATE holds SET seen_at=?, seen_path=?, seen_count=seen_count+1 "
                "WHERE digest=?",
                (when if when is not None else time.time(), path, digest),
            )

    def holds(self, *, came_back_only: bool = False) -> list[dict[str, Any]]:
        rows = self.connect().execute(
            "SELECT * FROM holds "
            + ("WHERE seen_count > 0 " if came_back_only else "")
            + "ORDER BY COALESCE(seen_at, held_at) DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def hold_summary(self) -> dict[str, Any]:
        """What the state document and Home Assistant read.

        ``came_back`` is the number that matters to somebody watching: it is
        zero on a frame where removal simply works, and it stops being zero the
        moment something -- nearly always a two-way sync -- keeps pushing a
        removed photograph back onto the disk.
        """
        row = self.connect().execute(
            "SELECT COUNT(*) AS n, "
            "SUM(CASE WHEN seen_count > 0 THEN 1 ELSE 0 END) AS back "
            "FROM holds"
        ).fetchone()
        last = self.connect().execute(
            "SELECT basename, seen_path, seen_at FROM holds "
            "WHERE seen_count > 0 ORDER BY seen_at DESC LIMIT 1"
        ).fetchone()
        return {
            "held": row["n"] or 0,
            "came_back": row["back"] or 0,
            "last_came_back": (last["basename"] if last else "") or "",
            "last_came_back_path": (last["seen_path"] if last else "") or "",
            "last_came_back_at": (last["seen_at"] if last else None),
        }

    def forget(self, paths: Iterable[str]) -> int:
        removed = 0
        with self.transaction() as conn:
            for path in paths:
                row = conn.execute("SELECT id FROM files WHERE path=?", (path,)).fetchone()
                if row is None:
                    continue
                conn.execute("DELETE FROM search WHERE rowid=?", (row["id"],))
                conn.execute("DELETE FROM files WHERE id=?", (row["id"],))
                removed += 1
        return removed

    def forget_under(self, folder: str) -> int:
        """Forget everything below a directory that has gone.

        A deleted directory arrives as one event about the directory itself,
        and forgetting only that path leaves every photograph that was inside
        it in the index -- shown as a missing file until the next full scan.
        The pattern is escaped because a real folder may well contain ``_``.
        """
        prefix = folder.rstrip("/") + "/"
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT id FROM files WHERE path LIKE ? ESCAPE '\\'",
                (like_escape(prefix) + "%",),
            ).fetchall()
            for row in rows:
                conn.execute("DELETE FROM search WHERE rowid=?", (row["id"],))
                conn.execute("DELETE FROM files WHERE id=?", (row["id"],))
        return len(rows)

    def prune_missing(self, roots: Sequence[str], *,
                      max_fraction: float = 0.2) -> int:
        """Drop rows whose file has gone, restricted to the scanned roots.

        ``max_fraction`` is the safety catch.  A Samba or NFS share that failed
        to mount looks exactly like somebody having deleted every photograph on
        it, and pruning takes the play counts, the rounds, the hidden flags and
        the geocoded place names with the rows -- work of months, gone in one
        pass over an empty mount point.  Nothing sane deletes a fifth of a
        library between two scans, so above that share the first scan refuses
        and says so.

        The *second* consecutive scan that sees the same thing goes ahead.  A
        mount problem is over by then -- the share is back and the files are
        there again -- whereas somebody who really did delete a year of
        photographs would otherwise be left with an index that refuses to
        forget them for ever, and a slideshow walking into the gaps one
        missing file at a time.
        """
        conn = self.connect()
        gone: list[str] = []
        indexed = 0
        for row in conn.execute("SELECT path FROM files"):
            path = row["path"]
            if roots and not any(path.startswith(r) for r in roots):
                continue
            indexed += 1
            if not os.path.exists(path):
                gone.append(path)
        if not gone:
            return 0
        share = len(gone) / indexed if indexed else 1.0
        signature = (len(gone), indexed)
        if 0 < max_fraction < share and self._prune_refused != signature:
            self._prune_refused = signature
            _log.warning(
                "refusing to prune: %d of %d indexed files under the scanned "
                "folders (%.0f%%) have vanished at once -- that looks like a "
                "mount problem, not a deletion; the index is left untouched. "
                "If they really are gone, the next scan will remove them",
                len(gone), indexed, share * 100,
            )
            return 0
        self._prune_refused = None
        _log.info("removing %d indexed files that no longer exist", len(gone))
        return self.forget(gone)

    # -- reading -----------------------------------------------------------
    def get(self, file_id: int) -> Record | None:
        row = self.connect().execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        return self._hydrate(row) if row else None

    def by_path(self, path: str) -> Record | None:
        row = self.connect().execute("SELECT * FROM files WHERE path=?", (path,)).fetchone()
        return self._hydrate(row) if row else None

    def _hydrate(self, row: sqlite3.Row) -> Record:
        rec = Record.from_row(row)
        rec.tags = [
            r["name"] for r in self.connect().execute(
                "SELECT t.name FROM tags t JOIN file_tags ft ON ft.tag_id=t.id "
                "WHERE ft.file_id=? ORDER BY t.name",
                (rec.id,),
            )
        ]
        return rec

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[Record]:
        return [self._hydrate(r) for r in self.connect().execute(sql, params)]

    def count(self, where: str = "hidden=0 AND held=0", params: Sequence[Any] = ()) -> int:
        return self.connect().execute(
            f"SELECT COUNT(*) AS n FROM files WHERE {where}", params
        ).fetchone()["n"]

    def folders(self) -> list[tuple[str, int]]:
        return [
            (r["folder"], r["n"])
            for r in self.connect().execute(
                "SELECT folder, COUNT(*) AS n FROM files WHERE hidden=0 AND held=0 "
                "GROUP BY folder ORDER BY folder"
            )
        ]

    def all_tags(self) -> list[tuple[str, int]]:
        return [
            (r["name"], r["n"])
            for r in self.connect().execute(
                "SELECT t.name, COUNT(*) AS n FROM tags t "
                "JOIN file_tags ft ON ft.tag_id=t.id "
                "JOIN files f ON f.id=ft.file_id AND f.hidden=0 AND f.held=0 "
                "GROUP BY t.name ORDER BY n DESC, t.name"
            )
        ]

    def locations(self) -> list[tuple[str, int]]:
        """Every place name in the index, commonest first.

        What the location filter offers as suggestions.  Grouped by the whole
        name rather than by town, because the filter matches any part of it:
        picking "Carteret, Normandy, France" and then shortening it by hand to
        "France" is how somebody widens a filter without knowing the schema.
        """
        return [
            (r["location"], r["n"])
            for r in self.connect().execute(
                "SELECT location, COUNT(*) AS n FROM files "
                "WHERE hidden=0 AND held=0 AND location IS NOT NULL AND location != '' "
                "GROUP BY location ORDER BY n DESC, location"
            )
        ]

    def locations_missing(self, limit: int = 100) -> list[tuple[int, float, float]]:
        """Rows that have coordinates but no place name yet.

        Geocoding is deliberately decoupled from indexing: a scan must not be
        held up by a rate-limited network service, and turning geocoding on
        later has to be able to fill in everything already indexed.
        """
        rows = self.connect().execute(
            "SELECT id, latitude, longitude FROM files "
            "WHERE latitude IS NOT NULL AND longitude IS NOT NULL "
            "AND (location IS NULL OR location = '') "
            "ORDER BY COALESCE(taken_at, mtime) DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [(r["id"], r["latitude"], r["longitude"]) for r in rows]

    def with_position(self, limit: int = 5000) -> list[tuple[int, float, float]]:
        """Every row that has coordinates, place name or not.

        Used when the wording of place names changes -- the raw Nominatim
        replies are already in the geocache, so every caption can be rewritten
        from disk without a single new request.
        """
        rows = self.connect().execute(
            "SELECT id, latitude, longitude FROM files "
            "WHERE latitude IS NOT NULL AND longitude IS NOT NULL LIMIT ?",
            (limit,),
        ).fetchall()
        return [(r["id"], r["latitude"], r["longitude"]) for r in rows]

    def faces_missing(self, limit: int = 25) -> list[tuple[int, str]]:
        """Pictures that have not had a face search run on them yet.

        Decoupled from indexing for the same reason geocoding is: scanning
        must not wait on a per-photo cascade pass, and turning face-aware
        cropping on later has to be able to work through everything already
        indexed. Videos are excluded -- there is no single frame to search --
        and so are hidden and held-out files, which will never be shown.
        """
        rows = self.connect().execute(
            "SELECT id, path FROM files WHERE face_checked=0 "
            "AND is_video=0 AND hidden=0 AND held=0 "
            "ORDER BY COALESCE(taken_at, mtime) DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [(r["id"], r["path"]) for r in rows]

    def set_face_box(self, file_id: int,
                     box: tuple[float, float, float, float] | None) -> None:
        """Record a face search's result -- found, or confirmed empty.

        Called exactly once per file per set of bytes (see upsert()'s reset of
        face_checked on change): ``box=None`` means the search ran and found
        nothing, which is recorded the same as a found one so the backfill
        does not keep retrying a photograph with nobody in it.
        """
        x0, y0, x1, y1 = box if box is not None else (None, None, None, None)
        with self.transaction() as conn:
            conn.execute(
                "UPDATE files SET face_x0=?, face_y0=?, face_x1=?, face_y1=?, "
                "face_checked=1 WHERE id=?",
                (x0, y0, x1, y1, file_id),
            )

    def count_faces_missing(self) -> int:
        return self.connect().execute(
            "SELECT COUNT(*) AS n FROM files WHERE face_checked=0 "
            "AND is_video=0 AND hidden=0 AND held=0"
        ).fetchone()["n"]

    def phash_missing(self, limit: int = 25) -> list[tuple[int, str]]:
        """Pictures that have not had a similarity hash computed yet.

        Same shape as faces_missing(): videos are excluded (there is no
        single frame to hash), and so are hidden and held-out files, which
        will never be offered as "similar to this" or as a result either.
        """
        rows = self.connect().execute(
            "SELECT id, path FROM files WHERE phash_checked=0 "
            "AND is_video=0 AND hidden=0 AND held=0 "
            "ORDER BY COALESCE(taken_at, mtime) DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [(r["id"], r["path"]) for r in rows]

    def count_phash_missing(self) -> int:
        return self.connect().execute(
            "SELECT COUNT(*) AS n FROM files WHERE phash_checked=0 "
            "AND is_video=0 AND hidden=0 AND held=0"
        ).fetchone()["n"]

    def set_phash(self, file_id: int, value: int | None) -> None:
        """Record a similarity hash's result -- found, or confirmed empty.

        ``value=None`` is recorded the same as a found hash (phash_checked=1
        either way), the same reason set_face_box() does: a picture that
        could not be decoded should not be retried on every backfill pass
        for the rest of the library's life.
        """
        with self.transaction() as conn:
            conn.execute(
                "UPDATE files SET phash=?, phash_checked=1 WHERE id=?",
                (value, file_id),
            )

    def similar(self, file_id: int, limit: int = 60) -> list[Record]:
        """The pictures whose hash is closest to this one's, closest first.

        A full scan of every hashed row, ranked through the hamming64()
        function registered in connect() -- fine at the few thousand rows a
        picture frame's library actually holds (each comparison is a handful
        of machine instructions), and simpler than maintaining a second index
        structure for a feature that is only ever asked about one photograph
        at a time. Excludes videos, hidden and held-out files, and the
        picture itself; a hash this picture does not have (not yet computed,
        or never could be) means there is nothing to rank it against.
        """
        row = self.connect().execute(
            "SELECT phash FROM files WHERE id=?", (file_id,)
        ).fetchone()
        if row is None or row["phash"] is None:
            return []
        rows = self.connect().execute(
            "SELECT *, hamming64(phash, ?) AS distance FROM files "
            "WHERE phash IS NOT NULL AND id != ? "
            "AND is_video=0 AND hidden=0 AND held=0 "
            "ORDER BY distance ASC, COALESCE(taken_at, mtime) DESC LIMIT ?",
            (row["phash"], file_id, limit),
        ).fetchall()
        return [self._hydrate(r) for r in rows]

    def count_locations_missing(self) -> int:
        return self.connect().execute(
            "SELECT COUNT(*) AS n FROM files WHERE latitude IS NOT NULL "
            "AND longitude IS NOT NULL AND (location IS NULL OR location = '')"
        ).fetchone()["n"]

    def clear_locations(self) -> int:
        """Forget every resolved place name, so they are looked up again."""
        with self.transaction() as conn:
            cur = conn.execute("UPDATE files SET location = NULL WHERE location IS NOT NULL")
            count = cur.rowcount
            conn.execute("UPDATE search SET location = ''")
        return count

    def search(self, text: str, limit: int = 200, *,
               taken_from: float | None = None,
               taken_to: float | None = None) -> list[Record]:
        """Title, caption, tags, place and filename -- plus, if given, a
        "taken on" range, so a name search and a date range narrow the same
        result together rather than one replacing the other."""
        if not text.strip():
            return []
        query = fts_query(text)
        if not query:
            return []
        clauses = ["search MATCH ?", "f.hidden=0", "f.held=0"]
        params: list[Any] = [query]
        if taken_from is not None:
            clauses.append("COALESCE(f.taken_at, f.mtime) >= ?")
            params.append(taken_from)
        if taken_to is not None:
            clauses.append("COALESCE(f.taken_at, f.mtime) <= ?")
            params.append(taken_to)
        params.append(limit)
        try:
            rows = self.connect().execute(
                "SELECT f.* FROM search s JOIN files f ON f.id=s.rowid "
                f"WHERE {' AND '.join(clauses)} ORDER BY rank LIMIT ?",
                params,
            ).fetchall()
        except sqlite3.OperationalError as exc:
            _log.debug("search query rejected: %s", exc)
            return []
        return [self._hydrate(r) for r in rows]

    def by_date(self, taken_from: float | None, taken_to: float | None,
               limit: int = 200, offset: int = 0) -> list[Record]:
        """Plain newest-first browsing narrowed to a "taken on" range, for
        when there is a date but no name or tag to search for."""
        clauses = ["hidden=0"]
        params: list[Any] = []
        if taken_from is not None:
            clauses.append("COALESCE(taken_at, mtime) >= ?")
            params.append(taken_from)
        if taken_to is not None:
            clauses.append("COALESCE(taken_at, mtime) <= ?")
            params.append(taken_to)
        params.extend([limit, offset])
        return self.query(
            f"SELECT * FROM files WHERE {' AND '.join(clauses)} "
            "ORDER BY COALESCE(taken_at, mtime) DESC LIMIT ? OFFSET ?",
            params,
        )

    def stats(self) -> dict[str, Any]:
        conn = self.connect()
        row = conn.execute(
            "SELECT COUNT(*) AS files, "
            "SUM(is_video) AS videos, "
            "SUM(CASE WHEN hidden=1 THEN 1 ELSE 0 END) AS hidden, "
            "SUM(CASE WHEN held=1 THEN 1 ELSE 0 END) AS held, "
            "MIN(taken_at) AS oldest, MAX(taken_at) AS newest, "
            "SUM(size) AS bytes, "
            "MIN(CASE WHEN hidden=0 AND held=0 THEN play_count END) AS shown_min, "
            "MAX(CASE WHEN hidden=0 AND held=0 THEN play_count END) AS shown_max, "
            "SUM(CASE WHEN hidden=0 AND held=0 AND play_count=0 THEN 1 ELSE 0 END) "
            "  AS never_shown "
            "FROM files"
        ).fetchone()
        return {
            "files": row["files"] or 0,
            "videos": row["videos"] or 0,
            "hidden": row["hidden"] or 0,
            "held": row["held"] or 0,
            "shown_min": row["shown_min"] or 0,
            "shown_max": row["shown_max"] or 0,
            "never_shown": row["never_shown"] or 0,
            "oldest": row["oldest"],
            "newest": row["newest"],
            "bytes": row["bytes"] or 0,
            "folders": len(self.folders()),
            "tags": len(self.all_tags()),
            "db_path": self.path,
            "db_bytes": os.path.getsize(self.path) if os.path.exists(self.path) else 0,
        }

    # -- key/value ---------------------------------------------------------
    def set_state(self, key: str, value: Any) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)",
                (key, json.dumps(value)),
            )

    def get_state(self, key: str, default: Any = None) -> Any:
        row = self.connect().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return row["value"]

    def close(self) -> None:
        """Close every connection this library ever handed out.

        Closing only the calling thread's connection -- which is what this did
        -- leaves the scanner's and the web server's open, so the WAL file
        stays behind and a test's temporary directory cannot be removed on
        Windows.  The connections are closed from whichever thread calls this,
        which is why they are opened with check_same_thread=False.
        """
        with self._conns_lock:
            conns, self._conns = self._conns, []
        for conn in conns:
            try:
                conn.close()
            except sqlite3.Error as exc:  # pragma: no cover - defensive
                _log.debug("could not close a library connection: %s", exc)
        self._local.conn = None
        self._local.depth = 0
