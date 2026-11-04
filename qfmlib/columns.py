"""Miller columns: a row of Columns (one TreeView per folder) in a horizontal ScrolledWindow."""
import functools
import os
import threading
import time

from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango

from . import archive, style, trash

ATTRIBUTES = ",".join((
    "standard::name", "standard::display-name", "standard::type", "standard::size",
    "standard::fast-content-type", "standard::is-hidden", "standard::is-backup",
    "standard::is-symlink", "time::modified", "time::created", "time::changed",
))
BATCH = 1000        # file infos per async enumeration step
OPEN_DELAY = 90     # ms a folder must stay selected before its column opens
COLUMN_WIDTH = 250
DETAILS = {"type": 130, "size": 80, "modified": 125, "created": 125}   # optional columns: width
DATE_FORMAT = "%Y-%m-%d %H:%M"
HOME = os.path.expanduser("~")

# ListStore columns; the optional detail columns are drawn from the Entry (cell data funcs)
C_ICON, C_NAME, C_ARROW, C_INDEX = range(4)
STORE_COLUMNS = list(range(4))


def human_size(size):
    return GLib.format_size(size)


def human_date(timestamp):
    return time.strftime(DATE_FORMAT, time.localtime(timestamp)) if timestamp else ""


@functools.lru_cache(maxsize=None)
def describe(content_type):
    return Gio.content_type_get_description(content_type)


def short_path(path):
    if path == HOME or path.startswith(HOME + os.sep):
        return "~" + path[len(HOME):]
    return path


class Entry:
    """One file or folder."""
    __slots__ = ("name", "display", "path", "is_dir", "size", "modified", "created",
                 "content_type", "hidden", "symlink", "key",
                 "archive", "inner", "browsable", "repo")
    # archive/inner: for something inside an archive, the archive file and its member path
    # (else None). browsable: opens as a column - folders, and archive files. repo: a folder
    # that's a git repository (set after listing, see Column._check_repos)

    @classmethod
    def from_info(cls, folder, info):
        e = cls()
        e.name = info.get_name()
        e.display = info.get_display_name()
        e.path = os.path.join(folder, e.name)
        e.is_dir = info.get_file_type() == Gio.FileType.DIRECTORY
        e.size = 0 if e.is_dir else info.get_size()
        e.modified = info.get_attribute_uint64("time::modified")
        e.created = info.get_attribute_uint64(
            "time::created" if info.has_attribute("time::created") else "time::changed")
        e.content_type = "inode/directory" if e.is_dir else (
            info.get_attribute_string("standard::fast-content-type") or "application/octet-stream")
        e.hidden = info.get_is_hidden() or info.get_is_backup()
        e.symlink = info.get_is_symlink()
        e.key = GLib.utf8_collate_key_for_filename(e.display, -1)
        e.archive = e.inner = None
        e.browsable = e.is_dir or archive.is_archive(e.name)
        e.repo = False
        return e

    @classmethod
    def from_member(cls, folder, archive_path, m):
        """An archive member (archive.Member), listed in the column for folder."""
        e = cls()
        e.name = e.display = m.name
        e.path = os.path.join(folder, m.name)
        e.is_dir = m.is_dir
        e.size = 0 if m.is_dir else m.size
        e.modified = e.created = m.mtime
        e.content_type = "inode/directory" if m.is_dir else Gio.content_type_guess(m.name, None)[0]
        e.hidden = m.name.startswith(".")
        e.symlink = m.symlink
        e.key = GLib.utf8_collate_key_for_filename(m.name, -1)
        e.archive, e.inner = archive_path, m.inner
        e.browsable = m.is_dir      # (archives inside archives aren't browsed)
        e.repo = False
        return e

    @classmethod
    def from_path(cls, path):
        """The Entry for one path, or None if it can't be read."""
        try:
            info = Gio.File.new_for_path(path).query_info(ATTRIBUTES, Gio.FileQueryInfoFlags.NONE, None)
        except GLib.Error:
            return None
        entry = cls.from_info(os.path.dirname(path) or "/", info)
        entry.path = path   # keeps "/" as "/"
        if path == "/":
            entry.display = "/"
        entry.repo = entry.is_dir and is_repo(path)
        return entry


def is_repo(path):
    """path is a git repository (.git is a folder, or a file for worktrees and submodules)."""
    return os.path.exists(os.path.join(path, ".git"))


