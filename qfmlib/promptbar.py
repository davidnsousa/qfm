"""The prompt bar: every question qfm asks (a name, a confirmation, a folder, an app) appears
as a bar above the status bar instead of a dialog window. Enter answers, Esc cancels; one
question at a time - asking another replaces it. The callbacks get the answer and return an
error message to show in the bar (it stays open), or None (it closes)."""
import os

from gi.repository import Gdk, Gio, GLib, Gtk, Pango

from .columns import HOME, short_path


def complete_folder(text):
    """Shell-style Tab completion of a folder path: (completed text, the folder names that
    matched). Keeps a leading ~ as it was typed."""
    parent, stem = os.path.split(os.path.expanduser(text))
    try:
        names = sorted(d.name for d in os.scandir(parent or os.sep)
                       if d.is_dir() and d.name.startswith(stem)
                       and (stem.startswith(".") or not d.name.startswith(".")))
    except OSError:
        names = []
    if not names:
        return text, []
    done = os.path.join(parent, os.path.commonprefix(names)) + ("/" if len(names) == 1 else "")
    return (short_path(done) if text.startswith("~") else done), names


def matches_hint(names):
    """The folder names Tab could complete to, for showing when there are several."""
    if not names:
        return "no folder matches"
    return "" if len(names) == 1 else "   ".join(names[:8]) + ("   …" if len(names) > 8 else "")


