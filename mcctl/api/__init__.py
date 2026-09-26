"""mcctl 的 HTTP 服务层(FastAPI)。

对外只暴露一个工厂 :func:`create_app`,以及在需要时单独复用的
鉴权依赖 :func:`require_api_key` 与服务层 :class:`ServerService`::

    from mcctl.api import create_app
    import uvicorn

    uvicorn.run(create_app(Config.load()), host=config.api_bind, port=config.api_port)

命令行入口是 ``mcctl serve``。
"""

from __future__ import annotations

from .app import create_app, get_service, router
from .security import require_api_key
from .service import ServerService

__all__ = ["create_app", "get_service", "require_api_key", "router", "ServerService"]
