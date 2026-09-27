"""编排层逻辑测试(不需要 Docker)。"""

from __future__ import annotations

import pytest

from mcctl.core.archive import ArchiveError
from mcctl.core.config import Config
from mcctl.core.engine import ArchiveNotFound, Engine, EngineError, InstanceConflict, InstanceNotFound
from mcctl.core.models import (
    CreateRequest,
    State,
    normalize_command,
    validate_player_name,
)
from mcctl.core.store import Store
from mcctl.provisioners.base import ProvisionError
from tests.fakes import FakeArchiveStore, FakeProvisioner


@pytest.fixture()
def engine(tmp_path) -> tuple[Engine, FakeProvisioner]:
    """构造一个绑定临时数据库与假 provisioner 的 Engine。"""
    config = Config(db_path=tmp_path / "mcctl.db")
    store = Store(config.db_path)
    store.init()
    provisioner = FakeProvisioner()
    return Engine(store, provisioner, config), provisioner


def test_create_runs_through_all_steps(engine):
    """create 完成后实例为 running,且 volume / 容器都已就位。"""
    eng, prov = engine
    instance = eng.create(CreateRequest(name="test"))

    assert instance.state is State.running
    assert instance.slug == "test"
    assert instance.container_id == "fake-1"
    assert prov.runtime_ready is True
    assert "mc-test-data" in prov.volumes
    assert "mc-test" in prov.containers
    assert instance.volume_name == "mc-test-data"


def test_create_is_resumable_after_health_failure(engine):
    """健康检查失败后标记 failed;再次 create 能续跑并复用已有资源。"""
    eng, prov = engine
    prov.fail_health_times = 1

    with pytest.raises(Exception):
        eng.create(CreateRequest(name="test"))

    failed = eng.get("test")
    assert failed is not None and failed.state is State.failed
    # 容器在健康失败前已创建,续跑应复用它而不是新建
    assert "mc-test" in prov.containers

    resumed = eng.create(CreateRequest(name="test"))
    assert resumed.state is State.running
    assert resumed.slug == "test"
    assert len(prov.containers) == 1


def test_create_rejects_duplicate_name(engine):
    """已存在的活跃实例不能被重复创建。"""
    eng, _ = engine
    eng.create(CreateRequest(name="test"))
    with pytest.raises(EngineError):
        eng.create(CreateRequest(name="test"))


def test_slug_collision_gets_suffix(engine):
    """不同名称解析到同一 slug 时自动追加后缀。"""
    eng, _ = engine
    first = eng.create(CreateRequest(name="my server"))
    second = eng.create(CreateRequest(name="my  server"))
    assert first.slug == "my-server"
    assert second.slug == "my-server-2"


def test_create_passes_java_choice_to_provisioner(engine):
    """--java 版本号既落到 DB,也以官方 tag 形式传给 runtime 层。"""
    eng, prov = engine
    instance = eng.create(CreateRequest(name="test", java="21"))

    assert instance.java == "21"
    assert prov.specs[-1].java is not None
    assert prov.specs[-1].java.tag == "java21"


def test_create_rejects_invalid_java_before_writing(engine, tmp_path):
    """非法 --java 在写 DB 之前就报错,不留下半成品记录。"""
    eng, _ = engine
    with pytest.raises(ValueError):
        eng.create(CreateRequest(name="test", java=str(tmp_path / "no-such-jdk")))
    assert eng.get("test") is None


def test_reconcile_marks_failed_when_container_removed(engine):
    """容器被手删后,reconcile 应把 DB 标记为 failed。"""
    eng, prov = engine
    eng.create(CreateRequest(name="test"))

    # 模拟 `docker rm -f mc-test`
    prov.containers.pop("mc-test")

    drifts = eng.reconcile()
    assert len(drifts) == 1
    assert drifts[0].expected is State.running
    assert drifts[0].actual is State.destroyed
    assert drifts[0].resolved is State.failed
    assert eng.get("test").state is State.failed


def test_reconcile_syncs_container_stopped_out_of_band(engine):
    """容器被 docker stop 后,reconcile 把 DB 同步为 stopped。"""
    eng, prov = engine
    eng.create(CreateRequest(name="test"))
    prov.containers["mc-test"].status = "exited"

    drifts = eng.reconcile()
    assert [d.resolved for d in drifts] == [State.stopped]
    assert eng.get("test").state is State.stopped


def test_reconcile_noop_when_consistent(engine):
    """状态一致时不产生漂移。"""
    eng, _ = engine
    eng.create(CreateRequest(name="test"))
    assert eng.reconcile() == []


def test_stop_and_start_roundtrip(engine):
    """stop / start 往返后状态正确。"""
    eng, _ = engine
    eng.create(CreateRequest(name="test"))

    assert eng.stop("test").state is State.stopped
    assert eng.start("test").state is State.running


