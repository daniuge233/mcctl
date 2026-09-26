"""Provisioner 接口定义。

规范要求的四个核心方法(``create`` / ``destroy`` / ``status`` / ``logs``)之外,
本文件额外声明了 CLI 与编排逻辑确实需要的方法,并在 docstring 中标注。

所有实现都必须是**幂等**的:``create`` 可以被安全重放(失败续跑),
``destroy`` 可以对不存在的资源调用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..core.java import JavaRuntime
from ..core.models import State


class ProvisionError(RuntimeError):
    """provisioner 无法完成操作(容器消失、启动失败、配置非法等)。"""


class ProvisionTimeout(ProvisionError):
    """等待实例就绪超时。"""


@dataclass(frozen=True)
class Spec:
    """创建一个实例所需的全部输入(由编排层组装)。"""

    name: str
    slug: str
    container_name: str
    volume_name: str
    network_name: str
    host: str
    server_type: str
    mc_version: str
    memory_mb: int
    online_mode: bool
    #: ``--java`` 的解析结果;``None`` 表示使用镜像自带的 Java
    java: JavaRuntime | None = None


@dataclass(frozen=True)
class Handle:
    """已创建资源的引用,供后续 start / stop / logs / destroy 使用。"""

    name: str
    slug: str
    container_name: str
    container_id: str | None
    volume_name: str


@runtime_checkable
class Provisioner(Protocol):
    """runtime 层接口:跑起 / 销毁"一个" MC 服务端。

    第一版唯一实现是 :class:`~mcctl.provisioners.docker_p.DockerProvisioner`。
    """

    # ---- 规范定义的核心接口 ----
    def create(self, spec: Spec) -> Handle:
        """确保 volume 与容器存在并处于启动状态(幂等),返回 :class:`Handle`。"""
        ...

    def destroy(self, handle: Handle) -> None:
        """删除容器与数据卷(幂等)。"""
        ...

    def status(self, handle: Handle) -> State:
        """查询实例当前状态;资源不存在时返回 ``State.destroyed``。"""
        ...

    def logs(self, handle: Handle, n: int = 100) -> str:
        """返回最近 ``n`` 行日志。"""
        ...

    # ---- 以下为 CLI / 编排所需的最小扩展 ----
    def start(self, handle: Handle) -> None:
        """启动已停止的容器。"""
        ...

    def stop(self, handle: Handle) -> None:
        """优雅停止容器。"""
        ...

    def wait_ready(self, handle: Handle, timeout: float | None = None) -> None:
        """阻塞直到 MC 服务端可接受连接,超时或容器退出则抛错。"""
        ...

    def online_players(self, handle: Handle) -> int | None:
        """通过容器内 ``rcon-cli list`` 返回在线人数;不可用时返回 ``None``。"""
        ...

    def ensure_runtime(self) -> None:
        """确保 runtime 前置条件就绪(自定义网络、mc-router 容器)。"""
        ...
