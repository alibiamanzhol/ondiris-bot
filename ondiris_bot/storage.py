import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .snapshot import OrgState

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id       INTEGER PRIMARY KEY,
    chat_id       INTEGER NOT NULL,
    name          TEXT NOT NULL DEFAULT '',
    monitor_time  TEXT,
    notify        INTEGER NOT NULL DEFAULT 1,
    last_run_date TEXT,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subscriptions (
    user_id         INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    bin             TEXT NOT NULL,
    label           TEXT NOT NULL DEFAULT '',
    added_at        TEXT NOT NULL,
    snapshot        TEXT,
    snapshot_at     TEXT,
    silent_baseline INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, bin)
);
CREATE INDEX IF NOT EXISTS subscriptions_bin ON subscriptions(bin);
CREATE TABLE IF NOT EXISTS outbox (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    chat_id    INTEGER NOT NULL,
    text       TEXT NOT NULL,
    dedup_key  TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    sent_at    TEXT,
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

MAX_SEND_ATTEMPTS = 5


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class User:
    user_id: int
    chat_id: int
    name: str
    monitor_time: str | None
    notify: bool
    last_run_date: str | None


@dataclass(frozen=True)
class Subscription:
    user_id: int
    bin: str
    label: str
    snapshot: OrgState | None
    silent_baseline: bool

    @property
    def title(self) -> str:
        if self.snapshot and self.snapshot.company:
            return self.snapshot.company
        return self.label


@dataclass(frozen=True)
class OutboxMessage:
    id: int
    user_id: int
    chat_id: int
    text: str


class Storage:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def _tx(self):
        return _Transaction(self.conn)

    # --- пользователи ---
    def upsert_user(self, user_id: int, chat_id: int, name: str = "") -> None:
        self.conn.execute(
            """INSERT INTO users(user_id, chat_id, name, created_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET chat_id = excluded.chat_id,
                   name = CASE WHEN excluded.name != '' THEN excluded.name ELSE users.name END""",
            (user_id, chat_id, name or "", _now()),
        )

    def get_user(self, user_id: int) -> User | None:
        r = self.conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return _user(r) if r else None

    def set_monitor_time(self, user_id: int, hhmm: str) -> None:
        self.conn.execute("UPDATE users SET monitor_time = ? WHERE user_id = ?", (hhmm, user_id))

    def set_notify(self, user_id: int, enabled: bool) -> None:
        self.conn.execute("UPDATE users SET notify = ? WHERE user_id = ?", (int(enabled), user_id))

    def users_due(self, today: str, now_hhmm: str, default_hhmm: str) -> list[User]:
        rows = self.conn.execute(
            """SELECT * FROM users WHERE notify = 1
               AND (last_run_date IS NULL OR last_run_date < ?)
               AND COALESCE(monitor_time, ?) <= ?""",
            (today, default_hhmm, now_hhmm),
        ).fetchall()
        return [_user(r) for r in rows]

    def set_last_run(self, user_ids: list[int], today: str) -> None:
        self.conn.executemany("UPDATE users SET last_run_date = ? WHERE user_id = ?", [(today, u) for u in user_ids])

    # --- списки мониторинга ---
    def add_bins(self, user_id: int, bins: list[str], labels: dict[str, str] | None = None,
                 silent_baseline: bool = False) -> tuple[list[str], list[str]]:
        labels = labels or {}
        added, existed = [], []
        with self._tx():
            for b in dict.fromkeys(bins):
                cur = self.conn.execute(
                    """INSERT OR IGNORE INTO subscriptions(user_id, bin, label, added_at, silent_baseline)
                       VALUES (?, ?, ?, ?, ?)""",
                    (user_id, b, labels.get(b, ""), _now(), int(silent_baseline)),
                )
                (added if cur.rowcount else existed).append(b)
        return added, existed

    def remove_bins(self, user_id: int, bins: list[str]) -> tuple[list[str], list[str]]:
        removed, missing = [], []
        with self._tx():
            for b in dict.fromkeys(bins):
                cur = self.conn.execute("DELETE FROM subscriptions WHERE user_id = ? AND bin = ?", (user_id, b))
                (removed if cur.rowcount else missing).append(b)
        return removed, missing

    def has_bin(self, user_id: int, bin_: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM subscriptions WHERE user_id = ? AND bin = ?", (user_id, bin_)
        ).fetchone() is not None

    def count_bins(self, user_id: int) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM subscriptions WHERE user_id = ?", (user_id,)).fetchone()[0]

    def list_subscriptions(self, user_id: int) -> list[Subscription]:
        rows = self.conn.execute(
            "SELECT * FROM subscriptions WHERE user_id = ? ORDER BY added_at, rowid", (user_id,)
        ).fetchall()
        return [_sub(r) for r in rows]

    def subscriptions_for(self, user_ids: list[int]) -> list[Subscription]:
        if not user_ids:
            return []
        marks = ",".join("?" * len(user_ids))
        rows = self.conn.execute(
            f"SELECT * FROM subscriptions WHERE user_id IN ({marks}) ORDER BY user_id, added_at, rowid", user_ids
        ).fetchall()
        return [_sub(r) for r in rows]

    def save_baseline(self, user_id: int, state: OrgState) -> bool:
        """Фиксирует базовое состояние, только если его ещё нет."""
        cur = self.conn.execute(
            """UPDATE subscriptions SET snapshot = ?, snapshot_at = ?, silent_baseline = 0
               WHERE user_id = ? AND bin = ? AND snapshot IS NULL""",
            (state.to_json(), _now(), user_id, state.bin),
        )
        return bool(cur.rowcount)

    def apply_changes(self, user_id: int, chat_id: int, states: list[OrgState],
                      messages: list[tuple[str, str]]) -> None:
        """Атомарно: новые снимки + уведомления в outbox. Сбой не приводит к повторной рассылке того же изменения."""
        with self._tx():
            for s in states:
                self.conn.execute(
                    """UPDATE subscriptions SET snapshot = ?, snapshot_at = ?, silent_baseline = 0
                       WHERE user_id = ? AND bin = ?""",
                    (s.to_json(), _now(), user_id, s.bin),
                )
            for dedup_key, text in messages:
                self.conn.execute(
                    """INSERT OR IGNORE INTO outbox(user_id, chat_id, text, dedup_key, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (user_id, chat_id, text, dedup_key, _now()),
                )

    # --- outbox ---
    def pending_messages(self) -> list[OutboxMessage]:
        rows = self.conn.execute(
            "SELECT id, user_id, chat_id, text FROM outbox WHERE sent_at IS NULL AND attempts < ? ORDER BY id",
            (MAX_SEND_ATTEMPTS,),
        ).fetchall()
        return [OutboxMessage(r["id"], r["user_id"], r["chat_id"], r["text"]) for r in rows]

    def mark_sent(self, msg_id: int) -> None:
        self.conn.execute("UPDATE outbox SET sent_at = ? WHERE id = ?", (_now(), msg_id))

    def mark_failed(self, msg_id: int, error: str, permanent: bool = False) -> None:
        self.conn.execute(
            "UPDATE outbox SET attempts = CASE WHEN ? THEN ? ELSE attempts + 1 END, last_error = ? WHERE id = ?",
            (int(permanent), MAX_SEND_ATTEMPTS, error[:500], msg_id),
        )

    # --- служебное ---
    def get_meta(self, key: str) -> str | None:
        r = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return r[0] if r else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))


class _Transaction:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        return False


def _user(r: sqlite3.Row) -> User:
    return User(r["user_id"], r["chat_id"], r["name"], r["monitor_time"], bool(r["notify"]), r["last_run_date"])


def _sub(r: sqlite3.Row) -> Subscription:
    return Subscription(
        user_id=r["user_id"],
        bin=r["bin"],
        label=r["label"],
        snapshot=OrgState.from_json(r["snapshot"]) if r["snapshot"] else None,
        silent_baseline=bool(r["silent_baseline"]),
    )
