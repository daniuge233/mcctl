"""存档(archive):把实例的数据卷打包成 ``tar.gz``,并在实例删除后保留一段时间。

设计要点:

* **归档是"删除"的保护伞**::meth:`~mcctl.core.engine.Engine.destroy` 先归档、
  再删容器与数据卷;**归档失败就拒绝删除**(宁可留着一个坏掉的实例,也不要把
  地图/存档弄丢)。
* 存档文件落在 ``<archives_dir>/<slug>.tar.gz``(slug 只含 ``[a-z0-9-]``,可以安全
  用作文件名);元数据行存在 SQLite 的 ``archives`` 表里——**服务器名就是索引**。
* 保留期内服务器名**保持占用**:同名实例不能直接重建,必须先 ``restore``(用存档
  重建)或 ``purge``(显式丢弃存档)。
* 只有"非意外"删除才会走归档:``docker rm -f`` 这类绕过 mcctl 的操作不产生存档。

本模块只放纯逻辑与接口;真正读写 docker 数据卷的实现见
:mod:`mcctl.provisioners.docker_archive`。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol, runtime_checkable

from .models import now_iso, parse_iso

#: 存档文件后缀。
ARCHIVE_SUFFIX = ".tar.gz"


class ArchiveError(RuntimeError):
    """存档无法创建 / 解包 / 清理。"""


def archive_filename(slug: str) -> str:
    """存档文件名(由 slug 派生,slug 由 :func:`~mcctl.core.models.slugify` 保证安全)。"""
    return f"{slug}{ARCHIVE_SUFFIX}"


def expires_at(created: datetime, retention_minutes: int) -> datetime:
    """按保留时长算出过期时刻。"""
    return created + timedelta(minutes=retention_minutes)


@dataclass(frozen=True)
class Archive:
    """一份已归档的实例存档(元数据行,文件在 :attr:`path`)。"""

    name: str
    slug: str
    path: Path
    size_bytes: int
    created_at: str
    expires_at: str
    server_type: str
    mc_version: str
    memory_mb: int
    online_mode: bool
    java: str | None = None

    @property
    def filename(self) -> str:
        """下载时建议的文件名。"""
        return self.path.name or archive_filename(self.slug)

    def expired(self, now: datetime) -> bool:
        """是否已过保留期。"""
        return parse_iso(self.expires_at) <= now

    def remaining(self, now: datetime) -> timedelta:
        """剩余保留时长(已过期为负)。"""
        return parse_iso(self.expires_at) - now

    def to_dict(self) -> dict[str, object]:
        """给 API / CLI 用的扁平结构。"""
        return {
            "name": self.name,
            "slug": self.slug,
            "path": str(self.path),
            "filename": self.filename,
            "size_bytes": self.size_bytes,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "server_type": self.server_type,
            "mc_version": self.mc_version,
            "memory_mb": self.memory_mb,
            "online_mode": self.online_mode,
            "java": self.java,
        }


@runtime_checkable
class ArchiveStore(Protocol):
    """数据卷的打包 / 解包接口(实现必须幂等)。"""

    def create(self, *, name: str, slug: str, volume_name: str) -> Path | None:
        """把数据卷打包到存档目录并返回文件路径。

        Returns:
            存档文件路径;数据卷不存在(没什么可归档的)时返回 ``None``。

        Raises:
            ArchiveError: 打包失败(磁盘满、镜像拉取失败等)。
        """
        ...

    def extract(self, path: Path, volume_name: str) -> None:
        """确保数据卷存在,并把存档内容解包进去(已有内容时跳过,保证可重入)。"""
        ...

    def discard(self, path: Path) -> None:
        """删除存档文件;文件不存在也视为成功。"""
        ...


def find_expired(archives: list[Archive], now: datetime) -> list[Archive]:
    """挑出已过保留期的存档。"""
    return [archive for archive in archives if archive.expired(now)]


def new_archive(
    *,
    name: str,
    slug: str,
    path: Path,
    size_bytes: int,
    server_type: str,
    mc_version: str,
    memory_mb: int,
    online_mode: bool,
    java: str | None,
    retention_minutes: int,
    created: datetime | None = None,
) -> Archive:
    """组装一条存档元数据(时间戳统一为 mcctl 的 ISO 格式)。

    ``created`` 同时决定 ``created_at`` 与 ``expires_at``(注入一个时间点就能完整
    控制保留期,测试不必等 24 小时)。
    """
    moment = created or datetime.fromisoformat(now_iso())
    return Archive(
        name=name,
        slug=slug,
        path=path,
        size_bytes=size_bytes,
        created_at=moment.isoformat(timespec="seconds"),
        expires_at=expires_at(moment, retention_minutes).isoformat(timespec="seconds"),
        server_type=server_type,
        mc_version=mc_version,
        memory_mb=memory_mb,
        online_mode=online_mode,
        java=java,
    )
