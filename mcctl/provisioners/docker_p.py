"""第一版 Provisioner 实现:基于 docker SDK。

硬性约束(见实现规格 §4):

* 自定义 bridge 网络 ``mc-net``,所有容器接入。
* ``mc-router`` 容器:``itzg/mc-router``,监听 25565,**唯一**端口映射
  ``127.0.0.1:25565:25565``,挂载 ``/var/run/docker.sock``,label 自动发现模式。
* 实例容器:``itzg/minecraft-server``,**不映射任何宿主机端口**,接入 ``mc-net``,
  打 ``mc-router.host`` label,named volume ``mc-<slug>-data:/data``。
* 容器之间用 docker 内建 DNS(容器名)互访;RCON 不开端口,用 ``docker exec rcon-cli``。

label / 环境变量名以官方 README 为准:
``mc-router.host`` 来自 itzg/mc-router;``EULA`` / ``TYPE`` / ``VERSION`` / ``MEMORY`` /
``ONLINE_MODE``(默认 true)来自 itzg/docker-minecraft-server。
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

import docker
from docker.errors import APIError, ImageNotFound, NotFound
from docker.models.containers import Container, ExecResult
from docker.models.networks import Network
from docker.models.volumes import Volume

from ..core.config import Config
from ..core.java import CONTAINER_JAVA_HOME, apply_java_tag
from ..core.models import State
from .base import Handle, ProvisionError, ProvisionTimeout, Spec

logger = logging.getLogger(__name__)

#: docker 容器状态 -> mcctl 状态
_STATUS_MAP: dict[str, State] = {
    "running": State.running,
    "restarting": State.running,
    "created": State.stopped,
    "exited": State.stopped,
    "paused": State.stopped,
    "removing": State.stopping,
    "dead": State.failed,
}

#: itzg/minecraft-server 启动完成后会打印 ``Done (12.345s)! For help, type "help"``
_DONE_MARKER = re.compile(r"Done \([0-9.]+s\)!")
#: ``There are 0 of a max of 20 players online:``
_PLAYER_COUNT = re.compile(r"There are (\d+) of a max")
#: 镜像没有显式 PATH 时的兜底(挂载自定义 JDK 时在其前面追加 bin 目录)
_DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


class DockerProvisioner:
    """:class:`~mcctl.provisioners.base.Provisioner` 的 Docker 实现。

    通过本机 docker socket 访问 docker daemon(管理器本身不容器化)。
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self._client: docker.DockerClient | None = None

    @property
    def client(self) -> docker.DockerClient:
        """惰性创建 docker 客户端。"""
        if self._client is None:
            self._client = docker.from_env()
        return self._client

    # ------------------------------------------------------------ runtime 前置
    def ensure_runtime(self) -> None:
        """确保 ``mc-net`` 与 ``mc-router`` 存在且运行(幂等)。"""
        self._ensure_network()
        self._ensure_router()

    def _ensure_network(self) -> Network:
        """确保自定义 bridge 网络存在。"""
        name = self.config.network_name
        try:
            network = self.client.networks.get(name)
            logger.debug("event=network.exists name=%s id=%s", name, network.short_id)
            return network
        except NotFound:
            logger.info("event=network.create name=%s driver=bridge", name)
            return self.client.networks.create(name, driver="bridge", check_duplicate=True)

    def _ensure_router(self) -> Container:
        """确保 mc-router 容器存在、运行且稳定(幂等)。

        label 自动发现模式:``IN_DOCKER=true``;唯一端口映射 ``127.0.0.1:25565:25565``。
        """
        name = self.config.router_name
        try:
            container = self.client.containers.get(name)
        except NotFound:
            container = None

        if container is not None:
            if container.status == "running":
                logger.debug("event=router.exists name=%s id=%s", name, container.short_id)
                return container
            logger.info("event=router.start name=%s", name)
            container.start()
            container.reload()
            self._verify_router(container)
            return container

        self._ensure_image(self.config.router_image)
        group_add, user = self._router_socket_access()
        logger.info(
            "event=router.create name=%s image=%s bind=%s:%s network=%s group_add=%s user=%s",
            name,
            self.config.router_image,
            self.config.router_bind,
            self.config.router_port,
            self.config.network_name,
            group_add,
            user,
        )
        run_kwargs: dict[str, Any] = {
            "name": name,
            "detach": True,
            # IN_DOCKER: 启用 docker label 自动发现(--in-docker)
            "environment": {"IN_DOCKER": "true", "PORT": str(self.config.server_port)},
            "ports": {
                f"{self.config.server_port}/tcp": (self.config.router_bind, self.config.router_port)
            },
            "volumes": {
                self.config.docker_socket: {"bind": self.config.docker_socket, "mode": "rw"}
            },
            # network= 会让 docker SDK 自动构造 networking_config
            "network": self.config.network_name,
            "restart_policy": {"Name": "unless-stopped"},
        }
        if group_add:
            run_kwargs["group_add"] = group_add
        if user:
            run_kwargs["user"] = user

        container = self.client.containers.run(self.config.router_image, **run_kwargs)
        self._verify_router(container)
        return container

    def _router_socket_access(self) -> tuple[list[str] | None, str | None]:
        """决定怎样让 mc-router 能读写 ``/var/run/docker.sock``。

        官方 README「User access to docker socket」指出:镜像默认以**非 root 用户**
        运行,而 socket 通常是 ``root:root 660``(Docker Desktop)或 ``root:docker 660``
        (Linux 原生),所以直接挂载会 `permission denied` 并 crash-loop。README 给出
        的办法是把 socket 的属组 GID ``group_add`` 进容器(或干脆 ``user: root``)。

        优先级:``MCCTL_ROUTER_SOCKET_GROUP`` > ``stat`` 取 socket 属组 GID
        (即 README 里的 ``stat -c '%g' /var/run/docker.sock``)>
        宿主机拿不到该路径时(Docker Desktop)退回 ``user=root``。
        """
        if self.config.router_socket_group:
            return [self.config.router_socket_group], None
        try:
            gid = os.stat(self.config.docker_socket).st_gid
        except OSError:
            logger.debug(
                "event=router.socket.stat-failed path=%s (改为以 root 运行)",
                self.config.docker_socket,
            )
            return None, "root"
        return [str(gid)], None

    def _verify_router(self, container: Container, settle: float = 3.0) -> None:
        """确认 router 稳定运行,而不是在 crash-loop。

        启动瞬间就报“已就绪”会掩盖 socket 权限这类致命错误,所以观察一小段时间:
        只要状态离开 ``running`` 或重启计数增加,就直接报错并带上日志尾部。
        """
        deadline = time.monotonic() + settle
        restarts_before = int(container.attrs.get("RestartCount", 0))
        while time.monotonic() < deadline:
            container.reload()
            if container.status != "running":
                break
            time.sleep(0.5)
        container.reload()
        restarts_after = int(container.attrs.get("RestartCount", 0))
        if container.status != "running" or restarts_after > restarts_before:
            raise ProvisionError(
                f"mc-router 未能正常运行(status={container.status}, "
                f"restarts={restarts_before}->{restarts_after}):\n{self._tail(container)}"
            )

    def _ensure_image(self, image: str) -> None:
        """确保镜像存在于本地,缺失时拉取。

        拉取失败时抛出携带原始成因的 :class:`ProvisionError`,以免把网络
        故障伪装成 "Docker 未运行"(``docker.errors.ImageNotFound`` 继承自
        ``APIError``,在 CLI 层会被当成 daemon 异常)。
        """
        try:
            self.client.images.get(image)
        except ImageNotFound:
            pass
        except APIError as exc:  # daemon 不可用 / API 版本不兼容等
            raise ProvisionError(f"无法查询本地镜像 {image}: {exc}") from exc
        else:
            logger.debug("event=image.cached image=%s", image)
            return

        logger.info("event=image.pull image=%s (首次拉取可能较慢)", image)
        try:
            self.client.images.pull(image)
        except ImageNotFound as exc:
            raise ProvisionError(f"镜像 {image} 拉取失败:仓库中不存在该镜像") from exc
        except APIError as exc:
            raise ProvisionError(
                f"镜像 {image} 拉取失败,请检查网络连通性或 Docker 镜像加速配置后重试: {exc}"
            ) from exc

    # ------------------------------------------------------------ 核心接口
    def create(self, spec: Spec) -> Handle:
        """确保 volume 与实例容器存在并已启动(幂等,可安全重放)。

        内部子步骤(各自幂等、各自记录日志):``建 volume`` → ``建容器`` → ``启动``。
        """
        self._ensure_network()
        self._ensure_volume(spec)

        container = self._ensure_container(spec)
        if container.status != "running":
            logger.info("event=container.start name=%s id=%s", container.name, container.short_id)
            container.start()
            container.reload()
        else:
            logger.debug("event=container.already-running name=%s", container.name)

        return Handle(
            name=spec.name,
            slug=spec.slug,
            container_name=spec.container_name,
            container_id=container.id,
            volume_name=spec.volume_name,
        )

    def destroy(self, handle: Handle) -> None:
        """删除容器与数据卷(幂等)。"""
        container = self._find_container(handle)
        if container is not None:
            logger.info("event=container.remove name=%s id=%s", container.name, container.short_id)
            try:
                container.remove(force=True)
            except NotFound:
                logger.debug("event=container.remove.gone name=%s", handle.container_name)

        volume = self._find_volume(handle.volume_name)
        if volume is not None:
            logger.info("event=volume.remove name=%s", handle.volume_name)
            try:
                volume.remove(force=True)
            except NotFound:
                logger.debug("event=volume.remove.gone name=%s", handle.volume_name)

    def status(self, handle: Handle) -> State:
        """查询实例状态;容器不存在时视为 ``destroyed``。"""
        container = self._find_container(handle)
        if container is None:
            return State.destroyed
        return _STATUS_MAP.get(container.status, State.failed)

    def logs(self, handle: Handle, n: int = 100) -> str:
        """返回容器最近 ``n`` 行日志。"""
        container = self._require_container(handle)
        return self._decode_logs(container, n)

    # ------------------------------------------------------------ 生命周期扩展
    def start(self, handle: Handle) -> None:
        """启动已停止的容器。"""
        container = self._require_container(handle)
        if container.status == "running":
            logger.debug("event=container.start.skip name=%s", container.name)
            return
        logger.info("event=container.start name=%s id=%s", container.name, container.short_id)
        container.start()
        container.reload()

    def stop(self, handle: Handle) -> None:
        """优雅停止容器。"""
        container = self._find_container(handle)
        if container is None:
            logger.warning("event=container.stop.missing name=%s", handle.container_name)
            return
        if container.status != "running":
            logger.debug("event=container.stop.skip name=%s status=%s", container.name, container.status)
            return
        logger.info("event=container.stop name=%s id=%s", container.name, container.short_id)
        container.stop(timeout=60)
        container.reload()

    def wait_ready(self, handle: Handle, timeout: float | None = None) -> None:
        """轮询直到服务端就绪。

        优先使用镜像自带的 HEALTHCHECK;若没有健康信息,则回退到日志中
        的 ``Done (...)`` 标记。
        """
        deadline = time.monotonic() + (
            timeout if timeout is not None else float(self.config.health_timeout)
        )
        while True:
            container = self._find_container(handle)
            if container is None:
                raise ProvisionError(f"容器 {handle.container_name} 在等待就绪期间消失")
            container.reload()

            if container.status in ("exited", "dead"):
                raise ProvisionError(
                    f"容器 {handle.container_name} 在就绪前退出:\n{self._tail(container)}"
                )

            health = (container.attrs.get("State") or {}).get("Health")
            if health is not None:
                health_status = health.get("Status")
                if health_status == "healthy":
                    logger.info("event=container.healthy name=%s", container.name)
                    return
                if health_status == "unhealthy":
                    raise ProvisionError(
                        f"容器 {handle.container_name} 健康检查失败:\n{self._tail(container)}"
                    )
            elif self._log_has_done_marker(container):
                logger.info("event=container.ready name=%s (log marker)", container.name)
                return

            if time.monotonic() >= deadline:
                raise ProvisionTimeout(
                    f"等待 {handle.container_name} 就绪超时({self.config.health_timeout:.0f}s)"
                )
            time.sleep(self.config.health_poll_interval)

    def online_players(self, handle: Handle) -> int | None:
        """通过 ``rcon-cli list`` 返回在线人数;容器未运行或命令失败时返回 ``None``。"""
        container = self._find_container(handle)
        if container is None or container.status != "running":
            return None
        try:
            # RCON 不开端口,通过容器内 rcon-cli 访问(见规格 §4.3)
            result = self._exec_rcon(container, "list")
        except APIError as exc:  # pragma: no cover - 依赖运行中的容器
            logger.warning("event=rcon.exec.failed name=%s error=%s", handle.container_name, exc)
            return None
        match = _PLAYER_COUNT.search(self._decode_exec(result))
        return int(match.group(1)) if match else None

    def send_command(self, handle: Handle, command: str) -> str:
        """通过 ``rcon-cli`` 向运行中的实例发送一条 **游戏指令**, 返回服务端输出。

        指令经 RCON 协议交给 Minecraft 服务端控制台(例如 ``op Steve``), 不是 Docker /
        Linux 命令。它以**参数列表**形式交给 ``docker exec``(不经过容器内 shell), 因此
        指令里的 ``$`` / ``;`` / 反引号等字符不会被 shell 解释。
        """
        container = self._find_container(handle)
        if container is None:
            raise ProvisionError(f"容器 {handle.container_name} 不存在")
        if container.status != "running":
            raise ProvisionError(
                f"实例 {handle.name!r} 未在运行(status={container.status}),无法发送命令"
            )
        try:
            result = self._exec_rcon(container, command)
        except APIError as exc:
            raise ProvisionError(f"向 {handle.name!r} 发送命令失败:{exc}") from exc
        output = self._decode_exec(result)
        if result.exit_code != 0:
            raise ProvisionError(
                f"命令 {command!r} 执行失败(退出码 {result.exit_code}):"
                f"{output.strip() or '<无输出>'}"
            )
        logger.info("event=rcon.command name=%s command=%s", handle.container_name, command)
        return output

    def _exec_rcon(self, container: Container, command: str) -> ExecResult:
        """在容器内执行 ``rcon-cli <command>``, 把游戏指令递给服务端(不经 shell)。"""
        return container.exec_run(["rcon-cli", command])

    @staticmethod
    def _decode_exec(result: ExecResult) -> str:
        """把 ``exec_run`` 的输出解码成文本。"""
        output = result.output
        if not output:
            return ""
        return output.decode("utf-8", errors="replace") if isinstance(output, bytes) else str(output)

    # ------------------------------------------------------------ 内部工具
    def _ensure_volume(self, spec: Spec) -> Volume:
        """确保 named volume 存在(幂等)。"""
        try:
            volume = self.client.volumes.get(spec.volume_name)
            logger.debug("event=volume.exists name=%s", spec.volume_name)
            return volume
        except NotFound:
            logger.info("event=volume.create name=%s", spec.volume_name)
            return self.client.volumes.create(
                name=spec.volume_name,
                labels={"mcctl.instance": spec.name, "mcctl.slug": spec.slug},
            )

    def _ensure_container(self, spec: Spec) -> Container:
        """确保实例容器存在(幂等);已存在则直接复用。

        实例容器不映射任何宿主机端口,仅接入 ``mc-net``。
        """
        try:
            container = self.client.containers.get(spec.container_name)
            logger.debug("event=container.exists name=%s id=%s", container.name, container.short_id)
            return container
        except NotFound:
            pass

        image = self._server_image_for(spec)
        self._ensure_image(image)
        logger.info(
            "event=container.create name=%s image=%s type=%s version=%s memory=%sM "
            "online_mode=%s host=%s java=%s",
            spec.container_name,
            image,
            spec.server_type,
            spec.mc_version,
            spec.memory_mb,
            spec.online_mode,
            spec.host,
            spec.java.raw if spec.java is not None else "-",
        )
        volumes: dict[str, dict[str, str]] = {
            # named volume: mc-<slug>-data:/data —— 实例数据持久化
            spec.volume_name: {"bind": "/data", "mode": "rw"}
        }
        volumes.update(self._java_volumes(spec))
        return self.client.containers.create(
            image,
            name=spec.container_name,
            environment=self._server_env(spec, image),
            labels=self._server_labels(spec),
            volumes=volumes,
            # 不传 ports,实例不映射任何宿主机端口;
            # network= 会让 docker SDK 自动构造 networking_config
            network=spec.network_name,
        )

    def _server_image_for(self, spec: Spec) -> str:
        """按 ``--java`` 选出镜像:版本号 → 官方 java tag;路径方式 → 默认镜像。"""
        java = spec.java
        if java is None or java.tag is None:
            return self.config.server_image
        return apply_java_tag(self.config.server_image, java.tag)

    def _java_volumes(self, spec: Spec) -> dict[str, dict[str, str]]:
        """``--java <JDK 路径>`` 时把宿主机 JDK 只读挂载进容器。"""
        java = spec.java
        if java is None or not java.uses_host_path:
            return {}
        assert java.host_home is not None  # uses_host_path 保证
        return {java.host_home: {"bind": CONTAINER_JAVA_HOME, "mode": "ro"}}

    def _server_env(self, spec: Spec, image: str) -> dict[str, str]:
        """itzg/minecraft-server 环境变量(名称以官方文档为准)。"""
        env = {
            "EULA": "TRUE",
            "TYPE": spec.server_type.upper(),
            "VERSION": spec.mc_version,
            "MEMORY": f"{spec.memory_mb}M",
            "ONLINE_MODE": "TRUE" if spec.online_mode else "FALSE",
        }
        java = spec.java
        if java is not None and java.uses_host_path:
            # 镜像通过 PATH 上的 java 启动服务端,把自定义 bin 放最前面即可生效;
            # JAVA_HOME 供启动脚本读版本(官方 start-utils 会读 $JAVA_HOME/release)
            env["JAVA_HOME"] = CONTAINER_JAVA_HOME
            env["PATH"] = f"{CONTAINER_JAVA_HOME}/bin:{self._image_path(image)}"
        return env

    def _image_path(self, image: str) -> str:
        """取镜像自带的 PATH,用于在其前面追加自定义 JDK 的 bin 目录。"""
        try:
            attrs = self.client.images.get(image).attrs
        except APIError:  # pragma: no cover - 调用前已确保镜像存在
            return _DEFAULT_PATH
        for entry in (attrs.get("Config") or {}).get("Env") or []:
            if entry.startswith("PATH="):
                return entry[len("PATH=") :]
        return _DEFAULT_PATH

    def _server_labels(self, spec: Spec) -> dict[str, str]:
        """mc-router 自动发现所需的 label + mcctl 自有标签。"""
        return {
            "mc-router.host": spec.host,
            "mc-router.port": str(self.config.server_port),
            "mcctl.instance": spec.name,
            "mcctl.slug": spec.slug,
        }

    def _find_container(self, handle: Handle) -> Container | None:
        """按 id 优先、容器名兜底查找容器;不存在返回 ``None``。"""
        if handle.container_id:
            try:
                return self.client.containers.get(handle.container_id)
            except NotFound:
                logger.debug("event=container.lookup.by-id-miss id=%s", handle.container_id)
        try:
            return self.client.containers.get(handle.container_name)
        except NotFound:
            return None

    def _require_container(self, handle: Handle) -> Container:
        """查找容器,不存在则抛 :class:`ProvisionError`。"""
        container = self._find_container(handle)
        if container is None:
            raise ProvisionError(f"容器 {handle.container_name} 不存在")
        return container

    def _find_volume(self, name: str) -> Volume | None:
        """按名称查找数据卷。"""
        try:
            return self.client.volumes.get(name)
        except NotFound:
            return None

    @staticmethod
    def _decode_logs(container: Container, n: int) -> str:
        """读取并解码容器日志。"""
        raw = container.logs(tail=n)
        return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)

    def _tail(self, container: Container, n: int = 30) -> str:
        """取日志尾部,用于错误信息。"""
        try:
            return self._decode_logs(container, n)
        except APIError:  # pragma: no cover
            return "<日志不可用>"

    def _log_has_done_marker(self, container: Container) -> bool:
        """日志中是否出现服务端启动完成标记。"""
        try:
            return bool(_DONE_MARKER.search(self._decode_logs(container, 200)))
        except APIError:  # pragma: no cover
            return False