def test_destroy_cleans_container_and_volume(engine):
    """rm 后容器与数据卷都被清掉,记录标记为 destroyed。"""
    eng, prov = engine
    eng.create(CreateRequest(name="test"))

    eng.destroy("test")

    assert "mc-test" not in prov.containers
    assert "mc-test-data" not in prov.volumes
    assert eng.get("test").state is State.destroyed
    # destroyed 记录不再出现在列表中,但同名可以重新创建
    assert eng.list_instances() == []
    assert eng.create(CreateRequest(name="test")).state is State.running


def test_destroy_is_idempotent_for_failed_instance(engine):
    """容器已被删的 failed 实例也能安全 rm。"""
    eng, prov = engine
    eng.create(CreateRequest(name="test"))
    prov.containers.pop("mc-test")
    eng.reconcile()

    eng.destroy("test")
    assert eng.get("test").state is State.destroyed


def test_endpoint_format(engine):
    """连接地址形如 <slug>.mc.loc:25565。"""
    eng, _ = engine
    inst = eng.create(CreateRequest(name="test"))
    assert eng.endpoint_of(inst) == "test.mc.loc:25565"


def test_online_mode_roundtrip(engine):
    """online_mode 会写入数据库并可读回。"""
    eng, _ = engine
    inst = eng.create(CreateRequest(name="test", online_mode=False))
    assert inst.online_mode is False
    assert eng.get("test").online_mode is False


# ---------------------------------------------------------------- 游戏指令
def test_send_command_strips_leading_slash(engine):
    """``/say hi`` 与 ``say hi`` 等价(带斜杠会被去掉后原样转发)。"""
    eng, prov = engine
    eng.create(CreateRequest(name="test"))
    prov.command_outputs["test"] = "hi"

    assert eng.send_command("test", "/say hi") == "hi"
    assert prov.commands == [("test", "say hi")]


def test_send_command_sends_raw_arguments(engine):
    """指令整体作为一个参数传给 rcon-cli(空格原样保留)。"""
    eng, prov = engine
    eng.create(CreateRequest(name="test"))
    eng.send_command("test", "give Steve diamond 64")
    assert prov.commands == [("test", "give Steve diamond 64")]


@pytest.mark.parametrize("command", ["", "   ", "/", "say\nhi", "say hi\nsay bye"])
def test_send_command_rejects_blank_or_multiline(engine, command):
    """空指令 / 纯空白 / 只有斜杠 / 含换行一律拒绝,且不会真的发出去。"""
    eng, prov = engine
    eng.create(CreateRequest(name="test"))
    with pytest.raises(ValueError):
        eng.send_command("test", command)
    assert prov.commands == []


def test_send_command_trims_surrounding_whitespace(engine):
    """首尾空白(含换行)会被去掉,不会因此被误判成多行命令。"""
    eng, prov = engine
    eng.create(CreateRequest(name="test"))
    eng.send_command("test", "  say hi \n")
    assert prov.commands == [("test", "say hi")]


def test_send_command_rejects_overlong_command(engine):
    """超长指令直接拒绝(避免误把整段文件当成指令发进去)。"""
    eng, prov = engine
    eng.create(CreateRequest(name="test"))
    with pytest.raises(ValueError, match="过长"):
        eng.send_command("test", "say " + "x" * 1000)
    assert prov.commands == []


def test_send_command_unknown_instance(engine):
    """实例不存在时报 InstanceNotFound,不会碰到 provisioner。"""
    eng, prov = engine
    with pytest.raises(InstanceNotFound):
        eng.send_command("nope", "say hi")
    assert prov.commands == []


def test_send_command_propagates_provision_error(engine):
    """runtime 层报错(容器没在跑 / RCON 连不上)要原样冒泡。"""
    eng, prov = engine
    eng.create(CreateRequest(name="test"))
    prov.fail_command = True
    with pytest.raises(ProvisionError):
        eng.send_command("test", "say hi")


def test_op_sends_op_command(engine):
    """op 会拼出 ``op <player>`` 这条指令。"""
    eng, prov = engine
    eng.create(CreateRequest(name="test"))
    prov.command_outputs["test"] = "Made Steve a server operator"

    assert eng.op("test", "Steve") == "Made Steve a server operator"
    assert prov.commands == [("test", "op Steve")]


def test_deop_sends_deop_command(engine):
    """deop 会拼出 ``deop <player>`` 这条指令。"""
    eng, prov = engine
    eng.create(CreateRequest(name="test"))
    eng.deop("test", "Steve")
    assert prov.commands == [("test", "deop Steve")]


