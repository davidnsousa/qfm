# qfm

A keyboard-driven file manager for Linux (GTK3) with Miller columns.

![qfm](docs/screenshot.png)

## Features

- Opens as a floating window in the centre of the screen. Esc closes it, and it closes
  after opening a file. It can also run as a normal window (settings).
- Every action has a keyboard shortcut. F1 shows the list.
- Questions (rename, delete, name conflicts, extract to, open with) are shown in a bar at
  the bottom of the window, not in dialog windows.
- The preview panel shows images, text with syntax highlighting and the first page of PDFs,
  and plays video and audio. The file's properties are shown below the preview.
- The status bar shows the applications that can open the selection; click one, or press
  Alt+1 to Alt+9, to open it with that app.
- Archives (zip, tar, 7z, rar, iso) can be browsed like folders, and files can be copied out
  of them. The menu has Compress, Extract Here and Extract To.
- Ctrl+Z undoes rename, move, copy, move to trash, restore, new folder, compress and extract.
- Files can be copied and pasted between qfm and other file managers.
- Optionally, qfm can be the default application for folders and for other applications'
  "Show in folder".
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
