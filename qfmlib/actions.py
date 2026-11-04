"""Open, open with, default apps, show in file manager, copy path - and the actions menu.
Every action takes a list of entries (one or several selected items)."""
import os
import shutil

from gi.repository import Gdk, Gio, GLib, Gtk

from . import archive
from .columns import describe
from . import templates
from .store import COLORS


def content_type(entry):
    """The sniffed content type (the column lists use the faster name-based guess)."""
    if entry.is_dir:
        return "inode/directory"
    try:
        info = Gio.File.new_for_path(entry.path).query_info(
            "standard::content-type", Gio.FileQueryInfoFlags.NONE, None)
        return info.get_content_type() or entry.content_type
    except GLib.Error:
        return entry.content_type


def types_of(entries):
    """Content types of entries: sniffed for one item, name-based for several (fast)."""
    if len(entries) == 1:
        return [content_type(entries[0])]
    return sorted({"inode/directory" if e.is_dir else e.content_type for e in entries})


def apps_for(types):
    """(the default app if every type shares it, apps that handle every type - default first)."""
    defaults = {ct: Gio.AppInfo.get_default_for_type(ct, False) for ct in types}
    ids = {app.get_id() if app else None for app in defaults.values()}
    default = defaults[types[0]] if len(ids) == 1 and None not in ids else None
    apps, common = [], None
    for ct in types:
        found = {app.get_id(): app for app in Gio.AppInfo.get_all_for_type(ct) if app.should_show()}
        if defaults[ct] is not None and defaults[ct].should_show():
            found[defaults[ct].get_id()] = defaults[ct]
        common = found if common is None else {i: a for i, a in common.items() if i in found}
    apps = sorted((common or {}).values(), key=lambda a: a.get_display_name().casefold())
    if default is not None:
        apps = [default] + [a for a in apps if a.get_id() != default.get_id()]
    return default, apps


def what(entries):
    return entries[0].display if len(entries) == 1 else f"{len(entries)} items"


def real_paths(entries):
    """Paths apps can open: things inside archives are extracted to a private temp folder first."""
    paths, inside = [], {}
    for e in entries:
        if e.archive is None:
            paths.append(e.path)
        else:
            inside.setdefault(e.archive, []).append(e.inner)
    for path, inners in inside.items():
        paths += archive.to_temp(path, inners)
    return paths


def launch(win, app, entries):
    try:
        files = [Gio.File.new_for_path(p) for p in real_paths(entries)]
    except (archive.Error, OSError) as err:
        win.flash(f"Could not extract {what(entries)}: {str(err).split(' (errno=')[0]}", error=True)
        return
    try:
        try:
            app.launch(files, win.get_display().get_app_launch_context())
        except GLib.Error:
            # GLib only knows a few terminals (not Alacritty, kitty, foot…): run it in ours
            if not (isinstance(app, Gio.DesktopAppInfo) and app.get_boolean("Terminal")):
                raise
            run_in_terminal(app, [f.get_path() for f in files])
        win.flash(f"Opened {what(entries)} with {app.get_display_name()}")
        win.close_after_open()
    except GLib.Error as err:
        win.flash(f"Could not open {what(entries)}: {err.message}", error=True)
    except OSError as err:
        win.flash(f"Could not open {what(entries)}: {err}", error=True)


# terminals and how each is told the command to run after them, in the order tried
# (after $TERMINAL): xdg-terminal-exec runs the user's chosen one
TERMINALS = (("xdg-terminal-exec", []), ("alacritty", ["-e"]), ("kitty", []), ("foot", []),
             ("wezterm", ["start", "--"]), ("ghostty", ["-e"]), ("konsole", ["-e"]),
             ("gnome-terminal", ["--"]), ("kgx", ["--"]), ("xfce4-terminal", ["-x"]),
             ("mate-terminal", ["-x"]), ("tilix", ["-e"]), ("terminator", ["-x"]), ("lxterminal", ["-e"]),
             ("qterminal", ["-e"]), ("sakura", ["-x"]), ("st", ["-e"]), ("urxvt", ["-e"]), ("xterm", ["-e"]))


