"""``--java`` 解析逻辑的单元测试(不需要 Docker)。"""

from __future__ import annotations

import os

import pytest

from mcctl.cli import _java_label
from mcctl.core.java import CONTAINER_JAVA_HOME, apply_java_tag, resolve_java


def _make_jdk(root) -> str:
    """在 ``root`` 造一个最小可识别的 JDK 目录,返回其路径。"""
    (root / "bin").mkdir(parents=True, exist_ok=True)
    (root / "bin" / "java").write_text("", encoding="utf-8")
    (root / "release").write_text('JAVA_VERSION="21.0.1"\n', encoding="utf-8")
    return str(root)


# ------------------------------------------------------------------ 版本号
def test_unset_java_is_none() -> None:
    """不指定 --java 时不做任何覆盖。"""
    assert resolve_java(None) is None
    assert resolve_java("") is None
    assert resolve_java("   ") is None


@pytest.mark.parametrize(
    ("value", "tag"),
    [("21", "java21"), ("17", "java17"), ("8", "java8"), ("1.8", "java8")],
)
def test_version_maps_to_official_image_tag(value: str, tag: str) -> None:
    """版本号映射为 itzg 官方 java tag(Java 8 的 1.8 写法也归一化)。"""
    resolved = resolve_java(value)
    assert resolved is not None
    assert resolved.tag == tag
    assert resolved.uses_host_path is False
    assert resolved.host_home is None


def test_explicit_tag_form_is_accepted() -> None:
    """直接写官方 tag(含 -jdk 之类的变体)也支持。"""
    resolved = resolve_java("java21-jdk")
    assert resolved is not None
    assert resolved.tag == "java21-jdk"


# ------------------------------------------------------------------ 路径
def test_jdk_home_path(tmp_path) -> None:
    """传入 JDK 根目录时直接使用它。"""
    home = _make_jdk(tmp_path / "jdk")
    resolved = resolve_java(home)
    assert resolved is not None
    assert resolved.uses_host_path is True
    assert resolved.tag is None
    assert resolved.host_home == os.path.realpath(home)


def test_bin_dir_path_resolves_to_parent(tmp_path) -> None:
    """传入 bin 目录时自动上溯到 JDK 根目录。"""
    home = _make_jdk(tmp_path / "jdk")
    resolved = resolve_java(os.path.join(home, "bin"))
    assert resolved is not None
    assert resolved.host_home == os.path.realpath(home)


def test_windows_jdk_is_rejected(tmp_path) -> None:
    """bin 下只有 java.exe 的是 Windows 版 JDK,容器里跑不起来,必须报错。"""
    jdk = tmp_path / "jdk"
    (jdk / "bin").mkdir(parents=True)
    (jdk / "bin" / "java.exe").write_text("", encoding="utf-8")

    with pytest.raises(ValueError, match="Windows 版 JDK"):
        resolve_java(str(jdk))
    with pytest.raises(ValueError, match="Windows 版 JDK"):
        resolve_java(str(jdk / "bin"))


def test_missing_path_is_rejected(tmp_path) -> None:
    """不存在的路径要报错,而不是静默变成一个坏容器。"""
    with pytest.raises(ValueError, match="不存在"):
        resolve_java(str(tmp_path / "nope" / "bin"))


def test_non_jdk_directory_is_rejected(tmp_path) -> None:
    """目录存在但不是 JDK 时报错。"""
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(ValueError, match="不是 JDK"):
        resolve_java(str(plain))


def test_container_java_home_is_absolute() -> None:
    """挂载点必须是容器内绝对路径。"""
    assert CONTAINER_JAVA_HOME.startswith("/")


# ------------------------------------------------------------------ 镜像 tag
@pytest.mark.parametrize(
    ("image", "expected"),
    [
        ("itzg/minecraft-server", "itzg/minecraft-server:java21"),
        ("itzg/minecraft-server:latest", "itzg/minecraft-server:java21"),
        ("localhost:5000/mc", "localhost:5000/mc:java21"),
        ("registry.example.com/mc:1.0", "registry.example.com/mc:java21"),
        ("itzg/minecraft-server@sha256:abc", "itzg/minecraft-server:java21"),
    ],
)
def test_apply_java_tag(image: str, expected: str) -> None:
    """替换/追加 java tag,且不把 registry 端口误判为 tag 分隔符。"""
    assert apply_java_tag(image, "java21") == expected


# ------------------------------------------------------------------ CLI 展示
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "-"),
        ("", "-"),
        ("21", "21"),
        ("java21-jdk", "java21-jdk"),
        ("/usr/lib/jvm/java-21-openjdk-amd64/bin", "jdk:java-21-openjdk-amd64"),
        ("/usr/lib/jvm/java-21-openjdk-amd64", "jdk:java-21-openjdk-amd64"),
        (r"C:\jvm\jdk-21", "jdk:jdk-21"),
    ],
)
def test_java_label_compacts_paths(value: str | None, expected: str) -> None:
    """list 表格里版本号原样显示,路径压缩成 jdk:<目录名>。"""
    assert _java_label(value) == expected
