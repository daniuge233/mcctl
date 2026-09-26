"""运行时配置:配置文件 + 环境变量 + 内置默认值。

优先级(从高到低)::

    命令行 ``--config`` / 环境变量 ``MCCTL_*``  >  配置文件  >  内置默认值

配置文件用 TOML(标准库 ``tomllib``,不引入额外依赖)。默认位置是
``$MCCTL_HOME/config.toml``(即 ``~/.mcctl/config.toml``),也可以用环境变量
``MCCTL_CONFIG`` 或全局参数 ``--config <path>`` 指定别的路径。

字段清单与注释示例见 :meth:`Config.to_toml_sample`(即 ``mcctl config --write``
生成的内容),也可以在命令行执行 ``mcctl config`` 查看**生效值**。

示例配置本身是一份**真实的 TOML 文件**(``mcctl/core/config.template.toml``),
不再写死在 Python 字符串里;:func:`_render_template` 只负责把里面的占位符换成
当前生效值。改注释/改结构直接改那个文件即可。
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .lifecycle import LifecyclePolicy

#: 配置文件在 ``$MCCTL_HOME`` 下的默认文件名。
CONFIG_FILENAME = "config.toml"

#: 示例配置的模板文件名(与本模块同目录,随包分发)。
TEMPLATE_FILENAME = "config.template.toml"

_ENV_PREFIX = "MCCTL_"
_MEMORY_RE = re.compile(r"^\s*(\d+)\s*([a-zA-Z]*)\s*$")

# 配置文件里允许出现的键;写错名字会直接报错,而不是被静默忽略。
_TOP_LEVEL_KEYS = frozenset(
    {"domain", "db", "network", "server_image", "docker_socket", "archives", "archive_image"}
)
_SECTION_KEYS: dict[str, frozenset[str]] = {
    "router": frozenset({"name", "image", "bind", "port", "container_port", "socket_group"}),
    "health": frozenset({"timeout", "poll_interval"}),
    "defaults": frozenset({"type", "version", "memory", "online_mode", "ttl", "java"}),
    "api": frozenset({"key", "bind", "port", "docs"}),
    "lifecycle": frozenset(
        {"enabled", "interval", "join_grace", "idle", "max_lifetime", "stopped", "archive_retention"}
    ),
}

#: 存档目录的默认子目录名(相对于 DB 所在目录)。
ARCHIVES_DIRNAME = "archives"


class ConfigError(ValueError):
    """配置文件或环境变量不合法(继承 ``ValueError``,CLI 会转成友好提示)。"""


# --------------------------------------------------------------------- 解析原语
def _env_raw(name: str) -> str | None:
    """读取 ``MCCTL_<name>`` 环境变量;未设置或空串视为未设置。"""
    return os.environ.get(f"{_ENV_PREFIX}{name}") or None


def parse_memory(value: str | int) -> int:
    """把 ``2G`` / ``1024M`` / ``2048`` 解析为 MB。"""
    if isinstance(value, int) and not isinstance(value, bool):
        megabytes = value
    else:
        match = _MEMORY_RE.match(str(value))
        if not match:
            raise ConfigError(f"无法解析内存大小 {value!r}(示例:2G / 1024M)")
        amount = int(match.group(1))
        unit = match.group(2).lower()
        if unit in ("", "m", "mb"):
            megabytes = amount
        elif unit in ("g", "gb"):
            megabytes = amount * 1024
        else:
            raise ConfigError(f"不支持的内存单位 {unit!r}(支持 M / G)")
    if megabytes < 512:
        raise ConfigError("内存不能小于 512M")
    return megabytes


def _stringify(value: Any) -> str:
    """把 TOML 里的标量统一成字符串,便于和环境变量走同一条转换路径。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


_TRUE = frozenset({"1", "true", "yes", "y", "on"})
_FALSE = frozenset({"0", "false", "no", "n", "off"})


def _to_int(raw: str, key: str) -> int:
    try:
        return int(raw.strip())
    except ValueError:
        raise ConfigError(f"配置项 {key} 需要整数,当前为 {raw!r}") from None


