"""HTTP 层与编排层之间的薄服务层。

为什么要有这一层而不是直接在路由里调 :class:`~mcctl.core.engine.Engine`:

* **串行化**:所有会改状态的操作共用一把 ``threading.Lock``。HTTP 服务天然并发,
  同一实例的 "create + delete" 同时进来必须排队,否则会出现"边建边删"的中间态。
  查询不取锁,所以读接口永远不阻塞。
* **参数回落**:请求里没给的字段回落到配置文件 ``[defaults]``(与 CLI 行为一致),
  并把 ``memory`` 字符串(``2G``)解析成 MB。
* **同步方法**:每个方法都是普通同步函数。FastAPI 对 ``def`` 端点会自动丢进线程池,
  所以这里不需要 async/await,业务代码也不必关心事件循环。
"""

from __future__ import annotations

import logging
import threading

from ..core.archive import Archive
from ..core.config import Config, parse_memory
from ..core.engine import Engine, InstanceConflict, InstanceNotFound
from ..core.lifecycle import Reaper
from ..core.models import (
    CreateRequest,
    Instance,
    State,
    normalize_command,
    validate_player_name,
)
from .schemas import CreateServerRequest, ReapReportModel, RestoreRequest

logger = logging.getLogger(__name__)


class ServerService:
    """把 Engine / Reaper 包装成 HTTP 层可直接调用的方法集合。"""

    def __init__(
        self,
        engine: Engine,
        reaper: Reaper,
        config: Config,
        *,
        lock: threading.Lock | None = None,
    ) -> None:
        self.engine = engine
        self.reaper = reaper
        self.config = config
        self._lock = lock or threading.Lock()

    # ------------------------------------------------------------------ 查询
    def list_servers(self) -> list[Instance]:
        """列出活跃实例(按创建时间排序)。"""
        return sorted(self.engine.list_instances(), key=lambda item: (item.created_at, item.id or 0))

    def get_server(self, name: str) -> Instance:
        """按名称取实例(不存在 → 404)。"""
        return self._require_live(name)

    def players(self, name: str) -> int | None:
        """在线人数;``None`` 表示当前不可知。"""
        self._require_live(name)
        return self.engine.probe_players(name)

    def logs(self, name: str, lines: int = 100) -> str:
        """实例日志尾部。"""
        self._require_live(name)
        return self.engine.logs(name, lines)

    # ------------------------------------------------------------------ 游戏指令
    def send_command(self, name: str, command: str) -> tuple[str, str]:
        """向游戏服务端发送一条指令, 返回 ``(规范化后的指令, 服务端输出)``。

        Note:
            这里**不取全局锁**:发指令不改 mcctl 自己的状态, 若取锁, 一条慢指令会把
            其它实例的 create / delete 一起堵住。副作用是实例刚好被生命周期回收时会
            报 503(容器已不在运行)。
        """
        self._require_live(name)
        normalized = normalize_command(command)
        return normalized, self.engine.send_command(name, normalized)

    def op(self, name: str, player: str) -> tuple[str, str]:
        """把玩家设为 OP,返回 ``(命令, 服务端输出)``。"""
        self._require_live(name)
        normalized = validate_player_name(player)
        return f"op {normalized}", self.engine.op(name, normalized)

    def deop(self, name: str, player: str) -> tuple[str, str]:
        """撤销玩家的 OP,返回 ``(命令, 服务端输出)``。"""
        self._require_live(name)
        normalized = validate_player_name(player)
        return f"deop {normalized}", self.engine.deop(name, normalized)

    def list_archives(self) -> list[Archive]:
        """列出全部存档(按创建时间排序)。"""
        return sorted(self.engine.list_archives(), key=lambda item: item.created_at)

    def get_archive(self, name: str) -> Archive:
        """按名称取存档(不存在 → 404)。"""
        return self.engine.archive_of(name)

    # ------------------------------------------------------------------ 变更
    def create_server(self, payload: CreateServerRequest) -> Instance:
        """创建实例(阻塞到服务端就绪;首次创建可能要等几分钟)。"""
        with self._lock:
            return self.engine.create(self._create_request(payload))

    def restore_server(self, name: str, payload: RestoreRequest | None = None) -> Instance:
        """用存档重建实例。"""
        payload = payload or RestoreRequest()
        with self._lock:
            return self.engine.restore(
                name,
                server_type=payload.type,
                mc_version=payload.version,
                memory_mb=parse_memory(payload.memory) if payload.memory else None,
                online_mode=payload.online_mode,
                java=payload.java,
            )

    def start_server(self, name: str) -> Instance:
        """启动已停止的实例。"""
        self._require_live(name)
        with self._lock:
            return self.engine.start(name)

    def stop_server(self, name: str) -> Instance:
        """优雅关停实例(保留容器与数据卷)。"""
        self._require_live(name)
        with self._lock:
            return self.engine.stop(name)

    def delete_server(self, name: str) -> tuple[Archive | None, bool]:
        """关停 + 归档 + 删除。

        Returns:
            ``(存档, 是否本次真的删除了)``。已销毁的记录视为幂等删除(``False``)。
        """
        with self._lock:
            instance = self.engine.get(name)
            if instance is None:
                self._reject_unknown_delete(name)
            assert instance is not None  # _reject_unknown_delete 不返回时才可能为 None
            if instance.state is State.destroyed:
                logger.info("event=api.delete.skipped name=%s reason=already-destroyed", name)
                return self.engine.get_archive(name), False
            # 复用生命周期里的 "先关停再归档删除" 路径,避免两套删除逻辑漂移
            return self.engine.reap(name, "api.delete"), True

    def download_archive(self, name: str, *, refresh: bool = False) -> Archive:
        """取存档用于下载:实例还活着时按需打包,已删除时用留存的存档。

        Args:
            refresh: 强制重新打包(即使已有存档)。运行中的实例是**热备份**——
                服务不停,要严格一致的快照请先 stop 或直接删除(删除会自动归档)。
        """
        with self._lock:
            return self.engine.ensure_archive(name, refresh=refresh)

    def pack_archive(self, name: str) -> Archive:
        """只打包(不下载、不删除实例)。"""
        self._require_live(name)
        with self._lock:
            return self.engine.pack_archive(name)

    def purge_archive(self, name: str) -> None:
        """丢弃存档(删文件 + 删索引),服务器名随之释放。"""
        with self._lock:
            self.engine.purge_archive(name)

    # ------------------------------------------------------------------ 巡检
    def reap_now(self) -> ReapReportModel:
        """立刻跑一轮生命周期巡检(与后台任务共用同一把锁)。"""
        report = self.reaper.tick()
        return ReapReportModel(
            purged=report.purged,
            reaped={item.name: item.reason for item in report.reaped},
            errors={name: message for name, message in report.errors},
        )

    # ------------------------------------------------------------------ 工具
    def _require_live(self, name: str) -> Instance:
        """取一个**还在**的实例;只剩存档的已删除记录一律视为不存在。

        删除时我们不物理删 DB 记录(要用它占住 slug 与数据卷名),所以 ``state`` 是
        ``destroyed``、``GET /servers`` 里又看不到它。若不显式排除,``GET /servers/{name}``
        会返回一个"其实已经没了"的实例,与列表接口自相矛盾。
        """
        instance = self.engine.get(name)
        if instance is None or instance.state is State.destroyed:
            raise InstanceNotFound(f"实例 {name!r} 不存在")
        return instance

    def _reject_unknown_delete(self, name: str) -> None:
        """实例不存在时,给出"到底是没建过还是只剩存档"的准确提示。"""
        archive = self.engine.get_archive(name)
        if archive is not None:
            raise InstanceConflict(
                f"实例 {name!r} 已不存在,但存档仍保留到 {archive.expires_at};"
                f"如确实要丢弃存档,请调用 DELETE /archives/{name}"
            )
        raise InstanceNotFound(f"实例 {name!r} 不存在")

    def _create_request(self, payload: CreateServerRequest) -> CreateRequest:
        """把请求体揉进 ``[defaults]``,得到编排层需要的 :class:`CreateRequest`。"""
        defaults = self.config.defaults
        ttl = payload.ttl if payload.ttl else defaults.ttl_minutes
        return CreateRequest(
            name=payload.name,
            server_type=payload.type or defaults.server_type,
            mc_version=payload.version or defaults.mc_version,
            memory_mb=parse_memory(payload.memory) if payload.memory else defaults.memory_mb,
            online_mode=defaults.online_mode if payload.online_mode is None else payload.online_mode,
            ttl_minutes=ttl,
            java=payload.java if payload.java is not None else defaults.java,
        )


__all__ = ["ServerService"]
