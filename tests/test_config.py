"""配置文件 / 环境变量 / 内置默认值 的优先级与校验测试(不需要 Docker)。"""

from __future__ import annotations

from pathlib import Path

import pytest

from mcctl.core import config as config_module
from mcctl.core.config import (
    Config,
    ConfigError,
    ServerDefaults,
    default_config_path,
    parse_memory,
    resolve_config_path,
    template_path,
)


def write(tmp_path, text: str, name: str = "config.toml"):
    """把文本写成配置文件并返回路径。"""
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------- 默认值 / 环境变量


def test_builtin_defaults_without_config_file():
    """没有任何配置文件时,取内置默认值,且 config_path 为 None。"""
    config = Config.load()

    assert config.config_path is None
    assert config.domain == "mc.loc"
    assert config.router_port == 25565
    assert config.router_bind == "127.0.0.1"
    assert config.network_name == "mc-net"
    assert config.defaults.server_type == "paper"
    assert config.defaults.memory_mb == 2048
    assert config.defaults.online_mode is True
    assert config.defaults.ttl_minutes is None
    assert config.defaults.java is None


def test_missing_default_path_is_not_an_error(tmp_path, monkeypatch):
    """默认位置没有配置文件时静默使用内置默认值。"""
    monkeypatch.setenv("MCCTL_HOME", str(tmp_path / "empty-home"))

    config = Config.load()

    assert config.config_path is None
    assert config.domain == "mc.loc"


def test_env_beats_config_file(tmp_path, monkeypatch):
    """环境变量 > 配置文件 > 内置默认值。"""
    path = write(
        tmp_path,
        """
domain = "from-file.loc"

[router]
port = 25999

[defaults]
memory = "3G"
""",
    )
    monkeypatch.setenv("MCCTL_DOMAIN", "from-env.loc")
    monkeypatch.setenv("MCCTL_DEFAULT_MEMORY", "1G")

    config = Config.load(path)

    assert config.domain == "from-env.loc"  # 环境变量赢
    assert config.router_port == 25999  # 文件赢过内置默认值
    assert config.defaults.memory_mb == 1024


def test_env_only_without_file(monkeypatch):
    """没有配置文件时环境变量依然生效。"""
    monkeypatch.setenv("MCCTL_ROUTER_PORT", "25888")
    monkeypatch.setenv("MCCTL_DEFAULT_VERSION", "1.20.1")

    config = Config.load()

    assert config.router_port == 25888
    assert config.defaults.mc_version == "1.20.1"


# ------------------------------------------------------------------- 配置文件


def test_config_file_overrides(tmp_path):
    """配置文件覆盖域名、router 端口/绑定、创建默认值。"""
    path = write(
        tmp_path,
        """
domain = "mc.home.arpa"
server_image = "itzg/minecraft-server:java21"

[router]
bind = "0.0.0.0"
port = 25599
container_port = 25566

[health]
timeout = 120
poll_interval = 1.5

[defaults]
type = "vanilla"
version = "1.20.4"
memory = "3G"
online_mode = false
ttl = 120
java = "17"
""",
    )

    config = Config.load(path)

    assert config.config_path == path
    assert config.domain == "mc.home.arpa"
    assert config.server_image == "itzg/minecraft-server:java21"
    assert config.router_bind == "0.0.0.0"
    assert config.router_port == 25599
    assert config.server_port == 25566
    assert config.health_timeout == 120
    assert config.health_poll_interval == 1.5
    assert config.defaults.server_type == "vanilla"
    assert config.defaults.mc_version == "1.20.4"
    assert config.defaults.memory_mb == 3072
    assert config.defaults.online_mode is False
    assert config.defaults.ttl_minutes == 120
    assert config.defaults.java == "17"


def test_config_from_env_var_path(tmp_path, monkeypatch):
    """MCCTL_CONFIG 指定配置文件。"""
    path = write(tmp_path, 'domain = "via-env.loc"\n')
    monkeypatch.setenv("MCCTL_CONFIG", str(path))

    assert Config.load().domain == "via-env.loc"


