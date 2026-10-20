"""Rotate, crop, undo, and the library grid's "Remove selected" -- the
in-place editing PicFrame.edit_photo/undo_edit/bulk_remove offer.

The thing worth protecting here is the same thing test_removed.py protects
for Remove: a picture's provenance (its date taken, here) must survive the
operation, and an edit must be genuinely undoable -- not "undoable until the
backup folder happens to be cleaned up by something else".
"""

import os

import pytest
from PIL import Image

from picframe3.library.edits import EditLog


def _noop(*args, **kwargs):
    return None


@pytest.fixture
def frame(tmp_path, photo_dir):
    """A PicFrame with a library and no screen, enough for editing."""
    from picframe3.app import PicFrame
    from picframe3.config import Config
    from picframe3.library.db import Library
    from picframe3.library.playlist import Playlist
    from picframe3.library.removed import RemovalLog
    from picframe3.library.scanner import Scanner

    config = Config()
    config.library.picture_folders = [str(photo_dir)]
    config.library.database = str(tmp_path / "library.db3")
    config.library.deleted_folder = str(tmp_path / "deleted")
    config.library.edit_backup_folder = str(tmp_path / "edit-originals")
    app = PicFrame(config)
    app.library = Library(config.library.database)
    app.removals = RemovalLog(config.library.deleted_folder)
    app.edits = EditLog(config.library.edit_backup_folder)
    app.scanner = Scanner(app.library, [str(photo_dir)])
    app.scanner.scan()
    app.playlist = Playlist(app.library)
    app._advance = _noop
    yield app
    app.library.close()


# -- rotate / crop -----------------------------------------------------

@pytest.mark.asyncio
async def test_rotate_swaps_width_and_height(frame, photo_dir):
    path = str(photo_dir / "2023" / "img00.jpg")
    before = frame.library.by_path(path)

    result = await frame.edit_photo(before.id, "rotate", degrees=90)
    assert result["ok"]

    after = frame.library.by_path(path)
    assert (after.width, after.height) == (before.height, before.width)


@pytest.mark.asyncio
async def test_rotate_keeps_the_date_taken_from_exif(frame, photo_dir):
    # exif.jpg carries an orientation tag (6) as well as a date -- the one
    # most likely to go wrong, since the EXIF sub-IFD has to survive both
    # the bake-in-orientation step and the re-save.
    path = str(photo_dir / "2024" / "exif.jpg")
    before = frame.library.by_path(path)
    assert before.taken_at is not None

    result = await frame.edit_photo(before.id, "rotate", degrees=90)
    assert result["ok"]

    after = frame.library.by_path(path)
    assert after.taken_at == before.taken_at


@pytest.mark.asyncio
async def test_crop_shrinks_the_picture(frame, photo_dir):
    path = str(photo_dir / "2023" / "img00.jpg")
    before = frame.library.by_path(path)
    result = await frame.edit_photo(before.id, "crop", box=[0.0, 0.0, 0.5, 0.5])
    assert result["ok"]
    after = frame.library.by_path(path)
    assert after.width < before.width
    assert after.height < before.height


@pytest.mark.asyncio
async def test_rotate_refuses_a_non_right_angle(frame, photo_dir):
    path = str(photo_dir / "2023" / "img00.jpg")
    rec = frame.library.by_path(path)
    result = await frame.edit_photo(rec.id, "rotate", degrees=45)
    assert not result["ok"]
    assert "90" in result["error"]
    # Refused before touching anything.
    assert frame.library.by_path(path).width == rec.width


@pytest.mark.asyncio
async def test_a_video_cannot_be_edited(frame, photo_dir, tmp_path):
    # None of photo_dir's fixtures are videos; fake one by hand.
    import time

    from picframe3.media.metadata import PhotoMeta

    meta = PhotoMeta(path=str(photo_dir / "2024" / "clip.mp4"), is_video=True,
                     width=640, height=480)
    open(meta.path, "wb").close()
    file_id = frame.library.upsert(meta, mtime=time.time(), size=0)
    result = await frame.edit_photo(file_id, "rotate", degrees=90)
    assert not result["ok"]
    assert "video" in result["error"]


# -- undo ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_undo_restores_the_original_bytes_and_size(frame, photo_dir):
    path = str(photo_dir / "2023" / "img00.jpg")
    before = frame.library.by_path(path)
    before_bytes = open(path, "rb").read()

    await frame.edit_photo(before.id, "crop", box=[0.0, 0.0, 0.5, 0.5])
    mid = frame.library.by_path(path)
    assert (mid.width, mid.height) != (before.width, before.height)

    result = await frame.undo_edit(before.id)
    assert result["ok"]
    after = frame.library.by_path(path)
    assert (after.width, after.height) == (before.width, before.height)
    assert open(path, "rb").read() == before_bytes


@pytest.mark.asyncio
async def test_a_second_edit_does_not_make_a_second_backup(frame, photo_dir):
    """Edit twice without undoing in between; Undo must still reach all the
    way back to the very first original, not to the intermediate step."""
    path = str(photo_dir / "2023" / "img00.jpg")
    before = frame.library.by_path(path)
    before_bytes = open(path, "rb").read()

    await frame.edit_photo(before.id, "rotate", degrees=90)
    await frame.edit_photo(before.id, "rotate", degrees=90)
    backups = os.listdir(frame.edits.folder)
    backups = [f for f in backups if not f.startswith("edits.jsonl")]
    assert len(backups) == 1, f"expected exactly one backup file, found {backups}"

    await frame.undo_edit(before.id)
    assert open(path, "rb").read() == before_bytes


@pytest.mark.asyncio
async def test_undo_with_nothing_to_undo_says_so(frame, photo_dir):
    path = str(photo_dir / "2023" / "img00.jpg")
    rec = frame.library.by_path(path)
    result = await frame.undo_edit(rec.id)
    assert not result["ok"]
    assert "nothing" in result["error"].lower()


@pytest.mark.asyncio
async def test_undo_twice_the_second_time_says_so(frame, photo_dir):
    path = str(photo_dir / "2023" / "img00.jpg")
    rec = frame.library.by_path(path)
    await frame.edit_photo(rec.id, "rotate", degrees=90)
    first = await frame.undo_edit(rec.id)
    assert first["ok"]
    second = await frame.undo_edit(rec.id)
    assert not second["ok"]


# -- bulk remove -----------------------------------------------------------

@pytest.mark.asyncio
async def test_bulk_remove_moves_every_requested_picture(frame, photo_dir):
    paths = [str(photo_dir / "2023" / f"img0{i}.jpg") for i in (0, 2)]
    ids = [frame.library.by_path(p).id for p in paths]
    result = await frame.bulk_remove(ids, source="http")
    assert set(result["removed"]) == set(ids)
    assert result["skipped"] == []
    for p in paths:
        assert not os.path.exists(p)
        assert frame.library.by_path(p) is None


@pytest.mark.asyncio
async def test_bulk_remove_skips_a_stale_id_without_failing_the_batch(frame, photo_dir):
    path = str(photo_dir / "2023" / "img00.jpg")
    rec = frame.library.by_path(path)
    result = await frame.bulk_remove([rec.id, 999999], source="http")
    assert result["removed"] == [rec.id]
    assert result["skipped"] == [999999]


@pytest.mark.asyncio
async def test_bulk_remove_writes_a_journal_entry_per_picture(frame, photo_dir):
    paths = [str(photo_dir / "2023" / f"img0{i}.jpg") for i in (0, 2)]
    ids = [frame.library.by_path(p).id for p in paths]
    await frame.bulk_remove(ids, source="http")
    assert len(frame.removals.entries()) == 2