def git_branch(path):
    """The branch checked out in the repository at path (a short commit when detached), or None."""
    git = os.path.join(path, ".git")
    try:
        if os.path.isfile(git):                  # "gitdir: <where the repository is>"
            with open(git, encoding="utf-8") as f:
                line = f.readline().strip()
            if not line.startswith("gitdir:"):
                return None
            git = os.path.join(path, line[7:].strip())
        with open(os.path.join(git, "HEAD"), encoding="utf-8") as f:
            head = f.readline().strip()
    except (OSError, UnicodeDecodeError):
        return None
    if head.startswith("ref: "):
        return head[5:].removeprefix("refs/heads/")
    return head[:7] or None


def column_width(options):
    return COLUMN_WIDTH + sum(w for key, w in DETAILS.items() if key in options["details"])


def sort_entries(items, field, descending):
    """Folders first, then files, each sorted by field (natural name order breaks ties)."""
    dirs = [e for e in items if e.is_dir]
    files = [e for e in items if not e.is_dir]
    if field == "size":
        key = lambda e: (e.size, e.key)
    elif field == "modified":
        key = lambda e: (e.modified, e.key)
    elif field == "type":
        key = lambda e: (os.path.splitext(e.name)[1].casefold(), e.key)
    else:
        key = lambda e: e.key
    dirs.sort(key=key if field in ("name", "modified") else (lambda e: e.key), reverse=descending)
    files.sort(key=key, reverse=descending)
    return dirs + files


