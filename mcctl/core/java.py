"""Java 运行时选择与校验。

``mcctl create --java`` 支持两种输入,分别对应两种**官方支持**的机制:

1. **版本号**(``17`` / ``21``,兼容旧写法 ``1.8``;也接受 itzg 的 tag 名如
   ``java21`` / ``java21-jdk``)

   切换官方镜像的 tag。这是 itzg/minecraft-server 切换 Java 版本的**官方方式**
   (见官方文档 "Java" 页的 image tags 表)。镜像里**没有**"选择 Java 版本"的
   环境变量——脚本里的 ``JAVA_VERSION`` 只是用来解析 JDK 自带 ``release`` 文件
   的,因此这里不臆造变量名,而是按官方文档改 tag。

2. **宿主机 JDK 路径**(指向 ``bin`` 目录,或 JDK 根目录)

   把该 JDK 只读挂载进容器,并用 ``JAVA_HOME`` / ``PATH`` 覆盖镜像自带的 JRE。
   镜像通过 ``PATH`` 上的 ``java`` 启动服务端,所以把自定义 ``bin`` 排在最前面
   即可生效;``JAVA_HOME`` 同时供镜像启动脚本读取版本号。

路径方式是把宿主机目录 **只读挂载**进容器,所以那个目录里必须是一份 **Linux 版
JDK**;如果目录里只有 ``java.exe``(Windows 版 JDK)会被直接拒绝——``java``
与平台绑定,``java.exe`` 不可能在 Linux 容器里跑起来。Windows 宿主机也可以
使用本功能,只要目录里放的是解压出来的 Linux 版 JDK(例如 ``~/jvm/jdk-21/bin``)。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

#: 自定义 JDK 在容器内的只读挂载点
CONTAINER_JAVA_HOME = "/opt/mcctl-java"

#: 纯数字版本号(兼容 ``1.8`` 这类旧写法)
_VERSION_RE = re.compile(r"^\d+(?:\.\d+)*$")
#: 已经是 itzg 镜像 tag 的形态,例如 ``java21`` / ``java21-jdk``
_TAG_RE = re.compile(r"^java(\d+)(?:-[a-z0-9]+)?$")


@dataclass(frozen=True)
class JavaRuntime:
    """``--java`` 的解析结果。"""

    #: 用户原始输入(存库 / 展示用)
    raw: str
    #: 镜像 tag 名,例如 ``java21``;路径方式为 ``None``
    tag: str | None = None
    #: 宿主机 JDK 根目录(其下应有 ``bin/java``);版本方式为 ``None``
    host_home: str | None = None

    @property
    def uses_host_path(self) -> bool:
        """是否通过挂载宿主机 JDK 来指定 Java。"""
        return self.host_home is not None


def resolve_java(value: str | None) -> JavaRuntime | None:
    """把 ``--java`` 的原始输入解析为 :class:`JavaRuntime`。

    Args:
        value: 版本号或 JDK 路径;``None`` / 空串表示不指定。

    Returns:
        ``None`` 表示使用镜像自带的 Java。

    Raises:
        ValueError: 既不是合法版本号,也不是可用的 Linux JDK 路径。
    """
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None

    # 1) itzg 官方 tag 形态(如 java21 / java21-jdk)
    lowered = raw.lower()
    if "/" not in raw and "\\" not in raw and _TAG_RE.match(lowered):
        return JavaRuntime(raw=raw, tag=lowered)

    # 2) 纯版本号(如 21 / 1.8)
    if _VERSION_RE.match(raw):
        return JavaRuntime(raw=raw, tag=f"java{_normalize_version(raw)}")

    # 3) 宿主机 JDK 路径
    return JavaRuntime(raw=raw, host_home=_resolve_host_home(raw))


def apply_java_tag(image: str, tag: str) -> str:
    """把镜像引用替换为 itzg 的 Java tag。

    例如 ``itzg/minecraft-server`` → ``itzg/minecraft-server:java21``。
    已有的 tag / digest 会被替换;registry 端口里的 ``:`` 不会被误判为 tag 分隔符。
    """
    repo = image.split("@", 1)[0]
    last_slash = repo.rfind("/")
    last_colon = repo.rfind(":")
    if last_colon > last_slash:
        repo = repo[:last_colon]
    return f"{repo}:{tag}"


def _normalize_version(raw: str) -> str:
    """``1.8`` → ``8``(Java 8 及更早的官方镜像 tag 是 ``java8``)。"""
    parts = raw.split(".")
    if parts[0] == "1" and len(parts) > 1:
        return parts[1]
    return parts[0]


def _resolve_host_home(path: str) -> str:
    """由"JDK bin 目录或 JDK 根目录"推出 JDK 根目录,并做完整性校验。"""
    resolved = os.path.realpath(os.path.expanduser(path))
    if not os.path.isdir(resolved):
        raise ValueError(f"--java 路径不存在或不是目录: {path}")

    # 传入的是 bin 目录(其下有 java / java.exe)
    if _looks_like_bin_dir(resolved):
        _reject_windows_jdk(resolved, path)
        return os.path.dirname(resolved)
    # 传入的是 JDK 根目录
    bin_dir = os.path.join(resolved, "bin")
    if _looks_like_bin_dir(bin_dir):
        _reject_windows_jdk(bin_dir, path)
        return resolved
    raise ValueError(f"--java 路径 {path} 看起来不是 JDK:其下没有 bin/java")


def _looks_like_bin_dir(directory: str) -> bool:
    """目录下是否有 ``java``(Linux)或 ``java.exe``(Windows)。"""
    return any(
        os.path.isfile(os.path.join(directory, name)) for name in ("java", "java.exe")
    )


def _reject_windows_jdk(bin_dir: str, original: str) -> None:
    """容器是 Linux,只有 ``java.exe`` 的 JDK 跑不起来,尽早报错而不是建出坏容器。"""
    if os.path.isfile(os.path.join(bin_dir, "java")):
        return
    raise ValueError(
        f"--java 路径 {original} 看起来是 Windows 版 JDK(只有 java.exe);"
        "实例容器是 Linux,请改用 Linux 版 JDK,或直接用版本号(例如 --java 21)"
    )