def find_terminal():
    """([the terminal and its options], [what goes before a command to run in it]) or None."""
    named = os.environ.get("TERMINAL", "").split()
    if named and shutil.which(named[0]):
        return named, dict(TERMINALS).get(os.path.basename(named[0]), ["-e"])
    for name, flags in TERMINALS:
        if shutil.which(name):
            return [name], flags
    return None


def open_terminal(win, folder):
    """A terminal (no command: its shell) in folder."""
    found = find_terminal()
    if found is None:
        return win.flash("No terminal found - install one, or set $TERMINAL", error=True)
    launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.NONE)
    launcher.set_cwd(folder)
    try:
        launcher.spawnv(found[0])
    except GLib.Error as err:
        return win.flash(f"Could not open a terminal: {err.message}", error=True)
    win.flash(f"Opened a terminal in {folder}")
    win.close_after_open()


def run_in_terminal(app, paths):
    """Start a Terminal=true app (its desktop file's Exec, with paths) in a terminal, in the
    first path's folder; once per path if the Exec takes one file at a time (%f, %u)."""
    found = find_terminal()
    if found is None:
        raise OSError("no terminal found - install one, or set $TERMINAL")
    terminal = found[0] + found[1]
    ok, argv = GLib.shell_parse_argv(app.get_string("Exec") or "")
    single = any(code in arg for arg in argv for code in ("%f", "%u"))
    for group in ([[p] for p in paths] or [[]]) if single else [paths]:
        command = []
        for arg in argv:
            if arg in ("%F", "%U"):
                command += group
            elif arg in ("%i", "%c", "%k"):
                continue
            else:
                first = group[0] if group else ""
                arg = arg.replace("%f", first).replace("%u", first).replace("%%", "%")
                if arg:
                    command.append(arg)
        launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.NONE)
        launcher.set_cwd(os.path.dirname(paths[0]) if paths else os.path.expanduser("~"))
        launcher.spawnv(terminal + command)


def program_kind(entry):
    """"script" (#!…) or "binary" (a Linux program, an AppImage) for a file you may run; else None.
    The file's start decides, not just its run bit (USB sticks often set it on everything)."""
    if entry.is_dir or entry.archive is not None or not os.access(entry.path, os.X_OK):
        return None
    try:
        with open(entry.path, "rb") as f:
            start = f.read(4)
    except OSError:
        return None
    return "binary" if start == b"\x7fELF" else "script" if start[:2] == b"#!" else None


def run_program(win, entry, terminal=False):
    """Run entry in its folder: by itself, or in a terminal that stays open when it ends."""
    folder = os.path.dirname(entry.path)
    if terminal:
        found = find_terminal()
        if found is None:
            return win.flash("No terminal found - install one, or set $TERMINAL", error=True)
        wait = '"$0"; printf "\\n[finished: %s] Press Enter to close" "$?"; read -r _'
        argv = found[0] + found[1] + ["sh", "-c", wait, entry.path]
    else:
        argv = [entry.path]
    launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.NONE)
    launcher.set_cwd(folder)
    try:
        launcher.spawnv(argv)
    except GLib.Error as err:
        return win.flash(f"Could not run {entry.display}: {err.message}", error=True)
    win.flash(f"Running {entry.display}" + (" in a terminal" if terminal else ""))
    win.close_after_open()


def ask_run(win, entry, kind):
    """Enter on a program or script: run it (by itself or in a terminal), or open it (a script, in its editor)."""
    app = Gio.AppInfo.get_default_for_type(content_type(entry), False)
    run = ("r", "Run", lambda: run_program(win, entry))
    in_terminal = ("t", "Run in Terminal", lambda: run_program(win, entry, terminal=True))
    choices = [in_terminal, run] if kind == "script" else [run, in_terminal]
    if app is not None:
        choices.append(("o", f"Open with {app.get_display_name()}", lambda: launch(win, app, [entry])))
    win.prompt.choose(f"{entry.display} is a {'script' if kind == 'script' else 'program'}:", choices)


