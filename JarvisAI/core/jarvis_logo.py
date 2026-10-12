"""
jarvis_logo.py - Animated Jarvis AI logo for PyQt6.

Drop-in widget:
    from jarvis_logo import JarvisLogoWidget
    logo = JarvisLogoWidget()
    layout.addWidget(logo)

Wiring hooks:
    logo.set_status("Listening")      # text under the wordmark
    logo.set_level(0.0 .. 1.0)        # drive the voice bars from mic / TTS level
    logo.set_level(None)              # back to the built-in idle animation
    logo.set_wordmark("JARVIS")       # change the letters
    logo.set_background(False)        # transparent, no dark stage
    logo.start() / logo.stop()        # auto-managed on show/hide

Run this file directly to preview it as a frameless splash window.
"""

import math
import sys

from PyQt6.QtCore import QElapsedTimer, QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)
from PyQt6.QtWidgets import QApplication, QVBoxLayout, QWidget

DESIGN_W, DESIGN_H = 550.0, 430.0
CX, CY = 275.0, 180.0

CYAN = QColor("#22d3ee")
VIOLET = QColor("#a78bfa")
ICE = QColor("#67e8f9")
GREEN = QColor("#34d399")
STAGE = QColor("#07080f")
STAGE_BORDER = QColor("#1b2238")
CORE_FILL = QColor("#0b1020")
STATUS_TEXT = QColor("#9fb4d6")

BAR_DURATIONS = (1.2, 1.0, 1.4, 0.9, 1.3, 1.1, 1.5)
BAR_DELAYS = (0.0, 0.3, 0.7, 0.1, 0.9, 0.5, 0.2)

TWINKLES = (
    (70, 80, 2.0, "#22d3ee", 0.0),
    (480, 110, 2.0, "#a78bfa", 0.8),
    (95, 290, 1.5, "#a78bfa", 1.6),
    (470, 270, 1.5, "#22d3ee", 0.4),
    (440, 50, 1.5, "#ffffff", 1.2),
)


def _color(c, alpha=1.0):
    out = QColor(c)
    out.setAlphaF(max(0.0, min(1.0, alpha)))
    return out


def _smooth(phase):
    """0 -> 1 -> 0 ease-in-out over phase 0..1."""
    return 0.5 - 0.5 * math.cos(2.0 * math.pi * phase)


