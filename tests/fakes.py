"""测试用的内存 Provisioner / ArchiveStore,用于在不启动 Docker 的前提下验证编排逻辑。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from mcctl.core.models import State
from mcctl.provisioners.base import Handle, ProvisionError, ProvisionTimeout, Spec


@dataclass
class FakeContainer:
    """一个假的"容器"。"""

    container_id: str
    status: str = "running"
    ready: bool = True


@dataclass
class FakeProvisioner:
    """实现 Provisioner 协议的内存版本。

    * ``fail_health_times`` 控制 ``wait_ready`` 先失败几次,用于验证 create 可续跑。
    * ``volumes`` / ``containers`` 模拟 docker 侧的实际状态。
    * ``specs`` 记录每次 create 收到的输入,便于断言传递给 runtime 层的内容。
    """

    fail_health_times: int = 0
    runtime_ready: bool = False
    volumes: set[str] = field(default_factory=set)
    containers: dict[str, FakeContainer] = field(default_factory=dict)
    specs: list[Spec] = field(default_factory=list)
    #: 按实例名覆盖在线人数;没列出的返回 0(``None`` = 读不到)
    player_counts: dict[str, int | None] = field(default_factory=dict)
    _counter: int = 0

    # ---- 运行时前置 ----
    def ensure_runtime(self) -> None:
        self.runtime_ready = True

    # ---- 核心接口 ----
    def create(self, spec: Spec) -> Handle:
        self.specs.append(spec)
        self.volumes.add(spec.volume_name)
        if spec.container_name not in self.containers:
            self._counter += 1
            self.containers[spec.container_name] = FakeContainer(
                container_id=f"fake-{self._counter}"
            )
        container = self.containers[spec.container_name]
        container.status = "running"
        return Handle(
            name=spec.name,
            slug=spec.slug,
            container_name=spec.container_name,
            container_id=container.container_id,
            volume_name=spec.volume_name,
        )

    def destroy(self, handle: Handle) -> None:
        self.containers.pop(handle.container_name, None)
        self.volumes.discard(handle.volume_name)

    def status(self, handle: Handle) -> State:
        container = self.containers.get(handle.container_name)
        if container is None:
            return State.destroyed
        return State.running if container.status == "running" else State.stopped

    def logs(self, handle: Handle, n: int = 100) -> str:
        return "fake logs"

    # ---- 扩展接口 ----
    def start(self, handle: Handle) -> None:
        container = self.containers.get(handle.container_name)
        if container is None:
            raise ProvisionError("missing container")
        container.status = "running"

    def stop(self, handle: Handle) -> None:
        container = self.containers.get(handle.container_name)
        if container is not None:
            container.status = "exited"

    def wait_ready(self, handle: Handle, timeout: float | None = None) -> None:
        if self.fail_health_times > 0:
            self.fail_health_times -= 1
            raise ProvisionTimeout("fake timeout")

    def online_players(self, handle: Handle) -> int | None:
        return self.player_counts.get(handle.name, 0)


@dataclass
class FakeArchiveStore:
    """实现 ArchiveStore 协议的内存版本:用一个本地目录模拟"数据卷"。

    * ``files``:``volume_name -> 该卷被打包时写进归档的内容``(这里只存一行文本,
      足够验证"归档 → 解包 → 数据回来"这条链路)。
    * ``missing_volumes``:把这些卷设成"不存在",用来验证"没有数据卷就不归档"的分支。
    * ``fail_create``:让打包直接失败,用来验证"归档失败就不删除"。
    """

    directory: Path
    files: dict[str, str] = field(default_factory=dict)
    missing_volumes: set[str] = field(default_factory=set)
    fail_create: bool = False

    def __post_init__(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)

    def create(self, *, name: str, slug: str, volume_name: str) -> Path | None:
        if self.fail_create:
            from mcctl.core.archive import ArchiveError

            raise ArchiveError("fake archive failure")
        if volume_name in self.missing_volumes or volume_name not in self.files:
            return None
        path = self.directory / f"{slug}.tar.gz"
        path.write_text(self.files[volume_name], encoding="utf-8")
        return path

    def extract(self, path: Path, volume_name: str) -> None:
        self.files[volume_name] = Path(path).read_text(encoding="utf-8")

    def discard(self, path: Path) -> None:
        Path(path).unlink(missing_ok=True)