def open_default(win, entries):
    """Open each file with its default app (one launch per app). Folders are skipped when
    several items are selected; one folder is entered instead (see MillerView.activate).
    One program or script: asks whether to run it (ask_run)."""
    if len(entries) == 1 and program_kind(entries[0]):
        return ask_run(win, entries[0], program_kind(entries[0]))
    files = [e for e in entries if not e.is_dir] if len(entries) > 1 else entries
    groups, missing = {}, []
    for e in files:
        ct = content_type(e) if len(files) == 1 else e.content_type
        app = Gio.AppInfo.get_default_for_type(ct, False)
        if app is None:
            missing.append(describe(ct))
        else:
            groups.setdefault(app.get_id(), (app, []))[1].append(e)
    for app, group in groups.values():
        launch(win, app, group)
    if missing:
        win.flash(f"No default app for {', '.join(sorted(set(missing)))} - choose one with Ctrl+O", error=True)
    elif not files:
        win.flash("Only folders are selected - nothing to open", error=True)


def set_default(win, app, ct):
    try:
        app.set_as_default_for_type(ct)
        win.flash(f"{app.get_display_name()} now opens {describe(ct)} files")
    except GLib.Error as err:
        win.flash(f"Could not set the default app: {err.message}", error=True)


def choose_app(win, entries, make_default=False):
    """Pick any installed app (by name, in the prompt bar) to open entries with, optionally
    making it the default for their type."""
    types = types_of(entries)
    ct = types[0]
    default_for = describe(ct) if len(types) == 1 else None

    def picked(app, always):
        if app is None:
            return "No app matches that name"
        if always and default_for:
            set_default(win, app, ct)
        launch(win, app, entries)

    win.prompt.ask_app(f"Open {what(entries)} with:", picked, default_for, make_default)


def reveal(win, entries):
    """Show entries in the system file manager (freedesktop FileManager1.ShowItems over D-Bus);
    without one, open their folder with the default app for folders."""
    from . import fm1
    paths = list(dict.fromkeys(e.archive or e.path for e in entries))   # inside an archive: the archive
    uris = [Gio.File.new_for_path(p).get_uri() for p in paths]

    def fallback():
        parent = Gio.File.new_for_path(paths[0]).get_parent() or Gio.File.new_for_path("/")
        app = fm1.other_folder_app()       # never qfm itself
        if app is None:
            win.flash("No other file manager is installed", error=True)
            return
        try:
            app.launch([parent], win.get_display().get_app_launch_context())
            win.flash(f"Opened the folder in {app.get_display_name()}")
            win.close_after_open()
        except GLib.Error as err:
            win.flash(f"Could not show it in a file manager: {err.message}", error=True)

    service = getattr(win.get_application(), "fm1", None)
    if service is not None and service.owned:   # qfm answers ShowItems itself: go to another file manager
        return fallback()

    def on_reply(bus, result):
        try:
            bus.call_finish(result)
            win.flash(f"Shown {what(entries)} in the file manager")
            win.close_after_open()
        except GLib.Error:
            fallback()

    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except GLib.Error:
        return fallback()
    bus.call("org.freedesktop.FileManager1", "/org/freedesktop/FileManager1",
             "org.freedesktop.FileManager1", "ShowItems", GLib.Variant("(ass)", (uris, "")),
             None, Gio.DBusCallFlags.NONE, 5000, None, on_reply)


def copy_path(win, entries):
    clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
    clipboard.set_text("\n".join(e.path for e in entries), -1)
    clipboard.store()
    win.flash(f"Copied {'the path of ' if len(entries) == 1 else ''}{what(entries)}"
              f"{'' if len(entries) == 1 else ' paths'}")


# -- the menu (right-click, Menu key, Shift+F10, Ctrl+O) ------------------------

def _image(icon_name=None, gicon=None, surface=None):
    if icon_name:
        image = Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.MENU)
    elif gicon:
        image = Gtk.Image.new_from_gicon(gicon, Gtk.IconSize.MENU)
    elif surface:
        image = Gtk.Image.new_from_surface(surface)
    else:
        image = Gtk.Image()
    image.set_pixel_size(16)
    image.set_size_request(16, 16)   # keeps labels aligned for items without an icon
    return image


