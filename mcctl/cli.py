"""mcctl 命令行入口(typer)。

命令一览::

    mcctl config                                # 查看生效配置(config --write 生成示例文件)
    mcctl init                                  # 初始化 mc-net + router
    mcctl create <name> --type paper --version 1.21 --memory 2G [--java 21] [--ttl 60]
    mcctl list
    mcctl start|stop|rm <name>
    mcctl logs <name>
    mcctl endpoint <name>                       # 本版: <slug>.mc.loc:25565
    mcctl reconcile                             # 对比 DB 与 docker,修正漂移
    mcctl players <name>                        # 通过 rcon-cli 查在线人数
    mcctl cmd <name> <command...>               # 向游戏服务端发送指令(如 op Steve)
    mcctl op|deop <name> <player>               # 设置 / 撤销玩家 OP
    mcctl archive list|download|purge <name>     # 存档:列出 / 下载 / 丢弃
    mcctl restore <name>                        # 用存档重建服务器
    mcctl reap                                  # 手动跑一轮生命周期回收(适合 cron)
    mcctl serve                                 # 启动持久 HTTP 服务(FastAPI)

全局参数 ``--config <path>`` 指定配置文件,``-v/--verbose`` 打开调试日志。
"""

from __future__ import annotations

import logging
import re
import shutil
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, NoReturn, Optional

import typer
from docker.errors import DockerException

from .core.archive import Archive
from .core.config import Config, ConfigError, parse_memory, resolve_config_path
from .core.engine import Engine, EngineError
from .core.lifecycle import Reaper
from .core.models import CreateRequest, Instance
from .core.store import Store
from .provisioners.base import ProvisionError
from .provisioners.docker_archive import DockerArchiveStore
from .provisioners.docker_p import DockerProvisioner

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="管理一组临时的 Docker Minecraft 服务器实例。",
)

#: 存档子命令组(``mcctl archive ...``)
archive_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="存档操作:列出 / 下载 / 丢弃。删除实例时存档会自动保留 24 小时。",
)
app.add_typer(archive_app, name="archive")

#: 全局 ``--config`` 指定的配置文件路径(在 ``main`` 回调里赋值)
_config_path: Optional[str] = None


# --------------------------------------------------------------------- 基础设施
def _configure_logging(verbose: bool) -> None:
    """配置结构化日志输出(``event=xxx key=value`` 形式)。"""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )


@app.callback()
def main(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="输出调试日志"),
    config: Optional[Path] = typer.Option(
        None,
        "--config",
        "-c",
        help="配置文件路径(默认 $MCCTL_HOME/config.toml,也可用 MCCTL_CONFIG 指定)",
    ),
) -> None:
    """mcctl:本地 Minecraft 临时服务器集群管理器。"""
    global _config_path
    _config_path = str(config) if config is not None else None
    _configure_logging(verbose)


def _load_config(allow_missing: bool = False) -> Config:
    """加载生效配置(``--config`` > ``MCCTL_CONFIG`` > ``$MCCTL_HOME/config.toml``)。"""
    return Config.load(_config_path, allow_missing=allow_missing)


def _build_engine(config: Optional[Config] = None) -> Engine:
    """按生效配置构造 Engine(并确保数据库已建表)。"""
    config = config or _load_config()
    store = Store(config.db_path)
    store.init()
    return Engine(
        store,
        DockerProvisioner(config),
        config,
        archives=DockerArchiveStore(config),
    )


