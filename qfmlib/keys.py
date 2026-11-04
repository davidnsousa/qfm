"""Every shortcut, in one table: the F1 panel and `qfm --help` are both made from it.
(No GTK here, so --help works without a display.)"""

SHORTCUTS = [
    ("Navigate", [
        ("↑ ↓  PgUp PgDn  Home End", "move in the column"),
        ("→ / Enter on a folder", "go into it (archives too)"),
        ("← / Backspace / Alt+↑", "back to the parent folder"),
        ("letters", "jump to a matching name"),
        ("Ctrl+L / ✎ button", "the path as text: copy it, or type one (Tab completes, Enter goes)"),
        ("Ctrl+F / 🔍 button", "filter the column; Enter searches everything below it"),
        ("Ctrl+B", "show / focus / hide the bookmarks sidebar"),
        ("Delete (in the sidebar)", "remove the selected bookmark"),
    ]),
    ("Select", [
        ("Shift+↑ ↓ / Shift+click", "extend the selection"),
        ("Ctrl+click", "add or remove an item"),
        ("Ctrl+A", "select everything in the column"),
    ]),
    ("Open", [
        ("Enter / double-click on a file", "open it with its default app"),
        ("Alt+1 … Alt+9", "open with app 1…9 in the status bar"),
        ("Alt+0", "open with any other app"),
        ("Ctrl+O / Menu / Copilot key / Shift+F10", "the actions menu (also right-click)"),
        ("Ctrl+E", "show in the system file manager"),
    ]),
    ("Files", [
        ("Ctrl+C / Ctrl+X / Ctrl+V", "copy / cut / paste into the current folder (other file managers too)"),
        ("Ctrl+Z", "undo the last rename, move, copy, trash, restore, new folder, compress or extract"),
        ("F2", "rename"),
        ("Ctrl+Shift+N", "new folder"),
        ("Delete", "move to the trash"),
        ("Shift+Delete", "delete permanently (asks y / n)"),
        ("Ctrl+Shift+C", "copy the path(s) as text"),
        ("drag onto a folder", "move (same disk) or copy; hold Ctrl to copy, Shift to move"),
        ("k / r / s", "a name already exists: keep both / replace (old one to the trash) / skip"),
        ("Esc (while copying)", "cancel the copy or move"),
    ]),
    ("Trash and drives (sidebar)", [
        ("Trash", "browse it; menu: Restore, Delete Permanently, Empty Trash"),
        ("Delete (in the trash)", "delete for good (asks y / n)"),
        ("a drive", "click to open (mounts it if needed); its ⏏ button ejects it"),
    ]),
    ("Archives", [
        ("→ / Enter on an archive", "browse it like a folder (read-only)"),
        ("Ctrl+C, then Ctrl+V in a folder", "extract what's selected inside it"),
        ("menu: Compress… / Extract Here / Extract To…", "make or unpack archives"),
    ]),
    ("Bookmarks and colors", [
        ("Ctrl+D", "bookmark / unbookmark the selection"),
        ("Ctrl+1 … Ctrl+7", "folder color: red orange yellow green blue purple gray"),
        ("Ctrl+0", "remove the folder color"),
    ]),
    ("View", [
        ("Space", "show / hide the preview"),
        ("Ctrl+H", "show / hide hidden files"),
        ("Ctrl+R / F5", "reload"),
        ("Ctrl+,", "options: sorting, columns, panels, window…"),
        ("F1", "this list"),
    ]),
    ("Video and audio (in the preview)", [
        ("Ctrl+P", "play / pause"),
        ("Ctrl+← / Ctrl+→", "back / forward 5 seconds"),
        ("Ctrl+M", "sound on / off (previews start muted)"),
    ]),
    ("Questions bar", [
        ("Enter", "answer"),
        ("Esc", "cancel"),
        ("y / n", "confirm / refuse deleting permanently"),
        ("Tab", "complete a folder name (Extract To)"),
        ("Ctrl+Enter", "take the folder you've moved to in the columns (Extract To)"),
    ]),
    ("Window", [
        ("Esc", "cancel a question / clear the filter / close a side panel / cancel copies; "
                "else quit (as a quick dialog - ⚙ Options)"),
        ("Ctrl+Q / Ctrl+W", "quit"),
    ]),
]


def help_text():
    """The shortcuts as plain text, for `qfm --help`."""
    width = max(len(keys) for _, rows in SHORTCUTS for keys, _ in rows) + 2
    lines = []
    for group, rows in SHORTCUTS:
        lines.append(f"\n{group}:")
        lines += [f"  {keys:<{width}}{what}" for keys, what in rows]
    return "\n".join(lines)
