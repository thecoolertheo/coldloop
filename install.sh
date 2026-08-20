#!/usr/bin/env bash
#
# Coldloop installer.
#
# Everything Coldloop installs is per-user: a venv inside this checkout, two
# `systemd --user` units, a desktop entry and an icon. Nothing is written
# outside $HOME and nothing needs root, with one exception noted below.
#
#   ./install.sh              install and start
#   ./install.sh --uninstall  remove everything this script created
#   ./install.sh --no-boot    install, but start at login instead of at boot
#
set -euo pipefail

# Resolve the checkout, so the generated units point at wherever this actually
# lives rather than at the author's home directory.
COLDLOOP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICON_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"

UNITS=(liquidctl.service coldloop-lighting.service)

say() { printf '\033[36m==>\033[0m %s\n' "$1"; }
die() { printf '\033[31merror:\033[0m %s\n' "$1" >&2; exit 1; }

# --------------------------------------------------------------------------
# uninstall
# --------------------------------------------------------------------------

usage() {
    cat <<USAGE
Coldloop installer.

    ./install.sh              install and start
    ./install.sh --no-boot    install, but start at login instead of at boot
    ./install.sh --uninstall  remove the units, desktop entry and icon
    ./install.sh --help       this message
USAGE
}

# Reject anything unrecognised rather than falling through to a full install.
# Without this, a typo like --uninstal, or any flag this script does not know,
# silently reinstalls -- which repoints the units and desktop entry at whatever
# directory the script was run from.
case "${1:-}" in
    ""|--no-boot|--uninstall) ;;
    -h|--help) usage; exit 0 ;;
    *) printf '\033[31merror:\033[0m unknown option: %s\n\n' "$1" >&2; usage >&2; exit 2 ;;
