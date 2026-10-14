# SPDX-License-Identifier: GPL-2.0-or-later
"""ztos: write ztOS apps in Python and Qt.

The pieces look and move as the phone's own apps do (ZrnMetroCore): black,
white text in Segoe/Selawik, the accent colour chosen in Settings, titles in
the corner, pages that swing in like a turnstile, lists that coast under a
finger. Plain Qt widgets (and pages drawn in Qt Designer) fit in too: the
theme styles them to match.

    import ztos

    app = ztos.App()                      # its name and id come from ztos.toml
    home = ztos.Page("hello")
    home.text("Something to read.")
    home.toggle("Lights", on=True, on_change=lambda on: print(on))
    home.entry("about", "what this is", lambda: app.push(about))
    about = ztos.Page("about")
    app.run(home)

Pages are 360 points wide on the phone, as the shell's own are.

The shell:  ztos.haptic("tap")  ztos.notify("Title", "text")  ztos.accent()
Your files: ztos.resource("data.json") -> bytes, ztos.load_ui("form.ui")
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
import zipfile
from typing import Callable, Iterable, Optional


from PySide6.QtCore import (QByteArray, QEasingCurve, QEvent, QObject, QPoint, QPointF, QRect, QRectF, Qt, QTimer,
                            QVariantAnimation, SIGNAL, Signal)
from PySide6.QtGui import (QColor, QFont, QFontMetricsF, QGuiApplication, QPainter, QPainterPath, QPen,
                           QImage, QPixmap, QRegion, QTransform)
from PySide6.QtWidgets import (QAbstractButton, QAbstractSlider, QAbstractSpinBox, QPlainTextEdit, QTextEdit,
                               QApplication, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QSizePolicy, QStackedLayout, QVBoxLayout, QWidget)

__all__ = [
    "App", "Page", "Pivot", "Entry", "Toggle", "Slider", "TextBox", "RoundButton", "AppBar",
    "menu", "haptic", "notify", "osk", "accent", "resource", "load_ui", "on_device", "manifest",
    "MARGIN", "FONT_LIGHT", "FONT_SEMILIGHT", "FONT_REGULAR", "FONT_SEMIBOLD",
    "ProgressBar", "LoadingDots", "Dialog", "message", "performance", "battery", "launch", "store", "Store",
]

MARGIN = 16
FONT_LIGHT = ["Segoe WP Light", "Selawik Light", "Selawik", "Open Sans Light", "sans-serif"]
FONT_SEMILIGHT = ["Segoe WP Semilight", "Selawik Semilight", "Selawik", "Open Sans", "sans-serif"]
FONT_REGULAR = ["Segoe WP", "Selawik", "Open Sans", "sans-serif"]
FONT_SEMIBOLD = ["Segoe WP Semibold", "Selawik Semibold", "Selawik", "Open Sans Semibold", "sans-serif"]
GREY = QColor(153, 153, 153)        # details under titles
DIM = QColor(97, 97, 97)            # the next pivot titles

_ACCENTS = {
    "lime": 0xa4c400, "green": 0x60a917, "emerald": 0x008a00, "teal": 0x00aba9, "cyan": 0x1ba1e2,
    "cobalt": 0x0050ef, "indigo": 0x6a00ff, "violet": 0xaa00ff, "pink": 0xf472d0, "magenta": 0xd80073,
    "crimson": 0xa20025, "red": 0xe51400, "orange": 0xfa6800, "amber": 0xf0a30a, "yellow": 0xe3c800,
    "brown": 0x825a2c, "olive": 0x6d8764, "steel": 0x647687, "mauve": 0x76608a, "taupe": 0x87794e,
}


# ---- the app's own description and files ------------------------------------

def manifest() -> dict:
    """What ztos.toml says about this app (id, name, caps, version, ...)."""
    try:
        return json.loads(os.environ.get("ZTOS_MANIFEST", "{}"))
    except ValueError:
        return {}


_zip: Optional[zipfile.ZipFile] = None


def resource(name: str) -> bytes:
    """A file of the app's, by its path in the project (e.g. "assets/logo.svg")."""
    global _zip
    root = os.environ.get("ZTOS_APP_ROOT") or os.getcwd()
    if "!" in root:     # inside the built binary: ARCHIVE!folder
        archive, folder = root.split("!", 1)
        if _zip is None:
            _zip = zipfile.ZipFile(archive)
        return _zip.read(f"{folder}/{name}" if folder else name)
    with open(os.path.join(root, name), "rb") as f:
        return f.read()


def load_ui(name: str, parent: Optional[QWidget] = None) -> QWidget:
    """A page drawn in Qt Designer, as a widget; find what is on it with
    widget.findChild(QPushButton, "okButton"), or as attributes (widget.ui.okButton).

    The build turns each .ui into Python (ui_NAME.py), so the phone needs
    nothing of Designer's; while trying the app on the laptop the .ui itself is read."""
    stem = os.path.splitext(os.path.basename(name))[0]
    widget = QWidget(parent)
    try:
        module = __import__(f"ui_{stem}")
        form = next(getattr(module, n) for n in dir(module) if n.startswith("Ui_"))()
        form.setupUi(widget)
        widget.ui = form
        return widget
    except ImportError:
        pass
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtUiTools import QUiLoader
    buffer = QBuffer()
    buffer.setData(QByteArray(resource(name)))
    buffer.open(QIODevice.ReadOnly)
    loaded = QUiLoader().load(buffer, parent)
    if loaded is None:
        raise RuntimeError(f"cannot load {name}")

    class _Names:
        def __getattr__(self, attr):
            found = loaded.findChild(QObject, attr)
            if found is None:
                raise AttributeError(attr)
            return found
    loaded.ui = _Names()
    return loaded


# ---- the shell ----------------------------------------------------------------