def test_relative_db_resolves_against_config_dir(tmp_path):
    """配置文件里的相对 db 路径相对该文件所在目录解析。"""
    path = write(tmp_path, 'db = "data/mcctl.db"\n')

    config = Config.load(path)

    assert config.db_path == tmp_path / "data" / "mcctl.db"


def test_absolute_db_stays_absolute(tmp_path):
    """绝对 db 路径原样使用。"""
    target = tmp_path / "elsewhere" / "mcctl.db"
    path = write(tmp_path, f"db = '{target}'\n")  # TOML 字面量字符串,不转义反斜杠

    assert Config.load(path).db_path == target


def test_env_db_beats_config_file(tmp_path, monkeypatch):
    """MCCTL_DB 优先于配置文件里的 db。"""
    path = write(tmp_path, 'db = "data/mcctl.db"\n')
    monkeypatch.setenv("MCCTL_DB", str(tmp_path / "env.db"))

    assert Config.load(path).db_path == tmp_path / "env.db"


# ------------------------------------------------------------------- 错误处理


def test_explicit_missing_file_raises(tmp_path):
    """显式指定的配置文件不存在时报错。"""
    with pytest.raises(ConfigError, match="不存在"):
        Config.load(tmp_path / "nope.toml")


def test_env_var_path_missing_file_raises(tmp_path, monkeypatch):
    """MCCTL_CONFIG 指向不存在的文件同样报错。"""
    monkeypatch.setenv("MCCTL_CONFIG", str(tmp_path / "nope.toml"))

    with pytest.raises(ConfigError, match="不存在"):
        Config.load()


def test_allow_missing_for_write(tmp_path):
    """allow_missing=True 时,显式路径不存在也不报错(供 config --write 用)。"""
    config = Config.load(tmp_path / "brand-new.toml", allow_missing=True)

    assert config.config_path is None
    assert config.domain == "mc.loc"


def test_unknown_top_level_key_raises(tmp_path):
    """未知的顶层字段直接报错,避免拼错字段名却毫无反应。"""
    path = write(tmp_path, 'domian = "typo.loc"\n')

    with pytest.raises(ConfigError, match="未知字段:domian"):
        Config.load(path)


def test_unknown_section_key_raises(tmp_path):
    """未知的段内字段报错,并列出可用字段。"""
    path = write(tmp_path, "[router]\nprt = 25599\n")

    with pytest.raises(ConfigError, match="未知字段:prt"):
        Config.load(path)


def test_unknown_section_raises(tmp_path):
    """未知的段名报错。"""
    path = write(tmp_path, "[routr]\nport = 25599\n")

    with pytest.raises(ConfigError, match="未知字段:routr"):
        Config.load(path)


def test_malformed_toml_raises(tmp_path):
    """TOML 语法错误报错并保留原始信息。"""
    path = write(tmp_path, "domain =\n")

    with pytest.raises(ConfigError, match="不是合法 TOML"):
        Config.load(path)


def test_bom_is_tolerated(tmp_path):
    """Windows 编辑器常写入 UTF-8 BOM,不应因此报错。"""
    path = tmp_path / "bom.toml"
    path.write_bytes('domain = "bom.loc"\n'.encode("utf-8-sig"))

    assert Config.load(path).domain == "bom.loc"


@pytest.mark.parametrize("value", ["mc.loc:25565", "mc.loc/foo", "a b.loc"])
def test_invalid_domain_raises(tmp_path, value):
    """域名里带端口/斜杠/空格时给出可操作的提示。"""
    path = write(tmp_path, f'domain = "{value}"\n')

    with pytest.raises(ConfigError) as excinfo:
        Config.load(path)

    assert "[router] port" in str(excinfo.value)


def test_empty_domain_raises(tmp_path):
    """域名不能为空。"""
    path = write(tmp_path, 'domain = ""\n')

    with pytest.raises(ConfigError, match="domain 不能为空"):
        Config.load(path)


@pytest.mark.parametrize("value", ["0", "65536", "-1"])
def test_invalid_router_port_raises(tmp_path, value):
    """端口越界报错。"""
    path = write(tmp_path, f"[router]\nport = {value}\n")

    with pytest.raises(ConfigError, match="port"):
        Config.load(path)


