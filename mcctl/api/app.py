"""FastAPI 应用工厂:mcctl 的持久 HTTP 服务。

设计要点
--------

* **全部接口都要鉴权**:密钥以应用级依赖(:func:`require_api_key`)挂载,
  因此 ``/docs``、``/openapi.json`` 同样受保护,没有"漏一个忘了加"的可能。
* **一切以服务器名为索引**:路径里的 ``{name}`` 就是唯一标识,包括下载存档
  (已删除的实例照样能按名字下载它的存档)。
* **业务代码全是同步的**:路由写成 ``def``,FastAPI 自动放进线程池执行,
  不会阻塞事件循环;只有巡检后台任务需要 ``async``。
* **异常 → 状态码**在 :func:`_install_handlers` 里集中映射,业务代码只管抛
  :class:`~mcctl.core.engine.EngineError` 这类领域异常。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from typing import Annotated, AsyncIterator

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from .. import __version__
from ..core.archive import Archive, ArchiveError
from ..core.config import Config, ConfigError
from ..core.engine import (
    ArchiveNotFound,
    Engine,
    EngineError,
    InstanceConflict,
    InstanceNotFound,
)
from ..core.lifecycle import Reaper, ReapReport
from ..core.models import Instance
from ..core.store import Store
from ..provisioners.base import ProvisionError, ProvisionTimeout
from ..provisioners.docker_archive import DockerArchiveStore
from ..provisioners.docker_p import DockerProvisioner
from .schemas import (
    ArchiveInfo,
    CommandRequest,
    CommandResult,
    CreateServerRequest,
    DeleteResult,
    HealthInfo,
    LifecycleInfo,
    LogsResponse,
    OpRequest,
    ReapReportModel,
    RestoreRequest,
    ServerInfo,
)
from .security import require_api_key
from .service import ServerService

logger = logging.getLogger(__name__)

#: 存档下载的媒体类型(gzip 过的 tar)
ARCHIVE_MEDIA_TYPE = "application/gzip"


# --------------------------------------------------------------------- 依赖
def get_service(request: Request) -> ServerService:
    """取出应用级服务对象。"""
    return request.app.state.service


ServiceDep = Annotated[ServerService, Depends(get_service)]


# --------------------------------------------------------------------- 转换
def _archive_info(archive: Archive) -> ArchiveInfo:
    """存档 → 响应模型(附带下载地址)。"""
    return ArchiveInfo(
        name=archive.name,
        slug=archive.slug,
        filename=archive.filename,
        size_bytes=archive.size_bytes,
        created_at=archive.created_at,
        expires_at=archive.expires_at,
        server_type=archive.server_type,
        mc_version=archive.mc_version,
        memory_mb=archive.memory_mb,
        online_mode=archive.online_mode,
        java=archive.java,
        download_url=f"/servers/{archive.name}/archive",
    )


def _server_info(
    instance: Instance, engine: Engine, *, players: int | None = None
) -> ServerInfo:
    """实例 → 响应模型。"""
    return ServerInfo(
        name=instance.name,
        slug=instance.slug,
        state=instance.state.value,
        endpoint=engine.endpoint_of(instance),
        server_type=instance.server_type,
        mc_version=instance.mc_version,
        memory_mb=instance.memory_mb,
        online_mode=instance.online_mode,
        java=instance.java,
        ttl_minutes=instance.ttl_minutes,
        created_at=instance.created_at,
        volume_name=instance.volume_name,
        container_id=instance.container_id,
        players=players,
    )


# --------------------------------------------------------------------- 路由
router = APIRouter()


@router.get("/healthz", response_model=HealthInfo, summary="健康检查")
def healthz(request: Request, service: ServiceDep) -> HealthInfo:
    """服务自身状态(也用于探活)。"""
    return HealthInfo(
        status="ok",
        version=__version__,
        servers=len(service.list_servers()),
        archives=len(service.list_archives()),
        lifecycle_enabled=service.config.lifecycle.enabled,
    )


# ------------------------------------------------------------------ 服务器
@router.get("/servers", response_model=list[ServerInfo], summary="列出服务器")
def list_servers(request: Request, service: ServiceDep) -> list[ServerInfo]:
    """列出所有由 mcctl 管理的实例。"""
    engine: Engine = request.app.state.engine
    return [_server_info(item, engine) for item in service.list_servers()]


@router.post(
    "/servers",
    response_model=ServerInfo,
    status_code=status.HTTP_201_CREATED,
    summary="创建服务器",
)
def create_server(payload: CreateServerRequest, request: Request, service: ServiceDep) -> ServerInfo:
    """创建实例。**会阻塞到服务端就绪**(首次创建要下载服务端 + 生成世界,可能数分钟)。"""
    engine: Engine = request.app.state.engine
    instance = service.create_server(payload)
    return _server_info(instance, engine)


@router.get("/servers/{name}", response_model=ServerInfo, summary="获取服务器信息")
def get_server(
    name: str,
    request: Request,
    service: ServiceDep,
    players: Annotated[bool, Query(description="是否顺带探测在线人数")] = True,
) -> ServerInfo:
    """按名称查询实例;``players=true``(默认)会额外用 ``rcon-cli`` 数一次人数。"""
    engine: Engine = request.app.state.engine
    instance = service.get_server(name)
    return _server_info(instance, engine, players=service.players(name) if players else None)


@router.post("/servers/{name}/start", response_model=ServerInfo, summary="启动服务器")
def start_server(name: str, request: Request, service: ServiceDep) -> ServerInfo:
    """启动一个已停止的实例。"""
    engine: Engine = request.app.state.engine
    return _server_info(service.start_server(name), engine)


@router.post("/servers/{name}/stop", response_model=ServerInfo, summary="关停服务器")
def stop_server(name: str, request: Request, service: ServiceDep) -> ServerInfo:
    """优雅关停(保存世界后停下容器),容器与数据卷都保留。"""
    engine: Engine = request.app.state.engine
    return _server_info(service.stop_server(name), engine)


@router.delete("/servers/{name}", response_model=DeleteResult, summary="删除服务器")
def delete_server(name: str, request: Request, service: ServiceDep) -> DeleteResult:
    """关停 + **先归档再删除**。

    归档失败会直接报错并且**不删除**(避免存档丢失);删除后名字仍被存档占用,
    直到用 :http:delete:`/archives/{name}` 丢掉存档,或用
    :http:post:`/servers/{name}/restore` 重建。
    """
    archive, deleted = service.delete_server(name)
    return DeleteResult(
        name=name,
        deleted=deleted,
        archive=_archive_info(archive) if archive else None,
        message=(
            f"已删除实例 {name!r},存档保留到 {archive.expires_at}"
            if archive
            else f"实例 {name!r} 已删除(没有可归档的数据卷)"
        ),
    )


@router.get("/servers/{name}/logs", response_model=LogsResponse, summary="查看日志")
def server_logs(
    name: str,
    service: ServiceDep,
    tail: Annotated[int, Query(ge=1, le=5000, description="返回最后多少行")] = 100,
) -> LogsResponse:
    """返回实例最近 ``tail`` 行日志。"""
    return LogsResponse(name=name, lines=tail, logs=service.logs(name, tail))


# ---------------------------------------------------------------- 游戏指令
@router.post("/servers/{name}/command", response_model=CommandResult, summary="发送游戏指令")
def send_command(name: str, payload: CommandRequest, service: ServiceDep) -> CommandResult:
    """向运行中的游戏服务端发送一条指令, 返回服务端响应。

    指令经容器内 ``rcon-cli`` 通过 RCON 协议交给 Minecraft 服务端控制台(RCON 端口
    不对外开放), 且以**参数**形式传给 ``docker exec``, 不经容器内 shell, 所以
    ``;`` / ``$()`` / 反引号都只是普通字符。前导 ``/`` 会被去掉(``/op Steve`` 等价于
    ``op Steve``)。
    """
    command, output = service.send_command(name, payload.command)
    return CommandResult(name=name, command=command, output=output)


@router.post("/servers/{name}/op", response_model=CommandResult, summary="设置玩家 OP")
def op_player(name: str, payload: OpRequest, service: ServiceDep) -> CommandResult:
    """把玩家设为管理员(等价于发送游戏指令 ``op <player>``)。"""
    command, output = service.op(name, payload.player)
    return CommandResult(name=name, command=command, output=output)


@router.post("/servers/{name}/deop", response_model=CommandResult, summary="撤销玩家 OP")
def deop_player(name: str, payload: OpRequest, service: ServiceDep) -> CommandResult:
    """撤销玩家的管理员权限(等价于发送游戏指令 ``deop <player>``)。"""
    command, output = service.deop(name, payload.player)
    return CommandResult(name=name, command=command, output=output)


# ---------------------------------------------------------------- 存档/重建
@router.post("/servers/{name}/archive", response_model=ArchiveInfo, summary="打包服务器存档")
def pack_archive(name: str, service: ServiceDep) -> ArchiveInfo:
    """把实例的数据卷打包成 ``<slug>.tar.gz`` 并登记(不停止、不删除实例)。"""
    return _archive_info(service.pack_archive(name))


@router.get(
    "/servers/{name}/archive",
    summary="下载服务器存档",
    response_class=FileResponse,
    responses={200: {"content": {ARCHIVE_MEDIA_TYPE: {}}, "description": "tar.gz 存档"}},
)
def download_archive(
    name: str,
    service: ServiceDep,
    refresh: Annotated[bool, Query(description="是否强制重新打包")] = False,
) -> FileResponse:
    """下载存档。

    * 实例还在:直接用留存的那份;没有或 ``refresh=true`` 时先打包(运行中的实例是热备份)。
    * 实例已删除但仍在保留期内:照常下载。
    """
    archive = service.download_archive(name, refresh=refresh)
    return FileResponse(
        archive.path,
        media_type=ARCHIVE_MEDIA_TYPE,
        filename=archive.filename,
    )


@router.post("/servers/{name}/restore", response_model=ServerInfo, summary="用存档重建服务器")
def restore_server(
    name: str, request: Request, service: ServiceDep, payload: RestoreRequest | None = None
) -> ServerInfo:
    """用该名称的存档重新创建一个实例(数据原样回来),成功后存档被消费掉。"""
    engine: Engine = request.app.state.engine
    return _server_info(service.restore_server(name, payload), engine)


# ------------------------------------------------------------------ 存档管理
@router.get("/archives", response_model=list[ArchiveInfo], summary="列出存档")
def list_archives(service: ServiceDep) -> list[ArchiveInfo]:
    """列出所有存档(含即将过期的)。"""
    return [_archive_info(item) for item in service.list_archives()]


@router.get("/archives/{name}", response_model=ArchiveInfo, summary="获取存档信息")
def get_archive(name: str, service: ServiceDep) -> ArchiveInfo:
    """按服务器名查询存档。"""
    return _archive_info(service.get_archive(name))


@router.delete("/archives/{name}", status_code=status.HTTP_204_NO_CONTENT, summary="丢弃存档")
def purge_archive(name: str, service: ServiceDep) -> Response:
    """删掉存档文件与索引,服务器名随之释放。"""
    service.purge_archive(name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ------------------------------------------------------------------ 生命周期
@router.get("/lifecycle", response_model=LifecycleInfo, summary="查看生命周期策略")
def get_lifecycle(service: ServiceDep) -> LifecycleInfo:
    """返回当前生效的自动回收 / 存档保留规则(单位:分钟,0 表示关闭)。"""
    return LifecycleInfo(**service.config.lifecycle.describe())


@router.post("/lifecycle/reap", response_model=ReapReportModel, summary="立刻执行一轮回收")
def reap_now(service: ServiceDep) -> ReapReportModel:
    """不等后台定时任务,立刻巡检一轮(清理到期存档 + 回收超时实例)。"""
    return service.reap_now()


# --------------------------------------------------------------------- 异常
def _install_handlers(app: FastAPI) -> None:
    """领域异常 → HTTP 状态码(统一 ``{"detail": "..."}`` 结构)。"""

    def handler(status_code: int):
        def _respond(request: Request, exc: Exception) -> JSONResponse:
            logger.info(
                "event=api.error path=%s status=%s error=%s",
                request.url.path,
                status_code,
                exc,
            )
            return JSONResponse(status_code=status_code, content={"detail": str(exc)})

        return _respond

    app.add_exception_handler(InstanceNotFound, handler(404))
    app.add_exception_handler(ArchiveNotFound, handler(404))
    app.add_exception_handler(InstanceConflict, handler(409))
    app.add_exception_handler(EngineError, handler(400))
    app.add_exception_handler(ArchiveError, handler(500))
    app.add_exception_handler(ConfigError, handler(400))
    app.add_exception_handler(ProvisionTimeout, handler(504))
    app.add_exception_handler(ProvisionError, handler(503))


# --------------------------------------------------------------------- 应用
async def _reaper_loop(app: FastAPI) -> None:
    """后台巡检循环:每个 ``interval`` 秒跑一轮。

    第一轮在**一个间隔之后**才跑——刚重启的服务不应该立刻动手删东西。
    """
    reaper: Reaper = app.state.reaper
    interval = max(1.0, float(reaper.policy.interval_seconds))
    logger.info("event=reaper.loop.start interval=%ss", interval)
    try:
        while True:
            await asyncio.sleep(interval)
            try:
                await run_in_threadpool(reaper.tick)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - 巡检自身不该拖垮服务
                logger.exception("event=reaper.tick.failed")
    except asyncio.CancelledError:
        logger.info("event=reaper.loop.stop")
        raise


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动 / 关闭后台巡检任务。"""
    task: asyncio.Task[None] | None = None
    if app.state.reaper.policy.enabled:
        task = asyncio.create_task(_reaper_loop(app))
    else:
        logger.info("event=reaper.disabled")
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def create_app(
    config: Config,
    *,
    engine: Engine | None = None,
    store: Store | None = None,
    provisioner=None,
    archives=None,
) -> FastAPI:
    """组装 FastAPI 应用。

    Args:
        config: 运行时配置(密钥、生命周期策略都从这里读)。
        engine: 直接注入编排器(测试用);一旦注入,下面的 store/provisioner/archives
            会被忽略。
        store: 覆盖存储层(默认 ``Store(config.db_path)`` 并 ``init()``)。
        provisioner: 覆盖 provisioner(默认 :class:`DockerProvisioner`)。
        archives: 覆盖存档实现(默认 :class:`DockerArchiveStore`)。
    """
    if engine is None:
        store = store or Store(config.db_path)
        store.init()
        provisioner = provisioner or DockerProvisioner(config)
        archives = archives if archives is not None else DockerArchiveStore(config)
        engine = Engine(store, provisioner, config, archives=archives)

    # 服务层与巡检共用一把锁:巡检不会打断"建到一半"的实例。
    lock = threading.Lock()
    reaper = Reaper(engine, config.lifecycle, lock=lock)
    service = ServerService(engine, reaper, config, lock=lock)

    app = FastAPI(
        title="mcctl",
        version=__version__,
        summary="Minecraft 临时服务器集群管理器的 HTTP 接口",
        dependencies=[Depends(require_api_key)],
        lifespan=lifespan,
        # 框架自带的文档入口不经过依赖校验(浏览器没法带自定义头),默认整组关掉;
        # 需要时用 [api] docs = true 打开,并在内网里访问。
        docs_url="/docs" if config.api_docs else None,
        redoc_url="/redoc" if config.api_docs else None,
        openapi_url="/openapi.json" if config.api_docs else None,
    )
    app.state.config = config
    app.state.engine = engine
    app.state.reaper = reaper
    app.state.service = service
    app.include_router(router)
    _install_handlers(app)

    if not config.api_key:
        logger.error(
            "event=api.no-key 尚未配置 [api] key,所有请求都会被拒绝(HTTP 401)。"
            "请先执行 mcctl config --write 并填入密钥。"
        )
    return app
