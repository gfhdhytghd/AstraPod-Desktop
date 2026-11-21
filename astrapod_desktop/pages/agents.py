"""Agents: the one agent source the owner chose in Settings -- herdr
workspaces, tabs and agents, or T3 Code threads -- and the questions they ask."""
from __future__ import annotations

import json
import time

from ..qt import QtCore, QtGui, QtWidgets, Qt
from ..widgets import (PageHeader, button, discard, elapsed_text, label, make_badge, make_card,
                       qcolor, set_badge)
from . import Page
from .songs import EmptyState

R = QtCore.QRectF
DATA_ROLE = Qt.ItemDataRole.UserRole + 50
STATUS_TEXT = {"working": "工作中", "waiting": "等待回复", "done": "已完成", "idle": "空闲"}
T3_STATUS_TEXT = {"running": "运行中", "idle": "空闲", "completed": "已完成"}
CONTROL_LABELS = {"herdr": "herdr", "t3": "T3 Code"}


def agent_control(state: dict) -> str:
    """The source the daemon controls; herdr when the daemon does not say."""
    control = (state.get("agents") or {}).get("control")
    return control if control in CONTROL_LABELS else "herdr"


def question_source(question: dict) -> str:
    return "t3" if str(question.get("id") or "").startswith("t3") else "herdr"


def status_color(tokens, status: str) -> QtGui.QColor:
    return QtGui.QColor(getattr(tokens, status, tokens.idle) if status in
                        ("working", "waiting", "done", "idle") else tokens.idle)


