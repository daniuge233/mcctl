"""mcctl —— Minecraft 临时服务器集群管理器(本地版)。

分层:

* runtime 层 —— ``itzg/minecraft-server`` 容器,一个容器 = 一个 MC 实例(复用现成镜像)。
* 编排层   —— 本包的 :mod:`mcctl.core`,负责多实例生命周期。
* 暴露层   —— :mod:`mcctl.adapters`,仅定义接口,本版为空实现。
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