class Column(Gtk.Overlay):
    """One folder's list. Talks to its MillerView for everything beyond itself."""

    def __init__(self, view, path, archive_path=None, inner=None):
        super().__init__()
        self.view = view
        self.path = path
        self.archive = archive_path   # the archive file, for a column showing inside one
        self.inner = inner            # the folder inside it ("" for its top)
        self.gfile = Gio.File.new_for_path(path)
        self.entries = []
        self.visible = []         # entries as listed, row i = visible[i]
        self.selected = None      # the lead: the selected row the cursor is on (navigation, preview)
        self.marked = []          # every selected entry, in list order (file operations)
        self._click_path = None   # a click on a multi-selection, applied on release unless it's a drag
        self.filter = ""
        self.pending = None       # names to select once loaded (see MillerView.resume)
        self.select_after = None  # a name to select after the next load (file operations)
        self.force_show = set()   # hidden names shown anyway (a path going through them was opened)
        self.loaded = False
        self.cancellable = None
        self.monitor = None
        self._reload_timer = 0
        self._quiet = 0           # > 0: selection changes are ours, not the user's

        self.store = Gtk.ListStore(GdkPixbuf.Pixbuf, str, str, int)
        tree = self.tree = Gtk.TreeView(model=self.store, headers_visible=False,
                                        enable_search=True, search_column=C_NAME)
        name = Gtk.TreeViewColumn()
        name.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
        name.set_expand(True)
        icon = Gtk.CellRendererPixbuf(xpad=4)
        name.pack_start(icon, False)
        name.set_cell_data_func(icon, self.view.icons.cell_func, C_ICON)
        text = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.MIDDLE)
        name.pack_start(text, True)
        name.add_attribute(text, "text", C_NAME)
        tree.append_column(name)
        self.details = {}
        for key in DETAILS:
            cell = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END, xalign=1 if key == "size" else 0)
            cell.set_property("foreground-rgba", Gdk.RGBA(0.6, 0.6, 0.6, 1))
            column = Gtk.TreeViewColumn()
            column.pack_start(cell, True)
            column.set_cell_data_func(cell, self._detail_text, key)
            column.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
            column.set_fixed_width(DETAILS[key])
            tree.append_column(column)
            self.details[key] = column
        arrow_cell = Gtk.CellRendererText(xalign=0.5)
        arrow_cell.set_property("foreground-rgba", Gdk.RGBA(0.55, 0.55, 0.55, 1))
        arrow = Gtk.TreeViewColumn(None, arrow_cell, text=C_ARROW)
        arrow.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
        arrow.set_fixed_width(22)
        tree.append_column(arrow)
        tree.set_fixed_height_mode(True)

        selection = tree.get_selection()
        selection.set_mode(Gtk.SelectionMode.MULTIPLE)   # Shift/Ctrl+click, Shift+arrows, Ctrl+A
        selection.connect("changed", self._on_selection)
        tree.connect("key-press-event", self._on_key)
        tree.connect("row-activated", lambda *_: view.activate(self))
        tree.connect("focus-in-event", lambda *_: view.focused(self))
        tree.connect("button-press-event", self._on_button)
        tree.connect("button-release-event", self._on_release)
        tree.connect("drag-begin", self._on_drag_begin)
        # drag-out: files as text/uri-list (file managers, browsers, chat apps), paths as text
        targets = [Gtk.TargetEntry.new("text/uri-list", 0, 0),
                   Gtk.TargetEntry.new("text/plain;charset=utf-8", 0, 1),
                   Gtk.TargetEntry.new("UTF8_STRING", 0, 1),
                   Gtk.TargetEntry.new("text/plain", 0, 1)]
        tree.enable_model_drag_source(Gdk.ModifierType.BUTTON1_MASK, targets,
                                      Gdk.DragAction.COPY | Gdk.DragAction.MOVE | Gdk.DragAction.LINK)
        tree.connect("drag-data-get", self._on_drag_data_get)
        tree.connect("drag-end", lambda *_: setattr(view, "dragging", None))
        # files move, not rows: never let GTK delete the dragged rows from the list
        tree.connect("drag-data-delete", lambda t, _: t.stop_emission_by_name("drag-data-delete"))
        # drops (from qfm or other apps): onto a folder row = into it, elsewhere = into this folder
        tree.drag_dest_set(0, [Gtk.TargetEntry.new("text/uri-list", 0, 0)],
                           Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        tree.connect("drag-motion", self._on_drag_motion)
        tree.connect("drag-leave", lambda t, *_: t.set_drag_dest_row(None, Gtk.TreeViewDropPosition.BEFORE))
        tree.connect("drag-drop", self._on_drag_drop)
        tree.connect("drag-data-received", self._on_drag_received)

        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.add(tree)
        self.separator = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)   # shown by MillerView
        inside = Gtk.Box()
        inside.pack_start(self.separator, False, False, 0)
        inside.pack_start(scroller, True, True, 0)
        self.add(inside)
        self.message = Gtk.Label(halign=Gtk.Align.CENTER, valign=Gtk.Align.START, margin_top=24,
                                 wrap=True, justify=Gtk.Justification.CENTER, max_width_chars=28)
        self.message.get_style_context().add_class("qfm-message")
        self.message.set_no_show_all(True)
        self.add_overlay(self.message)
        self.set_overlay_pass_through(self.message, True)
        self.get_style_context().add_class("qfm-column")
        self.apply_details()
        self._watch()
        self.load()

    # -- loading -------------------------------------------------------------

    def load(self, select=None):
        """(Re)read the folder asynchronously; refresh() fills the list when done.
        select: a name (or names) to select once it's read (e.g. a folder just created)."""
        if select is not None:
            self.select_after = select
        if self.cancellable:
            self.cancellable.cancel()
        cancellable = self.cancellable = Gio.Cancellable()
        entries = []
        if not self.loaded:
            GLib.timeout_add(250, self._show_loading, cancellable)
        if self.archive is not None:
            return self._load_archive(cancellable)

        def on_batch(enumerator, result):
            try:
                infos = enumerator.next_files_finish(result)
            except GLib.Error as err:
                return self._failed(err)
            if infos:
                folder = self.path
                entries.extend(Entry.from_info(folder, info) for info in infos)
                enumerator.next_files_async(BATCH, GLib.PRIORITY_DEFAULT, cancellable, on_batch)
            else:
                enumerator.close(None)
                if not cancellable.is_cancelled():
                    known = {e.path for e in self.entries if e.repo}
                    for e in entries:
                        e.repo = e.is_dir and e.path in known   # as before until checked again
                    self._loaded(entries)
                    self._check_repos(entries, cancellable)

        def on_enumerator(gfile, result):
            try:
                enumerator = gfile.enumerate_children_finish(result)
            except GLib.Error as err:
                return self._failed(err)
            enumerator.next_files_async(BATCH, GLib.PRIORITY_DEFAULT, cancellable, on_batch)

        self.gfile.enumerate_children_async(ATTRIBUTES, Gio.FileQueryInfoFlags.NONE,
                                            GLib.PRIORITY_DEFAULT, cancellable, on_enumerator)

    def _check_repos(self, entries, cancellable):
        """Which folders are git repositories, found in a thread (a slow disk mustn't block the
        list); the ones that changed get their icon updated in place."""
        folders = [e for e in entries if e.is_dir]
        if not folders:
            return

        def work():
            found = {e.path for e in folders if is_repo(e.path)}
            GLib.idle_add(done, found)

        def done(found):
            if cancellable.is_cancelled():
                return False
            changed = {e for e in folders if e.repo != (e.path in found)}
            for e in changed:
                e.repo = e.path in found
            if changed:
                icons, colors = self.view.icons, self.view.store.colors
                for row in self.store:
                    e = self.visible[row[C_INDEX]]
                    if e in changed:
                        row[C_ICON] = icons.for_entry(e, colors)
                if self.view.window.current in changed:
                    self.view.window._update_status()    # "git repository on <branch>"
            return False

        threading.Thread(target=work, daemon=True).start()

    def _load_archive(self, cancellable):
        """Read the archive's listing in a thread (a big .tar.gz takes a while)."""
        def work():
            try:
                folders = archive.listing(self.archive).folders
                if self.inner not in folders:
                    raise OSError(f"{self.inner} isn't in this archive")
                entries = [Entry.from_member(self.path, self.archive, m) for m in folders[self.inner].values()]
                error = None
            except (archive.Error, OSError) as err:
                entries, error = None, str(err).split(" (errno=")[0]
            GLib.idle_add(done, entries, error)

        def done(entries, error):
            if not cancellable.is_cancelled():
                self._loaded(entries) if error is None else self._show_failure(error)
            return False

        threading.Thread(target=work, daemon=True).start()

    def folder_entry(self):
        """The Entry for this column's own folder (what's shown when nothing is selected)."""
        if self.archive is None:
            return Entry.from_path(self.path)
        if not self.inner:
            return Entry.from_path(self.archive)          # the archive file itself
        try:
            member = archive.listing(self.archive).member(self.inner)
        except (archive.Error, OSError):
            return None
        return Entry.from_member(os.path.dirname(self.path), self.archive, member) if member else None

    def _show_loading(self, cancellable):
        if not self.loaded and not cancellable.is_cancelled():
            self._say("Loading…")
        return False

    def _failed(self, err):
        if err.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
            return
        self._show_failure(err.message)

    def _show_failure(self, message):
        self.entries = []
        self.loaded = True
        self.refresh()
        self._say(message)
        self._finish_pending()

    def _loaded(self, entries):
        if self.path == trash.FILES:
            trash.name_original(entries)     # the names things had before they were trashed
        self.entries = entries
        self.loaded = True
        self.refresh()
        names, self.select_after = self.select_after, None
        if names is not None and self in self.view.columns:
            entry = self.select_names([names] if isinstance(names, str) else names)
            if entry is not None:
                self.view.selected(self, entry)
        self._finish_pending()

    def _finish_pending(self):
        if self.pending is not None:
            names, self.pending = self.pending, None
            self.view.resume(self, names)

    def _watch(self):
        if self.archive is not None:
            return
        try:
            self.monitor = self.gfile.monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
        except GLib.Error:
            return
        self.monitor.connect("changed", self._on_disk_change)

    def _on_disk_change(self, *_):
        if not self._reload_timer:
            self._reload_timer = GLib.timeout_add(300, self._reload)

    def _reload(self):
        self._reload_timer = 0
        self.load()
        return False

    def close(self):
        """Stop loading and watching (the column is going away)."""
        if self.cancellable:
            self.cancellable.cancel()
        if self.monitor:
            self.monitor.cancel()
        if self._reload_timer:
            GLib.source_remove(self._reload_timer)
            self._reload_timer = 0

    # -- the list ------------------------------------------------------------

    def apply_details(self):
        shown = self.view.options["details"]
        for key, column in self.details.items():
            column.set_visible(key in shown)
        self.set_size_request(-1 if self.view.fit else column_width(self.view.options), -1)

    def refresh(self):
        """Fill the list from self.entries, with hidden files, filter and sort applied."""
        options = self.view.options
        items = self.entries
        if not options["show_hidden"]:
            items = [e for e in items if not e.hidden or e.name in self.force_show]
        if self.filter:
            needle = self.filter.casefold()
            items = [e for e in items if needle in e.display.casefold()]
        self.visible = sort_entries(items, options["sort"], options["descending"])

        previous, previous_names = self.selected, [e.name for e in self.marked]
        scroll = self.tree.get_vadjustment().get_value()
        icons, colors = self.view.icons, self.view.store.colors
        self._quiet += 1
        self.tree.set_model(None)
        self.store.clear()
        insert = self.store.insert_with_valuesv   # much faster than append() for big folders
        for i, e in enumerate(self.visible):
            insert(-1, STORE_COLUMNS, [icons.for_entry(e, colors), e.display, "›" if e.browsable else "", i])
        self.tree.set_model(self.store)
        self.selected, self.marked = None, []
        if previous_names:
            wanted = set(previous_names)
            lead = self._index_of(previous.name) if previous is not None else None
            if lead is not None:
                self.tree.set_cursor(Gtk.TreePath(lead), None, False)
            selection = self.tree.get_selection()
            for i, e in enumerate(self.visible):
                if e.name in wanted:
                    selection.select_path(Gtk.TreePath(i))
                    self.marked.append(e)
            if self.marked:
                self.selected = self.visible[lead] if lead is not None else self.marked[-1]
        self._quiet -= 1
        GLib.idle_add(self.tree.get_vadjustment().set_value, scroll)

        if self.visible:
            self._say(None)
        elif self.loaded:
            self._say("No matches" if self.filter else "Empty folder")
        if previous_names and [e.name for e in self.marked] != previous_names:
            self.view.selected(self, self.selected)       # some went away (deleted, hidden, filtered)
        elif self.selected is not None and self.selected is not previous:
            self.view.updated(self, self.selected)        # same names, fresh info

    def _detail_text(self, column, cell, model, it, key):
        e = self.visible[model.get_value(it, C_INDEX)]
        if key == "type":
            text = describe(e.content_type)
        elif key == "size":
            text = "" if e.is_dir else human_size(e.size)
        else:
            text = human_date(e.modified if key == "modified" else e.created)
        cell.set_property("text", text)

    def _say(self, text):
        self.message.set_text(text or "")
        self.message.set_visible(bool(text))

    def _index_of(self, name):
        for i, e in enumerate(self.visible):
            if e.name == name:
                return i
        return None

    def select_name(self, name):
        """Select the entry called name, quietly (no view.selected); returns it or None."""
        return self.select_names([name])

    def select_names(self, names):
        """Select the entries with these names, quietly; the first becomes the lead (returned)."""
        wanted = set(names)
        indexes = [i for i, e in enumerate(self.visible) if e.name in wanted]
        if not indexes:
            return None
        self._quiet += 1
        path = Gtk.TreePath(indexes[0])
        self.tree.set_cursor(path, None, False)
        self.tree.scroll_to_cell(path, None, True, 0.35, 0)
        selection = self.tree.get_selection()
        for i in indexes[1:]:
            selection.select_path(Gtk.TreePath(i))
        self._quiet -= 1
        self.marked = [self.visible[i] for i in indexes]
        self.selected = self.marked[0]
        return self.selected

    def select_first(self):
        """Select the first row if nothing is selected (as the user would: view.selected runs)."""
        if self.selected is None and self.visible:
            self.tree.set_cursor(Gtk.TreePath(0), None, False)

    def clear_selection(self):
        self._quiet += 1
        self.tree.get_selection().unselect_all()
        self._quiet -= 1
        self.selected, self.marked = None, []

    # -- events --------------------------------------------------------------

    def _on_selection(self, selection):
        if self._quiet:
            return
        model, paths = selection.get_selected_rows()
        marked = [self.visible[model.get_value(model.get_iter(p), C_INDEX)] for p in paths]
        cursor = self.tree.get_cursor()[0]
        lead = None
        if marked:
            lead = marked[-1]
            for p, e in zip(paths, marked):
                if cursor is not None and p.compare(cursor) == 0:
                    lead = e
        if lead is self.selected and len(marked) == len(self.marked) and all(
                a is b for a, b in zip(marked, self.marked)):
            return
        self.selected, self.marked = lead, marked
        self.view.selected(self, lead)

    def _on_key(self, tree, event):
        key = event.keyval
        mods = event.state & Gtk.accelerator_get_default_mod_mask()
        if mods == 0:
            if key in (Gdk.KEY_Left, Gdk.KEY_BackSpace):
                self.view.go_left(self)
            elif key == Gdk.KEY_Right:
                self.view.go_right(self)
            elif key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter):
                self.view.activate(self)
            elif key == Gdk.KEY_space:
                self.view.window.toggle_preview()
            else:
                return False
            return True
        if mods == Gdk.ModifierType.MOD1_MASK and key == Gdk.KEY_Up:
            self.view.go_left(self)
            return True
        return False

    def _on_button(self, tree, event):
        if event.type != Gdk.EventType.BUTTON_PRESS:
            return False
        hit = tree.get_path_at_pos(int(event.x), int(event.y))
        selection = tree.get_selection()
        if event.button == 3:
            tree.grab_focus()
            if hit and not selection.path_is_selected(hit[0]):
                tree.set_cursor(hit[0], None, False)
            self.view.window.show_menu(event)
            return True
        mods = event.state & Gtk.accelerator_get_default_mod_mask()
        if (event.button == 1 and hit and not mods and selection.path_is_selected(hit[0])
                and selection.count_selected_rows() > 1):
            # keep the multi-selection in case this starts a drag; a plain click
            # selects just this row on release (GTK 3 would drop the rest right away)
            self._click_path = hit[0]
            selection.set_select_function(lambda *_: False)
        return False

    def _on_release(self, tree, event):
        if self._click_path is not None:
            path, self._click_path = self._click_path, None
            tree.get_selection().set_select_function(lambda *_: True)
            tree.set_cursor(path, None, False)
        return False

    def _on_drag_begin(self, tree, context):
        self.view.dragging = [e.path for e in self.marked]
        if self._click_path is not None:
            self._click_path = None
            tree.get_selection().set_select_function(lambda *_: True)

    def _on_drag_data_get(self, tree, context, data, info, time_):
        entries = self.marked
        if not entries:
            return
        paths = [e.path for e in entries]
        if self.archive is not None:      # out of an archive: hand over extracted copies
            try:
                paths = archive.to_temp(self.archive, [e.inner for e in entries])
            except (archive.Error, OSError) as err:
                self.view.window.flash(f"Could not extract: {err}", error=True)
                return
        if info == 0:
            data.set_uris([Gio.File.new_for_path(p).get_uri() for p in paths])
        else:
            data.set_text("\n".join(paths), -1)

    def _drop_target(self, x, y):
        """(folder a drop at x, y goes into, the folder row under it or None)."""
        hit = self.tree.get_dest_row_at_pos(x, y)
        if hit and self.tree.get_model() is not None:
            path = hit[0]
            entry = self.visible[self.store.get_value(self.store.get_iter(path), C_INDEX)]
            if entry.is_dir and entry.archive is None and entry.path not in (self.view.dragging or ()):
                return entry.path, path
        return self.path, None

    def _on_drag_motion(self, tree, context, x, y, time_):
        if self.archive is not None:          # archives are read-only
            Gdk.drag_status(context, 0, time_)
            return True
        dest, row = self._drop_target(x, y)
        action = self.view.drop_action(context, dest)
        if action and row is not None:
            tree.set_drag_dest_row(row, Gtk.TreeViewDropPosition.INTO_OR_AFTER)
        else:
            tree.set_drag_dest_row(None, Gtk.TreeViewDropPosition.BEFORE)
        Gdk.drag_status(context, action, time_)
        return True

    def _on_drag_drop(self, tree, context, x, y, time_):
        target = tree.drag_dest_find_target(context, None)
        if target is None:
            return False
        tree.drag_get_data(context, target, time_)
        return True

    def _on_drag_received(self, tree, context, x, y, data, info, time_):
        tree.stop_emission_by_name("drag-data-received")   # (not GTK's row reordering)
        paths = [Gio.File.new_for_uri(u).get_path() for u in (data.get_uris() or [])]
        paths = [p for p in paths if p]
        dest, _ = self._drop_target(x, y)
        action = context.get_selected_action()
        ok = bool(paths) and action in (Gdk.DragAction.COPY, Gdk.DragAction.MOVE)
        Gtk.drag_finish(context, ok, False, time_)   # False: the source never deletes anything
        tree.set_drag_dest_row(None, Gtk.TreeViewDropPosition.BEFORE)
        if ok:
            self.view.window.fileops.transfer("cut" if action == Gdk.DragAction.MOVE else "copy", paths, dest)

    def row_rect(self):
        """Where the selected row is (for popping a menu up at it), or None."""
        if self.selected is None:
            return None
        index = self._index_of(self.selected.name)
        if index is None:
            return None
        return self.tree.get_cell_area(Gtk.TreePath(index), self.tree.get_column(0))