class WorkspaceDelegate(QtWidgets.QStyledItemDelegate):
    def __init__(self, page):
        super().__init__(page)
        self.page = page

    def sizeHint(self, option, _index):
        return QtCore.QSize(option.rect.width(), 64)

    def paint(self, painter, option, index):
        t = self.page.tokens
        ws = index.data(DATA_ROLE)
        painter.save()
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        rect = R(option.rect).adjusted(4, 3, -4, -3)
        selected = option.state & QtWidgets.QStyle.StateFlag.State_Selected
        painter.setPen(QtGui.QPen(qcolor(t.glass_border if selected else t.hairline), 1))
        painter.setBrush(qcolor(t.glass_strong if selected else t.row_hover))
        painter.drawRoundedRect(rect, 10, 10)
        if selected:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QtGui.QColor(t.accent))
            painter.drawRoundedRect(R(rect.left() + 1, rect.top() + 12, 3, rect.height() - 24), 1.5, 1.5)
        font = QtGui.QFont(option.font)
        font.setPixelSize(14)
        font.setWeight(QtGui.QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.setPen(QtGui.QColor(t.text_primary))
        painter.drawText(R(rect.left() + 14, rect.top() + 8, rect.width() - 20, 20),
                         Qt.AlignmentFlag.AlignVCenter, ws.get("name") or ws.get("id") or "")
        counts = ws.get("counts") or {}
        x = rect.left() + 14
        font.setPixelSize(11)
        painter.setFont(font)
        metrics = QtGui.QFontMetrics(font)
        for status in ("working", "waiting", "done", "idle"):
            value = counts.get(status) or 0
            if not value:
                continue
            color = status_color(t, status)
            text = f"{value} {STATUS_TEXT[status]}"
            width = metrics.horizontalAdvance(text) + 22
            chip = R(x, rect.top() + 34, width, 20)
            tint = QtGui.QColor(color)
            tint.setAlphaF(0.14)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(tint)
            painter.drawRoundedRect(chip, 10, 10)
            painter.setBrush(color)
            painter.drawEllipse(QtCore.QPointF(chip.left() + 9, chip.center().y()), 3, 3)
            painter.setPen(color.darker(115) if t.name == "light" else color)
            painter.drawText(chip.adjusted(16, 0, 0, 0), Qt.AlignmentFlag.AlignVCenter, text)
            x += width + 6
        painter.restore()


class AgentDelegate(QtWidgets.QStyledItemDelegate):
    def __init__(self, page):
        super().__init__(page)
        self.page = page
        self.phase = 0.0

    def sizeHint(self, option, _index):
        return QtCore.QSize(option.rect.width(), 58)

    def paint(self, painter, option, index):
        t = self.page.tokens
        agent = index.data(DATA_ROLE)
        painter.save()
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        rect = R(option.rect).adjusted(4, 2, -4, -2)
        if option.state & QtWidgets.QStyle.StateFlag.State_Selected:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(qcolor(t.row_selected))
            painter.drawRoundedRect(rect, 9, 9)
        elif option.state & QtWidgets.QStyle.StateFlag.State_MouseOver:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(qcolor(t.row_hover))
            painter.drawRoundedRect(rect, 9, 9)
        status = agent.get("status") or "idle"
        color = status_color(t, status)
        center = QtCore.QPointF(rect.left() + 20, rect.center().y())
        if status == "working":
            halo = QtGui.QColor(color)
            halo.setAlphaF(0.35 * (1 - self.phase))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(halo)
            painter.drawEllipse(center, 5 + 6 * self.phase, 5 + 6 * self.phase)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(center, 5, 5)
        font = QtGui.QFont(option.font)
        font.setPixelSize(14)
        font.setWeight(QtGui.QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.setPen(QtGui.QColor(t.text_primary))
        name = agent.get("name") or agent.get("pane") or ""
        metrics = QtGui.QFontMetrics(font)
        painter.drawText(R(rect.left() + 40, rect.top() + 7, rect.width() - 200, 20),
                         Qt.AlignmentFlag.AlignVCenter, name)
        name_width = metrics.horizontalAdvance(name)
        kind = agent.get("kind") or ""
        if kind:
            font.setPixelSize(11)
            font.setWeight(QtGui.QFont.Weight.Normal)
            painter.setFont(font)
            km = QtGui.QFontMetrics(font)
            chip = R(rect.left() + 48 + name_width, rect.top() + 9, km.horizontalAdvance(kind) + 12, 17)
            painter.setPen(QtGui.QPen(qcolor(t.hairline), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(chip, 8.5, 8.5)
            painter.setPen(QtGui.QColor(t.text_muted))
            painter.drawText(chip, Qt.AlignmentFlag.AlignCenter, kind)
        font.setPixelSize(12)
        font.setWeight(QtGui.QFont.Weight.Normal)
        painter.setFont(font)
        painter.setPen(QtGui.QColor(t.text_secondary))
        metrics = QtGui.QFontMetrics(font)
        activity = agent.get("activity") or STATUS_TEXT.get(status, status)
        painter.drawText(R(rect.left() + 40, rect.top() + 30, rect.width() - 160, 18),
                         Qt.AlignmentFlag.AlignVCenter,
                         metrics.elidedText(activity, Qt.TextElideMode.ElideRight, int(rect.width() - 170)))
        painter.setPen(color if status != "idle" else QtGui.QColor(t.text_muted))
        right = R(rect.right() - 130, rect.top() + 7, 120, 20)
        painter.drawText(right, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                         STATUS_TEXT.get(status, status))
        painter.setPen(QtGui.QColor(t.text_muted))
        painter.drawText(right.translated(0, 22), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                         elapsed_text(agent.get("since")))
        painter.restore()


class QuestionCard(QtWidgets.QFrame):
    def __init__(self, page, question: dict):
        super().__init__()
        self.setProperty("card", True)
        self.question = question
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(8)
        head = QtWidgets.QHBoxLayout()
        dot = QtWidgets.QLabel()
        dot.setFixedSize(10, 10)
        dot.setStyleSheet(f"background: {page.tokens.waiting}; border-radius: 5px;")
        head.addWidget(dot)
        head.addWidget(label(question.get("title") or "需要回复", "card-title"))
        head.addStretch(1)
        dismiss = button("忽略", "ghost", None,
                         lambda: page.dismiss(question))
        dismiss.setToolTip("先不回答，从列表里移除")
        head.addWidget(dismiss)
        layout.addLayout(head)
        text = label(question.get("text") or "", None, wrap=True)
        text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(text)
        options = QtWidgets.QHBoxLayout()
        options.setSpacing(8)
        self.option_buttons = []
        for option in question.get("options") or []:
            widget = button(f"{option.get('number')}. {option.get('label')}", "option", None,
                            lambda _c=False, o=option: page.answer(question, o))
            options.addWidget(widget)
            self.option_buttons.append(widget)
        options.addStretch(1)
        layout.addLayout(options)


class AgentsPage(Page):
    key = "agents"

    def __init__(self, window, parent=None):
        super().__init__(window, parent)
        self.header = PageHeader("Agent")
        self.source_badge = self.header.add(make_badge("", "accent"))
        self.herdr_badge = self.header.add(make_badge("herdr 未连接"))
        self.layout_.addWidget(self.header)

        self.questions_host = QtWidgets.QWidget()
        self.questions_box = QtWidgets.QVBoxLayout(self.questions_host)
        self.questions_box.setContentsMargins(0, 0, 0, 0)
        self.questions_box.setSpacing(10)
        self.questions_scroll = QtWidgets.QScrollArea()
        self.questions_scroll.setWidgetResizable(True)
        self.questions_scroll.setWidget(self.questions_host)
        self.questions_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.layout_.addWidget(self.questions_scroll)

        self.body = QtWidgets.QStackedWidget()
        split = QtWidgets.QSplitter(Qt.Orientation.Horizontal)
        split.setHandleWidth(14)
        split.setChildrenCollapsible(False)
        left, left_layout = make_card(6, expand=True, margins=(8, 12, 8, 8))
        left_layout.addWidget(label("工作区", "section-subtitle"))
        self.ws_model = QtGui.QStandardItemModel(self)
        self.ws_view = QtWidgets.QListView()
        self.ws_view.setModel(self.ws_model)
        self.ws_view.setItemDelegate(WorkspaceDelegate(self))
        self.ws_view.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.ws_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.ws_view.setAccessibleName("工作区")
        self.ws_view.selectionModel().currentChanged.connect(lambda *_: self._fill_tabs())
        left_layout.addWidget(self.ws_view)
        split.addWidget(left)

        right, right_layout = make_card(8, expand=True, margins=(14, 12, 14, 10))
        ws_head = QtWidgets.QHBoxLayout()
        self.ws_title = label("", "card-title")
        ws_head.addWidget(self.ws_title)
        ws_head.addStretch(1)
        self.focus_ws = button("聚焦工作区", None, None, self.focus_workspace)
        ws_head.addWidget(self.focus_ws)
        right_layout.addLayout(ws_head)
        self.tab_bar = QtWidgets.QTabBar()
        self.tab_bar.setDrawBase(False)
        self.tab_bar.setExpanding(False)
        self.tab_bar.currentChanged.connect(lambda _i: self._fill_agents())
        right_layout.addWidget(self.tab_bar)
        self.agent_model = QtGui.QStandardItemModel(self)
        self.agent_view = QtWidgets.QListView()
        self.agent_view.setModel(self.agent_model)
        self.agent_delegate = AgentDelegate(self)
        self.agent_view.setItemDelegate(self.agent_delegate)
        self.agent_view.setMouseTracking(True)
        self.agent_view.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.agent_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.agent_view.setAccessibleName("Agent")
        self.agent_view.doubleClicked.connect(lambda _i: self.focus_pane())
        self.agent_view.selectionModel().currentChanged.connect(lambda *_: self._update_actions())
        right_layout.addWidget(self.agent_view, 1)
        actions = QtWidgets.QHBoxLayout()
        self.focus_tab_button = button("聚焦标签页", None, None, self.focus_tab)
        self.focus_pane_button = button("聚焦这个 Agent", "primary", None, self.focus_pane)
        self.detail_button = button("详情", None, "info", self.show_detail)
        actions.addWidget(self.detail_button)
        actions.addStretch(1)
        actions.addWidget(self.focus_tab_button)
        actions.addWidget(self.focus_pane_button)
        right_layout.addLayout(actions)
        split.addWidget(right)
        split.setSizes([300, 700])
        split.setStretchFactor(1, 1)
        self.empty = EmptyState("agents", "没有连接 herdr",
                                "在设置里填好 herdr 套接字，后台远程服务连上后，这里会显示所有 Agent。")
        self.body.addWidget(split)
        self.body.addWidget(self.empty)
        self.body.addWidget(self._t3_panel())
        self.layout_.addWidget(self.body, 1)

        self._pulse = QtCore.QTimer(self)
        self._pulse.setInterval(60)
        self._pulse.timeout.connect(self._tick)
        self.state: dict = {}
        self.control = "herdr"
        self.question_cards: dict = {}
        window.daemon.state_changed.connect(self.update_state)
        self.update_state(window.daemon.state)

    def _t3_panel(self) -> QtWidgets.QWidget:
        panel, panel_layout = make_card(8, expand=True, margins=(14, 12, 14, 10))
        head = QtWidgets.QHBoxLayout()
        head.addWidget(label("T3 Code 线程", "card-title"))
        head.addStretch(1)
        panel_layout.addLayout(head)
        self.t3_stack = QtWidgets.QStackedWidget()
        self.t3_list = QtWidgets.QListWidget()
        self.t3_list.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.t3_list.setAccessibleName("T3 线程")
        self.t3_list.currentRowChanged.connect(lambda _r: self._update_t3_actions())
        self.t3_list.itemDoubleClicked.connect(lambda _i: self.open_t3_thread())
        self.t3_empty = EmptyState("agents", "T3 Code", "")
        self.t3_stack.addWidget(self.t3_list)
        self.t3_stack.addWidget(self.t3_empty)
        panel_layout.addWidget(self.t3_stack, 1)
        actions = QtWidgets.QHBoxLayout()
        actions.addStretch(1)
        self.t3_open_button = button("在 T3 中打开", "primary", None, self.open_t3_thread)
        actions.addWidget(self.t3_open_button)
        panel_layout.addLayout(actions)
        return panel

    # State -----------------------------------------------------------
    def _selected_ws(self) -> dict | None:
        index = self.ws_view.currentIndex()
        return index.data(DATA_ROLE) if index.isValid() else None

    def _selected_tab(self) -> dict | None:
        ws = self._selected_ws()
        i = self.tab_bar.currentIndex()
        tabs = (ws or {}).get("tabs") or []
        return tabs[i] if 0 <= i < len(tabs) else None

    def _selected_agent(self) -> dict | None:
        index = self.agent_view.currentIndex()
        return index.data(DATA_ROLE) if index.isValid() else None

    def live_questions(self) -> list:
        """Questions from the controlled source that are still open."""
        return [q for q in self.state.get("questions") or []
                if not q.get("dismissed") and question_source(q) == self.control]

    def update_state(self, state: dict) -> None:
        self.state = state
        self.control = agent_control(state)
        set_badge(self.source_badge, f"控制：{CONTROL_LABELS[self.control]}", "accent")
        if self.control == "t3":
            self._show_t3(state)
        else:
            self._show_herdr(state)
        self._update_questions(self.live_questions())
        self.window_.sidebar.set_badge("agents", len(self.live_questions()))

    def _show_t3(self, state: dict) -> None:
        t3 = state.get("t3") or {}
        if not self.window_.daemon.connected:
            set_badge(self.herdr_badge, "后台服务未连接", "neutral")
        elif t3.get("error"):
            set_badge(self.herdr_badge, "T3 出错", "danger")
        elif t3.get("connected"):
            set_badge(self.herdr_badge, "T3 已连接", "success")
        else:
            set_badge(self.herdr_badge, "T3 未连接", "neutral")
        self.body.setCurrentIndex(2)
        threads = [t for t in t3.get("threads") or []
                   if not (t.get("archived") or t.get("settled") or t.get("snoozed"))]
        current = (self._selected_thread() or {}).get("id")
        blocked = self.t3_list.blockSignals(True)
        self.t3_list.clear()
        for thread in threads:
            status = "等待确认" if thread.get("waiting") else T3_STATUS_TEXT.get(
                thread.get("status"), thread.get("status") or "")
            facts = " · ".join(x for x in (status, thread.get("model"), thread.get("branch")) if x)
            item = QtWidgets.QListWidgetItem(f"{thread.get('title') or thread.get('id')}\n{facts}")
            item.setData(DATA_ROLE, thread)
            self.t3_list.addItem(item)
            if thread.get("id") == current:
                self.t3_list.setCurrentItem(item)
        self.t3_list.blockSignals(blocked)
        if threads:
            self.t3_stack.setCurrentWidget(self.t3_list)
        else:
            self.t3_empty.text.setText(
                str(t3["error"]) if t3.get("error") else
                "iPod 的 Agents 正在控制 T3 Code 的线程。" if t3.get("connected") else
                "在设置里连接 T3 后，iPod 的 Agents 会显示 T3 项目和线程。")
            self.t3_stack.setCurrentWidget(self.t3_empty)
        self._pulse.stop()
        self._update_t3_actions()

    def _selected_thread(self) -> dict | None:
        item = self.t3_list.currentItem()
        return item.data(DATA_ROLE) if item is not None else None

    def _update_t3_actions(self) -> None:
        self.t3_open_button.setEnabled(bool((self._selected_thread() or {}).get("link")))

    def open_t3_thread(self) -> None:
        thread = self._selected_thread()
        if thread and thread.get("link"):
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(thread["link"]))

    def _show_herdr(self, state: dict) -> None:
        herdr = state.get("herdr") or {}
        workspaces = herdr.get("workspaces") or []
        if herdr.get("ok"):
            set_badge(self.herdr_badge, f"herdr 已连接，{len(workspaces)} 个工作区", "success")
        elif self.window_.daemon.connected:
            set_badge(self.herdr_badge, "herdr 不可用", "danger")
        else:
            set_badge(self.herdr_badge, "后台服务未连接", "neutral")
        self.body.setCurrentIndex(0 if workspaces else 1)
        selected = (self._selected_ws() or {}).get("id")
        blocked = self.ws_view.selectionModel().blockSignals(True)
        self.ws_model.clear()
        row_to_select = 0
        for row, ws in enumerate(workspaces):
            item = QtGui.QStandardItem(ws.get("name") or "")
            item.setData(ws, DATA_ROLE)
            item.setEditable(False)
            self.ws_model.appendRow(item)
            if ws.get("id") == selected:
                row_to_select = row
        self.ws_view.selectionModel().blockSignals(blocked)
        if workspaces:
            self.ws_view.setCurrentIndex(self.ws_model.index(row_to_select, 0))
        self._fill_tabs()

    def _fill_tabs(self) -> None:
        ws = self._selected_ws()
        current_tab = (self._selected_tab() or {}).get("id")
        blocked = self.tab_bar.blockSignals(True)
        while self.tab_bar.count():
            self.tab_bar.removeTab(0)
        index_to_select = 0
        for i, tab in enumerate((ws or {}).get("tabs") or []):
            busy = sum(1 for a in tab.get("agents") or [] if a.get("status") == "working")
            self.tab_bar.addTab(f"{tab.get('name')}" + (f"（{busy}）" if busy else ""))
            if tab.get("id") == current_tab:
                index_to_select = i
        self.tab_bar.setCurrentIndex(index_to_select)
        self.tab_bar.blockSignals(blocked)
        self.ws_title.setText((ws or {}).get("name") or "")
        self.focus_ws.setEnabled(ws is not None)
        self._fill_agents()

    def _fill_agents(self) -> None:
        tab = self._selected_tab()
        current = (self._selected_agent() or {}).get("pane")
        self.agent_model.clear()
        row_to_select = -1
        any_working = False
        for row, agent in enumerate((tab or {}).get("agents") or []):
            item = QtGui.QStandardItem(agent.get("name") or "")
            item.setData(agent, DATA_ROLE)
            item.setEditable(False)
            self.agent_model.appendRow(item)
            any_working |= agent.get("status") == "working"
            if agent.get("pane") == current:
                row_to_select = row
        if self.agent_model.rowCount():
            self.agent_view.setCurrentIndex(self.agent_model.index(max(0, row_to_select), 0))
        if any_working and self.window_.motion:
            self._pulse.start()
        else:
            self._pulse.stop()
        self._update_actions()

    def _update_actions(self) -> None:
        self.focus_tab_button.setEnabled(self._selected_tab() is not None)
        has_agent = self._selected_agent() is not None
        self.focus_pane_button.setEnabled(has_agent)
        self.detail_button.setEnabled(has_agent)

    def _tick(self) -> None:
        self.agent_delegate.phase = (self.agent_delegate.phase + 0.04) % 1.0
        self.agent_view.viewport().update()

    def _update_questions(self, live) -> None:
        ids = [q.get("id") for q in live]
        for qid in list(self.question_cards):
            if qid not in ids:
                discard(self.question_cards.pop(qid))
        for question in live:
            if question.get("id") not in self.question_cards:
                card = QuestionCard(self, question)
                self.question_cards[question.get("id")] = card
                self.questions_box.addWidget(card)
        self.questions_scroll.setVisible(bool(live))
        if live:
            self.questions_host.adjustSize()
            height = sum(card.sizeHint().height() for card in self.question_cards.values())
            height += 10 * (len(self.question_cards) - 1)
            self.questions_scroll.setFixedHeight(min(height + 2, 300))

    # Actions ----------------------------------------------------------
    def _result(self, done_text: str):
        def callback(ok, result):
            if ok:
                self.window_.flash(done_text)
            else:
                self.window_.flash(f"没有完成：{result}")
        return callback

    def answer(self, question, option) -> None:
        for card in self.question_cards.values():
            if card.question is question:
                for widget in card.option_buttons:
                    widget.setEnabled(False)
        self.window_.daemon.request("answer", self._result(f"已回答：{option.get('label')}"),
                                    question=question.get("id"), number=option.get("number"))

    def dismiss(self, question) -> None:
        self.window_.daemon.request("dismiss", self._result("已忽略这个问题"),
                                    question=question.get("id"))

    def focus_workspace(self) -> None:
        ws = self._selected_ws()
        if ws:
            self.window_.daemon.request("focus_workspace", self._result(f"已聚焦工作区 {ws.get('name')}"),
                                        workspace=ws.get("id"))

    def focus_tab(self) -> None:
        ws, tab = self._selected_ws(), self._selected_tab()
        if ws and tab:
            self.window_.daemon.request("focus_tab", self._result(f"已切换到 {tab.get('name')}"),
                                        workspace=ws.get("id"), tab=tab.get("id"))

    def focus_pane(self) -> None:
        agent = self._selected_agent()
        if agent:
            self.window_.daemon.request("focus_pane", self._result(f"已聚焦 {agent.get('name')}"),
                                        pane=agent.get("pane"))

    def show_detail(self) -> None:
        agent = self._selected_agent()
        if not agent:
            return

        def shown(ok, result):
            if not ok:
                self.window_.flash(f"无法读取详情：{result}")
                return
            dialog = QtWidgets.QDialog(self)
            dialog.setWindowTitle(f"{agent.get('name')} 的详情")
            dialog.resize(560, 420)
            layout = QtWidgets.QVBoxLayout(dialog)
            layout.setContentsMargins(20, 18, 20, 16)
            layout.addWidget(label(agent.get("name") or "", "hero-title"))
            text = QtWidgets.QPlainTextEdit()
            text.setReadOnly(True)
            text.setObjectName("LogView")
            text.setPlainText(detail_text(result))
            layout.addWidget(text, 1)
            row = QtWidgets.QHBoxLayout()
            row.addStretch(1)
            row.addWidget(button("完成", "primary", None, dialog.accept))
            layout.addLayout(row)
            dialog.show()
            self.detail_dialog = dialog

        self.window_.daemon.request("detail", shown, pane=agent.get("pane"))

    def apply_theme(self, tokens) -> None:
        for card in self.question_cards.values():
            discard(card)
        self.question_cards.clear()
        self._update_questions(self.live_questions())
        self.empty.apply_theme(tokens)
        self.t3_empty.apply_theme(tokens)


def detail_text(record) -> str:
    """The daemon's detail record as the plain text the dialog shows."""
    if not isinstance(record, dict):
        return json.dumps(record, ensure_ascii=False, indent=2)
    parts = []
    for key, caption in (("task", "任务"), ("now", "现在"), ("say", "说明")):
        if record.get(key):
            parts.append(f"{caption}：{record[key]}")
    if isinstance(record.get("age"), (int, float)):
        ago = elapsed_text(time.time() - record["age"])
        parts.append("刚刚更新" if ago == "刚刚" else f"{ago}前更新")
    steps = [step for step in record.get("steps") or [] if step]
    if steps:
        parts.append("步骤：\n" + "\n".join(f"  {i}. {step}" for i, step in enumerate(steps, 1)))
    if record.get("input"):
        parts.append("输入框里：\n" + record["input"])
    return "\n\n".join(parts) or "这个 Agent 没有可显示的详情。"
