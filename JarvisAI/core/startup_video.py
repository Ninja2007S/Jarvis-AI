"""Optional native startup video splash with embedded audio."""

from __future__ import annotations

import sys
from pathlib import Path

from PyQt6.QtCore import Qt, QUrl, pyqtSignal
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
from PyQt6.QtMultimediaWidgets import QVideoWidget
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget


def startup_video_path() -> Path:
    """Return the media path beside the source tree or frozen executable."""
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
    else:
        base = Path(__file__).resolve().parent.parent
    return base / "assets" / "startup_intro.mp4"


class StartupVideoSplash(QWidget):
    """Frameless video splash that hands startup to the HUD when finished."""

    finished = pyqtSignal()

    def __init__(self, video_path: Path):
        super().__init__(None, Qt.WindowType.FramelessWindowHint)
        self._finished = False
        self.setWindowTitle("JARVIS startup")
        self.setStyleSheet(
            "QWidget { background:#050b10; color:#d8f5f3; }"
            "QLabel#brand { color:#57e6d1; font:700 13px 'Consolas'; letter-spacing:3px; }"
            "QPushButton { background:#10222a; color:#bcece5; border:1px solid #32666a;"
            " padding:8px 16px; font:600 10px 'Consolas'; }"
            "QPushButton:hover { background:#17343a; border-color:#57e6d1; }"
        )

        self.video = QVideoWidget(self)
        self.video.setStyleSheet("background:#020609;")
        self.status = QLabel("SYSTEM INITIALIZATION // JARVIS", self)
        self.status.setObjectName("brand")
        self.skip = QPushButton("SKIP INTRO  ›", self)
        self.skip.clicked.connect(self.finish)

        header = QHBoxLayout()
        header.addWidget(self.status)
        header.addStretch(1)
        header.addWidget(self.skip)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(10)
        layout.addLayout(header)
        layout.addWidget(self.video, 1)

        self.audio = QAudioOutput(self)
        self.audio.setVolume(0.78)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video)
        self.player.mediaStatusChanged.connect(self._on_media_status)
        self.player.errorOccurred.connect(self._on_error)
        self.player.setSource(QUrl.fromLocalFile(str(video_path.resolve())))

    def start(self) -> None:
        self.showFullScreen()
        self.player.play()

    def _on_media_status(self, status) -> None:
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self.finish()

    def _on_error(self, *_args) -> None:
        # Unsupported codecs or missing platform multimedia backends should
        # never prevent the normal desktop HUD from opening.
        self.finish()

    def finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        try:
            self.player.stop()
        except Exception:
            pass
        self.close()
        self.finished.emit()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.finish()
            return
        super().keyPressEvent(event)


def create_startup_splash() -> StartupVideoSplash | None:
    """Build the splash when enabled and the optional intro file exists."""
    try:
        from memory.config_manager import get_startup_video_enabled
        if not get_startup_video_enabled():
            return None
    except Exception as exc:
        # A broken preference read must not prevent the normal HUD from opening.
        print(f"[Startup] Could not read intro preference; using default: {exc}")
    path = startup_video_path()
    if not path.is_file():
        return None
    try:
        return StartupVideoSplash(path)
    except Exception as exc:
        print(f"[Startup] Intro video unavailable: {exc}")
        return None
