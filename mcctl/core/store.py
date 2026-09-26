"""SQLite 持久化层。

只负责读写 :class:`~mcctl.core.models.Instance` 与 :class:`~mcctl.core.archive.Archive`,
不包含任何 Docker 逻辑。每次操作使用独立连接,避免长时间持有句柄
(CLI / HTTP 服务都是短操作,且可能来自不同线程)。
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .archive import Archive
from .models import Instance, State, can_transition, now_iso

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS instances (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL UNIQUE,
    slug         TEXT    NOT NULL UNIQUE,
    server_type  TEXT    NOT NULL,
    mc_version   TEXT    NOT NULL,
    memory_mb    INTEGER NOT NULL,
    online_mode  INTEGER NOT NULL DEFAULT 1,
    java         TEXT,
    container_id TEXT,
    volume_name  TEXT    NOT NULL,
    state        TEXT    NOT NULL,
    created_at   TEXT    NOT NULL,
    ttl_minutes  INTEGER
);

-- 存档索引:服务器名唯一(同一名字只保留最新的一份存档)。
CREATE TABLE IF NOT EXISTS archives (
    name         TEXT    PRIMARY KEY,
    slug         TEXT    NOT NULL,
    path         TEXT    NOT NULL,
    size_bytes   INTEGER NOT NULL,
    created_at   TEXT    NOT NULL,
    expires_at   TEXT    NOT NULL,
    server_type  TEXT    NOT NULL,
    mc_version   TEXT    NOT NULL,
    memory_mb    INTEGER NOT NULL,
    online_mode  INTEGER NOT NULL DEFAULT 1,
    java         TEXT
);
"""

#: ``instances`` 表里后加的列(旧库用 ``ALTER TABLE`` 补齐)。
_ADDED_COLUMNS: dict[str, str] = {
    "java": "TEXT",
    # ---- 生命周期巡检所需的事实 ----
    "ever_had_player": "INTEGER NOT NULL DEFAULT 0",
    "empty_since": "TEXT",
    "stopped_since": "TEXT",
    "last_players": "INTEGER",
    "observed_at": "TEXT",
}


def _row_to_instance(row: sqlite3.Row) -> Instance:
    """把数据库行映射为 :class:`Instance`。"""
    return Instance(
        id=row["id"],
        name=row["name"],
        slug=row["slug"],
        server_type=row["server_type"],
        mc_version=row["mc_version"],
        memory_mb=row["memory_mb"],
        online_mode=bool(row["online_mode"]),
        java=row["java"],
        container_id=row["container_id"],
        volume_name=row["volume_name"],
        state=State(row["state"]),
        created_at=row["created_at"],
        ttl_minutes=row["ttl_minutes"],
        ever_had_player=bool(row["ever_had_player"]),
        empty_since=row["empty_since"],
        stopped_since=row["stopped_since"],
        last_players=row["last_players"],
        observed_at=row["observed_at"],
    )