class PromptBar(Gtk.Revealer):
    def __init__(self, window):
        # no slide-in: keys typed right after the question appears (F2 then Enter) must reach it
        super().__init__(transition_type=Gtk.RevealerTransitionType.NONE, transition_duration=0)
        self.window = window
        self.kind = None          # None (closed), "text", "confirm", "folder" or "app"
        self._on_ok = None
        self._value = lambda: ()
        self.box = Gtk.Box(spacing=8)
        self.box.get_style_context().add_class("qfm-prompt")
        self.add(self.box)

    @property
    def active(self):
        return self.kind is not None

    # -- the kinds of question -------------------------------------------------

    def ask_text(self, label, initial, on_ok, ok_label="OK", select=None, extra=None):
        """A line of text (a name). select: (start, end) of initial to preselect, else all of it."""
        middle = self._start("text", label, ok_label, on_ok)
        entry = Gtk.Entry(text=initial, hexpand=True)
        entry.connect("activate", lambda *_: self.accept())
        middle.pack_start(entry, True, True, 0)
        if extra is not None:
            middle.pack_start(extra, False, False, 0)
        self._value = lambda: (entry.get_text().strip(),)
        self._show(entry)
        entry.select_region(*(select or (0, -1)))

    def choose(self, text, choices):
        """One of several answers: choices = [(key letter, button label, callback)], the first
        is what Enter picks. The letter keys pick too."""
        middle = self._start("choice", text, choices[0][1], lambda: choices[0][2]())
        self._choices = {key: callback for key, _, callback in choices}
        hint = Gtk.Label(label="   ".join(f"{key}: {label.lower()}" for key, label, _ in choices) + "   Esc: cancel",
                         xalign=0)
        hint.get_style_context().add_class("qfm-preview-key")
        middle.pack_start(hint, True, True, 0)
        for key, label, callback in reversed(choices[1:]):      # (the first is the OK button)
            button = Gtk.Button(label=label)
            button.connect("clicked", lambda _, c=callback: self._pick(c))
            middle.pack_end(button, False, False, 0)
        self._value = lambda: ()
        self._show(self.ok_button)

    def _pick(self, callback):
        self.close()
        callback()

    def confirm(self, text, on_ok, ok_label):
        """Yes or no to something that can't be undone: y (or the button) confirms; n, Esc and
        Enter (the focus starts on Cancel) don't."""
        middle = self._start("confirm", text, f"{ok_label} (y)", on_ok, destructive=True)
        hint = Gtk.Label(label="y: yes   n / Esc: no", xalign=0)
        hint.get_style_context().add_class("qfm-preview-key")
        middle.pack_start(hint, True, True, 0)
        self._value = lambda: ()
        self._show(self.cancel_button)

    def ask_folder(self, label, initial, on_ok, ok_label="OK"):
        """A folder: type a path (Tab completes it), or move to one in the columns and take it
        with Ctrl+Enter / the Use button."""
        middle = self._start("folder", label, ok_label, on_ok)
        self.folder_entry = entry = Gtk.Entry(text=initial, hexpand=True)
        entry.connect("activate", lambda *_: self.accept())
        entry.connect("key-press-event", self._on_folder_key)
        self.use_button = Gtk.Button()
        self.use_button.connect("clicked", lambda *_: self.use_current_folder())
        middle.pack_start(entry, True, True, 0)
        middle.pack_start(self.use_button, False, False, 0)
        self._value = lambda: (os.path.abspath(os.path.expanduser(entry.get_text().strip() or "~")),)
        column = self.window.view.focused_column
        self.location_changed(column.path if column else HOME)
        self._show(entry)
        entry.set_position(-1)

    def ask_app(self, label, on_ok, default_for=None, make_default=False):
        """An installed app, by name (completes as you type). default_for: a file type
        description to offer "Always use for …" for. on_ok(app or None, always)."""
        middle = self._start("app", label, "Open", on_ok)
        apps = sorted((a for a in Gio.AppInfo.get_all() if a.should_show()),
                      key=lambda a: a.get_display_name().casefold())
        store = Gtk.ListStore(str, str, Gio.Icon)
        for app in apps:
            store.append((app.get_display_name(), app.get_id(), app.get_icon()))
        by_id = {a.get_id(): a for a in apps}
        entry = Gtk.Entry(hexpand=True, placeholder_text="type an app name")
        completion = Gtk.EntryCompletion(model=store, minimum_key_length=1)
        icon = Gtk.CellRendererPixbuf()
        completion.pack_start(icon, False)
        completion.add_attribute(icon, "gicon", 2)
        completion.set_text_column(0)
        completion.set_match_func(lambda c, key, it: key.casefold() in store[it][0].casefold())
        completion.connect("match-selected", self._on_app_picked, entry)
        entry.set_completion(completion)
        entry.connect("activate", lambda *_: self.accept())
        middle.pack_start(entry, True, True, 0)
        always = None
        if default_for:
            always = Gtk.CheckButton(label=f"Always use it for {default_for}", active=make_default)
            middle.pack_start(always, False, False, 0)

        def value():
            text = entry.get_text().strip().casefold()
            matches = [a for a in apps if text and text in a.get_display_name().casefold()]
            exact = [a for a in matches if a.get_display_name().casefold() == text]
            app = (exact or matches or [None])[0]
            return by_id.get(app.get_id()) if app else None, bool(always and always.get_active())

        self._value = value
        self._show(entry)

    # -- answering -------------------------------------------------------------

    def accept(self):
        if self.kind is None:
            return
        on_ok, value = self._on_ok, self._value()
        error = on_ok(*value)
        if error:
            self._say(error)
        elif self._on_ok is on_ok:      # (on_ok may have asked something else)
            self.close()

    def cancel(self):
        self.close()

    def close(self, refocus=True):
        was_open = self.kind is not None
        self.kind, self._on_ok = None, None
        self.set_reveal_child(False)
        if refocus and was_open:
            self.window.focus_columns()

    def handle_key(self, event):
        """Keys for the open prompt, wherever the focus is (called first by the window)."""
        if self.kind is None:
            return False
        key = event.keyval
        mods = event.state & Gtk.accelerator_get_default_mod_mask()
        if key == Gdk.KEY_Escape and mods == 0:
            self.cancel()
            return True
        if self.kind == "choice" and mods == 0 and Gdk.keyval_to_unicode(key):
            callback = self._choices.get(chr(Gdk.keyval_to_unicode(key)).lower())
            if callback:
                self._pick(callback)
                return True
        if self.kind == "confirm" and mods == 0 and key in (Gdk.KEY_y, Gdk.KEY_Y, Gdk.KEY_n, Gdk.KEY_N):
            self.accept() if key in (Gdk.KEY_y, Gdk.KEY_Y) else self.cancel()
            return True
        if (self.kind == "folder" and mods == Gdk.ModifierType.CONTROL_MASK
                and key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter)):
            self.use_current_folder()
            return True
        return False

    def location_changed(self, path):
        """The focused column moved: that's the folder Ctrl+Enter / the Use button takes."""
        if self.kind == "folder":
            column = self.window.view.focused_column
            usable = column is not None and column.archive is None
            self.use_button.set_label(f"Use {short_path(path)} (Ctrl+Enter)" if usable else "Use the current folder")
            self.use_button.set_sensitive(usable)
            self.use_button.get_child().set_ellipsize(Pango.EllipsizeMode.START)
            self.use_button.get_child().set_max_width_chars(36)

    def use_current_folder(self):
        column = self.window.view.focused_column
        if self.kind == "folder" and column is not None and column.archive is None:
            self.folder_entry.set_text(short_path(column.path))
            self.accept()

    # -- internals -------------------------------------------------------------

    def _start(self, kind, label, ok_label, on_ok, destructive=False):
        """Replace whatever was asked with a new question; returns the box for its widgets."""
        for child in self.box.get_children():
            child.destroy()
        self.kind, self._on_ok = kind, on_ok
        title = Gtk.Label(label=label, xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE, max_width_chars=60)
        title.get_style_context().add_class("qfm-prompt-label")
        middle = Gtk.Box(spacing=6)
        self.error = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=48)
        self.cancel_button = Gtk.Button(label="Cancel")
        self.cancel_button.connect("clicked", lambda *_: self.cancel())
        ok = self.ok_button = Gtk.Button(label=ok_label)
        ok.get_style_context().add_class("destructive-action" if destructive else "suggested-action")
        ok.connect("clicked", lambda *_: self.accept())
        self.box.pack_start(title, False, False, 0)
        self.box.pack_start(middle, True, True, 0)
        self.box.pack_start(self.error, False, False, 0)
        self.box.pack_end(ok, False, False, 0)
        self.box.pack_end(self.cancel_button, False, False, 0)
        return middle

    def _show(self, focus):
        self.box.show_all()
        self.set_reveal_child(True)
        focus.grab_focus()

    def _on_app_picked(self, completion, model, it, entry):
        entry.set_text(model[it][0])
        self.accept()
        return True

    def _on_folder_key(self, entry, event):
        if event.keyval in (Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab) and not event.state & Gdk.ModifierType.SHIFT_MASK:
            self._complete(entry)
            return True
        return False

    def _complete(self, entry):
        """Shell-style Tab completion of a folder path."""
        text, names = complete_folder(entry.get_text())
        entry.set_text(text)
        entry.set_position(-1)
        self._say(matches_hint(names), error=not names)

    def _say(self, text, error=True):
        """A note in the bar: an error (red) or a hint (the folder names Tab could complete)."""
        context = self.error.get_style_context()
        (context.add_class if error else context.remove_class)("qfm-status-error")
        self.error.set_text(text)


