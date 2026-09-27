"""编排层:把 Store(期望状态)与 Provisioner(实际状态)粘合起来。

``create`` 严格分步、可重入:

1. ``allocate``  —— 分配 slug,写入 DB(state=``creating``)留下"意图"
2. ``provision`` —— 建 volume → 建容器(幂等,可续跑)
3. ``health``    —— 等健康
4. ``finalize``  —— 写 DB(state=``running``)

任一步骤失败都只把实例标记为 ``failed`` 并保留记录,重新执行 ``create`` 即可续跑
(已存在的 volume / 容器会被复用)。**没有把这些塞进一个大 try。**

``destroy`` 在此之上多了一步归档:

5. ``archive``   —— 删数据卷之前先打包成 ``<archives_dir>/<slug>.tar.gz``

归档**先于**删除,归档失败就不删——宁可留一个坏掉的实例,也不要把存档弄丢。
归档之后服务器名会被保留一段时间(存档就是它的索引),这期间可以用
:meth:`Engine.restore` 从存档重建。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime

from ..adapters.base import DnsAdapter, EdgeAdapter, RouterAdapter
from ..provisioners.base import Handle, Provisioner, Spec
from .archive import Archive, ArchiveError, ArchiveStore, new_archive
from .config import Config
from .java import resolve_java
from .lifecycle import Stats
from .models import (
    CreateRequest,
    Instance,
    State,
    normalize_command,
    now_iso,
    slugify,
    validate_player_name,
)
from .store import Store

logger = logging.getLogger(__name__)


class EngineError(RuntimeError):
    """编排层可预期的错误(名称冲突、状态非法、资源缺失等)。"""


class InstanceNotFound(EngineError):
    """实例不存在(HTTP 层映射为 404)。"""


class InstanceConflict(EngineError):
    """名称被占用 / 状态不允许(HTTP 层映射为 409)。"""


class ArchiveNotFound(EngineError):
    """该名称没有可用存档(HTTP 层映射为 404)。"""


@dataclass(frozen=True)
class Drift:
    """``reconcile`` 发现的一次状态漂移。"""

    name: str
    expected: State
    actual: State
    resolved: State


class Engine:
    """实例生命周期编排器。"""

    def __init__(
        self,
        store: Store,
        provisioner: Provisioner,
        config: Config,
        router: RouterAdapter | None = None,
        dns: DnsAdapter | None = None,
        edge: EdgeAdapter | None = None,
        archives: ArchiveStore | None = None,
    ) -> None:
        self.store = store
        self.provisioner = provisioner
        self.config = config
        # 存档实现:不传就只是“不归档”(destroy 会打警告),而不是假装成功。
        self.archives = archives
        # 暴露层适配器:本版一律为 None(或空实现),destroy 时不调用。
        self.router = router
        self.dns = dns
        self.edge = edge

    # ------------------------------------------------------------ 命名派生
    def container_name(self, instance: Instance) -> str:
        """实例容器名(也是 mc-net 内的 DNS 名)。"""
        return f"mc-{instance.slug}"

    def hostname(self, instance: Instance) -> str:
        """客户端使用的域名,写入 ``mc-router.host`` label。"""
        return f"{instance.slug}.{self.config.domain}"

    def endpoint_of(self, instance: Instance) -> str:
        """实例连接地址(本版 = ``<slug>.<domain>:<router_port>``)。"""
        return f"{self.hostname(instance)}:{self.config.router_port}"

    def endpoint_for(self, name: str) -> str:
        """按名称返回连接地址。"""
        return self.endpoint_of(self._require(name))

    # ------------------------------------------------------------ 初始化
    def init_runtime(self) -> None:
        """初始化 runtime:创建 ``mc-net`` 与 ``mc-router``(幂等)。"""
        logger.info(
            "event=runtime.init network=%s router=%s", self.config.network_name, self.config.router_name
        )
        self.provisioner.ensure_runtime()
        logger.info(
            "event=runtime.ready network=%s router=%s port=%s",
            self.config.network_name,
            self.config.router_name,
            self.config.router_port,
        )

    # ------------------------------------------------------------ 查询
    def list_instances(self) -> list[Instance]:
        """列出全部活跃实例。"""
        return self.store.list()

    def get(self, name: str) -> Instance | None:
        """按名称查询实例。"""
        return self.store.get(name)

    def require(self, name: str) -> Instance:
        """按名称查询实例,不存在则抛 :class:`InstanceNotFound`(公共入口)。"""
        return self._require(name)

    def logs(self, name: str, n: int = 100) -> str:
        """返回实例最近 ``n`` 行日志。"""
        return self.provisioner.logs(self._handle(self._require(name)), n)

    def online_players(self, name: str) -> int | None:
        """返回实例当前在线人数。"""
        return self.provisioner.online_players(self._handle(self._require(name)))

    # ------------------------------------------------------------ 游戏指令
    def send_command(self, name: str, command: str) -> str:
        """向游戏服务端发送一条指令, 返回服务端输出。

        ``command`` 可以带前导 ``/``(会被自动去掉), 例如 ``/op Steve``、``say hi``、
        ``give Steve diamond 64``。指令经 RCON 交给服务端控制台执行。

        Raises:
            InstanceNotFound: 实例不存在。
            ValueError: 指令为空 / 过长 / 含换行符。
            ProvisionError: 实例未运行或指令执行失败。
        """
        instance = self._require(name)
        normalized = normalize_command(command)
        logger.info("event=command.send name=%s command=%s", instance.name, normalized)
        return self.provisioner.send_command(self._handle(instance), normalized)

    def op(self, name: str, player: str) -> str:
        """把玩家设为 OP(等价于向游戏服务端发送 ``op <player>``)。

        Raises:
            InstanceNotFound: 实例不存在。
            ValueError: 玩家名不合法。
            ProvisionError: 实例未运行或指令执行失败。
        """
        return self.send_command(name, f"op {validate_player_name(player)}")

    def deop(self, name: str, player: str) -> str:
        """撤销玩家的 OP(等价于向游戏服务端发送 ``deop <player>``)。

        Raises:
            InstanceNotFound: 实例不存在。
            ValueError: 玩家名不合法。
            ProvisionError: 实例未运行或指令执行失败。
        """
        return self.send_command(name, f"deop {validate_player_name(player)}")

    # ------------------------------------------------------------ 创建
    def create(self, request: CreateRequest) -> Instance:
        """创建一个实例(分步、可重入)。

        Raises:
            InstanceConflict: 名称已被活跃实例占用,或已被存档占用(保留期内)。
            EngineError: 步骤执行失败(实例会被标记为 ``failed``)。
        """
        return self._provision(request)

    def restore(
        self,
        name: str,
        *,
        server_type: str | None = None,
        mc_version: str | None = None,
        memory_mb: int | None = None,
        online_mode: bool | None = None,
        java: str | None = None,
    ) -> Instance:
        """用该名称的存档重新创建一个实例(未指定的参数沿用存档里的原值)。

        存档在数据卷里解开之后才启动容器,所以世界/玩家数据会原样回来;
        **重建成功后存档被消费掉**(数据已经变回活实例了,名字也随之解除占用)。

        Raises:
            ArchiveNotFound: 该名称没有存档。
        """
        archive = self._require_archive(name)
        logger.info("event=restore name=%s archive=%s", name, archive.path)
        request = CreateRequest(
            name=name,
            server_type=server_type or archive.server_type,
            mc_version=mc_version or archive.mc_version,
            memory_mb=archive.memory_mb if memory_mb is None else memory_mb,
            online_mode=archive.online_mode if online_mode is None else online_mode,
            java=archive.java if java is None else java,
        )
        instance = self._provision(request, seed=archive)
        self.purge_archive(name)
        logger.info("event=restore.done name=%s slug=%s", name, instance.slug)
        return instance

    def _provision(self, request: CreateRequest, *, seed: Archive | None = None) -> Instance:
        """分步执行:allocate → (seed)→ provision → health → finalize。"""
        instance = self._prepare(request, seed=seed)
        try:
            if seed is not None:
                self._step_seed(instance, seed)
            self._step_provision(instance)
            instance = self._require(instance.name)  # 刷新 container_id
            self._step_health(instance)
            self._step_finalize(instance)
        except Exception as exc:
            logger.error("event=create.failed name=%s error=%s", request.name, exc)
            self._force_state(request.name, State.failed)
            raise
        return self._require(request.name)

    def _prepare(self, request: CreateRequest, *, seed: Archive | None = None) -> Instance:
        """步骤 1:分配 slug 并落库(幂等);返回待配置的实例。"""
        # 先校验 --java:非法配置不应该被写成一条 failed 记录(存库的是原始输入)
        resolve_java(request.java)
        if seed is None:
            self._reject_archived(request.name)
        existing = self.store.get(request.name)
        if existing is not None and existing.state is State.destroyed:
            # 已销毁的记录允许复用同名创建(存档占用的情况已在上面拦下)
            logger.info("event=create.reclaim name=%s", request.name)
            self.store.delete(request.name)
            existing = None

        if existing is not None:
            if existing.state not in (State.failed, State.creating):
                raise InstanceConflict(f"实例 {request.name!r} 已存在(state={existing.state.value})")
            logger.info(
                "event=create.resume name=%s state=%s", existing.name, existing.state.value
            )
            self.store.set_state(existing.name, State.creating)
            return self._require(request.name)

        # 用存档重建时抢回原来的 slug,这样数据卷名与域名都和当初一致
        slug = self._allocate_slug(request.name, preferred=seed.slug if seed else None)
        created_at = now_iso()
        instance = Instance(
            name=request.name,
            slug=slug,
            server_type=request.server_type,
            mc_version=request.mc_version,
            memory_mb=request.memory_mb,
            online_mode=request.online_mode,
            volume_name=f"mc-{slug}-data",
            state=State.creating,
            created_at=created_at,
            container_id=None,
            ttl_minutes=request.ttl_minutes,
            java=request.java,
            # 生命周期观测从“刚创建、没人来过、一直是 0 人”开始
            ever_had_player=False,
            empty_since=created_at,
        )
        saved = self.store.add(instance)
        logger.info(
            "event=create.step step=allocate name=%s slug=%s volume=%s",
            saved.name,
            saved.slug,
            saved.volume_name,
        )
        return saved

    def _step_seed(self, instance: Instance, seed: Archive) -> None:
        """步骤 1.5(仅重建):把存档解包进数据卷,再让 provisioner 复用这个卷。"""
        if self.archives is None:
            raise EngineError("未配置存档实现(ArchiveStore),无法用存档重建")
        logger.info(
            "event=restore.step step=seed name=%s archive=%s volume=%s",
            instance.name,
            seed.path,
            instance.volume_name,
        )
        self.archives.extract(seed.path, instance.volume_name)

    def _step_provision(self, instance: Instance) -> None:
        """步骤 2:建 volume → 建容器,并立刻把 container_id 写回 DB。"""
        # 保证网络(与 router)就绪,create 对未 init 的环境也能自愈
        self.provisioner.ensure_runtime()
        handle = self.provisioner.create(self._spec(instance))
        self.store.set_container(instance.name, handle.container_id)
        logger.info(
            "event=create.step step=provision name=%s container=%s id=%s",
            instance.name,
            handle.container_name,
            handle.container_id,
        )

    def _step_health(self, instance: Instance) -> None:
        """步骤 3:等待服务端就绪。"""
        logger.info(
            "event=create.step step=health name=%s timeout=%ss",
            instance.name,
            self.config.health_timeout,
        )
        self.provisioner.wait_ready(
            self._handle(instance), timeout=float(self.config.health_timeout)
        )

    def _step_finalize(self, instance: Instance) -> None:
        """步骤 4:写 DB,标记为 running。"""
        self._transition(instance.name, State.running)
        logger.info(
            "event=create.step step=finalize name=%s state=running endpoint=%s",
            instance.name,
            self.endpoint_of(instance),
        )

    def _allocate_slug(self, name: str, *, preferred: str | None = None) -> str:
        """为名称分配唯一 slug(冲突时追加 ``-2`` / ``-3`` ...)。

        ``preferred`` 用于“用存档重建”:那次要抢回**原来那个** slug(也就是原来的
        数据卷名与域名),所以得把该存档自己占的那个从“已占用”集合里摘出去。
        """
        base = preferred or slugify(name)
        taken = self.store.all_slugs()
        if preferred is not None:
            taken.discard(preferred)
        if base not in taken:
            return base
        index = 2
        while f"{base}-{index}" in taken:
            index += 1
        return f"{base}-{index}"

    def _reject_archived(self, name: str) -> None:
        """保留期内的服务器名不可直接复用(存档就是它的索引)。"""
        archive = self.store.get_archive(name)
        if archive is None:
            return
        raise InstanceConflict(
            f"名称 {name!r} 已被存档占用到 {archive.expires_at};"
            f"请用 `mcctl archive restore {name}` 从存档重建,或 `mcctl archive purge {name}` 丢弃存档"
        )

    # ------------------------------------------------------------ 生命周期
    def start(self, name: str) -> Instance:
        """启动一个已停止(或 failed)的实例。"""
        instance = self._require(name)
        if instance.state is State.destroyed:
            raise EngineError(f"实例 {name!r} 已销毁")
        if instance.state is State.running:
            logger.info("event=start.skip name=%s already running", name)
            return instance

        handle = self._handle(instance)
        if self.provisioner.status(handle) is State.destroyed:
            raise EngineError(
                f"实例 {name!r} 的容器已不存在;先执行 `mcctl rm {name}` 再重新 create"
            )
        self._transition(name, State.running)
        self.provisioner.start(handle)
        logger.info("event=start.done name=%s", name)
        return self._require(name)

    def stop(self, name: str) -> Instance:
        """优雅停止一个运行中的实例(保留容器与数据卷)。"""
        instance = self._require(name)
        if instance.state is State.destroyed:
            raise EngineError(f"实例 {name!r} 已销毁")
        if instance.state is State.stopped:
            logger.info("event=stop.skip name=%s already stopped", name)
            return instance

        self._transition(name, State.stopping)
        self.provisioner.stop(self._handle(instance))
        self._transition(name, State.stopped)
        logger.info("event=stop.done name=%s", name)
        return self._require(name)

    def destroy(self, name: str, *, archive: bool = True) -> Archive | None:
        """彻底清理:先归档数据卷,再删容器 + 数据卷 +(将来)路由 / DNS。

        幂等:对 ``failed``(容器已被删)或已销毁的实例也能安全调用。

        Args:
            archive: 是否先把数据卷打包留存。归档失败会**直接报错、不删除**,
                避免“删了才发现存档没打出来”。

        Returns:
            本次产生的存档;没有可归档内容(或未配置 ``ArchiveStore``)时为 ``None``。
        """
        instance = self._require(name)
        if instance.state is State.destroyed:
            logger.info("event=destroy.skip name=%s already destroyed", name)
            return self.store.get_archive(name)

        self._transition(name, State.stopping)
        saved: Archive | None = None
        try:
            if archive:
                saved = self._step_archive(instance)
            self._cleanup_edges(instance)
            self.provisioner.destroy(self._handle(instance))
        except Exception as exc:
            logger.error("event=destroy.failed name=%s error=%s", name, exc)
            self._force_state(name, State.failed)
            raise

        self._transition(name, State.destroyed)
        logger.info("event=destroy.done name=%s archived=%s", name, bool(saved))
        return saved

    def _step_archive(self, instance: Instance) -> Archive | None:
        """步骤 5:删卷前把数据卷打包,并在 DB 里登记存档(名字即索引)。"""
        if self.archives is None:
            logger.warning(
                "event=destroy.archive.skipped name=%s reason=no-archive-store", instance.name
            )
            return None

        path = self.archives.create(
            name=instance.name, slug=instance.slug, volume_name=instance.volume_name
        )
        if path is None:
            # 数据卷本来就不存在(例如已经被手工清掉),没有东西可归档
            return None

        meta = new_archive(
            name=instance.name,
            slug=instance.slug,
            path=path,
            size_bytes=path.stat().st_size,
            server_type=instance.server_type,
            mc_version=instance.mc_version,
            memory_mb=instance.memory_mb,
            online_mode=instance.online_mode,
            java=instance.java,
            retention_minutes=self.config.lifecycle.archive_retention_minutes,
        )
        saved = self.store.save_archive(meta)
        logger.info(
            "event=destroy.archive name=%s path=%s size=%s expires=%s",
            saved.name,
            saved.path,
            saved.size_bytes,
            saved.expires_at,
        )
        return saved

    def _cleanup_edges(self, instance: Instance) -> None:
        """清理暴露层资源。

        本版适配器为空实现,因此这里只记录日志,不实际调用,避免把
        ``NotImplementedError`` 变成 destroy 的失败原因。
        """
        logger.debug(
            "event=destroy.edges name=%s (adapters not implemented in this version)", instance.name
        )

    # ------------------------------------------------------------ 存档
    def list_archives(self) -> list[Archive]:
        """列出全部存档(含已过期待回收的)。"""
        return self.store.list_archives()

    def get_archive(self, name: str) -> Archive | None:
        """按服务器名取存档。"""
        return self.store.get_archive(name)

    def archive_of(self, name: str) -> Archive:
        """按服务器名取存档,没有则报错。"""
        return self._require_archive(name)

    def archive(self, name: str) -> Archive | None:
        """就地把运行中的实例打个包(不删除实例)。

        已经销毁的名字走不到这里:那种情况直接下载既有存档即可。
        ``None`` 表示数据卷不存在(没什么可打包的)。
        """
        return self._step_archive(self._require(name))

    def pack_archive(self, name: str) -> Archive:
        """就地把实例打个包,数据卷不存在则报错。"""
        created = self.archive(name)
        if created is None:
            raise ArchiveError(f"实例 {name!r} 的数据卷不存在,无法打包")
        return created

    def ensure_archive(self, name: str, *, refresh: bool = False) -> Archive:
        """取存档用于下载:实例还活着就按需打包,已删除就用留存的那份。

        Args:
            refresh: 强制重新打包(即使已有存档)。

        Note:
            运行中的实例是**热备份**——服务不停止,文件不保证严格一致。
            要一致的快照请先 ``stop`` 或直接删除(删除会自动归档)。
        """
        live = self.get(name)
        archive = self.get_archive(name)

        if live is not None and (refresh or archive is None):
            created = self.archive(live.name)
            if created is not None:
                archive = created

        if archive is None:
            if live is None:
                raise InstanceNotFound(f"实例 {name!r} 不存在")
            raise ArchiveError(f"实例 {name!r} 的数据卷为空或不存在,无法打包")
        if not archive.path.is_file():
            raise ArchiveNotFound(f"{name!r} 的存档文件已丢失:{archive.path}")
        return archive

    def purge_archive(self, name: str) -> None:
        """丢弃存档(删文件 + 删登记),服务器名随之释放。"""
        archive = self._require_archive(name)
        if self.archives is None:
            logger.warning("event=archive.discard.skipped name=%s reason=no-archive-store", name)
        else:
            self.archives.discard(archive.path)
        self.store.delete_archive(name)
        self._drop_placeholder(name)
        logger.info("event=archive.purged name=%s", name)

    def _drop_placeholder(self, name: str) -> None:
        """存档没了,那条占位的 ``destroyed`` 记录也就没用了,一并删掉。

        占位记录的唯一用途是“把 slug 与数据卷名留给存档”,所以存档一旦被丢弃
        (手动 purge 或巡检过期清理),继续留着只会让 :meth:`Store.all_slugs`
        永久占着这个 slug——后来者哪怕是**同名**创建,也会被顶成 ``name-2``;
        而且每删一台服务器就在库里沉淀一行,永远清不掉。

        只删 ``destroyed`` 记录:活实例(比如 `pack` 出来的存档)不受影响。
        """
        instance = self.store.get(name)
        if instance is not None and instance.state is State.destroyed:
            self.store.delete(name)
            logger.info("event=instance.reclaimed name=%s reason=archive-purged", name)

    # ------------------------------------------------------------ 巡检辅助
    def probe_players(self, name: str) -> int | None:
        """尽力读取在线人数:读不到(容器未运行 / RCON 不可用)返回 ``None``。

        与 :meth:`online_players` 的区别是不抛异常——生命周期巡检不能因为一个
        实例状态不对就整轮中断。
        """
        try:
            return self.online_players(name)
        except Exception as exc:
            logger.debug("event=players.probe.failed name=%s error=%s", name, exc)
            return None

    def record_observation(self, name: str, stats: Stats, players: int | None, now: datetime) -> None:
        """写回一次生命周期观测结果(供 :class:`~mcctl.core.lifecycle.Reaper` 使用)。"""
        self.store.save_observation(
            name,
            ever_had_player=stats.ever_had_player,
            empty_since=stats.empty_since.isoformat(timespec="seconds") if stats.empty_since else None,
            last_players=players,
            observed_at=now.isoformat(timespec="seconds"),
        )

    def reap(self, name: str, reason: str) -> Archive | None:
        """按生命周期策略回收实例:先关停,再归档并删除。

        Args:
            reason: 触发原因(只用于日志/审计)。
        """
        logger.info("event=reap name=%s reason=%s", name, reason)
        self.stop_if_running(name)
        return self.destroy(name)

    def stop_if_running(self, name: str) -> None:
        """尽力优雅关停;停不掉也继续(调用方随后通常会删除)。

        "关停并删除"里的关停没做成就放弃删除,会让一个卡死的实例永远赖着不走,
        所以这里只把失败记进日志。
        """
        instance = self._require(name)
        if instance.state is not State.running:
            return
        try:
            self.stop(name)
        except Exception as exc:
            logger.warning("event=stop.ignored name=%s error=%s", name, exc)

    # ------------------------------------------------------------ 漂移修正
    def reconcile(self) -> list[Drift]:
        """对比 DB 期望状态与 docker 实际状态,修正漂移。

        * 容器被人手删掉(实际 ``destroyed``)→ 标记为 ``failed``;
        * 容器被 Stopped/Started → 同步为 ``stopped`` / ``running``;
        * ``creating`` 中途被打断 → 保持 ``creating``,提示重新 create 续跑。

        Returns:
            本次修正的漂移列表(空列表表示无漂移)。
        """
        drifts: list[Drift] = []
        for instance in self.store.list():
            expected = instance.state
            actual = self.provisioner.status(self._handle(instance))
            if actual is expected:
                continue

            if expected is State.creating:
                resolved = State.failed if actual is State.destroyed else State.creating
            elif actual is State.destroyed:
                resolved = State.failed
            else:
                resolved = actual

            if resolved is not expected:
                self._force_state(instance.name, resolved)

            drifts.append(Drift(instance.name, expected, actual, resolved))
            logger.warning(
                "event=reconcile.drift name=%s expected=%s actual=%s resolved=%s",
                instance.name,
                expected.value,
                actual.value,
                resolved.value,
            )

        logger.info("event=reconcile.done drifted=%d", len(drifts))
        return drifts

    # ------------------------------------------------------------ 内部工具
    def _spec(self, instance: Instance) -> Spec:
        """由实例组装 provisioner 输入。"""
        return Spec(
            name=instance.name,
            slug=instance.slug,
            container_name=self.container_name(instance),
            volume_name=instance.volume_name,
            network_name=self.config.network_name,
            host=self.hostname(instance),
            server_type=instance.server_type,
            mc_version=instance.mc_version,
            memory_mb=instance.memory_mb,
            online_mode=instance.online_mode,
            java=resolve_java(instance.java),
        )

    def _handle(self, instance: Instance) -> Handle:
        """由实例组装 provisioner 句柄。"""
        return Handle(
            name=instance.name,
            slug=instance.slug,
            container_name=self.container_name(instance),
            container_id=instance.container_id,
            volume_name=instance.volume_name,
        )

    def _require(self, name: str) -> Instance:
        """按名称取实例,不存在则报错。"""
        instance = self.store.get(name)
        if instance is None:
            raise InstanceNotFound(f"实例 {name!r} 不存在")
        return instance

    def _require_archive(self, name: str) -> Archive:
        """按名称取存档,没有则报错。"""
        archive = self.store.get_archive(name)
        if archive is None:
            raise ArchiveNotFound(f"{name!r} 没有可用存档")
        return archive

    def _transition(self, name: str, target: State) -> Instance:
        """执行一次受校验的状态迁移。"""
        instance = self._require(name)
        if instance.state is target:
            return instance
        if not instance.can_transition_to(target):
            raise EngineError(
                f"实例 {name!r} 非法状态迁移:{instance.state.value} -> {target.value}"
            )
        self.store.set_state(name, target)
        logger.debug("event=state.transition name=%s %s->%s", name, instance.state.value, target.value)
        return replace(instance, state=target)

    def _force_state(self, name: str, target: State) -> None:
        """不校验迁移地写入状态(用于失败标记与漂移修正)。"""
        if self.store.get(name) is None:
            return
        self.store.set_state(name, target)
