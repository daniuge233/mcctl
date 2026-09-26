"""API 鉴权:所有 HTTP 接口都要求携带配置文件里的密钥。

支持两种写法(任选其一)::

    Authorization: Bearer <key>
    X-API-Key: <key>

实现说明:

* 密钥来自配置文件的 ``[api] key``(环境变量 ``MCCTL_API_KEY`` 可覆盖),
  **运行时从 ``app.state.config`` 读**,所以应用对象里不存密钥副本。
* 比较用 :func:`hmac.compare_digest`(常数时间),避免按字节比较带来的时序侧信道;
  先统一编码成 bytes,这样密钥里含非 ASCII 字符也不会抛 ``TypeError``。
* 密钥**永远不会被写进日志**;失败时只记路径与来源 IP。
* 没配置密钥时**一律拒绝**(fail-closed),而不是放行。
"""

from __future__ import annotations

import hmac
import logging

from fastapi import Header, HTTPException, Request, status

logger = logging.getLogger(__name__)

UNAUTHORIZED_DETAIL = "缺少或错误的 API 密钥"

_BEARER = "bearer "


def _extract_token(authorization: str | None, api_key_header: str | None) -> str | None:
    """从请求头里取出待校验的密钥。

    ``X-API-Key`` 优先;``Authorization`` 兼容 ``Bearer <key>`` 与裸写 ``<key>``。
    """
    if api_key_header and api_key_header.strip():
        return api_key_header.strip()
    if not authorization:
        return None
    value = authorization.strip()
    if not value:
        return None
    if value.lower().startswith(_BEARER):
        return value[len(_BEARER) :].strip() or None
    return value


def _unauthorized() -> HTTPException:
    """构造 401(附带标准的 ``WWW-Authenticate``,告诉客户端该用哪种方案)。"""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=UNAUTHORIZED_DETAIL,
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_api_key(
    request: Request,
    authorization: str | None = Header(None, description="``Bearer <key>``"),
    x_api_key: str | None = Header(None, description="直接给密钥"),
) -> None:
    """校验 API 密钥;校验不过抛 401。

    作为应用的全局依赖挂载,因此**每一个**接口(含 ``/docs`` 与 ``/openapi.json``)
    都受保护。
    """
    expected = getattr(request.app.state.config, "api_key", "") or ""
    if not expected:
        logger.error("event=auth.misconfigured path=%s reason=no-api-key", request.url.path)
        raise _unauthorized()

    provided = _extract_token(authorization, x_api_key)
    if provided is None or not hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
        client = request.client.host if request.client else "-"
        logger.warning("event=auth.rejected path=%s client=%s", request.url.path, client)
        raise _unauthorized()

    logger.debug("event=auth.ok path=%s", request.url.path)
