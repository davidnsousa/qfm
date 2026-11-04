"""File operations: new folder or file (from ~/Templates too), rename (several at once too),
copy / cut / paste, duplicate, links, move to trash, delete, compress, extract. Questions go to the prompt bar (win.prompt); copies, moves, deletes and archive work
run in a worker thread; the columns refresh through their file monitors."""
import os
import shutil
import threading
from urllib.parse import unquote, urlparse

from gi.repository import Gdk, Gio, GLib, Gtk

from . import archive, clipboard, trash as trashcan
from .transfer import Job
from .columns import short_path


def unique_name(folder, name, is_dir):
    """name, or "name (2).ext", "name (3).ext", … if it's taken in folder."""
    if not os.path.lexists(os.path.join(folder, name)):
        return name
    stem, ext = (name, "") if is_dir else os.path.splitext(name)
    n = 2
    while os.path.lexists(os.path.join(folder, f"{stem} ({n}){ext}")):
        n += 1
    return f"{stem} ({n}){ext}"


def templates_dir():
    """The templates folder (XDG Templates, else ~/Templates), or None."""
    folder = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_TEMPLATES)
    if not folder or os.path.realpath(folder) == os.path.realpath(os.path.expanduser("~")):
        folder = os.path.expanduser("~/Templates")
    return folder if os.path.isdir(folder) else None


def templates():
    """[(name, path)] of what's in the templates folder, sorted."""
    folder = templates_dir()
    try:
        names = sorted((n for n in os.listdir(folder) if not n.startswith(".")), key=str.casefold) if folder else []
    except OSError:
        names = []
    return [(n, os.path.join(folder, n)) for n in names]


def copy_name(folder, name, is_dir, word):
    """A free name for a copy or link of name in folder: "report (copy).txt", "report (copy 2).txt"…"""
    stem, ext = (name, "") if is_dir else os.path.splitext(name)
    candidate, n = f"{stem} ({word}){ext}", 2
    while os.path.lexists(os.path.join(folder, candidate)):
        candidate, n = f"{stem} ({word} {n}){ext}", n + 1
    return candidate


def name_error(name):
    """Why name can't be a file name, or None."""
    if not name:
        return "Type a name"
    if "/" in name or name in (".", ".."):
        return f"“{name}” isn't a valid name"
    return None


def in_background(win, busy, work, done):
    """Run work() in a thread; done(error or None) back on the main loop."""
    win.flash(busy)

    def run():
        try:
            work()
            error = None
        except OSError as err:
            error = f"{err.strerror or err}: {short_path(err.filename) if err.filename else ''}".rstrip(": ")
        except shutil.Error as err:
            error = f"{len(err.args[0])} item(s) failed" if err.args and isinstance(err.args[0], list) else str(err)
        except archive.Error as err:
            error = str(err).split(" (errno=")[0]
        except GLib.Error as err:
            error = err.message
        GLib.idle_add(done, error)

    threading.Thread(target=run, daemon=True).start()


def copy_item(source, target):
    if os.path.isdir(source) and not os.path.islink(source):
        shutil.copytree(source, target, symlinks=True)
    else:
        shutil.copy2(source, target, follow_symlinks=False)


def remove_item(path):
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    else:
        os.remove(path)


def system_clipboard_files():
    """Files another file manager copied or cut: ("copy" or "cut", [paths]), or None."""
    clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
    data = clipboard.wait_for_contents(Gdk.Atom.intern("x-special/gnome-copied-files", False))
    if data is not None and data.get_data():
        lines = data.get_data().decode(errors="replace").splitlines()
        mode, uris = (lines[0], lines[1:]) if lines and lines[0] in ("copy", "cut") else ("copy", lines)
    else:
        mode, uris = "copy", clipboard.wait_for_uris() or []
    paths = [unquote(urlparse(u).path) for u in uris if u.startswith("file://")]
    return (mode, paths) if paths else None


