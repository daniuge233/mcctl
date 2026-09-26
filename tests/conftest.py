"""测试用环境隔离。

``Config.load()`` 会读取真实环境变量和用户主目录(``~/.mcctl/config.toml``)。
不隔离的话,开发者本机已有的配置会悄悄污染测试结果,所以这里统一把 ``MCCTL_*``
环境变量清空,并把默认配置位置指向临时目录。
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_mcctl_env(monkeypatch, tmp_path):
    """清掉所有 ``MCCTL_*`` 环境变量,并把 MCCTL_HOME 指向空的临时目录。"""
    for name in list(os.environ):
        if name.startswith("MCCTL_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MCCTL_HOME", str(tmp_path / "home"))
