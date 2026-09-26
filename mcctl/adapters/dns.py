"""DnsAdapter 空实现(占位)。本版不接入 Cloudflare 或任何 DNS 服务。"""

from __future__ import annotations

from ..core.models import Instance

_MESSAGE = "DnsAdapter 在本版为空接口:不实现 Cloudflare DNS 等记录管理。"


class PlaceholderDnsAdapter:
    """空实现,仅占位。"""

    def create(self, instance: Instance) -> None:
        """未实现。"""
        raise NotImplementedError(_MESSAGE)

    def destroy(self, instance: Instance) -> None:
        """未实现。"""
        raise NotImplementedError(_MESSAGE)

    def status(self, instance: Instance) -> str:
        """未实现。"""
        raise NotImplementedError(_MESSAGE)


__all__ = ["PlaceholderDnsAdapter"]
