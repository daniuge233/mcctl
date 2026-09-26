"""针对 Docker provisioner 中不依赖 daemon 的纯逻辑的单元测试。"""

from __future__ import annotations

import os
from typing import Any

import pytest
from docker.errors import APIError, ImageNotFound

from mcctl.core.config import Config
from mcctl.core.java import CONTAINER_JAVA_HOME, resolve_java
from mcctl.provisioners.base import ProvisionError, Spec
from mcctl.provisioners.docker_p import DockerProvisioner

#: 假镜像自带的 PATH,用于验证自定义 JDK 会被插到最前面
_IMAGE_PATH = "/usr/local/bin:/usr/bin:/bin"


class _FakeImage:
    """最小化的 ``Image`` 替身(只需要 ``attrs.Config.Env``)。"""

    def __init__(self, path: str = _IMAGE_PATH) -> None:
        self.attrs = {"Config": {"Env": [f"PATH={path}"]}}


class _FakeImages:
    """最小化的 ``client.images`` 替身。"""

    def __init__(
        self,
        cached: bool,
        pull_error: Exception | None = None,
        path: str = _IMAGE_PATH,
    ) -> None:
        self.cached = cached
        self.pull_error = pull_error
        self.path = path
        self.pulled: list[str] = []

    def get(self, image: str) -> _FakeImage:
        if not self.cached:
            raise ImageNotFound(f"No such image: {image}")
        return _FakeImage(self.path)

    def pull(self, image: str) -> object:
        self.pulled.append(image)
        if self.pull_error is not None:
            raise self.pull_error
        return object()


class _FakeClient:
    """只提供 ``images`` 的 docker 客户端替身。"""

    def __init__(self, images: _FakeImages) -> None:
        self.images = images


class _FakeContainer:
    """用于 ``_verify_router`` 的容器替身:第 ``crash_after`` 次 reload 后开始重启。"""

    def __init__(self, crash_after: int | None = None) -> None:
        self.status = "running"
        self.attrs: dict[str, Any] = {"RestartCount": 0}
        self._reloads = 0
        self._crash_after = crash_after

    def reload(self) -> None:
        self._reloads += 1
        if self._crash_after is not None and self._reloads > self._crash_after:
            self.status = "restarting"
            self.attrs = {"RestartCount": 1}

    def logs(self, **_kwargs: Any) -> bytes:
        return b"permission denied while trying to connect to the Docker daemon socket\n"


def _provisioner(**overrides: object) -> DockerProvisioner:
    """构造一个不会连接 daemon 的 provisioner(client 是惰性创建的)。"""
    config = Config(db_path=":memory:", **overrides)  # type: ignore[arg-type]
    return DockerProvisioner(config)


def _with_client(images: _FakeImages, **overrides: object) -> DockerProvisioner:
    """构造一个注入了假 docker 客户端的 provisioner。"""
    provisioner = _provisioner(**overrides)
    provisioner._client = _FakeClient(images)  # type: ignore[assignment]
    return provisioner


def test_socket_group_override_wins(tmp_path) -> None:
    """显式配置的 GID 优先,且不退回 root。"""
    provisioner = _provisioner(router_socket_group="4242")
    assert provisioner._router_socket_access() == (["4242"], None)


def test_socket_group_detected_from_socket_file(tmp_path) -> None:
    """未配置时,取宿主机 socket 的属组 GID(等价官方 README 的 stat -c '%g')。"""
    socket_path = tmp_path / "docker.sock"
    socket_path.write_text("", encoding="utf-8")
    expected_gid = str(os.stat(socket_path).st_gid)

    provisioner = _provisioner(docker_socket=str(socket_path))
    assert provisioner._router_socket_access() == ([expected_gid], None)


def test_socket_group_falls_back_to_root_when_socket_invisible(tmp_path) -> None:
    """Docker Desktop(宿主看不到 socket 路径)时退回 user=root。"""
    provisioner = _provisioner(docker_socket=str(tmp_path / "does-not-exist.sock"))
    assert provisioner._router_socket_access() == (None, "root")


def test_ensure_image_skips_pull_when_cached() -> None:
    """本地已有镜像时不拉取。"""
    images = _FakeImages(cached=True)
    _with_client(images)._ensure_image("itzg/minecraft-server")
    assert images.pulled == []


