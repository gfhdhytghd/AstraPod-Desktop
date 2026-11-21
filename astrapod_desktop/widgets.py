"""Painted widgets shared by the pages.

They are Material 3 components drawn in code where a style sheet cannot
reach: the switch, segmented buttons, linear and circular indicators, the
navigation drawer that folds into a rail, the extended FAB, and the ripple
every clickable control gets. Motion uses M3's emphasized curves and stops
when the animations setting is off.
"""
from __future__ import annotations

import math
import time

from . import fmt, icons
from .artwork import placeholder_colors
from .qt import FONT_NUMERIC, QtCore, QtGui, QtWidgets, Qt, Signal, ThemeTokens, qcolor

P = QtCore.QPointF
R = QtCore.QRectF


def repolish(widget: QtWidgets.QWidget) -> None:
    style = widget.style()
    if style is not None:
        style.unpolish(widget)
        style.polish(widget)
    widget.update()


def discard(widget: QtWidgets.QWidget) -> None:
    """Remove a widget now; deleteLater alone leaves it painted until the
    event loop next idles, at its unlaid-out 640x480 default size."""
    widget.hide()
    widget.setParent(None)
    widget.deleteLater()


# ----------------------------------------------------------------------
# Motion
# ----------------------------------------------------------------------
MOTION = {"on": True}

_CURVES = {
    "decelerate": (0.05, 0.7, 0.1, 1.0),
    "accelerate": (0.3, 0.0, 0.8, 0.15),
    "standard": (0.2, 0.0, 0.0, 1.0),
}


def set_motion(on: bool) -> None:
    MOTION["on"] = bool(on)


def emphasized(kind: str = "decelerate") -> QtCore.QEasingCurve:
    """M3's emphasized decelerate/accelerate and standard easing."""
    x1, y1, x2, y2 = _CURVES[kind]
    curve = QtCore.QEasingCurve(QtCore.QEasingCurve.Type.BezierSpline)
    curve.addCubicBezierSegment(P(x1, y1), P(x2, y2), P(1.0, 1.0))
    return curve


# ----------------------------------------------------------------------
# Ripple
# ----------------------------------------------------------------------
class _Ripple(QtWidgets.QWidget):
    """One ink ripple, clipped to its host's shape, deleted when faded."""

    def __init__(self, host: QtWidgets.QWidget, rect: QtCore.QRect, radius: float,
                 origin: QtCore.QPoint, color: QtGui.QColor):
        super().__init__(host)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setGeometry(rect)
        self.radius = radius
        self.origin = P(origin - rect.topLeft())
        self.color = color
        corners = [P(0, 0), P(rect.width(), 0), P(0, rect.height()), P(rect.width(), rect.height())]
        self.reach = max(math.hypot(c.x() - self.origin.x(), c.y() - self.origin.y()) for c in corners)
        self.grow = 0.0
        self.alpha = 1.0
        self.released = False
        self._grow = QtCore.QVariantAnimation(self)
        self._grow.setDuration(350)
        self._grow.setStartValue(0.0)
        self._grow.setEndValue(1.0)
        self._grow.setEasingCurve(emphasized("standard"))
        self._grow.valueChanged.connect(self._set_grow)
        self._grow.finished.connect(self._maybe_fade)
        self._fade = QtCore.QVariantAnimation(self)
        self._fade.setDuration(220)
        self._fade.setStartValue(1.0)
        self._fade.setEndValue(0.0)
        self._fade.valueChanged.connect(self._set_alpha)
        self._fade.finished.connect(self.deleteLater)
        self.show()
        self.raise_()
        self._grow.start()

    def _set_grow(self, value) -> None:
        self.grow = float(value)
        self.update()

    def _set_alpha(self, value) -> None:
        self.alpha = float(value)
        self.update()

    def release(self) -> None:
        self.released = True
        self._maybe_fade()

    def _maybe_fade(self) -> None:
        if self.released and self._grow.state() != QtCore.QAbstractAnimation.State.Running \
                and self._fade.state() != QtCore.QAbstractAnimation.State.Running:
            self._fade.start()

    def paintEvent(self, _event) -> None:
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        clip = QtGui.QPainterPath()
        clip.addRoundedRect(R(self.rect()), self.radius, self.radius)
        painter.setClipPath(clip)
        color = QtGui.QColor(self.color)
        color.setAlphaF(0.12 * self.alpha)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        r = 8 + (self.reach - 8) * self.grow
        painter.drawEllipse(self.origin, r, r)
        painter.end()


class RippleFilter(QtCore.QObject):
    """Installed on the application: a press on a button, or on an item
    view row, starts a ripple there. A widget opts out with the property
    no_ripple; a view shapes its ripples with ripple_rect(index)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tokens: ThemeTokens | None = None
        self._live: _Ripple | None = None

    def eventFilter(self, obj, event) -> bool:
        kind = event.type()
        if kind == QtCore.QEvent.Type.MouseButtonPress and MOTION["on"] and self.tokens is not None:
            try:
                self._press(obj, event)
            except RuntimeError:
                pass
        elif kind == QtCore.QEvent.Type.MouseButtonRelease and self._live is not None:
            try:
                self._live.release()
            except RuntimeError:
                pass
            self._live = None
        return False

    def _press(self, obj, event) -> None:
        if not isinstance(obj, QtWidgets.QWidget) or event.button() != Qt.MouseButton.LeftButton:
            return
        if obj.property("no_ripple") or not obj.isEnabled():
            return
        t = self.tokens
        pos = event.position().toPoint()
        if isinstance(obj, QtWidgets.QAbstractButton):
            variant = obj.property("variant")
            color = {"primary": t.on_primary, "danger": t.on_error,
                     "tonal": t.on_secondary_container,
                     "option": t.on_secondary_container}.get(variant, t.primary)
            radius = obj.property("ripple_radius")
            radius = float(radius) if radius is not None else min(20.0, obj.height() / 2)
            self._live = _Ripple(obj, obj.rect(), radius, pos, QtGui.QColor(color))
            return
        view = obj.parent()
        if isinstance(view, QtWidgets.QAbstractItemView) and obj is view.viewport() \
                and not view.property("no_ripple"):
            index = view.indexAt(pos)
            if not index.isValid() or not (index.flags() & Qt.ItemFlag.ItemIsEnabled):
                return
            shaper = getattr(view, "ripple_rect", None)
            if shaper is not None:
                rect, radius = shaper(index)
            else:
                rect, radius = view.visualRect(index), 0.0
                if isinstance(view, QtWidgets.QTableView):
                    rect = QtCore.QRect(0, rect.top(), obj.width(), rect.height())
            if rect.isValid():
                self._live = _Ripple(obj, rect, radius, pos, QtGui.QColor(t.on_surface))


# ----------------------------------------------------------------------
# Extended FAB
# ----------------------------------------------------------------------
class Fab(QtWidgets.QAbstractButton):
    """An M3 extended FAB: 56 px, primary container, icon and label, with
    its elevation shadow drawn inside the widget's margin."""

    MARGIN = 8

    def __init__(self, text: str, icon_name: str, parent=None):
        super().__init__(parent)
        self.setText(text)
        self.icon_name = icon_name
        self.tokens: ThemeTokens | None = None
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setProperty("no_ripple", True)
        self.setAccessibleName(text)
        self._font = QtGui.QFont(self.font())
        self._font.setPixelSize(14)
        self._font.setWeight(QtGui.QFont.Weight.Medium)
        width = QtGui.QFontMetrics(self._font).horizontalAdvance(text) + 16 + 24 + 12 + 20
        self.setFixedSize(width + 2 * self.MARGIN, 56 + 2 * self.MARGIN)
        self.setMouseTracking(True)

    def body(self) -> QtCore.QRectF:
        m = self.MARGIN
        return R(m, m - 2, self.width() - 2 * m, 56)

    def hitButton(self, pos) -> bool:  # noqa: N802 (Qt API)
        return self.body().contains(P(pos))

    def mousePressEvent(self, event) -> None:
        super().mousePressEvent(event)
        if MOTION["on"] and self.tokens is not None and self.isDown():
            body = self.body().toRect()
            _Ripple(self, body, 16, event.position().toPoint(),
                    QtGui.QColor(self.tokens.on_primary_container))

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        for child in self.findChildren(_Ripple):
            child.release()

    def enterEvent(self, event) -> None:
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:
        t = self.tokens
        if t is None:
            return
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        body = self.body()
        draw_shadow(painter, body, 16, 1.6 if self.underMouse() else 1.2, t)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QtGui.QColor(t.primary_container))
        painter.drawRoundedRect(body, 16, 16)
        fg = QtGui.QColor(t.on_primary_container)
        if self.underMouse() or self.hasFocus():
            layer = QtGui.QColor(fg)
            layer.setAlphaF(0.10 if self.hasFocus() else 0.08)
            painter.setBrush(layer)
            painter.drawRoundedRect(body, 16, 16)
        icons.paint(painter, self.icon_name, R(body.left() + 16, body.center().y() - 12, 24, 24), fg)
        painter.setFont(self._font)
        painter.setPen(fg)
        painter.drawText(body.adjusted(16 + 24 + 12, 0, -20, 0), Qt.AlignmentFlag.AlignVCenter, self.text())
        painter.end()


