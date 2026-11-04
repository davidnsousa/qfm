"""Archives (zip, tar.*, 7z, rar, iso, …) through libarchive: listing their contents as
folders, extracting members, and creating archives. Inside qfm an archive's contents have
paths like a real folder's ("~/a.zip/docs/x.txt"); split() tells them apart."""
import os
import stat
import tempfile
import threading
import time
import uuid

import libarchive

SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz", ".tbz2", ".tar.xz", ".txz",
            ".tar.zst", ".tzst", ".tar.lz", ".tar.lzma", ".7z", ".rar", ".iso", ".cpio", ".cab", ".xar")
# files that are zip (or other) archives inside but documents, books, packages… outside: → looks
# inside them like an archive, Enter opens them as usual, and selecting one doesn't open a column
CONTAINER_SUFFIXES = (".docx", ".docm", ".xlsx", ".xlsm", ".pptx", ".pptm",     # Microsoft Office
                      ".odt", ".ods", ".odp", ".odg", ".ott", ".ots", ".otp",    # LibreOffice
                      ".epub", ".cbz", ".cbr", ".cb7", ".cbt",                   # books, comics
                      ".jar", ".war", ".ear", ".apk", ".aab", ".xapk", ".ipa",   # Java, Android, iOS
                      ".whl", ".nupkg", ".vsix", ".xpi", ".appx", ".msix", ".rpm",
                      ".3mf", ".kmz", ".ora", ".kra", ".xmind", ".sketch")
# Compress… offers these: suffix -> (libarchive format, filter)
FORMATS = {".zip": ("zip", None), ".tar.gz": ("gnutar", "gzip"), ".tar.xz": ("gnutar", "xz"),
           ".tar.zst": ("gnutar", "zstd"), ".7z": ("7zip", None)}
TEMP_DIR = os.path.join(tempfile.gettempdir(), f"qfm-{os.getuid()}")   # files opened from archives
Error = libarchive.ArchiveError


def is_archive(name):
    return name.lower().endswith(SUFFIXES)


def is_container(name):
    return name.lower().endswith(CONTAINER_SUFFIXES)


def strip_suffix(name):
    lower = name.lower()
    for suffix in sorted(SUFFIXES, key=len, reverse=True):
        if lower.endswith(suffix):
            return name[:-len(suffix)] or name
    return name


def split(path):
    """(archive file, member path inside it) if path is in an archive, else None.
    The archive itself gives (archive, "")."""
    probe = path
    while probe and probe != os.sep:
        if os.path.isfile(probe):
            if is_archive(probe):
                return probe, os.path.relpath(path, probe) if path != probe else ""
            return None
        if os.path.isdir(probe):
            return None
        probe = os.path.dirname(probe)
    return None


class Member:
    __slots__ = ("name", "inner", "is_dir", "size", "mtime", "symlink")

    def __init__(self, name, inner, is_dir, size=0, mtime=0, symlink=False):
        self.name, self.inner, self.is_dir = name, inner, is_dir
        self.size, self.mtime, self.symlink = size, mtime, symlink


class Listing:
    """An archive's members, by folder: folders[inner dir] = {name: Member}."""

    def __init__(self, path):
        self.folders = {"": {}}
        with libarchive.file_reader(path) as reader:
            for e in reader:
                inner = clean(e.pathname)
                if inner:
                    self._add(inner, e.isdir, e.size or 0, int(e.mtime or 0), e.issym)

    def _add(self, inner, is_dir, size, mtime, symlink):
        parent, name = os.path.split(inner)
        if parent and parent not in self.folders:
            self._add(parent, True, 0, mtime, False)    # folders some archives don't list
        members = self.folders.setdefault(parent, {})
        if is_dir:
            self.folders.setdefault(inner, {})
            if name in members:
                return
        members[name] = Member(name, inner, is_dir, size, mtime, symlink)

    def member(self, inner):
        parent, name = os.path.split(inner)
        return self.folders.get(parent, {}).get(name)

    def totals(self):
        """(files, folders, uncompressed bytes)."""
        files = folders = size = 0
        for members in self.folders.values():
            for m in members.values():
                if m.is_dir:
                    folders += 1
                else:
                    files += 1
                    size += m.size
        return files, folders, size

    def search(self, needle):
        """inner paths whose name contains needle (case-insensitive)."""
        needle = needle.casefold()
        return [m.inner for members in self.folders.values() for m in members.values()
                if needle in m.name.casefold()]