class JarvisLogoWidget(QWidget):
    def __init__(self, parent=None, wordmark="JARVIS", status="Intelligence online"):
        super().__init__(parent)
        self._wordmark = wordmark
        self._status = status
        self._background = True
        self._level_target = None
        self._level = 0.0

        self._clock = QElapsedTimer()
        self._clock.start()

        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self.update)

        self._word_font = QFont()
        self._word_font.setFamilies(["Inter", "Segoe UI", "SF Pro Display", "Helvetica Neue", "Arial"])
        self._word_font.setPixelSize(38)
        self._word_font.setWeight(QFont.Weight.Medium)

        self._mono_font = QFont()
        self._mono_font.setFamilies(["JetBrains Mono", "Cascadia Mono", "Consolas", "Menlo", "Monospace"])
        self._mono_font.setPixelSize(12)

        self.setMinimumSize(275, 215)

    def sizeHint(self):
        from PyQt6.QtCore import QSize

        return QSize(int(DESIGN_W), int(DESIGN_H))

    def start(self):
        if not self._timer.isActive():
            self._timer.start()

    def stop(self):
        self._timer.stop()

    def set_status(self, text):
        self._status = text
        self.update()

    def set_wordmark(self, text):
        self._wordmark = text
        self.update()

    def set_background(self, enabled):
        self._background = bool(enabled)
        self.update()

    def set_level(self, level):
        """Feed an audio level (0..1) to drive the bars, or None for idle animation."""
        self._level_target = None if level is None else max(0.0, min(1.0, float(level)))

    def showEvent(self, event):
        super().showEvent(event)
        self.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        self.stop()

    def paintEvent(self, event):
        t = self._clock.elapsed() / 1000.0

        p = QPainter(self)
        p.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )

        scale = min(self.width() / DESIGN_W, self.height() / DESIGN_H)
        p.translate((self.width() - DESIGN_W * scale) / 2.0, (self.height() - DESIGN_H * scale) / 2.0)
        p.scale(scale, scale)

        if self._background:
            self._draw_stage(p)
        self._draw_twinkles(p, t)
        self._draw_tracks(p)
        self._draw_ripples(p, t)
        self._draw_arcs(p, t)
        self._draw_orbit_dots(p, t)
        self._draw_core(p, t)
        self._draw_wordmark(p, t)
        self._draw_status(p, t)
        p.end()

    def _draw_stage(self, p):
        path = QPainterPath()
        path.addRoundedRect(QRectF(0.5, 0.5, DESIGN_W - 1, DESIGN_H - 1), 12, 12)
        p.setPen(QPen(STAGE_BORDER, 1))
        p.setBrush(STAGE)
        p.drawPath(path)

    def _draw_twinkles(self, p, t):
        p.setPen(Qt.PenStyle.NoPen)
        for x, y, r, color, delay in TWINKLES:
            phase = ((t - delay) % 2.5) / 2.5
            alpha = 0.15 + 0.75 * _smooth(phase)
            p.setBrush(_color(color, alpha))
            p.drawEllipse(QPointF(x, y), r, r)

    def _draw_tracks(self, p):
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(_color(CYAN, 0.10), 1))
        p.drawEllipse(QPointF(CX, CY), 120, 120)
        p.setPen(QPen(_color(VIOLET, 0.12), 1))
        p.drawEllipse(QPointF(CX, CY), 100, 100)

    def _draw_ripples(self, p, t):
        p.setBrush(Qt.BrushStyle.NoBrush)
        for offset, color in ((0.0, CYAN), (1.5, VIOLET)):
            phase = ((t + offset) % 3.0) / 3.0
            radius = 58.0 * (0.7 + 1.4 * phase)
            p.setPen(QPen(_color(color, 0.6 * (1.0 - phase)), 1.5))
            p.drawEllipse(QPointF(CX, CY), radius, radius)

    def _gradient_pen(self, radius, width):
        grad = QLinearGradient(QPointF(-radius, -radius), QPointF(radius, radius))
        grad.setColorAt(0.0, CYAN)
        grad.setColorAt(1.0, VIOLET)
        return QPen(
            QBrush(grad),
            width,
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        )

    def _draw_arcs(self, p, t):
        p.setBrush(Qt.BrushStyle.NoBrush)

        p.save()
        p.translate(CX, CY)
        p.rotate(360.0 * (t / 6.0))
        r = 120.0
        p.setPen(self._gradient_pen(r, 4))
        p.drawArc(QRectF(-r, -r, 2 * r, 2 * r), 0, int(-90.7 * 16))
        p.restore()

        p.save()
        p.translate(CX, CY)
        p.rotate(-360.0 * (t / 7.0))
        r = 100.0
        p.setPen(self._gradient_pen(r, 3))
        rect = QRectF(-r, -r, 2 * r, 2 * r)
        p.drawArc(rect, 0, int(-63.0 * 16))
        p.drawArc(rect, int(-180.0 * 16), int(-63.0 * 16))
        p.restore()

        p.save()
        p.translate(CX, CY)
        p.rotate(360.0 * (t / 16.0))
        r = 80.0
        pen = QPen(_color(ICE, 0.45), 2)
        pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        p.setPen(pen)
        rect = QRectF(-r, -r, 2 * r, 2 * r)
        for k in range(10):
            p.drawArc(rect, int(-k * 36.0 * 16), int(-21.5 * 16))
        p.restore()

    def _draw_orbit_dots(self, p, t):
        p.save()
        p.translate(CX, CY)
        p.rotate(360.0 * (t / 12.0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(VIOLET)
        p.drawEllipse(QPointF(140, 0), 4, 4)
        p.setBrush(CYAN)
        p.drawEllipse(QPointF(-140, 0), 3, 3)
        p.restore()

    def _draw_core(self, p, t):
        p.setPen(QPen(_color(CYAN, 0.3), 1))
        p.setBrush(CORE_FILL)
        p.drawEllipse(QPointF(CX, CY), 62, 62)

        if self._level_target is not None:
            self._level += (self._level_target - self._level) * 0.25

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#f2fbff"))
        for i in range(7):
            phase = ((t + BAR_DELAYS[i]) % BAR_DURATIONS[i]) / BAR_DURATIONS[i]
            wave = _smooth(phase)
            if self._level_target is None:
                s = 0.2 + 0.8 * wave
            else:
                s = 0.12 + 0.88 * self._level * (0.45 + 0.55 * wave)
            h = 56.0 * s
            x = 230.0 + i * 14.0
            radius = min(3.0, h / 2.0)
            p.drawRoundedRect(QRectF(x, CY - h / 2.0, 6.0, h), radius, radius)

    def _draw_wordmark(self, p, t):
        p.setFont(self._word_font)
        fm = QFontMetricsF(self._word_font)
        n = len(self._wordmark)
        loop = 7.0
        for i, ch in enumerate(self._wordmark):
            delay = 0.2 + 0.15 * i
            if t < delay:
                continue
            phase = ((t - delay) % loop) / loop
            if phase < 0.08:
                k = phase / 0.08
                k = 1.0 - (1.0 - k) ** 3
                alpha, dy = k, 12.0 * (1.0 - k)
            elif phase < 0.88:
                alpha, dy = 1.0, 0.0
            else:
                alpha, dy = 1.0 - (phase - 0.88) / 0.12, 0.0
            x = CX + (i - (n - 1) / 2.0) * 42.0
            p.setPen(_color("#ffffff", alpha))
            p.drawText(QPointF(x - fm.horizontalAdvance(ch) / 2.0, 352.0 + dy), ch)

        line = QLinearGradient(QPointF(215, 0), QPointF(335, 0))
        line.setColorAt(0.0, CYAN)
        line.setColorAt(1.0, VIOLET)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(line))
        p.drawRoundedRect(QRectF(215, 368, 120, 2), 1, 1)

    def _draw_status(self, p, t):
        p.setFont(self._mono_font)
        fm = QFontMetricsF(self._mono_font)
        text_w = fm.horizontalAdvance(self._status)
        total = 6.0 + 8.0 + text_w
        left = CX - total / 2.0

        blink = 1.0 if ((t % 1.6) / 1.6) < 0.5 else 0.15
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_color(GREEN, blink))
        p.drawEllipse(QPointF(left + 3.0, 398.0), 3, 3)

        p.setPen(STATUS_TEXT)
        p.drawText(QPointF(left + 14.0, 402.0), self._status)


class SplashWindow(QWidget):
    def __init__(self, auto_close_seconds=None):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize(550, 430)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.logo = JarvisLogoWidget(self)
        layout.addWidget(self.logo)

        self._drag = None
        if auto_close_seconds:
            QTimer.singleShot(int(auto_close_seconds * 1000), self.close)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.close()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event):
        if self._drag is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, event):
        self._drag = None


def main():
    app = QApplication(sys.argv)
    window = SplashWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