def set_badge(label: QtWidgets.QLabel, text: str, tone: str = "neutral") -> None:
    label.setText(text)
    if label.property("tone") != tone:
        label.setProperty("tone", tone)
        repolish(label)


def make_badge(text: str, tone: str = "neutral") -> QtWidgets.QLabel:
    badge = QtWidgets.QLabel(text)
    badge.setProperty("badge", True)
    badge.setProperty("tone", tone)
    badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
    badge.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Fixed)
    return badge


def make_card(spacing: int = 12, expand: bool = False, margins=(18, 16, 18, 16)):
    card = QtWidgets.QFrame()
    card.setProperty("card", True)
    card.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                       QtWidgets.QSizePolicy.Policy.Expanding if expand
                       else QtWidgets.QSizePolicy.Policy.Maximum)
    layout = QtWidgets.QVBoxLayout(card)
    layout.setContentsMargins(*margins)
    layout.setSpacing(spacing)
    return card, layout


def label(text: str = "", role: str | None = None, wrap: bool = False) -> QtWidgets.QLabel:
    widget = QtWidgets.QLabel(text)
    if role:
        widget.setProperty("role", role)
    widget.setWordWrap(wrap)
    return widget


def button(text: str, variant: str | None = None, icon_name: str | None = None,
           slot=None) -> QtWidgets.QPushButton:
    widget = QtWidgets.QPushButton(text)
    if variant:
        widget.setProperty("variant", variant)
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    if icon_name:
        widget.setProperty("icon_name", icon_name)
    if slot is not None:
        widget.clicked.connect(slot)
    return widget


def refresh_button_icons(root: QtWidgets.QWidget, tokens: ThemeTokens) -> None:
    """Re-tint every button that carries an icon_name property."""
    for widget in root.findChildren(QtWidgets.QAbstractButton):
        name = widget.property("icon_name")
        if name:
            variant = widget.property("variant")
            color = tokens.text_on_accent if variant in ("primary", "danger") else tokens.text_secondary
            widget.setIcon(icons.icon(name, color, 16))


class PageHeader(QtWidgets.QWidget):
    """Serif title, a muted line under it, and actions on the right."""

    def __init__(self, title: str, subtitle: str = "", parent=None):
        super().__init__(parent)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        column = QtWidgets.QVBoxLayout()
        column.setSpacing(0)
        self.title = label(title, "page-title")
        self.subtitle = label(subtitle, "page-subtitle")
        column.addWidget(self.title)
        column.addWidget(self.subtitle)
        layout.addLayout(column)
        layout.addStretch(1)
        self.actions = QtWidgets.QHBoxLayout()
        self.actions.setSpacing(8)
        layout.addLayout(self.actions)

    def add(self, widget: QtWidgets.QWidget) -> QtWidgets.QWidget:
        self.actions.addWidget(widget)
        return widget


