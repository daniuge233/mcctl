"""Instance 数据模型与状态机。

状态机:``creating → running → stopping → stopped → destroyed``,外加 ``failed``。

``failed`` 可以从任何非终态进入,用于表达"需要人工介入"的漂移
(例如容器被人手删掉之后由 ``reconcile`` 标记)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum


class State(str, Enum):
    """实例状态。继承 ``str`` 以便直接存入 SQLite。"""

    creating = "creating"
    running = "running"
    stopping = "stopping"
    stopped = "stopped"
    destroyed = "destroyed"
    failed = "failed"


#: 允许的状态迁移。未列出的组合视为非法迁移。
_TRANSITIONS: dict[State, frozenset[State]] = {
    State.creating: frozenset({State.running, State.stopping, State.failed, State.destroyed}),
    State.running: frozenset({State.stopping, State.stopped, State.failed, State.destroyed}),
    State.stopping: frozenset({State.stopped, State.failed, State.destroyed}),
    State.stopped: frozenset({State.running, State.stopping, State.failed, State.destroyed}),
    State.failed: frozenset(
        {State.creating, State.running, State.stopping, State.stopped, State.destroyed}
    ),
    State.destroyed: frozenset(),
}

#: 终态:不会再发生自动迁移。
TERMINAL_STATES: frozenset[State] = frozenset({State.destroyed})

#: 需要被 ``reconcile`` 关注的活跃状态(``destroyed`` 记录不再跟踪)。
ACTIVE_STATES: frozenset[State] = frozenset(
    {State.creating, State.running, State.stopping, State.stopped, State.failed}
)


def can_transition(source: State, target: State) -> bool:
    """判断 ``source -> target`` 是否为合法状态迁移。"""
    return target in _TRANSITIONS.get(source, frozenset())


def now_utc() -> datetime:
    """当前 UTC 时间(带时区)。"""
    return datetime.now(timezone.utc)


def now_iso() -> str:
    """当前 UTC 时间的 ISO-8601 字符串(秒级精度)。"""
    return now_utc().isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    """解析 mcctl 写入的时间戳,统一返回带时区的 UTC 时间。

    库里全部是 ``now_iso()`` 写出来的格式(固定长度、带 ``+00:00``),
    但这里不依赖字串比较来排序,避免手改库或将来换格式时出现静默错序。
    """
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


_SLUG_RE = re.compile(r"[^a-z0-9]+")

#: 游戏指令的最大长度(防止误把大段文本当成指令发进去)。
COMMAND_MAX_LENGTH = 512

#: Java 版玩家名:1-16 位字母、数字或下划线。
_PLAYER_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")


def slugify(value: str) -> str:
    """把实例名转换为可用于 DNS 标签的 slug(小写字母 / 数字 / 连字符)。

    Raises:
        ValueError: 当 ``value`` 中没有任何可用字符时。
    """
    slug = _SLUG_RE.sub("-", value.strip().lower()).strip("-")
    # DNS label 最长 63 字符,留出 ``-2`` 之类的去重后缀空间
    slug = slug[:60].strip("-")
    if not slug:
        raise ValueError(f"无法从 {value!r} 生成合法 slug(至少需要一个字母或数字)")
    return slug


def normalize_command(value: str) -> str:
    """规范化要发给游戏服务端的指令(服务端控制台指令, 不是 Linux 命令)。

    * 去掉前导 ``/``(RCON 传输的是不带斜杠的指令, 游戏内聊天框才用 ``/``);
    * 去掉首尾空白;
    * 拒绝空指令、超长指令与含换行的指令(换行会被误当成多条指令)。

    Raises:
        ValueError: 指令为空、过长或含换行符。
    """
    command = value.strip()
    if command.startswith("/"):
        command = command[1:].lstrip()
    if not command:
        raise ValueError("命令不能为空")
    if len(command) > COMMAND_MAX_LENGTH:
        raise ValueError(f"命令过长(最多 {COMMAND_MAX_LENGTH} 个字符)")
    if "\n" in command or "\r" in command:
        raise ValueError("命令不能包含换行符")
    return command


def validate_player_name(value: str) -> str:
    """校验 Java 版玩家名(1-16 位字母、数字或下划线)。

    Raises:
        ValueError: 玩家名不合法。
    """
    player = value.strip()
    if not _PLAYER_NAME_RE.match(player):
        raise ValueError(f"非法的玩家名 {value!r}:只能包含 1-16 位字母、数字或下划线")
    return player


@dataclass
class Instance:
    """一个 MC 实例的期望状态描述。

    注意:模型里**没有 host_port**,因为实例容器不映射任何宿主机端口;
    客户端统一连到 mc-router 的 ``router_port``。
    """

    name: str
    slug: str
    server_type: str
    mc_version: str
    memory_mb: int
    online_mode: bool
    volume_name: str
    state: State
    created_at: str
    container_id: str | None = None
    ttl_minutes: int | None = None
    #: ``--java`` 的原始输入(版本号或 JDK 路径);``None`` 表示用镜像自带 Java
    java: str | None = None
    id: int | None = None
    # ---- 生命周期巡检观测到的事实(见 mcctl/core/lifecycle.py)----
    #: 是否曾经看到过至少一名玩家(用于"创建后无人加入"规则)
    ever_had_player: bool = False
    #: 在线人数最近一次为 0 的时刻(用于"持续无人"规则);新实例初始为创建时刻
    empty_since: str | None = None
    #: 最近一次进入 ``stopped`` 的时刻(用于"停在停止状态太久"规则);离开该状态即清空
    stopped_since: str | None = None
    #: 最近一次观测到的在线人数;``None`` 表示观测不到(容器未运行 / RCON 不可用)
    last_players: int | None = None
    #: 最近一次观测时刻
    observed_at: str | None = None

    def can_transition_to(self, target: State) -> bool:
        """判断当前状态是否可以迁移到 ``target``。"""
        return can_transition(self.state, target)

    @property
    def is_active(self) -> bool:
        """是否为需要被跟踪的活跃实例(非 ``destroyed``)。"""
        return self.state in ACTIVE_STATES


@dataclass(frozen=True)
class CreateRequest:
    """``mcctl create`` 的输入参数(尚未分配 slug / volume)。"""

    name: str
    server_type: str = "paper"
    mc_version: str = "1.21"
    memory_mb: int = 2048
    online_mode: bool = True
    ttl_minutes: int | None = None
    #: ``--java``:Java 版本号(如 ``21``)或宿主机 JDK 的 bin 目录路径
    java: str | None = None