def _row_to_archive(row: sqlite3.Row) -> Archive:
    """把数据库行映射为 :class:`Archive`。"""
    return Archive(
        name=row["name"],
        slug=row["slug"],
        path=Path(row["path"]),
        size_bytes=row["size_bytes"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        server_type=row["server_type"],
        mc_version=row["mc_version"],
        memory_mb=row["memory_mb"],
        online_mode=bool(row["online_mode"]),
        java=row["java"],
    )


def _migrate(conn: sqlite3.Connection) -> None:
    """为已存在的旧库补齐后加的列(``CREATE TABLE IF NOT EXISTS`` 不会加列)。"""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(instances)")}
    for column, declaration in _ADDED_COLUMNS.items():
        if column in columns:
            continue
        conn.execute(f"ALTER TABLE instances ADD COLUMN {column} {declaration}")
        logger.debug("event=store.migrate column=%s", column)


class Store:
    """``instances`` 表的读写封装。"""

    def __init__(self, db_path: str | Path) -> None:
        self.path = str(db_path)

    # ------------------------------------------------------------------ 基础
    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        """提供一个自动提交 / 关闭的连接游标。"""
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn.cursor()
            conn.commit()
        finally:
            conn.close()

    def init(self) -> None:
        """建表(幂等),并为旧库补齐新增列。"""
        with self._cursor() as cur:
            cur.connection.executescript(_SCHEMA)
            _migrate(cur.connection)
        logger.debug("event=store.init path=%s", self.path)

    # ------------------------------------------------------------------ 写入
    def add(self, instance: Instance) -> Instance:
        """插入一条新记录,返回带 ``id`` 的实例。"""
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO instances (
                    name, slug, server_type, mc_version, memory_mb, online_mode,
                    java, container_id, volume_name, state, created_at, ttl_minutes,
                    ever_had_player, empty_since, stopped_since, last_players, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    instance.name,
                    instance.slug,
                    instance.server_type,
                    instance.mc_version,
                    instance.memory_mb,
                    int(instance.online_mode),
                    instance.java,
                    instance.container_id,
                    instance.volume_name,
                    instance.state.value,
                    instance.created_at,
                    instance.ttl_minutes,
                    int(instance.ever_had_player),
                    instance.empty_since,
                    instance.stopped_since,
                    instance.last_players,
                    instance.observed_at,
                ),
            )
            new_id = int(cur.lastrowid or 0)
        logger.debug("event=store.add name=%s id=%s", instance.name, new_id)
        return self.get(instance.name) or instance

    def set_state(self, name: str, state: State) -> None:
        """更新实例状态。非法迁移只告警,不阻断(便于 reconcile 修正漂移)。

        顺带维护 ``stopped_since``:进入 ``stopped`` 时记下时刻,离开时清空——
        生命周期据此判断"停在停止状态多久了"(见 :mod:`mcctl.core.lifecycle`)。
        已经是 ``stopped`` 时不重置计时(否则漂移修正会把计时器反复打回原点)。
        """
        current = self.get(name)
        if current is not None and not can_transition(current.state, state):
            logger.warning(
                "event=store.transition.illegal name=%s from=%s to=%s",
                name,
                current.state.value,
                state.value,
            )
        if state is not State.stopped:
            stopped_since: str | None = None
        elif current is not None and current.state is State.stopped and current.stopped_since:
            stopped_since = current.stopped_since
        else:
            stopped_since = now_iso()
        with self._cursor() as cur:
            cur.execute(
                "UPDATE instances SET state = ?, stopped_since = ? WHERE name = ?",
                (state.value, stopped_since, name),
            )

    def set_container(self, name: str, container_id: str | None) -> None:
        """记录容器 id(容器创建后立即写回,保证失败可续)。"""
        with self._cursor() as cur:
            cur.execute("UPDATE instances SET container_id = ? WHERE name = ?", (container_id, name))

    def save_observation(
        self,
        name: str,
        *,
        ever_had_player: bool,
        empty_since: str | None,
        last_players: int | None,
        observed_at: str,
    ) -> None:
        """写回一次生命周期观测结果(供 :class:`~mcctl.core.lifecycle.Reaper` 使用)。"""
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE instances
                   SET ever_had_player = ?, empty_since = ?, last_players = ?, observed_at = ?
                 WHERE name = ?
                """,
                (int(ever_had_player), empty_since, last_players, observed_at, name),
            )

    # ------------------------------------------------------------------ 存档
    def save_archive(self, archive: Archive) -> Archive:
        """写入 / 覆盖一条存档元数据(同一个服务器名只保留最新一份)。"""
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO archives (
                    name, slug, path, size_bytes, created_at, expires_at,
                    server_type, mc_version, memory_mb, online_mode, java
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    slug = excluded.slug,
                    path = excluded.path,
                    size_bytes = excluded.size_bytes,
                    created_at = excluded.created_at,
                    expires_at = excluded.expires_at,
                    server_type = excluded.server_type,
                    mc_version = excluded.mc_version,
                    memory_mb = excluded.memory_mb,
                    online_mode = excluded.online_mode,
                    java = excluded.java
                """,
                (
                    archive.name,
                    archive.slug,
                    str(archive.path),
                    archive.size_bytes,
                    archive.created_at,
                    archive.expires_at,
                    archive.server_type,
                    archive.mc_version,
                    archive.memory_mb,
                    int(archive.online_mode),
                    archive.java,
                ),
            )
        logger.debug("event=store.archive.save name=%s path=%s", archive.name, archive.path)
        return archive

    def get_archive(self, name: str) -> Archive | None:
        """按服务器名取存档元数据。"""
        with self._cursor() as cur:
            cur.execute("SELECT * FROM archives WHERE name = ?", (name,))
            row = cur.fetchone()
        return _row_to_archive(row) if row else None

    def list_archives(self) -> list[Archive]:
        """列出全部存档。"""
        with self._cursor() as cur:
            rows = cur.execute("SELECT * FROM archives ORDER BY created_at ASC").fetchall()
        return [_row_to_archive(row) for row in rows]

    def delete_archive(self, name: str) -> None:
        """删除一条存档元数据。"""
        with self._cursor() as cur:
            cur.execute("DELETE FROM archives WHERE name = ?", (name,))
        logger.debug("event=store.archive.delete name=%s", name)

    def delete(self, name: str) -> None:
        """物理删除记录。"""
        with self._cursor() as cur:
            cur.execute("DELETE FROM instances WHERE name = ?", (name,))
        logger.debug("event=store.delete name=%s", name)

    # ------------------------------------------------------------------ 读取
    def get(self, name: str) -> Instance | None:
        """按名称查询实例。"""
        with self._cursor() as cur:
            cur.execute("SELECT * FROM instances WHERE name = ?", (name,))
            row = cur.fetchone()
        return _row_to_instance(row) if row else None

    def list(self, include_destroyed: bool = False) -> list[Instance]:
        """列出实例,默认排除已销毁的记录。"""
        sql = "SELECT * FROM instances"
        if not include_destroyed:
            sql += " WHERE state != ?"
        sql += " ORDER BY id ASC"
        with self._cursor() as cur:
            rows = cur.execute(sql, () if include_destroyed else (State.destroyed.value,)).fetchall()
        return [_row_to_instance(row) for row in rows]

    def all_slugs(self) -> set[str]:
        """所有已占用的 slug(含已销毁记录与存档,避免复用冲突)。

        存档也必须算占用:保留期内用存档重建时,我们要能把**原来那个 slug**
        (也就是原来那个数据卷)拿回来,否则历史域名/数据卷就对不上了。
        """
        with self._cursor() as cur:
            rows = cur.execute(
                "SELECT slug FROM instances UNION SELECT slug FROM archives"
            ).fetchall()
        return {row["slug"] for row in rows}