# ----------------------------------------------------------------------
# Form controls
# ----------------------------------------------------------------------
class ChevronComboBox(QtWidgets.QComboBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._chevron = "#64748B"
        self._disabled = "#94A3B8"

    def update_theme(self, tokens: ThemeTokens) -> None:
        self._chevron = tokens.text_secondary
        self._disabled = tokens.text_disabled
        self.update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        pen = QtGui.QPen(QtGui.QColor(self._chevron if self.isEnabled() else self._disabled), 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        cx, cy = self.width() - 14.0, self.height() / 2.0
        path = QtGui.QPainterPath(P(cx - 4, cy - 1.8))
        path.lineTo(P(cx, cy + 2.2))
        path.lineTo(P(cx + 4, cy - 1.8))
        painter.drawPath(path)
        painter.end()


class PasswordLineEdit(QtWidgets.QLineEdit):
    def __init__(self, placeholder: str = "", parent=None):
        super().__init__(parent)
        from podlink_ui_theme import make_eye_icon
        self._eye = make_eye_icon
        self.setPlaceholderText(placeholder)
        self.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self._revealed = False
        self._color = "#64748B"
        self._toggle = self.addAction(make_eye_icon(False, self._color),
                                      QtWidgets.QLineEdit.ActionPosition.TrailingPosition)
        self._toggle.setToolTip("显示内容")
        self._toggle.triggered.connect(self.toggle_revealed)

    def toggle_revealed(self) -> None:
        self.set_revealed(not self._revealed)

    def set_revealed(self, revealed: bool) -> None:
        self._revealed = bool(revealed)
        self.setEchoMode(QtWidgets.QLineEdit.EchoMode.Normal if self._revealed
                         else QtWidgets.QLineEdit.EchoMode.Password)
        self._toggle.setToolTip("隐藏内容" if self._revealed else "显示内容")
        self._toggle.setIcon(self._eye(self._revealed, self._color))

    def clear_secret(self) -> None:
        self.clear()
        self.set_revealed(False)

    def update_theme(self, tokens: ThemeTokens) -> None:
        self._color = tokens.text_muted
        self._toggle.setIcon(self._eye(self._revealed, self._color))


class SegmentedControl(QtWidgets.QFrame):
    changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("segmented", True)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Fixed)
        self._layout = QtWidgets.QHBoxLayout(self)
        self._layout.setContentsMargins(1, 1, 1, 1)
        self._layout.setSpacing(0)
        self._tokens: ThemeTokens | None = None
        self._group = QtWidgets.QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons: dict[str, QtWidgets.QPushButton] = {}
        self._icon_modes: dict[str, str] = {}
        self._current: str | None = None

    def add_option(self, key: str, text: str, icon_mode: str | None = None):
        widget = QtWidgets.QPushButton(text, self)
        widget.setCheckable(True)
        widget.setProperty("segment", True)
        widget.setCursor(Qt.CursorShape.PointingHandCursor)
        widget.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        widget.clicked.connect(lambda _c=False, k=key: self._click(k))
        self._group.addButton(widget)
        self._layout.addWidget(widget)
        if not self._buttons:
            widget.setProperty("first", True)
        for other in self._buttons.values():
            if other.property("last"):
                other.setProperty("last", False)
                repolish(other)
        widget.setProperty("last", True)
        widget.toggled.connect(lambda _on: self._refresh_icons())
        self._buttons[key] = widget
        if icon_mode is not None:
            self._icon_modes[key] = icon_mode
            widget.setIconSize(QtCore.QSize(14, 14))
        return widget

    def _click(self, key: str) -> None:
        if key == self._current:
            self._buttons[key].setChecked(True)
            return
        self._current = key
        self.changed.emit(key)

    def set_current(self, key: str, emit: bool = False) -> None:
        widget = self._buttons.get(key)
        if widget is None:
            return
        changed = key != self._current
        self._current = key
        widget.setChecked(True)
        if emit and changed:
            self.changed.emit(key)

    def current(self) -> str | None:
        return self._current

    def button(self, key: str):
        return self._buttons.get(key)

    def set_options_enabled(self, enabled: bool) -> None:
        for widget in self._buttons.values():
            widget.setEnabled(enabled)

    def update_theme(self, tokens: ThemeTokens) -> None:
        self._tokens = tokens
        self._refresh_icons()

    def _refresh_icons(self) -> None:
        """M3 segments show a check on the selected one."""
        tokens = self._tokens
        if tokens is None:
            return
        from podlink_ui_theme import make_theme_icon
        for key, widget in self._buttons.items():
            on = widget.isChecked()
            color = tokens.on_secondary_container if on else tokens.on_surface
            if on:
                widget.setIcon(icons.icon("check", color, 18))
                widget.setIconSize(QtCore.QSize(18, 18))
            elif key in self._icon_modes:
                widget.setIcon(make_theme_icon(self._icon_modes[key], color, 18))
                widget.setIconSize(QtCore.QSize(18, 18))
            else:
                widget.setIcon(QtGui.QIcon())


class ToggleSwitch(QtWidgets.QAbstractButton):
    """The M3 switch: a 52x32 track whose handle grows and shows a check
    when on; Space toggles it."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setProperty("no_ripple", True)
        self.setFixedSize(56, 36)
        self.setMouseTracking(True)
        self._pos = 0.0
        self._anim = QtCore.QVariantAnimation(self)
        self._anim.setDuration(250)
        self._anim.setEasingCurve(emphasized())
        self._anim.valueChanged.connect(self._step)
        self.toggled.connect(self._animate)
        self.tokens: ThemeTokens | None = None

    def _step(self, value) -> None:
        self._pos = float(value)
        self.update()

    def _animate(self, on: bool) -> None:
        self._anim.stop()
        if not MOTION["on"]:
            self._step(1.0 if on else 0.0)
            return
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def setChecked(self, on: bool) -> None:  # noqa: N802 (Qt API)
        super().setChecked(on)
        self._anim.stop()
        self._pos = 1.0 if on else 0.0
        self.update()

    def enterEvent(self, event) -> None:
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:
        t = self.tokens
        if t is None:
            return
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        if not self.isEnabled():
            painter.setOpacity(0.38)
        p = self._pos
        track = R(2, 2, 52, 32)
        off_fill, on_fill = QtGui.QColor(t.surface_container_highest), QtGui.QColor(t.primary)
        painter.setPen(QtGui.QPen(QtGui.QColor(t.outline), 2) if p < 0.5 else Qt.PenStyle.NoPen)
        painter.setBrush(mix(off_fill, on_fill, p))
        painter.drawRoundedRect(track.adjusted(1, 1, -1, -1) if p < 0.5 else track, 16, 16)
        pressed = self.isDown()
        size = 28.0 if pressed else 16.0 + 8.0 * p
        cx = track.left() + 16 + 20 * p
        center = P(cx, track.center().y())
        if self.underMouse() or self.hasFocus():
            layer = QtGui.QColor(t.primary if p >= 0.5 else t.on_surface)
            layer.setAlphaF(0.10 if self.hasFocus() else 0.08)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(layer)
            painter.drawEllipse(center, 20, 20)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(mix(QtGui.QColor(t.outline), QtGui.QColor(t.on_primary), p))
        painter.drawEllipse(center, size / 2, size / 2)
        if p > 0.5:
            check = QtGui.QColor(t.on_primary_container)
            check.setAlphaF(min(1.0, (p - 0.5) * 2))
            icons.paint(painter, "check", R(cx - 8, center.y() - 8, 16, 16), check)
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QtGui.QPen(QtGui.QColor(t.secondary), 2))
            painter.drawRoundedRect(track.adjusted(-1, -1, 1, 1), 17, 17)
        painter.end()


def mix(a: QtGui.QColor, b: QtGui.QColor, amount: float) -> QtGui.QColor:
    amount = max(0.0, min(1.0, amount))
    return QtGui.QColor.fromRgbF(a.redF() + (b.redF() - a.redF()) * amount,
                                 a.greenF() + (b.greenF() - a.greenF()) * amount,
                                 a.blueF() + (b.blueF() - a.blueF()) * amount,
                                 a.alphaF() + (b.alphaF() - a.alphaF()) * amount)


# ----------------------------------------------------------------------
# Artwork
# ----------------------------------------------------------------------
def draw_art(painter: QtGui.QPainter, rect: QtCore.QRectF, image, seed: str,
             radius: float = 8.0, shadow: float = 1.0, loading: bool = False,
             tokens: ThemeTokens | None = None) -> None:
    """A cover with a soft layered shadow; a tinted note when there is none."""
    painter.save()
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)
    if shadow > 0:
        draw_shadow(painter, rect, radius, shadow, tokens)
    clip = QtGui.QPainterPath()
    clip.addRoundedRect(rect, radius, radius)
    painter.setClipPath(clip)
    if isinstance(image, QtGui.QImage) and not image.isNull():
        painter.drawImage(rect, image)
    else:
        first, second = placeholder_colors(seed)
        gradient = QtGui.QLinearGradient(rect.topLeft(), rect.bottomRight())
        gradient.setColorAt(0, first)
        gradient.setColorAt(1, second)
        painter.fillRect(rect, gradient)
        glyph = min(rect.width(), rect.height()) * 0.34
        note_rect = R(rect.center().x() - glyph / 2, rect.center().y() - glyph / 2, glyph, glyph)
        color = QtGui.QColor(255, 255, 255, 120 if loading else 170)
        icons.paint(painter, "disc" if loading else "note", note_rect, color)
    painter.restore()


def draw_shadow(painter, rect: QtCore.QRectF, radius: float, level: float = 1.0,
                tokens: ThemeTokens | None = None) -> None:
    """An M3 elevation shadow: a tight key shadow and a soft ambient one."""
    dark = tokens is not None and tokens.name == "dark"
    painter.save()
    painter.setPen(Qt.PenStyle.NoPen)
    for i in range(1, 6):
        spread = i * 1.6 * level
        color = QtGui.QColor(0, 0, 0)
        color.setAlphaF((0.11 if dark else 0.05) * level * (6 - i) / 5)
        painter.setBrush(color)
        painter.drawRoundedRect(rect.adjusted(-spread * 0.4, spread * 0.5, spread * 0.4, spread * 1.0),
                                radius + spread * 0.4, radius + spread * 0.4)
    painter.restore()


class ArtLabel(QtWidgets.QWidget):
    """A fixed-size cover; set_image(None) shows the placeholder."""

    def __init__(self, size: int, radius: float = 10, shadow: float = 1.0, parent=None):
        super().__init__(parent)
        self.side = size
        self.radius = radius
        self.shadow = shadow
        self.image = None
        self.seed = ""
        self.tokens = None
        pad = int(12 * shadow)
        self.setFixedSize(size + pad * 2, size + pad * 2)
        self.pad = pad

    def set_image(self, image, seed: str = "") -> None:
        self.image = image if isinstance(image, QtGui.QImage) else None
        self.seed = seed
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QtGui.QPainter(self)
        draw_art(painter, R(self.pad, self.pad * 0.6, self.side, self.side), self.image,
                 self.seed, self.radius, self.shadow, tokens=self.tokens)
        painter.end()


# ----------------------------------------------------------------------
# Ambient field
# ----------------------------------------------------------------------
class WindowSurface(QtWidgets.QWidget):
    """The window's background: one flat tonal surface colour."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.tokens: ThemeTokens | None = None

    def set_tokens(self, tokens: ThemeTokens) -> None:
        self.tokens = tokens
        self.update()

    def paintEvent(self, _event) -> None:
        if self.tokens is None:
            return
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), QtGui.QColor(self.tokens.window_bg))
        painter.end()


