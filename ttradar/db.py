"""SQLite による時系列ストア.

なぜ SQLite か
--------------
1 人運用でサーバーを立てたくない / GitHub Actions で回して成果物として
コミットできる / 数百万行までは余裕で捌ける、の 3 点。

スキーマ方針
------------
- ``entities``  : 追跡対象のマスタ。初回検出時刻を持つ (= 新規判定に使う)
- ``snapshots`` : append-only の計測ログ。**絶対に UPDATE しない**。
                  ここに履歴が積まれることで初めて「伸び率」が計算できる。
- ``signals``   : 分析結果のログ。通知の重複排除と後追い検証に使う。
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from .models import EntityType, Snapshot, TrendSignal

SCHEMA_VERSION = 1

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entities (
    entity_key   TEXT PRIMARY KEY,
    entity_type  TEXT NOT NULL,
    native_id    TEXT NOT NULL,
    name         TEXT NOT NULL,
    source       TEXT NOT NULL,
    region       TEXT NOT NULL DEFAULT 'JP',
    category     TEXT,
    url          TEXT,
    thumbnail    TEXT,
    first_seen   REAL NOT NULL,
    last_seen    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entities_type   ON entities(entity_type, region);
CREATE INDEX IF NOT EXISTS idx_entities_first  ON entities(first_seen);

-- append-only。同一 (entity_key, captured_at) は取り込み重複なので無視する。
CREATE TABLE IF NOT EXISTS snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_key     TEXT NOT NULL,
    entity_type    TEXT NOT NULL,
    source         TEXT NOT NULL,
    region         TEXT NOT NULL DEFAULT 'JP',
    captured_at    REAL NOT NULL,
    primary_value  REAL,
    metrics        TEXT NOT NULL DEFAULT '{}',
    extra          TEXT NOT NULL DEFAULT '{}',
    UNIQUE(entity_key, captured_at)
);
CREATE INDEX IF NOT EXISTS idx_snap_key_time ON snapshots(entity_key, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_snap_time     ON snapshots(captured_at DESC);

CREATE TABLE IF NOT EXISTS signals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_key    TEXT NOT NULL,
    entity_type   TEXT NOT NULL,
    computed_at   REAL NOT NULL,
    stage         TEXT NOT NULL,
    score         REAL NOT NULL,
    growth_rate   REAL,
    acceleration  REAL,
    payload       TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_signals_time ON signals(computed_at DESC);
CREATE INDEX IF NOT EXISTS idx_signals_key  ON signals(entity_key, computed_at DESC);

-- 通知済み記録。「随時知りたいがスパムは嫌」を両立させるための重複排除。
CREATE TABLE IF NOT EXISTS notified (
    entity_key    TEXT NOT NULL,
    channel       TEXT NOT NULL,
    notified_at   REAL NOT NULL,
    score         REAL,
    PRIMARY KEY (entity_key, channel)
);

-- ユーザーが明示的に追跡したいもの (自分の商品 / 競合クリエイター等)
CREATE TABLE IF NOT EXISTS watchlist (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,      -- keyword / creator / product / hashtag
    value       TEXT NOT NULL,
    note        TEXT,
    added_at    REAL NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    UNIQUE(kind, value)
);

CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    REAL NOT NULL,
    finished_at   REAL,
    ok            INTEGER,
    collected     INTEGER DEFAULT 0,
    errors        TEXT DEFAULT '[]'
);
"""


