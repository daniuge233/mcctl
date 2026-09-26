"""存档的 Docker 实现:用一次性容器给 named volume 打包 / 解包。

数据是 named volume(``mc-<slug>-data``),宿主机上没有现成路径,所以打包必须
借一个容器来做::

    打包  docker run --rm -v mc-foo-data:/data:ro -v <archives_dir>:/backup:rw \\
              alpine:3 tar czf /backup/foo.tar.gz -C /data .
    解包  docker run --rm -v mc-foo-data:/data:rw -v <archives_dir>:/backup:ro \\
              alpine:3 tar xzf /backup/foo.tar.gz -C /data

实现细节:

* 打包**先写 ``<slug>.tar.gz.part`` 再原子重命名**,这样下载接口永远不会读到
  写了一半的 tarball(写失败时也会主动删掉临时文件)。
* helper 容器 ``--network-disabled``,不需要网络,少一个出问题的途径。
* :meth:`DockerArchiveStore.create` 在数据卷不存在时返回 ``None``(没什么可归档的),
  而不是报错——否则一个"容器和卷都已经被手工清掉"的实例就永远删不掉了。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import docker
from docker.errors import APIError, ContainerError, ImageNotFound, NotFound
from docker.models.volumes import Volume

from ..core.archive import ARCHIVE_SUFFIX, ArchiveError, archive_filename
from ..core.config import Config

logger = logging.getLogger(__name__)


class DockerArchiveStore:
    """:class:`~mcctl.core.archive.ArchiveStore` 的 Docker 实现。"""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._client: docker.DockerClient | None = None

    @property
    def client(self) -> docker.DockerClient:
        """惰性创建 docker 客户端(与 provisioner 保持一致的做法)。"""
        if self._client is None:
            self._client = docker.from_env()
        return self._client

    @property
    def directory(self) -> Path:
        """存档目录。

        走 :meth:`Config.archives_root` 而不是直接读 ``config.archives_dir``:
        后者在没显式配置时是 ``None``,真正的默认值是"数据库文件旁边的 ``archives/``"。
        """
        return self.config.archives_root()

    # ------------------------------------------------------------ 接口实现
    def create(self, *, name: str, slug: str, volume_name: str) -> Path | None:
        """把 ``volume_name`` 打包成 ``<archives_dir>/<slug>.tar.gz``。"""
        volume = self._find_volume(volume_name)
        if volume is None:
            logger.warning(
                "event=archive.no-volume name=%s volume=%s (跳过归档)", name, volume_name
            )
            return None

        self.directory.mkdir(parents=True, exist_ok=True)
        filename = archive_filename(slug)
        target = self.directory / filename
        partial = self.directory / f"{filename}.part"

        logger.info("event=archive.create name=%s volume=%s target=%s", name, volume_name, target)
        try:
            self._run(
                ["tar", "czf", f"/backup/{partial.name}", "-C", "/data", "."],
                {volume_name: {"bind": "/data", "mode": "ro"}, self._bind(self.directory): {"bind": "/backup", "mode": "rw"}},
            )
            os.replace(partial, target)
        except ArchiveError:
            partial.unlink(missing_ok=True)
            raise
        except OSError as exc:
            partial.unlink(missing_ok=True)
            raise ArchiveError(f"存档写入失败:{exc}") from exc

        logger.info("event=archive.created name=%s path=%s size=%s", name, target, target.stat().st_size)
        return target

    def extract(self, path: Path, volume_name: str) -> None:
        """确保数据卷存在,并把存档解包进去(卷里已有内容则跳过)。"""
        source = Path(path)
        if not source.is_file():
            raise ArchiveError(f"存档文件不存在:{source}")

        if self._find_volume(volume_name) is None:
            logger.info("event=archive.volume.create volume=%s (用存档重建)", volume_name)
            # 这里不带 mcctl 标签:紧随其后的 provisioner.create() 会复用它。
            self._create_volume(volume_name)

        if not self._is_empty(volume_name):
            logger.warning(
                "event=archive.extract.skip volume=%s reason=not-empty (幂等重入,不重复解包)",
                volume_name,
            )
            return

        logger.info("event=archive.extract volume=%s source=%s", volume_name, source)
        self._run(
            ["tar", "xzf", f"/backup/{source.name}", "-C", "/data"],
            {
                volume_name: {"bind": "/data", "mode": "rw"},
                self._bind(source.parent): {"bind": "/backup", "mode": "ro"},
            },
        )

    def discard(self, path: Path) -> None:
        """删除存档文件(不存在也当成功)。"""
        target = Path(path)
        try:
            target.unlink(missing_ok=True)
        except OSError as exc:
            raise ArchiveError(f"删除存档失败 {target}:{exc}") from exc
        logger.debug("event=archive.discard path=%s", target)

    # ------------------------------------------------------------ 内部工具
    def _run(self, command: list[str], volumes: dict[str, dict[str, str]]) -> bytes:
        """跑一个一次性 helper 容器,返回它的 stdout。"""
        self._ensure_image()
        try:
            output = self.client.containers.run(
                self.config.archive_image,
                command=command,
                volumes=volumes,
                remove=True,
                network_disabled=True,
                user="0",
            )
        except ContainerError as exc:
            detail = (exc.stderr or b"").decode("utf-8", "replace").strip()
            raise ArchiveError(
                f"helper 容器执行失败({' '.join(command)}),退出码 {exc.exit_status}:{detail or exc}"
            ) from exc
        except APIError as exc:
            raise ArchiveError(f"helper 容器无法启动:{exc}") from exc
        return output if isinstance(output, bytes) else b""

    def _is_empty(self, volume_name: str) -> bool:
        """数据卷里是否没有任何条目(挂载不存在的卷时 docker 会新建,因此必然存在)。"""
        listing = self._run(
            ["sh", "-c", "ls -A /data 2>/dev/null | head -n 1"],
            {volume_name: {"bind": "/data", "mode": "ro"}},
        )
        return not listing.strip()

    def _ensure_image(self) -> None:
        """确保 helper 镜像在本地(缺失则拉取)。"""
        image = self.config.archive_image
        try:
            self.client.images.get(image)
            return
        except ImageNotFound:
            pass
        except APIError as exc:  # pragma: no cover - daemon 异常
            raise ArchiveError(f"无法查询本地镜像 {image}:{exc}") from exc

        logger.info("event=archive.image.pull image=%s", image)
        try:
            self.client.images.pull(image)
        except APIError as exc:
            raise ArchiveError(f"拉取 helper 镜像 {image} 失败,请检查网络连通性:{exc}") from exc

    def _find_volume(self, name: str) -> Volume | None:
        try:
            return self.client.volumes.get(name)
        except NotFound:
            return None

    def _create_volume(self, name: str) -> Volume:
        try:
            return self.client.volumes.create(name=name)
        except APIError as exc:
            raise ArchiveError(f"无法创建数据卷 {name}:{exc}") from exc

    @staticmethod
    def _bind(path: Path) -> str:
        """把宿主机路径转成 docker 能接受的挂载源(Windows 下换成正斜杠)。"""
        resolved = str(Path(path).resolve())
        if os.name == "nt":
            return resolved.replace("\\", "/")
        return resolved


__all__ = ["DockerArchiveStore", "ARCHIVE_SUFFIX"]
