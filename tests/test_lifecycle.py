"""生命周期规则测试(不涉及 Docker,时间由注入的 clock 控制)。

四条规则(配置文件 ``[lifecycle]``)::

    1. 创建后 10 分钟内无人加入          → 回收
    2. 在线人数持续为 0 达 1 小时        → 回收
    3. 创建时长超过 24 小时              → 回收
    4. 停在 stopped 状态超过 24 小时     → 回收

删除后存档保留 24 小时。测试全部用"注入时间点"的方式写,所以 10 分钟 / 1 小时 /
24 小时这些阈值都是瞬时验证的,不会真的等待。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from mcctl.core.archive import Archive
from mcctl.core.config import Config
from mcctl.core.engine import Engine
from mcctl.core.lifecycle import (
    REASON_IDLE,
    REASON_MAX_LIFETIME,
    REASON_NO_JOIN,
    REASON_STOPPED,
    LifecyclePolicy,
    Observation,
    Reaper,
    Stats,
    decide,
    observe,
)
from mcctl.core.models import Instance, State, slugify
from mcctl.core.store import Store
from tests.fakes import FakeArchiveStore, FakeContainer, FakeProvisioner

NOW = datetime(2025, 3, 1, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------- 纯函数
def _observation(
    *,
    age_minutes: float = 0,
    players: int | None = 0,
    ever_had_player: bool = False,
    empty_minutes: float | None = None,
    state: State = State.running,
    stopped_minutes: float | None = None,
) -> Observation:
    return Observation(
        name="test",
        state=state,
        created_at=NOW - timedelta(minutes=age_minutes),
        ever_had_player=ever_had_player,
        empty_since=None if empty_minutes is None else NOW - timedelta(minutes=empty_minutes),
        players=players,
        now=NOW,
        stopped_since=None if stopped_minutes is None else NOW - timedelta(minutes=stopped_minutes),
    )


def test_rule1_reaps_at_exactly_ten_minutes_without_join():
    """规则 1 的边界:满 10 分钟即回收,差一秒不回收。"""
    policy = LifecyclePolicy()

    assert decide(_observation(age_minutes=9.99), policy).reason is None
    decision = decide(_observation(age_minutes=10), policy)
    assert decision.reap is True
    assert decision.reason == REASON_NO_JOIN


def test_rule1_does_not_fire_when_somebody_joined():
    """曾经有人进来过,就不再算"没人加入"。"""
    decision = decide(_observation(age_minutes=30, ever_had_player=True), LifecyclePolicy())

    assert decision.reap is False


def test_rule2_reaps_when_empty_for_an_hour():
    """规则 2:持续 0 人满 1 小时回收;有玩家进来(ever_had_player)也照样受管。"""
    policy = LifecyclePolicy()

    assert decide(_observation(empty_minutes=59, ever_had_player=True), policy).reap is False
    decision = decide(_observation(empty_minutes=60, ever_had_player=True), policy)
    assert decision.reason == REASON_IDLE


def test_rule3_reaps_on_max_lifetime_regardless_of_players():
    """规则 3 与在线人数无关:即使有人正在玩,活过 24 小时也回收。"""
    decision = decide(
        _observation(age_minutes=1440, players=5, ever_had_player=True), LifecyclePolicy()
    )

    assert decision.reason == REASON_MAX_LIFETIME


def test_rules_never_fire_when_players_are_unknown():
    """观测不到人数(容器没跑 / RCON 不可用)时,前两条规则绝不触发。"""
    policy = LifecyclePolicy()

    for age in (10, 60, 600):
        assert decide(_observation(age_minutes=age, players=None), policy).reap is False


def test_rule4_reaps_a_long_stopped_instance_at_the_boundary():
    """规则 4 的边界:停满 24 小时即回收,差一点不回收。"""
    policy = LifecyclePolicy()

    assert decide(_observation(state=State.stopped, stopped_minutes=1439.99), policy).reap is False
    decision = decide(_observation(state=State.stopped, stopped_minutes=1440), policy)
    assert decision.reap is True
    assert decision.reason == REASON_STOPPED


def test_rule4_fires_even_though_a_stopped_container_reports_no_players():
    """停着的容器探测不到在线人数(``players is None``),规则 4 必须照样生效。

    这条用例把判定顺序钉死了:一旦有人把规则 4 挪到 ``players != 0`` 那道闸门
    后面,``None != 0`` 会让它永远不触发。
    """
    decision = decide(
        _observation(state=State.stopped, players=None, stopped_minutes=2000),
        LifecyclePolicy(),
    )

    assert decision.reason == REASON_STOPPED


def test_rule4_does_not_fire_while_running_or_never_stopped():
    """只有真的停在 ``stopped`` 才算数:运行中、或从未停过时不触发。"""
    policy = LifecyclePolicy()

    # 运行中:即使库里残留一个很早的 stopped_since 也不回收
    assert (
        decide(_observation(state=State.running, stopped_minutes=9000), policy).reap is False
    )
    # 处于 stopped 但没有任何停止时刻记录(旧库 / 手工改过)→ 无法计时,放过
    assert decide(_observation(state=State.stopped), policy).reap is False


def test_rule4_yields_to_players_still_playing():
    """万一停着的实例真读到有人在玩,放过它(宁可漏杀不可误杀)。"""
    decision = decide(
        _observation(state=State.stopped, players=2, stopped_minutes=9000),
        LifecyclePolicy(),
    )

    assert decision.reap is False


def test_rule4_zero_threshold_disables_only_this_rule():
    """``stopped = 0`` 只关掉规则 4,其它规则不受影响。"""
    policy = LifecyclePolicy(stopped_minutes=0)

    assert (
        decide(_observation(state=State.stopped, stopped_minutes=9999), policy).reap is False
    )
    assert (
        decide(_observation(age_minutes=2000, players=0), policy).reason == REASON_MAX_LIFETIME
    )


def test_rule3_wins_over_rule4_when_both_match():
    """两条都满足时先报"最长存活"(全局规则优先于状态规则)。"""
    decision = decide(
        _observation(age_minutes=1500, state=State.stopped, stopped_minutes=1490),
        LifecyclePolicy(),
    )

    assert decision.reason == REASON_MAX_LIFETIME


def test_transitional_states_are_left_alone():
    """creating / stopping 是过渡态,不介入(避免和 create/destroy 抢资源)。"""
    policy = LifecyclePolicy()

    for state in (State.creating, State.stopping, State.destroyed, State.failed):
        assert decide(_observation(age_minutes=9999, state=state), policy).reap is False


def test_zero_thresholds_disable_individual_rules():
    """把某个阈值设为 0 即关闭该规则。"""
    policy = LifecyclePolicy(join_grace_minutes=0, idle_minutes=0)

    assert decide(_observation(age_minutes=600, empty_minutes=600), policy).reap is False
    # 但最长存活仍然生效
    assert (
        decide(_observation(age_minutes=2000, empty_minutes=600), policy).reason
        == REASON_MAX_LIFETIME
    )


def test_disabled_policy_keeps_everything():
    """enabled = false 时任何实例都不回收。"""
    policy = LifecyclePolicy(enabled=False)

    assert decide(_observation(age_minutes=99999), policy).reap is False


def test_observe_ignores_unknown_player_count():
    """``players is None`` 不推进任何计时。"""
    instance = _instance(ever_had_player=True, empty_since=(NOW - timedelta(minutes=30)).isoformat())

    stats = observe(instance, None, NOW)

    assert stats.ever_had_player is True
    assert stats.empty_since == NOW - timedelta(minutes=30)


def test_observe_starts_and_clears_empty_timer():
    """有人进来就清空计时;读到 0 且还没计时才开始计时。"""
    instance = _instance()

    started = observe(instance, 0, NOW)
    assert started.empty_since == NOW

    joined = observe(_instance(ever_had_player=True, empty_since=NOW.isoformat()), 3, NOW)
    assert joined.ever_had_player is True
    assert joined.empty_since is None

    again = observe(_instance(ever_had_player=True), 0, NOW + timedelta(minutes=5))
    assert again.empty_since == NOW + timedelta(minutes=5)


def test_stats_is_a_plain_dataclass():
    """观测结果是最简单的两字段结构(便于序列化/断言)。"""
    assert Stats(True, NOW).ever_had_player is True


# --------------------------------------------------------------------- 巡检
def _instance(
    *,
    name: str = "test",
    created_at: str | None = None,
    ever_had_player: bool = False,
    empty_since: str | None = None,
    stopped_since: str | None = None,
    state: State = State.running,
) -> Instance:
    slug = slugify(name)
    return Instance(
        name=name,
        slug=slug,
        server_type="paper",
        mc_version="1.21",
        memory_mb=2048,
        online_mode=True,
        volume_name=f"mc-{slug}-data",
        state=state,
        created_at=created_at or NOW.isoformat(timespec="seconds"),
        container_id=f"fake-{slug}",
        ever_had_player=ever_had_player,
        empty_since=empty_since,
        stopped_since=stopped_since,
    )


@pytest.fixture()
def bench(tmp_path):
    """一套真实的 Engine + 假的 provisioner / 存档实现。"""
    config = Config(db_path=tmp_path / "mcctl.db")
    store = Store(config.db_path)
    store.init()
    provisioner = FakeProvisioner()
    archives = FakeArchiveStore(directory=tmp_path / "archives")
    return Engine(store, provisioner, config, archives=archives), store, provisioner, archives


def _seed(engine: Engine, provisioner: FakeProvisioner, instance: Instance) -> Instance:
    """把实例直接塞进 DB 与假 docker(FakeProvisioner),模拟"早就在跑"的实例。"""
    engine.store.add(instance)
    provisioner.volumes.add(instance.volume_name)
    provisioner.containers[engine.container_name(instance)] = FakeContainer(
        container_id=instance.container_id or "fake"
    )
    seeded = engine.store.get(instance.name)
    assert seeded is not None
    return seeded


def _reaper(engine: Engine, policy: LifecyclePolicy, now: datetime = NOW) -> Reaper:
    return Reaper(engine, policy, clock=lambda: now)


def test_reaper_reaps_and_archives_idle_instance(bench):
    """回收链路走通:关停 → 归档 → 删除,且存档进入 archives 表。"""
    engine, store, provisioner, archives = bench
    _seed(engine, provisioner, _instance(created_at=(NOW - timedelta(hours=30)).isoformat()))
    archives.files["mc-test-data"] = "world-data"

    report = _reaper(engine, LifecyclePolicy()).tick()

    assert [item.name for item in report.reaped] == ["test"]
    assert report.reaped[0].reason == REASON_MAX_LIFETIME
    assert engine.list_instances() == []
    saved = store.get_archive("test")
    assert saved is not None
    # 名字仍被存档占用:同名实例不能直接重建,只能 restore 或 purge
    assert "test" in store.all_slugs()


def test_reaper_leaves_a_joined_instance_alone(bench):
    """有人在玩的实例不被回收(即使创建很久,只要没超过 24 小时)。"""
    engine, _store, provisioner, _archives = bench
    _seed(
        engine,
        provisioner,
        _instance(created_at=(NOW - timedelta(hours=2)).isoformat(), ever_had_player=True),
    )
    provisioner.player_counts["test"] = 4

    report = _reaper(engine, LifecyclePolicy()).tick()

    assert report.reaped == []
    assert len(engine.list_instances()) == 1


def test_reaper_never_reaps_when_players_unreadable(bench):
    """读不到在线人数时:哪怕创建很久,前两条规则也不触发(只受最长存活约束)。"""
    engine, _store, provisioner, _archives = bench
    _seed(engine, provisioner, _instance(created_at=(NOW - timedelta(hours=5)).isoformat()))
    provisioner.player_counts["test"] = None

    report = _reaper(engine, LifecyclePolicy()).tick()

    assert report.reaped == []
    assert len(engine.list_instances()) == 1


def test_reaper_persists_observation(bench):
    """每轮巡检都把事实写回 DB:在线人数与"持续为 0"的起点。"""
    engine, store, provisioner, _archives = bench
    _seed(engine, provisioner, _instance())

    _reaper(engine, LifecyclePolicy()).tick()

    observed = store.get("test")
    assert observed is not None
    assert observed.last_players == 0
    assert observed.observed_at == NOW.isoformat(timespec="seconds")
    assert observed.empty_since == NOW.isoformat(timespec="seconds")


def test_reaper_idle_rule_fires_one_hour_after_empty_started(bench):
    """第一轮记下"开始空着"的时刻,1 小时后再跑才回收。

    这里用 ``ever_had_player=True`` 把规则 1 排除掉,才能看到规则 2 生效——
    否则"从没人加入"会先一步命中(规则 1 的判定更具体)。
    """
    engine, store, provisioner, _archives = bench
    _seed(engine, provisioner, _instance(ever_had_player=True))

    first = _reaper(engine, LifecyclePolicy()).tick()
    assert first.reaped == []
    assert store.get("test").empty_since == NOW.isoformat(timespec="seconds")

    later = _reaper(engine, LifecyclePolicy(), NOW + timedelta(minutes=60)).tick()
    assert [item.name for item in later.reaped] == ["test"]
    assert later.reaped[0].reason == REASON_IDLE


def test_reaper_prefers_rule1_over_rule2(bench):
    """两条规则同时满足时,"从没人加入"是更准确的原因。"""
    engine, _store, provisioner, _archives = bench
    _seed(
        engine,
        provisioner,
        _instance(created_at=(NOW - timedelta(minutes=90)).isoformat()),
    )

    report = _reaper(engine, LifecyclePolicy()).tick()

    assert report.reaped[0].reason == REASON_NO_JOIN


def test_reaper_reaps_an_instance_left_stopped_too_long(bench):
    """规则 4 端到端:停在 stopped 超过阈值 → 归档并删除。

    这里把 ``max_lifetime`` 关掉,否则 26 小时的创建时长会先一步命中规则 3,
    就看不到规则 4 了。同时把在线人数设成 ``None``(停着的容器就是读不到),
    用来盯住"规则 4 不能被 ``players != 0`` 那道闸门拦住"。
    """
    engine, store, provisioner, archives = bench
    archives.files["mc-test-data"] = "world-data"
    _seed(
        engine,
        provisioner,
        _instance(
            created_at=(NOW - timedelta(hours=26)).isoformat(),
            state=State.stopped,
            stopped_since=(NOW - timedelta(hours=25)).isoformat(),
        ),
    )
    provisioner.player_counts["test"] = None

    report = _reaper(engine, LifecyclePolicy(max_lifetime_minutes=0)).tick()

    assert [item.name for item in report.reaped] == ["test"]
    assert report.reaped[0].reason == REASON_STOPPED
    assert engine.list_instances() == []
    assert store.get_archive("test") is not None


def test_reaper_keeps_a_recently_stopped_instance(bench):
    """刚停下的实例不动——停在阈值以内时怎么都不回收。"""
    engine, _store, provisioner, _archives = bench
    _seed(
        engine,
        provisioner,
        _instance(
            created_at=(NOW - timedelta(hours=2)).isoformat(),
            state=State.stopped,
            stopped_since=(NOW - timedelta(minutes=30)).isoformat(),
        ),
    )
    provisioner.player_counts["test"] = None

    report = _reaper(engine, LifecyclePolicy()).tick()

    assert report.reaped == []
    assert len(engine.list_instances()) == 1


def test_reaper_purges_expired_archives(bench):
    """超过保留期的存档被清掉,服务器名随之释放。"""
    engine, store, provisioner, archives = bench
    from mcctl.core.archive import new_archive

    saved = archives.directory / "old.tar.gz"
    saved.write_text("old", encoding="utf-8")
    store.save_archive(
        new_archive(
            name="old",
            slug="old",
            path=saved,
            size_bytes=3,
            server_type="paper",
            mc_version="1.21",
            memory_mb=1024,
            online_mode=True,
            java=None,
            retention_minutes=60,
            created=NOW - timedelta(hours=2),
        )
    )

    report = _reaper(engine, LifecyclePolicy()).tick()

    assert report.purged == ["old"]
    assert store.get_archive("old") is None
    assert not saved.exists()


def test_reaper_keeps_archives_inside_retention(bench):
    """保留期内的存档不动。"""
    engine, store, _provisioner, archives = bench
    from mcctl.core.archive import new_archive

    saved = archives.directory / "fresh.tar.gz"
    saved.write_text("fresh", encoding="utf-8")
    store.save_archive(
        new_archive(
            name="fresh",
            slug="fresh",
            path=saved,
            size_bytes=5,
            server_type="paper",
            mc_version="1.21",
            memory_mb=1024,
            online_mode=True,
            java=None,
            retention_minutes=1440,
            created=NOW - timedelta(hours=1),
        )
    )

    report = _reaper(engine, LifecyclePolicy()).tick()

    assert report.purged == []
    assert store.get_archive("fresh") is not None


def test_reaper_isolates_per_instance_errors(bench):
    """单个实例处理失败不影响其他实例(错误被收集进 report.errors)。"""
    engine, _store, provisioner, _archives = bench
    _seed(engine, provisioner, _instance(name="boom"))
    _seed(
        engine,
        provisioner,
        _instance(name="ok", created_at=(NOW - timedelta(hours=30)).isoformat()),
    )

    def explode(name: str) -> int | None:
        if name == "boom":
            raise RuntimeError("probe exploded")
        return 0

    engine.probe_players = explode  # type: ignore[method-assign]

    report = _reaper(engine, LifecyclePolicy()).tick()

    assert [name for name, _message in report.errors] == ["boom"]
    assert [item.name for item in report.reaped] == ["ok"]
    assert report.changed is True


def test_reaper_does_nothing_when_disabled(bench):
    """关闭策略时连存档都不清理。"""
    engine, store, provisioner, archives = bench
    saved = archives.directory / "keep.tar.gz"
    saved.write_text("keep", encoding="utf-8")
    from mcctl.core.archive import new_archive

    store.save_archive(
        new_archive(
            name="keep",
            slug="keep",
            path=saved,
            size_bytes=4,
            server_type="paper",
            mc_version="1.21",
            memory_mb=1024,
            online_mode=True,
            java=None,
            retention_minutes=1,
            created=NOW - timedelta(days=30),
        )
    )

    report = _reaper(engine, LifecyclePolicy(enabled=False)).tick()

    assert report.purged == [] and report.reaped == []
    assert store.get_archive("keep") is not None


def test_reaper_archive_is_a_real_file(bench):
    """归档产物是磁盘上的真实文件(下载接口就是把它发出去)。"""
    engine, _store, provisioner, archives = bench
    _seed(engine, provisioner, _instance(created_at=(NOW - timedelta(hours=30)).isoformat()))
    archives.files["mc-test-data"] = "world"

    _reaper(engine, LifecyclePolicy()).tick()

    archive: Archive | None = engine.get_archive("test")
    assert archive is not None
    assert archive.path.read_text(encoding="utf-8") == "world"
