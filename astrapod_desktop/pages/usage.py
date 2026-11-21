"""Usage: each provider's five-hour and weekly limits as animated rings,
from the statistics the daemon reads out of T3 Code."""
from __future__ import annotations

from .. import fmt
from ..qt import QtWidgets, Qt
from ..widgets import PageHeader, RingGauge, label, make_badge, set_badge
from . import Page
from .songs import EmptyState

STATUS = {"ok": ("最新", "success"), "stale": ("数据较旧", "busy"), "error": ("读取失败", "danger"),
          "unavailable": ("T3 不可用", "danger"), "t3_unavailable": ("T3 不可用", "danger")}
NOTES = {
    "error": "无法从 T3 Code 读取这项用量。",
    "unavailable": "T3 Code 没有运行或无法读取统计，显示的数字可能不是最新。",
    "t3_unavailable": "T3 Code 没有运行或无法读取统计，显示的数字可能不是最新。",
}


class ProviderCard(QtWidgets.QFrame):
    def __init__(self, page):
        super().__init__()
        self.setProperty("card", True)
        self.page = page
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        head = QtWidgets.QHBoxLayout()
        self.name = label("", "hero-title")
        self.badge = make_badge("")
        head.addWidget(self.name)
        head.addStretch(1)
        head.addWidget(self.badge)
        layout.addLayout(head)
        self.note = label("", "helper", wrap=True)
        self.note.setVisible(False)
        layout.addWidget(self.note)
        rings = QtWidgets.QHBoxLayout()
        rings.setSpacing(18)
        self.five = RingGauge("5 小时")
        self.week = RingGauge("本周")
        rings.addStretch(1)
        rings.addWidget(self.five)
        rings.addWidget(self.week)
        rings.addStretch(1)
        layout.addLayout(rings)
        facts = QtWidgets.QHBoxLayout()
        self.tokens_label = self._fact(facts, "本周 Token")
        self.cost_label = self._fact(facts, "本周费用")
        layout.addLayout(facts)

    @staticmethod
    def _fact(row, caption):
        column = QtWidgets.QVBoxLayout()
        column.setSpacing(0)
        value = label("—", "stat-value")
        value.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        cap = label(caption, "stat-label")
        cap.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        column.addWidget(value)
        column.addWidget(cap)
        row.addLayout(column, 1)
        return value

    def update_row(self, row: dict, tokens) -> None:
        self.name.setText(row.get("provider") or "")
        status = row.get("status")
        text, tone = STATUS.get(status, (status or "未知", "neutral"))
        set_badge(self.badge, text, tone)
        self.note.setText(NOTES.get(status, ""))
        self.note.setVisible(status in NOTES)
        for gauge, window in ((self.five, row.get("five_hour") or {}),
                              (self.week, row.get("weekly") or {})):
            gauge.tokens = tokens
            gauge.set_value(window.get("percent"), fmt.until(window.get("resets_at")))
        tokens_week = row.get("tokens_week")
        self.tokens_label.setText(_compact(tokens_week) if tokens_week is not None else "—")
        cost = row.get("cost_usd")
        self.cost_label.setText(f"${cost:,.2f}" if isinstance(cost, (int, float)) else "—")


def _compact(value) -> str:
    value = float(value)
    if value >= 1e8:
        return f"{value / 1e8:.2f} 亿"
    if value >= 1e4:
        return f"{value / 1e4:.0f} 万"
    return f"{value:.0f}"


class UsagePage(Page):
    key = "usage"

    def __init__(self, window, parent=None):
        super().__init__(window, parent)
        self.header = PageHeader("Usage", "各家 AI 服务的用量和重置时间，来自 T3 Code 的统计")
        self.layout_.addWidget(self.header)
        self.grid_host = QtWidgets.QWidget()
        self.grid = QtWidgets.QGridLayout(self.grid_host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(14)
        self.empty = EmptyState("usage", "还没有用量数据",
                                "后台远程服务从 T3 Code 读到用量后会显示在这里。")
        self.stack = QtWidgets.QStackedWidget()
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.grid_host)
        self.stack.addWidget(scroll)
        self.stack.addWidget(self.empty)
        self.layout_.addWidget(self.stack, 1)
        self.cards: list[ProviderCard] = []
        self.rows: list = []
        window.daemon.state_changed.connect(self.update_state)
        self.update_state(window.daemon.state)

    def update_state(self, state: dict) -> None:
        self.rows = state.get("usage") or []
        while len(self.cards) < len(self.rows):
            card = ProviderCard(self)
            self.cards.append(card)
        for i, card in enumerate(self.cards):
            if i < len(self.rows):
                card.update_row(self.rows[i], self.tokens)
                self.grid.addWidget(card, i // 2, i % 2)
                card.show()
            else:
                card.hide()
        self.grid.setRowStretch(max(1, (len(self.rows) + 1) // 2), 1)
        self.stack.setCurrentIndex(0 if self.rows else 1)

    def apply_theme(self, tokens) -> None:
        for card in self.cards:
            for gauge in (card.five, card.week):
                gauge.tokens = tokens
                gauge.update()
        self.empty.apply_theme(tokens)