def test_invalid_bool_raises(tmp_path):
    """online_mode 只能是布尔值。"""
    path = write(tmp_path, '[defaults]\nonline_mode = "maybe"\n')

    with pytest.raises(ConfigError, match="online_mode"):
        Config.load(path)


def test_invalid_memory_raises(tmp_path):
    """内存格式非法时报错,并把出错位置定位到配置项。"""
    path = write(tmp_path, '[defaults]\nmemory = "lots"\n')

    with pytest.raises(ConfigError, match="defaults.memory"):
        Config.load(path)


def test_too_small_memory_raises(tmp_path):
    """内存下限 512M。"""
    path = write(tmp_path, '[defaults]\nmemory = "100M"\n')

    with pytest.raises(ConfigError, match="512M"):
        Config.load(path)


# ------------------------------------------------------------------ 工具函数


@pytest.mark.parametrize(
    ("value", "expected"),
    [(2048, 2048), ("2048", 2048), ("2G", 2048), ("2g", 2048), ("512M", 512), (" 1G ", 1024)],
)
def test_parse_memory(value, expected):
    """内存写法:纯数字按 MB,支持 M/G 后缀。"""
    assert parse_memory(value) == expected


@pytest.mark.parametrize("value", ["nope", "0M", "100M"])
def test_parse_memory_rejects(value):
    """非法或过小的内存值报错(下限 512M)。"""
    with pytest.raises(ConfigError):
        parse_memory(value)


def test_default_config_path_honours_home(tmp_path, monkeypatch):
    """默认配置位置是 $MCCTL_HOME/config.toml。"""
    monkeypatch.setenv("MCCTL_HOME", str(tmp_path / "home"))

    assert default_config_path() == tmp_path / "home" / "config.toml"


def test_resolve_config_path_precedence(tmp_path, monkeypatch):
    """显式参数 > MCCTL_CONFIG > 默认位置。"""
    explicit = tmp_path / "explicit.toml"
    from_env = tmp_path / "env.toml"
    monkeypatch.setenv("MCCTL_CONFIG", str(from_env))

    assert resolve_config_path(explicit) == explicit
    assert resolve_config_path(None) == from_env


# ------------------------------------------------------------------ 示例配置


def test_toml_sample_round_trips(tmp_path):
    """--write 生成的示例配置必须能被自己读回来,且字段一致。"""
    original = Config.load()
    path = write(tmp_path, original.to_toml_sample(), name="sample.toml")

    reloaded = Config.load(path)

    assert reloaded.domain == original.domain
    assert reloaded.network_name == original.network_name
    assert reloaded.server_image == original.server_image
    assert reloaded.docker_socket == original.docker_socket
    assert reloaded.router_name == original.router_name
    assert reloaded.router_image == original.router_image
    assert reloaded.router_bind == original.router_bind
    assert reloaded.router_port == original.router_port
    assert reloaded.server_port == original.server_port
    assert reloaded.health_timeout == original.health_timeout
    assert reloaded.health_poll_interval == original.health_poll_interval
    assert reloaded.defaults == original.defaults


def test_toml_sample_round_trips_with_everything_set(tmp_path):
    """ttl / java / socket_group 都设置时也要能原样读回。"""
    path = write(
        tmp_path,
        """
[router]
socket_group = 999

[defaults]
ttl = 30
java = "21"
""",
    )
    original = Config.load(path)
    path2 = write(tmp_path, original.to_toml_sample(), name="sample2.toml")

    reloaded = Config.load(path2)

    assert reloaded.router_socket_group == "999"  # GID 统一以字符串保存
    assert reloaded.defaults.ttl_minutes == 30
    assert reloaded.defaults.java == "21"


def test_toml_sample_escapes_windows_paths(tmp_path):
    """示例配置里的 Windows 路径要转义,保证是合法 TOML 且能原样读回。"""
    original = Config(db_path=tmp_path / "sub" / "mcctl.db")

    path = write(tmp_path, original.to_toml_sample(), name="escaped.toml")

    assert Config.load(path).db_path == original.db_path


# ------------------------------------------------- 示例配置模板(独立 TOML 文件)