@pytest.mark.parametrize("player", ["", " ", "bad name", "a" * 17, "玩家", "steve;"])
def test_op_rejects_invalid_player_name(engine, player):
    """非法玩家名在拼指令之前就被拦住(防注入 / 防打错)。"""
    eng, prov = engine
    eng.create(CreateRequest(name="test"))
    with pytest.raises(ValueError):
        eng.op("test", player)
    assert prov.commands == []


def test_command_helpers_are_idempotent():
    """纯函数:斜杠 / 空白只去掉一层,合法玩家名原样返回。"""
    assert normalize_command("  /say hi  ") == "say hi"
    assert normalize_command("say hi") == "say hi"
    assert normalize_command("//weird") == "/weird"
    assert validate_player_name("  Notch_1  ") == "Notch_1"


# ---------------------------------------------------------------- 归档与重建
@pytest.fixture()
def arch_engine(tmp_path):
    """带假存档实现的 Engine(存档真的落到临时目录里)。

    ``FakeArchiveStore.files`` 里没有的卷被视为"不存在",所以默认情况下
    create 出来的实例是**没有**可归档内容的;需要归档时先往 ``files`` 里塞内容。
    """
    config = Config(db_path=tmp_path / "mcctl.db")
    store = Store(config.db_path)
    store.init()
    provisioner = FakeProvisioner()
    archives = FakeArchiveStore(directory=tmp_path / "archives")
    return Engine(store, provisioner, config, archives=archives), provisioner, archives


def _with_data(eng, prov, archives, name="test"):
    """创建一个实例,并让它"数据卷里有世界数据"。"""
    inst = eng.create(CreateRequest(name=name))
    archives.files[inst.volume_name] = "world-data"
    return inst


def test_destroy_archives_before_deleting(arch_engine):
    """rm 会先生成存档:文件在磁盘上,元数据在 archives 表里。"""
    eng, prov, archives = arch_engine
    inst = _with_data(eng, prov, archives)

    saved = eng.destroy("test")

    assert saved is not None
    assert saved.path.is_file()
    assert saved.path.read_text(encoding="utf-8") == "world-data"
    assert eng.get_archive("test") is not None
    # 容器与数据卷确实被清了
    assert "mc-test" not in prov.containers
    assert inst.volume_name not in prov.volumes


def test_archived_name_stays_reserved(arch_engine):
    """删除后名字仍被存档占用:同名创建被拒,提示走 restore / purge。"""
    eng, prov, archives = arch_engine
    _with_data(eng, prov, archives)
    eng.destroy("test")

    with pytest.raises(InstanceConflict) as excinfo:
        eng.create(CreateRequest(name="test"))

    assert "restore" in str(excinfo.value)
    assert eng.get_archive("test") is not None


def test_destroy_without_volume_records_no_archive(arch_engine):
    """数据卷本来就不存在时不产生存档,名字立刻可复用。"""
    eng, _prov, _archives = arch_engine
    eng.create(CreateRequest(name="test"))

    assert eng.destroy("test") is None
    assert eng.get_archive("test") is None
    assert eng.create(CreateRequest(name="test")).state is State.running


def test_destroy_without_archiving(arch_engine):
    """--no-archive:直接删掉,不留下任何存档。"""
    eng, prov, archives = arch_engine
    _with_data(eng, prov, archives)

    assert eng.destroy("test", archive=False) is None
    assert eng.get_archive("test") is None
    assert "mc-test-data" not in prov.volumes


def test_archive_failure_aborts_deletion(arch_engine):
    """归档失败就不删:宁可留一个坏实例,也不要把存档弄丢。"""
    eng, prov, archives = arch_engine
    _with_data(eng, prov, archives)
    archives.fail_create = True

    with pytest.raises(ArchiveError):
        eng.destroy("test")

    assert "mc-test" in prov.containers
    assert "mc-test-data" in prov.volumes
    assert eng.get("test").state is State.failed


def test_restore_rebuilds_from_archive(arch_engine):
    """用存档重建:数据原样回来,沿用原 slug 与数据卷名,存档被消费掉。"""
    eng, prov, archives = arch_engine
    _with_data(eng, prov, archives)
    eng.destroy("test")

    restored = eng.restore("test")

    assert restored.state is State.running
    assert restored.slug == "test"
    assert restored.volume_name == "mc-test-data"
    assert archives.files["mc-test-data"] == "world-data"
    assert eng.get_archive("test") is None
    assert len(eng.list_instances()) == 1


def test_restore_inherits_archive_metadata(arch_engine):
    """重建时沿用存档里记录的原始配置(不用再把参数抄一遍)。"""
    eng, prov, archives = arch_engine
    inst = eng.create(
        CreateRequest(name="test", server_type="fabric", mc_version="1.20.4", memory_mb=4096, online_mode=False, java="21")
    )
    archives.files[inst.volume_name] = "world-data"
    eng.destroy("test")

    restored = eng.restore("test")

    assert restored.server_type == "fabric"
    assert restored.mc_version == "1.20.4"
    assert restored.memory_mb == 4096
    assert restored.online_mode is False
    assert restored.java == "21"


