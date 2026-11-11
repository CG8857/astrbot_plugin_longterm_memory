"""SQLite 持久化层。

所有数据都存放在 AstrBot 的 ``data/plugin_data/<插件名>/memory.db`` 中，
避免插件更新或重装时丢失。这里只使用 Python 标准库，不引入额外依赖。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_str() -> str:
    return date.today().isoformat()


def _split_keywords(raw: str | Iterable[str] | None) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        items = raw.replace("，", ",").replace("、", ",").split(",")
    else:
        items = list(raw)
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        kw = str(item).strip().lower()
        if not kw or len(kw) > 24 or kw in seen:
            continue
        seen.add(kw)
        result.append(kw)
    return result[:8]


def _loads_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [str(x) for x in data if str(x).strip()]
    except (ValueError, TypeError):
        pass
    return _split_keywords(raw)


class MemoryStore:
    """线程安全的 SQLite 存储。

    插件的所有数据库操作都在事件循环里通过 ``asyncio.to_thread`` 调用，
    单连接 + 可重入锁足以应付单实例机器人的写入量，也避免了连接池复杂度。
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            timeout=30,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------
    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_key TEXT PRIMARY KEY,
                    platform TEXT NOT NULL DEFAULT '',
                    platform_id TEXT NOT NULL DEFAULT '',
                    platform_user_id TEXT NOT NULL DEFAULT '',
                    qq TEXT NOT NULL DEFAULT '',
                    nickname TEXT NOT NULL DEFAULT '',
                    profile TEXT NOT NULL DEFAULT '',
                    tags TEXT NOT NULL DEFAULT '[]',
                    message_count INTEGER NOT NULL DEFAULT 0,
                    first_seen TEXT NOT NULL DEFAULT '',
                    last_seen TEXT NOT NULL DEFAULT '',
                    profile_updated_at TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_key TEXT NOT NULL,
                    platform TEXT NOT NULL DEFAULT '',
                    platform_msg_id TEXT NOT NULL DEFAULT '',
                    group_id TEXT NOT NULL DEFAULT '',
                    session_id TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL DEFAULT 'user',
                    content TEXT NOT NULL DEFAULT '',
                    image_desc TEXT NOT NULL DEFAULT '',
                    has_image INTEGER NOT NULL DEFAULT 0,
                    day TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    consolidated INTEGER NOT NULL DEFAULT 0,
                    extracted INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_messages_user_day
                    ON messages(user_key, day);
                CREATE INDEX IF NOT EXISTS idx_messages_user_extract
                    ON messages(user_key, extracted, consolidated, id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_dedup
                    ON messages(user_key, platform_msg_id)
                    WHERE platform_msg_id <> '' AND role = 'user';

                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_key TEXT NOT NULL,
                    content TEXT NOT NULL,
                    keywords TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT '其他',
                    importance INTEGER NOT NULL DEFAULT 3,
                    partition_key TEXT NOT NULL DEFAULT 'longterm',
                    source TEXT NOT NULL DEFAULT 'chat',
                    memory_date TEXT NOT NULL DEFAULT '',
                    group_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_memories_user
                    ON memories(user_key, memory_date);
                CREATE INDEX IF NOT EXISTS idx_memories_partition
                    ON memories(partition_key);

                CREATE TABLE IF NOT EXISTS memory_keywords (
                    memory_id INTEGER NOT NULL,
                    keyword TEXT NOT NULL,
                    PRIMARY KEY (memory_id, keyword),
                    FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_keywords_keyword
                    ON memory_keywords(keyword);

                CREATE TABLE IF NOT EXISTS daily_summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_key TEXT NOT NULL,
                    day TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    keywords TEXT NOT NULL DEFAULT '',
                    message_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    UNIQUE (user_key, day)
                );
                CREATE INDEX IF NOT EXISTS idx_summaries_user
                    ON daily_summaries(user_key, day);

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS job_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'ok',
                    detail TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                """
            )

    # ------------------------------------------------------------------
    # 用户
    # ------------------------------------------------------------------
    def touch_user(
        self,
        user_key: str,
        platform: str,
        platform_id: str,
        platform_user_id: str,
        nickname: str = "",
        qq: str = "",
        increment: bool = True,
    ) -> None:
        now = now_str()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO users (
                    user_key, platform, platform_id, platform_user_id,
                    qq, nickname, first_seen, last_seen, message_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_key) DO UPDATE SET
                    platform = excluded.platform,
                    platform_id = excluded.platform_id,
                    platform_user_id = excluded.platform_user_id,
                    nickname = CASE
                        WHEN excluded.nickname <> '' THEN excluded.nickname
                        ELSE users.nickname END,
                    qq = CASE
                        WHEN excluded.qq <> '' THEN excluded.qq
                        ELSE users.qq END,
                    last_seen = excluded.last_seen,
                    message_count = users.message_count + ?
                """,
                (
                    user_key,
                    platform,
                    platform_id,
                    platform_user_id,
                    qq,
                    nickname,
                    now,
                    now,
                    1 if increment else 0,
                    1 if increment else 0,
                ),
            )

    def update_identity(self, user_key: str, qq: str = "", nickname: str = "") -> None:
        if not qq and not nickname:
            return
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE users SET
                    qq = CASE WHEN ? <> '' THEN ? ELSE qq END,
                    nickname = CASE WHEN ? <> '' THEN ? ELSE nickname END
                WHERE user_key = ?
                """,
                (qq, qq, nickname, nickname, user_key),
            )

    def update_profile(self, user_key: str, profile: str, tags: list[str] | None = None) -> None:
        now = now_str()
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE users SET profile = ?, tags = ?, profile_updated_at = ?
                WHERE user_key = ?
                """,
                (profile.strip(), json.dumps(list(tags or []), ensure_ascii=False), now, user_key),
            )

    def clear_profile(self, user_key: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE users SET profile = '', tags = '[]', profile_updated_at = '' WHERE user_key = ?",
                (user_key,),
            )

    def get_user(self, user_key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE user_key = ?", (user_key,)
            ).fetchone()
        return self._user_row(row) if row else None

    def list_users(self, search: str = "", limit: int = 50, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        where = ""
        params: list[Any] = []
        if search:
            like = f"%{search}%"
            where = "WHERE nickname LIKE ? OR qq LIKE ? OR user_key LIKE ? OR profile LIKE ?"
            params.extend([like, like, like, like])
        with self._lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) FROM users {where}", params
            ).fetchone()[0]
            rows = self._conn.execute(
                f"""
                SELECT u.*,
                    (SELECT COUNT(*) FROM memories m WHERE m.user_key = u.user_key) AS memory_count,
                    (SELECT COUNT(*) FROM daily_summaries d WHERE d.user_key = u.user_key) AS summary_count
                FROM users u {where}
                ORDER BY u.last_seen DESC
                LIMIT ? OFFSET ?
                """,
                [*params, max(1, limit), max(0, offset)],
            ).fetchall()
        return [self._user_row(r, with_counts=True) for r in rows], int(total)

    @staticmethod
    def _user_row(row: sqlite3.Row, with_counts: bool = False) -> dict[str, Any]:
        data = {
            "user_key": row["user_key"],
            "platform": row["platform"],
            "platform_id": row["platform_id"],
            "platform_user_id": row["platform_user_id"],
            "qq": row["qq"],
            "nickname": row["nickname"],
            "profile": row["profile"],
            "tags": _loads_list(row["tags"]),
            "message_count": row["message_count"],
            "first_seen": row["first_seen"],
            "last_seen": row["last_seen"],
            "profile_updated_at": row["profile_updated_at"],
        }
        if with_counts:
            data["memory_count"] = row["memory_count"]
            data["summary_count"] = row["summary_count"]
        return data

    def delete_user(self, user_key: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM memory_keywords WHERE memory_id IN "
                "(SELECT id FROM memories WHERE user_key = ?)",
                (user_key,),
            )
            self._conn.execute("DELETE FROM memories WHERE user_key = ?", (user_key,))
            self._conn.execute("DELETE FROM messages WHERE user_key = ?", (user_key,))
            self._conn.execute("DELETE FROM daily_summaries WHERE user_key = ?", (user_key,))
            self._conn.execute("DELETE FROM users WHERE user_key = ?", (user_key,))

    def all_user_keys(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute("SELECT user_key FROM users ORDER BY last_seen DESC").fetchall()
        return [r["user_key"] for r in rows]

    # ------------------------------------------------------------------
    # 消息
    # ------------------------------------------------------------------
    def add_message(
        self,
        user_key: str,
        platform: str,
        role: str,
        content: str,
        *,
        platform_msg_id: str = "",
        group_id: str = "",
        session_id: str = "",
        image_desc: str = "",
        has_image: bool = False,
        day: str | None = None,
    ) -> int:
        day = day or today_str()
        now = now_str()
        with self._lock, self._conn:
            cur = self._conn.execute(
                """
                INSERT OR IGNORE INTO messages (
                    user_key, platform, platform_msg_id, group_id, session_id,
                    role, content, image_desc, has_image, day, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_key,
                    platform,
                    platform_msg_id,
                    group_id,
                    session_id,
                    role,
                    content[:6000],
                    image_desc[:2000],
                    1 if has_image else 0,
                    day,
                    now,
                ),
            )
            if cur.rowcount:
                return int(cur.lastrowid or 0)
            if platform_msg_id:
                row = self._conn.execute(
                    "SELECT id FROM messages WHERE user_key = ? AND platform_msg_id = ? AND role = 'user'",
                    (user_key, platform_msg_id),
                ).fetchone()
                return int(row["id"]) if row else 0
        return 0

    def set_message_image_desc(self, message_id: int, image_desc: str) -> None:
        if not message_id:
            return
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE messages SET image_desc = ? WHERE id = ?",
                (image_desc[:2000], message_id),
            )

    def get_unprocessed_messages(self, user_key: str, limit: int = 40) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM messages
                WHERE user_key = ? AND extracted = 0
                ORDER BY id ASC LIMIT ?
                """,
                (user_key, max(1, limit)),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_messages_extracted(self, ids: list[int]) -> None:
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE messages SET extracted = 1 WHERE id IN ({placeholders})", ids
            )

    def get_messages_for_day(self, user_key: str, day: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE user_key = ? AND day = ? ORDER BY id ASC",
                (user_key, day),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_day_consolidated(self, user_key: str, day: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE messages SET consolidated = 1, extracted = 1 WHERE user_key = ? AND day = ?",
                (user_key, day),
            )

    def unconsolidated_days(self, limit: int = 14) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT day, COUNT(*) AS message_count,
                       COUNT(DISTINCT user_key) AS user_count
                FROM messages
                WHERE consolidated = 0 AND role = 'user'
                GROUP BY day
                ORDER BY day DESC
                LIMIT ?
                """,
                (max(1, limit),),
            ).fetchall()
        return [dict(r) for r in rows]

    def users_with_unconsolidated(self, day: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT user_key FROM messages WHERE day = ? AND consolidated = 0",
                (day,),
            ).fetchall()
        return [r["user_key"] for r in rows]

    def count_messages(self, day: str | None = None) -> int:
        with self._lock:
            if day:
                return int(
                    self._conn.execute(
                        "SELECT COUNT(*) FROM messages WHERE day = ?", (day,)
                    ).fetchone()[0]
                )
            return int(self._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])

    def count_pending(self) -> int:
        with self._lock:
            return int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE consolidated = 0"
                ).fetchone()[0]
            )

    def count_unprocessed(self, user_key: str) -> int:
        with self._lock:
            return int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE user_key = ? AND extracted = 0 AND role = 'user'",
                    (user_key,),
                ).fetchone()[0]
            )

    # ------------------------------------------------------------------
    # 记忆与关键字
    # ------------------------------------------------------------------
    def add_memory(
        self,
        user_key: str,
        content: str,
        keywords: Iterable[str] | None = None,
        *,
        category: str = "其他",
        importance: int = 3,
        partition_key: str = "longterm",
        source: str = "chat",
        memory_date: str | None = None,
        group_id: str = "",
    ) -> int:
        content = (content or "").strip()
        if not content:
            return 0
        kw_list = _split_keywords(keywords)
        now = now_str()
        memory_date = memory_date or today_str()
        importance = max(1, min(5, int(importance or 3)))
        with self._lock, self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO memories (
                    user_key, content, keywords, category, importance,
                    partition_key, source, memory_date, group_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_key,
                    content[:2000],
                    ",".join(kw_list),
                    category[:20] or "其他",
                    importance,
                    partition_key[:80],
                    source,
                    memory_date,
                    group_id,
                    now,
                    now,
                ),
            )
            memory_id = int(cur.lastrowid or 0)
            if memory_id and kw_list:
                self._conn.executemany(
                    "INSERT OR IGNORE INTO memory_keywords (memory_id, keyword) VALUES (?, ?)",
                    [(memory_id, kw) for kw in kw_list],
                )
        return memory_id

    @staticmethod
    def _memory_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "user_key": row["user_key"],
            "content": row["content"],
            "keywords": _split_keywords(row["keywords"]),
            "category": row["category"],
            "importance": int(row["importance"]),
            "partition_key": row["partition_key"],
            "source": row["source"],
            "memory_date": row["memory_date"],
            "group_id": row["group_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_memories(
        self,
        user_key: str | None = None,
        *,
        query: str = "",
        keyword: str = "",
        limit: int = 30,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        clauses: list[str] = []
        params: list[Any] = []
        if user_key:
            clauses.append("user_key = ?")
            params.append(user_key)
        if query:
            like = f"%{query}%"
            clauses.append("(content LIKE ? OR keywords LIKE ? OR category LIKE ?)")
            params.extend([like, like, like])
        if keyword:
            clauses.append(
                "id IN (SELECT memory_id FROM memory_keywords WHERE keyword = ?)"
            )
            params.append(keyword.strip().lower())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            total = int(
                self._conn.execute(
                    f"SELECT COUNT(*) FROM memories {where}", params
                ).fetchone()[0]
            )
            rows = self._conn.execute(
                f"""
                SELECT * FROM memories {where}
                ORDER BY memory_date DESC, importance DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                [*params, max(1, limit), max(0, offset)],
            ).fetchall()
        return [self._memory_row(r) for r in rows], total

    def search_candidates(self, user_key: str, limit: int = 2000) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM memories WHERE user_key = ?
                ORDER BY memory_date DESC, importance DESC, id DESC
                LIMIT ?
                """,
                (user_key, max(1, limit)),
            ).fetchall()
        return [self._memory_row(r) for r in rows]

    def get_memory(self, memory_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
        return self._memory_row(row) if row else None

    def has_memory(self, user_key: str, content: str) -> bool:
        content = (content or "").strip()
        if not content:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM memories WHERE user_key = ? AND lower(content) = lower(?) LIMIT 1",
                (user_key, content),
            ).fetchone()
        return row is not None

    def delete_memory(self, memory_id: int) -> bool:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM memory_keywords WHERE memory_id = ?", (memory_id,))
            cur = self._conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        return bool(cur.rowcount)

    def delete_keyword(self, memory_id: int, keyword: str) -> bool:
        keyword = keyword.strip().lower()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM memory_keywords WHERE memory_id = ? AND keyword = ?",
                (memory_id, keyword),
            )
            rows = self._conn.execute(
                "SELECT keyword FROM memory_keywords WHERE memory_id = ? ORDER BY keyword",
                (memory_id,),
            ).fetchall()
            joined = ",".join(r["keyword"] for r in rows)
            self._conn.execute(
                "UPDATE memories SET keywords = ?, updated_at = ? WHERE id = ?",
                (joined, now_str(), memory_id),
            )
        return bool(cur.rowcount)

    def get_keyword_counts(self, user_key: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        params: list[Any] = []
        where = ""
        if user_key:
            where = "WHERE m.user_key = ?"
            params.append(user_key)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT k.keyword AS keyword, COUNT(*) AS count
                FROM memory_keywords k
                JOIN memories m ON m.id = k.memory_id
                {where}
                GROUP BY k.keyword
                ORDER BY count DESC, keyword ASC
                LIMIT ?
                """,
                [*params, max(1, limit)],
            ).fetchall()
        return [{"keyword": r["keyword"], "count": int(r["count"])} for r in rows]

    def clear_user_memories(self, user_key: str) -> int:
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM memory_keywords WHERE memory_id IN "
                "(SELECT id FROM memories WHERE user_key = ?)",
                (user_key,),
            )
            cur = self._conn.execute("DELETE FROM memories WHERE user_key = ?", (user_key,))
        return int(cur.rowcount)

    def count_memories(self, user_key: str | None = None) -> int:
        with self._lock:
            if user_key:
                return int(
                    self._conn.execute(
                        "SELECT COUNT(*) FROM memories WHERE user_key = ?", (user_key,)
                    ).fetchone()[0]
                )
            return int(self._conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])

    def count_keywords(self) -> int:
        with self._lock:
            return int(
                self._conn.execute("SELECT COUNT(DISTINCT keyword) FROM memory_keywords").fetchone()[0]
            )

    def recent_memories(self, limit: int = 8) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT m.*, u.nickname, u.qq
                FROM memories m LEFT JOIN users u ON u.user_key = m.user_key
                ORDER BY m.id DESC LIMIT ?
                """,
                (max(1, limit),),
            ).fetchall()
        result = []
        for row in rows:
            item = self._memory_row(row)
            item["nickname"] = row["nickname"] or ""
            item["qq"] = row["qq"] or ""
            result.append(item)
        return result

    # ------------------------------------------------------------------
    # 每日摘要
    # ------------------------------------------------------------------
    def upsert_daily_summary(
        self,
        user_key: str,
        day: str,
        summary: str,
        keywords: Iterable[str] | None = None,
        message_count: int = 0,
    ) -> None:
        kw = ",".join(_split_keywords(keywords))
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO daily_summaries (user_key, day, summary, keywords, message_count, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_key, day) DO UPDATE SET
                    summary = excluded.summary,
                    keywords = excluded.keywords,
                    message_count = excluded.message_count,
                    created_at = excluded.created_at
                """,
                (user_key, day, summary[:4000], kw, int(message_count), now_str()),
            )

    def get_recent_summaries(self, user_key: str, limit: int = 3) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM daily_summaries WHERE user_key = ? ORDER BY day DESC LIMIT ?",
                (user_key, max(1, limit)),
            ).fetchall()
        return [
            {
                "day": r["day"],
                "summary": r["summary"],
                "keywords": _split_keywords(r["keywords"]),
                "message_count": int(r["message_count"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def last_consolidation(self) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT created_at FROM job_logs WHERE job = 'daily_consolidation' AND status = 'ok' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return row["created_at"] if row else ""

    def log_job(self, job: str, status: str, detail: str = "") -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO job_logs (job, status, detail, created_at) VALUES (?, ?, ?, ?)",
                (job, status, detail[:2000], now_str()),
            )

    # ------------------------------------------------------------------
    # 维护
    # ------------------------------------------------------------------
    def purge_old(self, retention_days: int) -> int:
        if retention_days <= 0:
            return 0
        cutoff = (date.today() - timedelta(days=retention_days)).isoformat()
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM memory_keywords WHERE memory_id IN "
                "(SELECT id FROM memories WHERE memory_date < ?)",
                (cutoff,),
            )
            cur = self._conn.execute("DELETE FROM memories WHERE memory_date < ?", (cutoff,))
            deleted = int(cur.rowcount)
            self._conn.execute("DELETE FROM daily_summaries WHERE day < ?", (cutoff,))
            self._conn.execute("DELETE FROM messages WHERE day < ?", (cutoff,))
        return deleted

    def stats(self) -> dict[str, Any]:
        today = today_str()
        with self._lock:
            users = int(self._conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])
            memories = int(self._conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
            keywords = int(
                self._conn.execute("SELECT COUNT(DISTINCT keyword) FROM memory_keywords").fetchone()[0]
            )
            messages = int(self._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
            today_messages = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE day = ?", (today,)
                ).fetchone()[0]
            )
            pending = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE consolidated = 0"
                ).fetchone()[0]
            )
            profiles = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM users WHERE profile <> ''"
                ).fetchone()[0]
            )
        return {
            "users": users,
            "memories": memories,
            "keywords": keywords,
            "messages": messages,
            "today_messages": today_messages,
            "pending": pending,
            "profiles": profiles,
            "last_consolidation": self.last_consolidation(),
        }

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