#: 模板里允许出现的全部占位符。代码侧改动映射时必须同步这里。
_EXPECTED_PLACEHOLDERS = {
    "domain",
    "network",
    "server_image",
    "docker_socket",
    "db",
    "archives",
    "archive_image",
    "name",
    "image",
    "bind",
    "port",
    "container_port",
    "socket_group",
    "timeout",
    "poll_interval",
    "api_key_line",
    "api_bind",
    "api_port",
    "api_docs",
    "lifecycle_enabled",
    "lifecycle_join_grace",
    "lifecycle_idle",
    "lifecycle_max_lifetime",
    "lifecycle_stopped",
    "lifecycle_archive_retention",
    "lifecycle_interval",
    "type",
    "version",
    "memory",
    "online_mode",
    "ttl_line",
    "java_line",
}


def test_template_file_ships_beside_the_module():
    """示例配置来自真实文件(而不是 Python 字符串),并且随包分发。"""
    path = template_path()

    assert path.is_file()
    assert path.name == "config.template.toml"
    assert path.parent == Path(config_module.__file__).parent


def test_template_placeholders_match_expected_set():
    """模板占位符集合与代码约定一致(新增/删除字段时必须同时改两边)。"""
    found = set(
        config_module._PLACEHOLDER_RE.findall(template_path().read_text(encoding="utf-8"))
    )

    assert found == _EXPECTED_PLACEHOLDERS


def test_template_placeholders_appear_exactly_once():
    """每个占位符只出现一次——防止注释里手写占位符被意外替换掉。"""
    names = config_module._PLACEHOLDER_RE.findall(template_path().read_text(encoding="utf-8"))

    duplicated = sorted({n for n in names if names.count(n) > 1})
    assert duplicated == []


def test_toml_sample_has_no_leftover_placeholder():
    """渲染完不能残留花括号,否则写出来的配置就是坏的。"""
    sample = Config.load().to_toml_sample()

    assert "{{" not in sample
    assert "}}" not in sample
    assert config_module._PLACEHOLDER_RE.findall(sample) == []


def test_toml_sample_keeps_template_comments_and_committed_lines():
    """模板里的注释要原样带出来;未设置的 ttl / java 写成注释行。"""
    sample = Config.load().to_toml_sample()
    lines = sample.splitlines()

    assert "# 实例域名后缀:连接地址为 <slug>.<domain>:<router.port>" in lines
    assert 'socket_group = ""   # 留空 = 自动 stat docker.sock 取属组' in lines
    assert any(line.startswith("# ttl = 120") for line in lines)
    assert any(line.startswith('# java = "21"') for line in lines)


def test_toml_sample_uses_template_when_values_set(tmp_path):
    """设置过的 ttl / java 渲染成生效行,而不是注释行。"""
    config = Config(
        db_path=tmp_path / "mcctl.db",
        defaults=ServerDefaults(ttl_minutes=45, java="java21-jdk"),
    )

    lines = config.to_toml_sample().splitlines()

    assert any(line.startswith("ttl = 45") for line in lines)
    assert any(line.startswith('java = "java21-jdk"') for line in lines)
    assert not any(line.startswith("# ttl") for line in lines)
    assert not any(line.startswith("# java") for line in lines)


def test_toml_sample_drops_template_only_notes():
    """模板里给维护者看的 ``#@template`` 备注不能写进用户的配置文件。"""
    template = template_path().read_text(encoding="utf-8")
    sample = Config.load().to_toml_sample()

    assert config_module._TEMPLATE_ONLY_PREFIX in template  # 机制确实被用到
    assert "#@template" not in sample


def test_toml_sample_reports_unknown_placeholder(monkeypatch, tmp_path):
    """模板与代码不同步时给友好错误,而不是静默写出坏文件。"""
    broken = tmp_path / "broken.toml"
    broken.write_text('domain = "{{domain}}"\nnew_key = "{{not_in_code}}"\n', encoding="utf-8")
    monkeypatch.setattr(config_module, "template_path", lambda: broken)

    with pytest.raises(ConfigError, match="not_in_code"):
        Config.load().to_toml_sample()


