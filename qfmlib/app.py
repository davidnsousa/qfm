"""The qfm window, and the single-instance Gtk.Application around it."""
import fnmatch
import os
import sys

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")  # else gi picks Gdk 4.0, which clashes with Gtk 3
from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from . import actions, archive, commands, fm1, media, style, trash  # noqa: E402
from .fileops import FileOps  # noqa: E402
from .columns import APPLICATIONS, HOME, Entry, MillerView, describe, git_branch, human_date, human_size, short_path  # noqa: E402
from .preview import GRID_LIMIT, Preview  # noqa: E402
from .promptbar import ProgressArea, PromptBar, complete_folder, matches_hint  # noqa: E402
from .keyspane import KeysPane  # noqa: E402
from .search import SearchPane  # noqa: E402
from .store import COLORS, Store  # noqa: E402

APP_ID = "io.github.davidnsousa.qfm"
PREVIEW_DELAY = 80   # ms the selection must settle before the preview loads
FILTER_HINT = "Type to filter this column  ·  Enter: search all folders below  ·  ↓: go to the matches  ·  Esc: close"
PATH_HINT = "Type a path  ·  Tab: complete a folder name  ·  Enter: go there  ·  Esc: back"
# Keys that open the context menu besides Menu and Shift+F10: newer Lenovo (and other)
# keyboards have a "Copilot" key in its place, which sends Super+Shift+F23 - GTK sees it as
# F23 or as this XF86 keysym, with Shift (and Super) held
COPILOT_KEYS = (0x10081247, Gdk.KEY_F23)
SORT_FIELDS = (("name", "Name"), ("size", "Size"), ("modified", "Modified"), ("type", "Type"))
DETAIL_NAMES = (("type", "Kind"), ("size", "Size"), ("modified", "Modified"), ("created", "Created"))


def arrow_keys(page, scroller):
    """↑ / ↓ move the focus through page's rows (without choosing anything - a radio button
    would otherwise switch as the focus reaches it), scrolling to keep it in view; a number
    box keeps its own ↑ / ↓."""
    page.set_focus_vadjustment(scroller.get_vadjustment())

    def on_key(widget, event):
        if event.state & Gtk.accelerator_get_default_mod_mask() or event.keyval not in (Gdk.KEY_Up, Gdk.KEY_Down):
            return False
        if isinstance(widget.get_toplevel().get_focus(), Gtk.SpinButton):
            return False
        step_focus(page, 1 if event.keyval == Gdk.KEY_Down else -1)
        return True       # (at either end: stay)

    page.connect("key-press-event", on_key)


def step_focus(page, step):
    """Move the focus to the next (step 1) or previous (-1) focusable widget in page, in the order
    they're packed (GTK's own arrow / Tab order goes by position on screen, not always right)."""
    found = []

    def walk(widget):
        if not (widget.get_visible() and widget.get_sensitive()):
            return
        if widget.get_can_focus() and not isinstance(widget, Gtk.Container) or isinstance(widget, Gtk.Button):
            found.append(widget)        # (a button is a container too: its label inside isn't a stop)
        elif isinstance(widget, Gtk.Container):
            for child in widget.get_children():
                walk(child)

    for child in page.get_children():
        walk(child)
    if not found:
        return
    focus = page.get_toplevel().get_focus()
    here = next((i for i, w in enumerate(found) if focus is w or (focus is not None and focus.is_ancestor(w))), None)
    target = 0 if here is None else min(max(here + step, 0), len(found) - 1)
    found[target].grab_focus()


def icon_button(icon_name, tooltip, toggle=False):
    button = (Gtk.ToggleButton if toggle else Gtk.Button)()
    button.add(Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.BUTTON))
    button.set_tooltip_text(tooltip)
    button.set_relief(Gtk.ReliefStyle.NONE)
    button.set_can_focus(False)
    return button


