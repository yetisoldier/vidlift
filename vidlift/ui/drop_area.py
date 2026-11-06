"""A mode-specific drop surface; dropping stages files and never starts jobs."""
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QGroupBox


class SubtitleDropGroup(QGroupBox):
    pathsDropped = pyqtSignal(list)

    def __init__(self, title, parent=None):
        super().__init__(title, parent)
        self.setAcceptDrops(True)
        self.setAccessibleDescription("Drop video files or folders here to stage subtitles only.")

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and any(u.isLocalFile() for u in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.pathsDropped.emit(paths)
            event.acceptProposedAction()