def test_toml_sample_reports_missing_template(monkeypatch, tmp_path):
    """模板文件丢失(打包漏了/被删了)时给友好错误。"""
    monkeypatch.setattr(config_module, "template_path", lambda: tmp_path / "nope.toml")

    with pytest.raises(ConfigError, match="配置模板"):
        Config.load().to_toml_sample()


# --------------------------------------------------- HTTP 服务 / 存档 / 生命周期
def test_api_and_lifecycle_defaults():
    """没写配置文件时的 [api] / [lifecycle] / archives 默认值。"""
    config = Config.load()

    assert config.api_key == ""
    assert config.api_bind == "127.0.0.1"
    assert config.api_port == 8765
    assert config.api_docs is False
    assert config.archive_image == "alpine:3"
    # archives 未显式配置时会被推导成 <db 所在目录>/archives
    assert config.archives_dir == config.db_path.parent / "archives"
    assert config.archives_root() == config.archives_dir
    assert config.lifecycle.enabled is True
    assert config.lifecycle.interval_seconds == 30.0
    assert config.lifecycle.join_grace_minutes == 10
    assert config.lifecycle.idle_minutes == 60
    assert config.lifecycle.max_lifetime_minutes == 1440
    assert config.lifecycle.stopped_minutes == 1440
    assert config.lifecycle.archive_retention_minutes == 1440


def test_api_and_lifecycle_from_file(tmp_path):
    """[api] / [lifecycle] / archives 都能从配置文件读出来。"""
    path = write(
        tmp_path,
        """
archives = "./keep"
archive_image = "busybox:1.36"

[api]
key = "from-file"
bind = "0.0.0.0"
port = 9000
docs = true

[lifecycle]
enabled = false
interval = 5
join_grace = 0
idle = 30
max_lifetime = 0
stopped = 0
archive_retention = 120
""",
    )

    config = Config.load(path)

    assert config.api_key == "from-file"
    assert config.api_bind == "0.0.0.0"
    assert config.api_port == 9000
    assert config.api_docs is True
    assert config.archive_image == "busybox:1.36"
    assert config.archives_dir == tmp_path / "keep"
    assert config.lifecycle.describe() == {
        "enabled": False,
        "interval_seconds": 5.0,
        "join_grace_minutes": 0,
        "idle_minutes": 30,
        "max_lifetime_minutes": 0,
        "stopped_minutes": 0,
        "archive_retention_minutes": 120,
    }


def test_api_and_lifecycle_env_overrides_file(tmp_path, monkeypatch):
    """环境变量压过配置文件(命令行 > 环境变量 > 配置文件 > 默认值)。"""
    path = write(
        tmp_path,
        """
archives = "./keep"
archive_image = "busybox:1.36"

[api]
key = "from-file"
port = 9000
docs = false

[lifecycle]
enabled = true
join_grace = 10
""",
    )
    monkeypatch.setenv("MCCTL_API_KEY", "from-env")
    monkeypatch.setenv("MCCTL_API_BIND", "127.0.0.2")
    monkeypatch.setenv("MCCTL_API_PORT", "9999")
    monkeypatch.setenv("MCCTL_API_DOCS", "true")
    monkeypatch.setenv("MCCTL_ARCHIVES", str(tmp_path / "env-archives"))
    monkeypatch.setenv("MCCTL_ARCHIVE_IMAGE", "alpine:3.20")
    monkeypatch.setenv("MCCTL_LIFECYCLE_ENABLED", "false")
    monkeypatch.setenv("MCCTL_LIFECYCLE_INTERVAL", "2.5")
    monkeypatch.setenv("MCCTL_JOIN_GRACE", "1")
    monkeypatch.setenv("MCCTL_IDLE_TIMEOUT", "2")
    monkeypatch.setenv("MCCTL_MAX_LIFETIME", "3")
    monkeypatch.setenv("MCCTL_STOPPED_TIMEOUT", "5")
    monkeypatch.setenv("MCCTL_ARCHIVE_RETENTION", "4")

    config = Config.load(path)

    assert config.api_key == "from-env"
    assert config.api_bind == "127.0.0.2"
    assert config.api_port == 9999
    assert config.api_docs is True
    assert config.archives_dir == tmp_path / "env-archives"
    assert config.archive_image == "alpine:3.20"
    assert config.lifecycle.enabled is False
    assert config.lifecycle.interval_seconds == 2.5
    assert (config.lifecycle.join_grace_minutes, config.lifecycle.idle_minutes) == (1, 2)
    assert (
        config.lifecycle.max_lifetime_minutes,
        config.lifecycle.stopped_minutes,
        config.lifecycle.archive_retention_minutes,
    ) == (3, 5, 4)