def _item(menu, text, callback=None, icon=None, key=None, mods=0, gicon=None, surface=None):
    """A menu item with an icon, a label and (optionally) its shortcut shown on the right."""
    item = Gtk.MenuItem()
    box = Gtk.Box(spacing=10)
    label = Gtk.AccelLabel(label=text, xalign=0)
    label.set_accel_widget(item)
    if key:
        label.set_accel(key, mods)
    box.pack_start(_image(icon, gicon, surface), False, False, 0)
    box.pack_start(label, True, True, 0)
    item.add(box)
    if callback:
        item.connect("activate", lambda *_: callback())
    menu.append(item)
    return item


def _submenu(menu, text, icon):
    sub = Gtk.Menu()
    _item(menu, text, icon=icon).set_submenu(sub)
    return sub


def build_menu(win, entry, entries):
    ctrl, shift = Gdk.ModifierType.CONTROL_MASK, Gdk.ModifierType.SHIFT_MASK
    menu = Gtk.Menu()
    menu.attach_to_widget(win, None)
    types = types_of(entries)
    default, apps = apps_for(types)
    several = len(entries) > 1

    if not several and entry.is_dir:
        _item(menu, "Open", win.go_into_selection, "folder-open-symbolic", Gdk.KEY_Return)
    elif default:
        _item(menu, f"Open with {default.get_display_name()}", lambda: launch(win, default, entries),
              "document-open-symbolic", Gdk.KEY_Return)
    if not several and program_kind(entry):
        _item(menu, "Run", lambda: run_program(win, entry), "system-run-symbolic")
        _item(menu, "Run in Terminal", lambda: run_program(win, entry, terminal=True), "utilities-terminal-symbolic")
    if not several and entry.container:
        _item(menu, "Look Inside", win.go_into_selection, "package-x-generic-symbolic", Gdk.KEY_Right)
    open_with = _submenu(menu, "Open With", "system-run-symbolic")
    alt = Gdk.ModifierType.MOD1_MASK
    for i, app in enumerate(apps):
        suffix = " (default)" if default and app.get_id() == default.get_id() else ""
        _item(open_with, app.get_display_name() + suffix, lambda a=app: launch(win, a, entries), gicon=app.get_icon(),
              key=Gdk.KEY_1 + i if i < 9 else None, mods=alt)
    if apps:
        open_with.append(Gtk.SeparatorMenuItem())
    _item(open_with, "Other Application…", lambda: choose_app(win, entries), "view-app-grid-symbolic",
          Gdk.KEY_0, alt)
    if len(types) == 1:
        ct = types[0]
        default_menu = _submenu(menu, f"Default App for {describe(ct)}", "emblem-default-symbolic")
        for app in apps:
            is_default = default is not None and app.get_id() == default.get_id()
            _item(default_menu, app.get_display_name() + ("  ✓" if is_default else ""),
                  lambda a=app: set_default(win, a, ct), gicon=app.get_icon())
        if apps:
            default_menu.append(Gtk.SeparatorMenuItem())
        _item(default_menu, "Other Application…", lambda: choose_app(win, entries, make_default=True),
              "view-app-grid-symbolic")

    menu.append(Gtk.SeparatorMenuItem())
    ops, targets = win.fileops, win.targets()
    read_only = ops.in_archive() or ops.in_trash()     # inside an archive, or in the trash
    if ops.in_trash():
        from . import trash
        if targets and all(trash.is_top(e.path) for e in targets):
            _item(menu, "Restore", ops.restore, "edit-undo-symbolic")
    if targets and not read_only:
        _item(menu, "Rename…" if len(targets) == 1 else f"Rename {len(targets)} Items…", ops.rename,
              "document-edit-symbolic", Gdk.KEY_F2)
    if targets:
        _item(menu, f"Copy{' ' + what(targets) if len(targets) > 1 else ''}", ops.copy,
              "edit-copy-symbolic", Gdk.KEY_c, ctrl)
        if not read_only:
            _item(menu, f"Cut{' ' + what(targets) if len(targets) > 1 else ''}", lambda: ops.copy(cut=True),
                  "edit-cut-symbolic", Gdk.KEY_x, ctrl)
    if targets and not read_only:
        _item(menu, f"Duplicate{' ' + what(targets) if len(targets) > 1 else ''}", ops.duplicate,
              "edit-copy-symbolic", Gdk.KEY_D, ctrl | shift)
        _item(menu, "Make Link" + ("s" if len(targets) > 1 else ""), ops.make_links, "insert-link-symbolic")
    if not read_only:
        _item(menu, "Paste Here", ops.paste, "edit-paste-symbolic", Gdk.KEY_v, ctrl)
        _item(menu, "Paste as Link", ops.paste_links, "insert-link-symbolic", Gdk.KEY_V, ctrl | shift)
        _item(menu, "New Folder…", ops.new_folder, "folder-new-symbolic", Gdk.KEY_N, ctrl | shift)
        _item(menu, "New File…", ops.new_file, "document-new-symbolic", Gdk.KEY_n, ctrl)
        new_from = _submenu(menu, "New From Template", "document-new-symbolic")
        for name, source in templates.available():
            is_dir = isinstance(source, str) and os.path.isdir(source)
            _item(new_from, name, lambda t=(name, source): ops.new_file(t),
                  "folder-symbolic" if is_dir else "text-x-generic-symbolic")
        new_from.append(Gtk.SeparatorMenuItem())
        if os.path.isdir(templates.folder()):
            _item(new_from, "Open Templates Folder", lambda: win.go_to(templates.folder()), "folder-open-symbolic")
        else:
            _item(new_from, "Customize Templates…", ops.customize_templates, "document-edit-symbolic")
    if targets and not read_only:
        _item(menu, "Move to Trash", ops.trash, "user-trash-symbolic", Gdk.KEY_Delete)
    if targets and not ops.in_archive():
        _item(menu, "Delete Permanently…", ops.delete, "edit-delete-symbolic", Gdk.KEY_Delete, shift)
    if ops.in_trash():
        _item(menu, "Empty Trash…", ops.empty_trash, "user-trash-full-symbolic")

    # archives: compress the selection, extract archives (or things inside one)
    archives = [e for e in targets if e.archive is None and not e.is_dir and archive.is_archive(e.name)]
    if targets or archives:
        menu.append(Gtk.SeparatorMenuItem())
    if targets and not read_only:
        _item(menu, "Compress…", ops.compress, "package-x-generic-symbolic")
    if archives and len(archives) == len(targets):
        _item(menu, "Extract Here", ops.extract_here, "archive-extract-symbolic")
    if (archives and len(archives) == len(targets)) or (ops.in_archive() and targets):
        _item(menu, "Extract To…", ops.extract_to, "folder-download-symbolic")

    menu.append(Gtk.SeparatorMenuItem())
    _item(menu, "Show in File Manager", lambda: reveal(win, entries), "system-file-manager-symbolic",
          Gdk.KEY_e, ctrl)
    _item(menu, "Open Terminal Here", win.open_terminal_here, "utilities-terminal-symbolic", Gdk.KEY_F4)
    _item(menu, "Copy Path" + ("s" if several else ""), lambda: copy_path(win, entries), "insert-link-symbolic",
          Gdk.KEY_C, ctrl | shift)
    if all(e.archive is None for e in entries) and not ops.in_trash():
        menu.append(Gtk.SeparatorMenuItem())
        bookmarked = all(e.path in win.store.bookmarks for e in entries)
        _item(menu, "Remove Bookmark" if bookmarked else "Add Bookmark", win.toggle_bookmark,
              "non-starred-symbolic" if bookmarked else "starred-symbolic", Gdk.KEY_d, ctrl)
    folders = [e for e in entries if e.is_dir and e.archive is None]
    if folders:
        colors = _submenu(menu, "Folder Color", "preferences-color-symbolic")
        current = {win.store.colors.get(e.path) for e in folders}
        for i, name in enumerate(COLORS):
            mark = "  ✓" if current == {name} else ""
            _item(colors, name.capitalize() + mark, lambda n=name: win.set_color(n), key=Gdk.KEY_1 + i, mods=ctrl,
                  surface=win.icons.surface(win.icons.get("inode/directory", 16, name)))
        _item(colors, "None", lambda: win.set_color(None), key=Gdk.KEY_0, mods=ctrl,
              surface=win.icons.surface(win.icons.get("inode/directory", 16)))
    menu.show_all()
    return menu