class FileOps:
    """The operations, bound to the window (focused column, status bar, store)."""

    def __init__(self, win):
        self.win = win
        self.clip = None    # ("copy" or "cut", [paths]) from Ctrl+C / Ctrl+X
        self.history = []   # [(what was done, how to undo it)] for Ctrl+Z
        self.jobs = []      # copies and moves (transfer.Job), running or just ended

    def _column(self):
        return self.win.view.focused_column

    def in_archive(self):
        """True if the focused column shows inside an archive (read-only)."""
        column = self._column()
        return column is not None and column.archive is not None

    def in_trash(self):
        column = self._column()
        return column is not None and trashcan.in_trash(column.path)

    def _writable(self, trash_ok=False):
        if self.in_archive():
            self.win.flash("Archives are read-only here - copy (Ctrl+C) or extract what you need", error=True)
            return False
        if self.in_trash() and not trash_ok:
            self.win.flash("In the trash you can restore, copy or delete things (menu) - not change them",
                           error=True)
            return False
        return True

    def _select_in(self, dest, names):
        for column in self.win.view.columns:
            if column.path == dest:
                column.load(select=names)

    def _neighbour(self, column, entries):
        """The name to select once entries are gone: the first item after them, else before them."""
        gone = {e.name for e in entries}
        names = [e.name for e in column.visible]
        indexes = [i for i, n in enumerate(names) if n in gone]
        if not indexes:
            return None
        after = [n for n in names[indexes[-1] + 1:] if n not in gone]
        before = [n for n in names[:indexes[0]] if n not in gone]
        return after[0] if after else (before[-1] if before else None)

    @staticmethod
    def _what(entries):
        return entries[0].display if len(entries) == 1 else f"{len(entries)} items"

    def new_folder(self):
        column = self._column()
        if column is None or not self._writable():
            return
        dest = column.path

        def create(name):
            if name_error(name):
                return name_error(name)
            try:
                os.mkdir(os.path.join(dest, name))
            except OSError as err:
                return f"Could not create it: {err.strerror}"
            self._select_in(dest, [name])
            self.win.flash(f"Created {name}")
            created = os.path.join(dest, name)
            self._record(f"new folder {name}", lambda: os.rmdir(created))   # (only while it's empty)

        self.win.prompt.ask_text("New folder:", unique_name(dest, "New Folder", True), create, "Create")

    def new_file(self, template=None):
        """An empty file, or a copy of template (a path in the templates folder), named by the user."""
        column = self._column()
        if column is None or not self._writable():
            return
        dest = column.path
        initial = os.path.basename(template) if template else "New File"
        is_dir = bool(template) and os.path.isdir(template)

        def create(name):
            if name_error(name):
                return name_error(name)
            path = os.path.join(dest, name)
            if os.path.lexists(path):
                return f"{name} already exists"
            try:
                if template:
                    copy_item(template, path)
                else:
                    open(path, "x").close()
            except OSError as err:
                return f"Could not create it: {err.strerror}"
            self._select_in(dest, [name])
            self.win.flash(f"Created {name}")
            self._record_created(f"new {'file' if not is_dir else 'folder'} {name}", dest, [name])

        stem = initial if is_dir else os.path.splitext(initial)[0]
        name = unique_name(dest, initial, is_dir)
        self.win.prompt.ask_text("New file:" if not template else f"New from “{initial}”:", name, create,
                                 "Create", select=(0, len(stem) if name == initial else -1))

    def duplicate(self):
        """A copy of each selected item next to it ("name (copy).ext")."""
        targets = self.win.targets()
        if not targets:
            return self.win.flash("Select something to duplicate", error=True)
        if not self._writable():
            return
        folder = os.path.dirname(targets[0].path)
        pairs, taken = [], set()
        for e in targets:
            name = copy_name(folder, e.name, e.is_dir and not e.symlink, "copy")
            while name in taken:      # (two the same in one go: "a (copy).txt", "a (copy 2).txt")
                name = copy_name(folder, name, e.is_dir, "copy")
            taken.add(name)
            pairs.append((e.path, name))

        def work():
            for source, name in pairs:
                copy_item(source, os.path.join(folder, name))

        def done(error):
            names = [n for _, n in pairs if os.path.lexists(os.path.join(folder, n))]
            self._select_in(folder, names)
            self._record_created(f"duplicating {self._what(targets)}", folder, names)
            self.win.flash(f"Could not duplicate: {error}" if error else f"Duplicated {self._what(targets)}",
                           error=bool(error))
            return False

        in_background(self.win, f"Duplicating {self._what(targets)}…", work, done)

    def make_links(self, sources=None, dest=None):
        """Symbolic links to sources (default: the selection) in dest (default: next to them)."""
        if sources is None:
            targets = self.win.targets()
            if not targets:
                return self.win.flash("Select something to link to", error=True)
            sources = [e.path for e in targets]
        if not self._writable():
            return
        made = []
        for source in sources:
            folder = dest or os.path.dirname(source)
            name = os.path.basename(source)
            if dest is None or os.path.lexists(os.path.join(folder, name)):
                name = copy_name(folder, name, os.path.isdir(source), "link")
            try:
                os.symlink(source, os.path.join(folder, name))
            except OSError as err:
                return self.win.flash(f"Could not make a link: {err.strerror}", error=True)
            made.append((folder, name))
        if made:
            self._select_in(made[0][0], [n for _, n in made])
            links = [os.path.join(f, n) for f, n in made]
            self._record("making links", lambda: [os.unlink(p) for p in links if os.path.islink(p)])
            what = made[0][1] if len(made) == 1 else f"{len(made)} links"
            self.win.flash(f"Made {what}")

    def paste_links(self):
        """Ctrl+Shift+V: links (not copies) to what was copied, in the focused folder."""
        column = self._column()
        if column is None or not self._writable():
            return
        clip = self.clip if self.clip and self.clip[0] != "extract" else system_clipboard_files()
        sources = [p for p in (clip[1] if clip else []) if os.path.lexists(p)]
        if not sources:
            return self.win.flash("Nothing to link to - copy (Ctrl+C) something first", error=True)
        self.make_links(sources, column.path)

    def rename(self):
        targets, column = self.win.targets(), self._column()
        if not self._writable():
            return
        if len(targets) != 1:
            return self.win.flash("Select one item to rename", error=True)
        entry = targets[0]
        folder = os.path.dirname(entry.path)

        def rename(name):
            if name == entry.name:
                return None
            if name_error(name):
                return name_error(name)
            new_path = os.path.join(folder, name)
            if os.path.lexists(new_path):
                return f"{name} already exists"
            try:
                os.rename(entry.path, new_path)
            except OSError as err:
                return f"Could not rename it: {err.strerror}"
            self.win.store.rename_path(entry.path, new_path)
            self.win.rebuild_sidebar()
            self._select_in(folder, [name])
            self.win.flash(f"Renamed to {name}")

            def undo():
                if os.path.lexists(entry.path):
                    raise OSError(f"{entry.name} exists again")
                os.rename(new_path, entry.path)
                self.win.store.rename_path(new_path, entry.path)
            self._record(f"renaming {entry.name} to {name}", undo)

        stem = entry.name if entry.is_dir else os.path.splitext(entry.name)[0]
        self.win.prompt.ask_text(f"Rename “{entry.display}” to:", entry.name, rename, "Rename",
                                 select=(0, len(stem) or -1))

    def copy(self, cut=False):
        targets = self.win.targets()
        if not targets:
            return self.win.flash("Select something to copy", error=True)
        if self.in_archive():
            if cut:
                return self._writable()
            # pasting extracts them: remember the archive and the members
            self.clip = ("extract", [e.path for e in targets], (targets[0].archive, [e.inner for e in targets]))
            return self.win.flash(f"Copied {self._what(targets)} from the archive - paste with Ctrl+V")
        clip = self.clip = ("cut" if cut else "copy", [e.path for e in targets])
        # also on the system clipboard, so other file managers and apps can paste them
        clipboard.copy_files(clip[1], cut, on_lost=lambda: self._clip_lost(clip))
        self.win.flash(f"{'Cut' if cut else 'Copied'} {self._what(targets)} - paste with Ctrl+V")

    def _clip_lost(self, clip):
        """Another app put something on the clipboard: qfm's copied files aren't what Ctrl+V means now."""
        if self.clip is clip:
            self.clip = None

    def paste(self):
        column = self._column()
        if column is None or not self._writable():
            return
        if self.clip and self.clip[0] == "extract":
            return self._extract(self.clip[2][0], self.clip[2][1], column.path)
        clip = self.clip if self.clip and any(os.path.lexists(p) for p in self.clip[1]) else None
        if clip:
            clip = (clip[0], [p for p in clip[1] if os.path.lexists(p)])
        clip = clip or system_clipboard_files()
        if clip:
            clip = (clip[0], [p for p in clip[1] if os.path.lexists(p)])   # (cut ones may have moved already)
        if not clip or not clip[1]:
            return self.win.flash("Nothing to paste - copy (Ctrl+C) or cut (Ctrl+X) something first", error=True)
        mode, sources = clip
        self.transfer(mode, sources, column.path, clip)

    def can_transfer(self, mode, sources, dest):
        """None if sources can be copied ("copy") or moved ("cut") into dest, else why not."""
        for source in sources:
            if os.path.isdir(source) and (dest == source or dest.startswith(source + os.sep)):
                return f"Can't put {os.path.basename(source)} inside itself"
        if mode == "cut" and all(os.path.dirname(s) == dest for s in sources):
            return "Already in this folder"
        return None

    def transfer(self, mode, sources, dest, clip=None):
        """Copy ("copy") or move ("cut") sources into the folder dest, with progress (paste, and
        drag and drop). Names that exist there already: asks keep both / replace / skip."""
        problem = self.can_transfer(mode, sources, dest)
        if problem:
            return self.win.flash(problem, error=True)
        sources = [s for s in sources if not (mode == "cut" and os.path.dirname(s) == dest)]
        if not sources:
            return self.win.flash("Already in this folder")
        clashes = [s for s in sources if os.path.lexists(os.path.join(dest, os.path.basename(s)))]
        if not clashes:
            return self._start_transfer(mode, sources, dest, clip, "keep")
        what = f"“{os.path.basename(clashes[0])}” already exists" if len(clashes) == 1 \
            else f"{len(clashes)} items already exist"
        self.win.prompt.choose(f"{what} in {short_path(dest)}:", [
            ("k", "Keep both", lambda: self._start_transfer(mode, sources, dest, clip, "keep")),
            ("r", "Replace", lambda: self._start_transfer(mode, sources, dest, clip, "replace")),
            ("s", "Skip", lambda: self._start_transfer(mode, sources, dest, clip, "skip")),
        ])

    def _start_transfer(self, mode, sources, dest, clip, policy):
        pairs, replaced, taken = [], [], set()
        for source in sources:
            name, is_dir = os.path.basename(source), os.path.isdir(source)
            if os.path.lexists(os.path.join(dest, name)) or name in taken:
                if policy == "skip":
                    continue
                if policy == "replace" and name not in taken:
                    try:      # the old one goes to the trash (Ctrl+Z brings it back)
                        Gio.File.new_for_path(os.path.join(dest, name)).trash(None)
                        replaced.append(os.path.join(dest, name))
                    except GLib.Error as err:
                        return self.win.flash(f"Could not move the old {name} to the trash: {err.message}",
                                              error=True)
                else:
                    name = unique_name(dest, name, is_dir)
                    while name in taken:      # two sources with the same name
                        name = unique_name(dest, f"{os.path.splitext(name)[0]} (copy){os.path.splitext(name)[1]}", False)
            taken.add(name)
            pairs.append((source, os.path.join(dest, name)))
        if not pairs:
            return self.win.flash("Nothing copied - all skipped")
        move = mode == "cut"
        count = f"{len(pairs)} item{'s' if len(pairs) > 1 else ''}"
        job = Job(pairs, move, lambda job: GLib.idle_add(self._transfer_done, job, mode, dest, clip, replaced))
        self.jobs = [j for j in self.jobs if not getattr(j, "ended", False)] + [job]
        self.win.progress.add(job, f"{'Moving' if move else 'Copying'} {count} to {short_path(dest)}")
        self.win.flash(f"{'Moving' if move else 'Copying'} {count}…")
        job.start()

    def _transfer_done(self, job, mode, dest, clip, replaced):
        job.ended = True
        done = [(s, t) for s, t in job.pairs if t in job.finished]
        if mode == "cut":
            for source, target in done:
                self.win.store.rename_path(source, target)
            self.win.rebuild_sidebar()
            if clip is not None and clip is self.clip:
                self.clip = None
        self._select_in(dest, [os.path.basename(t) for _, t in done])
        if done or replaced:
            if mode == "cut":
                def undo():
                    for source, target in reversed(done):
                        if os.path.lexists(target) and not os.path.lexists(source):
                            shutil.move(target, source)
                    for path in replaced:
                        trashcan.untrash(path)
                self._record(f"moving {self._count(done)}", undo)
            else:
                def undo():
                    for _, target in done:
                        if os.path.lexists(target):
                            Gio.File.new_for_path(target).trash(None)
                    for path in replaced:
                        trashcan.untrash(path)
                self._record(f"copying {self._count(done)}", undo)
        verb = "Moved" if mode == "cut" else "Copied"
        if job.cancelled:
            self.win.flash(f"Cancelled - {len(done)} of {len(job.pairs)} done", error=True)
        elif job.error:
            self.win.flash(f"{verb} {len(done)} of {len(job.pairs)}; stopped at {job.error}", error=True)
        else:
            self.win.flash(f"{verb} {self._count(done)} to {short_path(dest)}")
        return False

    def running(self):
        """True while a copy or move is still going."""
        return any(not getattr(job, "ended", False) for job in self.jobs)

    @staticmethod
    def _count(pairs):
        return f"{len(pairs)} item{'s' if len(pairs) != 1 else ''}"

    # -- undo ----------------------------------------------------------------

    def _record(self, label, undo):
        """Remember how to undo something (Ctrl+Z); the last 20 are kept."""
        self.history.append((label, undo))
        del self.history[:-20]

    def _record_created(self, label, dest, names):
        """Undo = move what was created to the trash."""
        paths = [os.path.join(dest, n) for n in names]
        self._record(label, lambda: [Gio.File.new_for_path(p).trash(None) for p in paths if os.path.lexists(p)])

    def undo(self):
        if not self.history:
            return self.win.flash("Nothing to undo")
        label, undo = self.history.pop()

        def done(error):
            self.win.view.reload()
            self.win.rebuild_sidebar()
            self.win.flash(f"Could not undo {label}: {error}" if error else f"Undone: {label}", error=bool(error))
            return False

        in_background(self.win, f"Undoing {label}…", undo, done)

    def trash(self):
        targets, column = self.win.targets(), self._column()
        if self.in_trash():
            return self.delete()        # already in the trash: Delete deletes for good (asks)
        if not targets or not self._writable():
            return
        neighbour = self._neighbour(column, targets)
        failed, trashed = None, []
        for e in targets:
            try:
                Gio.File.new_for_path(e.path).trash(None)
                trashed.append(e.path)
            except GLib.Error as err:
                failed = err.message
                break
        if trashed:
            self._record(f"moving {self._count(trashed)} to the trash",
                         lambda: [trashcan.untrash(p) for p in trashed])
        column.load(select=neighbour)
        self.win.rebuild_sidebar()      # the trash icon is full now
        if failed:
            self.win.flash(f"Could not move to trash: {failed} (Shift+Delete deletes for good)", error=True)
        else:
            self.win.flash(f"Moved {self._what(targets)} to the trash")

    def delete(self):
        targets, column = self.win.targets(), self._column()
        if not targets or not self._writable(trash_ok=True):
            return
        if len(targets) == 1:
            e = targets[0]
            what = f"“{e.display}”" + (" and everything in it" if e.is_dir and not e.symlink else "")
        else:
            what = f"{len(targets)} items" + (" and everything in them" if any(e.is_dir for e in targets) else "")
        neighbour = self._neighbour(column, targets)
        paths = [e.path for e in targets]
        self.win.prompt.confirm(f"Delete {what} permanently? It can't be undone.",
                                lambda: self._delete(targets, paths, column, neighbour), "Delete")

    def _delete(self, targets, paths, column, neighbour):
        def work():
            for path in paths:
                remove_item(path)
                if trashcan.is_top(path):       # its record of where it came from goes too
                    info = os.path.join(trashcan.INFO, os.path.basename(path) + ".trashinfo")
                    if os.path.exists(info):
                        os.remove(info)

        def done(error):
            if column in self.win.view.columns:
                column.load(select=neighbour)
            self.win.rebuild_sidebar()
            self.win.flash(f"Delete failed: {error}" if error else f"Deleted {self._what(targets)}",
                           error=bool(error))
            return False

        in_background(self.win, f"Deleting {self._what(targets)}…", work, done)

    # -- archives ------------------------------------------------------------

    def _extract(self, path, inners, dest):
        """Extract members of the archive at path into dest, in the background."""
        names = []
        what = f"{len(inners)} item{'s' if len(inners) > 1 else ''}"

        def work():
            names.extend(archive.extract(path, inners, dest, unique_name))

        def done(error):
            if error:
                self.win.flash(f"Extract failed: {error}", error=True)
            else:
                self._select_in(dest, names)
                self.win.flash(f"Extracted {what} to {short_path(dest)}")
                self._record_created(f"extracting {what}", dest, names)
            return False

        in_background(self.win, f"Extracting {what}…", work, done)

    def _extract_archives(self, archives, dest):
        """Extract whole archives into dest (each: its single top item, or a folder named after it)."""
        names = []

        def work():
            for e in archives:
                names.append(archive.extract_all(e.path, dest, unique_name))

        def done(error):
            if error:
                self.win.flash(f"Extract failed: {error}", error=True)
            else:
                self._select_in(dest, names)
                self.win.flash(f"Extracted {self._what(archives)} to {short_path(dest)}")
                self._record_created(f"extracting {self._what(archives)}", dest, names)
            return False

        in_background(self.win, f"Extracting {self._what(archives)}…", work, done)

    def _archives(self):
        return [e for e in self.win.targets()
                if e.archive is None and not e.is_dir and archive.is_archive(e.name)]

    def extract_here(self):
        archives, column = self._archives(), self._column()
        if not archives:
            return self.win.flash("Select an archive to extract", error=True)
        self._extract_archives(archives, column.path)

    def extract_to(self):
        """Extract the selected archives - or the selected things inside an archive - to a folder."""
        targets = self.win.targets()
        if not targets:
            return
        column = self._column()
        if self.in_archive():
            start = os.path.dirname(column.archive)
            action = lambda folder: self._extract(targets[0].archive, [e.inner for e in targets], folder)
        elif self._archives():
            start, archives = column.path, self._archives()
            action = lambda folder: self._extract_archives(archives, folder)
        else:
            return self.win.flash("Select an archive to extract", error=True)

        def extract(folder):
            if not os.path.isdir(folder):
                return f"{short_path(folder)} isn't a folder"
            action(folder)

        self.win.prompt.ask_folder(f"Extract {self._what(targets)} to:", short_path(start) + "/", extract, "Extract")

    def compress(self):
        """Put the selection into a new archive next to it."""
        targets, column = self.win.targets(), self._column()
        if not targets or not self._writable():
            return
        initial = (archive.strip_suffix(targets[0].name) if len(targets) == 1
                   else os.path.basename(column.path) or "Archive")
        dest = column.path
        paths = [e.path for e in targets]
        formats = Gtk.ComboBoxText()
        for suffix in archive.FORMATS:
            formats.append(suffix, suffix)
        formats.set_active_id(self.win.options["archive_format"])

        def compress(name):
            if name_error(name):
                return name_error(name)
            suffix = formats.get_active_id()
            self.win.options["archive_format"] = suffix     # offered first next time
            self._compress(targets, paths, dest, name, suffix)

        self.win.prompt.ask_text(f"Compress {self._what(targets)} to:", initial, compress, "Compress", extra=formats)

    def _compress(self, targets, paths, dest, name, suffix):
        target_name = unique_name(dest, name + suffix, False)
        if not target_name.endswith(suffix):     # unique_name split ".gz" off ".tar.gz"
            stem, n = name, 2
            while os.path.lexists(os.path.join(dest, f"{stem} ({n}){suffix}")):
                n += 1
            target_name = f"{stem} ({n}){suffix}"
        target = os.path.join(dest, target_name)

        def work():
            try:
                archive.create(target, paths, suffix)
            except BaseException:
                if os.path.exists(target):
                    os.remove(target)      # no half-written archives
                raise

        def done(error):
            if error:
                self.win.flash(f"Compress failed: {error}", error=True)
            else:
                self._select_in(dest, [target_name])
                self.win.flash(f"Created {target_name}")
                self._record_created(f"compressing to {target_name}", dest, [target_name])
            return False

        in_background(self.win, f"Compressing {self._what(targets)}…", work, done)

    # -- the trash -----------------------------------------------------------

    def restore(self):
        """Put the selected things in the trash back where they were."""
        targets = [e for e in self.win.targets() if trashcan.is_top(e.path)]
        if not targets:
            return self.win.flash("Select things at the top of the trash to restore them", error=True)
        column, places = self._column(), []

        def work():
            places.extend(trashcan.restore([e.path for e in targets], unique_name))

        def done(error):
            if column in self.win.view.columns:
                column.load()
            self.win.rebuild_sidebar()
            if error:
                self.win.flash(f"Restore failed: {error}", error=True)
            else:
                folders = {short_path(f) for f, _ in places}
                self.win.flash(f"Restored {self._what(targets)} to {', '.join(sorted(folders))}")
                restored = [os.path.join(f, n) for f, n in places]
                self._record(f"restoring {self._what(targets)}",
                             lambda: [Gio.File.new_for_path(p).trash(None) for p in restored])
            return False

        in_background(self.win, f"Restoring {self._what(targets)}…", work, done)

    def empty_trash(self):
        if trashcan.is_empty():
            return self.win.flash("The trash is already empty")

        def empty():
            def done(error):
                for column in self.win.view.columns:
                    if trashcan.in_trash(column.path):
                        column.load()
                self.win.rebuild_sidebar()
                self.win.flash(f"Emptying the trash failed: {error}" if error else "The trash is empty",
                               error=bool(error))
                return False
            in_background(self.win, "Emptying the trash…", trashcan.empty, done)

        self.win.prompt.confirm("Empty the trash? Everything in it is deleted for good.", empty, "Empty")