class ProgressArea(Gtk.Box):
    """Running copies and moves, one row each (above the prompt bar): what, how far, Cancel.
    A row appears only if the job takes more than a moment."""

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)

    def add(self, job, label):
        GLib.timeout_add(400, self._show_row, job, label)

    def _show_row(self, job, label):
        if getattr(job, "ended", False):
            return False
        row = Gtk.Box(spacing=8)
        row.get_style_context().add_class("qfm-prompt")
        title = Gtk.Label(label=label, xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE, max_width_chars=50)
        title.get_style_context().add_class("qfm-prompt-label")
        bar = Gtk.ProgressBar(show_text=True, hexpand=True, valign=Gtk.Align.CENTER)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: job.cancel.set())
        row.pack_start(title, False, False, 0)
        row.pack_start(bar, True, True, 0)
        row.pack_start(cancel, False, False, 0)
        self.pack_start(row, False, False, 0)
        row.show_all()
        job.row = row

        def tick():
            if getattr(job, "ended", False):
                row.destroy()
                return False
            fraction = job.copied / job.total if job.total else 0
            bar.set_fraction(min(fraction, 1))
            speed = job.speed()
            bar.set_text(f"{human(job.copied)} of {human(job.total)}"
                         + (f"  ·  {human(speed)}/s" if speed else "") + f"  ·  {job.current}")
            return True

        tick()
        GLib.timeout_add(150, tick)
        return False

    def cancel_all(self):
        """Esc while copies run: cancel them. True if there was one."""
        rows = [c for c in self.get_children()]
        for row in rows:
            for widget in row.get_children():
                if isinstance(widget, Gtk.Button):
                    widget.clicked()
        return bool(rows)


def human(size):
    from .columns import human_size
    return human_size(int(size))
