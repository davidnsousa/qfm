"""Recursive name search below a folder, in a worker thread, and the pane listing the results."""
import os
import threading
import time
from collections import deque

from gi.repository import GdkPixbuf, Gio, GLib, Gtk, Pango

from .columns import short_path

MAX_RESULTS = 1000
MAX_FOLDERS = 50000


def walk(root, query, show_hidden, stop, emit):
    """Breadth-first (nearer matches first); emit(batch, done, truncated) every ~150 ms.
    Symlinked folders aren't followed."""
    needle = query.casefold()
    queue = deque([root])
    batch, found, folders, last = [], 0, 0, time.monotonic()
    while queue and not stop.is_set():
        folders += 1
        if folders > MAX_FOLDERS:
            return emit(batch, True, True)
        try:
            it = os.scandir(queue.popleft())
        except OSError:
            continue
        with it:
            for d in it:
                if not show_hidden and d.name.startswith("."):
                    continue
                try:
                    is_dir = d.is_dir(follow_symlinks=False)
                except OSError:
                    is_dir = False
                if is_dir:
                    queue.append(d.path)
                if needle in d.name.casefold():
                    batch.append((d.path, d.name, is_dir))
                    found += 1
                    if found >= MAX_RESULTS:
                        return emit(batch, True, True)
        if batch and time.monotonic() - last > 0.15:
            emit(batch, False, False)
            batch, last = [], time.monotonic()
    if not stop.is_set():
        emit(batch, True, False)


class SearchPane(Gtk.Box):
    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                         margin_start=8, margin_end=8, margin_top=8, margin_bottom=8)
        self.window = window
        self.stop = threading.Event()
        self.token = 0
        self.root = None
        self.query = ""

        header = Gtk.Box(spacing=6)
        self.heading = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.heading.get_style_context().add_class("qfm-pane-heading")
        close = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.MENU)
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.set_tooltip_text("Close (Esc)")
        close.connect("clicked", lambda *_: window.close_search())
        header.pack_start(self.heading, True, True, 0)
        header.pack_start(close, False, False, 0)
        self.count = Gtk.Label(xalign=0)
        self.count.get_style_context().add_class("qfm-preview-key")

        self.results = Gtk.ListStore(GdkPixbuf.Pixbuf, str, str, str)   # icon, name, where, path
        self.tree = Gtk.TreeView(model=self.results, headers_visible=False, search_column=1)
        column = Gtk.TreeViewColumn()
        icon = Gtk.CellRendererPixbuf(xpad=4)
        column.pack_start(icon, False)
        column.set_cell_data_func(icon, window.icons.cell_func, 0)
        name = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.MIDDLE)
        column.pack_start(name, True)
        column.add_attribute(name, "markup", 1)
        self.tree.append_column(column)
        self.tree.set_tooltip_column(3)
        self.tree.connect("row-activated", self._on_activated)
        scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.add(self.tree)
        hint = Gtk.Label(label="Enter: go to it · Esc: back to the preview", xalign=0)
        hint.get_style_context().add_class("qfm-preview-key")
        self.pack_start(header, False, False, 0)
        self.pack_start(self.count, False, False, 0)
        self.pack_start(scroller, True, True, 0)
        self.pack_start(hint, False, False, 0)

    def start(self, root, query, show_hidden):
        self.cancel()
        self.stop = stop = threading.Event()
        self.token += 1
        token = self.token
        self.root, self.query = root, query
        self.results.clear()
        self.heading.set_text(f"“{query}” in {short_path(root)}")
        self.count.set_text("Searching…")

        def emit(batch, done, truncated):
            GLib.idle_add(self._add, token, batch, done, truncated)

        threading.Thread(target=walk, args=(root, query, show_hidden, stop, emit), daemon=True).start()

    def start_archive(self, column, query):
        """Search the names inside the archive column shows (below its folder) - its listing
        is already read, so this is immediate."""
        from . import archive
        self.cancel()
        self.token += 1
        self.root, self.query = column.path, query
        self.results.clear()
        self.heading.set_text(f"“{query}” in {short_path(column.path)}")
        prefix = column.inner + "/" if column.inner else ""
        try:
            found = [i for i in archive.listing(column.archive).search(query) if i.startswith(prefix)]
        except (archive.Error, OSError) as err:
            self.count.set_text(f"Could not read the archive: {err}")
            return
        listing = archive.listing(column.archive)
        batch = [(os.path.join(column.archive, i), os.path.basename(i), listing.member(i).is_dir)
                 for i in sorted(found)[:MAX_RESULTS]]
        self._add(self.token, batch, True, len(found) > MAX_RESULTS)

    def cancel(self):
        self.stop.set()

    def _add(self, token, batch, done, truncated):
        if token != self.token:
            return False
        icons = self.window.icons
        for path, name, is_dir in batch:
            ct = "inode/directory" if is_dir else Gio.content_type_guess(name, None)[0]
            where = short_path(os.path.dirname(path))
            markup = f"{GLib.markup_escape_text(name)}  <small><span alpha=\"60%\">{GLib.markup_escape_text(where)}</span></small>"
            self.results.append((icons.get(ct), markup, where, path))
        n = len(self.results)
        if done:
            more = f" (stopped at {n} - narrow the search)" if truncated else ""
            self.count.set_text(("No matches" if n == 0 else f"{n} found") + more)
            if n and not self.tree.get_cursor()[0]:
                self.tree.set_cursor(Gtk.TreePath(0), None, False)
        else:
            self.count.set_text(f"Searching… {n} found")
        return False

    def focus(self):
        self.tree.grab_focus()

    def _on_activated(self, tree, path, column):
        self.window.go_to(self.results[path][3])
