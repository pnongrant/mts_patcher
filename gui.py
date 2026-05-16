import sys
import re
import json
import sqlite3
import threading
from pathlib import Path

from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QComboBox, QTextEdit, QTableWidget,
    QTableWidgetItem, QTabWidget, QMessageBox, QSpinBox, QHeaderView,
    QProgressBar, QFrame, QDialog, QDialogButtonBox, QCheckBox, QLineEdit
)
from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor

from dispatcher import Dispatcher, DB_PATH, init_db
from qr_worker_core import setup_driver, manual_login, save_session

ALIASES_FILE = "email_aliases.json"

DARK_QSS = """
QWidget { background-color: #121417; color: #E6EAF0; font-family: Segoe UI, Arial, sans-serif; font-size: 13px; }
QLabel { color: #C9D1D9; }
QComboBox, QSpinBox, QTextEdit, QTableWidget, QLineEdit {
    background-color: #1B1F24; border: 1px solid #2C3440; border-radius: 8px; padding: 6px; color: #E6EAF0;
}
QPushButton { background-color: #2B6DE0; border: none; border-radius: 8px; padding: 7px 12px; color: white; font-weight: 600; }
QPushButton:hover { background-color: #3B7CF0; }
QPushButton:pressed { background-color: #245CC0; }
QTabWidget::pane { border: 1px solid #2C3440; border-radius: 10px; top: -1px; }
QTabBar::tab {
    background: #1B1F24; border: 1px solid #2C3440; padding: 8px 12px; margin-right: 4px;
    border-top-left-radius: 8px; border-top-right-radius: 8px;
}
QTabBar::tab:selected { background: #26303A; }
QHeaderView::section {
    background-color: #202833; color: #C9D1D9; border: none; border-right: 1px solid #2C3440; padding: 7px;
}
QTableWidget {
    background-color: #1B1F24;
    alternate-background-color: #202833;
    gridline-color: #2A3440;
    selection-background-color: #2B6DE0;
    selection-color: #FFFFFF;
}
QTableWidget::item {
    background-color: transparent;
    color: #E6EAF0;
}
QTableWidget::item:selected {
    background-color: #2B6DE0;
    color: #FFFFFF;
}
QProgressBar {
    background-color: #1B1F24; border: 1px solid #2C3440; border-radius: 7px; text-align: center; color: #E6EAF0;
}
QProgressBar::chunk { border-radius: 6px; background-color: #2B6DE0; }
QFrame#Card { background-color: #171B21; border: 1px solid #2C3440; border-radius: 10px; }
"""


def normalize_phone(s: str):
    digits = re.sub(r"\D+", "", str(s))
    if len(digits) == 10:
        digits = "7" + digits
    elif len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return digits if len(digits) == 11 and digits.startswith("7") else None


def parse_phone_list(text: str):
    parts = re.split(r"[\s,;]+", text.strip())
    out, seen = [], set()
    for p in parts:
        if not p:
            continue
        n = normalize_phone(p)
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def is_valid_email(email: str) -> bool:
    return re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email or "") is not None


def load_aliases_file():
    p = Path(ALIASES_FILE)
    if not p.exists():
        return {}
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return {str(k).strip().lower(): str(v).strip() for k, v in data.items()}
            return {}
    except Exception:
        return {}


def save_aliases_file(aliases: dict):
    p = Path(ALIASES_FILE)
    with p.open("w", encoding="utf-8") as f:
        json.dump(aliases, f, ensure_ascii=False, indent=2)


