"""Copying and moving with progress and cancel (a worker thread; the window polls the counters).
Moves within a disk are renames; across disks they copy, then delete the source only once
that item is fully copied. Cancelling removes the item being copied, so nothing is left half-done."""
import errno
import os
import shutil
import threading
import time

CHUNK = 1 << 20     # bytes copied between progress updates / cancel checks


class Cancelled(Exception):
    pass


def tree_size(path):
    """Bytes in path and everything under it (symlinks count as themselves)."""
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            total = 0
            for folder, _, files in os.walk(path):
                for name in files:
                    try:
                        total += os.lstat(os.path.join(folder, name)).st_size
                    except OSError:
                        pass
            return total
        return os.lstat(path).st_size
    except OSError:
        return 0


class Job:
    """One copy or move of several items: [(source, target)]. start() runs it in a thread;
    done(job) is then called from the thread - use GLib.idle_add in it."""

    def __init__(self, pairs, move, done):
        self.pairs, self.move, self.done = pairs, move, done
        self.cancel = threading.Event()
        self.total = self.copied = 0
        self.current = ""
        self.finished = []          # targets fully done
        self.error = None
        self.cancelled = False
        self.started = time.monotonic()

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def speed(self):
        elapsed = time.monotonic() - self.started
        return self.copied / elapsed if elapsed > 0.5 else 0

    def _run(self):
        try:
            self.total = sum(tree_size(s) for s, _ in self.pairs)
            for source, target in self.pairs:
                self.current = os.path.basename(source)
                if os.path.lexists(target):     # (appeared since we checked: never touch it)
                    raise OSError(errno.EEXIST, "already exists", target)
                if self.move and self._rename(source, target):
                    pass
                else:
                    try:
                        self._copy(source, target)
                    except BaseException:
                        _remove(target)         # nothing half-copied stays behind
                        raise
                    if self.move:
                        _remove(source)
                self.finished.append(target)
        except Cancelled:
            self.cancelled = True
        except OSError as err:
            name = os.path.basename(err.filename) if err.filename else self.current
            self.error = f"{err.strerror or err}: {name}" if name else str(err.strerror or err)
        self.done(self)

    def _rename(self, source, target):
        """A move within one disk: instant. False if it has to be copied instead."""
        try:
            os.rename(source, target)
        except OSError as err:
            if err.errno == errno.EXDEV:
                return False
            raise
        self.copied += tree_size(target)
        return True

    def _copy(self, source, target):
        if self.cancel.is_set():
            raise Cancelled
        if os.path.islink(source):
            os.symlink(os.readlink(source), target)
        elif os.path.isdir(source):
            os.mkdir(target)
            for name in sorted(os.listdir(source)):
                self._copy(os.path.join(source, name), os.path.join(target, name))
            shutil.copystat(source, target, follow_symlinks=False)
        else:
            self.current = os.path.basename(source)
            with open(source, "rb") as src, open(target, "wb") as dst:
                while True:
                    if self.cancel.is_set():
                        raise Cancelled
                    block = src.read(CHUNK)
                    if not block:
                        break
                    dst.write(block)
                    self.copied += len(block)
            shutil.copystat(source, target)


def _remove(path):
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.lexists(path):
            os.remove(path)
    except OSError:
        pass
