#!/usr/bin/env python3
"""Coldloop — desktop control panel for the NZXT Kraken 2024 Elite RGB on Linux.

Covers pump/fan duty, LCD orientation and brightness, the HUD systemd service,
a preset gallery, an editor for the suite's own files, and diagnostics.

Only commands verified against the installed liquidctl driver and the real
device are issued here; see VERIFIED_COMMANDS.md.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from PyQt6.QtCore import (
    QEasingCurve,
    QMimeData,
    QSize,
    QObject,
    QPropertyAnimation,
    QRectF,
    QRunnable,
    Qt,
    QThreadPool,
    QTimer,
    pyqtProperty,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QColor,
    QDrag,
    QFont,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGraphicsItem,
    QGraphicsObject,
    QGraphicsScene,
    QGraphicsView,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

APP_NAME = "Coldloop"
APP_TAGLINE = "NZXT Kraken 2024 Elite RGB"

# --------------------------------------------------------------------------
# Paths and constants
# --------------------------------------------------------------------------

HERE = Path(__file__).resolve().parent
VENV_PYTHON = HERE / "venv" / "bin" / "python"
LIQUIDCTL = HERE / "venv" / "bin" / "liquidctl"
HUD_SCRIPT = HERE / "kraken_hud.py"
CONTROLLER_SCRIPT = Path(__file__).resolve()
SERVICE_NAME = "liquidctl.service"
SERVICE_UNIT = Path.home() / ".config" / "systemd" / "user" / SERVICE_NAME

PYTHON = str(VENV_PYTHON if VENV_PYTHON.exists() else sys.executable)
LIQ = str(LIQUIDCTL if LIQUIDCTL.exists() else "liquidctl")
MATCH = "Kraken"

# Duty limits come from the driver's own channel table
# (_SPEED_CHANNELS_KRAKEN2023): pump is clamped to 20-100, fan to 0-100.
PUMP_MIN, PUMP_MAX = 20, 100

# The fan channel's real floor is 0, and liquidctl accepts `set fan speed 0`
# silently with exit status 0 -- it stops the radiator fans outright while
# reporting success. Applied at every login by an auto-restarting service, that
# is a self-reinstating thermal shutdown. This app therefore refuses to expose
# anything below a safe floor; the slider cannot physically reach it.
FAN_MIN_SAFE, FAN_MAX = 25, 100

ORIENTATIONS = (0, 90, 180, 270)

# Shared with kraken_hud.py. The cooler is a HID endpoint with no arbitration:
# two processes talking to it at once steal each other's reply reports, which
# shows up as a status read of 0 rpm / 0% rather than as an error. Every
# liquidctl call from either process takes this lock.
LOCK_PATH = Path(os.environ.get("KRAKEN_LOCK_PATH", "/dev/shm/kraken_liquidctl.lock"))

# The HUD publishes its latest reading here. Preferring it means the GUI does
# not open a second conversation with the device while the service is running.
STATUS_PATH = Path(os.environ.get("KRAKEN_STATUS_PATH", "/dev/shm/kraken_status.json"))
STATUS_MAX_AGE = 8.0

# --------------------------------------------------------------------------
# Dark palette
#
# Text colours were checked against their own backgrounds with the WCAG
# relative-luminance formula; see palette_report(), which recomputes the
# numbers from these constants so they cannot silently drift.
# --------------------------------------------------------------------------

BG = "#0b111a"  # window
SURFACE = "#141d2a"  # cards
SURFACE_2 = "#1d2838"  # inputs, hover
BORDER = "#263345"
BORDER_SOFT = "#1e2937"

TEXT = "#e9f3f1"  # 14.6:1 on SURFACE
TEXT_DIM = "#9fb3b9"  # 7.0:1 on SURFACE
TEXT_FAINT = "#6b8189"  # decorative labels only

ACCENT = "#2dd4bf"
ACCENT_BRIGHT = "#5eead4"
ACCENT_DEEP = "#14b8a6"
ACCENT_SOFT = "rgba(45, 212, 191, 0.14)"

WARN = "#fbbf24"
DANGER = "#fb7185"
OFF = "#3d4b5c"


def _srgb_to_linear(channel: float) -> float:
    c = channel / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(color: str) -> float:
    value = color.lstrip("#")
    r, g, b = (float(int(value[i : i + 2], 16)) for i in (0, 2, 4))
    return 0.2126 * _srgb_to_linear(r) + 0.7152 * _srgb_to_linear(g) + 0.0722 * _srgb_to_linear(b)


def contrast_ratio(fg: str, bg: str) -> float:
    a, b = relative_luminance(fg), relative_luminance(bg)
    if a < b:
        a, b = b, a
    return (a + 0.05) / (b + 0.05)


def palette_report() -> list[tuple[str, float, bool]]:
    """Recompute foreground contrast against the surfaces they sit on."""
    checks = [
        ("body text on card", TEXT, SURFACE, 4.5),
        ("dim text on card", TEXT_DIM, SURFACE, 4.5),
        ("accent on card", ACCENT, SURFACE, 3.0),
        ("body text on window", TEXT, BG, 4.5),
        ("warning on card", WARN, SURFACE, 4.5),
        ("danger on card", DANGER, SURFACE, 3.0),
        # Inputs and lists sit on SURFACE_2, a lighter ground than the cards.
        ("input text", TEXT, SURFACE_2, 4.5),
        ("selected list item", ACCENT, SURFACE_2, 3.0),
        ("placeholder text", TEXT_FAINT, SURFACE_2, 3.0),
    ]
    out = []
    for name, fg, bg, floor in checks:
        ratio = contrast_ratio(fg, bg)
        out.append((name, ratio, ratio >= floor))
    return out


STYLESHEET = f"""
QWidget {{
    /* SF Pro first because it is installed here and is what kraken_hud.py
       renders the faces themselves with (see its FONT_FAMILIES), so the app
       and the display it controls read as one thing. Inter was previously
       first and is not installed on this system -- fc-match resolves it to
       Noto Sans -- so that preference never did anything. */
    font-family: "SF Pro", "SF Pro Display", "Inter", "Cantarell", "Noto Sans", sans-serif;
    font-size: 13px;
    color: {TEXT};
}}
QMainWindow, QWidget#Root {{ background: {BG}; }}

QLabel#Wordmark {{
    font-size: 24px; font-weight: 600; color: {TEXT};
    letter-spacing: 0.5px;
}}
QLabel#Tagline {{ font-size: 12px; color: {TEXT_FAINT}; }}
QLabel#CardTitle {{
    font-size: 11px; font-weight: 700; color: {TEXT_FAINT};
    letter-spacing: 1.4px;
}}
QLabel#RowLabel {{ font-size: 13px; color: {TEXT_DIM}; }}
QLabel#RowValue {{ font-size: 14px; font-weight: 700; color: {ACCENT}; }}
QLabel#Hint {{ font-size: 12px; color: {TEXT_FAINT}; }}
QLabel#Warn {{ font-size: 12px; color: {WARN}; }}

QFrame#Card {{
    background: {SURFACE};
    border: 1px solid {BORDER_SOFT};
    border-radius: 16px;
}}
QFrame#Tile {{
    background: {SURFACE};
    border: 1px solid {BORDER_SOFT};
    border-radius: 14px;
}}
QFrame#Divider {{ background: {BORDER_SOFT}; border: none; }}

/* ---- sidebar ---- */
QListWidget#Nav {{
    background: transparent; border: none; outline: none;
    font-size: 13.5px;
}}
QListWidget#Nav::item {{
    padding: 11px 14px; margin-bottom: 6px;
    border-radius: 11px; color: {TEXT_DIM};
}}
QListWidget#Nav::item:hover {{ background: {SURFACE_2}; color: {TEXT}; }}
QListWidget#Nav::item:selected {{
    background: {ACCENT_SOFT}; color: {ACCENT}; font-weight: 700;
}}

/* ---- buttons ---- */
QPushButton {{
    background: {ACCENT}; color: #05231f;
    /* Must be an explicit border, not `border: none`: with no border declared
       the Adwaita platform style takes over drawing the frame and the QSS
       background is never painted, leaving dark label text on the card. */
    border: 1px solid {ACCENT};
    border-radius: 10px;
    padding: 9px 18px; font-weight: 700; font-size: 13px;
    /* Without a floor the layout can squeeze buttons until their label is
       clipped away entirely when the window is short. */
    min-height: 19px;
}}
QPushButton:hover {{ background: {ACCENT_BRIGHT}; border-color: {ACCENT_BRIGHT}; }}
QPushButton:pressed {{ background: {ACCENT_DEEP}; border-color: {ACCENT_DEEP}; }}
QPushButton:disabled {{ background: {SURFACE_2}; color: {TEXT_FAINT}; border-color: {SURFACE_2}; }}

QPushButton#Ghost {{
    background: {SURFACE_2}; color: {TEXT};
    border: 1px solid {BORDER};
}}
QPushButton#Ghost:hover {{ background: {BORDER}; border-color: {ACCENT_DEEP}; }}
QPushButton#Ghost:pressed {{ background: {SURFACE}; }}

/* ---- inputs ----
   Every widget type used anywhere in the app needs a rule here. The base
   QWidget rule sets a near-white text colour for the dark theme, but does
   not set a background, so any widget without its own rule keeps the
   platform's light default and renders white-on-white. That is what
   happened to the studio's component list, name field and spin boxes, none
   of which existed when this sheet was written. */
QListWidget {{
    background: {SURFACE_2}; border: 1px solid {BORDER_SOFT};
    border-radius: 10px; color: {TEXT}; outline: none;
}}
QListWidget::item {{ padding: 7px 9px; border-radius: 8px; color: {TEXT}; }}
QListWidget::item:hover {{ background: {SURFACE}; }}
QListWidget::item:selected {{ background: {ACCENT_SOFT}; color: {ACCENT}; }}

/* QAbstractSpinBox covers both QSpinBox and QDoubleSpinBox. */
QLineEdit, QAbstractSpinBox {{
    background: {SURFACE_2}; border: 1px solid {BORDER};
    border-radius: 9px; padding: 7px 10px; color: {TEXT};
    selection-background-color: {ACCENT_DEEP}; selection-color: #05231f;
}}
QLineEdit:focus, QAbstractSpinBox:focus {{ border-color: {ACCENT_DEEP}; }}
QLineEdit::placeholder {{ color: {TEXT_FAINT}; }}
/* Same ground as the field itself: a contrasting button colour renders as
   a bright sliver at the edge of every spin box rather than as a control. */
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{
    background: transparent; border: none; width: 14px;
}}

