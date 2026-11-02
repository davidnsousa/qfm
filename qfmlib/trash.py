"""The trash (freedesktop spec): browsing ~/.local/share/Trash/files with the original names,
restoring, emptying, and undoing a move to the trash (through gvfs's trash:///, which also
covers the trash folders of other disks)."""
import os
import shutil
from datetime import datetime
from urllib.parse import unquote

from gi.repository import Gio, GLib

TRASH = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "Trash")
FILES = os.path.join(TRASH, "files")
INFO = os.path.join(TRASH, "info")


def in_trash(path):
    return path == FILES or path.startswith(FILES + os.sep)


def is_top(path):
    """Something put in the trash itself (not a file inside a trashed folder)."""
    return os.path.dirname(path) == FILES


def info(path):
    """(original path, deletion time or None) for an item at the top of the trash, or None."""
    try:
        with open(os.path.join(INFO, os.path.basename(path) + ".trashinfo"), encoding="utf-8") as f:
            fields = dict(line.strip().split("=", 1) for line in f if "=" in line)
    except OSError:
        return None
    original = unquote(fields.get("Path", ""))
    if not original:
        return None
    if not original.startswith("/"):
        original = os.path.join(os.path.dirname(TRASH), original)
    try:
        deleted = datetime.fromisoformat(fields.get("DeletionDate", "")).timestamp()
    except ValueError:
        deleted = None
    return original, deleted


def is_empty():
    try:
        return not os.listdir(FILES)
    except OSError:
        return True


def name_original(entries):
    """Show trashed items under their original names (the trash may have renamed them: notes.2.txt)."""
    for e in entries:
        found = info(e.path)
        if found:
            e.display = os.path.basename(found[0]) or e.display
            e.key = GLib.utf8_collate_key_for_filename(e.display, -1)


def restore(paths, unique):
    """Put items from the top of the trash back where they were (renamed if that's taken now);
    returns [(original folder, name it got)]."""
    done = []
    for path in paths:
        found = info(path)
        if not found:
            raise OSError(f"{os.path.basename(path)}: where it came from isn't recorded")
        folder, name = os.path.split(found[0])
        os.makedirs(folder, exist_ok=True)
        name = unique(folder, name, os.path.isdir(path))
        shutil.move(path, os.path.join(folder, name))
        try:
            os.remove(os.path.join(INFO, os.path.basename(path) + ".trashinfo"))
        except OSError:
            pass
        done.append((folder, name))
    return done


def empty():
    """Delete everything in the trash for good."""
    for folder in (FILES, INFO, os.path.join(TRASH, "expunged")):
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            path = os.path.join(folder, name)
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                try:
                    os.remove(path)
                except OSError:
                    pass


def untrash(original):
    """Undo a move to the trash: put the most recently trashed item that came from original
    back. Any trash (home or another disk's), through gvfs. Raises GLib.Error / OSError."""
    trash_root = Gio.File.new_for_uri("trash:///")
    newest = None
    for item in trash_root.enumerate_children("standard::name,trash::orig-path,trash::deletion-date",
                                              Gio.FileQueryInfoFlags.NONE, None):
        if item.get_attribute_byte_string("trash::orig-path") == original:
            when = item.get_attribute_string("trash::deletion-date") or ""
            if newest is None or when > newest[0]:
                newest = (when, item.get_name())
    if newest is None:
        raise OSError(f"{os.path.basename(original)} isn't in the trash any more")
    if os.path.lexists(original):
        raise OSError(f"{os.path.basename(original)} exists again where it was")
    trash_root.get_child(newest[1]).move(Gio.File.new_for_path(original), Gio.FileCopyFlags.NONE, None, None)
