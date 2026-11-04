# qfm

A keyboard-driven file manager for Linux (GTK3) with Miller columns.

![qfm](docs/screenshot.png)

## Features

- Opens as a floating window in the centre of the screen. Esc closes it, and it closes
  after opening a file. It can also run as a normal window (Options, Ctrl+,).
- Every action has a keyboard shortcut. F1 shows the list.
- Questions (rename, delete, name conflicts, extract to, open with) are shown in a bar at
  the bottom of the window, not in dialog windows.
- The preview panel shows images (SVG included), text with syntax highlighting, the first
  page of PDFs, Illustrator, PostScript and EPS files, and 3D models (STL, OBJ, PLY), and
  plays video and audio. Preview and Properties are two tabs (Ctrl+I), or one panel
  (Options). Permissions can be changed in Properties with checkboxes.
- The status bar shows the applications that can open the selection; click one, or press
  Alt+1 to Alt+9, to open it with that app.
- Archives (zip, tar, 7z, rar, iso) can be browsed like folders, and files can be copied out
  of them. The menu has Compress, Extract Here and Extract To.
- Files that are archives inside (Office and LibreOffice documents, EPUB, comic books, JAR,
  APK, wheels, 3MF…) open normally with Enter; → looks inside them.
- New file (Ctrl+N, or New From Template: text, Markdown, scripts, HTML, JSON, LibreOffice
  and Word documents - or your own, once Customize Templates puts them in `~/Templates`), duplicate (Ctrl+Shift+D), symbolic links (Make
  Link, Ctrl+Shift+V pastes as links), and renaming several items at once (F2: find and
  replace, or a new name with numbers).
- Alt+← and Alt+→ (or the mouse's back and forward buttons) go back and forward through the
  places you jumped to.
- The Size column (Options) shows folder sizes too, added up in the background.
- Ctrl+Z undoes rename, move, copy, move to trash, restore, new folder or file, duplicate,
  links, compress and extract.
- Files can be copied and pasted between qfm and other file managers.
- Optionally, qfm can be the default application for folders and for other applications'
  "Show in folder".
- Git repositories have their own folder icon, and the status bar shows their branch.
- Bookmarks, folder colors, trash, removable drives, search, multiple selection, and copy
  progress with cancel.

## Requirements

Arch Linux and X11. `install.sh` installs the required packages with pacman.

## Install

```bash
git clone https://github.com/davidnsousa/qfm.git
cd qfm
./install.sh
```

The installer asks for sudo and installs qfm in `/opt/qfm`. Run `./install.sh` again to
update. Run `qfm --uninstall` to remove it.

## Usage

```bash
qfm           # open at the last location
qfm PATH      # open at PATH (a file is selected in its folder)
```

If qfm is already open, the same window is used. Press F1 for the keyboard shortcuts, or
run `qfm --help`.

Settings, bookmarks and folder colors are stored in `~/.config/qfm/`.

## License

MIT. See [LICENSE](LICENSE).
