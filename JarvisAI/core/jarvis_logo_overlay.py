"""Movable, always-on-top desktop host for the supplied animated JARVIS logo."""

from __future__ import annotations

from PyQt6.QtCore import QEvent, QPoint, QSettings, Qt
from PyQt6.QtWidgets import QApplication, QVBoxLayout, QWidget

from core.jarvis_logo import JarvisLogoWidget


class JarvisLogoOverlay(QWidget):
    """Small draggable logo overlay with a persistent screen position."""

    _WIDTH = 330
    _HEIGHT = 258
    _MARGIN = 18

    def __init__(self, parent: QWidget | None = None):
        flags = (
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        super().__init__(parent, flags)
        self.setObjectName("jarvisLogoOverlay")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setMinimumSize(self._WIDTH, self._HEIGHT)
        self.resize(self._WIDTH, self._HEIGHT)
        self._drag_offset: QPoint | None = None
        self._settings = QSettings("JarvisAI", "Desktop")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.logo = JarvisLogoWidget(self)
        # Keep the provided logo's stage, colors, and animation untouched.
        self.logo.set_background(True)
        # Let a drag start anywhere on the logo surface without allowing the
        # full-screen HUD below it to receive clicks through the logo.
        self.logo.installEventFilter(self)
        layout.addWidget(self.logo)

        self._restore_or_place()

    def _restore_or_place(self) -> None:
        screens = QApplication.screens()
        saved_x = self._settings.value("logo_overlay/x", None)
        saved_y = self._settings.value("logo_overlay/y", None)
        if saved_x is not None and saved_y is not None:
            try:
                x, y = int(saved_x), int(saved_y)
                point = QPoint(x, y)
                if any(screen.availableGeometry().contains(point) for screen in screens):
                    self.move(point)
                    return
            except (TypeError, ValueError):
                pass

        screen = QApplication.primaryScreen()
        if screen is None:
            self.move(self._MARGIN, self._MARGIN)
            return
        area = screen.availableGeometry()
        self.move(
            area.right() - self.width() - self._MARGIN,
            area.top() + self._MARGIN,
        )

    def set_state(self, state: str) -> None:
        state = str(state or "INITIALISING").strip().upper().replace(" ", "_")
        friendly = {
            "PROCESSING": "THINKING",
            "STANDBY": "SLEEPING",
            "OFFLINE": "DISCONNECTED",
        }.get(state, state.replace("_", " "))
        self.logo.set_status(friendly)

    def set_audio_level(self, level: float | None) -> None:
        self.logo.set_level(level)

    def eventFilter(self, watched, event):
        if watched is self.logo:
            if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                return True
            if event.type() == QEvent.Type.MouseMove and self._drag_offset is not None:
                if event.buttons() & Qt.MouseButton.LeftButton:
                    self.move(event.globalPosition().toPoint() - self._drag_offset)
                return True
            if event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
                self._drag_offset = None
                self._save_position()
                return True
        return super().eventFilter(watched, event)

    def _save_position(self) -> None:
        self._settings.setValue("logo_overlay/x", self.x())
        self._settings.setValue("logo_overlay/y", self.y())
        self._settings.sync()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._drag_offset is not None:
            self._drag_offset = None
            self._save_position()
            event.accept()
            return
        super().mouseReleaseEvent(event)
