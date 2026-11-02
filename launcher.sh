#!/usr/bin/env bash
# qfm launcher (from the deskapp launcher_plain.sh). install.sh
# copies this file to /opt/qfm/qfm; /usr/local/bin/qfm
# (and the app-menu entry, if any) point to it. It runs main.py in its
# venv, passing every option through - the app handles --help and its own
# options - except --uninstall, which install.sh handles.
case "${1:-}" in
    --uninstall|-R|--R) exec "/opt/qfm/install.sh" --uninstall ;;
esac
exec "/opt/qfm/.venv/bin/python" "/opt/qfm/main.py" "$@"