# -- the apps that open the selection (the status bar) --------------------------

class AppRow(Gtk.Box):
    """One button per app that opens the selection (the default first, ringed), then
    "other app". Alt+number opens with the numbered one (see Window.open_with_number)."""
    SIZE = 20

    def __init__(self, win):
        super().__init__(spacing=2)
        self.win = win
        self.token = 0

    def show_for(self, entries):
        """Name-based types now; for one file, its sniffed type once read (if it differs)."""
        self.token += 1
        self._build(entries, types_of(entries) if len(entries) > 1 else [e.content_type for e in entries[:1]])
        if len(entries) == 1 and not entries[0].is_dir and entries[0].archive is None:
            token, entry = self.token, entries[0]

            def sniffed(gfile, result):
                try:
                    ct = gfile.query_info_finish(result).get_content_type()
                except GLib.Error:
                    return
                if token == self.token and ct and ct != entry.content_type:
                    self._build([entry], [ct])

            Gio.File.new_for_path(entry.path).query_info_async(
                "standard::content-type", Gio.FileQueryInfoFlags.NONE, GLib.PRIORITY_DEFAULT, None, sniffed)

    def _build(self, entries, types):
        for child in self.get_children():
            child.destroy()
        if not entries:
            return
        default, apps = apps_for(types)
        win, label = self.win, what(entries)
        if len(entries) == 1 and program_kind(entries[0]):
            for icon, tip, terminal in (("system-run-symbolic", "Run it", False),
                                        ("utilities-terminal-symbolic", "Run it in a terminal", True)):
                button = Gtk.Button(relief=Gtk.ReliefStyle.NONE, can_focus=False, tooltip_text=tip)
                image = Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
                image.set_pixel_size(self.SIZE)
                button.add(image)
                button.get_style_context().add_class("qfm-app")
                button.connect("clicked", lambda _, t=terminal: run_program(win, entries[0], terminal=t))
                self.pack_start(button, False, False, 0)
            self.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL, margin_start=2, margin_end=2),
                            False, False, 0)
        for i, app in enumerate(apps + [None]):
            button = Gtk.Button(relief=Gtk.ReliefStyle.NONE, can_focus=False)
            number = 0 if app is None else i + 1     # Alt+number opens with it
            if app is None:
                image = Gtk.Image.new_from_icon_name("view-app-grid-symbolic", Gtk.IconSize.BUTTON)
                button.set_tooltip_text(f"Open {label} with another app… (Alt+0)")
                button.connect("clicked", lambda *_: choose_app(win, entries))
            else:
                icon = app.get_icon()
                image = (Gtk.Image.new_from_gicon(icon, Gtk.IconSize.BUTTON) if icon
                         else Gtk.Image.new_from_icon_name("application-x-executable", Gtk.IconSize.BUTTON))
                is_default = default is not None and app.get_id() == default.get_id()
                button.set_tooltip_text(f"Open {label} with {app.get_display_name()}"
                                        + (" (default)" if is_default else "")
                                        + (f" (Alt+{number})" if number <= 9 else ""))
                if is_default:
                    button.get_style_context().add_class("qfm-default-app")
                button.connect("clicked", lambda _, a=app: launch(win, a, entries))
            image.set_pixel_size(self.SIZE)
            overlay = Gtk.Overlay()
            overlay.add(image)
            if number <= 9:
                badge = Gtk.Label(label=str(number), halign=Gtk.Align.END, valign=Gtk.Align.END)
                badge.get_style_context().add_class("qfm-app-number")
                overlay.add_overlay(badge)
            button.add(overlay)
            button.get_style_context().add_class("qfm-app")
            self.pack_start(button, False, False, 0)
        self.show_all()