# ----------------------------------------------------------------------
# Top bar status capsule
# ----------------------------------------------------------------------
class LcdDisplay(QtWidgets.QWidget):
    """The top bar's status capsule: a running job's progress, else the
    iPod's link and library state. Clicking it opens the iPod page."""

    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tokens: ThemeTokens | None = None
        self.setFixedHeight(48)
        self.setMinimumWidth(240)
        self.setMaximumWidth(380)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                           QtWidgets.QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.activity = ""
        self.fraction = None
        self.idle_line = ("AstraPod", "iPod 未连接")
        self.connected = False
        self._sweep = 0.0
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._tick)
        self.setAccessibleName("iPod 状态")

    def _tick(self) -> None:
        self._sweep = (self._sweep + 0.02) % 1.0
        self.update()

    def set_activity(self, text: str, fraction) -> None:
        self.activity = text
        self.fraction = fraction
        if text and fraction is None:
            self._timer.start()
        else:
            self._timer.stop()
        self.setToolTip(text)
        self.update()

    def set_idle(self, title: str, detail: str, connected: bool = False) -> None:
        self.idle_line = (title, detail)
        self.connected = connected
        self.setAccessibleDescription(f"{title}，{detail}")
        self.update()

    def mousePressEvent(self, event) -> None:
        self.clicked.emit()
        super().mousePressEvent(event)

    def paintEvent(self, _event) -> None:
        t = self.tokens
        if t is None:
            return
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        rect = R(self.rect())
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QtGui.QColor(t.surface_container_high))
        painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
        if self.underMouse():
            layer = QtGui.QColor(t.on_surface)
            layer.setAlphaF(0.08)
            painter.setBrush(layer)
            painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
        text = QtGui.QColor(t.on_surface)
        muted = QtGui.QColor(t.on_surface_variant)
        live = bool(self.activity) or self.connected
        avatar = R(rect.left() + 8, rect.center().y() - 16, 32, 32)
        painter.setBrush(QtGui.QColor(t.primary_container if live else t.surface_container_highest))
        painter.drawEllipse(avatar)
        icons.paint(painter, "sync" if self.activity else "device", avatar.adjusted(7, 7, -7, -7),
                    QtGui.QColor(t.on_primary_container if live else t.on_surface_variant))
        inner = R(avatar.right() + 12, rect.top() + 5, rect.right() - avatar.right() - 32,
                  rect.height() - 10)
        if self.activity:
            self._paint_lines(painter, inner.adjusted(0, -2, 0, -4), self.activity, "正在处理",
                              text, muted)
            bar = R(inner.left(), rect.bottom() - 9, inner.width(), 4)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QtGui.QColor(t.secondary_container))
            painter.drawRoundedRect(bar, 2, 2)
            painter.setBrush(QtGui.QColor(t.primary))
            if self.fraction is None:
                width = bar.width() * 0.3
                x = bar.left() + (bar.width() - width) * (0.5 - 0.5 * math.cos(self._sweep * 2 * math.pi))
                painter.drawRoundedRect(R(x, bar.top(), width, bar.height()), 2, 2)
            else:
                painter.drawRoundedRect(R(bar.left(), bar.top(),
                                          bar.width() * max(0.0, min(1.0, self.fraction)),
                                          bar.height()), 2, 2)
        else:
            self._paint_lines(painter, inner, self.idle_line[0], self.idle_line[1], text, muted)
        painter.end()

    def enterEvent(self, event) -> None:
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.update()
        super().leaveEvent(event)

    def _paint_lines(self, painter, rect, first, second, color, muted, center=False):
        align = Qt.AlignmentFlag.AlignHCenter if center else Qt.AlignmentFlag.AlignLeft
        font = QtGui.QFont(self.font())
        font.setPixelSize(14)
        font.setWeight(QtGui.QFont.Weight.Medium)
        painter.setFont(font)
        painter.setPen(color)
        metrics = QtGui.QFontMetrics(font)
        top = R(rect.left(), rect.top(), rect.width(), rect.height() / 2 + 1)
        painter.drawText(top, align | Qt.AlignmentFlag.AlignBottom,
                         metrics.elidedText(first, Qt.TextElideMode.ElideRight, int(rect.width())))
        font.setWeight(QtGui.QFont.Weight.Normal)
        font.setPixelSize(12)
        painter.setFont(font)
        painter.setPen(muted)
        metrics = QtGui.QFontMetrics(font)
        bottom = R(rect.left(), rect.center().y() + 1, rect.width(), rect.height() / 2)
        painter.drawText(bottom, align | Qt.AlignmentFlag.AlignTop,
                         metrics.elidedText(second, Qt.TextElideMode.ElideRight, int(rect.width())))


