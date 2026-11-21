"""Sidebar pages. Each takes the main window and reads services from it."""
from __future__ import annotations

from ..qt import QtWidgets, ThemeTokens


class Page(QtWidgets.QWidget):
    key = ""
    searchable = False

    def __init__(self, window, parent=None):
        super().__init__(parent)
        self.window_ = window
        self.layout_ = QtWidgets.QVBoxLayout(self)
        self.layout_.setContentsMargins(28, 18, 28, 20)
        self.layout_.setSpacing(14)

    @property
    def tokens(self) -> ThemeTokens:
        return self.window_.theme_manager.tokens

    def apply_theme(self, tokens: ThemeTokens) -> None:
        pass

    def set_search(self, text: str) -> None:
        pass

    def shown(self) -> None:
        pass
