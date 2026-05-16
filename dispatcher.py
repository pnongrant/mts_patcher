import sqlite3
import threading
import time
from datetime import datetime

from qr_worker_core import (
    setup_driver,
    manual_login,
    process_one,
    read_phones,
    load_aliases,
    save_session,
    load_session,
    BETWEEN_NUMBERS_PAUSE,
    RETRY_CSV,
    append_phone_if_not_exists,
    remove_phone_from_file,
    SkipNumber,
)
from selenium.common.exceptions import TimeoutException, WebDriverException
from telegram_notifier import send_hourly_stats

DB_PATH = "automation.db"

POLL_SLEEP = 0.7
MAX_CONSECUTIVE_ERRORS = 5
COOLDOWN_AFTER_ERROR_BURST = 20
WORKER_HEADLESS = False
HOURLY_REPORT_SECONDS = 3600


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def short_error(e, limit=170):
    txt = str(e).strip()
    if not txt:
        return e.__class__.__name__
    return txt.splitlines()[0][:limit]


def is_session_broken(e: Exception) -> bool:
    m = str(e).lower()
    keys = [
        "invalid session id",
        "disconnected",
        "chrome not reachable",
        "session deleted",
        "unable to receive message from renderer",
    ]
    return any(k in m for k in keys)


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS blocks (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      alias TEXT NOT NULL,
      range_start INTEGER NOT NULL,
      range_end INTEGER NOT NULL,
      created_at TEXT NOT NULL,
      finished_at TEXT,
      status TEXT NOT NULL DEFAULT 'running'
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS tasks (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      block_id INTEGER NOT NULL,
      phone TEXT NOT NULL,
      row_index INTEGER NOT NULL,
      alias TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'queued',
      attempts INTEGER NOT NULL DEFAULT 0,
      last_error TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      FOREIGN KEY(block_id) REFERENCES blocks(id)
    )
    """)

    cur.execute("CREATE INDEX IF NOT EXISTS idx_tasks_alias_status ON tasks(alias, status)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_tasks_updated ON tasks(updated_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_tasks_block ON tasks(block_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_blocks_status ON blocks(status)")

    conn.commit()
    conn.close()


class Worker:
    def __init__(self, alias: str, email: str):
        self.alias = alias
        self.email = email
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.driver = None
        self.consecutive_errors = 0
        self.last_seen = now_str()
        self.current_phone = None

        self.processed_count = 0
        self.success_count = 0
        self.error_count = 0
        self.skipped_count = 0

    def start(self):
        print(f"🚀 [{self.alias}] worker started (headless={WORKER_HEADLESS})")
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def join(self, timeout=10):
        self.thread.join(timeout=timeout)

    def _rebuild_driver(self):
        try:
            if self.driver:
                self.driver.quit()
        except Exception:
            pass

        self.driver = setup_driver(WORKER_HEADLESS, self.alias)
        restored = False
        try:
            restored = load_session(self.driver, self.alias)
        except Exception:
            restored = False

        if restored:
            print(f"[{self.alias}] ✅ profile session restored")
            return

        try:
            self.driver.quit()
        except Exception:
            pass

        print(f"[{self.alias}] 🔐 manual login required")
        self.driver = setup_driver(False, self.alias)
        manual_login(self.driver, timeout=300)
        save_session(self.driver, self.alias)
        print(f"[{self.alias}] ✅ login done")

        try:
            self.driver.quit()
        except Exception:
            pass

        self.driver = setup_driver(WORKER_HEADLESS, self.alias)
        if not load_session(self.driver, self.alias):
            raise Exception(f"[{self.alias}] Не удалось восстановить сессию профиля")
        print(f"[{self.alias}] ✅ browser ready")

    def _pick_task(self):
        cur = self.conn.cursor()
        cur.execute("""
            SELECT id, phone, block_id
            FROM tasks
            WHERE alias=? AND status='queued'
            ORDER BY block_id DESC, id ASC
            LIMIT 1
        """, (self.alias,))
        row = cur.fetchone()
        if not row:
            return None

        cur.execute("""
            UPDATE tasks
            SET status='running', updated_at=?
            WHERE id=? AND status='queued'
        """, (now_str(), row["id"]))
        self.conn.commit()

        if cur.rowcount == 0:
            return None
        return row

    def _set_task_result(self, task_id: int, status: str, err: str = None):
        cur = self.conn.cursor()
        cur.execute("""
            UPDATE tasks
            SET status=?, attempts=attempts+1, last_error=?, updated_at=?
            WHERE id=?
        """, (status, err, now_str(), task_id))
        self.conn.commit()

    def _try_close_block(self, block_id: int):
        cur = self.conn.cursor()
        cur.execute("""
            SELECT COUNT(*) AS cnt
            FROM tasks
            WHERE block_id=? AND status IN ('queued','running')
        """, (block_id,))
        cnt = cur.fetchone()["cnt"]
        if cnt == 0:
            cur.execute("""
                UPDATE blocks
                SET status='finished', finished_at=?
                WHERE id=? AND status='running'
            """, (now_str(), block_id))
            self.conn.commit()

    def _total_tasks_for_alias(self):
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) AS cnt FROM tasks WHERE alias=?", (self.alias,))
        return int(cur.fetchone()["cnt"] or 0)

    def run(self):
        try:
            self._rebuild_driver()

            while not self.stop_event.is_set():
                task = self._pick_task()
                self.last_seen = now_str()

                if not task:
                    time.sleep(POLL_SLEEP)
                    continue

                task_id = task["id"]
                phone = task["phone"]
                block_id = task["block_id"]
                self.current_phone = phone

                total_for_alias = self._total_tasks_for_alias()

                try:
                    process_one(self.driver, phone, self.email)
                    self._set_task_result(task_id, "success")
                    remove_phone_from_file(RETRY_CSV, phone)

                    self.processed_count += 1
                    self.success_count += 1
                    print(f"[{self.alias}] [#{self.processed_count} OK] [{self.processed_count}/{total_for_alias}] {phone}")
                    self.consecutive_errors = 0

                except SkipNumber as e:
                    msg = f"skipped: {short_error(e)}"
                    self._set_task_result(task_id, "skipped", msg)
                    remove_phone_from_file(RETRY_CSV, phone)

                    self.processed_count += 1
                    self.skipped_count += 1
                    print(f"[{self.alias}] [#{self.processed_count} SKIP] [{self.processed_count}/{total_for_alias}] {phone} | {msg}")
                    self.consecutive_errors = 0

                except TimeoutException:
                    msg = "timeout: не дождались нужного элемента"
                    self._set_task_result(task_id, "error", msg)
                    append_phone_if_not_exists(RETRY_CSV, phone)

                    self.processed_count += 1
                    self.error_count += 1
                    print(f"[{self.alias}] [#{self.processed_count} ERR] [{self.processed_count}/{total_for_alias}] {phone} | {msg}")
                    self.consecutive_errors += 1

                except WebDriverException as e:
                    msg = f"webdriver: {short_error(e)}"
                    self._set_task_result(task_id, "error", msg)
                    append_phone_if_not_exists(RETRY_CSV, phone)

                    self.processed_count += 1
                    self.error_count += 1
                    print(f"[{self.alias}] [#{self.processed_count} ERR] [{self.processed_count}/{total_for_alias}] {phone} | {msg}")
                    self.consecutive_errors += 1

                    if is_session_broken(e):
                        print(f"[{self.alias}] ♻️ browser restart...")
                        self._rebuild_driver()
                        self.consecutive_errors = 0

                except Exception as e:
                    msg = f"error: {short_error(e)}"
                    self._set_task_result(task_id, "error", msg)
                    append_phone_if_not_exists(RETRY_CSV, phone)

                    self.processed_count += 1
                    self.error_count += 1
                    print(f"[{self.alias}] [#{self.processed_count} ERR] [{self.processed_count}/{total_for_alias}] {phone} | {msg}")
                    self.consecutive_errors += 1

                self._try_close_block(block_id)

                if self.consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    print(f"[{self.alias}] 🛑 {self.consecutive_errors} ошибок подряд, пауза {COOLDOWN_AFTER_ERROR_BURST}s")
                    time.sleep(COOLDOWN_AFTER_ERROR_BURST)
                    self.consecutive_errors = 0

                self.current_phone = None
                time.sleep(BETWEEN_NUMBERS_PAUSE)

        finally:
            try:
                if self.driver:
                    self.driver.quit()
            except Exception:
                pass
            self.conn.close()
            print(f"🛑 [{self.alias}] worker stopped")


class Dispatcher:
    def __init__(self):
        self.aliases = load_aliases()
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.workers = {}

        self.stop_event = threading.Event()
        self.hourly_thread = threading.Thread(target=self._hourly_report_loop, daemon=True)
        self.hourly_thread.start()

    def ensure_worker(self, alias: str):
        if alias in self.workers and self.workers[alias].thread.is_alive():
            return
        email = self.aliases.get(alias)
        if not email:
            raise ValueError(f"alias '{alias}' не найден в aliases")
        w = Worker(alias, email)
        self.workers[alias] = w
        w.start()

    def stop_worker(self, alias: str):
        w = self.workers.get(alias)
        if not w:
            print(f"[{alias}] не запущен")
            return
        w.stop()
        w.join()
        print(f"[{alias}] остановлен")

    def stop_all(self):
        for alias in list(self.workers.keys()):
            self.stop_worker(alias)

    def _enqueue_block(self, alias: str, phones: list[str], start_row_idx: int):
        cur = self.conn.cursor()
        cur.execute("""
            INSERT INTO blocks(alias, range_start, range_end, created_at, status)
            VALUES (?, ?, ?, ?, 'running')
        """, (alias, start_row_idx, start_row_idx + len(phones) - 1, now_str()))
        block_id = cur.lastrowid

        t = now_str()
        rows = []
        for i, phone in enumerate(phones, start=start_row_idx):
            rows.append((block_id, phone, i, alias, "queued", 0, None, t, t))

        cur.executemany("""
            INSERT INTO tasks(block_id, phone, row_index, alias, status, attempts, last_error, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)
        self.conn.commit()
        print(f"📦 block #{block_id} | {alias} | phones={len(phones)}")

    def assign_from_common_pool(self, alias: str, count: int, pool_file: str = "phone_numbers.csv"):
        if alias not in self.aliases:
            raise ValueError(f"alias '{alias}' не найден")
        if count <= 0:
            raise ValueError("count должен быть > 0")

        phones = read_phones(pool_file)
        if not phones:
            raise ValueError("общий список пуст")

        take = phones[:count]
        if not take:
            raise ValueError("не удалось взять номера из общего списка")

        self._enqueue_block(alias, take, 1)
        self.ensure_worker(alias)

        rest = phones[len(take):]
        with open(pool_file, "w", encoding="utf-8-sig", newline="") as f:
            for p in rest:
                f.write(str(p).strip() + "\n")

        return {
            "requested": count,
            "assigned": len(take),
            "left_in_pool": len(rest),
        }

    def sendqr_manual(self, alias: str, phones: list[str], append_mode: bool = False):
        if alias not in self.aliases:
            raise ValueError(f"alias '{alias}' не найден")
        if not phones:
            raise ValueError("пустой список номеров")

        phones_in = []
        seen_in = set()
        for p in phones:
            p = str(p).strip()
            if not p or p in seen_in:
                continue
            seen_in.add(p)
            phones_in.append(p)

        if not phones_in:
            raise ValueError("после фильтрации список пуст")

        if append_mode:
            cur = self.conn.cursor()
            cur.execute("SELECT phone FROM tasks WHERE alias=?", (alias,))
            existing = {str(r["phone"]) for r in cur.fetchall()}

            filtered = [p for p in phones_in if p not in existing]
            skipped = len(phones_in) - len(filtered)

            if not filtered:
                raise ValueError(f"все номера уже есть в очереди/истории alias '{alias}' (пропущено {skipped})")

            self._enqueue_block(alias, filtered, 1)
            self.ensure_worker(alias)
            print(f"➕ append_mode: added={len(filtered)}, skipped_duplicates={skipped}")
        else:
            self._enqueue_block(alias, phones_in, 1)
            self.ensure_worker(alias)

    def preview_manual(self, alias: str, phones: list[str], append_mode: bool = False):
        phones_in = []
        seen_in = set()
        for p in phones:
            p = str(p).strip()
            if not p or p in seen_in:
                continue
            seen_in.add(p)
            phones_in.append(p)

        if not append_mode:
            return {"input": len(phones_in), "will_add": len(phones_in), "duplicates": 0}

        cur = self.conn.cursor()
        cur.execute("SELECT phone FROM tasks WHERE alias=?", (alias,))
        existing = {str(r["phone"]) for r in cur.fetchall()}

        will_add = sum(1 for p in phones_in if p not in existing)
        duplicates = len(phones_in) - will_add
        return {"input": len(phones_in), "will_add": will_add, "duplicates": duplicates}

    def retry_failed_today(self, alias):
        cur = self.conn.cursor()
        cur.execute("""
            UPDATE tasks
            SET status='queued', updated_at=?
            WHERE alias=? AND status='error' AND date(updated_at)=date('now','localtime')
        """, (now_str(), alias))
        n = cur.rowcount
        self.conn.commit()
        print(f"🔁 requeued: {n}")
        if n > 0:
            self.ensure_worker(alias)

    def clear_queued(self, alias: str):
        if alias not in self.aliases:
            raise ValueError(f"alias '{alias}' не найден")

        cur = self.conn.cursor()
        cur.execute("""
            UPDATE tasks
            SET status='skipped',
                last_error='manually cleared queued from GUI',
                updated_at=?
            WHERE alias=? AND status='queued'
        """, (now_str(), alias))
        n = cur.rowcount
        self.conn.commit()
        print(f"🧹 cleared queued for {alias}: {n}")
        return n

    def _collect_success_last_hour(self):
        cur = self.conn.cursor()
        cur.execute("""
            SELECT alias, COUNT(*) AS cnt
            FROM tasks
            WHERE status='success'
              AND datetime(updated_at) >= datetime('now','-1 hour')
            GROUP BY alias
            ORDER BY cnt DESC, alias ASC
        """)
        return [(r["alias"], int(r["cnt"])) for r in cur.fetchall()]

    def _hourly_report_loop(self):
        while not self.stop_event.is_set():
            if self.stop_event.wait(HOURLY_REPORT_SECONDS):
                break
            try:
                rows = self._collect_success_last_hour()
                ok, info = send_hourly_stats(rows, "последний 1 час")
                if not ok:
                    print(f"[TG][hourly] failed: {info}")
                else:
                    print(f"[TG][hourly] sent: rows={len(rows)}")
            except Exception as e:
                print(f"[TG][hourly] exception: {e}")

    def close(self):
        try:
            self.stop_event.set()
            if self.hourly_thread.is_alive():
                self.hourly_thread.join(timeout=3)
        except Exception:
            pass
        self.conn.close()
