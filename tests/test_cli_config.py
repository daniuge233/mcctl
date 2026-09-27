"""CLI 层:配置文件 → create 默认值 → provisioner 的贯通测试(不需要 Docker)。

``create`` 的连接参数在真实环境里要等容器起来才能验证,所以这里把
``_build_engine`` 换成内存 provisioner,直接断言传给 runtime 层的 Spec。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import mcctl.cli as cli
from mcctl.core.config import Config
from mcctl.core.engine import Engine
from mcctl.core.store import Store
from tests.fakes import FakeProvisioner


@pytest.fixture()
def cli_env(tmp_path, monkeypatch):
    """把 CLI 的 engine 换成"配置来自 tmp 文件 + 假 provisioner"的组合。"""

    def build(config_text: str):
        config_path = tmp_path / "config.toml"
        config_path.write_text(config_text, encoding="utf-8")
        store = Store(tmp_path / "mcctl.db")
        store.init()
        provisioner = FakeProvisioner()
        engine = Engine(store, provisioner, Config.load(config_path))
        monkeypatch.setattr(cli, "_build_engine", lambda: engine)
        return CliRunner(), provisioner, engine

    return build


CONFIG = """
domain = "mc.test.loc"

[router]
port = 25999

[defaults]
type = "vanilla"
version = "1.20.4"
memory = "3G"
online_mode = false
ttl = 90
java = "17"
"""


def test_create_uses_config_file_defaults(cli_env):
    """不传任何参数时,create 完全按配置文件里的 defaults 建实例。"""
    runner, provisioner, _ = cli_env(CONFIG)

    result = runner.invoke(cli.app, ["create", "demo"])

    assert result.exit_code == 0, result.output
    spec = provisioner.specs[-1]
    assert spec.server_type == "vanilla"
    assert spec.mc_version == "1.20.4"
    assert spec.memory_mb == 3072
    assert spec.online_mode is False
    assert spec.java is not None
    assert spec.host == "demo.mc.test.loc"


def test_create_arguments_beat_config_file(cli_env):
    """命令行参数优先于配置文件里的 defaults。"""
    runner, provisioner, _ = cli_env(CONFIG)

    result = runner.invoke(
        cli.app,
        ["create", "demo", "--type", "paper", "--version", "1.19.4", "--memory", "1G", "--online-mode"],
    )

    assert result.exit_code == 0, result.output
    spec = provisioner.specs[-1]
    assert spec.server_type == "paper"
    assert spec.mc_version == "1.19.4"
    assert spec.memory_mb == 1024
    assert spec.online_mode is True


def test_config_file_ttl_is_applied(cli_env):
    """配置文件里的 ttl 会写进实例记录。"""
    runner, _, engine = cli_env(CONFIG)

    result = runner.invoke(cli.app, ["create", "demo"])

    assert result.exit_code == 0, result.output
    instance = engine.get("demo")
    assert instance.ttl_minutes == 90


def test_endpoint_uses_config_domain_and_port(cli_env):
    """endpoint 展示的域名与端口来自配置文件。"""
    runner, _, engine = cli_env(CONFIG)
    runner.invoke(cli.app, ["create", "demo"])

    result = runner.invoke(cli.app, ["endpoint", "demo"])

    assert result.exit_code == 0, result.output
    assert "demo.mc.test.loc:25999" in result.output


def test_config_write_creates_file_and_respects_force(cli_env, tmp_path):
    """config --write 生成文件;重复写入需要 --force。"""
    runner, _, _ = cli_env("")
    target = tmp_path / "generated.toml"

    first = runner.invoke(cli.app, ["--config", str(target), "config", "--write"])

    assert first.exit_code == 0, first.output
    assert target.is_file()

    second = runner.invoke(cli.app, ["--config", str(target), "config", "--write"])

    assert second.exit_code == 1
    assert "--force" in second.output

    third = runner.invoke(cli.app, ["--config", str(target), "config", "--write", "--force"])

    assert third.exit_code == 0, third.output


def test_config_command_reports_file_path(cli_env):
    """config 会显示实际加载到的配置文件。"""
    runner, _, _ = cli_env(CONFIG)
    config_path = Path(runner.invoke(cli.app, ["config", "--path"]).output.strip())

    assert config_path.name == "config.toml"


def test_broken_config_file_gives_friendly_error(cli_env, tmp_path):
    """配置文件写错时给中文提示,而不是抛 traceback。"""
    runner, _, _ = cli_env("")
    bad = tmp_path / "bad.toml"
    bad.write_text("domian = 'typo'\n", encoding="utf-8")

    result = runner.invoke(cli.app, ["--config", str(bad), "config"])

    assert result.exit_code == 1
    assert "domian" in result.output


def test_missing_explicit_config_file_gives_friendly_error(cli_env, tmp_path):
    """显式指定的配置文件不存在时报错,而不是悄悄用默认值。"""
    runner, _, _ = cli_env("")

    result = runner.invoke(cli.app, ["--config", str(tmp_path / "ghost.toml"), "config"])

    assert result.exit_code == 1
    assert "不存在" in result.output


# ------------------------------------------------------------------ 游戏指令
def test_cmd_sends_game_command(cli_env):
    """``mcctl cmd`` 把游戏指令转发给服务端并打印响应。"""
    runner, provisioner, _ = cli_env(CONFIG)
    runner.invoke(cli.app, ["create", "demo"])
    provisioner.command_outputs["demo"] = "Broadcasted: hi"

    result = runner.invoke(cli.app, ["cmd", "demo", "say", "hi"])

    assert result.exit_code == 0, result.output
    assert "Broadcasted: hi" in result.output
    assert provisioner.commands == [("demo", "say hi")]


def test_cmd_accepts_leading_slash(cli_env):
    """带 / 的写法也接受(内部去掉斜杠,RCON 通道不需要)。"""
    runner, provisioner, _ = cli_env(CONFIG)
    runner.invoke(cli.app, ["create", "demo"])

    result = runner.invoke(cli.app, ["cmd", "demo", "/op", "Steve"])

    assert result.exit_code == 0, result.output
    assert provisioner.commands == [("demo", "op Steve")]


def test_cmd_reports_empty_command(cli_env):
    """空指令 → 友好报错(退出码 1),不会发出去。"""
    runner, provisioner, _ = cli_env(CONFIG)
    runner.invoke(cli.app, ["create", "demo"])

    result = runner.invoke(cli.app, ["cmd", "demo", "   "])

    assert result.exit_code == 1
    assert "命令不能为空" in result.output
    assert provisioner.commands == []


def test_cmd_unknown_instance_gives_friendly_error(cli_env):
    """实例不存在 → 退出码 1 + 中文提示。"""
    runner, _, _ = cli_env(CONFIG)

    result = runner.invoke(cli.app, ["cmd", "ghost", "say", "hi"])

    assert result.exit_code == 1
    assert "不存在" in result.output


def test_op_sends_op_command(cli_env):
    """``mcctl op`` 等价于发送 ``op <player>``。"""
    runner, provisioner, _ = cli_env(CONFIG)
    runner.invoke(cli.app, ["create", "demo"])
    provisioner.command_outputs["demo"] = "Made Steve a server operator"

    result = runner.invoke(cli.app, ["op", "demo", "Steve"])

    assert result.exit_code == 0, result.output
    assert "Made Steve a server operator" in result.output
    assert provisioner.commands == [("demo", "op Steve")]


def test_deop_sends_deop_command(cli_env):
    """``mcctl deop`` 等价于发送 ``deop <player>``。"""
    runner, provisioner, _ = cli_env(CONFIG)
    runner.invoke(cli.app, ["create", "demo"])

    result = runner.invoke(cli.app, ["deop", "demo", "Steve"])

    assert result.exit_code == 0, result.output
    assert provisioner.commands == [("demo", "deop Steve")]


def test_op_rejects_invalid_player(cli_env):
    """非法玩家名 → 退出码 1,不会发出去。"""
    runner, provisioner, _ = cli_env(CONFIG)
    runner.invoke(cli.app, ["create", "demo"])

    result = runner.invoke(cli.app, ["op", "demo", "bad name"])

    assert result.exit_code == 1
    assert "玩家名" in result.output
    assert provisioner.commands == []


def test_op_maps_rcon_failure_to_friendly_error(cli_env):
    """RCON 不可用 → 退出码 1 + 中文提示。"""
    runner, provisioner, _ = cli_env(CONFIG)
    runner.invoke(cli.app, ["create", "demo"])
    provisioner.fail_command = True

    result = runner.invoke(cli.app, ["op", "demo", "Steve"])

    assert result.exit_code == 1
    assert "错误" in result.output