_cache, _lock, _reading = {}, threading.Lock(), {}


def listing(path):
    """The Listing of the archive at path (cached until the file changes). Raises Error/OSError."""
    st = os.stat(path)
    key = (path, st.st_mtime_ns, st.st_size)
    with _lock:
        if key in _cache:
            return _cache[key]
        reading = _reading.setdefault(path, threading.Lock())
    with reading:           # the preview and the column ask at once: read it once
        with _lock:
            if key in _cache:
                return _cache[key]
        result = Listing(path)
        with _lock:
            for old in [k for k in _cache if k[0] == path]:
                del _cache[old]
            _cache[key] = result
    return result


def clean(name):
    """A member name as a safe relative path ("" if it isn't one)."""
    parts = [p for p in name.replace("\\", "/").split("/") if p not in ("", ".")]
    if ".." in parts:
        return ""
    return "/".join(parts)


def extract(path, inners, dest, unique):
    """Extract members (files or whole folders) of the archive at path into the folder dest,
    each under its own name - made unique with unique(dest, name, is_dir). Returns the names."""
    tops = {}                                   # member inner path -> name in dest
    want = [clean(i) for i in inners]
    for inner in want:
        member = listing(path).member(inner)
        is_dir = bool(member and member.is_dir)
        tops[inner] = unique(dest, os.path.basename(inner), is_dir)
    dir_times = []
    with libarchive.file_reader(path) as reader:
        for e in reader:
            name = clean(e.pathname)
            for inner, top in tops.items():
                if name == inner or name.startswith(inner + "/"):
                    target = os.path.join(dest, top, name[len(inner) + 1:]) if name != inner else os.path.join(dest, top)
                    _write(e, target, dir_times)
                    break
    for target, mtime in dir_times:
        os.utime(target, (mtime, mtime))
    for inner, top in tops.items():     # folders only listed implicitly still exist
        if not os.path.lexists(os.path.join(dest, top)):
            os.makedirs(os.path.join(dest, top))
    return list(tops.values())


def _write(e, target, dir_times):
    if e.isdir:
        os.makedirs(target, exist_ok=True)
        if e.mtime:
            dir_times.append((target, int(e.mtime)))
        return
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if e.issym:
        if not os.path.lexists(target):
            os.symlink(e.linkpath, target)
        return
    if not e.isreg:
        return                                 # hard links, devices, fifos: skipped
    with open(target, "wb") as f:
        for block in e.get_blocks():
            f.write(block)
    mode = (e.mode or 0o644) & 0o777
    os.chmod(target, mode | stat.S_IRUSR | stat.S_IWUSR)
    if e.mtime:
        os.utime(target, (int(e.mtime), int(e.mtime)))


def extract_all(path, dest, unique):
    """Extract a whole archive into dest: its single top item as is, or else everything into a
    folder named after the archive. Returns the name created in dest."""
    top = listing(path).folders[""]
    if len(top) == 1:
        return extract(path, list(top), dest, unique)[0]
    folder = unique(dest, strip_suffix(os.path.basename(path)), True)
    os.makedirs(os.path.join(dest, folder))
    extract(path, list(top), os.path.join(dest, folder), unique)
    return folder


def to_temp(path, inners):
    """Extract members into a new private temp folder (to open, preview or drag them); their paths."""
    os.makedirs(TEMP_DIR, mode=0o700, exist_ok=True)
    folder = os.path.join(TEMP_DIR, uuid.uuid4().hex[:12])
    os.makedirs(folder)
    names = extract(path, inners, folder, lambda dest, name, is_dir: name)
    return [os.path.join(folder, n) for n in names]


def clean_temp(max_age=86400):
    """Remove temp folders older than a day (opened files had time to be read)."""
    import shutil
    try:
        folders = os.listdir(TEMP_DIR)
    except OSError:
        return
    for name in folders:
        folder = os.path.join(TEMP_DIR, name)
        try:
            if time.time() - os.lstat(folder).st_mtime > max_age:
                shutil.rmtree(folder, ignore_errors=True)
        except OSError:
            pass


def create(target, paths, suffix):
    """Write the files/folders at paths into a new archive target (format by suffix)."""
    fmt, flt = FORMATS[suffix]
    with libarchive.file_writer(target, fmt, flt) as writer:
        for p in paths:
            writer.add_files(p, pathname=os.path.basename(p))
