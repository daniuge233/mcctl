"""实例生命周期:超时回收 + 存档保留。

四条回收规则(都能在配置文件 ``[lifecycle]`` 段调整,**0 表示关闭该规则**):::

    1. 创建后 ``join_grace_minutes`` 分钟内**没有任何玩家加入** → 关停并删除
    2. 在线人数**持续为 0** 达 ``idle_minutes`` 分钟       → 关停并删除
    3. 创建时长超过 ``max_lifetime_minutes`` 分钟          → 关停并删除
    4. 停在 ``stopped`` 状态达 ``stopped_minutes`` 分钟     → 直接删除

再加存档保留:删除后存档**额外保留** ``archive_retention_minutes`` 分钟,期间
服务器名继续占用,可以下载存档或用存档重新创建(见
:meth:`~mcctl.core.engine.Engine.restore`)。

误杀防护(重要)
---------------

前三条规则依赖的事实只有两个——``ever_had_player`` 与 ``empty_since``——而它们
**只在确实读到"0 人在线"时才推进**。也就是说:

* 容器没跑 / ``rcon-cli`` 不可用(``players is None``)→ 前两条规则**不触发**,
  只可能被"最长存活"规则回收。宁可多留一个空实例,也不要杀掉一个可能正在
  有人玩的服务器。
* 状态处于过渡态(``creating`` / ``stopping`` / ``destroyed``)时一律不介入,
  避免和正在进行的 ``create`` / ``destroy`` 抢资源。
* 规则 4 专盯 ``stopped``:这种实例本来就没在跑,读不到在线人数是正常的,
  所以它**不需要** ``players == 0`` 这个前提(但万一真读到有人在玩,仍然放过)。
  计时的起点由 :meth:`~mcctl.core.store.Store.set_state` 在进入 ``stopped``
  时写下(``stopped_since``),不依赖巡检是否恰好跑过,所以 ``mcctl serve``
  停机一段时间再启动也能立即算出真实已停时长。

:func:`observe` 与 :func:`decide` 都是纯函数(时间从外部注入),因此规则可以被
完整地单元测试,而不需要真的等 10 分钟。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from .models import Instance, State, now_utc, parse_iso

logger = logging.getLogger(__name__)

#: 共享锁类型别名(避免调用方为了拿类型去 import threading)
Lock = threading.Lock

#: 决策动作
KEEP = "keep"
REAP = "reap"

#: 回收原因(会写进日志与 API 响应,便于排障)
REASON_NO_JOIN = "no_join_within_grace"
REASON_IDLE = "idle_timeout"
REASON_MAX_LIFETIME = "max_lifetime"
REASON_STOPPED = "stopped_timeout"

#: 参与回收的状态。``creating`` / ``stopping`` 是过渡态,不介入。
RECLAIMABLE_STATES: frozenset[State] = frozenset({State.running, State.stopped})


@dataclass(frozen=True)
class LifecyclePolicy:
    """生命周期策略(配置文件 ``[lifecycle]`` 段)。"""

    enabled: bool = True
    #: 巡检间隔(秒)
    interval_seconds: float = 30.0
    #: 创建后多久无人加入就回收(分钟);0 = 关闭
    join_grace_minutes: int = 10
    #: 在线人数持续为 0 多久就回收(分钟);0 = 关闭
    idle_minutes: int = 60
    #: 最长存活时间(分钟);0 = 关闭
    max_lifetime_minutes: int = 1440
    #: 停在 ``stopped`` 状态多久就回收(分钟);0 = 关闭
    stopped_minutes: int = 1440
    #: 删除后存档额外保留多久(分钟)
    archive_retention_minutes: int = 1440

    @staticmethod
    def _window(minutes: int) -> timedelta | None:
        """把"分钟"配置变成时间窗;非正数表示关闭该规则。"""
        return timedelta(minutes=minutes) if minutes > 0 else None

    @property
    def join_grace(self) -> timedelta | None:
        """规则 1 的时间窗。"""
        return self._window(self.join_grace_minutes)

    @property
    def idle(self) -> timedelta | None:
        """规则 2 的时间窗。"""
        return self._window(self.idle_minutes)

    @property
    def max_lifetime(self) -> timedelta | None:
        """规则 3 的时间窗。"""
        return self._window(self.max_lifetime_minutes)

    @property
    def stopped(self) -> timedelta | None:
        """规则 4 的时间窗。"""
        return self._window(self.stopped_minutes)

    @property
    def archive_retention(self) -> timedelta:
        """存档保留时长。"""
        return timedelta(minutes=max(0, self.archive_retention_minutes))

    def describe(self) -> dict[str, object]:
        """给 API / CLI 展示用的策略快照。"""
        return {
            "enabled": self.enabled,
            "interval_seconds": self.interval_seconds,
            "join_grace_minutes": self.join_grace_minutes,
            "idle_minutes": self.idle_minutes,
            "max_lifetime_minutes": self.max_lifetime_minutes,
            "stopped_minutes": self.stopped_minutes,
            "archive_retention_minutes": self.archive_retention_minutes,
        }


@dataclass(frozen=True)
class Stats:
    """合并一次观测结果之后的累计事实。"""

    ever_had_player: bool
    empty_since: datetime | None


def observe(instance: Instance, players: int | None, now: datetime) -> Stats:
    """把一次在线人数观测合并进已有事实(纯函数)。

    * ``players is None``(观测不到)→ 保持原样,不推进任何计时;
    * ``players > 0`` → 记为"曾经有人来过",并清空"持续为 0"的计时;
    * ``players == 0`` → 若还没开始计时,就把起点设为 ``now``。
    """
    ever_had_player = instance.ever_had_player
    empty_since = parse_iso(instance.empty_since) if instance.empty_since else None

    if players is None:
        return Stats(ever_had_player, empty_since)
    if players > 0:
        return Stats(True, None)
    return Stats(ever_had_player, empty_since or now)


@dataclass(frozen=True)
class Observation:
    """一次决策所需的全部输入。"""

    name: str
    state: State
    created_at: datetime
    ever_had_player: bool
    empty_since: datetime | None
    players: int | None
    now: datetime
    #: 最近一次进入 ``stopped`` 的时刻(规则 4 用);从未停止过时为 ``None``
    stopped_since: datetime | None = None

    @property
    def age(self) -> timedelta:
        """实例已创建多久。"""
        return self.now - self.created_at

    @property
    def stopping_for(self) -> timedelta | None:
        """已停在 ``stopped`` 状态多久;没停过时为 ``None``。"""
        if self.stopped_since is None:
            return None
        return self.now - self.stopped_since


@dataclass(frozen=True)
class Decision:
    """对某个实例的处理结论。"""

    reap: bool
    reason: str | None = None

    def __bool__(self) -> bool:
        """允许 ``if decision:`` 这种写法。"""
        return self.reap


#: 保持实例不动
_KEEP = Decision(reap=False)


def decide(observation: Observation, policy: LifecyclePolicy) -> Decision:
    """按策略判定某个实例是否应当被回收(纯函数)。

    Returns:
        :class:`Decision`;``reap=True`` 时 :attr:`Decision.reason` 说明原因。
    """
    if not policy.enabled or observation.state not in RECLAIMABLE_STATES:
        return _KEEP

    # 规则 3:最长存活(与在线人数无关,所以放在最前面)
    lifetime = policy.max_lifetime
    if lifetime is not None and observation.age >= lifetime:
        return Decision(reap=True, reason=REASON_MAX_LIFETIME)

    # 规则 4:长期停在 stopped。
    # 停着的容器本来就探测不到人数,所以不要求 `players == 0`;但真读到有人在玩
    # (理论上不该发生)就放过——宁可漏杀不可误杀。
    stopped_window = policy.stopped
    if stopped_window is not None and observation.state is State.stopped:
        playing = observation.players is not None and observation.players > 0
        if not playing and observation.stopping_for is not None:
            if observation.stopping_for >= stopped_window:
                return Decision(reap=True, reason=REASON_STOPPED)

    # 下面两条只依据"确实读到 0 人"的事实,观测不到时一律不触发
    if observation.players != 0:
        return _KEEP

    # 规则 1:创建后一直没人加入
    grace = policy.join_grace
    if grace is not None and not observation.ever_had_player and observation.age >= grace:
        return Decision(reap=True, reason=REASON_NO_JOIN)

    # 规则 2:人数持续为 0
    idle = policy.idle
    if idle is not None and observation.empty_since is not None:
        if observation.now - observation.empty_since >= idle:
            return Decision(reap=True, reason=REASON_IDLE)

    return _KEEP


@dataclass(frozen=True)
class Reaped:
    """一次被执行的回收。"""

    name: str
    reason: str


@dataclass
class ReapReport:
    """一次巡检的结果汇总。"""

    purged: list[str] = field(default_factory=list)
    reaped: list[Reaped] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        """本次巡检是否动过东西。"""
        return bool(self.purged or self.reaped)


class Reaper:
    """生命周期巡检器:观测 → 决策 → 关停 + 归档 + 删除。

    它把"什么时候做什么"交给 :func:`decide`,自己只负责与
    :class:`~mcctl.core.engine.Engine` 打交道,因此可以直接在 CLI
    (``mcctl reap``,适合塞进 cron)或 HTTP 服务后台任务里跑。

    Args:
        lock: 可选的共享锁。HTTP 服务会把"服务层改实例"和"巡检"放在同一把锁上,
            于是巡检不会在某个实例正创建一个半截时把它回收掉。
    """

    def __init__(
        self,
        engine,
        policy: LifecyclePolicy,
        *,
        clock: Callable[[], datetime] = now_utc,
        lock: "Lock | None" = None,
    ) -> None:
        self.engine = engine
        self.policy = policy
        self.clock = clock
        self._lock = lock or threading.Lock()

    def tick(self) -> ReapReport:
        """跑一轮巡检(同一时刻只允许一轮)。**单个实例出错不影响其他实例。**"""
        with self._lock:
            return self._tick()

    def _tick(self) -> ReapReport:
        report = ReapReport()
        if not self.policy.enabled:
            logger.debug("event=reap.skipped reason=disabled")
            return report

        # 1) 先清理超过保留期的存档(服务器名随之释放)
        for archive in self.engine.list_archives():
            if not archive.expired(self.clock()):
                continue
            try:
                self.engine.purge_archive(archive.name)
            except Exception as exc:  # pragma: no cover - 文件系统异常
                logger.warning("event=reap.archive.purge-failed name=%s error=%s", archive.name, exc)
                report.errors.append((archive.name, str(exc)))
            else:
                report.purged.append(archive.name)

        # 2) 逐个实例观测 + 决策
        for instance in self.engine.list_instances():
            try:
                self._inspect(instance, report)
            except Exception as exc:
                logger.warning("event=reap.failed name=%s error=%s", instance.name, exc)
                report.errors.append((instance.name, str(exc)))

        if report.changed or report.errors:
            logger.info(
                "event=reap.done purged=%d reaped=%d errors=%d",
                len(report.purged),
                len(report.reaped),
                len(report.errors),
            )
        return report

    def _inspect(self, instance: Instance, report: ReapReport) -> None:
        """观测并处理单个实例。"""
        now = self.clock()
        players = self.engine.probe_players(instance.name)
        stats = observe(instance, players, now)
        self.engine.record_observation(instance.name, stats, players, now)

        decision = decide(
            Observation(
                name=instance.name,
                state=instance.state,
                created_at=parse_iso(instance.created_at),
                ever_had_player=stats.ever_had_player,
                empty_since=stats.empty_since,
                players=players,
                now=now,
                stopped_since=(
                    parse_iso(instance.stopped_since) if instance.stopped_since else None
                ),
            ),
            self.policy,
        )
        if not decision.reap:
            logger.debug(
                "event=reap.keep name=%s players=%s age=%s ever=%s empty_since=%s stopped_since=%s",
                instance.name,
                players,
                f"{(now - parse_iso(instance.created_at)).total_seconds():.0f}s",
                stats.ever_had_player,
                stats.empty_since.isoformat(timespec="seconds") if stats.empty_since else "-",
                instance.stopped_since or "-",
            )
            return

        assert decision.reason is not None  # reap=True 必然带原因
        self.engine.reap(instance.name, decision.reason)
        report.reaped.append(Reaped(instance.name, decision.reason))