# ----------------------------------------------------------------------
# Gauges
# ----------------------------------------------------------------------
class CapacityBar(QtWidgets.QWidget):
    """The iPod's capacity as an M3 linear indicator: audio and other as
    gapped segments, free space as the track ending in a stop dot, with
    the legend underneath."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tokens: ThemeTokens | None = None
        self.segments: list[tuple[str, int, str]] = []
        self.total = 0
        self.progress = 1.0
        # Bar widths as fractions of the total: drawn moves from start to end.
        self._start = self._end = (0.0, 0.0)
        self.setMinimumHeight(74)
        self._anim = QtCore.QVariantAnimation(self)
        self._anim.setDuration(500)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setEasingCurve(emphasized())
        self._anim.valueChanged.connect(self._step)

    def _step(self, value) -> None:
        self.progress = float(value)
        self.update()

    def _drawn(self) -> tuple[float, float]:
        return tuple(a + (b - a) * self.progress for a, b in zip(self._start, self._end))

    def set_usage(self, total: int, audio: int, other: int, free: int, animate=True) -> None:
        self.total = max(1, total)
        self.segments = [("音频", audio, "primary"), ("其他", other, "tertiary"),
                         ("可用", free, "secondary_container")]
        self.setAccessibleDescription(
            f"音频 {fmt.size(audio)}，其他 {fmt.size(other)}，可用 {fmt.size(free)}")
        end = (audio / self.total, other / self.total)
        if end == self._end:
            self.update()
            return
        self._start, self._end = self._drawn(), end
        self._anim.stop()
        if animate and MOTION["on"]:
            self.progress = 0.0
            self._anim.start()
        else:
            self.progress = 1.0
        self.update()

    def paintEvent(self, _event) -> None:
        t = self.tokens
        if t is None or not self.segments:
            return
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        height, gap = 12.0, 4.0
        bar = R(0, 4, self.width(), height)
        x = bar.left()
        for (_name, _amount, token), share in zip(self.segments[:-1], self._drawn()):
            width = bar.width() * share
            if width >= 1:
                painter.setBrush(QtGui.QColor(getattr(t, token)))
                painter.drawRoundedRect(R(x, bar.top(), max(width - gap, height / 2), height),
                                        height / 2, height / 2)
                x += width
        if bar.right() - x > height:
            painter.setBrush(QtGui.QColor(t.secondary_container))
            painter.drawRoundedRect(R(x, bar.top(), bar.right() - x, height), height / 2, height / 2)
            painter.setBrush(QtGui.QColor(t.primary))
            painter.drawEllipse(P(bar.right() - height / 2, bar.center().y()), 2, 2)

        font = QtGui.QFont(self.font())
        lx = 0.0
        for name, amount, token in self.segments:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QtGui.QColor(getattr(t, token)))
            painter.drawRoundedRect(R(lx, 39, 12, 12), 4, 4)
            font.setPixelSize(14)
            font.setWeight(QtGui.QFont.Weight.Medium)
            painter.setFont(font)
            painter.setPen(QtGui.QColor(t.on_surface))
            painter.drawText(R(lx + 20, 34, 200, 22), Qt.AlignmentFlag.AlignVCenter, name)
            width = QtGui.QFontMetrics(font).horizontalAdvance(name)
            numeric = QtGui.QFont(font)
            numeric.setFamilies(list(FONT_NUMERIC))
            numeric.setWeight(QtGui.QFont.Weight.Normal)
            numeric.setPixelSize(12)
            painter.setFont(numeric)
            painter.setPen(QtGui.QColor(t.on_surface_variant))
            painter.drawText(R(lx + 20, 54, 200, 18), Qt.AlignmentFlag.AlignVCenter, fmt.size(amount))
            lx += 20 + max(width, QtGui.QFontMetrics(numeric).horizontalAdvance(fmt.size(amount))) + 32
        painter.end()


class RingGauge(QtWidgets.QWidget):
    """A percentage as an M3 circular indicator: the active arc and the
    track separated by gaps. The arc turns warning past 70 and error past 90."""

    def __init__(self, caption: str, size: int = 112, parent=None):
        super().__init__(parent)
        self.tokens: ThemeTokens | None = None
        self.caption = caption
        self.value: float | None = None
        self.shown = 0.0
        self.detail = ""
        self.setFixedSize(size, size + 38)
        self._anim = QtCore.QVariantAnimation(self)
        self._anim.setDuration(500)
        self._anim.setEasingCurve(emphasized())
        self._anim.valueChanged.connect(self._step)

    def _step(self, value) -> None:
        self.shown = float(value)
        self.update()

    def set_value(self, percent: float | None, detail: str = "") -> None:
        self.detail = detail
        self.setToolTip(detail)
        if percent is None:
            self.value = None
            self.shown = 0.0
            self.update()
            return
        target = max(0.0, min(100.0, float(percent)))
        if self.value != target:
            self.value = target
            self._anim.stop()
            if MOTION["on"]:
                self._anim.setStartValue(self.shown)
                self._anim.setEndValue(target)
                self._anim.start()
            else:
                self.shown = target
        self.update()

    def paintEvent(self, _event) -> None:
        t = self.tokens
        if t is None:
            return
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        side = self.width()
        stroke = 8.0
        ring = R(stroke / 2 + 4, stroke / 2 + 4, side - stroke - 8, side - stroke - 8)
        # The gap between arc and track, in degrees, measured cap to cap.
        gap = math.degrees((stroke + 6) / (ring.width() / 2))
        sweep = 360.0 * (self.shown / 100 if self.value is not None else 0.0)
        pen = QtGui.QPen(QtGui.QColor(t.secondary_container), stroke)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        if sweep < 1:
            painter.drawArc(ring, 0, 360 * 16)
        elif sweep < 360 - 2 * gap:
            start = 90 - sweep - gap
            painter.drawArc(ring, int(start * 16), int(-(360 - sweep - 2 * gap) * 16))
        if sweep >= 1:
            color = QtGui.QColor(t.primary if self.shown < 70 else
                                 t.warning if self.shown < 90 else t.error)
            pen.setColor(color)
            painter.setPen(pen)
            painter.drawArc(ring, 90 * 16, int(-max(0.5, min(sweep, 360 - 0.5)) * 16))
        font = QtGui.QFont(self.font())
        font.setFamilies(list(FONT_NUMERIC))
        font.setPixelSize(22)
        font.setWeight(QtGui.QFont.Weight.Normal)
        painter.setFont(font)
        painter.setPen(QtGui.QColor(t.on_surface))
        text = "—" if self.value is None else f"{self.shown:.0f}%"
        painter.drawText(R(0, 0, side, side), Qt.AlignmentFlag.AlignCenter, text)
        font = QtGui.QFont(self.font())
        font.setPixelSize(14)
        font.setWeight(QtGui.QFont.Weight.Medium)
        painter.setFont(font)
        painter.setPen(QtGui.QColor(t.on_surface))
        painter.drawText(R(0, side - 2, side, 20), Qt.AlignmentFlag.AlignCenter, self.caption)
        font.setWeight(QtGui.QFont.Weight.Normal)
        font.setPixelSize(12)
        painter.setFont(font)
        painter.setPen(QtGui.QColor(t.on_surface_variant))
        metrics = QtGui.QFontMetrics(font)
        painter.drawText(R(0, side + 18, side, 18), Qt.AlignmentFlag.AlignCenter,
                         metrics.elidedText(self.detail, Qt.TextElideMode.ElideRight, side))
        painter.end()


class StatusDot(QtWidgets.QWidget):
    """A status colour dot; "working" breathes."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(14, 14)
        self.color = QtGui.QColor("#888")
        self.pulse = False
        self._phase = 0.0
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(60)
        self._timer.timeout.connect(self._tick)

    def set_state(self, color: QtGui.QColor, pulse: bool) -> None:
        self.color = QtGui.QColor(color)
        self.pulse = pulse
        if pulse:
            self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def _tick(self) -> None:
        self._phase = (self._phase + 0.05) % 1.0
        self.update()

    def paintEvent(self, _event) -> None:
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        if self.pulse:
            halo = QtGui.QColor(self.color)
            halo.setAlphaF(0.35 * (1 - self._phase))
            painter.setBrush(halo)
            r = 3.5 + 3.5 * self._phase
            painter.drawEllipse(P(7, 7), r, r)
        painter.setBrush(self.color)
        painter.drawEllipse(P(7, 7), 3.5, 3.5)
        painter.end()