def test_ensure_image_pulls_when_missing() -> None:
    """本地缺镜像时拉取一次。"""
    images = _FakeImages(cached=False)
    _with_client(images)._ensure_image("itzg/minecraft-server")
    assert images.pulled == ["itzg/minecraft-server"]


def test_ensure_image_wraps_pull_failure_with_hint() -> None:
    """拉取失败要报 ProvisionError 并说明是网络问题,而不是伪装成 daemon 异常。"""
    images = _FakeImages(cached=False, pull_error=APIError("500 Server Error: EOF"))
    with pytest.raises(ProvisionError, match="拉取失败"):
        _with_client(images)._ensure_image("itzg/minecraft-server")


def test_verify_router_detects_crash_loop() -> None:
    """router 一起就崩时必须报错,并附上日志尾部。"""
    provisioner = _provisioner()
    container = _FakeContainer(crash_after=1)
    with pytest.raises(ProvisionError, match="permission denied"):
        provisioner._verify_router(container, settle=0.1)  # type: ignore[arg-type]


def test_verify_router_accepts_stable_container() -> None:
    """稳定运行的 router 不报错。"""
    provisioner = _provisioner()
    container = _FakeContainer(crash_after=None)
    provisioner._verify_router(container, settle=0.1)  # type: ignore[arg-type]
    assert container.status == "running"


# ------------------------------------------------------------------ --java
def _make_jdk_home(root) -> str:
    """造一个最小可识别的 JDK 目录。"""
    (root / "bin").mkdir(parents=True, exist_ok=True)
    (root / "bin" / "java").write_text("", encoding="utf-8")
    return str(root)


def _spec(**overrides: Any) -> Spec:
    """构造一个最小的 Spec。"""
    values: dict[str, Any] = {
        "name": "test",
        "slug": "test",
        "container_name": "mc-test",
        "volume_name": "mc-test-data",
        "network_name": "mc-net",
        "host": "test.mc.loc",
        "server_type": "paper",
        "mc_version": "1.21",
        "memory_mb": 2048,
        "online_mode": True,
        "java": None,
    }
    values.update(overrides)
    return Spec(**values)


def test_server_image_uses_official_java_tag() -> None:
    """--java 21 → 镜像切到 itzg 官方 java tag。"""
    provisioner = _provisioner(server_image="itzg/minecraft-server")
    assert provisioner._server_image_for(_spec(java=resolve_java("21"))) == (
        "itzg/minecraft-server:java21"
    )


def test_server_image_unchanged_without_java() -> None:
    """未指定 --java 时不改镜像。"""
    provisioner = _provisioner(server_image="itzg/minecraft-server")
    assert provisioner._server_image_for(_spec()) == "itzg/minecraft-server"


def test_java_path_is_mounted_read_only(tmp_path) -> None:
    """路径方式把宿主机 JDK 只读挂载进容器。"""
    java = resolve_java(_make_jdk_home(tmp_path / "jdk"))
    assert java is not None and java.host_home is not None

    volumes = _provisioner()._java_volumes(_spec(java=java))
    assert volumes == {java.host_home: {"bind": CONTAINER_JAVA_HOME, "mode": "ro"}}


def test_no_java_mount_by_default() -> None:
    """不指定 --java 时不额外挂载。"""
    assert _provisioner()._java_volumes(_spec()) == {}


def test_java_path_overrides_java_home_and_path(tmp_path) -> None:
    """路径方式通过 JAVA_HOME / PATH 覆盖镜像自带的 JRE。"""
    java = resolve_java(_make_jdk_home(tmp_path / "jdk"))
    provisioner = _with_client(_FakeImages(cached=True, path="/opt/java/openjdk/bin:/usr/bin"))

    env = provisioner._server_env(_spec(java=java), "itzg/minecraft-server")

    assert env["JAVA_HOME"] == CONTAINER_JAVA_HOME
    assert env["PATH"] == f"{CONTAINER_JAVA_HOME}/bin:/opt/java/openjdk/bin:/usr/bin"
    assert env["EULA"] == "TRUE"


def test_java_version_does_not_change_env() -> None:
    """版本方式只换镜像,不注入 JAVA_HOME / PATH。"""
    provisioner = _provisioner()
    env = provisioner._server_env(_spec(java=resolve_java("17")), "itzg/minecraft-server")
    assert "JAVA_HOME" not in env
    assert "PATH" not in env