class MillerView(Gtk.ScrolledWindow):
    """The row of columns, and keyboard/selection navigation between them."""

    def __init__(self, window):
        super().__init__(hscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
                         vscrollbar_policy=Gtk.PolicyType.NEVER)
        self.window = window
        self.store = window.store
        self.options = window.store.options
        self.icons = window.icons
        self.columns = []
        self.focus_index = 0
        self._focus_selected = False
        self._open_timer = 0
        self._reveal_target = None
        self.dragging = None      # paths being dragged out of a column (None: a drag from another app)
        self.box = Gtk.Box()
        self.add(self.box)
        # Stands in for the next column while the focused one has none (a file is
        # selected), so the row keeps its width and nothing slides while moving up/down
        self.placeholder = Gtk.Box()
        self.placeholder.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 0)
        self.placeholder_space = Gtk.Box()
        self.placeholder.pack_start(self.placeholder_space, False, False, 0)
        self.placeholder_space.set_size_request(column_width(self.options), -1)
        self.box.pack_start(self.placeholder, False, False, 0)
        self.placeholder.set_no_show_all(True)
        self.placeholder.get_children()[0].show()
        self.placeholder_space.show()
        self.get_hadjustment().connect("changed", lambda *_: self._reveal())

    @property
    def focused_column(self):
        return self.columns[self.focus_index] if self.columns else None

    def add_column_for(self, entry):
        """The column showing inside entry: a folder, an archive, or a folder in an archive."""
        if entry.archive is not None:
            return self.add_column(entry.path, entry.archive, entry.inner)
        if not entry.is_dir:
            return self.add_column(entry.path, entry.path, "")
        return self.add_column(entry.path)

    def add_column(self, path, archive_path=None, inner=None):
        column = Column(self, path, archive_path, inner)
        self.box.pack_start(column, False, False, 0)
        self.box.reorder_child(self.placeholder, -1)
        column.show_all()
        self.columns.append(column)
        self._layout()
        if self._reveal_target not in self.columns:
            self._reveal_target = column
        GLib.idle_add(self._reveal)
        return column

    def truncate(self, index):
        """Remove every column after columns[index] (index -1: all of them)."""
        self._cancel_open()
        for column in self.columns[index + 1:]:
            column.close()
            column.destroy()
        del self.columns[index + 1:]
        self.focus_index = max(0, min(self.focus_index, index))
        self._layout()

    @property
    def fit(self):
        return bool(self.options["fit_columns"])

    def _layout(self):
        """Scrolling: every column at its width, the placeholder after the focused one when it
        has no next column. Fitting: the focused column, the slot after it (its next column, or
        the placeholder) and the columns before, at most max_columns, splitting the width; the
        others hidden until navigated to. The slot stays while moving between files and folders,
        so only it changes; a lone first column showing a file fills the width."""
        fit = self.fit
        self.set_policy(Gtk.PolicyType.NEVER if fit else Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        self.box.set_homogeneous(fit)
        focused, has_next = self.focused_column, self.focus_index + 1 < len(self.columns)
        if fit:
            most = max(1, int(self.options["max_columns"]))
            slot = most > 1 and (has_next or self.focus_index > 0
                                 or (focused is not None and focused.selected is not None
                                     and focused.selected.browsable))
            first = max(0, self.focus_index + (2 if slot else 1) - most)
            last = self.focus_index + 1 if slot else self.focus_index
        else:
            slot, first, last = focused is not None, 0, len(self.columns)
        for prop in ("expand", "fill"):
            self.box.child_set_property(self.placeholder, prop, fit)
        for i, column in enumerate(self.columns):
            for prop in ("expand", "fill"):
                self.box.child_set_property(column, prop, fit)
            column.set_visible(first <= i <= last)
            column.separator.set_visible(i > first)
        self.placeholder_space.set_size_request(-1 if fit else column_width(self.options), -1)
        self.placeholder.set_visible(slot and not has_next)

    def go_to(self, path, select=None):
        """Show the folder path (columns from ~ or / down to it), focused.
        select: a name in it to select and focus instead of its first entry."""
        path = os.path.normpath(path)
        if trash.in_trash(path):
            base = trash.FILES           # the trash starts its own row of columns
        elif path == HOME or path.startswith(HOME + os.sep):
            base = HOME
        else:
            base = os.sep
        names = [p for p in os.path.relpath(path, base).split(os.sep) if p not in (".", "")]
        if select:
            names.append(select)
        self._focus_selected = select is not None
        self.truncate(-1)
        self.focus_index = 0
        self.add_column(base).pending = names

    def resume(self, column, names):
        """A column go_to() created has loaded: select the next name on the way."""
        if column not in self.columns:
            return
        if not names:
            return self.focus(column)
        entry = column.select_name(names[0])
        if entry is None and any(e.name == names[0] for e in column.entries):
            column.force_show.add(names[0])      # a hidden folder on the way: show just that one
            column.refresh()
            entry = column.select_name(names[0])
        if entry is None:
            return self.focus(column)
        self.truncate(self.columns.index(column))
        if len(names) == 1 and self._focus_selected:
            self.focus(column)
            self.selected(column, entry)
        elif entry.browsable:
            self.window.selection_changed(entry)
            self.add_column_for(entry).pending = names[1:]
        else:
            self.focus(column)
            self.window.selection_changed(entry)

    def focus(self, column):
        column.tree.grab_focus()
        self.focused(column)
        column.select_first()

    def focused(self, column):
        if column not in self.columns:
            return
        index = self.columns.index(column)
        if index != self.focus_index:
            for other in self.columns:
                if other.filter and other is not column:
                    other.filter = ""
                    other.refresh()
                    self.window.filter_cleared()
        self.focus_index = index
        self._layout()
        for other in self.columns:
            context = other.get_style_context()
            (context.add_class if other is column else context.remove_class)("active")
        self.window.location_changed(column.path)
        self._reveal_target = column
        self._reveal()

    def selected(self, column, entry):
        """The user selected entry (or nothing) in column."""
        if column not in self.columns:
            return
        self.truncate(self.columns.index(column))
        many = column.marked if len(column.marked) > 1 else None
        self.window.selection_changed(entry, many)
        if entry is not None and entry.browsable and not many:
            self._open_timer = GLib.timeout_add(OPEN_DELAY, self._open_child, column, entry)

    def updated(self, column, entry):
        """entry was re-read from disk (still selected)."""
        if self.window.current is not None and self.window.current.path == entry.path:
            self.window.selection_changed(entry, column.marked if len(column.marked) > 1 else None)

    def _open_child(self, column, entry):
        self._open_timer = 0
        if self.columns and self.columns[-1] is column and column.selected is entry and len(column.marked) == 1:
            self.add_column_for(entry)
        return False

    def _cancel_open(self):
        if self._open_timer:
            GLib.source_remove(self._open_timer)
            self._open_timer = 0

    def go_right(self, column):
        entry = column.selected
        if entry is None or not entry.browsable or len(column.marked) > 1:
            return
        index = self.columns.index(column)
        if index + 1 < len(self.columns) and self.columns[index + 1].path == entry.path:
            child = self.columns[index + 1]
            if child.loaded:
                self.focus(child)
            else:
                child.pending = []
        else:
            self.truncate(index)
            self.add_column_for(entry).pending = []

    def go_left(self, column):
        index = self.columns.index(column)
        if index == 0:
            if column.path == trash.FILES:
                return
            parent = os.path.dirname(column.path)
            if parent != column.path:
                self.go_to(parent, select=os.path.basename(column.path))
            return
        parent = self.columns[index - 1]
        self.truncate(index)
        column.clear_selection()
        self.focus(parent)
        self.window.selection_changed(parent.selected)

    def activate(self, column):
        entry = column.selected
        if entry is None:
            return
        if len(column.marked) > 1:
            self.window.open_entries(column.marked)
        elif entry.browsable:
            self.go_right(column)          # folders and archives open as columns
        else:
            self.window.open_entries([entry])

    def drop_action(self, context, dest):
        """What dropping onto the folder dest does: Ctrl copies, Shift moves; otherwise qfm's own
        drags move within a disk and copy across disks, other apps' drags copy. 0: refused."""
        offered = context.get_actions()
        mods = self.window.pointer_modifiers()
        copy, move = Gdk.DragAction.COPY, Gdk.DragAction.MOVE
        sources = self.dragging
        if mods & Gdk.ModifierType.CONTROL_MASK:
            action = copy
        elif mods & Gdk.ModifierType.SHIFT_MASK:
            action = move
        elif sources:
            try:
                action = move if os.stat(sources[0]).st_dev == os.stat(dest).st_dev else copy
            except OSError:
                action = copy
        else:
            action = copy
        if not offered & action:
            action = copy if offered & copy else (move if offered & move else 0)
        if action and sources and self.window.fileops.can_transfer(
                "cut" if action == move else "copy", sources, dest):
            return 0
        return action

    def _reveal(self):
        """Scroll so the target column (and the one after it) are in view."""
        column = self._reveal_target
        if self.fit or column not in self.columns:
            return False
        alloc = column.get_allocation()
        if alloc.width <= 1:
            return False   # not laid out yet; the adjustment's "changed" brings us back
        left, right = alloc.x, alloc.x + alloc.width
        index = self.columns.index(column)
        if index + 1 < len(self.columns):
            after = self.columns[index + 1].get_allocation()
            if after.width > 1:
                right = after.x + after.width
        adj = self.get_hadjustment()
        value = adj.get_value()
        if right > value + adj.get_page_size():
            value = right - adj.get_page_size()
        if left < value:
            value = left
        adj.set_value(value)
        return False

    # -- applied to every column ---------------------------------------------

    def refresh_all(self):
        for column in list(self.columns):
            if column in self.columns:
                column.apply_details()
                column.refresh()
        self._layout()

    def refresh_folder(self, path):
        for column in list(self.columns):
            if column in self.columns and column.path == path:
                column.refresh()

    def reload(self):
        for column in self.columns:
            column.load()

    def set_filter(self, text):
        column = self.focused_column
        if column is not None and column.filter != text:
            column.filter = text
            column.refresh()
