"""RouterAdapter 空实现(占位)。

本版不做任何公网暴露,调用即抛 :class:`NotImplementedError`,以免被静默忽略。
"""

from __future__ import annotations

from ..core.models import Instance

_MESSAGE = (
    "RouterAdapter 在本版为空接口:不实现 frp / 内网穿透 / 路由下发。"
    "本版实例通过 mc-router 的 mc-router.host label 在本机发现并路由。"
)


class PlaceholderRouterAdapter:
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


__all__ = ["PlaceholderRouterAdapter"]
