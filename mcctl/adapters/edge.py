"""EdgeAdapter 空实现(占位)。本版不做公网反代与 SSL。"""

from __future__ import annotations

from ..core.models import Instance

_MESSAGE = "EdgeAdapter 在本版为空接口:不实现公网反代 / SSL / 证书管理。"


class PlaceholderEdgeAdapter:
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


__all__ = ["PlaceholderEdgeAdapter"]