class Database:
    """時系列ストアへの薄いラッパ."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self._conn.commit()

    # ------------------------------------------------------------------ 基本
    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -------------------------------------------------------------- 書き込み
    def upsert_snapshots(self, snapshots: Iterable[Snapshot]) -> int:
        """スナップショットを取り込む. 戻り値は新規に挿入された件数."""
        inserted = 0
        with self.tx() as c:
            for s in snapshots:
                key = s.entity_key
                now = s.captured_at
                # エンティティ登録 (first_seen は最初の値を保持し続ける)
                c.execute(
                    """
                    INSERT INTO entities
                        (entity_key, entity_type, native_id, name, source,
                         region, category, url, thumbnail, first_seen, last_seen)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(entity_key) DO UPDATE SET
                        name      = excluded.name,
                        category  = COALESCE(excluded.category, entities.category),
                        url       = COALESCE(excluded.url, entities.url),
                        thumbnail = COALESCE(excluded.thumbnail, entities.thumbnail),
                        last_seen = MAX(entities.last_seen, excluded.last_seen)
                    """,
                    (key, s.entity_type.value, str(s.native_id), s.name, s.source,
                     s.region, s.category, s.url, s.thumbnail, now, now),
                )
                cur = c.execute(
                    """
                    INSERT OR IGNORE INTO snapshots
                        (entity_key, entity_type, source, region, captured_at,
                         primary_value, metrics, extra)
                    VALUES (?,?,?,?,?,?,?,?)
                    """,
                    (key, s.entity_type.value, s.source, s.region, now,
                     s.primary_value,
                     json.dumps(s.metrics, ensure_ascii=False),
                     json.dumps(s.extra, ensure_ascii=False, default=str)),
                )
                inserted += cur.rowcount or 0
        return inserted

    def record_signals(self, signals: Iterable[TrendSignal]) -> None:
        now = time.time()
        with self.tx() as c:
            for sig in signals:
                c.execute(
                    """INSERT INTO signals
                       (entity_key, entity_type, computed_at, stage, score,
                        growth_rate, acceleration, payload)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (sig.entity_key, sig.entity_type.value, now, sig.stage.value,
                     sig.score, sig.growth_rate, sig.acceleration,
                     json.dumps(sig.to_dict(), ensure_ascii=False, default=str)),
                )

    def start_run(self) -> int:
        with self.tx() as c:
            cur = c.execute("INSERT INTO runs(started_at) VALUES(?)", (time.time(),))
            return int(cur.lastrowid or 0)

    def finish_run(self, run_id: int, ok: bool, collected: int, errors: list[str]) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE runs SET finished_at=?, ok=?, collected=?, errors=? WHERE id=?",
                (time.time(), 1 if ok else 0, collected,
                 json.dumps(errors, ensure_ascii=False), run_id),
            )

    # ---------------------------------------------------------------- 読み出し
    def latest_snapshot(self, entity_key: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM snapshots WHERE entity_key=? ORDER BY captured_at DESC LIMIT 1",
            (entity_key,),
        ).fetchone()

    def history(self, entity_key: str, since: float | None = None) -> list[sqlite3.Row]:
        """古い順の履歴を返す (伸び率・加速度計算の入力)."""
        if since is None:
            rows = self._conn.execute(
                "SELECT * FROM snapshots WHERE entity_key=? ORDER BY captured_at ASC",
                (entity_key,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM snapshots WHERE entity_key=? AND captured_at>=? "
                "ORDER BY captured_at ASC",
                (entity_key, since),
            ).fetchall()
        return list(rows)

    def active_entities(
        self,
        entity_type: EntityType | str | None = None,
        region: str | None = None,
        since: float | None = None,
    ) -> list[sqlite3.Row]:
        """直近に観測されたエンティティ一覧."""
        sql = "SELECT * FROM entities WHERE 1=1"
        args: list[Any] = []
        if entity_type is not None:
            et = entity_type.value if isinstance(entity_type, EntityType) else entity_type
            sql += " AND entity_type=?"
            args.append(et)
        if region:
            sql += " AND region=?"
            args.append(region)
        if since is not None:
            sql += " AND last_seen>=?"
            args.append(since)
        sql += " ORDER BY last_seen DESC"
        return list(self._conn.execute(sql, args).fetchall())

    def snapshot_count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) c FROM snapshots").fetchone()["c"])

    def entity_count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) c FROM entities").fetchone()["c"])

    def distinct_capture_times(self, limit: int = 50) -> list[float]:
        rows = self._conn.execute(
            "SELECT DISTINCT captured_at FROM snapshots ORDER BY captured_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [float(r["captured_at"]) for r in rows]

    # ------------------------------------------------------------- 通知の重複排除
    def was_notified(self, entity_key: str, channel: str, cooldown_h: float) -> bool:
        row = self._conn.execute(
            "SELECT notified_at FROM notified WHERE entity_key=? AND channel=?",
            (entity_key, channel),
        ).fetchone()
        if row is None:
            return False
        return (time.time() - float(row["notified_at"])) < cooldown_h * 3600.0

    def mark_notified(self, entity_key: str, channel: str, score: float) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO notified(entity_key, channel, notified_at, score) "
                "VALUES (?,?,?,?)",
                (entity_key, channel, time.time(), score),
            )

    # ------------------------------------------------------------------ 監視リスト
    def add_watch(self, kind: str, value: str, note: str | None = None) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO watchlist(kind, value, note, added_at) VALUES (?,?,?,?)",
                (kind, value, note, time.time()),
            )

    def remove_watch(self, kind: str, value: str) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM watchlist WHERE kind=? AND value=?", (kind, value))

    def list_watch(self, kind: str | None = None) -> list[sqlite3.Row]:
        if kind:
            return list(self._conn.execute(
                "SELECT * FROM watchlist WHERE active=1 AND kind=? ORDER BY added_at", (kind,),
            ).fetchall())
        return list(self._conn.execute(
            "SELECT * FROM watchlist WHERE active=1 ORDER BY kind, added_at",
        ).fetchall())

    # ------------------------------------------------------------------- 保守
    def prune(self, keep_days: int = 180) -> int:
        """古いスナップショットを削除. 戻り値は削除件数."""
        cutoff = time.time() - keep_days * 86400
        with self.tx() as c:
            cur = c.execute("DELETE FROM snapshots WHERE captured_at < ?", (cutoff,))
            deleted = cur.rowcount or 0
            c.execute("DELETE FROM signals WHERE computed_at < ?", (cutoff,))
        self._conn.execute("VACUUM")
        return deleted