def test_restore_allows_overriding_metadata(arch_engine):
    """重建时可以覆盖部分字段,其余仍按存档。"""
    eng, prov, archives = arch_engine
    _with_data(eng, prov, archives)
    eng.destroy("test")

    restored = eng.restore("test", memory_mb=8192, online_mode=False)

    assert restored.memory_mb == 8192
    assert restored.online_mode is False
    assert restored.server_type == "paper"


def test_restore_requires_an_archive(arch_engine):
    """没有存档时重建报 ArchiveNotFound。"""
    eng, _prov, _archives = arch_engine

    with pytest.raises(ArchiveNotFound):
        eng.restore("nope")


def test_purge_archive_frees_the_name(arch_engine):
    """丢弃存档后,名字可以重新使用,存档文件也被删掉。"""
    eng, prov, archives = arch_engine
    _with_data(eng, prov, archives)
    saved = eng.destroy("test")
    assert saved is not None

    eng.purge_archive("test")

    assert eng.get_archive("test") is None
    assert not saved.path.exists()
    assert eng.create(CreateRequest(name="test")).state is State.running


def test_purge_archive_drops_the_placeholder_row(arch_engine):
    """存档丢弃后,占位的 destroyed 记录也要删掉(slug 才算真的释放)。"""
    eng, prov, archives = arch_engine
    _with_data(eng, prov, archives)
    eng.destroy("test")
    assert "test" in eng.store.all_slugs()

    eng.purge_archive("test")

    assert eng.store.get("test") is None
    assert "test" not in eng.store.all_slugs()
    # slug 真的空了:同名重建能拿回原来的 slug,而不是被顶成 test-2
    assert eng.create(CreateRequest(name="test")).slug == "test"


def test_purge_archive_keeps_a_live_instance(arch_engine):
    """给活实例单独打包出来的存档被丢弃时,实例本身不能跟着消失。"""
    eng, prov, archives = arch_engine
    _with_data(eng, prov, archives)
    eng.pack_archive("test")

    eng.purge_archive("test")

    assert eng.get("test").state is State.running
    assert eng.get_archive("test") is None


def test_pack_archive_keeps_instance_alive(arch_engine):
    """单独打包不停止、也不删除实例(用于在线备份/下载)。"""
    eng, prov, archives = arch_engine
    _with_data(eng, prov, archives)

    saved = eng.pack_archive("test")

    assert saved.path.is_file()
    assert eng.get("test").state is State.running
    assert "mc-test-data" in prov.volumes


def test_ensure_archive_works_after_deletion(arch_engine):
    """实例删除后,ensure_archive 仍能按名字拿到那份存档(下载照常提供)。"""
    eng, prov, archives = arch_engine
    _with_data(eng, prov, archives)
    eng.destroy("test")

    archive = eng.ensure_archive("test")

    assert archive.name == "test"
    assert archive.path.is_file()


def test_ensure_archive_packs_live_instance(arch_engine):
    """实例还活着且没有存档时,ensure_archive 现场打包。"""
    eng, prov, archives = arch_engine
    _with_data(eng, prov, archives)

    archive = eng.ensure_archive("test")

    assert archive.path.is_file()
    assert eng.get("test").state is State.running


def test_ensure_archive_reports_missing_everything(arch_engine):
    """既没实例也没存档 → InstanceNotFound。"""
    eng, _prov, _archives = arch_engine

    with pytest.raises(InstanceNotFound):
        eng.ensure_archive("nope")


def test_archive_of_reports_missing_archive(arch_engine):
    """按名字取存档,没有则 ArchiveNotFound。"""
    eng, _prov, _archives = arch_engine

    with pytest.raises(ArchiveNotFound):
        eng.archive_of("nope")


def test_archived_slug_is_not_reallocated(arch_engine):
    """名字被占用时,别的名字也不能抢走它的 slug(数据卷名要稳定)。"""
    eng, prov, archives = arch_engine
    _with_data(eng, prov, archives, name="my server")
    eng.destroy("my server")

    other = eng.create(CreateRequest(name="my  server"))

    assert other.slug != "my-server"


def test_destroy_is_idempotent_with_archive(arch_engine):
    """重复 rm 不会生成第二份存档,直接返回已有那份。"""
    eng, prov, archives = arch_engine
    _with_data(eng, prov, archives)
    first = eng.destroy("test")

    second = eng.destroy("test")

    assert second is not None and first is not None
    assert second.path == first.path
    assert len(eng.list_archives()) == 1