def _to_optional_int(raw: str, key: str) -> int | None:
    return None if not raw.strip() else _to_int(raw, key)


def _to_float(raw: str, key: str) -> float:
    try:
        return float(raw.strip())
    except ValueError:
        raise ConfigError(f"配置项 {key} 需要数字,当前为 {raw!r}") from None


def _to_bool(raw: str, key: str) -> bool:
    lowered = raw.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ConfigError(f"配置项 {key} 需要布尔值(true/false),当前为 {raw!r}")


def _to_optional_str(raw: str, key: str) -> str | None:
    return raw.strip() or None


def _to_memory(raw: str, key: str) -> int:
    """解析内存大小,并把出错信息定位到具体配置项。"""
    try:
        return parse_memory(raw)
    except ConfigError as exc:
        raise ConfigError(f"配置项 {key} 不合法:{exc}") from None


def _validate_domain(value: str) -> str:
    """域名后缀只写后缀本身,不要混入端口或协议。"""
    cleaned = value.strip().strip(".")
    if not cleaned:
        raise ConfigError("配置项 domain 不能为空")
    if any(character in cleaned for character in "/: "):
        raise ConfigError(
            f"配置项 domain {value!r} 不合法:只写域名后缀(例如 mc.loc),"
            "端口请用 [router] port 设置"
        )
    return cleaned


def _validate_port(port: int, key: str) -> int:
    if not 1 <= port <= 65535:
        raise ConfigError(f"配置项 {key} 必须在 1..65535 之间,当前为 {port}")
    return port