QSlider::groove:horizontal {{
    height: 6px; background: {SURFACE_2}; border-radius: 3px;
}}
QSlider::sub-page:horizontal {{
    background: {ACCENT}; border-radius: 3px;
}}
QSlider::handle:horizontal {{
    background: {TEXT}; border: 3px solid {ACCENT};
    width: 14px; height: 14px; margin: -7px 0; border-radius: 10px;
}}
QSlider::handle:horizontal:hover {{ border-color: {ACCENT_BRIGHT}; }}
QSlider::groove:horizontal:disabled {{ background: {SURFACE_2}; }}

QComboBox {{
    background: {SURFACE_2}; border: 1px solid {BORDER};
    border-radius: 10px; padding: 8px 12px; color: {TEXT};
}}
QComboBox:hover {{ border-color: {ACCENT_DEEP}; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox QAbstractItemView {{
    background: {SURFACE_2}; border: 1px solid {BORDER};
    border-radius: 8px; color: {TEXT};
    selection-background-color: {ACCENT_DEEP}; selection-color: #05231f;
    outline: none;
}}

QPlainTextEdit {{
    background: #0d1520; border: 1px solid {BORDER_SOFT};
    border-radius: 12px; padding: 12px;
    color: {TEXT}; selection-background-color: {ACCENT_DEEP};
    selection-color: #05231f;
    /* Declared here, not via setFont(): the QWidget font-family rule above
       outranks a programmatic QFont, which left code in a proportional face. */
    font-family: "Noto Sans Mono", "DejaVu Sans Mono", monospace;
    font-size: 12px;
}}

QScrollBar:vertical {{
    background: transparent; width: 10px; margin: 4px;
}}
QScrollBar::handle:vertical {{
    background: {BORDER}; border-radius: 5px; min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: {OFF}; }}
QScrollBar:horizontal {{
    background: transparent; height: 10px; margin: 4px;
}}
QScrollBar::handle:horizontal {{
    background: {BORDER}; border-radius: 5px; min-width: 30px;
}}
QScrollBar::handle:horizontal:hover {{ background: {OFF}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

QStatusBar {{ color: {TEXT_DIM}; border-top: 1px solid {BORDER_SOFT}; }}
QToolTip {{
    background: {SURFACE_2}; color: {TEXT};
    border: 1px solid {BORDER}; border-radius: 6px; padding: 5px;
}}
"""


# --------------------------------------------------------------------------
# Custom widgets
# --------------------------------------------------------------------------


class ToggleSwitch(QAbstractButton):
    """An animated switch that only ever shows externally-confirmed state.

    Clicking does not flip the switch. The click fires a command, the widget
    shows a muted "pending" track, and the knob only moves once a poll of
    `systemctl is-active`/`is-enabled` confirms the change. That way the switch
    cannot claim the service is running when it silently died.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(False)
        self.setFixedSize(46, 26)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._pos = 0.0
        self._on = False
        self._pending = False
        self._anim = QPropertyAnimation(self, b"knob", self)
        self._anim.setDuration(180)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutCubic)

    def get_knob(self) -> float:
        return self._pos

    def set_knob(self, value: float) -> None:
        self._pos = value
        self.update()

    knob = pyqtProperty(float, fget=get_knob, fset=set_knob)

    def is_on(self) -> bool:
        return self._on

    def set_state(self, on: bool, pending: bool = False) -> None:
        changed = on != self._on
        self._on = on
        self._pending = pending
        if changed:
            self._anim.stop()
            self._anim.setStartValue(self._pos)
            self._anim.setEndValue(1.0 if on else 0.0)
            self._anim.start()
        else:
            self._pos = 1.0 if on else 0.0
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        radius = self.height() / 2.0

        if self._pending:
            track = QColor(ACCENT_DEEP)
            track.setAlpha(110)
        elif self._on:
            track = QColor(ACCENT)
        else:
            track = QColor(OFF)

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(QRectF(0, 0, self.width(), self.height()), radius, radius)

        margin = 3.0
        knob_d = self.height() - margin * 2
        travel = self.width() - knob_d - margin * 2
        x = margin + travel * self._pos

        p.setBrush(QColor("#0b111a") if self._on else QColor(TEXT_DIM))
        p.drawEllipse(QRectF(x, margin, knob_d, knob_d))
        p.end()

    def sizeHint(self):  # noqa: N802
        return self.size()


class StatTile(QFrame):
    """A compact live-telemetry readout used in the header strip."""

    def __init__(self, label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Tile")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(18)
        shadow.setOffset(0, 3)
        shadow.setColor(QColor(0, 0, 0, 110))
        self.setGraphicsEffect(shadow)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(3)

        self.caption = QLabel(label.upper())
        self.caption.setObjectName("CardTitle")
        layout.addWidget(self.caption)

        row = QHBoxLayout()
        row.setSpacing(4)
        row.setContentsMargins(0, 0, 0, 0)
        self.value = QLabel("--")
        self.value.setStyleSheet(
            f"font-size: 25px; font-weight: 600; color: {TEXT}; letter-spacing: -0.5px;"
        )
        self.unit = QLabel("")
        self.unit.setStyleSheet(f"font-size: 12px; color: {TEXT_FAINT};")
        self.unit.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom
        )
        row.addWidget(self.value)
        row.addWidget(self.unit)
        row.addStretch(1)
        layout.addLayout(row)

    def set_value(self, value: str, unit: str = "", color: str = TEXT) -> None:
        self.value.setText(value)
        self.value.setStyleSheet(
            f"font-size: 25px; font-weight: 600; color: {color}; letter-spacing: -0.5px;"
        )
        self.unit.setText(unit)


class StatusDot(QWidget):
    """Small filled circle used for the device-connection indicator."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(9, 9)
        self._color = QColor(OFF)

    def set_color(self, color: str) -> None:
        self._color = QColor(color)
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._color)
        p.drawEllipse(self.rect())
        p.end()


def make_card(title: str | None = None, *, spacing: int = 12) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("Card")
    shadow = QGraphicsDropShadowEffect(frame)
    shadow.setBlurRadius(26)
    shadow.setOffset(0, 5)
    shadow.setColor(QColor(0, 0, 0, 120))
    frame.setGraphicsEffect(shadow)

    layout = QVBoxLayout(frame)
    layout.setContentsMargins(20, 18, 20, 18)
    layout.setSpacing(spacing)
    if title:
        heading = QLabel(title.upper())
        heading.setObjectName("CardTitle")
        layout.addWidget(heading)
    return frame, layout


def divider() -> QFrame:
    line = QFrame()
    line.setObjectName("Divider")
    line.setFixedHeight(1)
    return line


# The dark disc behind a gallery preview, and the frame drawn on it. The gap
# between them is the bezel; keep the well the larger of the two or the
# preview's edge lands on the well's own antialiased rim and looks ragged.
PREVIEW_WELL = 250
PREVIEW_DISC = 226


def circular(pixmap: QPixmap, size: int) -> QPixmap:
    """Crop a rendered HUD frame to a disc, the way the panel displays it.

    The frames themselves stay square -- that is what the device is sent, and
    kraken_hud.py's vignette already fades content out before the edge -- but
    a square preview misrepresents the result, most obviously on the dark
    faces where the corners the panel never shows are a solid block of colour.
    """
    scaled = pixmap.scaled(
        size,
        size,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    out = QPixmap(size, size)
    out.fill(Qt.GlobalColor.transparent)  # so the card's well shows through
    painter = QPainter(out)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addEllipse(0.0, 0.0, float(size), float(size))
    painter.setClipPath(path)
    # Centre the frame in case scaling produced an off-square pixmap.
    painter.drawPixmap((size - scaled.width()) // 2, (size - scaled.height()) // 2, scaled)
    painter.end()
    return out


def scrollable(page: QWidget) -> QScrollArea:
    """Wrap a page so a short window scrolls instead of crushing its contents.

    Qt will shrink widgets past their sensible size to fit the viewport, which
    clips button labels rather than producing a scrollbar.
    """
    area = QScrollArea()
    area.setWidget(page)
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    # Deliberately no stylesheet on the viewport. A stylesheet set directly on a
    # widget cascades to every descendant and outranks the application
    # stylesheet, so an unqualified `background: transparent` here silently
    # stripped the fill from every button on the page. The page widget carries
    # objectName "Root" and paints the background itself instead.
    return area


def clear_layout(layout) -> None:
    """Remove and schedule deletion of every item in a layout, recursively.

    Used to rebuild the lighting tab's device cards after each device
    refresh without leaking the previous widgets.
    """
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.deleteLater()
            continue
        child = item.layout()
        if child is not None:
            clear_layout(child)


# --------------------------------------------------------------------------
# Async command execution
# --------------------------------------------------------------------------


class TaskSignals(QObject):
    done = pyqtSignal(object, int, str, str)  # token, returncode, stdout, stderr


@contextlib.contextmanager
def device_lock(timeout: float = 20.0):
    """Hold exclusive access to the cooler for the duration of a command."""
    handle = None
    try:
        handle = open(LOCK_PATH, "w")
    except OSError:
        yield False
        return

    deadline = time.monotonic() + timeout
    acquired = False
    try:
        while time.monotonic() < deadline:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                time.sleep(0.05)
        yield acquired
    finally:
        if acquired:
            with contextlib.suppress(OSError):
                fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def _needs_lock(cmd: list[str]) -> bool:
    """Only device commands need the lock; systemctl and journalctl do not."""
    return bool(cmd) and (cmd[0] == LIQ or cmd[0].endswith("liquidctl"))


class Task(QRunnable):
    def __init__(self, token, cmd: list[str], timeout: float = 40.0):
        super().__init__()
        self.token = token
        self.cmd = cmd
        self.timeout = timeout
        self.signals = TaskSignals()

    def run(self) -> None:
        try:
            if _needs_lock(self.cmd):
                with device_lock():
                    proc = subprocess.run(
                        self.cmd, capture_output=True, text=True, timeout=self.timeout
                    )
            else:
                proc = subprocess.run(
                    self.cmd, capture_output=True, text=True, timeout=self.timeout
                )
            self.signals.done.emit(self.token, proc.returncode, proc.stdout, proc.stderr)
        except subprocess.TimeoutExpired:
            self.signals.done.emit(self.token, 124, "", f"timed out after {self.timeout}s")
        except OSError as exc:
            self.signals.done.emit(self.token, 127, "", str(exc))


def run_sync(cmd: list[str], timeout: float = 15.0) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    except OSError as exc:
        return 127, "", str(exc)


def systemctl(*args: str) -> list[str]:
    return ["systemctl", "--user", *args]


# --------------------------------------------------------------------------
# Presets
# --------------------------------------------------------------------------


# Where kraken_hud.py keeps the user's chosen colours, and the teal it falls
# back to. Both are duplicated here rather than imported for the same reason
# as PRESETS below -- importing kraken_hud would pull numpy/PIL/psutil into
# the GUI process just to read three strings. The HUD owns writing this file;
# the GUI only ever reads it, and delegates writes to `kraken_hud.py
# --save-colours` so validation and the atomic replace live in one place.
PALETTE_PATH = Path(
    os.environ.get(
        "KRAKEN_PALETTE_PATH",
        Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "coldloop" / "palette.json",
    )
)
DEFAULT_COLOURS = {"primary": "#2dd4bf", "secondary": "#67e8f9", "tertiary": "#a7f3d0"}


FACE_PATH = PALETTE_PATH.with_name("face.json")

# Label -> value for the HUD's --transition modes, with what each costs on
# this device (a push is ~0.56s) so the trade is visible at the point of
# choosing rather than buried in the README.
TRANSITIONS = (
    ("Wipe — radial sweep (~1.5s)", "wipe"),
    ("Instant — no transition (~0.6s)", "instant"),
    ("Loading frame (~1.1s)", "loading"),
)


def load_transition() -> str:
    try:
        value = json.loads(FACE_PATH.read_text()).get("transition")
    except (OSError, ValueError, AttributeError):
        return "wipe"
    return value if value in {v for _, v in TRANSITIONS} else "wipe"


def load_colours() -> dict[str, str]:
    """Current palette, falling back to the default for anything unreadable."""
    colours = dict(DEFAULT_COLOURS)
    try:
        raw = json.loads(PALETTE_PATH.read_text())
    except (OSError, ValueError):
        return colours
    for role in colours:
        value = raw.get(role)
        if isinstance(value, str) and len(value.lstrip("#")) == 6:
            colours[role] = "#" + value.lstrip("#").lower()
    return colours


@dataclass
class Preset:
    key: str
    title: str
    description: str
    args: list[str] = field(default_factory=list)


# Kept in sync with kraken_hud.py's STYLES registry -- key must match a
# --style choice there. Descriptions are duplicated (not imported) so this
# file has no import-time dependency on kraken_hud.py's heavier deps
# (numpy/PIL/psutil) just to populate the gallery.
PRESETS = [
    Preset(
        key="telemetry",
        title="Telemetry rings",
        description="Coolant hero gauge with CPU, GPU and RAM rings, plus FPS.",
        args=["--style", "rings"],
    ),
    Preset(
        key="minimal",
        title="Minimal coolant",
        description="Just the coolant reading and FPS -- nothing else competing for attention.",
        args=["--style", "minimal"],
    ),
    Preset(
        key="bars",
        title="System bars",
        description="Coolant reading up top, CPU/GPU/RAM load as bar meters below.",
        args=["--style", "bars"],
    ),
    Preset(
        key="hardware",
        title="Pump & fan",
        description="Pump and fan RPM as the headline -- what the cooler itself is doing.",
        args=["--style", "hardware"],
    ),
    Preset(
        key="dial",
        title="Analogue dial",
        description="Coolant as a needle swept over a tick scale, read by position.",
        args=["--style", "dial"],
    ),
    Preset(
        key="trend",
        title="Coolant trend",
        description="The last few minutes of coolant temperature as a line graph.",
        args=["--style", "trend"],
    ),
    Preset(
        key="orbit",
        title="Orbit rings",
        description="CPU, GPU and RAM as three nested full circles around the coolant number.",
        args=["--style", "orbit"],
    ),
    Preset(
        key="night",
        title="Night mode",
        description="Light readouts on a near-black ground, for a dark room or case.",
        args=["--style", "night"],
    ),
]


# --------------------------------------------------------------------------
# Face studio
#
# Builds the JSON component specs that kraken_hud.py's render_custom() draws.
# The canvas paints its own approximation of each component rather than
# calling the real renderer: dragging has to repaint at interactive speed and
# a real frame costs a subprocess, numpy and PIL. The Preview button renders
# the genuine frame, so the approximation is never the last word -- which is
# also why it stays deliberately rough rather than trying to imitate the
# renderer's antialiasing and fonts.
# --------------------------------------------------------------------------

HUD_SIZE = 640  # the spec's coordinate space, matching the panel


class ComponentItem(QGraphicsObject):
    """One component on the studio canvas, backed by its spec dict."""

    changed = pyqtSignal()

    def __init__(self, spec: dict):
        super().__init__()
        self.spec = spec
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        # Arcs are drawn concentric with the display by the renderer, so
        # dragging one would be a lie -- only its radius and angles matter.
        if spec.get("type") != "arc":
            self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
            self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setPos(float(spec.get("x", HUD_SIZE / 2)), float(spec.get("y", HUD_SIZE / 2)))

    def boundingRect(self) -> QRectF:
        kind = self.spec.get("type")
        if kind == "arc":
            r = float(self.spec.get("radius", 240)) + float(self.spec.get("thickness", 24))
            # Centred on the display, not on this item's own position.
            return QRectF(-HUD_SIZE / 2 - r, -HUD_SIZE / 2 - r, 2 * r + HUD_SIZE, 2 * r + HUD_SIZE)
        if kind == "bar":
            w = float(self.spec.get("width", 360))
            h = float(self.spec.get("height", 24))
            return QRectF(-w / 2 - 4, -h / 2 - 4, w + 8, h + 8)
        size = float(self.spec.get("size", 32))
        text = self._text()
        return QRectF(-size * len(text) * 0.36 - 6, -size * 0.72 - 6, size * len(text) * 0.72 + 12, size * 1.44 + 12)

    def _text(self) -> str:
        if self.spec.get("type") == "label":
            return str(self.spec.get("text", "")) or "TEXT"
        return SAMPLE_VALUES.get(self.spec.get("metric", ""), "88")

    def paint(self, painter: QPainter, _option, _widget=None) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        kind = self.spec.get("type")
        colour = QColor(self.spec.get("colour", ACCENT))
        track = QColor(self.spec.get("track_colour", "#ddf5f0"))

        if kind == "arc":
            r = float(self.spec.get("radius", 240))
            th = float(self.spec.get("thickness", 24))
            start = float(self.spec.get("start", -135.0))
            sweep = float(self.spec.get("sweep", 270.0))
            # Scene origin is this item's pos; arcs sit at the display centre.
            cx = HUD_SIZE / 2 - self.pos().x()
            cy = HUD_SIZE / 2 - self.pos().y()
            box = QRectF(cx - r, cy - r, 2 * r, 2 * r)
            # Qt angles: degrees*16, counterclockwise, 0 at 3 o'clock. The
            # spec uses clockwise from 12 o'clock, hence the conversion.
            qt_start = int((90.0 - start) * 16)
            qt_span = int(-sweep * 16)
            if self.spec.get("track", True):
                painter.setPen(QPen(track, th, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                painter.drawArc(box, qt_start, qt_span)
            painter.setPen(QPen(colour, th, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawArc(box, qt_start, int(qt_span * 0.62))  # a plausible fill
        elif kind == "bar":
            w = float(self.spec.get("width", 360))
            h = float(self.spec.get("height", 24))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(track)
            painter.drawRoundedRect(QRectF(-w / 2, -h / 2, w, h), h / 2, h / 2)
            painter.setBrush(colour)
            painter.drawRoundedRect(QRectF(-w / 2, -h / 2, w * 0.62, h), h / 2, h / 2)
        else:
            size = float(self.spec.get("size", 32))
            font = QFont()
            font.setPixelSize(int(size))
            font.setBold(self.spec.get("weight", "bold") in ("bold", "black", "semibold"))
            if self.spec.get("spacing"):
                font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, float(self.spec["spacing"]))
            painter.setFont(font)
            painter.setPen(QPen(colour))
            painter.drawText(self.boundingRect(), Qt.AlignmentFlag.AlignCenter, self._text())

        if self.isSelected():
            pen = QPen(QColor(ACCENT_BRIGHT), 2, Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(self.boundingRect())

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self.spec["x"] = round(self.pos().x())
            self.spec["y"] = round(self.pos().y())
            self.changed.emit()
        return super().itemChange(change, value)


# Stand-in readings so text components have a plausible width on the canvas
# before any real telemetry exists.
SAMPLE_VALUES = {
    "coolant": "100.4",
    "cpu_load": "72%",
    "gpu_load": "44%",
    "ram_percent": "38%",
    "ram_used_gb": "12.1G",
    "cpu_temp": "142°",
    "gpu_temp": "131°",
    "fps": "144",
    "pump_rpm": "2400",
    "fan_rpm": "1300",
}


class FaceCanvas(QGraphicsView):
    """The 640x640 display, with components dropped and dragged onto it."""

    dropped = pyqtSignal(str, float, float)
    selected = pyqtSignal(object)

    def __init__(self) -> None:
        scene = QGraphicsScene(0, 0, HUD_SIZE, HUD_SIZE)
        super().__init__(scene)
        self.setAcceptDrops(True)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setFixedSize(470, 470)
        self.setFrameShape(QFrame.Shape.NoFrame)
        # Outside the disc is not part of the display; paint it as window
        # chrome so the round work area reads as the panel itself.
        self.setBackgroundBrush(QColor(BG))
        self.fitInView(0, 0, HUD_SIZE, HUD_SIZE, Qt.AspectRatioMode.KeepAspectRatio)
        scene.selectionChanged.connect(self._emit_selection)
        self.dark = False

    def _emit_selection(self) -> None:
        items = self.scene().selectedItems()
        self.selected.emit(items[0] if items else None)

    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:
        super().drawBackground(painter, rect)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        # The panel is round: anything outside this disc is never displayed,
        # so the studio shows the same disc rather than a square work area.
        painter.setBrush(QColor("#04211f") if self.dark else QColor("#ffffff"))
        painter.drawEllipse(QRectF(0, 0, HUD_SIZE, HUD_SIZE))

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasText():
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasText():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        point = self.mapToScene(event.position().toPoint())
        self.dropped.emit(event.mimeData().text(), point.x(), point.y())
        event.acceptProposedAction()


class PaletteList(QListWidget):
    """The component types, dragged onto the canvas."""

    def __init__(self) -> None:
        super().__init__()
        self.setDragEnabled(True)
        self.setFixedWidth(190)
        self.setWordWrap(True)
        self.setSpacing(4)

    def startDrag(self, _actions) -> None:
        item = self.currentItem()
        if item is None:
            return
        mime = QMimeData()
        mime.setText(item.data(Qt.ItemDataRole.UserRole))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction)


FACES_DIR = PALETTE_PATH.parent / "faces"

# (title, spec type, short line shown under the title, full tooltip). The
# short line has to fit the narrow list without eliding mid-word, so the
# fuller explanation lives in the tooltip rather than being truncated.
COMPONENT_CATALOGUE = (
    ("Arc gauge", "arc", "A ring that fills.", "A ring that fills with a metric. Always centred on the display."),
    ("Bar meter", "bar", "A bar that fills.", "A horizontal bar that fills with a metric. Drag to position."),
    ("Metric value", "value", "The live reading.", "The current value of a metric, drawn as text."),
    ("Text label", "label", "Fixed caption text.", "Fixed text you type, for captions like COOLANT."),
)

# Starting geometry per component, so a dropped one is immediately visible
# and plausibly sized rather than a dot at the origin.
COMPONENT_DEFAULTS = {
    "arc": {
        "metric": "coolant",
        "radius": 250,
        "thickness": 24,
        "start": -135.0,
        "sweep": 270.0,
        "track": True,
    },
    "bar": {"metric": "cpu_load", "width": 300, "height": 22, "track": True},
    "value": {"metric": "coolant", "size": 72, "weight": "black"},
    "label": {"text": "LABEL", "size": 22, "weight": "semibold", "spacing": 3.0},
}

# Reserved name for the studio's own scratch render. kraken_hud.py hides
# faces starting with an underscore from the selectable list, so this never
# shows up in the gallery or in --list-faces.
PREVIEW_FACE = "__studio_preview"


def safe_face_name(name: str) -> str:
    """Mirror kraken_hud.py's own filename sanitising for face names."""
    return re.sub(r"[^A-Za-z0-9 _-]", "", name or "").strip()


class FaceStudio(QDialog):
    """Drag-and-drop editor producing the JSON that render_custom() draws."""

    def __init__(self, parent, vocab: dict, name: str = "", spec: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle("Face studio")
        # Without the Root name the stylesheet's window rule does not match
        # and the dialog falls back to the platform's light background.
        self.setObjectName("Root")
        self.setStyleSheet(STYLESHEET)
        self.vocab = vocab
        self.spec = spec or {"dark": False, "components": []}
        self.saved_name: str | None = None
        self.current = None

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        top = QHBoxLayout()
        top.setSpacing(14)
        root.addLayout(top)

        left_card, left = make_card("COMPONENTS")
        left_card.setFixedWidth(226)
        self.palette_list = PaletteList()
        for title, key, hint, tip in COMPONENT_CATALOGUE:
            entry = QListWidgetItem()
            entry.setData(Qt.ItemDataRole.UserRole, key)
            entry.setToolTip(tip)
            entry.setSizeHint(QSize(170, 54))
            self.palette_list.addItem(entry)
            # A two-line string in one item can only have one text style, so
            # the title and its description are separate labels in a widget.
            # WA_TransparentForMouseEvents is essential: without it the widget
            # swallows the presses the list needs to start a drag.
            row = QWidget()
            row.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            box = QVBoxLayout(row)
            box.setContentsMargins(10, 7, 10, 7)
            box.setSpacing(2)
            # Not `name`: that is the face-name parameter, and shadowing it
            # here made QLineEdit(name) take this label as its parent widget
            # instead of its text, so editing a face opened with an empty
            # name field.
            heading = QLabel(title)
            heading.setStyleSheet(f"font-size: 14px; font-weight: 700; color: {TEXT};")
            note = QLabel(hint)
            note.setStyleSheet(f"font-size: 11.5px; color: {TEXT_FAINT};")
            box.addWidget(heading)
            box.addWidget(note)
            self.palette_list.setItemWidget(entry, row)
        # Tall enough for the whole catalogue, so no component is hidden
        # behind a scrollbar in a list of four. Item height plus the 4px
        # inter-item spacing plus the frame -- omitting the spacing left the
        # last entry half-cut.
        self.palette_list.setFixedHeight((54 + 6) * len(COMPONENT_CATALOGUE) + 20)
        self.palette_list.itemDoubleClicked.connect(
            lambda item: self.add_component(
                item.data(Qt.ItemDataRole.UserRole), HUD_SIZE / 2, HUD_SIZE / 2
            )
        )
        left.addWidget(self.palette_list)
        tip = QLabel("Drag one onto the display, or double-click to drop it in the centre.")
        tip.setObjectName("Hint")
        tip.setWordWrap(True)
        left.addWidget(tip)
        left.addStretch(1)
        top.addWidget(left_card)

        centre_card, centre = make_card("DISPLAY")
        self.canvas = FaceCanvas()
        self.canvas.dark = bool(self.spec.get("dark"))
        self.canvas.dropped.connect(self.add_component)
        self.canvas.selected.connect(self.show_properties)
        centre.addWidget(self.canvas, alignment=Qt.AlignmentFlag.AlignHCenter)
        centre.addStretch(1)
        top.addWidget(centre_card)

        right_card, right = make_card("PROPERTIES")
        right_card.setFixedWidth(286)
        self.prop_title = QLabel("Nothing selected")
        self.prop_title.setStyleSheet(f"font-size: 15px; font-weight: 700; color: {TEXT};")
        right.addWidget(self.prop_title)
        self.prop_hint = QLabel("Pick a component on the display to change it.")
        self.prop_hint.setObjectName("Hint")
        self.prop_hint.setWordWrap(True)
        right.addWidget(self.prop_hint)
        self.prop_host = QWidget()
        self.prop_form = QFormLayout(self.prop_host)
        self.prop_form.setContentsMargins(0, 0, 0, 0)
        self.prop_form.setSpacing(9)
        self.prop_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        right.addWidget(self.prop_host)
        self.delete_btn = QPushButton("Remove component")
        self.delete_btn.setObjectName("Ghost")
        self.delete_btn.clicked.connect(self.remove_selected)
        self.delete_btn.setEnabled(False)
        right.addWidget(self.delete_btn)
        right.addStretch(1)
        top.addWidget(right_card)

        bottom_card, bottom_box = make_card()
        bottom = QHBoxLayout()
        bottom.setSpacing(10)
        bottom_box.addLayout(bottom)

        label = QLabel("Name")
        label.setObjectName("RowLabel")
        bottom.addWidget(label)
        self.name_edit = QLineEdit(name)
        self.name_edit.setPlaceholderText("My face")
        self.name_edit.setFixedWidth(200)
        bottom.addWidget(self.name_edit)

        self.dark_box = QComboBox()
        self.dark_box.addItem("Light background", False)
        self.dark_box.addItem("Dark background", True)
        self.dark_box.setCurrentIndex(1 if self.spec.get("dark") else 0)
        self.dark_box.currentIndexChanged.connect(self.set_dark)
        bottom.addWidget(self.dark_box)

        preview = QPushButton("Render preview")
        preview.setObjectName("Ghost")
        preview.clicked.connect(self.render_preview)
        bottom.addWidget(preview)

        self.status = QLabel("")
        self.status.setObjectName("Hint")
        bottom.addWidget(self.status, 1)

        # Plain buttons rather than QDialogButtonBox: the box renders with the
        # platform's own icons and mnemonics, which sit oddly against the rest
        # of this app's flat styling.
        cancel = QPushButton("Cancel")
        cancel.setObjectName("Ghost")
        cancel.clicked.connect(self.reject)
        bottom.addWidget(cancel)
        save_btn = QPushButton("Save face")
        save_btn.clicked.connect(self.save)
        bottom.addWidget(save_btn)
        root.addWidget(bottom_card)

        for component in self.spec.get("components", []):
            self._add_item(component)

    # -- canvas contents -------------------------------------------------

    def _add_item(self, component: dict) -> ComponentItem:
        item = ComponentItem(component)
        self.canvas.scene().addItem(item)
        return item

    def add_component(self, kind: str, x: float, y: float) -> None:
        if kind not in COMPONENT_DEFAULTS:
            return
        component = {"type": kind, "x": round(x), "y": round(y)}
        component.update(COMPONENT_DEFAULTS[kind])
        component["colour"] = self.default_colour(kind)
        self.spec.setdefault("components", []).append(component)
        item = self._add_item(component)
        self.canvas.scene().clearSelection()
        item.setSelected(True)

    def default_colour(self, kind: str) -> str:
        palette = self.vocab.get("palette", {})
        if kind in ("arc", "bar"):
            return palette.get("primary", ACCENT)
        if self.spec.get("dark"):
            return palette.get("night_text", "#99f6e4")
        return palette.get("text" if kind == "value" else "label", "#115e59")

    def remove_selected(self) -> None:
        for item in list(self.canvas.scene().selectedItems()):
            if item.spec in self.spec.get("components", []):
                self.spec["components"].remove(item.spec)
            self.canvas.scene().removeItem(item)
        self.show_properties(None)

    def set_dark(self) -> None:
        self.spec["dark"] = bool(self.dark_box.currentData())
        self.canvas.dark = self.spec["dark"]
        self.canvas.viewport().update()

    # -- property panel --------------------------------------------------

    def show_properties(self, item) -> None:
        # removeRow(), not takeAt(): taking the items empties a row but leaves
        # the row itself in place, so the panel accumulates blank space every
        # time the selection changes. removeRow also deletes the widgets now
        # rather than deferring to the event loop.
        while self.prop_form.rowCount():
            self.prop_form.removeRow(0)

        self.current = item
        self.delete_btn.setEnabled(item is not None)
        if item is None:
            self.prop_title.setText("Nothing selected")
            self.prop_hint.setText("Pick a component on the display to change it.")
            self.prop_hint.setVisible(True)
            return

        kind = item.spec.get("type")
        titles = {key: title for title, key, _, _ in COMPONENT_CATALOGUE}
        self.prop_title.setText(titles.get(kind, str(kind)))
        # Arcs cannot be dragged, so say why rather than leaving it a mystery.
        self.prop_hint.setVisible(kind == "arc")
        if kind == "arc":
            self.prop_hint.setText("Centred on the display; use radius and angles to place it.")
        for field_name in self.vocab.get("components", {}).get(kind, []):
            widget = self.build_field(item, field_name)
            if widget is not None:
                self.prop_form.addRow(field_name.replace("_", " ").capitalize(), widget)

    def build_field(self, item, field_name: str):
        spec = item.spec

        def commit(key, value):
            spec[key] = value
            item.prepareGeometryChange()
            item.update()

        if field_name == "metric":
            box = QComboBox()
            for key, label in self.vocab.get("metrics", {}).items():
                box.addItem(label, key)
            index = box.findData(spec.get("metric"))
            if index >= 0:
                box.setCurrentIndex(index)
            box.currentIndexChanged.connect(lambda: commit("metric", box.currentData()))
            return box

        if field_name == "colour":
            button = QPushButton(spec.get("colour", "#000000"))

            def paint_swatch(value: str) -> None:
                ink = "#000000" if QColor(value).lightnessF() > 0.55 else "#ffffff"
                button.setText(value)
                button.setStyleSheet(
                    f"background: {value}; color: {ink}; border: none; border-radius: 8px;"
                )

            paint_swatch(spec.get("colour", ACCENT))

            def pick() -> None:
                chosen = QColorDialog.getColor(
                    QColor(spec.get("colour", ACCENT)), self, "Component colour"
                )
                if not chosen.isValid():
                    return
                commit("colour", chosen.name().lower())
                paint_swatch(chosen.name().lower())

            button.clicked.connect(pick)
            return button

        if field_name == "text":
            edit = QLineEdit(str(spec.get("text", "")))
            edit.textChanged.connect(lambda value: commit("text", value))
            return edit

        if field_name == "weight":
            box = QComboBox()
            for weight in self.vocab.get("weights", []):
                box.addItem(weight, weight)
            index = box.findData(spec.get("weight"))
            if index >= 0:
                box.setCurrentIndex(index)
            box.currentIndexChanged.connect(lambda: commit("weight", box.currentData()))
            return box

        if field_name == "track":
            box = QComboBox()
            box.addItem("Show track", True)
            box.addItem("No track", False)
            box.setCurrentIndex(0 if spec.get("track", True) else 1)
            box.currentIndexChanged.connect(lambda: commit("track", bool(box.currentData())))
            return box

        ranges = {
            "radius": (10, 300),
            "thickness": (2, 80),
            "start": (-180, 180),
            "sweep": (5, 360),
            "width": (20, 620),
            "height": (4, 120),
            "size": (8, 200),
            "spacing": (0, 20),
        }
        if field_name in ranges:
            low, high = ranges[field_name]
            floaty = field_name in ("start", "sweep", "spacing")
            box = QDoubleSpinBox() if floaty else QSpinBox()
            box.setRange(low, high)
            current = spec.get(field_name, low)
            box.setValue(float(current) if floaty else int(current))
            box.valueChanged.connect(lambda value: commit(field_name, value))
            return box
        return None

    # -- output ----------------------------------------------------------

    def write_face(self, name: str) -> Path:
        FACES_DIR.mkdir(parents=True, exist_ok=True)
        path = FACES_DIR / f"{safe_face_name(name)}.json"
        tmp = path.with_suffix(".tmp.json")
        tmp.write_text(json.dumps(self.spec, indent=2) + "\n")
        os.replace(tmp, path)  # atomic, so the HUD never reads a half-written face
        return path

    def render_preview(self) -> None:
        """Render the real frame, not the canvas's approximation of it."""
        self.write_face(PREVIEW_FACE)
        out = Path("/tmp/kraken_studio_preview.png")
        self.status.setText("rendering…")
        rc, _stdout, stderr = run_sync(
            [
                PYTHON,
                str(HUD_SCRIPT),
                "--preview",
                "--output",
                str(out),
                "--style",
                f"custom:{PREVIEW_FACE}",
            ],
            timeout=40,
        )
        if rc != 0 or not out.exists():
            self.status.setText(f"preview failed: {stderr.strip()[:120]}")
            return
        self.status.setText("")
        dialog = QDialog(self)
        dialog.setWindowTitle("Preview")
        dialog.setStyleSheet(STYLESHEET)
        layout = QVBoxLayout(dialog)
        label = QLabel()
        label.setPixmap(circular(QPixmap(str(out)), 420))
        layout.addWidget(label)
        dialog.exec()

    def save(self) -> None:
        name = safe_face_name(self.name_edit.text())
        if not name:
            self.status.setText("Give the face a name first.")
            return
        if not self.spec.get("components"):
            self.status.setText("Add at least one component first.")
            return
        self.write_face(name)
        self.saved_name = name
        self.accept()


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------


class Controller(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1000, 760)
        self.setMinimumSize(880, 640)
        self.pool = QThreadPool.globalInstance()
        self._service_active = False
        self._service_enabled = False
        self._duty_synced = False

        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(24, 20, 24, 12)
        outer.setSpacing(18)

        outer.addLayout(self._build_header())
        outer.addLayout(self._build_telemetry_strip())

        body = QHBoxLayout()
        body.setSpacing(20)
        outer.addLayout(body, 1)

        self.nav = QListWidget()
        self.nav.setObjectName("Nav")
        self.nav.setFixedWidth(178)
        self.nav.setFrameShape(QFrame.Shape.NoFrame)
        for name in ("Hardware", "Gallery", "Script Editor", "Diagnostics"):
            QListWidgetItem(name, self.nav)
        self.nav.setCurrentRow(0)
        self.nav.currentRowChanged.connect(self._on_nav_changed)
        body.addWidget(self.nav)

        self.pages = QStackedWidget()
        body.addWidget(self.pages, 1)
        self.pages.addWidget(scrollable(self._build_hardware_page()))
        self.pages.addWidget(scrollable(self._build_gallery_page()))
        self.pages.addWidget(self._build_editor_page())
        self.pages.addWidget(self._build_diagnostics_page())

        self.status = self.statusBar()
        self.status.showMessage("Ready")

        # Service and device state are polled rather than inferred from clicks,
        # so nothing in the UI can claim a state that is not real.
        self.state_timer = QTimer(self)
        self.state_timer.timeout.connect(self.refresh_service_state)
        self.state_timer.start(3000)

        self.telemetry_timer = QTimer(self)
        self.telemetry_timer.timeout.connect(self.refresh_telemetry)
        self.telemetry_timer.start(5000)

        self.refresh_service_state()
        self.refresh_telemetry()

    # -- header ----------------------------------------------------------

    def _build_header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(12)

        titles = QVBoxLayout()
        titles.setSpacing(1)
        wordmark = QLabel(APP_NAME)
        wordmark.setObjectName("Wordmark")
        tagline = QLabel(APP_TAGLINE)
        tagline.setObjectName("Tagline")
        titles.addWidget(wordmark)
        titles.addWidget(tagline)
        row.addLayout(titles)
        row.addStretch(1)

        self.conn_dot = StatusDot()
        self.conn_label = QLabel("checking…")
        self.conn_label.setObjectName("Hint")
        row.addWidget(self.conn_dot)
        row.addWidget(self.conn_label)
        return row

    def _build_telemetry_strip(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(14)
        self.tile_coolant = StatTile("Coolant")
        self.tile_pump = StatTile("Pump")
        self.tile_fan = StatTile("Fan")
        self.tile_service = StatTile("HUD service")
        for tile in (self.tile_coolant, self.tile_pump, self.tile_fan, self.tile_service):
            row.addWidget(tile)
        return row

    def refresh_telemetry(self) -> None:
        # Prefer the reading the HUD already published. Polling the device
        # ourselves while the service is running makes both processes contend
        # for the same HID endpoint, which corrupts readings rather than
        # erroring.
        published = self._read_published_status()
        if published is not None:
            self._show_telemetry(published)
            return

        task = Task("telemetry", [LIQ, "--match", MATCH, "status", "--json"], 12.0)
        task.signals.done.connect(lambda _t, rc, out, err: self._apply_telemetry(rc, out))
        self.pool.start(task)

    def _read_published_status(self) -> dict[str, float] | None:
        try:
            raw = json.loads(STATUS_PATH.read_text())
        except (OSError, json.JSONDecodeError):
            return None

        stamp = raw.get("timestamp")
        if not isinstance(stamp, (int, float)) or time.time() - stamp > STATUS_MAX_AGE:
            return None

        readings: dict[str, float] = {}
        for source, key in (
            ("coolant", "Liquid temperature"),
            ("pump_rpm", "Pump speed"),
            ("fan_rpm", "Fan speed"),
            ("pump_duty", "Pump duty"),
            ("fan_duty", "Fan duty"),
        ):
            value = raw.get(source)
            if value is not None:
                readings[key] = float(value)
        return readings

    def _apply_telemetry(self, rc: int, out: str) -> None:
        if rc != 0 or not out.strip():
            self.conn_dot.set_color(DANGER)
            self.conn_label.setText("device not found")
            for tile in (self.tile_coolant, self.tile_pump, self.tile_fan):
                tile.set_value("--", "", TEXT_FAINT)
            return

        try:
            payload = json.loads(out)
        except json.JSONDecodeError:
            return

        readings: dict[str, float] = {}
        for device in payload:
            for item in device.get("status", []):
                if item.get("value") is not None:
                    readings[item["key"]] = float(item["value"])
        self._show_telemetry(readings)

    def _show_telemetry(self, readings: dict[str, float]) -> None:
        self.conn_dot.set_color(ACCENT)
        self.conn_label.setText("connected")

        coolant = readings.get("Liquid temperature")
        if coolant is None:
            self.tile_coolant.set_value("--", "", TEXT_FAINT)
        else:
            # Warm readings get a colour cue, matching the HUD's redundant
            # temperature encoding.
            color = ACCENT if coolant < 45 else (WARN if coolant < 55 else DANGER)
            self.tile_coolant.set_value(f"{coolant:.1f}", "°C", color)

        pump = readings.get("Pump speed")
        self.tile_pump.set_value(
            f"{pump:.0f}" if pump is not None else "--", "rpm", TEXT if pump else TEXT_FAINT
        )
        fan = readings.get("Fan speed")
        self.tile_fan.set_value(
            f"{fan:.0f}" if fan is not None else "--", "rpm", TEXT if fan else TEXT_FAINT
        )

        # Sync the duty sliders to what the device is actually doing, but
        # only the first time a reading arrives. Telemetry refreshes every
        # 5s -- resyncing on every tick would fight the user mid-drag, and
        # would even stomp a duty they just applied, since the device takes
        # a moment to report the new value back and an intervening refresh
        # would show the still-stale old one. Once, on the first real
        # reading, is enough to fix the actual bug (sliders starting at a
        # hardcoded 75/50 guess instead of whatever the device is really at).
        if not self._duty_synced:
            pump_duty = readings.get("Pump duty")
            fan_duty = readings.get("Fan duty")
            if pump_duty is not None:
                self.pump_slider.setValue(int(pump_duty))
            if fan_duty is not None:
                self.fan_slider.setValue(int(fan_duty))
            if pump_duty is not None or fan_duty is not None:
                self._duty_synced = True

    # -- shared helpers --------------------------------------------------

    def dispatch(self, label: str, cmd: list[str], after=None, timeout: float = 40.0) -> None:
        self.status.showMessage(f"{label}…")
        task = Task(label, cmd, timeout)

        def handle(token, rc, out, err):
            if rc == 0:
                self.status.showMessage(f"{token} — done", 4000)
            else:
                detail = (err or out or "").strip().splitlines()
                message = detail[-1] if detail else f"exit {rc}"
                self.status.showMessage(f"{token} — FAILED: {message}", 9000)
            if after is not None:
                after(rc, out, err)

        task.signals.done.connect(handle)
        self.pool.start(task)

    def _slider_row(
        self,
        parent: QVBoxLayout,
        label: str,
        lo: int,
        hi: int,
        initial: int,
        suffix: str = "%",
    ) -> QSlider:
        row = QHBoxLayout()
        row.setSpacing(14)
        name = QLabel(label)
        name.setObjectName("RowLabel")
        name.setMinimumWidth(92)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(lo, hi)
        slider.setValue(initial)
        readout = QLabel(f"{initial}{suffix}")
        readout.setObjectName("RowValue")
        readout.setMinimumWidth(50)
        readout.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        slider.valueChanged.connect(lambda v: readout.setText(f"{v}{suffix}"))
        row.addWidget(name)
        row.addWidget(slider, 1)
        row.addWidget(readout)
        parent.addLayout(row)
        return slider

    # -- hardware page ---------------------------------------------------

    def _build_hardware_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("Root")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)

        # --- cooling ---
        cooling, cl = make_card("Cooling")
        self.pump_slider = self._slider_row(cl, "Pump duty", PUMP_MIN, PUMP_MAX, 75)
        self.fan_slider = self._slider_row(cl, "Fan duty", FAN_MIN_SAFE, FAN_MAX, 50)

        note = QLabel(
            f"Fan duty is limited to {FAN_MIN_SAFE}% and above — liquidctl accepts "
            "“set fan speed 0” silently with exit status 0 and stops the radiator fans."
        )
        note.setObjectName("Warn")
        note.setWordWrap(True)
        cl.addWidget(note)
        cl.addWidget(divider())

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        apply_pump = QPushButton("Apply pump")
        apply_pump.clicked.connect(self.apply_pump)
        apply_fan = QPushButton("Apply fan")
        apply_fan.clicked.connect(self.apply_fan)
        hint = QLabel("Cooling changes are applied deliberately, not while dragging.")
        hint.setObjectName("Hint")
        buttons.addWidget(apply_pump)
        buttons.addWidget(apply_fan)
        buttons.addSpacing(6)
        buttons.addWidget(hint)
        buttons.addStretch(1)
        cl.addLayout(buttons)
        layout.addWidget(cooling)

        # --- display ---
        display, dl = make_card("Display")
        orow = QHBoxLayout()
        orow.setSpacing(14)
        olabel = QLabel("Orientation")
        olabel.setObjectName("RowLabel")
        olabel.setMinimumWidth(92)
        self.orientation_box = QComboBox()
        for value in ORIENTATIONS:
            self.orientation_box.addItem(f"{value}°", value)
        self.orientation_box.setCurrentIndex(2)  # 180, the device default here
        self.orientation_box.activated.connect(self.apply_orientation)
        orow.addWidget(olabel)
        orow.addWidget(self.orientation_box, 1)
        dl.addLayout(orow)

        self.brightness_slider = self._slider_row(dl, "Brightness", 0, 100, 100)
        # Display settings are harmless and apply on release; cooling does not.
        self.brightness_slider.sliderReleased.connect(self.apply_brightness)

        dl.addWidget(divider())
        brow = QHBoxLayout()
        brow.setSpacing(10)
        restore = QPushButton("Restore native display")
        restore.setObjectName("Ghost")
        restore.clicked.connect(self.restore_native_display)
        rhint = QLabel("Stops the HUD first, or the next frame would overwrite it.")
        rhint.setObjectName("Hint")
        brow.addWidget(restore)
        brow.addWidget(rhint)
        brow.addStretch(1)
        dl.addLayout(brow)
        layout.addWidget(display)

        # --- service ---
        service, sl = make_card("HUD service")

        srow = QHBoxLayout()
        srow.setSpacing(12)
        self.service_switch = ToggleSwitch()
        self.service_switch.clicked.connect(self.toggle_service)
        slabel = QLabel("Run the HUD")
        slabel.setObjectName("RowLabel")
        self.service_state = QLabel("checking…")
        self.service_state.setObjectName("Hint")
        srow.addWidget(self.service_switch)
        srow.addWidget(slabel)
        srow.addStretch(1)
        srow.addWidget(self.service_state)
        sl.addLayout(srow)

        arow = QHBoxLayout()
        arow.setSpacing(12)
        self.autostart_switch = ToggleSwitch()
        self.autostart_switch.clicked.connect(self.toggle_autostart)
        alabel = QLabel("Start at login")
        alabel.setObjectName("RowLabel")
        self.autostart_state = QLabel("")
        self.autostart_state.setObjectName("Hint")
        arow.addWidget(self.autostart_switch)
        arow.addWidget(alabel)
        arow.addStretch(1)
        arow.addWidget(self.autostart_state)
        sl.addLayout(arow)

        sl.addWidget(divider())
        rrow = QHBoxLayout()
        restart = QPushButton("Restart service")
        restart.setObjectName("Ghost")
        restart.clicked.connect(
            lambda: self.dispatch("Restart service", systemctl("restart", SERVICE_NAME))
        )
        switch_hint = QLabel("Switches follow systemd, not your last click.")
        switch_hint.setObjectName("Hint")
        rrow.addWidget(restart)
        rrow.addWidget(switch_hint)
        rrow.addStretch(1)
        sl.addLayout(rrow)
        layout.addWidget(service)

        layout.addStretch(1)
        return page

    def apply_pump(self) -> None:
        value = max(PUMP_MIN, min(self.pump_slider.value(), PUMP_MAX))
        self.dispatch(f"Pump {value}%", [LIQ, "--match", MATCH, "set", "pump", "speed", str(value)])

    def apply_fan(self) -> None:
        # Clamped again here, not just in the widget range, so a future change
        # to the slider cannot quietly reintroduce a stop-the-fans value.
        value = max(FAN_MIN_SAFE, min(self.fan_slider.value(), FAN_MAX))
        self.dispatch(f"Fan {value}%", [LIQ, "--match", MATCH, "set", "fan", "speed", str(value)])

    def apply_orientation(self) -> None:
        value = self.orientation_box.currentData()
        self.dispatch(
            f"Orientation {value}°",
            [LIQ, "--match", MATCH, "set", "lcd", "screen", "orientation", str(value)],
        )

    def apply_brightness(self) -> None:
        value = max(0, min(self.brightness_slider.value(), 100))
        self.dispatch(
            f"Brightness {value}%",
            [LIQ, "--match", MATCH, "set", "lcd", "screen", "brightness", str(value)],
        )

    def restore_native_display(self) -> None:
        def then_restore(_rc, _out, _err):
            self.dispatch(
                "Native display",
                [LIQ, "--match", MATCH, "set", "lcd", "screen", "liquid"],
                after=lambda *_: self.refresh_service_state(),
            )

        self.dispatch("Stop service", systemctl("stop", SERVICE_NAME), after=then_restore)

    # -- service state ---------------------------------------------------

    def refresh_service_state(self) -> None:
        active = run_sync(systemctl("is-active", SERVICE_NAME), timeout=6)[1].strip()
        enabled = run_sync(systemctl("is-enabled", SERVICE_NAME), timeout=6)[1].strip()

        self._service_active = active == "active"
        self._service_enabled = enabled == "enabled"

        self.service_switch.set_state(self._service_active)
        self.autostart_switch.set_state(self._service_enabled)
        self.service_state.setText(active or "unknown")
        self.autostart_state.setText(enabled or "unknown")

        if self._service_active:
            self.tile_service.set_value("Active", "", ACCENT)
        elif active == "failed":
            self.tile_service.set_value("Failed", "", DANGER)
        else:
            self.tile_service.set_value("Stopped", "", TEXT_FAINT)

    def toggle_service(self) -> None:
        action = "stop" if self._service_active else "start"
        self.service_switch.set_state(self._service_active, pending=True)
        self.dispatch(
            f"{action.capitalize()} service",
            systemctl(action, SERVICE_NAME),
            after=lambda *_: QTimer.singleShot(700, self.refresh_service_state),
        )

    def toggle_autostart(self) -> None:
        action = "disable" if self._service_enabled else "enable"
        self.autostart_switch.set_state(self._service_enabled, pending=True)
        self.dispatch(
            f"{action.capitalize()} autostart",
            systemctl(action, SERVICE_NAME),
            after=lambda *_: QTimer.singleShot(700, self.refresh_service_state),
        )

    # -- gallery ---------------------------------------------------------

    def _build_gallery_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("Root")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        intro = QLabel("Previews are rendered live by kraken_hud.py --preview.")
        intro.setObjectName("Hint")
        layout.addWidget(intro)

        layout.addWidget(self._build_colour_card())

        self.gallery_cards: dict[str, QLabel] = {}

        # -- your faces, above the built-ins -----------------------------
        yours = QHBoxLayout()
        yours.setSpacing(10)
        heading = QLabel("Your faces")
        heading.setStyleSheet(f"font-size: 15px; font-weight: 700; color: {TEXT};")
        yours.addWidget(heading)
        create = QPushButton("Create a face")
        create.clicked.connect(lambda: self.open_studio())
        yours.addWidget(create)
        yours.addStretch(1)
        layout.addLayout(yours)

        self.custom_host = QWidget()
        self.custom_grid = QGridLayout(self.custom_host)
        self.custom_grid.setContentsMargins(0, 0, 0, 0)
        self.custom_grid.setSpacing(16)
        self.custom_grid.setColumnStretch(2, 1)
        layout.addWidget(self.custom_host)

        self.custom_empty = QLabel(
            "No custom faces yet. Create a face opens a studio where you drag "
            "gauges, bars and readouts onto the display."
        )
        self.custom_empty.setObjectName("Hint")
        self.custom_empty.setWordWrap(True)
        layout.addWidget(self.custom_empty)

        builtin = QLabel("Built-in faces")
        builtin.setStyleSheet(f"font-size: 15px; font-weight: 700; color: {TEXT};")
        layout.addWidget(builtin)

        grid = QGridLayout()
        grid.setSpacing(16)
        # Keep cards packed to the left as presets are added, rather than
        # letting a single card float in the middle of the row.
        grid.setColumnStretch(2, 1)
        layout.addLayout(grid)

        for index, preset in enumerate(PRESETS):
            grid.addWidget(self._face_card(preset), index // 2, index % 2)

        layout.addStretch(1)
        self.refresh_custom_faces(previews=False)
        return page

    # -- custom faces ----------------------------------------------------

    def _face_card(self, preset: Preset, custom: bool = False) -> QFrame:
        """One gallery card. Custom faces get Edit/Delete instead of Refresh."""
        frame, cl = make_card()
        frame.setFixedWidth(320)

        image = QLabel("rendering…")
        image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # Square widget with a half-width radius, i.e. a disc: the well
        # around the preview is the bezel of a round panel, so a rounded
        # rectangle behind a circular frame just reads as a stray box.
        image.setFixedSize(PREVIEW_WELL, PREVIEW_WELL)
        image.setObjectName("Hint")
        image.setStyleSheet(
            f"background: #070c13; border: 1px solid {BORDER_SOFT};"
            f"border-radius: {PREVIEW_WELL // 2}px;"
        )
        cl.addWidget(image, alignment=Qt.AlignmentFlag.AlignHCenter)
        self.gallery_cards[preset.key] = image

        name = QLabel(preset.title)
        name.setStyleSheet(f"font-size: 15px; font-weight: 700; color: {TEXT};")
        cl.addWidget(name)
        desc = QLabel(preset.description)
        desc.setObjectName("Hint")
        desc.setWordWrap(True)
        cl.addWidget(desc)

        row = QHBoxLayout()
        row.setSpacing(10)
        if custom:
            face_name = preset.key.split(":", 1)[1]
            edit = QPushButton("Edit")
            edit.setObjectName("Ghost")
            edit.clicked.connect(lambda _, n=face_name: self.open_studio(n))
            row.addWidget(edit)
        else:
            regen = QPushButton("Refresh")
            regen.setObjectName("Ghost")
            regen.clicked.connect(lambda _, p=preset: self.generate_preview(p))
            row.addWidget(regen)

        apply_btn = QPushButton("Apply to LCD")
        apply_btn.clicked.connect(lambda _, p=preset: self.push_preset(p))
        row.addWidget(apply_btn)

        if custom:
            face_name = preset.key.split(":", 1)[1]
            delete = QPushButton("Delete")
            delete.setObjectName("Ghost")
            delete.clicked.connect(lambda _, n=face_name: self.delete_face(n))
            row.addWidget(delete)

        row.addStretch(1)
        cl.addLayout(row)
        return frame

    def custom_presets(self) -> list[Preset]:
        try:
            names = sorted(
                p.stem for p in FACES_DIR.glob("*.json") if not p.stem.startswith("_")
            )
        except OSError:
            names = []
        return [
            Preset(
                key=f"custom:{n}",
                title=n,
                description="Your face, built in the studio.",
                args=["--style", f"custom:{n}"],
            )
            for n in names
        ]

    def refresh_custom_faces(self, previews: bool = True) -> None:
        """Rebuild the custom-face cards.

        ``previews`` is off for the build-time call: that runs while the
        gallery page is being constructed, which is before __init__ has
        created the status bar dispatch() reports into. Generating previews
        there raised AttributeError and took the whole app down on startup
        for anyone who had saved a face. Those previews are generated when
        the Gallery tab is first opened instead.
        """
        while self.custom_grid.count():
            entry = self.custom_grid.takeAt(0)
            widget = entry.widget() if entry is not None else None
            if widget is not None:
                widget.deleteLater()

        presets = self.custom_presets()
        self.custom_empty.setVisible(not presets)
        # deleteLater() only detaches the cards when the event loop next
        # runs, and the host keeps its own layout margins regardless, so
        # hiding just the placeholder left a band of empty space under the
        # heading after the last face was deleted. Hide the container too.
        self.custom_host.setVisible(bool(presets))
        for index, preset in enumerate(presets):
            self.custom_grid.addWidget(self._face_card(preset, custom=True), index // 2, index % 2)
        if previews:
            for preset in presets:
                self.generate_preview(preset)

    def studio_vocab(self) -> dict:
        """Metric and component vocabulary, straight from the renderer."""
        rc, out, err = run_sync([PYTHON, str(HUD_SCRIPT), "--dump-vocab"], timeout=30)
        if rc != 0:
            QMessageBox.warning(self, APP_NAME, f"Could not read the HUD vocabulary:\n{err[:400]}")
            return {}
        try:
            return json.loads(out)
        except ValueError as exc:
            QMessageBox.warning(self, APP_NAME, f"Bad vocabulary from kraken_hud.py: {exc}")
            return {}

    def open_studio(self, name: str = "") -> None:
        vocab = self.studio_vocab()
        if not vocab:
            return
        spec = None
        if name:
            try:
                spec = json.loads((FACES_DIR / f"{name}.json").read_text())
            except (OSError, ValueError) as exc:
                QMessageBox.warning(self, APP_NAME, f"Could not open {name}: {exc}")
                return
        studio = FaceStudio(self, vocab, name=name, spec=spec)
        if studio.exec() and studio.saved_name:
            self.refresh_custom_faces()
            self.status.showMessage(f"Saved face “{studio.saved_name}”", 5000)

    def delete_face(self, name: str) -> None:
        confirm = QMessageBox.question(
            self,
            APP_NAME,
            f"Delete the face “{name}”?\n\nIf the HUD is showing it, it falls back to "
            "the default face.",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            (FACES_DIR / f"{name}.json").unlink(missing_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, APP_NAME, f"Could not delete {name}: {exc}")
            return
        # Nudge the HUD so it re-resolves the face if it was showing this one.
        self.dispatch("Refresh face", [PYTHON, str(HUD_SCRIPT), "--transition", load_transition()])
        self.refresh_custom_faces()

    # -- palette ---------------------------------------------------------

    def _build_colour_card(self) -> QWidget:
        frame, cl = make_card()

        title = QLabel("Colours")
        title.setStyleSheet(f"font-size: 15px; font-weight: 700; color: {TEXT};")
        cl.addWidget(title)

        hint = QLabel(
            "Every face is drawn from these three. Text shades are derived "
            "automatically so the readouts stay legible whatever you pick."
        )
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        cl.addWidget(hint)

        self.colours = load_colours()
        self.colour_swatches: dict[str, QPushButton] = {}

        row = QHBoxLayout()
        row.setSpacing(10)
        for role in ("primary", "secondary", "tertiary"):
            swatch = QPushButton(role.capitalize())
            swatch.setFixedHeight(38)
            swatch.clicked.connect(lambda _, r=role: self.pick_colour(r))
            self.colour_swatches[role] = swatch
            row.addWidget(swatch)
        cl.addLayout(row)
        self._refresh_swatches()

        actions = QHBoxLayout()
        actions.setSpacing(10)
        reset = QPushButton("Reset to default")
        reset.setObjectName("Ghost")
        reset.clicked.connect(self.reset_colours)
        actions.addWidget(reset)
        actions.addStretch(1)
        cl.addLayout(actions)

        divider = QFrame()
        divider.setObjectName("Divider")
        divider.setFixedHeight(1)
        cl.addWidget(divider)

        switch_label = QLabel("Face switching")
        switch_label.setStyleSheet(f"font-size: 15px; font-weight: 700; color: {TEXT};")
        cl.addWidget(switch_label)

        switch_hint = QLabel(
            "The LCD accepts about 1.8 frames a second, so a transition costs "
            "roughly 0.56s per frame it takes."
        )
        switch_hint.setObjectName("Hint")
        switch_hint.setWordWrap(True)
        cl.addWidget(switch_hint)

        self.transition_box = QComboBox()
        for label, value in TRANSITIONS:
            self.transition_box.addItem(label, value)
        current = load_transition()
        index = self.transition_box.findData(current)
        if index >= 0:
            self.transition_box.setCurrentIndex(index)
        self.transition_box.currentIndexChanged.connect(self.apply_transition)
        cl.addWidget(self.transition_box)

        return frame

    def apply_transition(self) -> None:
        value = self.transition_box.currentData()
        self.dispatch(
            f"Transition {value}",
            [PYTHON, str(HUD_SCRIPT), "--transition", value],
            timeout=30.0,
        )

    def _refresh_swatches(self) -> None:
        """Paint each button in the colour it selects, with readable text.

        The label has to flip between black and white depending on how light
        the chosen colour is, or picking a dark primary leaves the word
        'Primary' invisible on its own swatch.
        """
        for role, button in self.colour_swatches.items():
            colour = self.colours[role]
            ink = "#000000" if QColor(colour).lightnessF() > 0.55 else "#ffffff"
            button.setStyleSheet(
                f"background: {colour}; color: {ink}; border: none;"
                f"border-radius: 10px; font-weight: 700;"
            )

    def pick_colour(self, role: str) -> None:
        chosen = QColorDialog.getColor(
            QColor(self.colours[role]), self, f"Choose {role} colour"
        )
        if not chosen.isValid():
            return  # dialog cancelled -- leave the palette untouched
        self.colours[role] = chosen.name().lower()
        self._refresh_swatches()
        self._save_colours()

    def _save_colours(self) -> None:
        self.dispatch(
            "Save colours",
            [
                PYTHON,
                str(HUD_SCRIPT),
                "--save-colours",
                "--primary",
                self.colours["primary"],
                "--secondary",
                self.colours["secondary"],
                "--tertiary",
                self.colours["tertiary"],
            ],
            after=lambda rc, _o, _e: self._regenerate_all_previews() if rc == 0 else None,
        )

    def reset_colours(self) -> None:
        self.colours = dict(DEFAULT_COLOURS)
        self._refresh_swatches()
        self.dispatch(
            "Reset colours",
            [PYTHON, str(HUD_SCRIPT), "--reset-colours"],
            after=lambda rc, _o, _e: self._regenerate_all_previews() if rc == 0 else None,
        )

    def _regenerate_all_previews(self) -> None:
        """Re-render every gallery card so the cards show the new colours.

        The running HUD service picks the palette up on its own (it watches
        the file), so this only refreshes what is on screen here.
        """
        for preset in [*PRESETS, *self.custom_presets()]:
            self.generate_preview(preset)

    def generate_preview(self, preset: Preset) -> None:
        # A custom face's key is "custom:<name>" and the name is user-typed,
        # so it cannot go into a path unescaped.
        slug = re.sub(r"[^A-Za-z0-9_-]", "_", preset.key)
        out = Path(f"/tmp/kraken_preview_{slug}.png")
        label = self.gallery_cards[preset.key]
        label.setText("rendering…")

        def done(rc, _stdout, _stderr):
            if rc == 0 and out.exists():
                pix = QPixmap(str(out))
                if not pix.isNull():
                    label.setPixmap(circular(pix, PREVIEW_DISC))
                    return
            label.setText("preview failed")

        self.dispatch(
            f"Preview {preset.title}",
            [PYTHON, str(HUD_SCRIPT), "--preview", "--output", str(out), *preset.args],
            after=done,
        )

    def push_preset(self, preset: Preset) -> None:
        """Switch the running HUD to this face.

        This writes the face config and lets the service pick it up and
        crossfade, rather than pushing a frame from here: with the service
        running, a one-shot push was overwritten by its next frame within a
        second, so applying a face appeared to do nothing but flicker.
        """
        style = preset.args[preset.args.index("--style") + 1]

        def then_push(rc, _stdout, _stderr):
            # With the service stopped nothing is watching the config, so the
            # face would silently not appear. Push it directly instead -- the
            # one case where a one-shot push is the right thing.
            if rc == 0 and not self._service_active:
                self.dispatch(
                    f"Push {preset.title}",
                    [PYTHON, str(HUD_SCRIPT), "--once", *preset.args],
                    timeout=60.0,
                )

        self.dispatch(
            f"Apply {preset.title}",
            [PYTHON, str(HUD_SCRIPT), "--set-face", style],
            timeout=30.0,
            after=then_push,
        )

    # -- script editor ---------------------------------------------------

    def _build_editor_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("Root")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        self.editor_targets = {
            "HUD script — kraken_hud.py": HUD_SCRIPT,
            "systemd unit — liquidctl.service": SERVICE_UNIT,
            f"{APP_NAME} itself — kraken_controller.py": CONTROLLER_SCRIPT,
        }

        row = QHBoxLayout()
        row.setSpacing(10)
        self.editor_choice = QComboBox()
        self.editor_choice.addItems(self.editor_targets.keys())
        self.editor_choice.currentTextChanged.connect(self.load_editor_file)
        reload_btn = QPushButton("Reload")
        reload_btn.setObjectName("Ghost")
        reload_btn.clicked.connect(lambda: self.load_editor_file(self.editor_choice.currentText()))
        save_btn = QPushButton("Save")
        save_btn.clicked.connect(self.save_editor_file)
        row.addWidget(self.editor_choice, 1)
        row.addWidget(reload_btn)
        row.addWidget(save_btn)
        layout.addLayout(row)

        self.editor = QPlainTextEdit()
        self.editor.setFont(QFont("monospace", 10))
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self.editor, 1)

        self.editor_note = QLabel("")
        self.editor_note.setObjectName("Hint")
        self.editor_note.setWordWrap(True)
        layout.addWidget(self.editor_note)

        self.load_editor_file(self.editor_choice.currentText())
        return page

    def load_editor_file(self, key: str) -> None:
        path = self.editor_targets.get(key)
        if path is None:
            return
        try:
            self.editor.setPlainText(path.read_text())
            note = str(path)
        except OSError as exc:
            self.editor.setPlainText("")
            self.editor_note.setText(f"could not read {path}: {exc}")
            return

        if path == CONTROLLER_SCRIPT:
            note += "  ·  saving syntax-checks, writes a .bak, then offers to relaunch."
        elif path == SERVICE_UNIT:
            note += "  ·  saving runs daemon-reload, then restarts if active."
        else:
            note += "  ·  saving restarts the service if it is running."
        self.editor_note.setText(note)

    def save_editor_file(self) -> None:
        key = self.editor_choice.currentText()
        path = self.editor_targets[key]
        text = self.editor.toPlainText()

        if path.suffix == ".py":
            try:
                compile(text, str(path), "exec")
            except SyntaxError as exc:
                QMessageBox.critical(
                    self,
                    "Syntax error",
                    f"Not saved — {path.name} line {exc.lineno}: {exc.msg}",
                )
                return

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
            path.write_text(text)
        except OSError as exc:
            QMessageBox.critical(self, "Save failed", str(exc))
            return

        self.status.showMessage(f"Saved {path.name}", 5000)

        if path == SERVICE_UNIT:
            self.dispatch(
                "daemon-reload",
                systemctl("daemon-reload"),
                after=lambda *_: (
                    self.dispatch("Restart service", systemctl("restart", SERVICE_NAME))
                    if self._service_active
                    else None
                ),
            )
        elif path == HUD_SCRIPT:
            if self._service_active:
                self.dispatch("Restart service", systemctl("restart", SERVICE_NAME))
        elif path == CONTROLLER_SCRIPT:
            answer = QMessageBox.question(
                self,
                f"Relaunch {APP_NAME}",
                f"{APP_NAME}'s own source was changed. Relaunch now to apply it?",
            )
            if answer == QMessageBox.StandardButton.Yes:
                os.execv(PYTHON, [PYTHON, str(CONTROLLER_SCRIPT)])

    # -- diagnostics -----------------------------------------------------

    def _build_diagnostics_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("Root")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        row = QHBoxLayout()
        row.setSpacing(10)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh_diagnostics)
        hint = QLabel("liquidctl status, unit state and recent journal output.")
        hint.setObjectName("Hint")
        row.addWidget(refresh)
        row.addWidget(hint)
        row.addStretch(1)
        layout.addLayout(row)

        self.diagnostics = QPlainTextEdit()
        self.diagnostics.setReadOnly(True)
        self.diagnostics.setFont(QFont("monospace", 10))
        self.diagnostics.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self.diagnostics, 1)
        return page

    def refresh_diagnostics(self) -> None:
        self.diagnostics.setPlainText("collecting…")
        sections = [
            ("liquidctl status", [LIQ, "--match", MATCH, "status"]),
            ("systemctl --user status", systemctl("status", SERVICE_NAME, "--no-pager")),
            (
                "journalctl (recent)",
                ["journalctl", "--user", "-u", SERVICE_NAME, "-n", "40", "--no-pager"],
            ),
        ]
        collected: dict[str, str] = {}

        def handler(name: str, rc: int, out: str, err: str) -> None:
            collected[name] = (out or "").rstrip() or (err or "").rstrip() or f"(exit {rc})"
            if len(collected) == len(sections):
                self.diagnostics.setPlainText(
                    "\n\n".join(
                        f"$ {title}\n{'─' * 62}\n{collected.get(title, '')}"
                        for title, _ in sections
                    )
                )

        for name, cmd in sections:
            task = Task(name, cmd, 25.0)
            task.signals.done.connect(
                lambda token, rc, out, err: handler(str(token), rc, out, err)
            )
            self.pool.start(task)

    def _on_nav_changed(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        label = self.nav.item(index).text()
        if label == "Diagnostics":
            self.refresh_diagnostics()
        elif label == "Gallery":
            for preset in [*PRESETS, *self.custom_presets()]:
                if preset.key not in self.gallery_cards:
                    continue
                # QLabel.pixmap() returns an empty QPixmap rather than None, and
                # a QPixmap is always truthy -- so `if not pixmap()` never fires
                # and the preview would never be generated. Test isNull().
                current = self.gallery_cards[preset.key].pixmap()
                if current is None or current.isNull():
                    self.generate_preview(preset)


def main() -> int:
    if "--check-contrast" in sys.argv:
        ok = True
        for name, ratio, passed in palette_report():
            print(f"{name:22s} {ratio:5.2f}:1  {'PASS' if passed else 'FAIL'}")
            ok = ok and passed
        return 0 if ok else 1

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setDesktopFileName("coldloop")
    app.setStyleSheet(STYLESHEET)
    window = Controller()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
