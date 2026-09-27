"""HTTP 接口的请求 / 响应模型(pydantic v2)。

约定:

* **所有接口都以服务器名为索引**,路径里的 ``{name}`` 就是 ``mcctl create`` 时用的名字。
* 时间一律是 UTC 的 ISO-8601 字符串(mcctl 内部就是这么存的)。
* ``memory`` 用人类可读的写法(``2G`` / ``1024M``),与 CLI 的 ``--memory`` 一致。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from ..core.models import COMMAND_MAX_LENGTH, normalize_command, slugify, validate_player_name

__all__ = [
    "ArchiveInfo",
    "CommandRequest",
    "CommandResult",
    "CreateServerRequest",
    "DeleteResult",
    "HealthInfo",
    "LifecycleInfo",
    "LogsResponse",
    "OpRequest",
    "ReapReportModel",
    "RestoreRequest",
    "ServerInfo",
]


class CreateServerRequest(BaseModel):
    """创建服务器。未提供的字段回落到配置文件 ``[defaults]`` 的值。"""

    name: str = Field(..., min_length=1, max_length=60, description="服务器名,唯一索引")
    type: str | None = Field(None, description="服务端类型,如 paper / vanilla / fabric")
    version: str | None = Field(None, description="Minecraft 版本,如 1.21")
    memory: str | None = Field(None, description="内存大小,如 2G / 1024M")
    online_mode: bool | None = Field(
        None, description="是否开启正版验证;false = 离线模式"
    )
    ttl: int | None = Field(None, ge=0, description="分钟;>0 时到点自动销毁")
    java: str | None = Field(
        None, description='Java 版本或 JDK 的 bin 目录,如 "21" / "java21-jdk" / "C:/jdk21/bin"'
    )

    @field_validator("name")
    @classmethod
    def _name_must_be_slugifiable(cls, value: str) -> str:
        """名字要能派生出合法 slug(例如至少含一个字母/数字),否则容器名/域名会没法拼。"""
        slugify(value)
        return value


class RestoreRequest(BaseModel):
    """用存档重建服务器。未提供的字段沿用存档里记录的原始配置。"""

    type: str | None = None
    version: str | None = None
    memory: str | None = None
    online_mode: bool | None = None
    java: str | None = None


class ServerInfo(BaseModel):
    """实例信息(列表与详情共用;``players`` 只在详情查询时才填)。"""

    name: str
    slug: str
    state: str = Field(..., description="creating / running / stopping / stopped / destroyed / failed")
    endpoint: str = Field(..., description="客户端连接地址")
    server_type: str
    mc_version: str
    memory_mb: int
    online_mode: bool
    java: str | None = None
    ttl_minutes: int | None = None
    created_at: datetime
    volume_name: str
    container_id: str | None = None
    players: int | None = Field(None, description="在线人数;null = 未查询或暂不可知")


class ArchiveInfo(BaseModel):
    """存档信息(服务器名就是索引)。"""

    name: str
    slug: str
    filename: str
    size_bytes: int
    created_at: datetime
    expires_at: datetime = Field(..., description="超过此刻会被自动清理,名字随之释放")
    server_type: str
    mc_version: str
    memory_mb: int
    online_mode: bool
    java: str | None = None
    download_url: str


class DeleteResult(BaseModel):
    """删除结果。"""

    name: str
    deleted: bool
    archive: ArchiveInfo | None = Field(None, description="删除时留存下来的存档(如有)")
    message: str


class LogsResponse(BaseModel):
    """实例日志尾部。"""

    name: str
    lines: int
    logs: str


class CommandRequest(BaseModel):
    """向游戏服务端发送一条指令。"""

    command: str = Field(
        ...,
        min_length=1,
        max_length=COMMAND_MAX_LENGTH,
        description="游戏指令,可带前导 /(如 op Steve、/say hi),会自动去掉 / 与首尾空白",
    )

    @field_validator("command")
    @classmethod
    def _command_must_be_valid(cls, value: str) -> str:
        """空指令 / 含换行的指令在这里就被拦掉(→ 422),同时统一去掉前导 ``/``。"""
        return normalize_command(value)


class OpRequest(BaseModel):
    """设置 / 撤销某个玩家的 OP。"""

    player: str = Field(..., description="玩家名(1-16 位字母 / 数字 / 下划线)")

    @field_validator("player")
    @classmethod
    def _player_must_be_valid(cls, value: str) -> str:
        """玩家名不合法直接 422,不把脏字符串拼进命令里。"""
        return validate_player_name(value)


class CommandResult(BaseModel):
    """命令执行结果。"""

    name: str = Field(..., description="服务器名")
    command: str = Field(..., description="实际发给服务端的命令(已规范化)")
    output: str = Field(..., description="服务端返回的文本;无响应时为空字符串")


class LifecycleInfo(BaseModel):
    """当前生效的生命周期策略(时间单位:分钟,``0`` 表示关闭该规则)。

    四条回收规则(满足任一即"关停 + 归档 + 删除"):

    1. 创建后 ``join_grace_minutes`` 分钟内无人加入;
    2. 在线人数为 0 持续 ``idle_minutes`` 分钟;
    3. 创建时长超过 ``max_lifetime_minutes`` 分钟;
    4. 停在 ``stopped`` 状态达 ``stopped_minutes`` 分钟(已停着,不再关停)。

    删除后存档保留 ``archive_retention_minutes`` 分钟,期间名字仍可用来下载 / 重建。
    """

    enabled: bool
    interval_seconds: float
    join_grace_minutes: int
    idle_minutes: int
    max_lifetime_minutes: int
    stopped_minutes: int
    archive_retention_minutes: int


class ReapReportModel(BaseModel):
    """一轮生命周期巡检的结果。"""

    purged: list[str] = Field(default_factory=list, description="因超过保留期而被清理的存档")
    reaped: dict[str, str] = Field(
        default_factory=dict, description="被回收的实例:名字 -> 触发原因"
    )
    errors: dict[str, str] = Field(default_factory=dict, description="出错的名字 -> 错误信息")


class HealthInfo(BaseModel):
    """服务健康信息。"""

    status: str
    version: str
    servers: int
    archives: int
    lifecycle_enabled: bool
