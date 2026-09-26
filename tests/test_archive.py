"""存档元数据测试(不涉及 Docker)。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mcctl.core.archive import (
    ARCHIVE_SUFFIX,
    Archive,
    archive_filename,
    expires_at,
    find_expired,
    new_archive,
)

NOW = datetime(2025, 3, 1, 12, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(hours=24)


def _build(
    name: str = "test", *, created: datetime | None = NOW, retention: int = 1440, java=None
) -> Archive:
    return new_archive(
        name=name,
        slug=name,
        path=Path("archives") / archive_filename(name),
        size_bytes=2048,
        server_type="paper",
        mc_version="1.21",
        memory_mb=2048,
        online_mode=True,
        java=java,
        retention_minutes=retention,
        created=created,
    )


def test_archive_filename_derives_from_slug():
    """存档文件名由 slug 派生(slug 只含 [a-z0-9-],可安全当文件名)。"""
    assert archive_filename("my-server") == f"my-server{ARCHIVE_SUFFIX}"
    assert ARCHIVE_SUFFIX == ".tar.gz"


def test_expires_at_adds_retention_window():
    """过期时刻 = 创建时刻 + 保留分钟数。"""
    assert expires_at(NOW, 1440) == LATER
    assert expires_at(NOW, 0) == NOW


def test_new_archive_records_metadata():
    """新建的存档要带上"重建实例所需"的全部元数据。"""
    archive = _build(java="21")

    assert archive.name == "test"
    assert archive.slug == "test"
    assert archive.filename == f"test{ARCHIVE_SUFFIX}"
    assert archive.size_bytes == 2048
    assert archive.created_at == NOW.isoformat(timespec="seconds")
    assert archive.expires_at == LATER.isoformat(timespec="seconds")
    assert archive.server_type == "paper"
    assert archive.mc_version == "1.21"
    assert archive.memory_mb == 2048
    assert archive.online_mode is True
    assert archive.java == "21"


def test_new_archive_defaults_to_now():
    """不注入时间时用当前时刻(两次时间戳都不该为空,且 created < expires)。"""
    archive = _build(created=None)

    assert archive.created_at
    assert archive.expires_at > archive.created_at


def test_expired_and_remaining_are_boundary_exact():
    """边界:恰好到期即算过期;过期后 remaining 为负。"""
    archive = _build()

    assert archive.expired(LATER - timedelta(seconds=1)) is False
    assert archive.expired(LATER) is True
    assert archive.remaining(NOW) == timedelta(hours=24)
    assert archive.remaining(LATER + timedelta(hours=1)) == -timedelta(hours=1)


def test_to_dict_is_json_friendly():
    """``to_dict`` 给 API / CLI 用,必须是扁平且可直接序列化的。"""
    payload = _build().to_dict()

    assert payload["expires_at"] == LATER.isoformat(timespec="seconds")
    assert payload["filename"] == f"test{ARCHIVE_SUFFIX}"
    assert isinstance(payload["size_bytes"], int)
    assert isinstance(payload["path"], str)


def test_find_expired_filters_by_clock():
    """只挑出已过期的存档。"""
    fresh = _build("fresh", retention=60)
    stale = _build("stale", created=NOW - timedelta(hours=5), retention=60)

    expired = find_expired([fresh, stale], NOW)

    assert [item.name for item in expired] == ["stale"]


def test_archive_is_immutable_and_hashable():
    """存档对象不可变,可直接放进集合(便于测试断言)。"""
    archive = _build()

    with pytest.raises(Exception):
        archive.name = "other"  # type: ignore[misc]
    assert archive in {archive}