def _abort(message: str) -> NoReturn:
    """打印错误并以退出码 1 结束。"""
    typer.secho(f"错误: {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


@contextmanager
def _errors() -> Iterator[None]:
    """把已知异常转换为友好的 CLI 错误。"""
    try:
        yield
    except (EngineError, ProvisionError, ValueError) as exc:
        _abort(str(exc))
    except DockerException as exc:
        _abort(f"无法访问 Docker daemon,请确认本机 docker 正在运行:{exc}")


def _java_label(value: Optional[str]) -> str:
    """list 表格里的 JAVA 列:版本号原样显示,长路径压缩成 ``jdk:<目录名>``。"""
    if not value:
        return "-"
    if "/" not in value and "\\" not in value:
        return value
    parts = [part for part in re.split(r"[\\/]+", value.strip()) if part]
    if len(parts) > 1 and parts[-1].lower() == "bin":
        parts.pop()  # 指向 bin 目录时展示 JDK 根目录名
    return f"jdk:{parts[-1]}" if parts else "jdk"


def _fmt_time(value: str | datetime) -> str:
    """把时间格式化为本地 ``YYYY-MM-DD HH:MM``。"""
    if isinstance(value, datetime):
        return value.astimezone().strftime("%Y-%m-%d %H:%M")
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value


def _minutes_label(minutes: int) -> str:
    """生命周期里的分钟数:``0`` 表示该规则关闭。"""
    return f"{minutes} 分钟" if minutes else "(不启用)"


def _human_size(size: int) -> str:
    """字节数转成易读形式(存档体积大小差别很大,用单位更直观)。"""
    if size < 1024:
        return f"{size} B"
    value = float(size)
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1024
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
    return f"{size} B"  # pragma: no cover - 上面的循环总会返回


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    """打印一个简单的等宽表格。"""
    if not rows:
        typer.echo("(没有实例)")
        return
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    typer.echo("  ".join(header.ljust(widths[i]) for i, header in enumerate(headers)))
    typer.echo("  ".join("-" * width for width in widths))
    for row in rows:
        typer.echo("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))


def _echo_endpoint(engine: Engine, instance: Instance) -> None:
    """打印连接地址与 hosts 提示。"""
    endpoint = engine.endpoint_of(instance)
    hostname = engine.hostname(instance)
    typer.echo(f"连接地址: {endpoint}")
    typer.echo(f"提示: 在 MC 客户端中填入以上地址;若域名未解析,请在本机 hosts 加入:")
    typer.echo(f"      127.0.0.1 {hostname}")


# --------------------------------------------------------------------- 命令
@app.command()
def config(
    write: bool = typer.Option(
        False, "--write", help="把带注释的示例配置写入配置文件(已存在则报错,除非 --force)"
    ),
    force: bool = typer.Option(False, "--force", help="配合 --write 覆盖已存在的文件"),
    path_only: bool = typer.Option(False, "--path", help="只打印配置文件路径"),
) -> None:
    """查看生效配置,或生成示例配置文件。"""
    config_path = resolve_config_path(_config_path)

    if path_only:
        typer.echo(str(config_path))
        return

    with _errors():
        effective = _load_config(allow_missing=write)
        if write:
            if config_path.exists() and not force:
                raise ConfigError(f"配置文件 {config_path} 已存在(用 --force 覆盖)")
            try:
                config_path.parent.mkdir(parents=True, exist_ok=True)
                config_path.write_text(effective.to_toml_sample(), encoding="utf-8")
            except OSError as exc:
                raise ConfigError(f"无法写入配置文件 {config_path}:{exc}") from None

    if write:
        typer.secho(f"已写入示例配置:{config_path}", fg=typer.colors.GREEN)
        return

    loaded = effective.config_path
    if loaded is not None:
        typer.echo(f"配置文件: {loaded}")
    else:
        typer.echo(f"配置文件: {config_path}(不存在,当前值全部来自环境变量/内置默认值)")
    typer.echo("优先级:命令行 --config / 环境变量 MCCTL_*  >  配置文件  >  内置默认值")
    typer.echo()
    _print_table(
        ["字段", "生效值"],
        [
            ["domain", effective.domain],
            ["db", str(effective.db_path)],
            ["network", effective.network_name],
            ["server_image", effective.server_image],
            ["docker_socket", effective.docker_socket],
            ["archives", str(effective.archives_root())],
            ["archive_image", effective.archive_image],
            ["router.name", effective.router_name],
            ["router.image", effective.router_image],
            ["router.bind", effective.router_bind],
            ["router.port", str(effective.router_port)],
            ["router.container_port", str(effective.server_port)],
            ["router.socket_group", effective.router_socket_group or "(自动探测)"],
            ["health.timeout", f"{effective.health_timeout:g}"],
            ["health.poll_interval", f"{effective.health_poll_interval:g}"],
            ["api.key", effective.masked_api_key()],
            ["api.bind", effective.api_bind],
            ["api.port", str(effective.api_port)],
            ["api.docs", "true" if effective.api_docs else "false"],
            ["lifecycle.enabled", "true" if effective.lifecycle.enabled else "false"],
            ["lifecycle.interval", f"{effective.lifecycle.interval_seconds:g}s"],
            ["lifecycle.join_grace", _minutes_label(effective.lifecycle.join_grace_minutes)],
            ["lifecycle.idle", _minutes_label(effective.lifecycle.idle_minutes)],
            ["lifecycle.max_lifetime", _minutes_label(effective.lifecycle.max_lifetime_minutes)],
            ["lifecycle.stopped", _minutes_label(effective.lifecycle.stopped_minutes)],
            [
                "lifecycle.archive_retention",
                _minutes_label(effective.lifecycle.archive_retention_minutes),
            ],
            ["defaults.type", effective.defaults.server_type],
            ["defaults.version", effective.defaults.mc_version],
            ["defaults.memory", f"{effective.defaults.memory_mb}M"],
            ["defaults.online_mode", "true" if effective.defaults.online_mode else "false"],
            [
                "defaults.ttl",
                str(effective.defaults.ttl_minutes) if effective.defaults.ttl_minutes else "(不设置)",
            ],
            ["defaults.java", effective.defaults.java or "(镜像自带)"],
        ],
    )
    typer.echo()
    typer.echo(f"提示:用 `mcctl config --write` 把当前生效值写成示例配置({config_path})。")


@app.command()
def init() -> None:
    """初始化 runtime:创建 mc-net 网络与 mc-router 容器。"""
    engine = _build_engine()
    with _errors():
        engine.init_runtime()
    typer.secho("mc-net 与 mc-router 已就绪。", fg=typer.colors.GREEN)


@app.command()
def create(
    name: str = typer.Argument(..., help="实例名称,例如 test"),
    server_type: Optional[str] = typer.Option(
        None,
        "--type",
        "-t",
        help="服务端类型:paper / vanilla / fabric / forge ...(默认取配置的 defaults.type)",
    ),
    mc_version: Optional[str] = typer.Option(
        None, "--version", "-V", help="Minecraft 版本,例如 1.21(默认取配置的 defaults.version)"
    ),
    memory: Optional[str] = typer.Option(
        None, "--memory", "-m", help="内存上限,例如 2G / 1024M(默认取配置的 defaults.memory)"
    ),
    java: Optional[str] = typer.Option(
        None,
        "--java",
        help="Java 版本(如 21,或 itzg tag 如 java21-jdk),或宿主机 JDK 的 bin 目录路径",
    ),
    ttl: Optional[int] = typer.Option(
        None, "--ttl", help="存活时间(分钟),本版仅记录不自动回收(默认取配置的 defaults.ttl)"
    ),
    online_mode: Optional[bool] = typer.Option(
        None,
        "--online-mode/--offline",
        help="是否启用正版验证(默认取配置的 defaults.online_mode)",
    ),
) -> None:
    """创建一个实例并等待其就绪(未指定的参数取配置文件的 defaults 段)。"""
    engine = _build_engine()
    fallback = engine.config.defaults
    with _errors():
        request = CreateRequest(
            name=name,
            server_type=server_type or fallback.server_type,
            mc_version=mc_version or fallback.mc_version,
            memory_mb=parse_memory(memory) if memory else fallback.memory_mb,
            online_mode=fallback.online_mode if online_mode is None else online_mode,
            ttl_minutes=fallback.ttl_minutes if ttl is None else ttl,
            java=fallback.java if java is None else java,
        )
        instance = engine.create(request)
    typer.secho(f"实例 {instance.name} 已就绪(state={instance.state.value})。", fg=typer.colors.GREEN)
    _echo_endpoint(engine, instance)


@app.command("list")
def list_instances() -> None:
    """列出全部实例。"""
    engine = _build_engine()
    with _errors():
        instances = engine.list_instances()
    rows = [
        [
            instance.name,
            instance.slug,
            instance.server_type,
            instance.mc_version,
            _java_label(instance.java),
            f"{instance.memory_mb}M",
            instance.state.value,
            engine.endpoint_of(instance),
            str(instance.ttl_minutes) if instance.ttl_minutes else "-",
            _fmt_time(instance.created_at),
        ]
        for instance in instances
    ]
    _print_table(
        ["NAME", "SLUG", "TYPE", "VERSION", "JAVA", "MEM", "STATE", "ENDPOINT", "TTL", "CREATED"],
        rows,
    )


@app.command()
def start(name: str = typer.Argument(..., help="实例名称")) -> None:
    """启动一个已停止的实例。"""
    engine = _build_engine()
    with _errors():
        instance = engine.start(name)
    typer.secho(f"实例 {instance.name} 已启动。", fg=typer.colors.GREEN)


@app.command()
def stop(name: str = typer.Argument(..., help="实例名称")) -> None:
    """停止一个运行中的实例(保留容器与数据)。"""
    engine = _build_engine()
    with _errors():
        instance = engine.stop(name)
    typer.secho(f"实例 {instance.name} 已停止(state={instance.state.value})。", fg=typer.colors.YELLOW)


@app.command("rm")
def remove(
    name: str = typer.Argument(..., help="实例名称"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认"),
    archive: bool = typer.Option(
        True, "--archive/--no-archive", help="删除前先打包数据卷留档(默认开启)"
    ),
) -> None:
    """删除实例:先把数据卷归档留档,再清理容器与数据卷。

    打包失败会中止删除(不丢存档)。归档后名称仍被存档占用,可随时
    ``mcctl archive download <name>`` 下载,或 ``mcctl restore <name>`` 重建。
    """
    engine = _build_engine()
    if not yes:
        typer.confirm(f"确认删除实例 {name} 及其容器?(数据卷会先归档留档)", abort=True)
    with _errors():
        saved = engine.destroy(name, archive=archive)
    typer.secho(f"实例 {name} 的容器与数据卷已清除。", fg=typer.colors.GREEN)
    if saved is not None:
        typer.echo(f"存档: {saved.path}")
        typer.echo(f"      保留至 {_fmt_time(saved.expires_at)}({_human_size(saved.size_bytes)})")
        typer.echo(f"      下载: mcctl archive download {name}")


@app.command()
def logs(
    name: str = typer.Argument(..., help="实例名称"),
    lines: int = typer.Option(100, "--lines", "-n", help="输出最近 n 行"),
) -> None:
    """查看实例容器日志。"""
    engine = _build_engine()
    with _errors():
        output = engine.logs(name, lines)
    typer.echo(output)


@app.command()
def endpoint(name: str = typer.Argument(..., help="实例名称")) -> None:
    """打印实例的连接地址。"""
    engine = _build_engine()
    with _errors():
        instance = engine.get(name)
        if instance is None:
            _abort(f"实例 {name!r} 不存在")
        _echo_endpoint(engine, instance)


@app.command()
def players(name: str = typer.Argument(..., help="实例名称")) -> None:
    """查询实例在线人数(容器内 rcon-cli,不开 RCON 端口)。"""
    engine = _build_engine()
    with _errors():
        count = engine.online_players(name)
    if count is None:
        typer.secho(f"无法获取 {name} 的在线人数(实例未运行或 RCON 不可用)。", fg=typer.colors.YELLOW)
        return
    typer.echo(f"{name}: {count} 人在线")


@app.command()
def cmd(
    name: str = typer.Argument(..., help="实例名称"),
    command: list[str] = typer.Argument(
        ..., help="游戏指令,例如: op Steve / say hi(可带前导 /,会自动去掉)"
    ),
) -> None:
    """向游戏服务端发送一条指令(经容器内 rcon-cli,不是 Linux 命令)。"""
    engine = _build_engine()
    with _errors():
        output = engine.send_command(name, " ".join(command))
    typer.echo(output.strip() or "(命令已执行,无输出)")


@app.command()
def op(
    name: str = typer.Argument(..., help="实例名称"),
    player: str = typer.Argument(..., help="玩家名(1-16 位字母 / 数字 / 下划线)"),
) -> None:
    """把玩家设为管理员(op)。"""
    engine = _build_engine()
    with _errors():
        output = engine.op(name, player)
    typer.echo(output.strip() or f"已将 {player} 设为 {name} 的管理员")


@app.command()
def deop(
    name: str = typer.Argument(..., help="实例名称"),
    player: str = typer.Argument(..., help="玩家名(1-16 位字母 / 数字 / 下划线)"),
) -> None:
    """撤销玩家的管理员权限(deop)。"""
    engine = _build_engine()
    with _errors():
        output = engine.deop(name, player)
    typer.echo(output.strip() or f"已撤销 {player} 在 {name} 上的管理员权限")


@app.command()
def reconcile() -> None:
    """对比 DB 期望状态与 docker 实际状态,修正漂移。"""
    engine = _build_engine()
    with _errors():
        drifts = engine.reconcile()
    if not drifts:
        typer.secho("未发现状态漂移。", fg=typer.colors.GREEN)
        return
    _print_table(
        ["NAME", "EXPECTED", "ACTUAL", "RESOLVED"],
        [[d.name, d.expected.value, d.actual.value, d.resolved.value] for d in drifts],
    )
    typer.secho(f"已修正 {len(drifts)} 处漂移。", fg=typer.colors.YELLOW)


@app.command()
def restore(
    name: str = typer.Argument(..., help="服务器名(存档的索引)"),
    server_type: Optional[str] = typer.Option(
        None, "--type", "-t", help="覆盖存档里记录的服务端类型"
    ),
    mc_version: Optional[str] = typer.Option(
        None, "--version", "-V", help="覆盖存档里记录的版本"
    ),
    memory: Optional[str] = typer.Option(None, "--memory", "-m", help="覆盖存档里记录的内存"),
    java: Optional[str] = typer.Option(None, "--java", help="覆盖存档里记录的 Java"),
    online_mode: Optional[bool] = typer.Option(
        None, "--online-mode/--offline", help="覆盖存档里记录的正版验证设置"
    ),
) -> None:
    """用存档重建服务器(世界数据原样回来),成功后该存档被消费掉。

    没给的参数沿用存档里记录的原始配置,因此一条命令就能还原一个实例。
    """
    engine = _build_engine()
    with _errors():
        archive = engine.archive_of(name)
        typer.echo(f"存档: {archive.path}({_human_size(archive.size_bytes)})")
        instance = engine.restore(
            name,
            server_type=server_type,
            mc_version=mc_version,
            memory_mb=parse_memory(memory) if memory else None,
            online_mode=online_mode,
            java=java,
        )
    typer.secho(f"实例 {instance.name} 已从存档重建(state={instance.state.value})。", fg=typer.colors.GREEN)
    _echo_endpoint(engine, instance)


@app.command()
def reap() -> None:
    """按生命周期策略回收实例,并清理过期存档。

    四条规则(任一满足即"关停 + 归档 + 删除"):

    1. 创建后 10 分钟内无人加入;
    2. 在线人数为 0 持续 1 小时;
    3. 创建时长超过 24 小时;
    4. 被 stop 后一直没启动超过 24 小时。

    删除后存档保留额外 24 小时,期间可用 ``mcctl archive download`` 下载、
    ``mcctl restore`` 重建。阈值都在配置文件的 lifecycle 段里,改 0 即关闭该规则。

    这个命令是一次性的(不做循环),方便交给 cron / systemd timer 定时执行;
    想让它跟着 HTTP 服务一直跑,用 ``mcctl serve``。
    """
    config = _load_config()
    engine = _build_engine(config)
    policy = config.lifecycle
    if not policy.enabled:
        typer.secho("生命周期回收已关闭(lifecycle.enabled = false)。", fg=typer.colors.YELLOW)
        return

    with _errors():
        report = Reaper(engine, policy).tick()

    if report.purged:
        _print_table(["已清理的存档"], [[name] for name in report.purged])
    if report.reaped:
        _print_table(
            ["已回收的实例", "原因"],
            [[item.name, item.reason] for item in report.reaped],
        )
    if report.errors:
        _print_table(["出错的名字", "错误"], [[name, message] for name, message in report.errors])

    if not report.changed and not report.errors:
        typer.secho("没有需要回收的实例。", fg=typer.colors.GREEN)
    elif report.errors:
        typer.secho(f"本轮有 {len(report.errors)} 个实例处理失败。", fg=typer.colors.YELLOW)
    else:
        typer.secho(
            f"已回收 {len(report.reaped)} 个实例,清理 {len(report.purged)} 个存档。",
            fg=typer.colors.YELLOW,
        )


@app.command()
def serve(
    host: Optional[str] = typer.Option(None, "--host", help="监听地址(默认取配置的 api.bind)"),
    port: Optional[int] = typer.Option(None, "--port", help="监听端口(默认取配置的 api.port)"),
) -> None:
    """启动持久 HTTP 服务(FastAPI + uvicorn)。

    **所有接口都需要 API 密钥**,密钥在配置文件的 api 段里设置(key 为空则拒绝启动),
    请求时用 ``Authorization: Bearer <key>`` 或 ``X-API-Key: <key>`` 头带上。

    服务同时会跑一个后台任务,按 lifecycle 段的规则自动回收空闲/超时的实例。
    """
    config = _load_config()
    try:
        config.require_api_key()
    except ConfigError as exc:
        _abort(str(exc))

    bind = host or config.api_bind
    listen_port = port or config.api_port

    import uvicorn

    from .api import create_app

    application = create_app(config)
    typer.secho(f"HTTP 服务: http://{bind}:{listen_port}", fg=typer.colors.GREEN)
    if config.api_docs:
        typer.echo(f"接口文档: http://{bind}:{listen_port}/docs(不校验密钥,仅建议内网使用)")
    else:
        typer.echo("接口文档: 已关闭(需要时在配置的 api 段设 docs = true)")
    typer.echo(f"存档目录: {config.archives_root()}")
    typer.echo(f"生命周期: {'开启' if config.lifecycle.enabled else '关闭'}")
    if bind not in {"127.0.0.1", "localhost", "::1"}:
        typer.secho(
            f"注意: 正在监听 {bind},非本机也能访问;本版不做 TLS,请自行确保网络可信。",
            fg=typer.colors.YELLOW,
        )
    uvicorn.run(application, host=bind, port=listen_port, log_level="info")


# --------------------------------------------------------------- 存档子命令
@archive_app.command("list")
def archive_list() -> None:
    """列出所有存档(含过期时间)。"""
    engine = _build_engine()
    with _errors():
        archives = engine.list_archives()
    _print_table(
        ["NAME", "SLUG", "TYPE", "VERSION", "SIZE", "CREATED", "EXPIRES", "FILE"],
        [
            [
                item.name,
                item.slug,
                item.server_type,
                item.mc_version,
                _human_size(item.size_bytes),
                _fmt_time(item.created_at),
                _fmt_time(item.expires_at),
                str(item.path),
            ]
            for item in archives
        ],
    )
    if not archives:
        typer.echo("提示: 删除实例(mcctl rm)会自动生成存档。")


@archive_app.command("pack")
def archive_pack(name: str = typer.Argument(..., help="实例名称")) -> None:
    """就地打包一个实例的数据卷(不停止、不删除实例)。"""
    engine = _build_engine()
    with _errors():
        archive = engine.pack_archive(name)
    typer.secho(
        f"已打包 {name} → {archive.path}({_human_size(archive.size_bytes)})", fg=typer.colors.GREEN
    )
    typer.echo("注意: 运行中的实例是热备份(服务不停,文件不保证严格一致)。")


@archive_app.command("download")
def archive_download(
    name: str = typer.Argument(..., help="服务器名(实例已删除也能下载)"),
    output: Optional[Path] = typer.Option(
        None, "--output", "-o", help="保存路径或是目录(默认当前目录下用存档原名)"
    ),
    refresh: bool = typer.Option(False, "--refresh", help="实例仍在运行时强制重新打包"),
) -> None:
    """下载存档(存档以服务器名为索引)。

    实例还在:优先用已有存档,没有就现场打包;实例已删除:下载保留期内那份。
    """
    engine = _build_engine()
    with _errors():
        archive = engine.ensure_archive(name, refresh=refresh)
    target = Path(output) if output is not None else Path.cwd() / archive.filename
    if target.is_dir():
        target = target / archive.filename
    if target.resolve() == archive.path.resolve():
        typer.echo(f"存档就在 {archive.path}。")
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(archive.path, target)
    except OSError as exc:
        _abort(f"无法写入 {target}:{exc}")
    typer.secho(f"已下载到 {target}({_human_size(archive.path.stat().st_size)})", fg=typer.colors.GREEN)
    typer.echo(f"重建: mcctl restore {name}")


@archive_app.command("purge")
def archive_purge(
    name: str = typer.Argument(..., help="服务器名"),
    yes: bool = typer.Option(False, "--yes", "-y", help="跳过确认"),
) -> None:
    """丢弃存档(删文件 + 删索引),服务器名随之释放。"""
    engine = _build_engine()
    with _errors():
        archive = engine.archive_of(name)
    if not yes:
        typer.confirm(f"确认丢弃 {name} 的存档 {archive.path}?", abort=True)
    with _errors():
        engine.purge_archive(name)
    typer.secho(f"存档 {name} 已丢弃,名字可以重新使用了。", fg=typer.colors.GREEN)


if __name__ == "__main__":  # pragma: no cover
    app()