# ----------------------------------------------------------------------
# Sidebar
# ----------------------------------------------------------------------
KEY_ROLE = Qt.ItemDataRole.UserRole + 10
ICON_ROLE = Qt.ItemDataRole.UserRole + 11
HEADER_ROLE = Qt.ItemDataRole.UserRole + 12
BADGE_ROLE = Qt.ItemDataRole.UserRole + 13
PARENT_ROLE = Qt.ItemDataRole.UserRole + 14     # a child row's node key
EXPANDED_ROLE = Qt.ItemDataRole.UserRole + 15   # a node row: True or False
INDENT = 20


class SidebarDelegate(QtWidgets.QStyledItemDelegate):
    """Drawer rows are 48 px pills with the label beside the icon; rail
    rows put a 56x32 indicator over a small label. The active page takes
    the secondary container colour. A node row carries a chevron; its
    children are indented in the drawer and sit on a tinted band in the rail."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tokens: ThemeTokens | None = None
        self.drop_row = -1
        self.compact = False
        self.keyboard_focus = False

    def sizeHint(self, option, index):
        if index.data(HEADER_ROLE):
            return QtCore.QSize(option.rect.width(), 0 if self.compact else 30)
        return QtCore.QSize(option.rect.width(), 48 if self.compact else 40)

    def indicator(self, rect: QtCore.QRectF, child: bool = False) -> tuple[QtCore.QRectF, float]:
        if self.compact:
            return R(rect.center().x() - 28, rect.top(), 56, 32), 16.0
        return rect.adjusted(12 + (INDENT if child else 0), 1, -12, -1), 20.0

    def chevron_area(self, rect: QtCore.QRectF) -> QtCore.QRectF:
        """Where a node row's chevron sits and takes clicks."""
        pill, _radius = self.indicator(rect)
        if self.compact:
            return R(pill.right(), pill.top(), max(12.0, rect.right() - pill.right()), pill.height())
        return R(pill.right() - 40, pill.top(), 40, pill.height())

    @staticmethod
    def _band(painter, rect: QtCore.QRectF, index, color: QtGui.QColor) -> None:
        """The rail's backdrop behind a node's children, rounded at the
        group's first and last row."""
        parent = index.data(PARENT_ROLE)
        first = index.siblingAtRow(index.row() - 1).data(PARENT_ROLE) != parent
        last = index.siblingAtRow(index.row() + 1).data(PARENT_ROLE) != parent
        band = rect.adjusted(6, 0, -6, 0)
        path = QtGui.QPainterPath()
        path.setFillRule(Qt.FillRule.WindingFill)
        path.addRoundedRect(band, 14, 14)
        if not first:
            path.addRect(R(band.left(), band.top(), band.width(), band.height() / 2))
        if not last:
            path.addRect(R(band.left(), band.center().y(), band.width(), band.height() / 2))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawPath(path)

    @staticmethod
    def _chevron(painter, center: QtCore.QPointF, expanded: bool, size: float,
                 color: QtGui.QColor) -> None:
        half = size / 2
        path = QtGui.QPainterPath()
        if expanded:
            path.moveTo(center.x() - half, center.y() - half / 2)
            path.lineTo(center.x(), center.y() + half / 2)
            path.lineTo(center.x() + half, center.y() - half / 2)
        else:
            path.moveTo(center.x() - half / 2, center.y() - half)
            path.lineTo(center.x() + half / 2, center.y())
            path.lineTo(center.x() - half / 2, center.y() + half)
        pen = QtGui.QPen(color, 2 if size > 6 else 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)

    def paint(self, painter, option, index):
        t = self.tokens
        if t is None:
            return
        painter.save()
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        rect = R(option.rect)
        font = QtGui.QFont(option.font)
        if index.data(HEADER_ROLE):
            if not self.compact:
                font.setPixelSize(14)
                font.setWeight(QtGui.QFont.Weight.Medium)
                painter.setFont(font)
                painter.setPen(QtGui.QColor(t.on_surface_variant))
                painter.drawText(rect.adjusted(28, 6, -12, 0),
                                 Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                                 index.data(Qt.ItemDataRole.DisplayRole))
            painter.restore()
            return
        child = bool(index.data(PARENT_ROLE))
        expanded = index.data(EXPANDED_ROLE)
        selected = bool(option.state & QtWidgets.QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QtWidgets.QStyle.StateFlag.State_MouseOver)
        pill, radius = self.indicator(rect, child)
        if child and self.compact:
            self._band(painter, rect, index, QtGui.QColor(t.surface_container_high))
        painter.setPen(Qt.PenStyle.NoPen)
        if selected:
            painter.setBrush(QtGui.QColor(t.secondary_container))
            painter.drawRoundedRect(pill, radius, radius)
        if hover or index.row() == self.drop_row:
            layer = QtGui.QColor(t.on_secondary_container if selected else t.on_surface)
            layer.setAlphaF(0.12 if index.row() == self.drop_row else 0.08)
            painter.setBrush(layer)
            painter.drawRoundedRect(pill, radius, radius)
        if self.keyboard_focus and option.state & QtWidgets.QStyle.StateFlag.State_HasFocus and selected:
            painter.setPen(QtGui.QPen(QtGui.QColor(t.secondary), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(pill.adjusted(1, 1, -1, -1), radius - 1, radius - 1)
        fg = QtGui.QColor(t.on_secondary_container if selected else t.on_surface_variant)
        text = index.data(Qt.ItemDataRole.DisplayRole)
        badge = index.data(BADGE_ROLE)
        if self.compact:
            icon_rect = R(pill.center().x() - 12, pill.center().y() - 12, 24, 24)
            icons.paint(painter, index.data(ICON_ROLE), icon_rect, fg)
            font.setPixelSize(11)
            font.setWeight(QtGui.QFont.Weight.Bold if selected else QtGui.QFont.Weight.Medium)
            painter.setFont(font)
            painter.setPen(QtGui.QColor(t.on_surface if selected else t.on_surface_variant))
            metrics = QtGui.QFontMetrics(font)
            painter.drawText(R(rect.left() + 2, pill.bottom(), rect.width() - 4, 16),
                             Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                             metrics.elidedText(text, Qt.TextElideMode.ElideRight, int(rect.width() - 4)))
            if badge:
                self._badge(painter, font, str(badge), P(icon_rect.right() + 2, icon_rect.top() + 2), t)
            if expanded is not None:
                area = self.chevron_area(rect)
                self._chevron(painter, area.center(), expanded, 6,
                              QtGui.QColor(t.on_surface_variant))
        else:
            icons.paint(painter, index.data(ICON_ROLE), R(pill.left() + 16, pill.center().y() - 12, 24, 24), fg)
            font.setPixelSize(14)
            font.setWeight(QtGui.QFont.Weight.DemiBold if selected else QtGui.QFont.Weight.Medium)
            painter.setFont(font)
            painter.setPen(fg)
            metrics = QtGui.QFontMetrics(font)
            text_rect = pill.adjusted(52, 0, -48, 0)
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                             metrics.elidedText(text, Qt.TextElideMode.ElideRight,
                                                int(text_rect.width())))
            if badge:
                self._badge(painter, font, str(badge), P(pill.right() - 24, pill.center().y()), t)
            elif expanded is not None:
                area = self.chevron_area(rect)
                self._chevron(painter, P(area.center().x() + 2, area.center().y()), expanded, 9, fg)
        painter.restore()

    @staticmethod
    def _badge(painter, font, text: str, center: QtCore.QPointF, t: ThemeTokens) -> None:
        """An M3 large badge: error colour, 16 px high."""
        font = QtGui.QFont(font)
        font.setPixelSize(11)
        font.setWeight(QtGui.QFont.Weight.Medium)
        painter.setFont(font)
        width = max(16.0, QtGui.QFontMetrics(font).horizontalAdvance(text) + 8.0)
        chip = R(center.x() - width / 2, center.y() - 8, width, 16)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QtGui.QColor(t.error))
        painter.drawRoundedRect(chip, 8, 8)
        painter.setPen(QtGui.QColor(t.on_error))
        painter.drawText(chip, Qt.AlignmentFlag.AlignCenter, text)