def _ctl(line: str) -> bool:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime:
        return False
    try:
        fd = os.open(os.path.join(runtime, "ztmetro.ctl"), os.O_WRONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return False
    try:
        os.write(fd, (line + "\n").encode())
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def on_device() -> bool:
    """True inside the ztMETRO shell; False while trying the app on the laptop."""
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    return bool(runtime) and os.path.exists(os.path.join(runtime, "ztmetro.ctl"))


def haptic(kind: str = "tap") -> None:
    """A buzz, at the strength set for its kind in Settings > haptics:
    "tick" (a key), "tap", "click" (a switch), "heavy"."""
    _ctl(f"haptic {kind}")


def notify(title: str, text: str = "") -> None:
    """A notification in the action centre, under this app's name."""
    clean = lambda s: str(s).replace("|", "/").replace("\n", " ")
    _ctl(f"notify {clean(manifest().get('name', 'App'))}|{clean(title)}|{clean(text)}")


def osk(show: bool, box: Optional[QWidget] = None) -> None:
    """Bring up (or put away) the on-screen keyboard. It lies over the app; with
    `box` (what is typed into), the shell lifts the app just enough for that
    box to stay above it: nothing is resized or laid out again."""
    if show and box is not None and box.window() is not None:
        bottom = box.mapTo(box.window(), QPoint(0, box.height())).y()
        _ctl(f"osk show {bottom}")
    else:
        _ctl("osk show" if show else "osk hide")


def accent() -> QColor:
    """The accent colour chosen in Settings."""
    color = _ACCENTS["cobalt"]
    name = os.environ.get("ZTOS_ACCENT")
    if not name:
        try:
            with open(os.path.expanduser("~/.config/ztmetro/config")) as f:
                for line in f:
                    key, _, value = line.partition("=")
                    if key.strip() == "accent":
                        name = value.strip()
        except OSError:
            pass
    if name in _ACCENTS:
        color = _ACCENTS[name]
    elif name and name.startswith("#"):
        return QColor(name)
    return QColor(color >> 16, (color >> 8) & 0xff, color & 0xff)


# ---- drawing helpers -------------------------------------------------------------

def _font(families: list, size: float) -> QFont:
    font = QFont()
    font.setFamilies(families)
    font.setPixelSize(max(1, round(size)))
    font.setHintingPreference(QFont.PreferNoHinting)
    return font


def _label(text: str, families: list, size: float, color: Optional[QColor] = None,
           wrap: bool = True) -> QLabel:
    label = QLabel(text)
    label.setFont(_font(families, size))
    label.setWordWrap(wrap)
    label.setTextInteractionFlags(Qt.NoTextInteraction)
    if color is not None:
        label.setStyleSheet(f"color: {color.name()};")
    return label


def _now() -> float:
    """Milliseconds on the clock the shell uses too (CLOCK_MONOTONIC)."""
    return time.monotonic() * 1000.0


def _clamp01(v: float) -> float:
    return 0.0 if v < 0 else 1.0 if v > 1 else v


def _ancestor(widget: Optional[QWidget], kind) -> Optional[QWidget]:
    while widget is not None and not isinstance(widget, kind):
        widget = widget.parentWidget()
    return widget


class _Ticker(QObject):
    """Calls `step(now_ms)` about every frame until it returns False."""

    def __init__(self, parent: QObject, step: Callable[[float], bool]):
        super().__init__(parent)
        self._step = step
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.setInterval(16)       # a display frame: the motions are by the clock, not by ticks
        self._timer.timeout.connect(self._tick)

    def start(self):
        if not self._timer.isActive():
            self._timer.start()

    def stop(self):
        self._timer.stop()

    def active(self) -> bool:
        return self._timer.isActive()

    def _tick(self):
        if not self._step(_now()):
            self._timer.stop()


# ---- the toolkit's motion, the same numbers (ztmetro/core/zrnmetrocore.c) -------

SLOP = 8                    # points a finger moves before it is a drag, not a tap
HOLD_MS = 500
TILT_DEG, TILT_LIFT, PRESS_SINK = 14.0, 40.0, 0.045
TILT_IN_MS, TILT_OUT_MS = 70, 160
DEPTH_FOV_DEG = 34.0        # the shell's field of view
ROW_OUT_MS, ROW_IN_MS, ROW_STAGGER_MS, GAP_MS = 110, 180, 33, 120
TURN_OUT_DEG = TURN_IN_DEG = 80.0
ENTER_STAGGER_MS, ENTER_ROW_MS, ENTER_TRAVEL = 28, 190, 50.0
ENTER_TITLE_TRAVEL, ENTER_TITLE_MS, ENTER_HOLD_MS = 35.0, 200, 60
PIVOT_FLICK = 0.3           # points per ms that throw the tabs on to the next
PIVOT_SPRING_W = 0.018      # per ms: in place in about a quarter second
ANIM_DPR = 2.0              # pixels per point while things move: a quarter of the work.
                            # Raised per-p picture where it is cheap: the lean of a row
                            # (LEAN_DPR) is drawn native, or its edges look jagged.
LEAN_DPR = 4.0              # pixels per point for a pressable's flat picture
HEADER_H = 108


def _eye(screen_h: float) -> float:
    return (screen_h / 2.0) / math.tan(DEPTH_FOV_DEG * math.pi / 360.0)


def _turn(degrees: float, w: float, screen_h: float, y0: float, y1: float) -> QTransform:
    """A band of the page turned round its left edge and seen from in front of
    the screen's middle (across) and its own middle (up and down): the
    toolkit's turned_strip, as one projective transform."""
    a = math.radians(degrees)
    d = _eye(screen_h)
    cx, cy = w / 2.0, (y0 + y1) / 2.0
    sa, ca = math.sin(a), math.cos(a)
    return QTransform((cx * sa + d * ca) / d, cy * sa / d, sa / d, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)


_drawing_pictures = 0       # while the SDK draws pictures, the paints are its own


def _snapshot(widget: QWidget, size=None) -> QPixmap:
    """The widget drawn into a picture at the moving resolution."""
    global _drawing_pictures
    size = size or widget.size()
    pixmap = QPixmap(max(1, round(size.width() * ANIM_DPR)), max(1, round(size.height() * ANIM_DPR)))
    pixmap.setDevicePixelRatio(ANIM_DPR)
    pixmap.fill(QColor("black"))
    _drawing_pictures += 1
    try:
        widget.render(pixmap, QPoint(0, 0), QRegion(), QWidget.RenderFlag.DrawChildren |
                      QWidget.RenderFlag.DrawWindowBackground)
    finally:
        _drawing_pictures -= 1
    return pixmap


def _band(p: QPainter, pixmap: QPixmap, x0: float, y0: float, x1: float, y1: float, dx: float = 0.0):
    """The part x0..x1, y0..y1 (points) of `pixmap`, moved across by dx."""
    if x1 <= x0 or y1 <= y0:
        return
    r = pixmap.devicePixelRatio()
    p.drawPixmap(QRectF(x0 + dx, y0, x1 - x0, y1 - y0), pixmap, QRectF(x0 * r, y0 * r, (x1 - x0) * r, (y1 - y0) * r))


class _Moving(QWidget):
    """A widget that draws only while things move: each frame is put together at
    ANIM_DPR (a quarter of the pixels at the phone's 4) and laid on the screen
    in one scaled copy, as the toolkit does it."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self._frame: Optional[QImage] = None

    def compose(self, p: QPainter):
        raise NotImplementedError

    def paintEvent(self, event):
        w, h = self.width(), self.height()
        size = (max(1, round(w * ANIM_DPR)), max(1, round(h * ANIM_DPR)))
        if self._frame is None or (self._frame.width(), self._frame.height()) != size:
            self._frame = QImage(size[0], size[1], QImage.Format_RGB32)
            self._frame.setDevicePixelRatio(ANIM_DPR)
        self._frame.fill(QColor("black"))
        p = QPainter(self._frame)
        self.compose(p)
        p.end()
        screen = QPainter(self)
        screen.setRenderHint(QPainter.SmoothPixmapTransform, True)
        screen.drawImage(QRectF(0, 0, w, h), self._frame)


_settle_until = 0.0


def _settle(ms: float = 250.0):
    """Things were just shown, hidden or uncovered by the SDK itself: the
    repaints that follow (Qt sends them a little later) are not counted as
    changes. A change that happened in that moment is not lost: once things are
    still, each page on the screen checks its picture against the widgets."""
    global _settle_until
    _settle_until = max(_settle_until, _now() + ms)
    for w in QApplication.topLevelWidgets():
        for page in w.findChildren(Page):
            if page.isVisible():
                page._check.start()


def _animating() -> bool:
    """A page turning, tabs sliding, a list moving: then work that can wait,
    waits (animations come first; this thread draws them)."""
    app = App.current
    if app is None:
        return False
    return app._turn is not None or (app.gl is not None and app.gl.running)


def _layer_of(widget: QWidget):
    window = widget.window()
    app = getattr(window, "app", None)
    layer = getattr(app, "gl", None)
    return layer if layer is not None and layer.initialized and not layer.broken else None


# ---- the finger ----------------------------------------------------------------------

def _drag_axis(dx: float, dy: float) -> int:
    """Across or along, as the toolkit decides it: a thumb's stroke is an arc that
    may start at a slant, so only a nearly flat stroke (within 20 degrees, over
    18 points) is across; anything else is along, decided after 12 points.
    0: not yet, 1: across, 2: along."""
    length = math.hypot(dx, dy)
    if length < 12:
        return 0
    if math.degrees(math.atan2(abs(dy), abs(dx))) > 20:
        return 2
    return 0 if length < 18 else 1


_WHOLE = QRectF(0, 0, 1, 1)

_WATCHED = frozenset((QEvent.Paint, QEvent.KeyPress, QEvent.MouseButtonPress, QEvent.MouseButtonDblClick,
                      QEvent.MouseMove, QEvent.MouseButtonRelease))


class _Finger(QObject):
    """Every touch of the app goes past here first. A finger that stays put
    is the widget's (a tap, a hold); one that moves is a drag, and belongs to
    the list under it (up and down) or the pivot (across). As soon as it is a
    drag, whatever it was pressing lets go, and sees no more of it."""

    def __init__(self, app: "App"):
        super().__init__(app.qt)
        self.app = app
        self.active = False
        self._seen = None
        self._answer = False
        self.held_focus = None

    def eventFilter(self, obj, event):
        kind = event.type()
        if kind not in _WATCHED:
            return False
        if kind == QEvent.KeyPress:
            # back (the phone's back key; Escape on a keyboard) goes up a page
            if event.key() in (Qt.Key_Back, Qt.Key_Escape) and obj is QGuiApplication.focusWindow():
                self.app.back()
                return True
            return False
        if kind == QEvent.Paint:
            if self.app.gl is not None and isinstance(obj, QWidget):
                if isinstance(obj, _Pressable) and obj._leaning():
                    return False        # the press tilt is motion, not a change
                if getattr(obj, "_motion", False):
                    return False        # dots, gliding bars: they report when they settle
                page = _ancestor(obj, Page)
                # (the list's own background repaints whenever something over it
                # comes or goes; a real change repaints a row as well)
                if page is not None and obj is not page.viewport and obj is not page.body \
                        and page.body.isAncestorOf(obj):
                    page._changed()
            return False
        if kind not in (QEvent.MouseButtonPress, QEvent.MouseButtonDblClick, QEvent.MouseMove,
                        QEvent.MouseButtonRelease) or not isinstance(obj, QWidget):
            return False
        at = event.globalPosition()
        # an event a widget passes up to its parent comes past again: same answer
        key = (kind, event.timestamp(), at.x(), at.y())
        if key == self._seen:
            return self._answer
        self._seen = key
        if kind in (QEvent.MouseButtonPress, QEvent.MouseButtonDblClick):
            self._answer = self._down(obj, event, at) if event.button() == Qt.LeftButton else False
        elif not self.active:
            self._answer = False
        elif kind == QEvent.MouseMove:
            self._answer = self._move(event, at)
        else:
            self._answer = self._up(event, at)
        return self._answer

    def _down(self, obj, event, at) -> bool:
        if self.app.turning():
            return True             # nothing can be tapped while the pages turn
        if obj.window() is not self.app.window:
            return False
        self.target = obj
        self.page = _ancestor(obj, Page)
        self.pivot = _ancestor(obj, Pivot)
        if self.pivot is not None and self.pivot._outer() is not self.pivot:
            self.pivot = self.pivot._outer()
        self.down = QPointF(at)
        self.last = QPointF(at)
        self.last_ms = event.timestamp()
        self.vx = self.vy = 0.0
        self.moved = False
        self.axis = 0
        self.active = True
        self.passive = isinstance(obj, (Slider, QAbstractSlider)) or (self.page is None and self.pivot is None)
        self.app.last_press_y = self.app.window.mapFromGlobal(at).y()
        self.held_focus = None
        typing = _ancestor(obj, (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox))
        if typing is not None and not typing.hasFocus() and typing.focusPolicy() & Qt.ClickFocus:
            # a touch on a text box gives it the keyboard only once it is a tap
            self.held_focus = (typing, typing.focusPolicy())
            typing.setFocusPolicy(Qt.TabFocus)
        if self.passive:
            return False
        if self.pivot is not None and self.pivot.sliding():
            # the tabs moving: a finger takes hold of them where they are
            self.pivot.catch()
            self.moved, self.axis = True, 1
            self.grab = self.pivot._x
            return True
        if self.page is not None and self.page._flinging():
            # a finger on a moving list stops it, no more
            self.page._stop()
            self.moved = True
            self.axis = 2
            self.down_scroll = self.page._raw_scroll()
            return True
        return False

    def _move(self, event, at) -> bool:
        ms = event.timestamp()
        if ms != self.last_ms:
            dt = float(ms - self.last_ms)
            self.vy = self.vy * 0.4 + (-(at.y() - self.last.y()) / dt) * 0.6
            self.vx = self.vx * 0.4 + ((at.x() - self.last.x()) / dt) * 0.6
            self.last, self.last_ms = QPointF(at), ms
        if self.passive:
            return False
        dx, dy = at.x() - self.down.x(), at.y() - self.down.y()
        if not self.moved and (abs(dx) > SLOP or abs(dy) > SLOP):
            self.moved = True
            self._let_go()
        if self.moved and self.axis == 0:
            self.axis = _drag_axis(dx, dy) if self.pivot is not None else 2
            if self.axis == 2:
                # the list starts from here, not from where the finger was
                self.down = QPointF(at)
                self.down_scroll = self.page._raw_scroll() if self.page is not None else 0.0
                dy = 0.0
            elif self.axis == 1:
                self.down = QPointF(at)     # from here: nothing jumps as it starts
                self.grab = 0.0
                dx = 0.0
                self.pivot.begin_drag()
        if self.axis == 1:
            self.pivot.drag(self.grab + dx)
        elif self.axis == 2 and self.page is not None:
            self.page._drag_to(self.down_scroll - dy)
        return self.moved

    def _up(self, event, at) -> bool:
        self.active = False
        if self.held_focus is not None:
            typing, policy = self.held_focus
            self.held_focus = None
            typing.setFocusPolicy(policy)
            if not self.moved and typing.rect().contains(typing.mapFromGlobal(at).toPoint()):
                typing.setFocus(Qt.MouseFocusReason)
        if self.passive or not self.moved:
            return False
        recent = event.timestamp() - self.last_ms <= 80
        if self.axis == 1:
            self.pivot.release(self.vx if recent else 0.0)
        elif self.axis == 2 and self.page is not None:
            self.page._fling(self.vy if recent else 0.0)
        return True

    def _let_go(self):
        """It is a drag: whatever the finger was pressing is let go at once."""
        w = self.target
        while w is not None:
            if isinstance(w, _Pressable):
                w.cancel()
            elif isinstance(w, QAbstractButton) and w.isDown():
                w.setDown(False)
            if w is self.page or w is self.pivot:
                break
            w = w.parentWidget()


# ---- pressed things lean toward the finger ---------------------------------------------

class _Pressable(QWidget):
    """Something a finger taps, or rests on. While pressed it leans toward the
    finger as a tile on Start does: it turns about its own middle so the
    touched point sinks and the far side rises, and drops back a little.
    Tapped and held are signals."""
    tapped = Signal()
    held = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._down: Optional[QPointF] = None
        self._was_held = False
        self._p_from = self._p_to = 0.0
        self._p_start = 0.0
        self._hold = QTimer(self, singleShot=True, interval=HOLD_MS)
        self._hold.timeout.connect(self._on_hold)
        self._ticker = _Ticker(self, self._press_step)

    # how far pressed, 0..1, eased like the toolkit's
    def _amount(self) -> float:
        ms = TILT_IN_MS if self._p_to >= self._p_from else TILT_OUT_MS
        t = _clamp01((_now() - self._p_start) / ms)
        t = 1 - (1 - t) * (1 - t)
        return self._p_from + (self._p_to - self._p_from) * t

    def _press_set(self, to: float, at_once: bool = False):
        self._p_from = to if at_once else self._amount()
        self._p_to = to
        self._p_start = _now()
        self._ticker.start()
        self.update()

    def content_changed(self):
        """What this shows changed (not just how it leans): the page's pictures
        are made again. Call it from a subclass that changes its own look."""
        self._flat = None
        page = _ancestor(self.parentWidget(), Page)
        if page is not None:
            page._changed(explicit=True)

    def _leaning(self) -> bool:
        """Pressed, or easing back from it (or just done): its paints then are motion."""
        return self._p_to > 0 or _now() - self._p_start < TILT_OUT_MS + 60

    def _press_step(self, now) -> bool:
        self.update()
        return now - self._p_start < max(TILT_IN_MS, TILT_OUT_MS)

    def _on_hold(self):
        if self._down is not None and self.receivers(SIGNAL("held()")) > 0:
            self._was_held = True
            haptic("tap")
            self._press_set(0)
            self._down = None
            self.held.emit()

    def cancel(self):
        """The finger went on to scroll: let go now, with no easing."""
        self._down = None
        self._hold.stop()
        self._press_set(0, at_once=True)

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        self._down = event.position()
        self._flat = None
        self._cache = None
        self._fx = _clamp01(self._down.x() / max(1, self.width()))
        self._fy = _clamp01(self._down.y() / max(1, self.height()))
        self._was_held = False
        self._hold.start()
        if self.height() < self.window().height():     # "anywhere" does not give
            self._press_set(1)

    mouseDoubleClickEvent = mousePressEvent

    def mouseReleaseEvent(self, event):
        tap = self._down is not None and not self._was_held
        self._down = None
        self._hold.stop()
        self._press_set(0)
        if tap and self.rect().contains(event.position().toPoint()):
            self.tapped.emit()

    def leaned(self, painter: QPainter) -> bool:
        """While pressed: draws the widget leaning, and returns True (its paint
        code then draws nothing itself). Text drawn in perspective is rebuilt
        from its outlines letter by letter, which on the phone pinned a core for
        every tap; so, as the toolkit does it, the widget is drawn flat once into
        a picture, and that is laid down in a grid of small cells, each placed
        where its corners fall seen from in front of the widget: plain scaled
        copies, quick."""
        p = self._amount()
        if p <= 0.001 or self._flat_drawing:
            return False
        if self._flat is None or self._flat.size() != self.size() * LEAN_DPR:
            self._flat_drawing = True
            try:
                self._flat = _snapshot(self)
            finally:
                self._flat_drawing = False
        w, h = float(self.width()), float(self.height())
        eye = _eye(self.window().height() or 640)
        most = math.radians(TILT_DEG)
        ay = min(most, math.asin(min(1.0, TILT_LIFT / (w / 2))))
        ax = min(most, math.asin(min(1.0, TILT_LIFT / (h / 2))))
        ry, rx = (self._fx - 0.5) * 2 * ay * p, (self._fy - 0.5) * 2 * ax * p
        z0 = -PRESS_SINK * eye * p
        sy, cy_, sx, cx_ = math.sin(ry), math.cos(ry), math.sin(rx), math.cos(rx)

        def at(X, Y):           # a point of the widget (from its middle), as seen
            den = eye - z0 + X * sy + Y * sx
            return w / 2 + eye * cy_ * X / den, h / 2 + eye * cx_ * Y / den
        nx, ny = 10, 3
        r = self._flat.devicePixelRatio()
        painter.fillRect(self.rect(), QColor("black"))
        for i in range(nx):
            for j in range(ny):
                X0, X1 = (i / nx - 0.5) * w, ((i + 1) / nx - 0.5) * w
                Y0, Y1 = (j / ny - 0.5) * h, ((j + 1) / ny - 0.5) * h
                pts = (at(X0, Y0), at(X1, Y0), at(X0, Y1), at(X1, Y1))
                left, right = min(q[0] for q in pts), max(q[0] for q in pts)
                top, bottom = min(q[1] for q in pts), max(q[1] for q in pts)
                painter.drawPixmap(QRectF(left - 0.25, top - 0.25, right - left + 0.5, bottom - top + 0.5), self._flat,
                                   QRectF((X0 + w / 2) * r, (Y0 + h / 2) * r, (X1 - X0) * r, (Y1 - Y0) * r))
        return True

    _fx = _fy = 0.5
    _flat: Optional[QPixmap] = None     # the widget drawn flat, for the lean
    _flat_drawing = False
    _cache: Optional[QPixmap] = None    # the widget as drawn, at the screen's resolution

    def draw(self, p: QPainter):
        """What a subclass looks like (drawn once into a picture, then copied)."""

    def redraw(self):
        """What it shows changed: drawn again at the next paint."""
        self._cache = None
        self._flat = None
        self.update()

    def paintEvent(self, event):
        # Text costs: a row's two lines took 25-30 ms to draw on the phone (Qt
        # draws letters over 64 pixels from their outlines, every time). So a
        # widget draws itself once into a picture, and a repaint (it is uncovered,
        # a page lands) only copies that picture.
        p = QPainter(self)
        if self.leaned(p):
            return
        ratio = self.devicePixelRatioF()
        size = self.size() * ratio
        if self._cache is None or self._cache.size() != size:
            picture = QPixmap(size)
            picture.setDevicePixelRatio(ratio)
            picture.fill(Qt.transparent)    # (an app bar is grey, not black)
            q = QPainter(picture)
            q.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
            self.draw(q)
            q.end()
            self._cache = picture
        p.drawPixmap(0, 0, self._cache)


# ---- the pieces of a page -------------------------------------------------------------

class Entry(_Pressable):
    """A row of a list: a title, and a detail under it in grey (as Settings' rows)."""

    def __init__(self, title: str, detail: str = "", on_tap: Optional[Callable] = None,
                 on_hold: Optional[Callable] = None, parent=None):
        super().__init__(parent)
        self._title, self._detail = title, detail
        self.title_color: Optional[QColor] = None
        self.detail_color: QColor = GREY
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(59 if detail else 45)
        if on_tap:
            self.tapped.connect(on_tap)
        if on_hold:
            self.held.connect(on_hold)

    def set_title(self, title: str):
        self._title = title
        self.redraw()
        self.content_changed()

    def set_detail(self, detail: str):
        self._detail = detail
        self.setFixedHeight(59 if detail else 45)
        self.redraw()
        self.content_changed()

    def draw(self, p: QPainter):
        p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        p.setPen(self.title_color or QColor("white"))
        p.setFont(_font(FONT_SEMILIGHT, 25.5))
        fm = QFontMetricsF(p.font())
        p.drawText(QPointF(0, fm.ascent()), fm.elidedText(self._title, Qt.ElideRight, self.width()))
        if self._detail:
            p.setPen(self.detail_color)
            p.setFont(_font(FONT_REGULAR, 12))
            fm2 = QFontMetricsF(p.font())
            p.drawText(QPointF(0, 31 + fm2.ascent()), fm2.elidedText(self._detail, Qt.ElideRight, self.width()))


class Toggle(_Pressable):
    """A title with an on/off switch at the right, and "On"/"Off" under it."""
    changed = Signal(bool)

    def __init__(self, title: str, on: bool = False, on_change: Optional[Callable[[bool], None]] = None,
                 states: tuple = ("Off", "On"), parent=None):
        super().__init__(parent)
        self._title, self._on, self._states = title, on, states
        self._pos = 1.0 if on else 0.0
        self._anim = QVariantAnimation(self, duration=130, easingCurve=QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._moved)
        self._anim.finished.connect(self.content_changed)
        self.setFixedHeight(59)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.tapped.connect(lambda: self.set_on(not self._on, user=True))
        if on_change:
            self.changed.connect(on_change)

    def _moved(self, value):
        self._pos = float(value)
        self.redraw()               # the switch moved (the lean shows it where it is too)

    def is_on(self) -> bool:
        return self._on

    def set_on(self, on: bool, user: bool = False):
        if on == self._on:
            return
        self._on = on
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()
        if user:
            haptic("click")
            self.changed.emit(on)

    def draw(self, p: QPainter):
        p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        w = self.width()
        p.setPen(QColor("white"))
        p.setFont(_font(FONT_SEMILIGHT, 25.5))
        fm = QFontMetricsF(p.font())
        p.drawText(QPointF(0, fm.ascent()), fm.elidedText(self._title, Qt.ElideRight, w - 70))
        p.setPen(GREY)
        p.setFont(_font(FONT_REGULAR, 12))
        p.drawText(QPointF(0, 31 + QFontMetricsF(p.font()).ascent()), self._states[1 if self._on else 0])
        # the switch: a frame, the accent inside it when on, a white thumb
        x, y = w - 54, 8
        p.setPen(QPen(QColor("white"), 2))
        p.setBrush(Qt.NoBrush)
        p.drawRect(QRectF(x + 1, y + 1, 52, 22))
        p.setPen(Qt.NoPen)
        if self._pos > 0:
            p.setBrush(accent())
            p.drawRect(QRectF(x + 4, y + 4, 46 * self._pos, 16))
        p.setBrush(QColor("white"))
        p.drawRect(QRectF(x + 1 + 40 * self._pos, y - 1, 12, 26))


class Slider(QWidget):
    """A caption and value over a bar the finger drags (the list stays put)."""
    changed = Signal(int)       # while dragging
    released = Signal(int)      # when the finger lifts

    def __init__(self, caption: str, value: int = 50, on_change: Optional[Callable[[int], None]] = None,
                 minimum: int = 0, maximum: int = 100, suffix: str = "%", parent=None):
        super().__init__(parent)
        self._caption, self._min, self._max, self._suffix = caption, minimum, maximum, suffix
        self._value = max(minimum, min(maximum, value))
        self.setFixedHeight(56)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        if on_change:
            self.changed.connect(on_change)

    def value(self) -> int:
        return self._value

    def set_value(self, value: int):
        self._value = max(self._min, min(self._max, int(value)))
        self.update()

    def _at(self, x: float):
        span = max(1, self._max - self._min)
        value = self._min + round(_clamp01(x / max(1, self.width())) * span)
        if value != self._value:
            self._value = value
            self.changed.emit(value)
            self.update()

    def mousePressEvent(self, event):
        haptic("tick")
        self._at(event.position().x())

    def mouseMoveEvent(self, event):
        self._at(event.position().x())

    def mouseReleaseEvent(self, event):
        self.released.emit(self._value)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        w = self.width()
        p.setPen(GREY)
        p.setFont(_font(FONT_REGULAR, 13))
        p.drawText(QPointF(0, QFontMetricsF(p.font()).ascent()), self._caption)
        p.setPen(QColor("white"))
        p.setFont(_font(FONT_SEMIBOLD, 14))
        shown = f"{self._value}{self._suffix}"
        fm = QFontMetricsF(p.font())
        p.drawText(QPointF(w - fm.horizontalAdvance(shown), fm.ascent()), shown)
        track = 34
        fill = w * (self._value - self._min) / max(1, self._max - self._min)
        p.fillRect(QRectF(0, track, w, 4), QColor(56, 56, 56))
        p.fillRect(QRectF(0, track, fill, 4), accent())
        p.fillRect(QRectF(min(max(fill - 5, 0), w - 10), track - 7, 10, 18), QColor("white"))


class _Field(QLineEdit):
    def __init__(self, *args):
        super().__init__(*args)
        self.setFocusPolicy(Qt.TabFocus)    # not on the touch: on the tap (below)

    def mouseReleaseEvent(self, event):
        if not self.hasFocus():
            self.setFocus(Qt.MouseFocusReason)
        super().mouseReleaseEvent(event)

    def focusInEvent(self, event):
        super().focusInEvent(event)
        osk(True, self)             # the shell lifts the app if this box would be under the keyboard

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        osk(False)


class TextBox(QWidget):
    """A caption over a box to type in. The keyboard comes up when it is tapped."""
    changed = Signal(str)
    submitted = Signal(str)

    def __init__(self, caption: str = "", text: str = "", on_change: Optional[Callable[[str], None]] = None,
                 on_submit: Optional[Callable[[str], None]] = None, password: bool = False,
                 placeholder: str = "", parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        if caption:
            layout.addWidget(_label(caption, FONT_REGULAR, 13, GREY))
        self.field = _Field(text)
        self.field.setPlaceholderText(placeholder)
        self.field.setFixedHeight(40)
        if password:
            self.field.setEchoMode(QLineEdit.Password)
        layout.addWidget(self.field)
        self.field.textChanged.connect(self.changed)
        self.field.returnPressed.connect(lambda: self.submitted.emit(self.field.text()))
        if on_change:
            self.changed.connect(on_change)
        if on_submit:
            self.submitted.connect(on_submit)

    def text(self) -> str:
        return self.field.text()

    def set_text(self, text: str):
        self.field.setText(text)


_GLYPHS = {"add", "save", "edit", "delete", "send", "cancel", "check", "back", "refresh", "search",
           "call", "message", "more", "share", "settings", "play", "pause"}


def _glyph(p: QPainter, name: str, size: float):
    """The phone's round-button glyphs, drawn about (0, 0), `size` across."""
    s = size / 2
    path = QPainterPath()
    if name == "add":
        path.moveTo(-s, 0); path.lineTo(s, 0); path.moveTo(0, -s); path.lineTo(0, s)
    elif name == "cancel":
        path.moveTo(-s, -s); path.lineTo(s, s); path.moveTo(s, -s); path.lineTo(-s, s)
    elif name == "check":
        path.moveTo(-s, 0); path.lineTo(-s / 3, s * 0.7); path.lineTo(s, -s * 0.7)
    elif name == "back":
        path.moveTo(s, 0); path.lineTo(-s, 0); path.moveTo(-s * 0.2, -s * 0.8); path.lineTo(-s, 0)
        path.lineTo(-s * 0.2, s * 0.8)
    elif name == "more":
        for x in (-s * 0.8, 0, s * 0.8):
            path.addEllipse(QPointF(x, 0), 1.6, 1.6)
    elif name == "save":
        path.addRect(QRectF(-s, -s, 2 * s, 2 * s)); path.addRect(QRectF(-s * 0.5, -s, s, s * 0.7))
        path.addRect(QRectF(-s * 0.6, s * 0.2, s * 1.2, s * 0.8))
    elif name == "edit":
        path.moveTo(-s, s); path.lineTo(-s * 0.8, s * 0.3); path.lineTo(s * 0.5, -s); path.lineTo(s, -s * 0.5)
        path.lineTo(-s * 0.3, s * 0.8); path.closeSubpath()
    elif name == "delete":
        path.moveTo(-s, -s * 0.6); path.lineTo(s, -s * 0.6); path.moveTo(-s * 0.3, -s * 0.6)
        path.lineTo(-s * 0.3, -s); path.lineTo(s * 0.3, -s); path.lineTo(s * 0.3, -s * 0.6)
        path.moveTo(-s * 0.7, -s * 0.6); path.lineTo(-s * 0.55, s); path.lineTo(s * 0.55, s); path.lineTo(s * 0.7, -s * 0.6)
    elif name == "send":
        path.moveTo(-s, -s * 0.8); path.lineTo(s, 0); path.lineTo(-s, s * 0.8); path.lineTo(-s * 0.55, 0)
        path.closeSubpath(); path.moveTo(-s * 0.55, 0); path.lineTo(s * 0.3, 0)
    elif name == "refresh":
        path.arcMoveTo(QRectF(-s, -s, 2 * s, 2 * s), 60); path.arcTo(QRectF(-s, -s, 2 * s, 2 * s), 60, 280)
        path.moveTo(s * 0.5, -s); path.lineTo(s * 0.5, -s * 0.4); path.lineTo(s * 1.05, -s * 0.5)
    elif name == "search":
        path.addEllipse(QPointF(s * 0.2, -s * 0.2), s * 0.65, s * 0.65); path.moveTo(-s * 0.25, s * 0.25)
        path.lineTo(-s, s)
    elif name == "message":
        path.addRect(QRectF(-s, -s * 0.8, 2 * s, s * 1.3)); path.moveTo(-s * 0.4, s * 0.5)
        path.lineTo(-s * 0.7, s); path.lineTo(0, s * 0.5)
    elif name == "call":
        path.moveTo(-s * 0.8, -s); path.cubicTo(-s * 1.2, 0, 0, s * 1.2, s, s * 0.8)
        path.lineTo(s * 0.8, s * 0.3); path.lineTo(s * 0.3, s * 0.4); path.lineTo(-s * 0.4, -s * 0.3)
        path.lineTo(-s * 0.3, -s * 0.8); path.closeSubpath()
    elif name == "share":
        for c in ((s * 0.7, -s * 0.7), (-s * 0.7, 0), (s * 0.7, s * 0.7)):
            path.addEllipse(QPointF(*c), s * 0.25, s * 0.25)
        path.moveTo(s * 0.7, -s * 0.7); path.lineTo(-s * 0.7, 0); path.lineTo(s * 0.7, s * 0.7)
    elif name == "settings":
        path.addEllipse(QPointF(0, 0), s * 0.4, s * 0.4); path.addEllipse(QPointF(0, 0), s * 0.85, s * 0.85)
    elif name == "play":
        path.moveTo(-s * 0.5, -s * 0.8); path.lineTo(s * 0.8, 0); path.lineTo(-s * 0.5, s * 0.8); path.closeSubpath()
    elif name == "pause":
        path.addRect(QRectF(-s * 0.6, -s * 0.8, s * 0.4, s * 1.6)); path.addRect(QRectF(s * 0.2, -s * 0.8, s * 0.4, s * 1.6))
    p.drawPath(path)


class RoundButton(_Pressable):
    """A glyph in a ring, with a word under it: "add", "save", "edit", "delete",
    "send", "cancel", "check", "back", "refresh", "search", "call", "message",
    "more", "share", "settings", "play", "pause" -- or any short text, or an
    .svg of the app's."""

    def __init__(self, glyph: str, label: str = "", on_tap: Optional[Callable] = None, parent=None):
        super().__init__(parent)
        self._glyph, self._label = glyph, label
        self._svg = None
        if glyph.endswith(".svg"):
            from PySide6.QtSvg import QSvgRenderer
            self._svg = QSvgRenderer(QByteArray(resource(glyph)))
        self.setFixedSize(68, 74 if label else 52)
        if on_tap:
            self.tapped.connect(on_tap)

    def draw(self, p: QPainter):
        p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        cx, d = self.width() / 2, 44
        pressed = self._p_to > 0
        if pressed:
            p.setPen(Qt.NoPen)
            p.setBrush(accent())
            p.drawEllipse(QPointF(cx, 4 + d / 2), d / 2, d / 2)
        p.setPen(QPen(QColor("white"), 2.2))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QPointF(cx, 4 + d / 2), d / 2 - 1.1, d / 2 - 1.1)
        p.save()
        p.translate(cx, 4 + d / 2)
        if self._svg is not None:
            self._svg.render(p, QRectF(-11, -11, 22, 22))
        elif self._glyph in _GLYPHS:
            p.setPen(QPen(QColor("white"), 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            _glyph(p, self._glyph, 18)
        else:
            p.setFont(_font(FONT_SEMIBOLD, 16))
            p.drawText(QRectF(-d / 2, -d / 2, d, d), Qt.AlignCenter, self._glyph)
        p.restore()
        if self._label:
            p.setPen(QColor("white"))
            p.setFont(_font(FONT_REGULAR, 11.5))
            p.drawText(QRectF(0, d + 7, self.width(), 18), Qt.AlignHCenter | Qt.AlignTop, self._label)


class AppBar(QWidget):
    """The bar along the bottom of a page: round buttons, with their words."""

    def __init__(self, buttons: Iterable[RoundButton] = (), parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet("background: #1f1f1f;")
        self.setFixedHeight(80)
        self._row = QHBoxLayout(self)
        self._row.setContentsMargins(MARGIN, 2, MARGIN, 4)
        self._row.addStretch(1)
        for b in buttons:
            self.add(b)
        self._row.addStretch(1)

    def add(self, button: RoundButton) -> RoundButton:
        self._row.insertWidget(self._row.count() - 1, button)
        return button


class _Menu(QWidget):
    """The hold menu: everything else goes dim; a white band of choices."""

    def __init__(self, host: QWidget, y: float, items: list):
        super().__init__(host)
        self._items, self._y = items, y
        self.setGeometry(host.rect())
        self.show()
        self.raise_()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        p.fillRect(self.rect(), QColor(0, 0, 0, 184))
        top = self._band_top()
        p.fillRect(QRectF(0, top, self.width(), len(self._items) * 50 + 16), QColor("white"))
        p.setPen(QColor("black"))
        p.setFont(_font(FONT_SEMILIGHT, 24))
        fm = QFontMetricsF(p.font())
        for i, (text, _) in enumerate(self._items):
            p.drawText(QPointF(MARGIN, top + 16 + i * 50 + fm.ascent()), text)

    def _band_top(self) -> float:
        h = len(self._items) * 50 + 16
        return max(0.0, min(self._y, self.height() - h))

    def mouseReleaseEvent(self, event):
        y = event.position().y() - self._band_top() - 8
        self.close()
        self.deleteLater()
        if 0 <= y < len(self._items) * 50:
            callback = self._items[int(y // 50)][1]
            if callback:
                callback()

    def mousePressEvent(self, event):
        pass


def menu(anchor: QWidget, items: list):
    """Show the hold menu under `anchor`: items is [(text, callback), ...].
    A tap outside it closes it."""
    host = anchor.window()
    y = anchor.mapTo(host, QPointF(0, anchor.height())).y()
    return _Menu(host, y, items)


# ---- pages -----------------------------------------------------------------------------

class _Header(QWidget):
    """The app's name in small capitals, and the page's title under it."""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.title = title
        self.setFixedHeight(HEADER_H)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        p.setPen(QColor("white"))
        p.setFont(_font(FONT_SEMIBOLD, 11.5))
        p.drawText(QPointF(MARGIN, 14 + QFontMetricsF(p.font()).ascent()), _caps())
        p.setFont(_font(FONT_SEMILIGHT, 48))
        fm = QFontMetricsF(p.font())
        p.drawText(QPointF(MARGIN - 3, 24 + fm.ascent()), self.title)


def _caps() -> str:
    m = manifest()
    return (m.get("caps") or m.get("name") or "APP").upper()


class _Viewport(QWidget):
    """The window onto a page's list: the list moves inside it."""

    def __init__(self, page: "Page"):
        super().__init__(page)
        self.page = page
        self.setAttribute(Qt.WA_OpaquePaintEvent)

    def paintEvent(self, event):
        QPainter(self).fillRect(event.rect(), QColor("black"))

    def resizeEvent(self, event):
        self.page._fit()


class Page(QWidget):
    """One page: a title, and under it a list that scrolls. Add to it from top
    to bottom; the helpers return what they made, to keep and change later.

    Override shown() to load what the page shows when it arrives, and back()
    to keep the back key (return True)."""

    def __init__(self, title: str = "", scroll: bool = True, header: bool = True, parent=None):
        super().__init__(parent)
        self.setObjectName("ztosPage")
        self.setAttribute(Qt.WA_StyledBackground, True)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.header = _Header(title)
        self.header.setVisible(header)
        outer.addWidget(self.header)
        self.viewport = _Viewport(self)
        outer.addWidget(self.viewport, 1)
        self.body = QWidget(self.viewport)
        self.body.setObjectName("ztosBody")
        self.body.setAttribute(Qt.WA_StyledBackground, True)
        self.body.installEventFilter(self)
        self.list = QVBoxLayout(self.body)
        self.list.setContentsMargins(MARGIN, 0, MARGIN, 24)
        self.list.setSpacing(0)
        self.list.addStretch(1)
        self.scrolls = scroll
        self._scroll = 0.0
        self._vel = 0.0
        self._last = 0.0
        self._ticker = _Ticker(self, self._fling_step)
        self.app_bar: Optional[AppBar] = None
        # the list as one picture on the GPU, made while idle, moved while scrolling
        self._gl_on = False
        self._gl_dirty = True
        self._version = 0           # counts changes: a picture made at this count is current
        self._print_top: Optional[int] = None
        self._print_img: Optional[QImage] = None
        self._check = QTimer(self, singleShot=True, interval=350)
        self._check.timeout.connect(self._verify)
        self._gl_top = self._gl_h = 0.0
        self._quiet = False
        self._gl_idle = QTimer(self, singleShot=True, interval=400)
        self._gl_idle.timeout.connect(self._gl_prepare)

    @property
    def title(self) -> str:
        return self.header.title

    @title.setter
    def title(self, value: str):
        self.header.title = value
        self.header.update()
        outer = _ancestor(self.parentWidget(), Pivot)
        if outer is not None:
            outer._titles.update()

    # what an app overrides
    def shown(self):
        """The page has arrived on the screen."""

    def back(self) -> bool:
        """The back key, while this page is in front: True if it was used."""
        return False

    # the list, and how it moves: the toolkit's numbers
    def eventFilter(self, obj, event):
        if obj is self.body and event.type() == QEvent.LayoutRequest:
            QTimer.singleShot(0, self._fit)     # a change only if the list changed shape (see _fit)
        return False

    def _changed(self, explicit: bool = False):
        """Something on the list was drawn anew: its pictures are out of date
        (redrawn once things are still, and only then)."""
        if self._quiet or (not explicit and (_drawing_pictures or _now() < _settle_until)):
            return
        self._version += 1
        if not self._gl_dirty:
            self._gl_dirty = True
            if _layer_of(self) is not None and not self._gl_on:
                self._gl_idle.start()

    def _gl_prepare(self, force: bool = False):
        """Draw the list (around where it is) into the GPU's picture of it."""
        layer = _layer_of(self)
        if layer is None or not self.scrolls or self._max() <= 0 or not self.isVisible():
            return
        if self._gl_on and not force:
            return
        if not force and _animating():
            self._gl_idle.start()       # after the animation
            return
        if not force and not self._gl_dirty and layer.has((id(self), "list")) and self._gl_covers(self._scroll):
            return                  # the picture there is still right
        ratio = self.devicePixelRatioF()
        most = layer.max_texture / ratio
        view, body_h, w = float(self.viewport.height()), float(self.body.height()), self.viewport.width()
        height = min(most, body_h)
        top = max(0.0, min(self._scroll - (height - view) / 2, body_h - height))
        image = QImage(max(1, round(w * ratio)), max(1, round(height * ratio)), QImage.Format_RGB32)
        image.setDevicePixelRatio(ratio)
        image.fill(QColor("black"))
        self._quiet = True
        self.body.render(image, QPoint(0, 0), QRegion(0, round(top), w, round(height)),
                         QWidget.RenderFlag.DrawChildren | QWidget.RenderFlag.DrawWindowBackground)
        self._quiet = False
        layer.upload((id(self), "list"), image)
        self._gl_top, self._gl_h = float(round(top)), float(round(height))
        self._print_top, self._print_img = round(top), self._print(round(top), round(height))
        self._gl_dirty = False

    def _print(self, top: int, height: int) -> QImage:
        """The list from `top` down `height` points, at one pixel a point: drawn
        the same way each time, so two of them are equal exactly when nothing
        there has changed (a check costing a sixteenth of a real picture)."""
        global _drawing_pictures
        w = max(1, self.viewport.width())
        image = QImage(w, max(1, height), QImage.Format_RGB32)
        image.setDevicePixelRatio(1.0)
        image.fill(QColor("black"))
        _drawing_pictures += 1
        self._quiet = True
        try:
            self.body.render(image, QPoint(0, 0), QRegion(0, top, w, height), QWidget.RenderFlag.DrawChildren)
        finally:
            self._quiet = False
            _drawing_pictures -= 1
        return image

    def _verify(self):
        """Still again after something moved: is the GPU's picture of what is on
        the screen still what the widgets show? If not, it is made again."""
        if self._gl_on or self._gl_dirty or self._print_top is None or not self.isVisible():
            return
        if _animating():
            self._check.start()         # after the animation
            return
        view = self.viewport.height()
        top = max(0, round(self._scroll))
        if top < self._print_top or top + view > self._print_top + self._print_img.height():
            return
        now = self._print(top, view)
        then = self._print_img.copy(0, top - self._print_top, now.width(), view)
        if now != then:
            self._changed(explicit=True)

    def _gl_covers(self, scroll: float) -> bool:
        view = self.viewport.height()
        return self._gl_top <= max(0.0, scroll) and min(scroll + view, self.body.height()) <= self._gl_top + self._gl_h

    def _gl_begin(self):
        layer = _layer_of(self)
        if self._gl_on or layer is None or not self.scrolls or self._max() <= 0:
            return
        if self._gl_dirty or not layer.has((id(self), "list")) or not self._gl_covers(self._scroll):
            # the picture is not ready: this time the plain way, never a wait
            # for the finger; it is drawn once the list is still
            self._gl_idle.start()
            return
        self._gl_on = True
        layer.show(QRect(self.viewport.mapTo(self.window(), QPoint(0, 0)), self.viewport.size()), self._gl_quads)
        self.viewport.setUpdatesEnabled(False)

    def _gl_quads(self) -> list:
        from ._gl import Quad
        w, view = float(self.viewport.width()), float(self.viewport.height())
        s0 = self._scroll - self._gl_top
        c0, c1 = max(0.0, s0), min(self._gl_h, s0 + view)
        if c1 <= c0:
            return []
        return [Quad.rect((id(self), "list"), QRectF(0, c0 / self._gl_h, 1, (c1 - c0) / self._gl_h),
                          0, c0 - s0, w, c1 - c0)]

    def _gl_end(self):
        """Still again: the widgets themselves, where the picture was."""
        if not self._gl_on:
            return
        _settle()
        self._gl_on = False
        self._quiet = True
        self.viewport.setUpdatesEnabled(True)
        self.body.move(0, -round(self._scroll))
        self.viewport.repaint()
        self._quiet = False
        layer = _layer_of(self)
        if layer is not None:
            layer.hide()

    def _fit(self):
        w, view_h = self.viewport.width(), self.viewport.height()
        layout = self.list
        layout.activate()
        h = layout.totalHeightForWidth(w) if layout.hasHeightForWidth() else layout.totalSizeHint().height()
        self.body.setGeometry(0, -round(self._scroll), w, max(view_h, h))
        shape = (w, h, layout.count())
        if shape != getattr(self, "_shape", None):
            if getattr(self, "_shape", None) is not None:
                self._changed(explicit=True)    # rows added, taken away or resized
            self._shape = shape
        if not self._ticker.active():
            self._set(min(self._scroll, self._max()) if self._scroll > 0 else 0.0)

    def _max(self) -> float:
        return max(0.0, float(self.body.height() - self.viewport.height())) if self.scrolls else 0.0

    def _set(self, scroll: float):
        if self._gl_on:
            if self._gl_covers(scroll):
                self._scroll = scroll
                _layer_of(self).update()
                return
            # past the end of the picture: on the plain way from here
            self._gl_end()
        if scroll != self._scroll or self.body.y() != -round(scroll):
            self._scroll = scroll
            self.body.move(0, -round(scroll))

    def _raw_scroll(self) -> float:
        """Where the finger's pull is, given where an overscrolled list is."""
        s, top = self._scroll, self._max()
        return s / 0.4 if s < 0 else top + (s - top) / 0.4 if s > top else s

    def _drag_to(self, raw: float):
        """Under the finger the list goes where it is taken; past either end it
        follows at a fraction of the pull."""
        top = self._max()
        self._gl_begin()
        self._set(raw * 0.4 if raw < 0 else top + (raw - top) * 0.4 if raw > top else raw)

    def _fling(self, v: float):
        """Let go: carry on at the speed it had, slowing; a little past an end
        and drawn back to it."""
        self._vel = v
        self._last = _now()
        self._gl_begin()
        self._ticker.start()

    def _flinging(self) -> bool:
        return self._ticker.active()

    def _stop(self):
        self._ticker.stop()
        self._vel = 0.0

    def _fling_step(self, now) -> bool:
        dt = min(50.0, now - self._last)
        self._last = now
        top = self._max()
        s = self._scroll + self._vel * dt
        bound = 0.0 if s < 0 else top if s > top else s
        going = True
        if bound != s:
            self._vel *= math.exp(-dt / 35.0)
            s += (bound - s) * (1 - math.exp(-dt / 110.0))
            if abs(self._vel) < 0.05 and abs(bound - s) < 0.5:
                s, going = bound, False
        else:
            self._vel *= math.exp(-dt / 350.0)
            going = abs(self._vel) >= 0.02
        self._set(s)
        if not going:
            self._gl_end()
            self._gl_idle.start()
        return going

    def scroll_to(self, y: float):
        self._stop()
        self._gl_end()
        self._set(max(0.0, min(float(y), self._max())))

    def ensure_visible(self, widget: QWidget, margin: int = 24):
        """Scroll so that `widget` (on this page) can be seen."""
        y = widget.mapTo(self.body, QPoint(0, 0)).y()
        view = self.viewport.height()
        if y - margin < self._scroll:
            self.scroll_to(y - margin)
        elif y + widget.height() + margin > self._scroll + view:
            self.scroll_to(y + widget.height() + margin - view)

    def _rows(self) -> list:
        """The tops of the rows, for page turns."""
        return [w for w in (self.list.itemAt(i).widget() for i in range(self.list.count()))
                if w is not None and w.isVisible()]

    # building it
    def add(self, widget: QWidget, gap: int = 0) -> QWidget:
        if gap:
            self.list.insertSpacing(self.list.count() - 1, gap)
        self.list.insertWidget(self.list.count() - 1, widget)
        return widget

    def space(self, points: int = 16):
        self.list.insertSpacing(self.list.count() - 1, points)

    def text(self, text: str, size: float = 15, color: Optional[QColor] = None) -> QLabel:
        label = _label(text, FONT_REGULAR, size, color)
        self.add(label)
        self.space(12)
        return label

    def section(self, text: str) -> QLabel:
        """A small grey caption over the rows that follow."""
        label = _label(text, FONT_REGULAR, 13, GREY)
        self.add(label, gap=10)
        self.space(6)
        return label

    def entry(self, title: str, detail: str = "", on_tap: Optional[Callable] = None,
              on_hold: Optional[Callable] = None) -> Entry:
        return self.add(Entry(title, detail, on_tap, on_hold))

    def toggle(self, title: str, on: bool = False, on_change: Optional[Callable[[bool], None]] = None) -> Toggle:
        return self.add(Toggle(title, on, on_change))

    def slider(self, caption: str, value: int = 50, on_change: Optional[Callable[[int], None]] = None,
               minimum: int = 0, maximum: int = 100, suffix: str = "%") -> Slider:
        slider = self.add(Slider(caption, value, on_change, minimum, maximum, suffix))
        self.space(8)
        return slider

    def textbox(self, caption: str = "", text: str = "", on_change=None, on_submit=None,
                password: bool = False, placeholder: str = "") -> TextBox:
        box = self.add(TextBox(caption, text, on_change, on_submit, password, placeholder))
        self.space(16)
        return box

    def button(self, label: str, on_tap: Callable) -> QPushButton:
        button = QPushButton(label)
        button.clicked.connect(on_tap)
        button.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        self.add(button)
        self.space(12)
        return button

    def round_buttons(self, *buttons: RoundButton) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(4)
        for b in buttons:
            layout.addWidget(b)
        layout.addStretch(1)
        return self.add(row)

    def bar(self, *buttons: RoundButton) -> AppBar:
        """An app bar along the bottom of this page."""
        if self.app_bar is None:
            self.app_bar = AppBar()
            self.layout().addWidget(self.app_bar)
        for b in buttons:
            self.app_bar.add(b)
        return self.app_bar

    def busy(self, on: bool = True):
        """The loading dots across the top of the page, while something loads."""
        if on and getattr(self, "_busy", None) is None:
            self._busy = LoadingDots(self)
            self._busy.setGeometry(0, 0, self.width(), 8)
            self._busy.show()
            self._busy.raise_()
        elif not on and getattr(self, "_busy", None) is not None:
            self._busy.hide()
            self._busy.deleteLater()
            self._busy = None

    def clear(self):
        """Take everything off the list (to fill it again)."""
        while self.list.count() > 1:
            item = self.list.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self._stop()
        self._set(0.0)


# ---- pivots ------------------------------------------------------------------------------
#
# As the phone's Settings does it: the tabs lie side by side a screen apart and
# move whole, under the finger and after it; the row of titles moves with them
# but slower, a title's width while the tabs move a screen's, so the next title
# arrives where this one was as the next tab does, lighting up past half way.
# Let go, the tabs carry on at the finger's own speed into place (or back) on a
# spring that does not overshoot. The app's name stays.

class Pivot(QWidget):
    """Pages side by side under one row of titles: swipe, or tap a title.
    The pages are ordinary Pages; their own headers are not shown."""
    turned = Signal(int)

    def __init__(self, pages: list, parent=None):
        super().__init__(parent)
        self.setObjectName("ztosPage")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.pages = list(pages)
        self.index = 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._titles = _PivotTitles(self)
        layout.addWidget(self._titles)
        self._area = QWidget()
        self._stack = QStackedLayout(self._area)
        layout.addWidget(self._area, 1)
        for page in self.pages:
            page.header.hide()
            self._stack.addWidget(page)
        self._x = 0.0           # how far the strip of tabs has moved (points, right +)
        self._v = 0.0
        self._target = 0.0
        self._last = 0.0
        self._snaps: dict = {}
        self._snap_at: dict = {}    # each picture's page change count and scroll
        self._texts: dict = {}      # the titles' pictures: key -> (text, width, height)
        self._motion = _PivotMotion(self)
        self._spring = _Ticker(self, self._spring_step)
        self._idle = QTimer(self, singleShot=True, interval=300)
        self._idle.timeout.connect(self._prepare_neighbours)

    def _outer(self) -> "Pivot":
        return self

    def current(self) -> Page:
        return self.pages[self.index]

    def shown(self):
        self.current().shown()
        self._idle.start()

    def back(self) -> bool:
        return self.current().back()

    def _rows(self) -> list:
        return self.current()._rows()

    @property
    def body(self):
        return self.current().body

    # pictures of the tabs, at the moving resolution
    def _at(self, k: int) -> int:
        return (self.index + k) % len(self.pages)

    def _snap(self, i: int) -> QPixmap:
        page = self.pages[i]
        if page.size() != self._area.size():
            page.setGeometry(self._area.rect())
        page.layout().activate()
        page._fit()
        picture = _snapshot(page, self._area.size())
        self._snap_at[i] = (page._version, page._scroll)
        layer = _layer_of(self)
        if layer is not None:
            layer.upload((id(self), i), picture.toImage())
        return picture

    def _current_snap(self, i: int) -> bool:
        page = self.pages[i]
        return i in self._snaps and self._snap_at.get(i) == (page._version, page._scroll)

    def _forget(self, page: "Page"):
        """A tab's content changed: its picture is drawn again when idle."""
        if page in self.pages:
            self._snaps.pop(self.pages.index(page), None)
            self._idle.start()

    def _text_texture(self, layer, key, text: str, font: QFont) -> tuple:
        """A line of white text as a GPU picture (made once per text): (key, width, height)."""
        known = self._texts.get(key)
        if known is not None and known[0] == text and layer.has(key):
            return known[1:]
        fm = QFontMetricsF(font)
        w, h = max(1.0, math.ceil(fm.horizontalAdvance(text)) + 2.0), math.ceil(fm.height())
        ratio = self.devicePixelRatioF()
        image = QImage(round(w * ratio), round(h * ratio), QImage.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(ratio)
        image.fill(Qt.transparent)          # only the letters: titles may overlap the name above them
        p = QPainter(image)
        p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        p.setPen(QColor("white"))
        p.setFont(font)
        p.drawText(QPointF(0, fm.ascent()), text)
        p.end()
        layer.upload(key, image)
        self._texts[key] = (text, w, h)
        return w, h

    def _prepare_titles(self):
        """The name and titles as GPU pictures, and how wide each title is: once
        per slide, so that a frame is only arithmetic."""
        layer = _layer_of(self)
        caps_font, title_font = _font(FONT_SEMIBOLD, 11.5), _font(FONT_SEMILIGHT, 48)
        self._caps_size = self._text_texture(layer, (id(self), "caps"), _caps(), caps_font)
        fm = QFontMetricsF(title_font)
        self._widths = [fm.horizontalAdvance(p.title) + 16 for p in self.pages]
        self._title_sizes = [self._text_texture(layer, (id(self), "title", i), p.title, title_font)
                             for i, p in enumerate(self.pages)]

    def _quads(self) -> list:
        """One frame of a slide, all of it the GPU's: the app's name, the titles
        (each its own picture, dimmed as it should be) and the tabs."""
        from ._gl import Quad
        w, h = float(self._area.width()), float(self._area.height())
        u = -self._x / w
        k0 = math.floor(u)
        whole = _WHOLE
        cw, ch = self._caps_size
        quads = [Quad.rect((id(self), "caps"), whole, MARGIN, 14, cw, ch)]
        width = self._widths.__getitem__
        a, b = self._title_pos(k0, width), self._title_pos(k0 + 1, width)
        strip = a + (u - k0) * (b - a)
        for k in range(k0 - 1, k0 + 4):
            i = self._at(k)
            at = MARGIN - 3 + self._title_pos(k, width) - strip
            if at > w or at + self._widths[i] < 0:
                continue
            tw, th = self._title_sizes[i]
            light = _clamp01((w * 0.5 + 30 - abs(u - k) * w) / 60)
            quads.append(Quad.rect((id(self), "title", i), whole, at, 24, tw, th, 0.38 + 0.62 * light))
        top = float(self._titles.height())
        for k in (k0, k0 + 1):
            quads.append(Quad.rect((id(self), self._at(k)), whole, self._x + k * w, top, w, h))
        return quads

    def _prepare_neighbours(self):
        """The tabs either side, drawn while nothing moves, so a swipe starts at once."""
        if self._spring.active() or getattr(self, "_moving", False) or len(self.pages) < 2:
            return
        if _animating():
            self._idle.start()          # after the animation
            return
        for k in (0, 1, -1):
            i = self._at(k)
            if not self._current_snap(i):
                self._snaps[i] = self._snap(i)

    def sliding(self) -> bool:
        return self._spring.active()

    def begin_drag(self):
        self._spring.stop()
        for k in (0, 1, -1):         # drawn again only if changed since
            if not self._current_snap(self._at(k)):
                self._snaps[self._at(k)] = self._snap(self._at(k))
        layer = _layer_of(self)
        if layer is not None:
            self._prepare_titles()  # the titles' pictures, made before the first frame
            layer.show(QRect(self.mapTo(self.window(), QPoint(0, 0)), self.size()), self._quads)
            self.setUpdatesEnabled(False)   # under the layer: nothing to repaint until it goes
        else:
            self._motion.setGeometry(self._area.geometry())
            self._motion.show()
            self._motion.raise_()
        self._moving = True

    def catch(self):
        self._spring.stop()

    def drag(self, x: float):
        self._x = x
        self._redraw()

    def _redraw(self):
        layer = _layer_of(self)
        if layer is not None and layer.running:
            layer.update()          # titles and all: the GPU's
        else:
            self._motion.update()
            self._titles.update()

    def release(self, v: float):
        w = float(self.width())
        u = -self._x / w
        # thrown: on to the next tab that way; else whichever is nearest
        k = math.floor(u) + 1 if v < -PIVOT_FLICK else math.ceil(u) - 1 if v > PIVOT_FLICK else round(u)
        self._go(-k * w, v)

    def _go(self, target: float, v: float):
        self._target, self._v, self._last = target, v, _now()
        self._spring.start()

    def _spring_step(self, now) -> bool:
        # a critically damped spring, by the clock, in small steps
        dt = min(50.0, now - self._last)
        self._last = now
        x, v, T, k = self._x, self._v, self._target, PIVOT_SPRING_W
        arrived = False
        while dt > 0 and not arrived:
            step = min(4.0, dt)
            dt -= step
            before = x - T
            v += (-k * k * before - 2 * k * v) * step
            x += v * step
            if (x - T) * before < 0 or (abs(x - T) < 0.4 and abs(v) < 0.02):
                arrived = True      # in place, and never past it
        self._x, self._v = (T if arrived else x), v
        if arrived:
            self._land(round(-T / max(1.0, self.width())))
            return False
        self._redraw()
        return True

    def _land(self, k: int):
        _settle()
        self._x = 0.0
        self._moving = False
        self._motion.hide()
        if k % len(self.pages) != 0:
            self.index = self._at(k)
            self._stack.setCurrentIndex(self.index)
        layer = _layer_of(self)
        if layer is not None:
            page = self.pages[self.index]
            page._quiet = True
            self.setUpdatesEnabled(True)
            self.repaint()              # the tab as it is, before the pictures go
            page._quiet = False
            layer.hide()
        self._titles.update()
        if k % len(self.pages) != 0:
            self.turned.emit(self.index)
            self.pages[self.index].shown()
        self._idle.start()

    def turn(self, index: int, forward: bool = True):
        """Slide to page `index`, as a tap on its title does."""
        n = len(self.pages)
        steps = (index - self.index) % n
        if steps == 0:
            return
        if not forward:
            steps -= n
        self.begin_drag()
        for k in range(-abs(steps), abs(steps) + 1):
            if not self._current_snap(self._at(k)):
                self._snaps[self._at(k)] = self._snap(self._at(k))
        self._go(-steps * float(self.width()), 0.0)

    # where tab k's title is along the strip
    def _title_widths(self, fm: QFontMetricsF):
        return lambda i: fm.horizontalAdvance(self.pages[i].title) + 16

    def _title_pos(self, k: int, width) -> float:
        pos = 0.0
        if k >= 0:
            for j in range(k):
                pos += width(self._at(j))
        else:
            for j in range(k, 0):
                pos -= width(self._at(j))
        return pos


class _PivotTitles(QWidget):
    def __init__(self, pivot: Pivot):
        super().__init__(pivot)
        self.pivot = pivot
        self.setFixedHeight(HEADER_H)
        self._spans: list = []

    _cache: Optional[QPixmap] = None
    _cache_key = None

    def paintEvent(self, event):
        # at rest, the titles are a picture drawn once (drawing big text costs)
        pivot = self.pivot
        key = (pivot.index, tuple(pg.title for pg in pivot.pages), self.width(), _caps())
        if pivot._x == 0:
            if self._cache is None or self._cache_key != key:
                ratio = self.devicePixelRatioF()
                picture = QPixmap(self.size() * ratio)
                picture.setDevicePixelRatio(ratio)
                q = QPainter(picture)
                self._draw(q)
                q.end()
                self._cache, self._cache_key = picture, key
            QPainter(self).drawPixmap(0, 0, self._cache)
            return
        p = QPainter(self)
        self._draw(p)

    def _draw(self, p: QPainter):
        pivot = self.pivot
        p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        p.fillRect(self.rect(), QColor("black"))
        p.setPen(QColor("white"))
        p.setFont(_font(FONT_SEMIBOLD, 11.5))
        p.drawText(QPointF(MARGIN, 14 + QFontMetricsF(p.font()).ascent()), _caps())
        p.setFont(_font(FONT_SEMILIGHT, 48))
        fm = QFontMetricsF(p.font())
        width = pivot._title_widths(fm)
        w = float(self.width())
        u = -pivot._x / w
        k0 = math.floor(u)
        f = u - k0
        a, b = pivot._title_pos(k0, width), pivot._title_pos(k0 + 1, width)
        strip = a + f * (b - a)
        self._spans = []
        for k in range(k0 - 1, k0 + 4):
            at = MARGIN - 3 + pivot._title_pos(k, width) - strip
            if at > w or at + width(pivot._at(k)) < 0:
                continue
            # white for the tab nearest the middle, grey for the rest, crossing at half way
            light = _clamp01((w * 0.5 + 30 - abs(u - k) * w) / 60)
            p.setOpacity(0.38 + 0.62 * light)
            p.drawText(QPointF(at, 24 + fm.ascent()), pivot.pages[pivot._at(k)].title)
            self._spans.append((at, at + width(pivot._at(k)) - 16, k))

    def mouseReleaseEvent(self, event):
        if self.pivot.sliding() or self.pivot._x != 0:
            return
        x = event.position().x()
        for x0, x1, k in self._spans:
            if k > 0 and x0 <= x <= x1:
                haptic("tap")
                self.pivot.turn(self.pivot._at(k), forward=True)
                return


class _PivotMotion(_Moving):
    """The tabs while they move: pictures of them side by side, a screen apart."""

    def __init__(self, pivot: Pivot):
        super().__init__(pivot)
        self.pivot = pivot
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.hide()

    def compose(self, p: QPainter):
        pivot = self.pivot
        w, h = float(self.width()), float(self.height())
        u = -pivot._x / w
        k0 = math.floor(u)
        for k in (k0, k0 + 1):
            snap = pivot._snaps.get(pivot._at(k))
            if snap is not None:
                _band(p, snap, 0, 0, w, h, pivot._x + k * w)


# ---- page turns ---------------------------------------------------------------------------
#
# Timed off recordings of the phone itself (see the toolkit): going into an
# entry, the page's rows leave one after another from the top, each turning
# away round the left edge, the tapped row last; a moment of black; the new
# page's rows arrive the same way. Going back, both turn from the bottom up.
# The first page arrives as the phone's Settings does: row after row from the
# top, each sliding home from the right as it turns in.

def _row_bands(page: QWidget, window: QWidget, tapped_y: Optional[float] = None, reverse: bool = False) -> list:
    h, w = float(window.height()), float(window.width())
    top_of_list = HEADER_H
    edges: list = []
    rows = page._rows() if hasattr(page, "_rows") else []
    for widget in rows:
        if widget.width() < w * 0.8:
            continue
        y = float(widget.mapTo(window, QPoint(0, 0)).y())
        if y < top_of_list:
            if not edges:
                edges.append(float(top_of_list))     # under the title: part of a row
            continue
        if 0 < y < h and (not edges or y > edges[-1] + 8):
            edges.append(y)
    bands, top = [], 0.0
    for bottom in edges + [h]:
        if bottom > top:
            bands.append([top, bottom, len(bands)])
            top = bottom
    if tapped_y is not None:
        hit = next((i for i, (y0, y1, _) in enumerate(bands) if y0 <= tapped_y < y1 and y0 > 0), None)
        if hit is not None:
            # the tapped one goes last; those after it close up
            for band in bands[hit + 1:]:
                band[2] -= 1
            bands[hit][2] = len(bands) - 1
    if reverse:
        for band in bands:
            band[2] = len(bands) - 1 - band[2]
    return bands


def _rows_ms(bands: list, row_ms: float, stagger: float = ROW_STAGGER_MS) -> float:
    return (max(b[2] for b in bands) * stagger + row_ms) if bands else 0.0


class _Turnstile(_Moving):
    def __init__(self, host: QWidget, before: Optional[QPixmap], out_rows: list, after: Optional[QPixmap],
                 in_rows: list, back: bool, entrance: bool = False, start: Optional[float] = None,
                 done: Optional[Callable] = None):
        super().__init__(host)
        self.setGeometry(host.rect())
        self.before, self.after, self.out_rows, self.in_rows = before, after, out_rows, in_rows
        self.back, self.entrance, self.done = back, entrance, done
        self.out_ms = _rows_ms(out_rows, ROW_OUT_MS) if before is not None else 0.0
        self.in_start = self.out_ms + (GAP_MS if before is not None and after is not None else 0)
        if after is None:
            self.total = self.out_ms
        elif entrance:
            last = max((b[2] for b in in_rows), default=0)
            self.total = self.in_start + max(last * ENTER_STAGGER_MS + ENTER_ROW_MS, ENTER_TITLE_MS)
        else:
            self.total = self.in_start + _rows_ms(in_rows, ROW_IN_MS)
        self.start = start if start is not None else _now()
        self.ms = 0.0
        self.ticker = _Ticker(self, self._step)
        # Drawn by the CPU for now: a GPU layer over the whole window is not put
        # on the screen by the shell (on the phone, recorded: the entrance played
        # unseen), where one over part of it (the tabs) is. Until that is found,
        # whole-window turns are drawn here, at half resolution, in strips.
        self.layer = None
        if self.layer is not None:
            for which, picture in (("before", before), ("after", after)):
                if picture is not None:
                    self.layer.upload((id(self), which), picture.toImage())
            self.setAttribute(Qt.WA_TransparentForMouseEvents)
            self.layer.show(host.rect(), self.quads)
        else:
            self.show()
            self.raise_()
        self.ticker.start()

    def hurry(self):
        """A finger does not wait for the first page to finish gliding in."""
        if self.entrance and _now() - self.start > ENTER_HOLD_MS:
            self.start = _now() - self.total

    def _step(self, now) -> bool:
        slow = max(1.0, float(os.environ.get("ZTOS_SLOW", "1") or 1))     # for checking it: slow motion
        self.ms = (now - self.start) / slow
        if self.ms >= self.total:
            self.finish()
            return False
        if self.layer is not None:
            self.layer.update()
        else:
            self.update()
        return True

    def finish(self):
        _settle()
        self.ticker.stop()
        if self.layer is not None:
            self.layer.hide()
            self.layer.drop((id(self), "before"))
            self.layer.drop((id(self), "after"))
        self.hide()
        self.deleteLater()
        if self.done:
            done, self.done = self.done, None
            done()

    def _plan(self) -> list:
        """Each row's state this frame: (which page, y0, y1, degrees, alpha, travel)."""
        plan = []
        if self.ms < 0:
            return plan             # an entrance set to start a moment from now: black until then
        if self.before is not None and self.ms < self.out_ms:
            for y0, y1, order in self.out_rows:
                t = _clamp01((self.ms - order * ROW_STAGGER_MS) / ROW_OUT_MS)
                e = t * t           # gathering speed as it goes
                alpha = 1.0 if e < 0.7 else (1 - e) / 0.3
                plan.append(("before", y0, y1, (TURN_OUT_DEG if self.back else -TURN_OUT_DEG) * e, alpha, 0.0))
        if self.after is not None and self.ms >= self.in_start:
            for y0, y1, order in self.in_rows:
                title = y0 < 1
                travel = 0.0
                if self.entrance:
                    life = ENTER_TITLE_MS if title else ENTER_ROW_MS
                    t = _clamp01((self.ms - self.in_start - order * ENTER_STAGGER_MS) / life)
                    left = (1 - t) ** 4
                    travel = (ENTER_TITLE_TRAVEL if title else ENTER_TRAVEL) * left
                    e = 1 - left
                else:
                    t = _clamp01((self.ms - self.in_start - order * ROW_STAGGER_MS) / ROW_IN_MS)
                    e = 1 - (1 - t) ** 3    # settling as it lands
                if t <= 0:
                    continue        # not set off yet
                alpha = 1.0 if e > 0.3 else e / 0.3
                angle = (0.0 if title and self.entrance else TURN_IN_DEG) * (1 - e)
                plan.append(("after", y0, y1, angle, alpha, travel))
        return plan

    def compose(self, p: QPainter):
        w, h = float(self.width()), float(self.height())
        for which, y0, y1, degrees, alpha, travel in self._plan():
            self._row(p, self.before if which == "before" else self.after, y0, y1, degrees, alpha, travel, w, h)

    def quads(self) -> list:
        """The same frame for the GPU: each row one quad, turned in true perspective."""
        from ._gl import Quad
        w, h = float(self.width()), float(self.height())
        d = _eye(h)
        out = []
        for which, y0, y1, degrees, alpha, travel in self._plan():
            a = math.radians(degrees)
            sa, ca = math.sin(a), math.cos(a)
            cx, cy = w / 2.0, (y0 + y1) / 2.0
            corners = []
            for x, y in ((0.0, y0), (w, y0), (0.0, y1), (w, y1)):
                den = (d + x * sa) / d
                corners.append((x * (cx * sa + d * ca) / d + travel * den, (x * cy * sa + d * y) / d, den))
            out.append(Quad((id(self), which), QRectF(0, y0 / h, 1, (y1 - y0) / h), corners, alpha))
        return out

    @staticmethod
    def _row(p: QPainter, pixmap: QPixmap, y0, y1, degrees, alpha, travel, w, h):
        if alpha <= 0.01:
            return
        p.save()
        p.setOpacity(min(1.0, alpha))
        if abs(degrees) < 0.05:
            _band(p, pixmap, 0, y0, w, y1, round(travel))
        else:
            _turned_strip(p, pixmap, y0, y1, degrees, w, h, travel)
        p.restore()


SLICES = 16


def _turned_strip(p: QPainter, pixmap: QPixmap, y0: float, y1: float, degrees: float, w: float, h: float,
                  travel: float = 0.0):
    """A band of the page turned round its left edge, seen from in front of the
    screen's middle (across) and its own middle (up and down): as the toolkit
    draws it, in SLICES upright strips, each placed and scaled for its own depth,
    so that every strip is a plain scaled copy (quick) and not a perspective one."""
    a = math.radians(degrees)
    d = _eye(h)
    cx, cy = w / 2.0, (y0 + y1) / 2.0
    sa, ca = math.sin(a), math.cos(a)
    r = pixmap.devicePixelRatio()
    for k in range(SLICES):
        x0, x1 = w * k / SLICES, w * (k + 1) / SLICES
        xm = (x0 + x1) / 2
        s0, s1, sm = d / (d + x0 * sa), d / (d + x1 * sa), d / (d + xm * sa)
        px0, px1 = cx + (x0 * ca - cx) * s0, cx + (x1 * ca - cx) * s1
        if px1 - px0 < 0.05 or sm <= 0:
            continue        # edge on
        top = cy - (cy - y0) * sm
        p.drawPixmap(QRectF(px0 + travel, top, px1 - px0, (y1 - y0) * sm), pixmap,
                     QRectF(x0 * r, y0 * r, (x1 - x0) * r, (y1 - y0) * r))


# ---- the app -----------------------------------------------------------------------------

_STYLE = """
QWidget { color: white; font-family: %(fonts)s; font-size: 15px; }
#ztosRoot, #ztosPage, #ztosBody { background: black; }
QScrollArea, QScrollArea > QWidget > QWidget { background: black; border: none; }
QScrollBar { width: 0px; height: 0px; }
QLabel { background: transparent; }
QPushButton { background: transparent; border: none; border-radius: 4px; padding: 6px 14px; min-height: 26px;
    font-size: 16px; }
QPushButton:pressed { background: %(accent)s; color: white; }
QPushButton:focus { outline: none; }
QPushButton:disabled { color: #666; }
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QDateEdit, QTimeEdit {
    background: #c7c7c7; color: black; border: 2px solid #c7c7c7; padding: 4px 8px; font-size: 17px;
    selection-background-color: %(accent)s; selection-color: white; }
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus { background: white; border-color: white; }
QCheckBox, QRadioButton { spacing: 12px; font-size: 17px; background: transparent; }
QCheckBox::indicator, QRadioButton::indicator { width: 22px; height: 22px; border: 2px solid white;
    background: black; }
QRadioButton::indicator { border-radius: 13px; }
QCheckBox::indicator:checked, QRadioButton::indicator:checked { background: %(accent)s; }
QComboBox { background: black; border: 2px solid #999; padding: 6px 10px; font-size: 17px; }
QComboBox::drop-down { width: 0px; border: none; }
QComboBox QAbstractItemView { background: black; color: white; border: 2px solid white;
    selection-background-color: %(accent)s; font-size: 22px; }
QListWidget, QListView, QTreeView, QTableView { background: black; border: none; font-size: 22px; }
QListWidget::item, QListView::item { padding: 8px 0px; }
QListWidget::item:selected, QListView::item:selected { background: transparent; color: %(accent)s; }
QProgressBar { background: #383838; border: none; max-height: 4px; text-align: center; color: transparent; }
QProgressBar::chunk { background: %(accent)s; }
QSlider::groove:horizontal { height: 4px; background: #383838; }
QSlider::sub-page:horizontal { background: %(accent)s; }
QSlider::handle:horizontal { width: 10px; height: 18px; margin: -7px 0px; background: white; }
QTabBar::tab { background: transparent; color: #616161; font-size: 28px; padding: 4px 12px 4px 0px; }
QTabBar::tab:selected { color: white; }
QTabWidget::pane { border: none; }
QGroupBox { border: none; margin-top: 22px; font-size: 13px; color: #999; }
QGroupBox::title { subcontrol-origin: margin; left: 0px; }
QToolTip { background: white; color: black; border: none; }
"""


class App:
    """The app: its window, and the pages in it one in front of another."""
    current: Optional["App"] = None

    def __init__(self, argv: Optional[list] = None):
        self.qt = QApplication.instance() or QApplication(argv if argv is not None else sys.argv)
        info = manifest()
        self.id = info.get("id", "ztos.app")
        self.name = info.get("name", "App")
        self.qt.setApplicationName(self.name)
        self.qt.setApplicationDisplayName(self.name)
        # the window's app_id: "ztos." tells the shell this app brings its own
        # rows in as the phone's apps do (it then only uncovers it)
        self.qt.setDesktopFileName(f"ztos.{self.id}")
        self.qt.setFont(_font(FONT_REGULAR, 15))
        fonts = ", ".join(f'"{f}"' if " " in f else f for f in FONT_REGULAR)
        self.qt.setStyleSheet(_STYLE % {"fonts": fonts, "accent": accent().name()})
        App.current = self
        self._dialogs: list = []
        if info.get("performance") in ("eco", "big"):
            performance(info["performance"])
        self.window = _Window(self)
        self.window.setObjectName("ztosRoot")
        self.window.setWindowTitle(self.name)
        self._layout = QStackedLayout(self.window)
        self.stack: list = []
        self.last_press_y: Optional[float] = None
        self._turn: Optional[_Turnstile] = None
        self.gl = None              # the GPU layer, once there is a window to put it on
        self._gl_tried = False
        self._finger = _Finger(self)
        self.qt.installEventFilter(self._finger)
        _install_profiler(self)
        _phase("app made")

    @property
    def page(self) -> Optional[QWidget]:
        return self.stack[-1] if self.stack else None

    def turning(self) -> bool:
        if self._turn is not None and self._turn.entrance:
            self._turn.hurry()
            return False
        return self._turn is not None

    def _snap(self) -> QPixmap:
        return _snapshot(self.window)

    def _turnstile(self, before, out_rows, after, in_rows, back, entrance=False, start=None, done=None):
        def finished():
            self._turn = None
            if done:
                done()
        if self._turn is not None:
            self._turn.done = None
            self._turn.finish()
        self._turn = _Turnstile(self.window, before, out_rows, after, in_rows, back, entrance, start, finished)

    def push(self, page: QWidget):
        """Go deeper: the rows of the page in front turn away, the tapped one
        last, and `page`'s rows turn in."""
        old = self.page
        if old is None:
            self.stack.append(page)
            self._place(page)
            return
        before = self._snap()
        out_rows = _row_bands(old, self.window, self.last_press_y)
        self.stack.append(page)
        self._place(page)
        self._turnstile(before, out_rows, self._snap(), _row_bands(page, self.window), back=False)
        self._arrived(page)

    def pop(self) -> bool:
        """Back a page; False when this was the first page."""
        if len(self.stack) < 2:
            return False
        before = self._snap()
        gone = self.stack.pop()
        out_rows = _row_bands(gone, self.window, reverse=True)
        page = self.stack[-1]
        self._place(page)
        self._layout.removeWidget(gone)
        gone.hide()
        self._turnstile(before, out_rows, self._snap(), _row_bands(page, self.window, reverse=True), back=True)
        self._arrived(page)
        return True

    def replace(self, page: QWidget):
        """Put `page` in front in place of the one there, without a way back to it."""
        old = self.stack.pop() if self.stack else None
        if old is not None:
            self.stack.append(old)          # turn away from it, then forget it
        self.push(page)
        if old is not None:
            self.stack.remove(old)
            self._layout.removeWidget(old)
            old.hide()

    def back(self):
        if self.turning():
            return
        if self._dialogs:
            self._dialogs[-1].close_with(None)      # a message first
            return
        page = self.page
        if page is not None and hasattr(page, "back") and page.back():
            return
        if not self.pop():
            self.close()

    def close(self):
        """The first page's rows turn away from the bottom up, and the app ends."""
        page = self.page
        if page is None or not self.window.isVisible():
            self.qt.quit()
            return
        self._turnstile(self._snap(), _row_bands(page, self.window, reverse=True), None, [], back=True,
                        done=self.qt.quit)

    def _place(self, page: QWidget):
        _settle()
        if self._layout.indexOf(page) < 0:
            self._layout.addWidget(page)
        self._layout.setCurrentWidget(page)
        page.layout() and page.layout().activate()
        if isinstance(page, Page):
            page._fit()

    def _arrived(self, page: QWidget):
        # shown() is where an app loads what a page shows: once the page has
        # finished arriving, not while it is turning in
        if hasattr(page, "shown"):
            self._when_still(page.shown)
        for p in ([page.current()] if isinstance(page, Pivot) else [page]):
            if isinstance(p, Page):
                p._gl_idle.start()

    def notify(self, title: str, text: str = ""):
        notify(title, text)

    def run(self, first: QWidget) -> int:
        """Show `first` and run until the app is closed. Returns the exit code."""
        self.stack.append(first)
        self._place(first)
        if not on_device():
            self.window.resize(360, 640)        # the phone's size, to try the app on the laptop
        # the first frame is black, as the phone's apps' are: the shell uncovers
        # the window at once and the rows come in over black (not over themselves)
        self._cover = _Loading(self.window)
        self._cover.setGeometry(self.window.rect())
        self._cover.raise_()
        self.window.show()
        self._entrance_due = True
        self._arrived(first)
        shot = os.environ.get("ZTOS_SHOT")
        if shot:
            # for the SDK: a picture of the app after it has settled, then quit
            def take():
                self.window.grab().save(shot)
                self.qt.quit()
            QTimer.singleShot(int(os.environ.get("ZTOS_SHOT_AFTER", "1500")), take)
        return self.qt.exec()

    def _when_still(self, work: Callable):
        """Do `work` once nothing is animating (at once if nothing is)."""
        def attempt():
            if _animating():
                QTimer.singleShot(50, attempt)
            else:
                work()
        QTimer.singleShot(0, attempt)

    def _make_gl(self):
        """The GPU layer, if this phone (or computer) has one to give: else
        everything moves the plain way, drawn by the CPU."""
        if self._gl_tried:
            return
        self._gl_tried = True
        if os.environ.get("ZTOS_GL", "1") == "0" or QGuiApplication.platformName() in ("offscreen", "minimal"):
            return
        try:
            from ._gl import Layer
            self.gl = Layer(self.window)
            self.gl.warm()
        except Exception as e:     # no QtOpenGL, no EGL: the plain way still works
            print(f"ztos: no GPU layer ({e}); moving things are drawn by the CPU", file=sys.stderr)
        _phase("window up, GPU layer asked for")

    def _entrance(self):
        """First time on screen: the rows arrive. The shell says when (Start gone,
        the black after it), on the clock both read."""
        self._make_gl()
        if not getattr(self, "_entrance_due", False) or self.page is None:
            if getattr(self, "_cover", None) is not None and not getattr(self, "_entrance_due", False):
                self._cover.hide()
            return
        # the GPU layer is made in a frame or two: the entrance waits for it (the
        # screen is black until it starts anyway), at most 300 ms
        waited = getattr(self, "_gl_waited", 0)
        if self.gl is not None and not self.gl.initialized and not self.gl.broken and waited < 300:
            self._gl_waited = waited + 16
            QTimer.singleShot(16, self._entrance)
            return
        self._entrance_due = False
        start = None
        enter, stamp = os.environ.get("ZTMETRO_ENTER_MS"), os.environ.get("ZTMETRO_LAUNCH_MS")
        if enter or stamp:
            at = int(enter) if enter else int(stamp) + 340 + 80
            now = int(_now()) & 0xffffffff
            ahead = (at - now) & 0xffffffff
            if 0 < ahead < 6000:
                start = _now() + ahead
        page = self.page
        page.layout() and page.layout().activate()
        if isinstance(page, (Page, Pivot)):
            (page.current() if isinstance(page, Pivot) else page)._fit()
        cover = self._cover
        cover.hide()                     # (out of the picture of the page)
        picture = self._snap()
        if _layer_of(self.window) is None:
            cover.show()                 # the CPU-drawn entrance goes over it (raised below)
            cover.raise_()
        # (on the GPU it stays hidden: the layer draws the black itself, and a
        # widget raised over a window container is given a surface of its own
        # by Qt, above the layer: the entrance played unseen under it)
        start = None                     # now: the shell uncovers the window as it hears "ready"
        _ctl(f"ready ztos.{self.id}")

        def uncover():
            cover.hide()
            cover.deleteLater()
            _phase("entrance done")
        _phase("entrance starts" + (f" (shell asked for +{int(start - _now())} ms)" if start else " now") +
               (", on the GPU" if _layer_of(self.window) else ", drawn by the CPU"))
        self._turnstile(None, [], picture, _row_bands(page, self.window), back=False, entrance=True, start=start,
                        done=uncover)
        if self._turn is not None and self._turn.layer is None:
            self._turn.raise_()          # the CPU-drawn turn above the cover
        _phase("entrance under way")


class _Window(QWidget):
    def __init__(self, app: App):
        super().__init__()
        self.app = app

    def showEvent(self, event):
        super().showEvent(event)
        _phase("window shown")
        # The entrance starts once the shell is showing the window. The loading
        # frame is painted first (see _Loading); after that, Wayland's frame
        # callbacks (what requestUpdate is answered by) say the shell has put
        # it on the screen: the second one is after it was seen. At most 1.5 s.
        self._frames_seen = 0
        if self.windowHandle() is None or QGuiApplication.platformName() in ("offscreen", "minimal"):
            QTimer.singleShot(0, self.app._entrance)
            return
        QTimer.singleShot(1500, self._seen)

    def _painted(self):
        # The first frame is painted (and committed): the window is mapped. The
        # shell keeps it behind "Loading..." until "ready", so nothing here
        # waits for it to be seen: straight on to preparing the entrance.
        _phase("first frame painted")
        QTimer.singleShot(0, self._seen)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.UpdateRequest and self._frames_seen < 2:
            self._frames_seen += 1
            if self._frames_seen < 2:
                obj.requestUpdate()
            else:
                QTimer.singleShot(0, self._seen)
        return False

    def _seen(self):
        if getattr(self, "_entered", False):
            return
        self._entered = True
        _phase("shell showing the window")
        self.app._entrance()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.app._turn is not None:
            self.app._turn.setGeometry(self.rect())
        cover = getattr(self.app, "_cover", None)
        if cover is not None:
            try:
                cover.setGeometry(self.rect())
            except RuntimeError:
                pass        # (already gone)


class _Keys(QObject):
    """Back (the phone's back key; Escape on a keyboard) goes up a page."""

    def __init__(self, app: App):
        super().__init__(app.qt)
        self.app = app

    def eventFilter(self, obj, event):
        if event.type() == QEvent.KeyPress and event.key() in (Qt.Key_Back, Qt.Key_Escape) \
                and obj is QGuiApplication.focusWindow():
            self.app.back()
            return True
        return False


# ---- progress, loading, dialogs --------------------------------------------------------

class LoadingDots(QWidget):
    """The phone's loading dots: five of them cross from the left, quick at the
    edges and slow through the middle, then a pause (as the toolkit's)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._motion = True         # its paints are motion, not a change of the page
        self.setFixedHeight(8)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._since = _now()
        self._ticker = _Ticker(self, self._tick)

    def showEvent(self, event):
        self._since = _now()
        self._ticker.start()

    def hideEvent(self, event):
        self._ticker.stop()

    def _tick(self, now) -> bool:
        self.update()
        return self.isVisible()

    def paintEvent(self, event):
        p = QPainter(self)
        t = (_now() - self._since) % 2600
        color = accent()
        w = float(self.width())
        for i in range(5):
            k = (t - i * 160) / 1700.0
            if 0 < k < 1:
                u = 2 * k - 1
                x = w * (0.5 + 0.5 * (0.25 * u + 0.75 * u * u * u))
                p.fillRect(QRectF(x - 1.5, 3, 3, 3), color)


class _Loading(QWidget):
    """The app's first frames: black. The shell draws "Loading..." and its dots
    over the window until the app says it is ready, so that nothing animated
    is drawn by this thread while it is busy starting."""

    def __init__(self, parent):
        super().__init__(parent)
        self._motion = True
        self.setAttribute(Qt.WA_OpaquePaintEvent)

    def paintEvent(self, event):
        if not getattr(self, "_painted", False):
            self._painted = True        # painted, and committed right after: now count frames
            window = self.window()
            if hasattr(window, "_painted"):
                QTimer.singleShot(0, window._painted)
        QPainter(self).fillRect(event.rect(), QColor("black"))


class ProgressBar(QWidget):
    """A thin bar in the accent colour. set_value() glides to the new value
    (quick, then easing); with no value (indeterminate) it shows the dots."""

    def __init__(self, value: Optional[float] = 0, maximum: float = 100, caption: str = "", parent=None):
        super().__init__(parent)
        self._motion = True
        self._max = float(maximum) or 1.0
        self._caption = caption
        self._shown = 0.0           # 0..1 as drawn
        self._anim = QVariantAnimation(self, duration=320, easingCurve=QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._moved)
        self._anim.finished.connect(self._settled)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._dots = LoadingDots()
        layout.addStretch(1)
        layout.addWidget(self._dots)
        self.setFixedHeight(28 if caption else 12)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._value: Optional[float] = None
        self.set_value(value, animate=False)

    def value(self) -> Optional[float]:
        return self._value

    def set_value(self, value: Optional[float], animate: bool = True):
        """A number up to the maximum, or None for "working on it" (the dots)."""
        self._value = value
        self._dots.setVisible(value is None)
        if value is None:
            self.update()
            return
        target = max(0.0, min(1.0, float(value) / self._max))
        self._anim.stop()
        if animate and self.isVisible():
            self._anim.setStartValue(self._shown)
            self._anim.setEndValue(target)
            self._anim.start()
        else:
            self._shown = target
            self.update()
            self._settled()

    def _moved(self, v):
        self._shown = float(v)
        self.update()

    def _settled(self):
        page = _ancestor(self.parentWidget(), Page)
        if page is not None:
            page._changed(explicit=True)    # its picture shows the value it came to

    def paintEvent(self, event):
        p = QPainter(self)
        w = float(self.width())
        y = self.height() - 8.0
        if self._caption:
            p.setPen(GREY)
            p.setFont(_font(FONT_REGULAR, 13))
            p.drawText(QPointF(0, QFontMetricsF(p.font()).ascent()), self._caption)
        if self._value is None:
            return                  # the dots, below the caption
        p.fillRect(QRectF(0, y, w, 4), QColor(56, 56, 56))
        p.fillRect(QRectF(0, y, w * self._shown, 4), accent())


class Dialog(QWidget):
    """A message from the top, as the phone's: the page goes dim, and a panel
    swings down from the top edge with a title, a message and buttons. A
    button's index is passed to `on_result`; back (or a tap on the dim part)
    gives None."""
    finished = Signal(object)

    def __init__(self, title: str, text: str = "", buttons: Iterable[str] = ("ok",),
                 on_result: Optional[Callable[[Optional[int]], None]] = None, content: Optional[QWidget] = None):
        app = App.current
        if app is None:
            raise RuntimeError("make the App first")
        super().__init__(app.window)
        self.app = app
        self.setGeometry(app.window.rect())
        self._dim = 0.0
        self._angle = -90.0
        self._picture: Optional[QPixmap] = None
        self._result: Optional[int] = None
        if on_result:
            self.finished.connect(on_result)
        self.panel = QWidget(self)
        self.panel.setObjectName("ztosDialog")
        self.panel.setAttribute(Qt.WA_StyledBackground, True)
        self.panel.setStyleSheet("#ztosDialog { background: #1f1f1f; }")
        box = QVBoxLayout(self.panel)
        box.setContentsMargins(MARGIN + 8, 40, MARGIN + 8, 20)
        box.setSpacing(12)
        heading = _label(title, FONT_SEMIBOLD, 20)
        box.addWidget(heading)
        if text:
            box.addWidget(_label(text, FONT_REGULAR, 15))
        if content is not None:
            box.addWidget(content)
        row = QHBoxLayout()
        row.setSpacing(12)
        for i, name in enumerate(buttons):
            b = QPushButton(name)
            b.setMinimumHeight(40)
            b.clicked.connect(lambda _=False, i=i: self.close_with(i))
            row.addWidget(b, 1)
        box.addSpacing(8)
        box.addLayout(row)
        self.panel.setFixedWidth(self.width())
        self.panel.adjustSize()
        self.panel.move(0, 0)
        self._anim = QVariantAnimation(self, startValue=0.0, endValue=1.0, duration=260,
                                       easingCurve=QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._step)
        self._closing = False
        app._dialogs.append(self)
        haptic("tap")
        self._picture = _snapshot(self.panel, self.panel.size())
        self.panel.hide()               # drawn as a picture while it swings
        self.show()
        self.raise_()
        self._anim.finished.connect(self._arrived)
        self._anim.start()

    def _step(self, t):
        t = float(t)
        self._dim = t if not self._closing else 1 - t
        self._angle = -90.0 * (1 - t) if not self._closing else -70.0 * t
        self.update()

    def _arrived(self):
        if self._closing:
            self.hide()
            self.deleteLater()
            if self in self.app._dialogs:
                self.app._dialogs.remove(self)
            self.finished.emit(self._result)
            return
        self.panel.show()               # the real one, with its buttons, now it is still
        self.update()

    def close_with(self, result: Optional[int]):
        """Swing it away and report `result` (a button's index, or None)."""
        if self._closing:
            return
        self._closing = True
        self._result = result
        self._picture = _snapshot(self.panel, self.panel.size())
        self.panel.hide()
        self._anim.stop()
        self._anim.setDuration(170)
        self._anim.setEasingCurve(QEasingCurve.InCubic)
        self._anim.start()

    def mousePressEvent(self, event):
        if event.position().y() > self.panel.height() and not self._closing:
            self.close_with(None)

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, int(160 * self._dim)))
        if self.panel.isVisible() or self._picture is None:
            return
        # the panel turned about its top edge: in bands, each placed and scaled
        # for its own depth (as page turns are drawn), seen from the screen's middle
        a = math.radians(self._angle)
        w, h = float(self.width()), float(self.panel.height())
        d = _eye(float(self.height()))
        r = self._picture.devicePixelRatio()
        bands = 12
        p.setOpacity(min(1.0, self._dim * 1.5))
        for k in range(bands):
            y0, y1 = h * k / bands, h * (k + 1) / bands
            s0, s1 = d / (d - y0 * math.sin(a)), d / (d - y1 * math.sin(a))
            top, bottom = y0 * math.cos(a) * s0, y1 * math.cos(a) * s1
            if bottom - top < 0.05:
                continue
            sm = (s0 + s1) / 2
            x0 = w / 2 - w / 2 * sm
            p.drawPixmap(QRectF(x0, top, w * sm, bottom - top), self._picture,
                         QRectF(0, y0 * r, w * r, (y1 - y0) * r))


def message(title: str, text: str = "", buttons: Iterable[str] = ("ok",),
            on_result: Optional[Callable[[Optional[int]], None]] = None) -> Dialog:
    """Show a message from the top; `on_result` gets the button's index (or None)."""
    return Dialog(title, text, buttons, on_result)


# ---- the phone ------------------------------------------------------------------------

def performance(mode: str = "normal") -> bool:
    """Tell the phone's power manager (zrnperfd) how this app wants to be
    treated while it is in front:
        "eco"     its work never brings the big cores in: fine on the small ones
        "big"     the big cores stay open to it (a game, a camera preview)
        "normal"  as any app: big cores only while it keeps a core busy
    Returns False if there is no power manager to tell."""
    if mode not in ("normal", "eco", "big"):
        raise ValueError("performance: normal, eco or big")
    try:
        fd = os.open("/run/zrnperfd.ctl", os.O_WRONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return False
    try:
        os.write(fd, f"policy {os.getpid()} {mode}\n".encode())
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def battery() -> dict:
    """{"percent": 0..100 or None, "charging": bool, "full": bool}"""
    base = "/sys/class/power_supply"
    out = {"percent": None, "charging": False, "full": False}
    try:
        for name in sorted(os.listdir(base)):
            kind = zmc_read(os.path.join(base, name, "type"))
            if kind == "Battery":
                pct = zmc_read(os.path.join(base, name, "capacity"))
                status = zmc_read(os.path.join(base, name, "status"))
                out["percent"] = int(pct) if pct.isdigit() else None
                out["charging"] = status == "Charging"
                out["full"] = status == "Full"
                break
    except OSError:
        pass
    return out


def zmc_read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def launch(command: str) -> bool:
    """Open another app (its command, as its .desktop file has it) in the shell."""
    return _ctl(f"launch {command}")


class Store(dict):
    """A dict kept on the phone for this app (JSON, in ~/.local/share/ztos/data/ID/):
    store = ztos.store(); store["name"] = "Ometha"; store.save()"""

    def __init__(self, name: str = "settings"):
        super().__init__()
        app_id = manifest().get("id", "ztos.app")
        folder = os.path.expanduser(f"~/.local/share/ztos/data/{app_id}")
        self.path = os.path.join(folder, f"{name}.json")
        try:
            with open(self.path) as f:
                self.update(json.load(f))
        except (OSError, ValueError):
            pass

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".new"
        with open(tmp, "w") as f:
            json.dump(self, f, indent=1)
        os.replace(tmp, self.path)


def store(name: str = "settings") -> Store:
    return Store(name)


# ---- profiling (off unless ~/.config/ztos/profile exists, or $ZTOS_PROFILE) ----------
#
# Where an app's time goes, every 2 s, into its log (ztos-sdk logs): calls,
# total and worst ms of each part the SDK runs per frame, against the CPU the
# whole process used. For finding what keeps a core busy.

def _profile_on() -> bool:
    return bool(os.environ.get("ZTOS_PROFILE")) or os.path.exists(os.path.expanduser("~/.config/ztos/profile"))


_profiling = False


def _phase(label: str):
    """With profiling on: how much CPU the app had used by this step of its
    start, and when it was (from the tap, on the shell's clock)."""
    if _profiling:
        tapped = os.environ.get("ZTMETRO_LAUNCH_MS")
        since = f"{(int(_now()) - int(tapped)) & 0xffffffff:6d} ms after the tap" if tapped else ""
        print(f"ztos start: {label:<32} {time.process_time() * 1000:7.0f} ms CPU  {since}", file=sys.stderr,
              flush=True)


def _install_profiler(app: "App"):
    """Wrap the parts the SDK runs per frame, and report every 2 s. Done when the
    app is made (the warm runtime imports this module long before)."""
    global _snapshot, _profiling
    if _profiling or not _profile_on():
        return
    _profiling = True
    prof: dict = {}
    last = [time.monotonic(), time.process_time()]

    def timed_as(owner, name, label):
        original = getattr(owner, name)

        def timed(*args, **kwargs):
            t = time.perf_counter()
            try:
                return original(*args, **kwargs)
            finally:
                ms = (time.perf_counter() - t) * 1000
                entry = prof.setdefault(label, [0, 0.0, 0.0])
                entry[0] += 1
                entry[1] += ms
                entry[2] = max(entry[2], ms)
        setattr(owner, name, timed)

    for owner, name in ((_PivotTitles, "paintEvent"), (_Ticker, "_tick"), (_Finger, "eventFilter"),
                        (Page, "_fling_step"), (Page, "_gl_prepare"), (Page, "_print"), (Page, "_verify"),
                        (Page, "_fit"), (Pivot, "_snap"), (Pivot, "_spring_step"), (Pivot, "_quads"),
                        (Entry, "paintEvent"), (Toggle, "paintEvent"), (_Moving, "paintEvent"),
                        (_Turnstile, "_step"), (_Turnstile, "__init__")):
        timed_as(owner, name, f"{owner.__name__}.{name}")
    try:
        # before any surface exists: Qt calls it from C++, by what the class had then
        from . import _gl as glmod
        timed_as(glmod._Surface, "paintGL", "GL.paintGL")
    except Exception:
        pass
    plain = _snapshot

    def timed_snapshot(widget, size=None):
        t = time.perf_counter()
        try:
            return plain(widget, size)
        finally:
            entry = prof.setdefault("_snapshot", [0, 0.0, 0.0])
            entry[0] += 1
            entry[1] += (time.perf_counter() - t) * 1000
    _snapshot = timed_snapshot

    def report():
        wall, cpu = time.monotonic(), time.process_time()
        span, used = wall - last[0], cpu - last[1]
        last[:] = [wall, cpu]
        if not prof:
            return
        lines = [f"ztos profile: {span:.1f} s, process CPU {used / span * 100:.0f}% of a core"]
        for label, (n, total, worst) in sorted(prof.items(), key=lambda kv: -kv[1][1]):
            lines.append(f"  {label:<28} {n:5d} calls {total:8.1f} ms  worst {worst:6.1f} ms")
        print("\n".join(lines), file=sys.stderr, flush=True)
        prof.clear()
    app._prof_timer = QTimer(app.qt, interval=2000)
    app._prof_timer.timeout.connect(report)
    app._prof_timer.start()
