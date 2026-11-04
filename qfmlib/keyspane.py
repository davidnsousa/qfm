"""The F1 panel: every shortcut (from keys.SHORTCUTS), grouped, with a filter box."""
from gi.repository import Gdk, Gtk, Pango

from .keys import SHORTCUTS


class KeysPane(Gtk.Box):
    def __init__(self, window):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                         margin_start=8, margin_end=8, margin_top=8, margin_bottom=8)
        self.window = window
        header = Gtk.Box(spacing=6)
        heading = Gtk.Label(label="Keyboard shortcuts", xalign=0)
        heading.get_style_context().add_class("qfm-pane-heading")
        close = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.MENU)
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.set_tooltip_text("Close (F1 or Esc)")
        close.connect("clicked", lambda *_: window.close_keys())
        header.pack_start(heading, True, True, 0)
        header.pack_start(close, False, False, 0)
        self.filter = Gtk.SearchEntry(placeholder_text="Filter: a key or what it does")
        self.filter.connect("search-changed", lambda *_: self._apply_filter())
        self.filter.connect("stop-search", lambda *_: window.close_keys())

        # each shortcut: the keys on one line, what they do below (the pane is narrow)
        rows_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.groups = []      # (heading label, [(keys label, what label, text)])
        for group, shortcuts in SHORTCUTS:
            title = Gtk.Label(label=group, xalign=0, margin_top=10 if self.groups else 0)
            title.get_style_context().add_class("qfm-pane-heading")
            rows_box.pack_start(title, False, False, 0)
            rows = []
            for keys, what in shortcuts:
                k = Gtk.Label(label=keys, xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, margin_top=3)
                k.get_style_context().add_class("qfm-key")
                w = Gtk.Label(label=what, xalign=0, wrap=True, margin_start=14)
                w.get_style_context().add_class("qfm-preview-key")
                rows_box.pack_start(k, False, False, 0)
                rows_box.pack_start(w, False, False, 0)
                rows.append((k, w, f"{group} {keys} {what}".casefold()))
            self.groups.append((title, rows))
        self.none = Gtk.Label(label="No shortcut matches", xalign=0)
        self.none.get_style_context().add_class("qfm-preview-key")
        self.none.set_no_show_all(True)
        rows_box.pack_start(self.none, False, False, 0)
        scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.add(rows_box)
        self.scroller = scroller
        self.filter.connect("key-press-event", self._on_filter_key)   # ↑ ↓ PgUp PgDn scroll the list
        self.pack_start(header, False, False, 0)
        self.pack_start(self.filter, False, False, 0)
        self.pack_start(scroller, True, True, 0)

    def _apply_filter(self):
        words = self.filter.get_text().casefold().split()
        shown_any = False
        for title, rows in self.groups:
            group_shown = False
            for k, w, text in rows:
                shown = all(word in text for word in words)
                k.set_visible(shown)
                w.set_visible(shown)
                group_shown |= shown
            title.set_visible(group_shown)
            shown_any |= group_shown
        self.none.set_visible(not shown_any)

    def _on_filter_key(self, entry, event):
        adj = self.scroller.get_vadjustment()
        step = {Gdk.KEY_Down: adj.get_step_increment() * 2, Gdk.KEY_Up: -adj.get_step_increment() * 2,
                Gdk.KEY_Page_Down: adj.get_page_increment(), Gdk.KEY_Page_Up: -adj.get_page_increment()}
        if event.keyval not in step or event.state & Gtk.accelerator_get_default_mod_mask():
            return False
        adj.set_value(min(max(adj.get_value() + step[event.keyval], adj.get_lower()),
                          adj.get_upper() - adj.get_page_size()))
        return True

    def focus(self):
        self.filter.set_text("")
        self._apply_filter()
        self.filter.grab_focus()