@pytest.mark.parametrize("value", ["maybe", "", "真"])
def test_invalid_api_docs_raises(tmp_path, value):
    """[api] docs 只接受布尔值。"""
    path = write(tmp_path, f'[api]\ndocs = "{value}"\n')

    with pytest.raises(ConfigError, match="api.docs"):
        Config.load(path)


@pytest.mark.parametrize(("value", "expected"), [("yes", True), ("off", False), ("1", True)])
def test_api_docs_accepts_common_truthy_forms(tmp_path, value, expected):
    """布尔写法沿用它处的宽松约定(true/yes/on/1 …)。"""
    path = write(tmp_path, f'[api]\ndocs = "{value}"\n')

    assert Config.load(path).api_docs is expected


def test_invalid_lifecycle_interval_raises(tmp_path):
    """[lifecycle] interval 必须是数字。"""
    path = write(tmp_path, '[lifecycle]\ninterval = "一会儿"\n')

    with pytest.raises(ConfigError, match="lifecycle.interval"):
        Config.load(path)


def test_archives_root_defaults_next_to_db(tmp_path, monkeypatch):
    """没配 archives 时,存档目录默认落在 DB 旁边的 archives/。"""
    monkeypatch.setenv("MCCTL_HOME", str(tmp_path))

    config = Config.load()

    assert config.archives_root() == tmp_path / "archives"


def test_require_api_key_rejects_empty():
    """没设密钥时拒绝启动 HTTP 服务,并提示怎么设置。"""
    config = Config.load()

    with pytest.raises(ConfigError) as excinfo:
        config.require_api_key()

    assert "MCCTL_API_KEY" in str(excinfo.value)


def test_require_api_key_returns_configured_value(tmp_path):
    """设了密钥就直接返回它。"""
    path = write(tmp_path, '[api]\nkey = "hunter2"\n')

    assert Config.load(path).require_api_key() == "hunter2"


def test_masked_api_key_hides_the_middle(tmp_path):
    """``mcctl config`` 展示的密钥要脱敏,不能把原文打出来。"""
    assert "(未设置" in Config.load().masked_api_key()

    short = write(tmp_path, '[api]\nkey = "hunter2"\n', name="short.toml")
    assert Config.load(short).masked_api_key() == "*******"

    long = write(tmp_path, '[api]\nkey = "abcdefghijklmnop"\n', name="long.toml")
    masked = Config.load(long).masked_api_key()
    assert masked.startswith("abcd")
    assert masked.endswith("(共 16 字符)")
    assert "ijkl" not in masked


def test_sample_contains_api_and_lifecycle_sections(tmp_path, monkeypatch):
    """示例配置里要有 [api] / [lifecycle] 段,密钥未设置时渲染成注释行。"""
    monkeypatch.setenv("MCCTL_HOME", str(tmp_path / "empty-home"))

    sample = Config.load().to_toml_sample()

    assert "[api]" in sample
    assert "[lifecycle]" in sample
    assert "# key = " in sample
    assert "docs = false" in sample
    assert "join_grace = 10" in sample
    assert "archive_retention = 1440" in sample
    # 生成的样例必须还是合法 TOML
    written = tmp_path / "sample.toml"
    written.write_text(sample, encoding="utf-8")
    assert Config.load(written).api_docs is False


def test_sample_renders_configured_api_key(tmp_path):
    """已经配好密钥时,样例里写成真正的 key 行(不是注释)。"""
    path = write(tmp_path, '[api]\nkey = "s3cret"\ndocs = true\n')

    sample = Config.load(path).to_toml_sample()

    assert 'key = "s3cret"' in sample
    assert "docs = true" in sample