class Sidebar(QtWidgets.QListView):
    """Sections of pages; accepts files dropped on it for import.

    A page added with a parent is that node's child: it shows only while
    the node is expanded. Nodes start collapsed. Clicking a node opens its
    page; its chevron, a click on the open node, or Left and Right toggle
    it. Opening a child expands its node. The drawer shows a scroll bar
    when the list outgrows the window; the rail scrolls by wheel only."""

    page_selected = Signal(str)
    files_dropped = Signal(list)
    expanded_changed = Signal(str, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.model_ = QtGui.QStandardItemModel(self)
        self.setModel(self.model_)
        self.delegate = SidebarDelegate(self)
        self.setItemDelegate(self.delegate)
        self.setMouseTracking(True)
        self.setAcceptDrops(True)
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setUniformItemSizes(False)
        self.setAccessibleName("导航")
        self.rows: dict[str, int] = {}
        self.compact = False
        self.selectionModel().currentChanged.connect(self._current)

    def set_compact(self, compact: bool) -> None:
        """Compact turns the drawer into a rail: headers hide, labels go
        under the icons, and the names move into tooltips."""
        self.compact = compact
        self.delegate.compact = compact
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff if compact
                                        else Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        for row in range(self.model_.rowCount()):
            item = self.model_.item(row)
            if not item.data(HEADER_ROLE):
                item.setToolTip(item.text() if compact else "")
        self._apply_visibility()

    def _apply_visibility(self) -> None:
        for row in range(self.model_.rowCount()):
            item = self.model_.item(row)
            parent = item.data(PARENT_ROLE)
            if item.data(HEADER_ROLE):
                hidden = self.compact
            else:
                hidden = bool(parent) and not self.is_expanded(parent)
            self.setRowHidden(row, hidden)
        self.doItemsLayout()
        self.scrollTo(self.currentIndex())
        self.viewport().update()

    def focusInEvent(self, event) -> None:
        """The focus ring shows only for keyboard focus, as in M3."""
        self.delegate.keyboard_focus = event.reason() in (
            Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason)
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:
        self.delegate.keyboard_focus = False
        super().focusOutEvent(event)

    def ripple_rect(self, index) -> tuple[QtCore.QRect, float]:
        pill, radius = self.delegate.indicator(R(self.visualRect(index)),
                                               bool(index.data(PARENT_ROLE)))
        return pill.toRect(), radius

    def add_header(self, text: str) -> None:
        item = QtGui.QStandardItem(text)
        item.setData(True, HEADER_ROLE)
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        self.model_.appendRow(item)

    def add_page(self, key: str, text: str, icon_name: str, parent: str | None = None) -> None:
        """A page row; with parent, a child row of that node."""
        item = QtGui.QStandardItem(text)
        item.setData(key, KEY_ROLE)
        item.setData(icon_name, ICON_ROLE)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        if parent is not None:
            item.setData(parent, PARENT_ROLE)
            node = self._item(parent)
            if node.data(EXPANDED_ROLE) is None:
                node.setData(False, EXPANDED_ROLE)
        self.rows[key] = self.model_.rowCount()
        self.model_.appendRow(item)

    def _item(self, key: str):
        row = self.rows.get(key)
        return self.model_.item(row) if row is not None else None

    def set_text(self, key: str, text: str) -> None:
        item = self._item(key)
        if item is not None and item.text() != text:
            item.setText(text)
            if self.compact:
                item.setToolTip(text)

    def children(self, key: str) -> list[str]:
        return [k for k, row in self.rows.items() if self.model_.item(row).data(PARENT_ROLE) == key]

    def is_expanded(self, key: str) -> bool:
        item = self._item(key)
        return bool(item is not None and item.data(EXPANDED_ROLE))

    def set_expanded(self, key: str, expanded: bool) -> None:
        item = self._item(key)
        if item is None or item.data(EXPANDED_ROLE) is None or self.is_expanded(key) == expanded:
            return
        item.setData(expanded, EXPANDED_ROLE)
        if not expanded and self.current_key() in self.children(key):
            self.select(key)
        self._apply_visibility()
        self.expanded_changed.emit(key, expanded)

    def mousePressEvent(self, event) -> None:
        index = self.indexAt(event.position().toPoint())
        key = index.data(KEY_ROLE) if index.isValid() else None
        if key and index.data(EXPANDED_ROLE) is not None and event.button() == Qt.MouseButton.LeftButton:
            on_chevron = self.delegate.chevron_area(R(self.visualRect(index))).contains(
                event.position())
            if on_chevron or key == self.current_key():
                self.set_expanded(key, not self.is_expanded(key))
                event.accept()
                return
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:
        key = self.current_key()
        item = self._item(key) if key else None
        if item is not None and event.key() in (Qt.Key.Key_Right, Qt.Key.Key_Left):
            expandable = item.data(EXPANDED_ROLE) is not None
            if event.key() == Qt.Key.Key_Right and expandable:
                self.set_expanded(key, True)
                return
            if event.key() == Qt.Key.Key_Left:
                if expandable and self.is_expanded(key):
                    self.set_expanded(key, False)
                elif item.data(PARENT_ROLE):
                    self.select(item.data(PARENT_ROLE))
                return
        super().keyPressEvent(event)

    def set_badge(self, key: str, value) -> None:
        row = self.rows.get(key)
        if row is not None:
            self.model_.item(row).setData(value or None, BADGE_ROLE)

    def select(self, key: str) -> None:
        row = self.rows.get(key)
        if row is None:
            return
        parent = self.model_.item(row).data(PARENT_ROLE)
        if parent and not self.is_expanded(parent):
            self.set_expanded(parent, True)
        self.setCurrentIndex(self.model_.index(row, 0))

    def current_key(self) -> str | None:
        return self.currentIndex().data(KEY_ROLE)

    def _current(self, current, _previous) -> None:
        key = current.data(KEY_ROLE)
        self.scrollTo(current)
        if key:
            self.page_selected.emit(key)

    def set_tokens(self, tokens: ThemeTokens) -> None:
        self.delegate.tokens = tokens
        self.viewport().update()

    @staticmethod
    def _paths(event) -> list[str]:
        data = event.mimeData()
        if not data.hasUrls():
            return []
        return [url.toLocalFile() for url in data.urls() if url.isLocalFile()]

    def dragEnterEvent(self, event) -> None:
        if self._paths(event):
            event.acceptProposedAction()
            self.delegate.drop_row = self.rows.get("import", -1)
            self.viewport().update()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if self._paths(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:
        self.delegate.drop_row = -1
        self.viewport().update()

    def dropEvent(self, event) -> None:
        paths = self._paths(event)
        self.delegate.drop_row = -1
        self.viewport().update()
        if paths:
            event.acceptProposedAction()
            self.files_dropped.emit(paths)


# ----------------------------------------------------------------------
# Page stack with a cross-fade and slide
# ----------------------------------------------------------------------
class _Snapshot(QtWidgets.QWidget):
    def __init__(self, pixmap: QtGui.QPixmap, parent):
        super().__init__(parent)
        self.pixmap = pixmap
        self.opacity = 1.0
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def paintEvent(self, _event) -> None:
        painter = QtGui.QPainter(self)
        painter.setOpacity(self.opacity)
        painter.drawPixmap(0, 0, self.pixmap)
        painter.end()


class PageStack(QtWidgets.QStackedWidget):
    """Switching pages fades the old one out while the new one rises in
    on the emphasized decelerate curve."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.animate = True
        self._snapshot: _Snapshot | None = None
        self._group: QtCore.QParallelAnimationGroup | None = None

    def show_page(self, widget: QtWidgets.QWidget) -> None:
        old = self.currentWidget()
        if old is widget:
            return
        if not self.animate or old is None or not self.isVisible():
            self.setCurrentWidget(widget)
            return
        if self._group is not None:
            self._group.stop()
            self._finish()
        pixmap = old.grab()
        self.setCurrentWidget(widget)
        self._snapshot = _Snapshot(pixmap, self)
        self._snapshot.setGeometry(old.geometry())
        self._snapshot.show()
        self._snapshot.raise_()
        fade = QtCore.QVariantAnimation(self)
        fade.setDuration(150)
        fade.setEasingCurve(emphasized("accelerate"))
        fade.setStartValue(1.0)
        fade.setEndValue(0.0)
        fade.valueChanged.connect(self._fade)
        slide = QtCore.QPropertyAnimation(widget, b"pos", self)
        slide.setDuration(350)
        end = widget.pos()
        slide.setStartValue(end + QtCore.QPoint(0, 24))
        slide.setEndValue(end)
        slide.setEasingCurve(emphasized())
        self._group = QtCore.QParallelAnimationGroup(self)
        self._group.addAnimation(fade)
        self._group.addAnimation(slide)
        self._group.finished.connect(self._finish)
        self._group.start()

    def _fade(self, value) -> None:
        if self._snapshot is not None:
            self._snapshot.opacity = float(value)
            self._snapshot.update()

    def _finish(self) -> None:
        if self._snapshot is not None:
            self._snapshot.deleteLater()
            self._snapshot = None
        self._group = None
        current = self.currentWidget()
        if current is not None:
            current.setGeometry(self.contentsRect())


# ----------------------------------------------------------------------
# Table helpers
# ----------------------------------------------------------------------
class StarDelegate(QtWidgets.QStyledItemDelegate):
    """Rockbox 0..10 ratings as five stars with halves."""

    def __init__(self, role, parent=None):
        super().__init__(parent)
        self.role = role
        self.tokens: ThemeTokens | None = None

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        t = self.tokens
        rating = index.data(self.role) or 0
        if t is None:
            return
        painter.save()
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        size = 11.0
        x = option.rect.left() + 8
        cy = option.rect.center().y() + 0.5
        on = QtGui.QColor(t.star)
        off = QtGui.QColor(t.outline_variant)
        for i in range(5):
            path = icons.star_path(x + size / 2, cy, size / 2, size / 4.6)
            value = rating / 2 - i
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(off)
            painter.drawPath(path)
            if value > 0:
                painter.save()
                painter.setClipRect(R(x, cy - size, size * min(1.0, value), size * 2))
                painter.setBrush(on)
                painter.drawPath(path)
                painter.restore()
            x += size + 3
        painter.restore()


def configure_table(view: QtWidgets.QTableView, row_height: int = 40) -> None:
    """M3 data-table rows: no stripes, dividers come from the style sheet."""
    view.setShowGrid(False)
    view.setAlternatingRowColors(False)
    view.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
    view.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
    view.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
    view.setHorizontalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
    view.setWordWrap(False)
    view.setTextElideMode(Qt.TextElideMode.ElideRight)
    view.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
    vertical = view.verticalHeader()
    vertical.setVisible(False)
    vertical.setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Fixed)
    vertical.setDefaultSectionSize(row_height)
    header = view.horizontalHeader()
    header.setHighlightSections(False)
    header.setSectionsMovable(True)
    header.setStretchLastSection(True)
    header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    header.setMinimumSectionSize(36)


class DropFrame(QtWidgets.QFrame):
    """A card that accepts dropped files and shows a dashed target."""

    files_dropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.hovering = False

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            self.hovering = True
            self.update()
            event.acceptProposedAction()

    def dragLeaveEvent(self, _event) -> None:
        self.hovering = False
        self.update()

    def dropEvent(self, event) -> None:
        self.hovering = False
        self.update()
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            event.acceptProposedAction()
            self.files_dropped.emit(paths)


def elapsed_text(since) -> str:
    if not since:
        return ""
    seconds = max(0, int(time.time() - float(since)))
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{seconds // 60} 分钟"
    if seconds < 86400:
        return f"{seconds // 3600} 小时 {seconds % 3600 // 60} 分"
    return f"{seconds // 86400} 天"
