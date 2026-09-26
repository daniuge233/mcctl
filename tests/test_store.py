"""SQLite 存储层测试(存档表 / 观测字段 / 旧库迁移)。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mcctl.core.archive import archive_filename, new_archive
from mcctl.core.models import Instance, State
from mcctl.core.store import Store

NOW = datetime(2025, 3, 1, 12, 0, tzinfo=timezone.utc)


def _store(tmp_path) -> Store:
    store = Store(tmp_path / "mcctl.db")
    store.init()
    return store


def _instance(name: str = "test", **overrides) -> Instance:
    slug = name.lower().replace(" ", "-")
    fields = {
        "name": name,
        "slug": slug,
        "server_type": "paper",
        "mc_version": "1.21",
        "memory_mb": 2048,
        "online_mode": True,
        "volume_name": f"mc-{slug}-data",
        "state": State.running,
        "created_at": NOW.isoformat(timespec="seconds"),
    }
    fields.update(overrides)
    return Instance(**fields)


def _archive(name: str = "test", *, path: Path, retention: int = 1440, created=NOW):
    return new_archive(
        name=name,
        slug=name,
        path=path,
        size_bytes=1024,
        server_type="paper",
        mc_version="1.21",
        memory_mb=2048,
        online_mode=False,
        java="21",
        retention_minutes=retention,
        created=created,
    )


# ------------------------------------------------------------------ 实例
def test_add_and_get_roundtrip(tmp_path):
    """基本读写:所有字段都要能原样回来(含新增的观测字段)。"""
    store = _store(tmp_path)
    store.add(_instance())

    loaded = store.get("test")

    assert loaded is not None
    assert loaded.slug == "test"
    assert loaded.state is State.running
    assert loaded.ever_had_player is False
    assert loaded.empty_since is None
    assert loaded.stopped_since is None
    assert loaded.last_players is None
    assert loaded.observed_at is None


def test_list_skips_destroyed_by_default(tmp_path):
    """默认列表不含 destroyed,但 include_destroyed 能拿到。"""
    store = _store(tmp_path)
    store.add(_instance())
    store.set_state("test", State.destroyed)

    assert store.list() == []
    assert len(store.list(include_destroyed=True)) == 1


def test_save_observation_writes_four_facts(tmp_path):
    """观测写回:曾经有人来过 / 开始空着的时刻 / 最近人数 / 观测时刻。"""
    store = _store(tmp_path)
    store.add(_instance())

    store.save_observation(
        "test",
        ever_had_player=True,
        empty_since="2025-03-01T13:00:00+00:00",
        last_players=0,
        observed_at="2025-03-01T14:00:00+00:00",
    )

    loaded = store.get("test")
    assert loaded is not None
    assert loaded.ever_had_player is True
    assert loaded.empty_since == "2025-03-01T13:00:00+00:00"
    assert loaded.last_players == 0
    assert loaded.observed_at == "2025-03-01T14:00:00+00:00"


def test_set_state_remembers_and_clears_the_stopped_moment(tmp_path, monkeypatch):
    """进 stopped 记下时刻,离开清空,重复写 stopped 不重置计时。

    重置是个真实的坑:``reconcile`` 发现容器已停就会再写一次 ``stopped``,
    如果每次都盖章,一个真停了 30 小时的实例会被反复“续命”而永远回收不掉。
    """
    import mcctl.core.store as store_module

    store = _store(tmp_path)
    store.add(_instance())

    monkeypatch.setattr(store_module, "now_iso", lambda: "2025-03-01T12:00:00+00:00")
    store.set_state("test", State.stopped)
    assert store.get("test").stopped_since == "2025-03-01T12:00:00+00:00"

    # 一天后再写一次同一个状态:计时起点保持不动
    monkeypatch.setattr(store_module, "now_iso", lambda: "2025-03-02T12:00:00+00:00")
    store.set_state("test", State.stopped)
    assert store.get("test").stopped_since == "2025-03-01T12:00:00+00:00"

    # 重新启动后就不再是“停着”了
    store.set_state("test", State.running)
    assert store.get("test").stopped_since is None

    # 再次停下来,重新开始计时
    monkeypatch.setattr(store_module, "now_iso", lambda: "2025-03-03T12:00:00+00:00")
    store.set_state("test", State.stopped)
    assert store.get("test").stopped_since == "2025-03-03T12:00:00+00:00"


# ------------------------------------------------------------------ 存档
def test_save_and_get_archive(tmp_path):
    """存档元数据读写(含 online_mode=False 与 java)。"""
    store = _store(tmp_path)
    saved = store.save_archive(_archive(path=tmp_path / archive_filename("test")))

    loaded = store.get_archive("test")

    assert loaded == saved
    assert loaded is not None
    assert loaded.online_mode is False
    assert loaded.java == "21"
    assert loaded.size_bytes == 1024


def test_save_archive_upserts(tmp_path):
    """同名存档只保留最新一份(服务器名是主键)。"""
    store = _store(tmp_path)
    store.save_archive(_archive(path=Path("a.tar.gz"), retention=60))
    store.save_archive(_archive(path=Path("b.tar.gz"), retention=60))

    loaded = store.get_archive("test")

    assert loaded is not None
    assert loaded.path == Path("b.tar.gz")
    assert len(store.list_archives()) == 1


def test_list_archives_is_ordered_by_creation(tmp_path):
    """列表按创建时间升序(巡检按这个顺序清理)。"""
    store = _store(tmp_path)
    store.save_archive(_archive("later", path=Path("later.tar.gz"), created=NOW + timedelta(hours=2)))
    store.save_archive(_archive("earlier", path=Path("earlier.tar.gz"), created=NOW))

    assert [item.name for item in store.list_archives()] == ["earlier", "later"]


def test_delete_archive(tmp_path):
    """删除存档索引。"""
    store = _store(tmp_path)
    store.save_archive(_archive(path=Path("a.tar.gz")))

    store.delete_archive("test")

    assert store.get_archive("test") is None
    assert store.list_archives() == []


def test_all_slugs_is_a_union(tmp_path):
    """slug 占用表要合并实例与存档:存档期间 slug 不能被别人抢走。"""
    store = _store(tmp_path)
    store.add(_instance("live"))
    store.save_archive(_archive("saved", path=Path("saved.tar.gz")))

    slugs = store.all_slugs()

    assert {"live", "saved"} <= set(slugs)


# ------------------------------------------------------------------ 迁移
_V1_SCHEMA = """
CREATE TABLE instances (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL UNIQUE,
    slug         TEXT    NOT NULL UNIQUE,
    server_type  TEXT    NOT NULL,
    mc_version   TEXT    NOT NULL,
    memory_mb    INTEGER NOT NULL,
    online_mode  INTEGER NOT NULL DEFAULT 1,
    container_id TEXT,
    volume_name  TEXT    NOT NULL,
    state        TEXT    NOT NULL,
    created_at   TEXT    NOT NULL,
    ttl_minutes  INTEGER
);
"""


def test_init_migrates_old_database(tmp_path):
    """老库(第一版 schema)能自动补列,老数据照常读得出来。"""
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(_V1_SCHEMA)
    conn.execute(
        "INSERT INTO instances (name, slug, server_type, mc_version, memory_mb, online_mode,"
        " container_id, volume_name, state, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("legacy", "legacy", "paper", "1.20", 1024, 1, "abc", "mc-legacy-data", "running", "2024-01-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    store = Store(db_path)
    store.init()

    loaded = store.get("legacy")
    assert loaded is not None
    assert loaded.java is None
    assert loaded.ever_had_player is False
    assert loaded.empty_since is None
    assert loaded.stopped_since is None
    # 迁移之后新增的功能也要能直接用
    store.save_observation(
        "legacy", ever_had_player=True, empty_since=None, last_players=2, observed_at="2024-01-02T00:00:00+00:00"
    )
    assert store.get("legacy").last_players == 2
    store.save_archive(_archive("legacy", path=Path("legacy.tar.gz")))
    assert store.get_archive("legacy") is not None


def test_init_is_idempotent(tmp_path):
    """重复 init 不报错(迁移只跑该跑的那部分)。"""
    store = _store(tmp_path)
    store.add(_instance())

    store.init()

    assert len(store.list()) == 1