class Window(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="qfm")
        self.store = Store()
        self.options = options = self.store.options
        self.icons = style.Icons()
        self.back, self.forward = [], []     # history of jumps (go_to): [(folder, selected name)]
        self.current = None          # the selected Entry (or the focused folder's, if none)
        self.many = None             # every selected Entry when several are, else None
        self._closing = False
        self._preview_timer = 0
        self._flash_timer = 0
        self._hint = None            # what the focused toolbar box does, shown in the status bar
        self._quiet_toggles = False
        self._save_timer = 0
        self.fileops = FileOps(self)
        self._apply_window_hint()
        display = Gdk.Display.get_default()
        area = (display.get_primary_monitor() or display.get_monitor(0)).get_workarea()
        self.set_default_size(min(options["window_width"], area.width * 9 // 10),
                              min(options["window_height"], area.height * 9 // 10))
        self.set_icon_name("system-file-manager")
        self.icons.set_scale(self.get_scale_factor())
        self.connect("notify::scale-factor", lambda *_: self._scale_changed())

        self.view = MillerView(self)
        self.preview = Preview(self)
        self.search = SearchPane(self)
        self.keys = KeysPane(self)
        self.prompt = PromptBar(self)     # every question qfm asks, above the status bar
        self.progress = ProgressArea()    # running copies and moves

        # toolbar: sidebar | breadcrumbs or path box | path button | (filter) search button | options | preview
        toolbar = Gtk.Box(spacing=4)
        toolbar.get_style_context().add_class("qfm-toolbar")
        self.sidebar_button = icon_button("user-bookmarks-symbolic", "Bookmarks (Ctrl+B)", toggle=True)
        self.sidebar_button.connect("toggled", lambda b: self.set_sidebar(b.get_active()))
        self.crumbs = Gtk.Box(spacing=0)
        crumb_scroller = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.NEVER,
                                            hscrollbar_policy=Gtk.PolicyType.EXTERNAL)
        crumb_scroller.add(self.crumbs)
        self.crumb_scroller = crumb_scroller
        self.path_entry = Gtk.Entry()
        self.path_entry.connect("activate", self._on_path_entry)
        self.path_entry.connect("key-press-event", self._on_path_key)
        self.path_entry.connect("focus-in-event", lambda *_: self._set_hint(PATH_HINT))
        self.path_entry.connect("focus-out-event", self._on_path_focus_out)
        self.path_button = icon_button("document-edit-symbolic", "Type or copy the path (Ctrl+L)", toggle=True)
        self.path_button.connect("toggled", self._on_path_button)
        self.location = Gtk.Stack(hexpand=True)
        self.location.add_named(crumb_scroller, "crumbs")
        self.location.add_named(self.path_entry, "entry")
        # the filter box stays hidden until the search button or Ctrl+F opens it
        self.filter_entry = Gtk.SearchEntry(placeholder_text="Filter", width_chars=26)
        self.filter_entry.connect("search-changed", self._on_filter_changed)
        self.filter_entry.connect("activate", self._on_filter_activate)
        self.filter_entry.connect("stop-search", self._on_filter_stop)
        self.filter_entry.connect("key-press-event", self._on_filter_key)
        self.filter_entry.connect("focus-in-event", lambda *_: self._set_hint(FILTER_HINT))
        self.filter_entry.connect("focus-out-event", self._on_filter_focus_out)
        self.filter_revealer = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_LEFT,
                                            transition_duration=120)
        self.filter_revealer.add(self.filter_entry)
        self.search_button = icon_button("edit-find-symbolic", "Filter / search (Ctrl+F)")
        self.search_button.connect("clicked", lambda *_: self.focus_filter())
        actions_button = icon_button("open-menu-symbolic", "Actions (Ctrl+K) - everything the menu has, in a panel")
        actions_button.connect("clicked", lambda *_: self.toggle_actions())
        options_button = icon_button("emblem-system-symbolic", "Options (Ctrl+,)")
        options_button.connect("clicked", lambda *_: self.toggle_options())
        self.preview_button = icon_button("sidebar-show-right-symbolic", "Preview (Space)", toggle=True)
        self.preview_button.connect("toggled", lambda b: self.set_preview(b.get_active()))
        toolbar.pack_start(self.sidebar_button, False, False, 0)
        toolbar.pack_start(self.location, True, True, 0)
        toolbar.pack_start(self.path_button, False, False, 0)
        toolbar.pack_start(self.filter_revealer, False, False, 0)
        toolbar.pack_start(self.search_button, False, False, 0)
        toolbar.pack_start(actions_button, False, False, 0)
        toolbar.pack_start(options_button, False, False, 0)
        toolbar.pack_start(self.preview_button, False, False, 0)

        # body: sidebar | columns | preview or search results
        self.sidebar = Gtk.ListBox()
        self.sidebar.get_style_context().add_class("qfm-sidebar")
        self.sidebar.set_header_func(self._sidebar_header)
        self.sidebar.connect("row-activated", self._on_sidebar_activated)
        # drives: USB sticks, SD cards, phones, other partitions - as they come and go
        self.volumes = Gio.VolumeMonitor.get()
        for signal in ("mount-added", "mount-removed", "mount-changed",
                       "volume-added", "volume-removed", "volume-changed"):
            self.volumes.connect(signal, lambda *_: self.rebuild_sidebar())
        self.sidebar.connect("key-press-event", self._on_sidebar_key)
        self.sidebar.connect("button-press-event", self._on_sidebar_button)
        self.sidebar_scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.sidebar_scroller.get_style_context().add_class("qfm-sidebar-pane")
        self.sidebar_scroller.add(self.sidebar)
        self.side = Gtk.Stack(transition_type=Gtk.StackTransitionType.NONE)
        self.side.get_style_context().add_class("qfm-side")      # preview / search / keys
        # the window decides the panel's size, not what's previewed: the preview scrolls if
        # it can't fit, and pages not showing don't count (a stack sizes to its biggest page)
        self.side.set_homogeneous(False)
        preview_scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        preview_scroller.add(self.preview)
        preview_scroller.connect("size-allocate", lambda _, alloc: self.preview.fit_to(alloc.height))
        self.side.add_named(preview_scroller, "preview")
        self.side.add_named(self.search, "search")
        self.side.add_named(self.keys, "keys")
        self.side.add_named(self._options_page(), "options")
        self.side.add_named(self._actions_page(), "actions")
        self.side.add_named(self._commands_page(), "commands")
        self.inner = Gtk.Paned()
        self.inner.pack1(self.view, True, False)
        self.inner.pack2(self.side, False, False)
        self.outer = Gtk.Paned()
        self.outer.pack1(self.sidebar_scroller, False, False)
        self.outer.pack2(self.inner, True, False)
        self.outer.set_position(options["sidebar_width"])
        self._pane_placed = False
        self.inner.connect("size-allocate", self._place_pane)

        # status bar: what's selected | messages / item count
        status = Gtk.Box(spacing=12)
        status.get_style_context().add_class("qfm-statusbar")
        self.status_left = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.status_right = Gtk.Label(xalign=1, ellipsize=Pango.EllipsizeMode.START)
        self.apps = actions.AppRow(self)   # the apps that open the selection
        status.pack_start(self.apps, False, False, 0)
        status.pack_start(self.status_left, True, True, 0)
        status.pack_start(self.status_right, False, False, 0)
        terminal = icon_button("utilities-terminal-symbolic", "Open a terminal here (F4)")
        terminal.get_style_context().add_class("qfm-app")
        terminal.connect("clicked", lambda *_: self.open_terminal_here())
        status.pack_start(terminal, False, False, 0)

        layout = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        layout.pack_start(toolbar, False, False, 0)
        layout.pack_start(self.outer, True, True, 0)
        layout.pack_start(self.progress, False, False, 0)
        layout.pack_start(self.prompt, False, False, 0)
        layout.pack_start(status, False, False, 0)
        self.add(layout)
        self.connect("key-press-event", self._on_key)
        self.connect("delete-event", self._on_delete)
        # the mouse's back / forward (side) buttons, caught before the lists under the pointer
        self._side_buttons = Gtk.GestureMultiPress.new(self)
        self._side_buttons.set_button(0)
        self._side_buttons.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        self._side_buttons.connect("pressed", self._on_side_button)
        self.connect("destroy", lambda *_: setattr(self, "_destroyed", True))
        self._destroyed = False
        self.rebuild_sidebar()

    def start(self):
        """Show everything, then apply the saved sidebar/preview visibility."""
        self.show_all()
        self._quiet_toggles = True
        self.sidebar_button.set_active(self.options["sidebar"])
        self.preview_button.set_active(self.options["preview"])
        self._quiet_toggles = False
        self.sidebar_scroller.set_visible(self.options["sidebar"])
        self.side.set_visible(self.options["preview"])

    def _place_pane(self, paned, alloc):
        if not self._pane_placed and alloc.width > 1:
            self._pane_placed = True
            paned.set_position(max(200, alloc.width - self.options["preview_width"]))

    # -- navigation ----------------------------------------------------------

    def go_to(self, path, remember=True):
        """Show path: a folder opens focused, a file is selected in its folder. remember: a jump
        Alt+← can come back from (steps between columns aren't: they stay in view)."""
        path = os.path.abspath(os.path.expanduser(path))
        here = self.here()
        if remember and here and path not in (here[0], os.path.join(*here)):
            self.back.append(here)
            del self.back[:-50]
            self.forward.clear()
        inside = None if os.path.exists(path) else archive.split(path)
        if inside and inside[1]:     # inside an archive: open a folder, select a file
            try:
                member = archive.listing(inside[0]).member(inside[1])
            except (archive.Error, OSError):
                member = None
            if member is not None and member.is_dir:
                self.view.go_to(path)
            else:
                self.view.go_to(os.path.dirname(path), select=os.path.basename(path))
            return
        missing = None
        while not os.path.exists(path) and path != "/":
            missing = missing or path
            path = os.path.dirname(path)
        if missing:
            self.flash(f"{short_path(missing)} doesn't exist", error=True)
        if os.path.isdir(path):
            self.view.go_to(path)
        else:
            self.view.go_to(os.path.dirname(path), select=os.path.basename(path))

    def here(self):
        """Where you are, for the history: (the focused column's folder, the selected name or "")."""
        column = self.view.focused_column
        if column is None:
            return None
        return column.path, column.selected.name if column.selected is not None else ""

    def go_back(self, forward=False):
        """Alt+← / Alt+→ (and the mouse's back / forward buttons): the places jumped from."""
        source, other = (self.forward, self.back) if forward else (self.back, self.forward)
        if not source:
            return self.flash("Nothing further forward" if forward else "No earlier place to go back to")
        here = self.here()
        if here:
            other.append(here)
        folder, name = source.pop()
        if name and (os.path.isdir(folder) or archive.split(folder)):
            self.view.go_to(folder, select=name)      # selected again, not entered
        else:
            self.go_to(folder, remember=False)

    def _on_side_button(self, gesture, *_):
        button = gesture.get_current_button()
        if button in (8, 9):
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            self.go_back(forward=button == 9)

    def select_matching(self):
        """Ctrl+S: select the focused column's items whose names match a pattern (* ? [...]:
        like the shell; plain text: names containing it), case-insensitive."""
        column = self.view.focused_column
        if column is None:
            return

        def matches(pattern):
            pattern = pattern.strip().casefold()
            if not pattern:
                return []
            if not any(c in pattern for c in "*?["):
                pattern = f"*{pattern}*"
            return [e for e in column.visible if fnmatch.fnmatchcase(e.name.casefold(), pattern)]

        def preview(pattern):
            found = matches(pattern)
            if not pattern.strip():
                return "* = anything, ? = one character, plain text = names containing it", False
            if not found:
                return "Nothing matches", True
            names = ", ".join(e.name for e in found[:3])
            return f"{len(found)} match{'es' if len(found) != 1 else ''}: {names}{', …' if len(found) > 3 else ''}", False

        def select(pattern):
            found = matches(pattern)
            if not found:
                return "Nothing matches"
            self.prompt.close(refocus=False)
            column.tree.grab_focus()
            column.select_names([e.name for e in found])
            self.view.selected(column, column.selected)
            self.flash(f"Selected {len(found)} item{'s' if len(found) != 1 else ''}")

        self.prompt.ask_fields("Select matching:", [("*.jpg, IMG_*, report…", "")], select, "Select",
                               on_change=preview)

    def go_into_selection(self):
        column = self.view.focused_column
        if column is not None:
            self.view.go_right(column)

    def location_changed(self, path):
        if self._destroyed:       # columns going away while the window closes
            return
        for child in self.crumbs.get_children():
            child.destroy()
        if trash.in_trash(path):
            parts = [("Trash", trash.FILES)]
            rest = path[len(trash.FILES):].strip(os.sep)
            base = trash.FILES
        elif path == HOME or path.startswith(HOME + os.sep):
            parts = [("~", HOME)]
            rest = path[len(HOME):].strip(os.sep)
            base = HOME
        else:
            parts = [("/", "/")]
            rest = path.strip(os.sep)
            base = "/"
        for name in rest.split(os.sep) if rest else []:
            base = os.path.join(base, name)
            parts.append((name, base))
        for i, (name, target) in enumerate(parts):
            if i:
                sep = Gtk.Label(label="›")
                sep.get_style_context().add_class("qfm-crumb-sep")
                self.crumbs.pack_start(sep, False, False, 0)
            button = Gtk.Button(label=name, relief=Gtk.ReliefStyle.NONE, can_focus=False)
            button.get_style_context().add_class("qfm-crumb")
            if i == len(parts) - 1:
                button.get_style_context().add_class("current")
            button.connect("clicked", lambda _, t=target: self.go_to(t))
            self.crumbs.pack_start(button, False, False, 0)
        self.crumbs.show_all()
        GLib.idle_add(self._crumbs_to_end)
        self.set_title(f"{'Trash' if path == trash.FILES else os.path.basename(path) or path} - qfm")
        self.prompt.location_changed(path)
        self._update_status()

    def _crumbs_to_end(self):
        if self._destroyed:
            return False
        adj = self.crumb_scroller.get_hadjustment()
        adj.set_value(adj.get_upper())
        return False

    def selection_changed(self, entry, many=None):
        if entry is None:
            column = self.view.focused_column
            entry = column.folder_entry() if column else None
        self.current = entry
        self.many = list(many) if many else None
        self._update_status()
        if entry is not None:
            self.options["last"] = entry.path
            self._save_soon()
        if self._preview_timer:
            GLib.source_remove(self._preview_timer)
        self._preview_timer = GLib.timeout_add(PREVIEW_DELAY, self._load_preview)

    def _load_preview(self):
        self._preview_timer = 0
        self.apps.show_for(self.many or ([self.current] if self.current is not None else []))
        self.refresh_preview()
        return False

    def refresh_preview(self):
        if self.side.get_visible() and self.side.get_visible_child_name() == "actions":
            return self.refill_actions()
        if not self.side.get_visible() or self.side.get_visible_child_name() != "preview":
            return
        if self.many:
            self.preview.show_many(self.many)
        else:
            self.preview.show_entry(self.current)

    def selected_entry(self):
        """What actions apply to: the selection, or the focused folder."""
        return self.current

    def targets(self):
        """What file operations apply to: the items actually selected in the focused
        column - never the folder shown when nothing is selected."""
        column = self.view.focused_column
        return list(column.marked) if column is not None else []

    def chosen(self):
        """What other actions (open, bookmark, color, reveal, copy path) apply to:
        the selected items, or the shown folder when nothing is selected."""
        return self.targets() or ([self.current] if self.current else [])

    def close_after_open(self):
        """Something was opened in another app: as a quick dialog, qfm's job is done."""
        if self.options["window_mode"] != "dialog":
            return
        try:
            Gio.bus_get_sync(Gio.BusType.SESSION, None).flush_sync(None)   # D-Bus-activated apps
        except GLib.Error:
            pass
        if not self._closing:
            self._closing = True
            GLib.timeout_add(150, lambda: self.close() and False)

    def _save_soon(self):
        """Save the options a moment after the last change (navigation, panes)."""
        if self._save_timer:
            GLib.source_remove(self._save_timer)
        self._save_timer = GLib.timeout_add(1000, self._save_now)

    def _save_now(self):
        self._save_timer = 0
        self.store.save_options()
        return False

    def pointer_modifiers(self):
        """Modifier keys held right now (for drag and drop)."""
        pointer = self.get_display().get_default_seat().get_pointer()
        return self.get_window().get_device_position(pointer)[3]

    def open_with_number(self, n):
        """Alt+n: open the selection with the nth app other than the default (that's Enter), as
        numbered in the status bar (Alt+0: choose any app)."""
        entries = self.chosen()
        if not entries:
            return
        if n == 0:
            return actions.choose_app(self, entries)
        others = actions.alternatives(*actions.apps_for(actions.types_of(entries)))
        if n > len(others):
            if not others:
                return self.flash("No other app opens this - Alt+0 picks any app", error=True)
            return self.flash(f"There are {len(others)} other apps for this - Alt+1…{len(others)}, "
                              "or Alt+0 for any app", error=True)
        actions.launch(self, others[n - 1], entries)

    def _in_columns(self):
        focus = self.get_focus()
        return isinstance(focus, Gtk.TreeView) and focus.is_ancestor(self.view)

    # -- status bar ----------------------------------------------------------

    def _update_status(self):
        entry = self.current
        if self.many:
            files = [e for e in self.many if not e.is_dir]
            folders = len(self.many) - len(files)
            parts = [f"{len(self.many)} selected"]
            if files:
                parts.append(f"{len(files)} file{'s' if len(files) > 1 else ''}, "
                             f"{human_size(sum(e.size for e in files))}")
            if folders:
                parts.append(f"{folders} folder{'s' if folders > 1 else ''}")
            self.status_left.set_text("  ·  ".join(parts))
        elif entry is None:
            self.status_left.set_text("")
        elif entry.is_dir:
            branch = entry.repo and git_branch(entry.path)
            kind = f"git repository on {branch}" if branch else "git repository" if entry.repo else "folder"
            self.status_left.set_text(f"{entry.display}  ·  {kind}  ·  modified {human_date(entry.modified)}")
        else:
            self.status_left.set_text(f"{entry.display}  ·  {describe(entry.content_type)}  ·  "
                                      f"{human_size(entry.size)}  ·  modified {human_date(entry.modified)}")
        if not self._flash_timer:
            column = self.view.focused_column
            if self._hint:
                self.status_right.set_text(self._hint)
            elif column is not None and column.filter:
                total = sum(1 for e in column.entries if self.options["show_hidden"] or not e.hidden)
                self.status_right.set_text(f"{len(column.visible)} of {total} items match “{column.filter}”"
                                           "  ·  Esc clears the filter")
            elif column is not None:
                n = len(column.visible)
                hidden = "" if self.options["show_hidden"] else ", hidden files not shown"
                self.status_right.set_text(f"{n} item{'s' if n != 1 else ''} in {short_path(column.path)}{hidden}")
            self.status_right.get_style_context().remove_class("qfm-status-error")

    def _set_hint(self, text):
        self._hint = text
        self._update_status()

    def flash(self, text, error=False):
        """A message in the status bar for a few seconds."""
        if self._flash_timer:
            GLib.source_remove(self._flash_timer)
        context = self.status_right.get_style_context()
        (context.add_class if error else context.remove_class)("qfm-status-error")
        self.status_right.set_text(text)
        self._flash_timer = GLib.timeout_add(5000 if error else 3000, self._end_flash)

    def _end_flash(self):
        self._flash_timer = 0
        self._update_status()
        return False

    # -- panes ---------------------------------------------------------------

    def set_sidebar(self, shown):
        if self._quiet_toggles:
            return
        self.options["sidebar"] = shown
        self._save_soon()
        self.sidebar_scroller.set_visible(shown)
        if shown:
            row = self.sidebar.get_selected_row() or self.sidebar.get_row_at_index(0)
            if row:
                row.grab_focus()
        else:
            self.focus_columns()

    def toggle_sidebar(self):
        """Hidden: show and focus it. Shown: focus it, or hide it if it has the focus."""
        if not self.sidebar_scroller.get_visible():
            self.sidebar_button.set_active(True)
        elif self.get_focus() is not None and self.get_focus().is_ancestor(self.sidebar):
            self.sidebar_button.set_active(False)
        else:
            row = self.sidebar.get_selected_row() or self.sidebar.get_row_at_index(0)
            if row:
                row.grab_focus()

    def set_preview(self, shown):
        if self._quiet_toggles:
            return
        self.options["preview"] = shown
        self._save_soon()
        self.side.set_visible(shown)
        if not shown:
            self.preview.stop_media()
        if shown:
            self.side.set_visible_child_name("preview")
            self.refresh_preview()

    def toggle_preview(self):
        self.preview_button.set_active(not self.side.get_visible()
                                       or self.side.get_visible_child_name() != "preview")
        if self.side.get_visible_child_name() == "search":
            self.close_search()

    def switch_pane(self):
        """F6: from the columns to the side panel showing (Actions, Options, keys, search) and
        back, leaving it open."""
        if not self._in_columns():
            return self.focus_columns()
        if not self.side.get_visible():
            return self.flash("No side panel is open")
        page = self.side.get_visible_child_name()
        if page == "actions":
            self.focus_actions()
        elif page == "options":
            self.options_first.grab_focus()
        elif page == "commands":
            self.cmd_name.grab_focus()
        elif page == "keys":
            self.keys.filter.grab_focus()
        elif page == "search":
            self.search.focus()
        else:
            self.flash("The preview has nothing to focus - F6 works with Actions, Options, keys and search")

    def focus_columns(self):
        column = self.view.focused_column
        if column is not None:
            self.view.focus(column)

    # -- sidebar: places and bookmarks ---------------------------------------

    def _scale_changed(self):
        """Moved to a screen with another scale: reload the icons at that scale."""
        if self.icons.set_scale(self.get_scale_factor()):
            self.view.refresh_all()
            self.rebuild_sidebar()
            self.refresh_preview()

    def rebuild_sidebar(self):
        for row in self.sidebar.get_children():
            row.destroy()
        self._sidebar_row("Home", HOME, "user-home-symbolic", "places")
        self._sidebar_row("Applications", APPLICATIONS, "view-app-grid-symbolic", "places")
        self._sidebar_row("Computer", "/", "drive-harddisk-symbolic", "places")
        self._sidebar_row("Trash", trash.FILES,
                          "user-trash-symbolic" if trash.is_empty() else "user-trash-full-symbolic", "places")
        for device in self.devices():
            self._device_row(device)
        for path in self.store.bookmarks:
            self._sidebar_row(os.path.basename(path) or path, path, None, "bookmarks")
        if not self.store.bookmarks:
            hint = Gtk.ListBoxRow(selectable=False, activatable=False)
            label = Gtk.Label(label="Ctrl+D bookmarks the selection", xalign=0, wrap=True)
            label.get_style_context().add_class("qfm-preview-key")
            hint.add(label)
            hint.section, hint.path = "bookmarks", None
            self.sidebar.add(hint)
        self.sidebar.show_all()

    def _sidebar_row(self, name, path, icon_name, section):
        row = Gtk.ListBoxRow()
        row.path, row.section = path, section
        box = Gtk.Box(spacing=8)
        if icon_name:
            image = Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.MENU)
        else:
            entry = Entry.from_path(path)
            if entry is None:
                image = Gtk.Image.new_from_icon_name("dialog-warning-symbolic", Gtk.IconSize.MENU)
                row.set_tooltip_text(f"{short_path(path)} (missing - Delete removes it)")
            else:
                image = self.icons.image(self.icons.for_entry(entry, self.store.colors))
                row.set_tooltip_text(short_path(path))
        box.pack_start(image, False, False, 0)
        box.pack_start(Gtk.Label(label=name, xalign=0, ellipsize=Pango.EllipsizeMode.END), True, True, 0)
        if section == "bookmarks":
            remove = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.MENU)
            remove.set_relief(Gtk.ReliefStyle.NONE)
            remove.set_can_focus(False)
            remove.set_tooltip_text("Remove this bookmark (Delete)")
            remove.get_style_context().add_class("qfm-unbookmark")
            remove.connect("clicked", lambda *_: self.remove_bookmark(path))
            box.pack_start(remove, False, False, 0)
        row.add(box)
        self.sidebar.add(row)

    def devices(self):
        """Drives to list: mounted ones (with their folder) and ones that can be mounted."""
        found = []
        for mount in self.volumes.get_mounts():
            root = mount.get_root().get_path()
            if root and not mount.is_shadowed():
                found.append({"name": mount.get_name(), "path": root, "icon": mount.get_symbolic_icon(),
                              "mount": mount, "volume": None})
        for volume in self.volumes.get_volumes():
            if volume.get_mount() is None and volume.can_mount():
                found.append({"name": volume.get_name(), "path": None, "icon": volume.get_symbolic_icon(),
                              "mount": None, "volume": volume})
        return found

    def _device_row(self, device):
        row = Gtk.ListBoxRow()
        row.path, row.section, row.volume = device["path"], "devices", device["volume"]
        box = Gtk.Box(spacing=8)
        box.pack_start(Gtk.Image.new_from_gicon(device["icon"], Gtk.IconSize.MENU), False, False, 0)
        label = Gtk.Label(label=device["name"], xalign=0, ellipsize=Pango.EllipsizeMode.END)
        if device["path"] is None:
            label.get_style_context().add_class("qfm-preview-key")     # not mounted yet
            row.set_tooltip_text(f"{device['name']} - not mounted; click to mount it")
        else:
            row.set_tooltip_text(short_path(device["path"]))
        box.pack_start(label, True, True, 0)
        mount = device["mount"]
        if mount is not None and (mount.can_eject() or mount.can_unmount()):
            eject = Gtk.Button.new_from_icon_name("media-eject-symbolic", Gtk.IconSize.MENU)
            eject.set_relief(Gtk.ReliefStyle.NONE)
            eject.set_can_focus(False)
            eject.set_tooltip_text("Eject" if mount.can_eject() else "Unmount")
            eject.connect("clicked", lambda *_: self.eject(mount))
            box.pack_start(eject, False, False, 0)
        row.add(box)
        self.sidebar.add(row)

    def _on_sidebar_activated(self, listbox, row):
        if getattr(row, "volume", None) is not None:
            return self.mount(row.volume)
        if row.path:
            self.go_to(row.path)

    def mount(self, volume):
        """Mount a drive (asking for a password if the system wants one), then open it."""
        def done(volume, result):
            try:
                volume.mount_finish(result)
            except GLib.Error as err:
                return self.flash(f"Could not mount {volume.get_name()}: {err.message}", error=True)
            mount = volume.get_mount()
            if mount is not None and mount.get_root().get_path():
                self.go_to(mount.get_root().get_path())
            self.flash(f"Mounted {volume.get_name()}")

        self.flash(f"Mounting {volume.get_name()}…")
        volume.mount(Gio.MountMountFlags.NONE, Gtk.MountOperation(parent=self), None, done)

    def eject(self, mount):
        """Eject (or unmount) a drive; if qfm was showing it, go home."""
        root = mount.get_root().get_path() or ""
        name = mount.get_name()
        column = self.view.focused_column
        if column is not None and root and (column.path == root or column.path.startswith(root + os.sep)):
            self.go_to(HOME)     # let go of it first: open folders keep a drive busy

        def done(mount, result):
            try:
                (mount.eject_with_operation_finish if eject else mount.unmount_with_operation_finish)(result)
                self.flash(f"{name} can be removed now" if eject else f"Unmounted {name}")
            except GLib.Error as err:
                self.flash(f"Could not {'eject' if eject else 'unmount'} {name}: {err.message}", error=True)

        eject = mount.can_eject()
        operation = Gtk.MountOperation(parent=self)
        if eject:
            mount.eject_with_operation(Gio.MountUnmountFlags.NONE, operation, None, done)
        else:
            mount.unmount_with_operation(Gio.MountUnmountFlags.NONE, operation, None, done)

    def _sidebar_header(self, row, before):
        if before is None or before.section != row.section:
            label = Gtk.Label(label=row.section.capitalize(), xalign=0)
            label.get_style_context().add_class("qfm-sidebar-heading")
            row.set_header(label)
        else:
            row.set_header(None)

    def _on_sidebar_key(self, listbox, event):
        row = listbox.get_selected_row() or listbox.get_focus_child()
        if event.keyval == Gdk.KEY_Delete and row is not None and row.section == "bookmarks" and row.path:
            self.remove_bookmark(row.path, refocus=True)
            return True
        if event.keyval in (Gdk.KEY_Escape, Gdk.KEY_Right):
            self.focus_columns()
            return True
        return False

    def remove_bookmark(self, path, refocus=False):
        """Take path off the bookmarks (the sidebar's ×, its right-click menu, Delete there)."""
        if path not in self.store.bookmarks:
            return
        index = next((r.get_index() for r in self.sidebar.get_children() if getattr(r, "path", None) == path), 0)
        self.store.toggle_bookmark(path)
        self.rebuild_sidebar()
        if refocus:
            rows = self.sidebar.get_children()
            rows[min(index, len(rows) - 1)].grab_focus()
        self.flash(f"Removed bookmark {short_path(path)}")

    def _on_sidebar_button(self, listbox, event):
        """Right-click on a bookmark: Open, Remove Bookmark."""
        if event.type != Gdk.EventType.BUTTON_PRESS or event.get_button()[1] != 3:
            return False
        row = listbox.get_row_at_y(int(event.y))
        if row is None or getattr(row, "section", None) != "bookmarks" or not row.path:
            return False
        menu = Gtk.Menu()
        menu.attach_to_widget(listbox, None)
        sink = actions.MenuSink(menu)
        sink.item("Open", lambda: self.go_to(row.path), "folder-open-symbolic")
        sink.item("Remove Bookmark", lambda: self.remove_bookmark(row.path), "non-starred-symbolic", Gdk.KEY_Delete)
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    # -- actions on the selection --------------------------------------------

    def terminal_folder(self):
        """Where "here" is: the focused column's folder (for an archive, the folder it's in)."""
        column = self.view.focused_column
        if column is None:
            return HOME
        return os.path.dirname(column.archive) if column.archive is not None else column.path

    def open_terminal_here(self):
        actions.open_terminal(self, self.terminal_folder())

    def open_entries(self, entries):
        actions.open_default(self, entries)

    def toggle_bookmark(self):
        """Bookmark the chosen items; if they all are already, remove their bookmarks."""
        entries = self.chosen()
        if not entries:
            return
        if any(e.archive is not None for e in entries):
            return self.flash("Things inside archives can't be bookmarked - bookmark the archive", error=True)
        remove = all(e.path in self.store.bookmarks for e in entries)
        for e in entries:
            if (e.path in self.store.bookmarks) == remove:
                self.store.toggle_bookmark(e.path)
        self.rebuild_sidebar()
        what = short_path(entries[0].path) if len(entries) == 1 else f"{len(entries)} items"
        self.flash(f"{'Removed bookmark' if remove else 'Bookmarked'} {what}")

    def set_color(self, color):
        folders = [e for e in self.chosen() if e.is_dir and e.archive is None]
        if not folders:
            self.flash("Colors are for folders", error=True)
            return
        for e in folders:
            self.store.set_color(e.path, color)
        for parent in {os.path.dirname(e.path) for e in folders}:
            self.view.refresh_folder(parent)
        self.rebuild_sidebar()
        self.refresh_preview()
        what = short_path(folders[0].path) if len(folders) == 1 else f"{len(folders)} folders"
        self.flash(f"{what}: {color or 'no color'}")

    def show_menu(self, event=None):
        entry = self.selected_entry()
        if entry is None:
            return
        menu = actions.build_menu(self, entry, self.chosen())
        if event is not None:
            menu.popup_at_pointer(event)
            return
        column = self.view.focused_column
        rect = column.row_rect() if column is not None and column.selected is entry else None
        if rect is not None:
            menu.popup_at_rect(column.tree.get_bin_window(), rect,
                               Gdk.Gravity.SOUTH_WEST, Gdk.Gravity.NORTH_WEST, None)
        else:
            menu.popup_at_widget(self.view, Gdk.Gravity.CENTER, Gdk.Gravity.CENTER, None)

    # -- filter, search, path entry ------------------------------------------

    def focus_filter(self):
        """Ctrl+F / the search button: open the filter box and type in it."""
        self.filter_revealer.set_reveal_child(True)
        self.filter_entry.grab_focus()

    def hide_filter(self):
        self.filter_revealer.set_reveal_child(False)

    def filter_cleared(self):
        """The focus moved to another column, which cleared the filter."""
        if self.filter_entry.get_text():
            self.filter_entry.set_text("")
        if not self.filter_entry.has_focus():
            self.hide_filter()

    def _on_filter_changed(self, entry):
        self.view.set_filter(entry.get_text().strip())
        self._update_status()

    def _on_filter_focus_out(self, entry, _):
        self._set_hint(None)
        if not entry.get_text().strip():     # an active filter keeps its box open, to show what it is
            self.hide_filter()
        return False

    def _on_filter_activate(self, entry):
        query = entry.get_text().strip()
        column = self.view.focused_column
        if not query or column is None:
            return
        self.preview.stop_media()
        self.side.set_visible_child_name("search")
        self.side.set_visible(True)
        if column.archive is not None:
            self.search.start_archive(column, query)
        else:
            self.search.start(column.path, query, self.options["show_hidden"])
        entry.set_text("")   # the column shows everything again; the pane keeps the query
        self.view.set_filter("")
        self.hide_filter()
        self.search.focus()

    def _on_filter_stop(self, entry):
        entry.set_text("")
        self.view.set_filter("")
        self.hide_filter()
        self.focus_columns()

    def _on_filter_key(self, entry, event):
        if event.keyval == Gdk.KEY_Down:
            self.focus_columns()
            return True
        return False

    def close_search(self):
        self.search.cancel()
        self.side.set_visible_child_name("preview")
        self.side.set_visible(self.options["preview"])
        self.refresh_preview()
        self.focus_columns()

    def toggle_keys(self):
        """F1: the keyboard shortcuts in the side pane, or back to the preview."""
        if self.side.get_visible() and self.side.get_visible_child_name() == "keys":
            return self.close_keys()
        self.preview.stop_media()
        self.side.set_visible_child_name("keys")
        self.side.set_visible(True)
        self.keys.focus()

    def close_keys(self):
        self.side.set_visible_child_name("preview")
        self.side.set_visible(self.options["preview"])
        self.refresh_preview()
        self.focus_columns()

    def toggle_options(self):
        """Ctrl+, / the gear button: the Options panel in the side pane, or back to the preview."""
        if self.side.get_visible() and self.side.get_visible_child_name() == "options":
            return self.close_options()
        self.preview.stop_media()
        self.side.set_visible_child_name("options")
        self.side.set_visible(True)
        self.options_first.grab_focus()

    close_options = close_keys    # (both go back to the preview, or no side pane)
    close_actions = close_keys

    def _actions_page(self):
        """The Actions panel (Ctrl+K): the menu's actions for the selection, as rows that stay
        while you move around (refill_actions follows the selection)."""
        header = Gtk.Box(spacing=6)
        self.actions_title = Gtk.Label(label="Actions", xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.actions_title.get_style_context().add_class("qfm-pane-heading")
        close = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.MENU)
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.set_tooltip_text("Close (Ctrl+K or Esc)")
        close.connect("clicked", lambda *_: self.close_actions())
        header.pack_start(self.actions_title, True, True, 0)
        header.pack_start(close, False, False, 0)
        # "main": the actions; one more page per submenu (Open With, Folder Color…)
        self.actions_pages = Gtk.Stack(transition_type=Gtk.StackTransitionType.SLIDE_LEFT_RIGHT,
                                       transition_duration=120, vhomogeneous=False)
        scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.add(self.actions_pages)
        self.actions_scroller = scroller
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                       margin_start=8, margin_end=8, margin_top=8, margin_bottom=8)
        page.pack_start(header, False, False, 0)
        page.pack_start(scroller, True, True, 0)
        return page

    # -- your commands (Run Command in the menu / Actions) -------------------------

    def _commands_page(self):
        """The Commands page: your commands (edit, delete) and a form to add or change one."""
        self._editing = None     # the index of the command in the form, None: a new one
        header = Gtk.Box(spacing=6)
        title = Gtk.Label(label="Commands", xalign=0)
        title.get_style_context().add_class("qfm-pane-heading")
        close = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.MENU)
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.set_tooltip_text("Close (Esc)")
        close.connect("clicked", lambda *_: self.close_keys())
        header.pack_start(title, True, True, 0)
        header.pack_start(close, False, False, 0)

        self.commands_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=10)
        self.form_title = Gtk.Label(label="NEW COMMAND", xalign=0)
        self.form_title.get_style_context().add_class("qfm-section")
        self.cmd_name = Gtk.Entry(placeholder_text="Name, e.g. Convert to JPEG")
        self.cmd_line = Gtk.Entry(placeholder_text="Command, e.g. magick %f %f.jpg")
        self.cmd_line.get_style_context().add_class("mono")
        self.cmd_for = Gtk.Entry(placeholder_text="For: *.png *.webp  (empty: anything)")
        self.cmd_terminal = Gtk.CheckButton(label="Run in a terminal (it waits for Enter at the end)")
        buttons = Gtk.Box(spacing=6)
        self.cmd_save = Gtk.Button(label="Add")
        self.cmd_save.get_style_context().add_class("suggested-action")
        self.cmd_save.connect("clicked", lambda *_: self._save_command())
        self.cmd_cancel = Gtk.Button(label="Cancel edit")
        self.cmd_cancel.connect("clicked", lambda *_: self._edit_command(None))
        self.cmd_cancel.set_no_show_all(True)
        buttons.pack_end(self.cmd_save, False, False, 0)
        buttons.pack_end(self.cmd_cancel, False, False, 0)
        for entry in (self.cmd_name, self.cmd_line, self.cmd_for):
            entry.connect("activate", lambda *_: self._save_command())
        help_text = Gtk.Label(xalign=0, wrap=True, max_width_chars=34, margin_top=6, use_markup=True, label=(
            "<tt>%f</tt> the item   <tt>%F</tt> all selected\n<tt>%n</tt> its name   <tt>%N</tt> all names\n"
            "<tt>%d</tt> their folder\nWith <tt>%f</tt> / <tt>%n</tt> it runs once per item; with none, "
            "the items go at the end. It runs in their folder."))
        help_text.get_style_context().add_class("qfm-preview-key")
        for widget in (self.form_title, self.cmd_name, self.cmd_line, self.cmd_for, self.cmd_terminal, buttons,
                       help_text):
            form.pack_start(widget, False, False, 0)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.pack_start(self.commands_list, False, False, 0)
        box.pack_start(form, False, False, 0)
        scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.add(box)
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                       margin_start=8, margin_end=8, margin_top=8, margin_bottom=8)
        page.pack_start(header, False, False, 0)
        page.pack_start(scroller, True, True, 0)
        self._list_commands()
        return page

    def _list_commands(self):
        for child in self.commands_list.get_children():
            child.destroy()
        if not self.store.commands:
            empty = Gtk.Label(label="No commands yet - add one below. They show in the menu's Run Command.",
                              xalign=0, wrap=True, max_width_chars=34)
            empty.get_style_context().add_class("qfm-preview-key")
            self.commands_list.pack_start(empty, False, False, 0)
        for i, cmd in enumerate(self.store.commands):
            row = Gtk.Box(spacing=4)
            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            name = Gtk.Label(label=cmd["name"] + ("   ▭ terminal" if cmd.get("terminal") else ""), xalign=0,
                             ellipsize=Pango.EllipsizeMode.END)
            line = Gtk.Label(label=cmd["command"] + (f"   · for {cmd['patterns']}" if cmd.get("patterns") else ""),
                             xalign=0, ellipsize=Pango.EllipsizeMode.END)
            line.get_style_context().add_class("qfm-preview-key")
            line.get_style_context().add_class("mono")
            text.pack_start(name, False, False, 0)
            text.pack_start(line, False, False, 0)
            row.pack_start(text, True, True, 0)
            for icon, tip, callback in (("document-edit-symbolic", "Edit", lambda _, i=i: self._edit_command(i)),
                                        ("user-trash-symbolic", "Delete", lambda _, i=i: self._delete_command(i))):
                button = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.MENU)
                button.set_relief(Gtk.ReliefStyle.NONE)
                button.set_tooltip_text(tip)
                button.connect("clicked", callback)
                row.pack_start(button, False, False, 0)
            self.commands_list.pack_start(row, False, False, 0)
        self.commands_list.show_all()

    def _edit_command(self, index):
        """Put command index into the form (None: an empty form for a new one)."""
        self._editing = index
        cmd = self.store.commands[index] if index is not None else commands.new()
        self.cmd_name.set_text(cmd["name"])
        self.cmd_line.set_text(cmd["command"])
        self.cmd_for.set_text(cmd.get("patterns", ""))
        self.cmd_terminal.set_active(bool(cmd.get("terminal")))
        self.form_title.set_text("NEW COMMAND" if index is None else "EDIT COMMAND")
        self.cmd_save.set_label("Add" if index is None else "Save")
        self.cmd_cancel.set_visible(index is not None)
        self.cmd_name.grab_focus()

    def _save_command(self):
        cmd = commands.new(self.cmd_name.get_text().strip(), self.cmd_line.get_text().strip(),
                           self.cmd_for.get_text().strip(), self.cmd_terminal.get_active())
        if not cmd["name"] or not cmd["command"]:
            return self.flash("A command needs a name and a command line", error=True)
        if self._editing is None:
            self.store.commands.append(cmd)
        else:
            self.store.commands[self._editing] = cmd
        self.store.save_commands()
        self.flash(f"Saved “{cmd['name']}” - it's in the menu's Run Command")
        self._list_commands()
        self._edit_command(None)

    def _delete_command(self, index):
        cmd = self.store.commands.pop(index)
        self.store.save_commands()
        if self._editing == index:
            self._edit_command(None)
        elif self._editing is not None and self._editing > index:
            self._editing -= 1
        self._list_commands()
        self.flash(f"Deleted “{cmd['name']}”")

    def toggle_commands(self):
        """The Commands page in the side pane (from Run Command → Add or Edit Commands…)."""
        if self.side.get_visible() and self.side.get_visible_child_name() == "commands":
            return self.close_keys()
        self.preview.stop_media()
        self.side.set_visible_child_name("commands")
        self.side.set_visible(True)
        self._edit_command(None)

    def _after_action(self):
        """An action in the panel ran: show what's true now (Add / Remove Bookmark, …)."""
        GLib.timeout_add(150, lambda: (self.side.get_visible_child_name() == "actions"
                                       and self.refill_actions(), False)[1])

    def toggle_actions(self):
        """Ctrl+K / the ☰ button: the Actions panel in the side pane, or back to the preview."""
        if self.side.get_visible() and self.side.get_visible_child_name() == "actions":
            return self.close_actions()
        self.preview.stop_media()
        self.side.set_visible_child_name("actions")
        self.side.set_visible(True)
        self.refill_actions()
        self.focus_actions()

    def refill_actions(self):
        for child in self.actions_pages.get_children():
            child.destroy()
        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        arrow_keys(main, self.actions_scroller)
        self.actions_pages.add_named(main, "main")
        entry, entries = self.selected_entry(), self.chosen()
        if entry is None or not entries:
            self.actions_title.set_text("Actions")
            main.pack_start(Gtk.Label(label="Select something", xalign=0), False, False, 0)
        else:
            self.actions_title.set_text(f"Actions · {actions.what(entries)}")
            actions.fill_actions(actions.PanelSink(main, self._after_action, self.actions_pages), self, entry, entries)
        self.actions_pages.show_all()
        self.actions_pages.set_visible_child_name("main")
        if self.get_focus() is None or self.get_focus().is_ancestor(self.actions_pages):
            self.focus_actions()

    def focus_actions(self):
        """The first row of the actions page showing (↑ ↓ from there)."""
        page = self.actions_pages.get_visible_child()
        rows = [c for c in page.get_children() if c.get_can_focus()] if page else []
        if rows:
            rows[0].grab_focus()

    def show_path_entry(self):
        """Ctrl+L / the ✎ button: the current folder's full path in a box, to read, copy or edit."""
        column = self.view.focused_column
        path = column.path if column else HOME
        self.path_entry.set_text(path.rstrip(os.sep) + os.sep)
        self.location.set_visible_child_name("entry")
        self._quiet_toggles = True
        self.path_button.set_active(True)
        self._quiet_toggles = False
        self.path_entry.grab_focus()
        self.path_entry.select_region(0, -1)    # ready to copy; typing replaces it, → keeps it

    def hide_path_entry(self, refocus=True):
        self.location.set_visible_child_name("crumbs")
        self._quiet_toggles = True
        self.path_button.set_active(False)
        self._quiet_toggles = False
        if refocus:
            self.focus_columns()

    def _on_path_button(self, button):
        if self._quiet_toggles:
            return
        if button.get_active():
            self.show_path_entry()
        else:
            self.hide_path_entry()

    def _on_path_entry(self, entry):
        text = entry.get_text().strip()
        self.hide_path_entry(refocus=False)
        if text:
            self.go_to(text)

    def _on_path_key(self, entry, event):
        if event.keyval == Gdk.KEY_Escape:
            self.hide_path_entry()
            return True
        if event.keyval == Gdk.KEY_Tab:
            text, names = complete_folder(entry.get_text())
            entry.set_text(text)
            entry.set_position(-1)
            hint = matches_hint(names)
            if hint:
                self.flash(hint, error=not names)
            return True
        return False

    def _on_path_focus_out(self, entry, _):
        self._set_hint(None)
        if self.location.get_visible_child_name() == "entry":
            self.hide_path_entry(refocus=False)
        return False

    # -- options -------------------------------------------------------------

    def _options_page(self):
        """The Options panel (the gear button, Ctrl+,) in the side pane: how the columns look,
        the window, and qfm as the default file manager. Changes apply at once."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)

        def heading(text):
            label = Gtk.Label(label=text, xalign=0, margin_top=12 if box.get_children() else 0)
            label.get_style_context().add_class("qfm-pane-heading")
            box.pack_start(label, False, False, 0)

        def note(text):
            label = Gtk.Label(label=text, xalign=0, wrap=True, max_width_chars=30, margin_start=24)
            label.get_style_context().add_class("qfm-preview-key")
            box.pack_start(label, False, False, 0)

        heading("Sort by")
        group = None
        for key, label in SORT_FIELDS:
            radio = Gtk.RadioButton(label=label, group=group, active=self.options["sort"] == key)
            group = group or radio
            radio.connect("toggled", lambda b, k=key: b.get_active() and self._set_option("sort", k))
            box.pack_start(radio, False, False, 0)
        descending = Gtk.CheckButton(label="Descending", active=self.options["descending"])
        descending.connect("toggled", lambda b: self._set_option("descending", b.get_active()))
        box.pack_start(descending, False, False, 0)
        heading("Details")
        for key, label in DETAIL_NAMES:
            check = Gtk.CheckButton(label=label, active=key in self.options["details"])
            check.connect("toggled", lambda b, k=key: self._toggle_detail(k, b.get_active()))
            box.pack_start(check, False, False, 0)
        heading("Panels")
        fit = Gtk.CheckButton(label="Fit to the window, no scrolling", active=self.options["fit_columns"])
        most = Gtk.Box(spacing=6, margin_start=24, sensitive=self.options["fit_columns"])
        most.pack_start(Gtk.Label(label="At most"), False, False, 0)
        count = Gtk.SpinButton.new_with_range(1, 9, 1)
        count.set_value(self.options["max_columns"])
        count.connect("value-changed", lambda b: self._set_option("max_columns", b.get_value_as_int()))
        most.pack_start(count, False, False, 0)
        most.pack_start(Gtk.Label(label="panels"), False, False, 0)
        fit.connect("toggled", lambda b: (most.set_sensitive(b.get_active()),
                                          self._set_option("fit_columns", b.get_active())))
        box.pack_start(fit, False, False, 0)
        box.pack_start(most, False, False, 0)
        heading("Show")
        self.hidden_check = Gtk.CheckButton(label="Hidden files (Ctrl+H)", active=self.options["show_hidden"])
        self.hidden_check.connect("toggled", lambda b: self._set_option("show_hidden", b.get_active()))
        box.pack_start(self.hidden_check, False, False, 0)
        thumbnails = Gtk.CheckButton(label="Thumbnails (bigger rows)", active=self.options["thumbnails"])
        thumbnails.connect("toggled", lambda b: self._set_option("thumbnails", b.get_active()))
        box.pack_start(thumbnails, False, False, 0)
        note("Pictures of images, PDFs and 3D models, shared with other file managers' "
             "thumbnail cache. Videos too once a video thumbnailer (ffmpegthumbnailer) is installed.")
        heading("Preview")
        tabs = Gtk.RadioButton(label="Tabs: Preview and Properties", active=self.options["preview_layout"] == "tabs")
        single = Gtk.RadioButton(label="One panel, properties below", group=tabs,
                                 active=self.options["preview_layout"] == "single")
        tabs.connect("toggled", lambda b: b.get_active() and self._set_preview_layout("tabs"))
        single.connect("toggled", lambda b: b.get_active() and self._set_preview_layout("single"))
        box.pack_start(tabs, False, False, 0)
        note("Ctrl+I switches tabs. With nothing to preview, the properties show.")
        box.pack_start(single, False, False, 0)
        folder_thumbs = Gtk.CheckButton(label="Thumbnails of what's in folders",
                                        active=self.options["folder_thumbnails"])
        folder_thumbs.connect("toggled", lambda b: (self._set_option("folder_thumbnails", b.get_active()),
                                                    self.refresh_preview()))
        box.pack_start(folder_thumbs, False, False, 0)
        note(f"A folder's preview shows its first {GRID_LIMIT} images, videos, PDFs… "
             "Click one to open the folder with it selected.")
        heading("Window")
        dialog = Gtk.RadioButton(label="Quick dialog", active=self.options["window_mode"] == "dialog")
        normal = Gtk.RadioButton(label="Normal window", group=dialog, active=self.options["window_mode"] == "normal")
        dialog.connect("toggled", lambda b: b.get_active() and self.set_window_mode("dialog"))
        normal.connect("toggled", lambda b: b.get_active() and self.set_window_mode("normal"))
        box.pack_start(dialog, False, False, 0)
        note("Floats in the middle of the screen; Esc closes it, and it closes by itself once it opens a file.")
        box.pack_start(normal, False, False, 0)
        note("A regular window your window manager places; it stays open until you close it (Ctrl+Q).")
        heading("Default file manager")
        self.default_fm_check = Gtk.CheckButton(label="Open folders with qfm", active=self.options["default_fm"])
        self.default_fm_check.connect("toggled", self._on_default_fm)
        box.pack_start(self.default_fm_check, False, False, 0)
        note("Folders you open from other apps, and their “Show in folder”, come to qfm. "
             "Turning it off gives folders back to the app that had them.")
        keys = Gtk.Button(label="Keyboard Shortcuts (F1)", relief=Gtk.ReliefStyle.NONE, margin_top=12)
        keys.get_child().set_xalign(0)
        keys.connect("clicked", lambda *_: self.toggle_keys())
        box.pack_start(keys, False, False, 0)

        header = Gtk.Box(spacing=6)
        title = Gtk.Label(label="Options", xalign=0)
        title.get_style_context().add_class("qfm-pane-heading")
        close = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.MENU)
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.set_tooltip_text("Close (Ctrl+, or Esc)")
        close.connect("clicked", lambda *_: self.close_options())
        header.pack_start(title, True, True, 0)
        header.pack_start(close, False, False, 0)
        scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.add(box)
        arrow_keys(box, scroller)
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                       margin_start=8, margin_end=8, margin_top=8, margin_bottom=8)
        page.pack_start(header, False, False, 0)
        page.pack_start(scroller, True, True, 0)
        self.options_first = group           # focused when the panel opens
        return page

    def _apply_window_hint(self):
        if self.options["window_mode"] == "dialog":
            self.set_type_hint(Gdk.WindowTypeHint.DIALOG)   # tiling WMs (pytwm/dwm) float dialogs
            self.set_position(Gtk.WindowPosition.CENTER)
        else:
            self.set_type_hint(Gdk.WindowTypeHint.NORMAL)
            self.set_position(Gtk.WindowPosition.NONE)

    def set_window_mode(self, mode):
        """Switch between a quick dialog and a normal window (the window is shown again, so
        the window manager places it the new way)."""
        if self.options["window_mode"] == mode:
            return
        self.options["window_mode"] = mode
        self.store.save_options()
        if self.get_mapped():
            self.hide()
            self._apply_window_hint()
            self.show()
            self.present()
        else:
            self._apply_window_hint()
        self.flash("Quick dialog: Esc closes qfm" if mode == "dialog" else "Normal window: Esc no longer closes qfm")

    def _on_default_fm(self, check):
        want = check.get_active()
        if want == self.options["default_fm"]:
            return
        try:
            (fm1.enable if want else fm1.disable)(self.options)
        except (GLib.Error, OSError) as err:
            self.flash(f"Could not change the default file manager: {getattr(err, 'message', None) or err}",
                       error=True)
            check.set_active(not want)
            return
        self.options["default_fm"] = want
        self.store.save_options()
        service = getattr(self.get_application(), "fm1", None)
        if want:
            if service:
                service.start(self.get_application().get_dbus_connection())
            self.flash("qfm now opens folders and answers “Show in folder”")
        else:
            if service:
                service.stop()
            self.flash("Folders go back to the previous app")

    def _set_preview_layout(self, layout):
        self.options["preview_layout"] = layout
        self.store.save_options()
        self.preview.layout_tabs()

    def _toggle_detail(self, key, shown):
        details = [k for k, _ in DETAIL_NAMES if (k == key and shown) or (k != key and k in self.options["details"])]
        self._set_option("details", details)

    def _set_option(self, key, value):
        self.options[key] = value
        self.store.save_options()
        self.view.refresh_all()
        self._update_status()

    # -- keys ----------------------------------------------------------------

    def _on_key(self, win, event):
        """Window-wide keys; column keys (arrows, Enter, Space) are in Column._on_key."""
        if self.prompt.handle_key(event):     # Esc, y/n, Ctrl+Enter for an open question
            return True
        key = event.keyval
        mods = event.state & Gtk.accelerator_get_default_mod_mask()
        ctrl, shift = Gdk.ModifierType.CONTROL_MASK, Gdk.ModifierType.SHIFT_MASK
        typing = isinstance(self.get_focus(), Gtk.Entry)
        if (key == Gdk.KEY_Escape and mods == 0 and typing and self.get_focus().is_ancestor(self.side)
                and self.side.get_visible_child_name() in ("options", "commands")):
            self.close_options()          # (from a box in Options or the Commands form, Esc still closes)
            return True
        if key == Gdk.KEY_F1 and mods == 0:
            self.toggle_keys()
            return True
        if key == Gdk.KEY_F6 and mods == 0:
            self.switch_pane()
            return True
        if mods == ctrl:
            lower = Gdk.keyval_to_lower(key)
            if lower == Gdk.KEY_f:
                self.focus_filter()
            elif lower == Gdk.KEY_l:
                self.show_path_entry()
            elif key == Gdk.KEY_comma:
                self.toggle_options()
            elif lower == Gdk.KEY_k:
                self.toggle_actions()
            elif lower in (Gdk.KEY_q, Gdk.KEY_w):
                self.close()
            elif typing:
                return False
            elif lower == Gdk.KEY_b:
                self.toggle_sidebar()
            elif lower == Gdk.KEY_d:
                self.toggle_bookmark()
            elif lower == Gdk.KEY_h:
                self.hidden_check.set_active(not self.hidden_check.get_active())
            elif lower == Gdk.KEY_i:
                if self.preview.tabbed:
                    self.preview.toggle_tab()
                else:
                    self.flash("The preview shows everything in one panel (Options: Preview)")
            elif lower == Gdk.KEY_o:
                self.show_menu()
            elif lower == Gdk.KEY_e:
                if self.chosen():
                    actions.reveal(self, self.chosen())
            elif lower == Gdk.KEY_r:
                self.view.reload()
            elif lower == Gdk.KEY_z:
                self.fileops.undo()
            elif lower == Gdk.KEY_s and self.view.focused_column is not None:
                self.select_matching()
            elif lower == Gdk.KEY_n and self._in_columns():
                self.fileops.new_file()
            elif lower == Gdk.KEY_p and self.preview.media_active:
                self.preview.player.toggle()
            elif lower == Gdk.KEY_m and self.preview.media_active:
                self.preview.player.toggle_mute()
            elif key in (Gdk.KEY_Left, Gdk.KEY_Right) and self.preview.media_active:
                self.preview.player.step(media.SEEK_STEP if key == Gdk.KEY_Right else -media.SEEK_STEP)
            elif lower in (Gdk.KEY_c, Gdk.KEY_x, Gdk.KEY_v) and not self._in_columns():
                return False
            elif lower == Gdk.KEY_c:
                self.fileops.copy()
            elif lower == Gdk.KEY_x:
                self.fileops.copy(cut=True)
            elif lower == Gdk.KEY_v:
                self.fileops.paste()
            elif Gdk.KEY_1 <= key <= Gdk.KEY_1 + len(COLORS) - 1:
                self.set_color(list(COLORS)[key - Gdk.KEY_1])
            elif key == Gdk.KEY_0:
                self.set_color(None)
            else:
                return False
            return True
        if mods == ctrl | shift and not typing:
            lower = Gdk.keyval_to_lower(key)
            if lower == Gdk.KEY_c and self.chosen():
                actions.copy_path(self, self.chosen())
            elif lower == Gdk.KEY_n and self._in_columns():
                self.fileops.new_folder()
            elif lower == Gdk.KEY_d and self._in_columns():
                self.fileops.duplicate()
            elif lower == Gdk.KEY_v and self._in_columns():
                self.fileops.paste_links()
            else:
                return False
            return True
        if ((key in (Gdk.KEY_Menu, Gdk.KEY_MenuKB) and mods == 0) or (key == Gdk.KEY_F10 and mods == shift)
                or key in COPILOT_KEYS):
            # the context-menu key opens qfm's menu from anywhere in the window - also from the
            # filter or path box, whose own Cut/Copy/Paste menu it would show otherwise
            if typing or not self._in_columns():
                self.focus_columns()
            self.show_menu()
            return True
        if mods == Gdk.ModifierType.MOD1_MASK and key in (Gdk.KEY_Left, Gdk.KEY_Right) and not typing:
            self.go_back(forward=key == Gdk.KEY_Right)
            return True
        if mods == Gdk.ModifierType.MOD1_MASK and Gdk.KEY_0 <= key <= Gdk.KEY_9 and not typing:
            self.open_with_number(key - Gdk.KEY_0)
            return True
        if typing:
            return False
        if key == Gdk.KEY_F5:
            self.view.reload()
        elif key == Gdk.KEY_F4 and mods == 0:
            self.open_terminal_here()
        elif key == Gdk.KEY_Escape and mods == 0:
            if self.get_focus() is not None and self.get_focus().is_ancestor(self.sidebar):
                return False                     # the sidebar's Esc goes back to the columns
            if self.side.get_visible_child_name() == "search":
                self.close_search()
            elif (self.side.get_visible() and self.side.get_visible_child_name() == "actions"
                  and self.actions_pages.get_visible_child_name() != "main"):
                self.actions_pages.set_visible_child_name("main")     # a submenu's page: back
            elif self.side.get_visible() and self.side.get_visible_child_name() in ("keys", "options", "actions",
                                                                                     "commands"):
                self.close_keys()
            elif self.progress.cancel_all():
                self.flash("Cancelling…")
            elif self.view.focused_column is not None and self.view.focused_column.filter:
                self.filter_entry.set_text("")
                self.view.set_filter("")
                self.hide_filter()
            elif self.view.focused_column is not None and len(self.view.focused_column.marked) > 1:
                column = self.view.focused_column       # several selected: keep just the one at the cursor
                column.select_names([column.selected.name])
                self.view.selected(column, column.selected)
            elif self.options["window_mode"] == "dialog":
                self.close()
            else:
                return False
        elif not self._in_columns():
            return False
        elif key == Gdk.KEY_F2 and mods == 0:
            self.fileops.rename()
        elif key == Gdk.KEY_Delete and mods == 0:
            self.fileops.trash()
        elif key == Gdk.KEY_Delete and mods == shift:
            self.fileops.delete()
        else:
            return False
        return True

    def _on_delete(self, *_):
        if self.progress.get_children() or self.fileops.running():
            self._closing = False
            self.flash("Copies are still running - wait for them, or cancel them (Esc)", error=True)
            self.present()
            return True       # closing now would cut a copy off in the middle
        self.search.cancel()
        self.preview.stop_media()
        if self._save_timer:
            GLib.source_remove(self._save_timer)
            self._save_timer = 0
        width, height = self.get_size()
        self.options.update(window_width=width, window_height=height)
        if self.sidebar_scroller.get_visible():
            self.options["sidebar_width"] = self.outer.get_position()
        if self.side.get_visible():
            self.options["preview_width"] = self.inner.get_allocated_width() - self.inner.get_position()
        self.store.save_options()
        return False


class App(Gtk.Application):
    """One qfm per user session: `qfm [path]` while it's open hands path to the open window
    (over D-Bus, via Gtk.Application's uniqueness) and exits."""

    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.window = None
        self.fm1 = fm1.Service(self.show_paths)

    def do_command_line(self, command_line):
        args = command_line.get_arguments()[1:]
        new = self.window is None
        if new:
            archive.clean_temp()      # files opened from archives more than a day ago
            style.install()
            self.window = Window(self)
            self.window.start()
            if self.window.options["default_fm"]:
                self.fm1.start(self.get_dbus_connection())
        if args:
            self.window.go_to(args[0])
        elif new:
            self.window.go_to(self.window.options["last"] or HOME)   # where you left off
        self._raise()
        return 0

    def show_paths(self, method, paths):
        """Another app asked (FileManager1) to show folders or items."""
        if self.window is None:
            return False
        self.window.go_to(paths[0])      # a folder opens; an item is selected in its folder
        self._raise()
        return False

    def _raise(self):
        # X11 focus-stealing prevention ignores present() without a timestamp
        # when the request comes from another process: use the X server's time
        try:
            gi.require_version("GdkX11", "3.0")
            from gi.repository import GdkX11
            gdk_window = self.window.get_window()
            if isinstance(gdk_window, GdkX11.X11Window):
                self.window.present_with_time(GdkX11.x11_get_server_time(gdk_window))
                return
        except (ValueError, ImportError):
            pass
        self.window.present()


def run(path=None):
    GLib.set_prgname("qfm")
    GLib.set_application_name("qfm")
    Gdk.set_program_class("qfm")   # WM_CLASS "qfm", "qfm" (for WM rules), not "Main.py"
    return App().run([sys.argv[0]] + ([path] if path else []))
