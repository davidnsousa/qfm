"""Tells when GitHub has a newer qfm than the one installed. It only tells: updating stays
`git pull` and ./install.sh, which also saves the date of the commit it installs."""
import json
import os
import threading
import time
import urllib.request
from datetime import datetime

URL = "https://api.github.com/repos/davidnsousa/qfm/commits/main"
INSTALLED = os.path.expanduser("~/.local/state/qfm/installed")
INTERVAL = 3600      # seconds between checks (unauthenticated GitHub calls are limited)


def installed_date():
    """The date of the commit installed; None if it wasn't installed from a git clone."""
    try:
        with open(INSTALLED) as f:
            return datetime.fromisoformat(f.read().strip().replace("Z", "+00:00"))
    except (OSError, ValueError):
        return None


def latest_date():
    """The date of the newest commit on GitHub; None if GitHub can't be asked."""
    try:
        with urllib.request.urlopen(URL, timeout=15) as reply:
            return datetime.fromisoformat(json.load(reply)["commit"]["committer"]["date"].replace("Z", "+00:00"))
    except Exception:    # offline, rate limited, a reply that isn't what's expected
        return None


def watch(on_change):
    """Checks now and then every INTERVAL; on_change(available) runs, on this thread, when
    the answer changes. The last date known is kept while GitHub can't be reached."""
    available, latest = False, None
    while True:
        latest = latest_date() or latest
        installed = installed_date()      # read each time: a reinstall clears the notice
        now = bool(latest and installed and latest > installed)
        if now != available:
            available = now
            on_change(now)
        time.sleep(INTERVAL)


def start(on_change):
    threading.Thread(target=watch, args=(on_change,), daemon=True).start()
