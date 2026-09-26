"""暴露层空接口。

本版**只定义接口,不实现**:不存在 frp / 内网穿透 / Cloudflare DNS / 公网反代 / SSL。
三个适配器各自包含 ``create`` / ``destroy`` / ``status``。

将来接入时的分工建议:

* :class:`RouterAdapter` —— 把 ``<slug>.<domain>`` 路由规则下发给 frp / 反代。
* :class:`DnsAdapter`    —— 在 Cloudflare 上增删对应 DNS 记录。
* :class:`EdgeAdapter`   —— 负责证书签发 / TLS 终结等边缘能力。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..core.models import Instance


@runtime_checkable
class RouterAdapter(Protocol):
    """路由规则下发(frp / 反代)。"""

    def create(self, instance: Instance) -> None:
        """下发实例的路由规则。"""
        ...

    def destroy(self, instance: Instance) -> None:
        """撤销实例的路由规则。"""
        ...

    def status(self, instance: Instance) -> str:
        """查询路由规则当前状态。"""
        ...


@runtime_checkable
class DnsAdapter(Protocol):
    """DNS 记录管理(如 Cloudflare)。"""

    def create(self, instance: Instance) -> None:
        """创建解析记录。"""
        ...

    def destroy(self, instance: Instance) -> None:
        """删除解析记录。"""
        ...

    def status(self, instance: Instance) -> str:
        """查询解析记录状态。"""
        ...


@runtime_checkable
class EdgeAdapter(Protocol):
    """边缘能力(公网入口 / TLS 等)。"""

    def create(self, instance: Instance) -> None:
        """接入边缘入口。"""
        ...

    def destroy(self, instance: Instance) -> None:
        """摘除边缘入口。"""
        ...

    def status(self, instance: Instance) -> str:
        """查询边缘状态。"""
        ...