def _toml_inner(value: str) -> str:
    """转义成 TOML 基本字符串的**内容**(不含外层引号,Windows 路径也能用)。

    模板里字符串都自带引号(``key = "{{name}}"``),所以这里返回的是引号里面的部分。
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _toml_str(value: str) -> str:
    """把字符串转成合法的 TOML 基本字符串(带外层引号)。"""
    return f'"{_toml_inner(value)}"'


# --------------------------------------------------------------------- 路径
def default_config_path() -> Path:
    """默认配置文件位置:``$MCCTL_HOME/config.toml``。"""
    home = Path(os.environ.get(f"{_ENV_PREFIX}HOME") or (Path.home() / ".mcctl"))
    return home / CONFIG_FILENAME


def resolve_config_path(explicit: str | Path | None = None) -> Path:
    """决定配置文件路径(不判断文件是否存在)。

    顺序:``explicit``(``--config``)> ``MCCTL_CONFIG`` > ``$MCCTL_HOME/config.toml``。
    """
    if explicit is not None:
        return Path(explicit).expanduser()
    from_env = _env_raw("CONFIG")
    if from_env is not None:
        return Path(from_env).expanduser()
    return default_config_path()


def _is_explicit(explicit: str | Path | None) -> bool:
    """是否由用户显式指定(显式指定但文件不存在时报错,而不是悄悄忽略)。"""
    return explicit is not None or _env_raw("CONFIG") is not None


# --------------------------------------------------------------------- 模型
@dataclass
class ServerDefaults:
    """``mcctl create`` 未显式传参时的默认值(配置文件 ``[defaults]``)。"""

    server_type: str = "paper"
    mc_version: str = "1.21"
    memory_mb: int = 2048
    online_mode: bool = True
    ttl_minutes: int | None = None
    java: str | None = None


def _resolve_db_path(config_path: Path, file_value: Any) -> Path:
    """SQLite 路径:``MCCTL_DB`` > 配置文件 ``db``(相对则相对配置目录)> 默认。

    ``MCCTL_HOME`` 只影响默认值(``<home>/mcctl.db``),与旧行为一致。
    """
    from_env = _env_raw("DB")
    if from_env is not None:
        return Path(from_env).expanduser()
    if file_value is not None:
        candidate = Path(str(file_value)).expanduser()
        return candidate if candidate.is_absolute() else config_path.parent / candidate
    home = Path(os.environ.get(f"{_ENV_PREFIX}HOME") or (Path.home() / ".mcctl"))
    return home / "mcctl.db"


def _resolve_archives_dir(config_path: Path, file_value: Any, db_path: Path) -> Path:
    """存档目录:``MCCTL_ARCHIVES`` > 配置文件 ``archives``(相对则相对配置目录)> ``<db 同目录>/archives``。"""
    from_env = _env_raw("ARCHIVES")
    if from_env is not None:
        return Path(from_env).expanduser()
    if file_value is not None:
        candidate = Path(str(file_value)).expanduser()
        return candidate if candidate.is_absolute() else config_path.parent / candidate
    return db_path.parent / ARCHIVES_DIRNAME


def _read_toml(path: Path) -> dict[str, Any]:
    """读取并解析 TOML 配置(容忍 Windows 编辑器写入的 UTF-8 BOM)。"""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件 {path}:{exc}") from None
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"配置文件 {path} 不是 UTF-8 编码:{exc}") from None
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"配置文件 {path} 不是合法 TOML:{exc}") from None


def _reject_unknown_keys(data: dict[str, Any], path: Path) -> None:
    """未知字段直接报错——写错键名静默失效是最难查的一类问题。"""
    unknown = sorted(set(data) - _TOP_LEVEL_KEYS - set(_SECTION_KEYS))
    if unknown:
        allowed = ", ".join(sorted(_TOP_LEVEL_KEYS) + [f"[{name}]" for name in sorted(_SECTION_KEYS)])
        raise ConfigError(f"配置文件 {path} 含未知字段:{', '.join(unknown)}(可用:{allowed})")
    for section, allowed_keys in _SECTION_KEYS.items():
        if section not in data:
            continue
        table = data[section]
        if not isinstance(table, dict):
            raise ConfigError(f"配置文件 {path} 的 [{section}] 必须是表(table)")
        extra = sorted(set(table) - allowed_keys)
        if extra:
            raise ConfigError(
                f"配置文件 {path} 的 [{section}] 含未知字段:{', '.join(extra)}"
                f"(可用:{', '.join(sorted(allowed_keys))})"
            )


# --------------------------------------------------------------------- 配置模板
_PLACEHOLDER_RE = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")

#: 模板里以此开头的行只给维护者看,渲染时整行丢弃(不会写进生成的配置文件)。
_TEMPLATE_ONLY_PREFIX = "#@template"


def template_path() -> Path:
    """示例配置模板的路径(与本模块同目录,随包分发)。"""
    return Path(__file__).with_name(TEMPLATE_FILENAME)


def _render_template(values: dict[str, str]) -> str:
    """把模板里的 ``{{name}}`` 换成实际值,并丢掉 ``#@template`` 维护者备注行。

    单趟替换:替换进去的内容不会被再扫描一遍,所以用户配置里就算含花括号也不会
    被当成占位符。模板与代码不同步(缺少模板文件 / 模板里有代码不认识的占位符)
    属于安装或开发问题,同样用 :class:`ConfigError` 报出来,CLI 会显示成友好提示。
    """
    path = template_path()
    try:
        template = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"读取配置模板 {path} 失败:{exc}") from None

    def substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            raise ConfigError(f"配置模板 {path} 含未知占位符 {{{{{name}}}}},请同步 config.py")
        return values[name]

    rendered = _PLACEHOLDER_RE.sub(substitute, template)
    lines = [
        line for line in rendered.splitlines() if not line.lstrip().startswith(_TEMPLATE_ONLY_PREFIX)
    ]
    return "\n".join(lines) + "\n"


@dataclass
class Config:
    """mcctl 的运行时参数集合。

    注意:实例容器**不映射任何宿主机端口**,因此这里没有实例的 host_port 概念;
    唯一对宿主机暴露的端口属于 mc-router(``router_bind`` + ``router_port``)。
    """

    db_path: Path
    network_name: str = "mc-net"
    router_name: str = "mc-router"
    router_image: str = "itzg/mc-router"
    # 唯一端口映射: 127.0.0.1:25565 -> router:25565
    router_bind: str = "127.0.0.1"
    router_port: int = 25565
    docker_socket: str = "/var/run/docker.sock"
    # mc-router 镜像默认以非 root 运行,读不到 docker.sock(官方 README
    # "User access to docker socket")。留空表示自动探测 socket 的属组 GID,
    # 探测不到时退回以 root 运行。
    router_socket_group: str | None = None
    server_image: str = "itzg/minecraft-server"
    # 实例容器内部监听端口(不映射到宿主机,由 router 在内网访问)
    server_port: int = 25565
    # 实例域名后缀,最终连接地址为 ``<slug>.<domain>:<router_port>``
    domain: str = "mc.loc"
    # 等健康超时:首次启动需要下载服务端并生成世界,给足时间
    health_timeout: float = 600.0
    health_poll_interval: float = 3.0
    # ---------------------------------------------------------- 存档(archive)
    #: 存档目录;``None`` 表示用 :meth:`archives_root` 推导出的默认位置
    archives_dir: Path | None = None
    #: 打包 / 解包数据卷用的 helper 镜像(需要带 ``tar``)
    archive_image: str = "alpine:3"
    # ---------------------------------------------------------- HTTP 服务
    #: API 密钥(必填才能启动 ``mcctl serve``);所有 HTTP 接口都靠它鉴权
    api_key: str = ""
    api_bind: str = "127.0.0.1"
    api_port: int = 8765
    #: 是否暴露 ``/docs`` 与 ``/openapi.json``(框架自带、不经过密钥校验,默认关)
    api_docs: bool = False
    # ---------------------------------------------------------- 生命周期
    #: 超时回收 + 存档保留策略(见 mcctl/core/lifecycle.py)
    lifecycle: LifecyclePolicy = field(default_factory=LifecyclePolicy)
    #: ``mcctl create`` 未显式传参时的默认值
    defaults: ServerDefaults = field(default_factory=ServerDefaults)
    #: 实际加载到的配置文件;``None`` 表示没有配置文件,值来自环境变量/内置默认值
    config_path: Path | None = None

    # -------------------------------------------------------------- 派生值
    def archives_root(self) -> Path:
        """存档目录的实际位置(未配置时取 ``<db 所在目录>/archives``)。"""
        return self.archives_dir or (self.db_path.parent / ARCHIVES_DIRNAME)

    def require_api_key(self) -> str:
        """取 API 密钥;未配置则报错(拒绝“裸奔”启动 HTTP 服务)。"""
        if not self.api_key:
            raise ConfigError(
                "尚未设置 API 密钥,HTTP 服务拒绝启动。\n"
                "请在配置文件的 [api] 段填入 key(可先执行 ``mcctl config --write`` 生成模板),"
                "或设置环境变量 MCCTL_API_KEY。"
            )
        return self.api_key

    def masked_api_key(self) -> str:
        """给 ``mcctl config`` 展示用的脱敏密钥。"""
        if not self.api_key:
            return "(未设置,HTTP 服务不可用)"
        if len(self.api_key) <= 8:
            return "*" * len(self.api_key)
        return f"{self.api_key[:4]}…{self.api_key[-4:]}(共 {len(self.api_key)} 字符)"

    # -------------------------------------------------------------- 加载
    @classmethod
    def load(cls, path: str | Path | None = None, *, allow_missing: bool = False) -> "Config":
        """加载配置:配置文件打底,环境变量覆盖。

        Args:
            path: ``--config`` 指定的配置文件路径;``None`` 表示走环境变量/默认位置。
            allow_missing: 显式指定的文件不存在时是否也放过(供 ``mcctl config --write``
                在尚未创建配置文件时算出当前生效值)。

        Raises:
            ConfigError: 文件不是合法 TOML、含未知字段/非法值,或显式指定的文件不存在。
        """
        config_path = resolve_config_path(path)
        if config_path.is_file():
            data = _read_toml(config_path)
            _reject_unknown_keys(data, config_path)
            loaded_path: Path | None = config_path
        elif _is_explicit(path) and not allow_missing:
            raise ConfigError(f"配置文件 {config_path} 不存在")
        else:
            data = {}
            loaded_path = None

        def entry(section: str | None, key: str) -> Any:
            """取配置文件里的值(``section=None`` 表示顶层)。"""
            if section is None:
                return data.get(key)
            table = data.get(section)
            return table.get(key) if isinstance(table, dict) else None

        def raw(env_name: str, section: str | None, key: str, default: str) -> str:
            """取字符串形态的原始值:环境变量 > 配置文件 > 默认值。"""
            from_env = _env_raw(env_name)
            if from_env is not None:
                return from_env
            file_value = entry(section, key)
            return default if file_value is None else _stringify(file_value)

        db_path = _resolve_db_path(config_path, entry(None, "db"))

        return cls(
            db_path=db_path,
            archives_dir=_resolve_archives_dir(config_path, entry(None, "archives"), db_path),
            archive_image=raw("ARCHIVE_IMAGE", None, "archive_image", "alpine:3") or "alpine:3",
            api_key=raw("API_KEY", "api", "key", "").strip(),
            api_bind=raw("API_BIND", "api", "bind", "127.0.0.1"),
            api_port=_validate_port(
                _to_int(raw("API_PORT", "api", "port", "8765"), "api.port"), "api.port"
            ),
            api_docs=_to_bool(raw("API_DOCS", "api", "docs", "false"), "api.docs"),
            lifecycle=LifecyclePolicy(
                enabled=_to_bool(
                    raw("LIFECYCLE_ENABLED", "lifecycle", "enabled", "true"), "lifecycle.enabled"
                ),
                interval_seconds=_to_float(
                    raw("LIFECYCLE_INTERVAL", "lifecycle", "interval", "30"),
                    "lifecycle.interval",
                ),
                join_grace_minutes=_to_int(
                    raw("JOIN_GRACE", "lifecycle", "join_grace", "10"), "lifecycle.join_grace"
                ),
                idle_minutes=_to_int(
                    raw("IDLE_TIMEOUT", "lifecycle", "idle", "60"), "lifecycle.idle"
                ),
                max_lifetime_minutes=_to_int(
                    raw("MAX_LIFETIME", "lifecycle", "max_lifetime", "1440"),
                    "lifecycle.max_lifetime",
                ),
                stopped_minutes=_to_int(
                    raw("STOPPED_TIMEOUT", "lifecycle", "stopped", "1440"),
                    "lifecycle.stopped",
                ),
                archive_retention_minutes=_to_int(
                    raw("ARCHIVE_RETENTION", "lifecycle", "archive_retention", "1440"),
                    "lifecycle.archive_retention",
                ),
            ),
            network_name=raw("NETWORK", None, "network", "mc-net"),
            server_image=raw("SERVER_IMAGE", None, "server_image", "itzg/minecraft-server"),
            docker_socket=raw("DOCKER_SOCKET", None, "docker_socket", "/var/run/docker.sock"),
            domain=_validate_domain(raw("DOMAIN", None, "domain", "mc.loc")),
            router_name=raw("ROUTER_NAME", "router", "name", "mc-router"),
            router_image=raw("ROUTER_IMAGE", "router", "image", "itzg/mc-router"),
            router_bind=raw("ROUTER_BIND", "router", "bind", "127.0.0.1"),
            router_port=_validate_port(
                _to_int(raw("ROUTER_PORT", "router", "port", "25565"), "router.port"), "router.port"
            ),
            server_port=_validate_port(
                _to_int(
                    raw("SERVER_PORT", "router", "container_port", "25565"), "router.container_port"
                ),
                "router.container_port",
            ),
            router_socket_group=_to_optional_str(
                raw("ROUTER_SOCKET_GROUP", "router", "socket_group", ""), "router.socket_group"
            ),
            health_timeout=_to_float(raw("HEALTH_TIMEOUT", "health", "timeout", "600"), "health.timeout"),
            health_poll_interval=_to_float(
                raw("HEALTH_POLL_INTERVAL", "health", "poll_interval", "3"), "health.poll_interval"
            ),
            defaults=ServerDefaults(
                server_type=raw("DEFAULT_TYPE", "defaults", "type", "paper"),
                mc_version=raw("DEFAULT_VERSION", "defaults", "version", "1.21"),
                memory_mb=_to_memory(raw("DEFAULT_MEMORY", "defaults", "memory", "2048"), "defaults.memory"),
                online_mode=_to_bool(
                    raw("DEFAULT_ONLINE_MODE", "defaults", "online_mode", "true"), "defaults.online_mode"
                ),
                ttl_minutes=_to_optional_int(raw("DEFAULT_TTL", "defaults", "ttl", ""), "defaults.ttl"),
                java=_to_optional_str(raw("DEFAULT_JAVA", "defaults", "java", ""), "defaults.java"),
            ),
            config_path=loaded_path,
        )

    @classmethod
    def from_env(cls) -> "Config":
        """``load()`` 的别名:保留旧调用方式,但环境变量优先于配置文件。"""
        return cls.load()

    # -------------------------------------------------------------- 配置模板
    def to_toml_sample(self) -> str:
        """按当前生效值渲染示例配置(模板见 ``config.template.toml``)。

        未设置的 ``ttl`` / ``java`` / ``api.key`` 会渲染成**注释行**,保证生成的
        文件始终是合法 TOML(写成 ``ttl = `` 是解析不了的)。
        """
        ttl_line = (
            f"ttl = {self.defaults.ttl_minutes}"
            if self.defaults.ttl_minutes is not None
            else "# ttl = 120"
        )
        java_line = (
            f"java = {_toml_str(self.defaults.java)}"
            if self.defaults.java is not None
            else '# java = "21"'
        )
        api_key_line = (
            f"key = {_toml_str(self.api_key)}" if self.api_key else '# key = "请填入一个只有你知道的密钥"'
        )
        return _render_template(
            {
                "domain": _toml_inner(self.domain),
                "network": _toml_inner(self.network_name),
                "server_image": _toml_inner(self.server_image),
                "docker_socket": _toml_inner(self.docker_socket),
                "db": _toml_inner(str(self.db_path).replace("\\", "/")),
                "archives": _toml_inner(str(self.archives_root()).replace("\\", "/")),
                "archive_image": _toml_inner(self.archive_image),
                "api_key_line": api_key_line,
                "api_bind": _toml_inner(self.api_bind),
                "api_port": str(self.api_port),
                "api_docs": "true" if self.api_docs else "false",
                "lifecycle_enabled": "true" if self.lifecycle.enabled else "false",
                "lifecycle_interval": f"{self.lifecycle.interval_seconds:g}",
                "lifecycle_join_grace": str(self.lifecycle.join_grace_minutes),
                "lifecycle_idle": str(self.lifecycle.idle_minutes),
                "lifecycle_max_lifetime": str(self.lifecycle.max_lifetime_minutes),
                "lifecycle_stopped": str(self.lifecycle.stopped_minutes),
                "lifecycle_archive_retention": str(self.lifecycle.archive_retention_minutes),
                "name": _toml_inner(self.router_name),
                "image": _toml_inner(self.router_image),
                "bind": _toml_inner(self.router_bind),
                "port": str(self.router_port),
                "container_port": str(self.server_port),
                "socket_group": _toml_inner(self.router_socket_group or ""),
                "timeout": f"{self.health_timeout:g}",
                "poll_interval": f"{self.health_poll_interval:g}",
                "type": _toml_inner(self.defaults.server_type),
                "version": _toml_inner(self.defaults.mc_version),
                "memory": _toml_inner(f"{self.defaults.memory_mb}M"),
                "online_mode": "true" if self.defaults.online_mode else "false",
                "ttl_line": ttl_line,
                "java_line": java_line,
            }
        )