class ManualNumbersDialog(QDialog):
    def __init__(self, aliases, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Список номеров вручную")
        self.resize(760, 560)

        lay = QVBoxLayout(self)

        row = QHBoxLayout()
        row.addWidget(QLabel("Alias:"))
        self.alias_box = QComboBox()
        self.alias_box.addItems(sorted(aliases))
        row.addWidget(self.alias_box, 1)
        lay.addLayout(row)

        lay.addWidget(QLabel("Вставь номера списком (строки / запятая / ; / пробел):"))
        self.numbers_edit = QTextEdit()
        self.numbers_edit.setPlaceholderText("79131234567\n+7 (913) 123-45-68\n89131234569")
        lay.addWidget(self.numbers_edit, 1)

        self.chk_append = QCheckBox("Добавлять в существующую очередь alias (без дублей)")
        self.chk_append.setChecked(True)
        lay.addWidget(self.chk_append)

        self.info = QLabel("Предпросмотр: —")
        lay.addWidget(self.info)

        self.btn_preview = QPushButton("Проверить")
        lay.addWidget(self.btn_preview)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.button(QDialogButtonBox.Ok).setText("Добавить в очередь")
        btns.button(QDialogButtonBox.Cancel).setText("Отмена")
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def get_data(self):
        alias = self.alias_box.currentText().strip().lower()
        phones = parse_phone_list(self.numbers_edit.toPlainText())
        append_mode = self.chk_append.isChecked()
        return alias, phones, append_mode


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MTS Dispatcher GUI • Multi Alias")
        self.resize(1320, 860)

        self.dispatcher = Dispatcher()
        self.aliases = sorted(list(self.dispatcher.aliases.keys()))

        self._build_ui()
        self._bind_events()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_all)
        self.timer.start(3000)

        self.refresh_all()
        self.load_aliases_tab()

    def _build_ui(self):
        root = QVBoxLayout(self)

        card = QFrame()
        card.setObjectName("Card")
        top = QGridLayout(card)

        top.addWidget(QLabel("Alias"), 0, 0)
        self.alias_box = QComboBox()
        self.alias_box.addItems(self.aliases)
        top.addWidget(self.alias_box, 0, 1)

        top.addWidget(QLabel("Кол-во номеров из общего списка"), 0, 2)
        self.assign_count_spin = QSpinBox()
        self.assign_count_spin.setRange(1, 100000)
        self.assign_count_spin.setValue(50)
        top.addWidget(self.assign_count_spin, 0, 3)

        self.btn_assign_count = QPushButton("📦 Выдать по количеству")
        top.addWidget(self.btn_assign_count, 0, 4)

        self.btn_send_manual = QPushButton("✍ Внести список вручную")
        top.addWidget(self.btn_send_manual, 0, 5)

        self.btn_login_alias = QPushButton("🔐 Login Alias")
        self.btn_stop_alias = QPushButton("■ Stop Alias")
        self.btn_stop_all = QPushButton("■■ Stop All")
        top.addWidget(self.btn_login_alias, 0, 6)
        top.addWidget(self.btn_stop_alias, 0, 7)
        top.addWidget(self.btn_stop_all, 0, 8)

        self.btn_refresh = QPushButton("⟳ Refresh")
        self.btn_retry_failed = QPushButton("↺ Retry Failed Today")
        self.btn_clear_queued = QPushButton("🧹 Clear Queued")
        top.addWidget(self.btn_refresh, 1, 0)
        top.addWidget(self.btn_retry_failed, 1, 1)
        top.addWidget(self.btn_clear_queued, 1, 2)

        top.addWidget(QLabel("Blocks limit"), 1, 3)
        self.blocks_limit = QSpinBox()
        self.blocks_limit.setRange(1, 2000)
        self.blocks_limit.setValue(50)
        top.addWidget(self.blocks_limit, 1, 4)

        root.addWidget(card)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)

        self.tab_status = QWidget()
        self.tab_blocks = QWidget()
        self.tab_failed = QWidget()
        self.tab_aliases = QWidget()

        self.tabs.addTab(self.tab_status, "Status")
        self.tabs.addTab(self.tab_blocks, "Blocks")
        self.tabs.addTab(self.tab_failed, "Failed")
        self.tabs.addTab(self.tab_aliases, "Aliases")

        st = QVBoxLayout(self.tab_status)
        self.table_status = QTableWidget(0, 3)
        self.table_status.setHorizontalHeaderLabels(["Alias", "Status", "Count"])
        self._table_common(self.table_status)
        st.addWidget(self.table_status)

        bl = QVBoxLayout(self.tab_blocks)
        self.table_blocks = QTableWidget(0, 8)
        self.table_blocks.setHorizontalHeaderLabels(["Block", "Alias", "Range", "Status", "Total", "Success", "Error", "Skipped"])
        self._table_common(self.table_blocks)
        bl.addWidget(self.table_blocks)

        fl = QVBoxLayout(self.tab_failed)
        self.table_failed = QTableWidget(0, 6)
        self.table_failed.setHorizontalHeaderLabels(["ID", "Alias", "Phone", "Row", "Attempts", "Error"])
        self._table_common(self.table_failed)
        fl.addWidget(self.table_failed)

        al = QVBoxLayout(self.tab_aliases)
        aliases_card = QFrame()
        aliases_card.setObjectName("Card")
        ag = QGridLayout(aliases_card)

        self.table_aliases = QTableWidget(0, 2)
        self.table_aliases.setHorizontalHeaderLabels(["Alias", "Email"])
        self._table_common(self.table_aliases)
        self.table_aliases.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        ag.addWidget(self.table_aliases, 0, 0, 1, 6)

        ag.addWidget(QLabel("Alias"), 1, 0)
        self.alias_edit_name = QLineEdit()
        self.alias_edit_name.setPlaceholderText("например: m1")
        ag.addWidget(self.alias_edit_name, 1, 1)

        ag.addWidget(QLabel("Email"), 1, 2)
        self.alias_edit_email = QLineEdit()
        self.alias_edit_email.setPlaceholderText("name@example.com")
        ag.addWidget(self.alias_edit_email, 1, 3)

        self.btn_alias_load_selected = QPushButton("⬇ Load selected")
        self.btn_alias_save = QPushButton("💾 Save / Update")
        self.btn_alias_add = QPushButton("➕ Add new")
        self.btn_alias_reload_dispatcher = QPushButton("♻ Reload aliases in Dispatcher")

        ag.addWidget(self.btn_alias_load_selected, 1, 4)
        ag.addWidget(self.btn_alias_save, 1, 5)
        ag.addWidget(self.btn_alias_add, 2, 4)
        ag.addWidget(self.btn_alias_reload_dispatcher, 2, 5)

        al.addWidget(aliases_card)

        pcard = QFrame()
        pcard.setObjectName("Card")
        pv = QVBoxLayout(pcard)
        pv.addWidget(QLabel("Progress by alias"))
        self.progress_wrap = QVBoxLayout()
        pv.addLayout(self.progress_wrap)
        root.addWidget(pcard)

        lcard = QFrame()
        lcard.setObjectName("Card")
        lv = QVBoxLayout(lcard)
        lv.addWidget(QLabel("Log"))
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        lv.addWidget(self.log)
        root.addWidget(lcard)

    def _table_common(self, table):
        table.setAlternatingRowColors(True)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.NoEditTriggers)

    def _bind_events(self):
        self.btn_assign_count.clicked.connect(self.on_assign_count)
        self.btn_send_manual.clicked.connect(self.on_send_manual)
        self.btn_login_alias.clicked.connect(self.on_login_alias)
        self.btn_stop_alias.clicked.connect(self.on_stop_alias)
        self.btn_stop_all.clicked.connect(self.on_stop_all)
        self.btn_refresh.clicked.connect(self.refresh_all)
        self.btn_retry_failed.clicked.connect(self.on_retry_failed)
        self.btn_clear_queued.clicked.connect(self.on_clear_queued)

        self.btn_alias_load_selected.clicked.connect(self.on_alias_load_selected)
        self.btn_alias_save.clicked.connect(self.on_alias_save_update)
        self.btn_alias_add.clicked.connect(self.on_alias_add_new)
        self.btn_alias_reload_dispatcher.clicked.connect(self.on_alias_reload_dispatcher)
        self.table_aliases.itemSelectionChanged.connect(self.on_alias_table_select)

    def _sync_aliases_everywhere(self):
        self.dispatcher.aliases = load_aliases_file()
        self.aliases = sorted(self.dispatcher.aliases.keys())

        current = self.alias_box.currentText().strip().lower()
        self.alias_box.blockSignals(True)
        self.alias_box.clear()
        self.alias_box.addItems(self.aliases)
        if current and current in self.aliases:
            self.alias_box.setCurrentText(current)
        self.alias_box.blockSignals(False)

    def on_assign_count(self):
        alias = self.alias_box.currentText().strip().lower()
        count = int(self.assign_count_spin.value())
        if not alias:
            self._err("Выбери alias")
            return
        if count <= 0:
            self._err("Количество должно быть > 0")
            return

        self._log(f"📦 assign request: alias={alias}, count={count}")

        def _task():
            try:
                res = self.dispatcher.assign_from_common_pool(alias, count, "phone_numbers.csv")
                self._log(
                    f"✅ assigned: alias={alias}, requested={res['requested']}, "
                    f"assigned={res['assigned']}, left_in_pool={res['left_in_pool']}"
                )
            except Exception as ex:
                self._log(f"❌ assign error: {ex}")

        threading.Thread(target=_task, daemon=True).start()

    def on_send_manual(self):
        dlg = ManualNumbersDialog(self.aliases, self)
        dlg.alias_box.setCurrentText(self.alias_box.currentText())

        def do_preview():
            alias, phones, append_mode = dlg.get_data()
            if not alias:
                dlg.info.setText("Предпросмотр: выбери alias")
                return
            stats = self.dispatcher.preview_manual(alias, phones, append_mode=append_mode)
            dlg.info.setText(
                f"Предпросмотр: вход={stats['input']} | будет добавлено={stats['will_add']} | дублей пропущено={stats['duplicates']}"
            )

        dlg.btn_preview.clicked.connect(do_preview)
        dlg.chk_append.stateChanged.connect(lambda _: do_preview())
        dlg.alias_box.currentIndexChanged.connect(lambda _: do_preview())
        dlg.numbers_edit.textChanged.connect(lambda: do_preview())
        do_preview()

        if dlg.exec() != QDialog.Accepted:
            return

        alias, phones, append_mode = dlg.get_data()
        if not alias or not phones:
            self._err("Нужен alias и хотя бы один валидный номер")
            return

        mode_txt = "append/no-duplicates" if append_mode else "new block (as is)"
        self._log(f"✍ manual enqueue: {alias} | phones={len(phones)} | mode={mode_txt}")

        def _task():
            try:
                self.dispatcher.sendqr_manual(alias, phones, append_mode=append_mode)
                self._log(f"✅ queued manual: {alias} | {len(phones)} phones | mode={mode_txt}")
            except Exception as ex:
                self._log(f"❌ Send manual error: {ex}")

        threading.Thread(target=_task, daemon=True).start()

    def on_login_alias(self):
        alias = self.alias_box.currentText().strip().lower()
        if not alias:
            self._err("Alias is empty")
            return

        self._log(f"🔐 login started for alias: {alias}")

        def _task():
            driver = None
            try:
                driver = setup_driver(False, alias)
                manual_login(driver, timeout=300)
                save_session(driver, alias)
                self._log(f"✅ session/profile ready: {alias}")
            except Exception as ex:
                self._log(f"❌ Login error: {ex}")
            finally:
                try:
                    if driver:
                        driver.quit()
                except Exception:
                    pass

        threading.Thread(target=_task, daemon=True).start()

    def on_stop_alias(self):
        alias = self.alias_box.currentText().strip().lower()
        self.dispatcher.stop_worker(alias)
        self._log(f"■ stopped alias: {alias}")

    def on_stop_all(self):
        self.dispatcher.stop_all()
        self._log("■■ stopped all workers")

    def on_retry_failed(self):
        alias = self.alias_box.currentText().strip().lower()
        self.dispatcher.retry_failed_today(alias)
        self._log(f"↺ retry failed today: {alias}")

    def on_clear_queued(self):
        alias = self.alias_box.currentText().strip().lower()
        if not alias:
            self._err("Выбери alias")
            return

        reply = QMessageBox.question(
            self,
            "Подтверждение",
            f"Очистить все queued задачи для alias '{alias}'?\nОни будут помечены как skipped.",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return

        def _task():
            try:
                n = self.dispatcher.clear_queued(alias)
                self._log(f"🧹 cleared queued: alias={alias}, count={n}")
            except Exception as ex:
                self._log(f"❌ clear queued error: {ex}")

        threading.Thread(target=_task, daemon=True).start()

    def load_aliases_tab(self):
        aliases = load_aliases_file()
        rows = sorted(aliases.items(), key=lambda x: x[0])

        self.table_aliases.setRowCount(len(rows))
        for i, (a, e) in enumerate(rows):
            self.table_aliases.setItem(i, 0, QTableWidgetItem(a))
            self.table_aliases.setItem(i, 1, QTableWidgetItem(e))

    def on_alias_table_select(self):
        items = self.table_aliases.selectedItems()
        if not items:
            return
        row = items[0].row()
        alias_item = self.table_aliases.item(row, 0)
        email_item = self.table_aliases.item(row, 1)
        if alias_item:
            self.alias_edit_name.setText(alias_item.text())
        if email_item:
            self.alias_edit_email.setText(email_item.text())

    def on_alias_load_selected(self):
        self.on_alias_table_select()

    def on_alias_save_update(self):
        alias = self.alias_edit_name.text().strip().lower()
        email = self.alias_edit_email.text().strip()

        if not alias:
            self._err("Alias пустой")
            return
        if not is_valid_email(email):
            self._err("Некорректный email")
            return

        aliases = load_aliases_file()
        old_email = aliases.get(alias)
        aliases[alias] = email
        save_aliases_file(aliases)

        self._sync_aliases_everywhere()

        w = self.dispatcher.workers.get(alias)
        if w and w.thread.is_alive():
            self.dispatcher.stop_worker(alias)
            self.dispatcher.ensure_worker(alias)
            self._log(f"♻ worker restarted for alias={alias} (email updated)")

        self._log(f"💾 alias updated: {alias} | {old_email} -> {email}")
        self.load_aliases_tab()

    def on_alias_add_new(self):
        alias = self.alias_edit_name.text().strip().lower()
        email = self.alias_edit_email.text().strip()

        if not alias:
            self._err("Alias пустой")
            return
        if not re.match(r"^[a-z0-9_\-]+$", alias):
            self._err("Alias: только a-z, 0-9, _, -")
            return
        if not is_valid_email(email):
            self._err("Некорректный email")
            return

        aliases = load_aliases_file()
        if alias in aliases:
            self._err(f"Alias '{alias}' уже существует. Используй Save / Update.")
            return

        aliases[alias] = email
        save_aliases_file(aliases)

        self._sync_aliases_everywhere()

        self._log(f"➕ alias added: {alias} -> {email}")
        self.load_aliases_tab()

    def on_alias_reload_dispatcher(self):
        try:
            self._sync_aliases_everywhere()
            self._log("♻ aliases reloaded into dispatcher")
            self.load_aliases_tab()
        except Exception as ex:
            self._log(f"❌ reload aliases error: {ex}")

    def refresh_all(self):
        try:
            self.load_status_table()
            self.load_blocks_table()
            self.load_failed_table()
            self.load_progress_bars()
        except Exception as ex:
            self._err(f"Refresh error: {ex}")

    def _db(self):
        c = sqlite3.connect(DB_PATH)
        c.row_factory = sqlite3.Row
        return c

    def load_status_table(self):
        conn = self._db()
        cur = conn.cursor()
        cur.execute("""
            SELECT alias, status, COUNT(*) AS cnt
            FROM tasks
            GROUP BY alias, status
            ORDER BY alias, status
        """)
        rows = cur.fetchall()
        conn.close()

        self.table_status.setRowCount(len(rows))
        for i, r in enumerate(rows):
            self.table_status.setItem(i, 0, QTableWidgetItem(str(r["alias"])))
            it = QTableWidgetItem(str(r["status"]))
            self._paint_status_item(it, r["status"])
            self.table_status.setItem(i, 1, it)
            self.table_status.setItem(i, 2, QTableWidgetItem(str(r["cnt"])))

    def load_blocks_table(self):
        conn = self._db()
        cur = conn.cursor()
        cur.execute("""
            SELECT
              b.id, b.alias, b.range_start, b.range_end, b.status,
              SUM(CASE WHEN t.status='success' THEN 1 ELSE 0 END) s,
              SUM(CASE WHEN t.status='error' THEN 1 ELSE 0 END) e,
              SUM(CASE WHEN t.status='skipped' THEN 1 ELSE 0 END) sk,
              COUNT(t.id) total
            FROM blocks b
            LEFT JOIN tasks t ON t.block_id=b.id
            GROUP BY b.id
            ORDER BY b.id DESC
            LIMIT ?
        """, (int(self.blocks_limit.value()),))
        rows = cur.fetchall()
        conn.close()

        self.table_blocks.setRowCount(len(rows))
        for i, r in enumerate(rows):
            self.table_blocks.setItem(i, 0, QTableWidgetItem(f"#{r['id']}"))
            self.table_blocks.setItem(i, 1, QTableWidgetItem(str(r["alias"])))
            self.table_blocks.setItem(i, 2, QTableWidgetItem(f"{r['range_start']}-{r['range_end']}"))
            st = QTableWidgetItem(str(r["status"]))
            self._paint_status_item(st, r["status"])
            self.table_blocks.setItem(i, 3, st)
            self.table_blocks.setItem(i, 4, QTableWidgetItem(str(r["total"])))
            self.table_blocks.setItem(i, 5, QTableWidgetItem(str(r["s"] or 0)))
            self.table_blocks.setItem(i, 6, QTableWidgetItem(str(r["e"] or 0)))
            self.table_blocks.setItem(i, 7, QTableWidgetItem(str(r["sk"] or 0)))

    def load_failed_table(self):
        conn = self._db()
        cur = conn.cursor()
        cur.execute("""
            SELECT id, alias, phone, row_index, attempts, last_error
            FROM tasks
            WHERE status='error'
            ORDER BY id DESC
            LIMIT 300
        """)
        rows = cur.fetchall()
        conn.close()

        self.table_failed.setRowCount(len(rows))
        for i, r in enumerate(rows):
            self.table_failed.setItem(i, 0, QTableWidgetItem(str(r["id"])))
            self.table_failed.setItem(i, 1, QTableWidgetItem(str(r["alias"])))
            self.table_failed.setItem(i, 2, QTableWidgetItem(str(r["phone"])))
            self.table_failed.setItem(i, 3, QTableWidgetItem(str(r["row_index"])))
            self.table_failed.setItem(i, 4, QTableWidgetItem(str(r["attempts"])))
            err = QTableWidgetItem(str(r["last_error"] or ""))
            err.setForeground(QColor("#FF7B72"))
            self.table_failed.setItem(i, 5, err)

    def load_progress_bars(self):
        while self.progress_wrap.count():
            item = self.progress_wrap.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        conn = self._db()
        cur = conn.cursor()
        cur.execute("""
            SELECT
                alias,
                SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) success_cnt,
                SUM(CASE WHEN status IN ('success','error','skipped') THEN 1 ELSE 0 END) done_cnt
            FROM tasks
            GROUP BY alias
            ORDER BY alias
        """)
        rows = cur.fetchall()
        conn.close()

        if not rows:
            self.progress_wrap.addWidget(QLabel("Нет данных"))
            return

        for r in rows:
            alias = r["alias"]
            success = int(r["success_cnt"] or 0)
            done = int(r["done_cnt"] or 0)
            percent = int((success / done) * 100) if done else 0

            row = QHBoxLayout()
            lab = QLabel(f"{alias}: {success}/{done}")
            lab.setMinimumWidth(180)
            bar = QProgressBar()
            bar.setValue(percent)
            bar.setFormat(f"{percent}%")
            row.addWidget(lab)
            row.addWidget(bar)

            holder = QWidget()
            holder.setLayout(row)
            self.progress_wrap.addWidget(holder)

    def _paint_status_item(self, item, status: str):
        s = (status or "").lower()
        if s == "success":
            item.setForeground(QColor("#3FB950"))
        elif s == "error":
            item.setForeground(QColor("#FF7B72"))
        elif s == "running":
            item.setForeground(QColor("#D29922"))
        elif s == "queued":
            item.setForeground(QColor("#58A6FF"))
        elif s == "skipped":
            item.setForeground(QColor("#A371F7"))

    def _log(self, text):
        self.log.append(text)

    def _err(self, text):
        self.log.append(f"❌ {text}")
        QMessageBox.warning(self, "Error", text)

    def closeEvent(self, event):
        try:
            self.dispatcher.stop_all()
            self.dispatcher.close()
        except Exception:
            pass
        event.accept()


def main():
    if not Path(DB_PATH).exists():
        init_db()

    app = QApplication(sys.argv)
    app.setStyleSheet(DARK_QSS)

    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
