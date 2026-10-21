"""media.fontlib: the named catalog every overlay widget's font setting
picks from. The thing worth protecting is that the catalog and the
settings page agree on what's choosable, and that resolve() never raises
or returns a path that doesn't exist -- a bad font choice should fall back
quietly, not take a redraw down with it.
"""

import os

from picframe3.media import fontlib


def test_choices_starts_with_empty_then_is_sorted():
    assert fontlib.CHOICES[0] == ""
    names = fontlib.CHOICES[1:]
    assert list(names) == sorted(names)
    assert len(names) == len(fontlib._LIBRARY)


def test_resolve_of_empty_or_unknown_name_is_none():
    assert fontlib.resolve("") is None
    assert fontlib.resolve(None) is None
    assert fontlib.resolve("Comic Sans MS") is None


def test_resolve_never_returns_a_missing_path():
    # Whatever is actually installed in this environment -- the point is
    # that a returned path is always real, never a guess.
    for name in fontlib.CHOICES:
        resolved = fontlib.resolve(name)
        if resolved is not None:
            assert os.path.exists(resolved)


def test_every_catalog_entry_points_somewhere_under_a_known_debian_package():
    # Guards the comment in fontlib.py promising DEBIAN_PACKAGES covers the
    # catalog -- a path that belongs to neither "dejavu", "liberation" nor
    # "noto" would mean a package install.sh never installs.
    package_hints = ("dejavu", "liberation", "noto", "freefont")
    for name, paths in fontlib._LIBRARY.items():
        for path in paths:
            assert any(hint in path for hint in package_hints), \
                f"{name!r}'s path {path!r} doesn't look covered by {fontlib.DEBIAN_PACKAGES}"
