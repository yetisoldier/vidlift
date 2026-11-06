"""Scrollable review for large destructive batches. No execution logic."""
from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QPlainTextEdit, QVBoxLayout


class BatchReviewDialog(QDialog):
    def __init__(self, title: str, details: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(720, 420)
        layout = QVBoxLayout(self)
        intro = QLabel("Review every affected source and output. No work starts until you choose Start reviewed jobs.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.review = QPlainTextEdit()
        self.review.setReadOnly(True)
        self.review.setAccessibleName("Affected files and post-success deletion options")
        self.review.setPlainText(details)
        layout.addWidget(self.review)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Yes | QDialogButtonBox.StandardButton.No)
        self.start_button = buttons.button(QDialogButtonBox.StandardButton.Yes)
        self.back_button = buttons.button(QDialogButtonBox.StandardButton.No)
        assert self.start_button is not None and self.back_button is not None
        self.start_button.setText("Start reviewed jobs")
        self.start_button.setAutoDefault(False)
        self.start_button.setDefault(False)
        self.back_button.setText("Go back")
        self.back_button.setDefault(True)
        self.back_button.setFocus()
        self.start_button.clicked.connect(self.accept)
        self.back_button.clicked.connect(self.reject)
        layout.addWidget(buttons)