esac
if [[ $# -gt 1 ]]; then
    printf '\033[31merror:\033[0m too many arguments\n\n' >&2; usage >&2; exit 2
fi

if [[ "${1:-}" == "--uninstall" ]]; then
    say "Stopping and disabling services"
    for unit in "${UNITS[@]}"; do
        systemctl --user disable --now "$unit" 2>/dev/null || true
        rm -f "$UNIT_DIR/$unit"
    done
    systemctl --user daemon-reload
    # Leave any start-limit failure behind cleared, or a later reinstall
    # inherits a unit systemd still considers failed.
    for unit in "${UNITS[@]}"; do
        systemctl --user reset-failed "$unit" 2>/dev/null || true
    done

    rm -f "$DESKTOP_DIR/coldloop.desktop" "$ICON_DIR/coldloop.svg"
    command -v update-desktop-database >/dev/null && \
        update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true

    say "Removed. Left in place deliberately:"
    echo "    ~/.config/coldloop/   your palette, faces and lighting settings"
    echo "    $COLDLOOP_DIR/venv    delete it yourself if you want the space back"
    echo "    linger: run 'loginctl disable-linger $USER' if nothing else needs it"
    exit 0
fi

START_AT_BOOT=1
[[ "${1:-}" == "--no-boot" ]] && START_AT_BOOT=0

# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------

[[ "$(uname -s)" == "Linux" ]] || die "Coldloop is Linux-only (it drives a hidraw device)."
command -v systemctl >/dev/null || die "systemd is required."
command -v python3 >/dev/null || die "python3 is required."

# --------------------------------------------------------------------------
# venv
# --------------------------------------------------------------------------

if [[ ! -x "$COLDLOOP_DIR/venv/bin/python" ]]; then
    say "Creating the virtualenv"
    python3 -m venv "$COLDLOOP_DIR/venv"
fi

say "Installing pinned dependencies"
"$COLDLOOP_DIR/venv/bin/pip" install --quiet --upgrade pip

# --no-deps deliberately. requirements.txt already pins every transitive
# dependency, and resolving them again would drag in liquidctl's declared
# `smbus`, a C extension that needs Python headers and a compiler. Coldloop
# never touches an I2C bus; see compat/smbus.py for how that import is
# satisfied instead.
"$COLDLOOP_DIR/venv/bin/pip" install --quiet --no-deps -r "$COLDLOOP_DIR/requirements.txt"

# liquidctl imports its SMBus drivers unconditionally at startup, so `smbus`
# must resolve or even `liquidctl list` raises ModuleNotFoundError.
if ! "$COLDLOOP_DIR/venv/bin/python" -c "import smbus" 2>/dev/null; then
    say "Installing the smbus compatibility alias"
    SITE="$("$COLDLOOP_DIR/venv/bin/python" -c \
        'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
    cp "$COLDLOOP_DIR/compat/smbus.py" "$SITE/smbus.py"
fi

# Prove the environment actually works before installing anything that relies
# on it. A venv that imports cleanly but cannot enumerate the bus is exactly
# the failure this check exists to catch.
say "Verifying the environment"
"$COLDLOOP_DIR/venv/bin/python" - <<'PY' || die "the virtualenv is not usable; see the error above"
import sys
from liquidctl.driver import find_liquidctl_devices
import numpy, PIL, psutil, PyQt6  # noqa: F401

found = [d.description for d in find_liquidctl_devices()]
print(f"    liquidctl enumerates: {', '.join(found) if found else 'nothing'}")
PY

# --------------------------------------------------------------------------
# device check
# --------------------------------------------------------------------------
#
# Done before installing anything that touches hardware. A cooler this project
# has not been verified against must not get its pump and fan curves.
say "Checking for a supported cooler"
if ! "$COLDLOOP_DIR/venv/bin/python" "$COLDLOOP_DIR/kraken_hud.py" --wait-for-device 5; then
    echo
    echo "Coldloop has only been verified against the NZXT Kraken 2024 Elite RGB"
    echo "(USB 1e71:3012). It refuses to configure anything else, because the pump"
    echo "and fan duties in its service unit were checked against that device only"
    echo "and are applied at boot."
    echo
    echo "If the cooler is present but not readable, you are probably missing the"
    echo "udev rule that grants your user access to the hidraw node. liquidctl"
    echo "ships one:  https://github.com/liquidctl/liquidctl/blob/main/extra/linux/71-liquidctl.rules"
    echo "Installing it is the one step here that needs root."
    die "No supported cooler found."
fi

# --------------------------------------------------------------------------
# units, desktop entry, icon
# --------------------------------------------------------------------------

render() {  # render <template> <destination>
    mkdir -p "$(dirname "$2")"
    sed "s#@COLDLOOP_DIR@#$COLDLOOP_DIR#g" "$1" > "$2"
}

say "Installing systemd user units"
for unit in "${UNITS[@]}"; do
    render "$COLDLOOP_DIR/$unit.in" "$UNIT_DIR/$unit"
done

say "Installing the desktop entry and icon"
render "$COLDLOOP_DIR/coldloop.desktop.in" "$DESKTOP_DIR/coldloop.desktop"
mkdir -p "$ICON_DIR"
cp "$COLDLOOP_DIR/coldloop.svg" "$ICON_DIR/coldloop.svg"
command -v update-desktop-database >/dev/null && \
    update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true

# --------------------------------------------------------------------------
# start
# --------------------------------------------------------------------------

if (( START_AT_BOOT )); then
    # Two halves, and neither works alone: linger starts this user's systemd
    # instance at boot, and the units' WantedBy=default.target gives it
    # something to start. graphical-session.target does not exist until login.
    say "Enabling lingering so the services start at boot"
    if ! loginctl enable-linger "$USER" 2>/dev/null; then
        echo "    could not enable lingering; services will start at login instead."
        echo "    run 'sudo loginctl enable-linger $USER' to start them at boot."
    fi
fi

systemctl --user daemon-reload
say "Enabling and starting services"
systemctl --user enable --now "${UNITS[@]}"

echo
say "Done."
systemctl --user --no-pager --plain list-units --type=service "${UNITS[@]}" || true
echo
echo "  Coldloop is in your app grid, or run it directly:"
echo "    $COLDLOOP_DIR/venv/bin/python $COLDLOOP_DIR/kraken_controller.py"
echo
echo "  Uninstall with:  ./install.sh --uninstall"
