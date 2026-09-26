"""HTTP 服务测试(用 starlette 的 TestClient,不启动 uvicorn、不碰 Docker)。

覆盖:

* **鉴权**:没有密钥 / 密钥错误 / 密钥正确的两种情况(``X-API-Key`` 与 Bearer);
  未配置密钥时一律拒绝(fail-closed);文档接口同样受保护。
* **接口**:创建 / 列表 / 详情 / 启停 / 删除 / 日志 / 打包 / 下载 / 重建 / 丢弃存档 /
  生命周期查询与手动巡检。
* **状态码映射**:404 / 409 / 422 等。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from mcctl.api import create_app
from mcctl.core.archive import new_archive
from mcctl.core.config import Config
from mcctl.core.engine import Engine
from mcctl.core.lifecycle import LifecyclePolicy
from mcctl.core.models import CreateRequest, State
from mcctl.core.store import Store
from tests.fakes import FakeArchiveStore, FakeProvisioner

KEY = "s3cret-key-please-rotate"
HEADERS = {"X-API-Key": KEY}
NOW = datetime(2025, 3, 1, 12, 0, tzinfo=timezone.utc)


def _config(tmp_path, *, api_key: str = KEY, lifecycle: LifecyclePolicy | None = None, **overrides) -> Config:
    # 默认关掉后台巡检:测试关心的是接口行为,不是定时器
    return Config(
        db_path=tmp_path / "mcctl.db",
        api_key=api_key,
        lifecycle=lifecycle or LifecyclePolicy(enabled=False),
        **overrides,
    )


def _bench(tmp_path, config: Config):
    """把假 provisioner / 假存档接到一个 TestClient 上。"""
    store = Store(config.db_path)
    store.init()
    provisioner = FakeProvisioner()
    archives = FakeArchiveStore(directory=tmp_path / "archives")
    engine = Engine(store, provisioner, config, archives=archives)
    client = TestClient(create_app(config, engine=engine))
    return client, engine, provisioner, archives, config


@pytest.fixture()
def bench(tmp_path):
    """一套接好假 provisioner / 假存档的 HTTP 客户端。"""
    return _bench(tmp_path, _config(tmp_path))


def _create(client: TestClient, name: str = "test", **body):
    payload = {"name": name, **body}
    return client.post("/servers", json=payload, headers=HEADERS)


def _with_data(engine: Engine, archives: FakeArchiveStore, name: str = "test") -> str:
    """造一个"有世界数据"的实例,返回数据卷名(方便断言)。"""
    instance = engine.create(CreateRequest(name=name))
    archives.files[instance.volume_name] = "world-data"
    return instance.volume_name


# ------------------------------------------------------------------ 鉴权
def test_missing_key_is_rejected(bench):
    """不带密钥 → 401,并且响应体里不会泄露真实密钥。"""
    client, *_ = bench

    response = client.get("/servers")

    assert response.status_code == 401
    assert KEY not in response.text


def test_wrong_key_is_rejected(bench):
    """密钥不对 → 401。"""
    client, *_ = bench

    response = client.get("/servers", headers={"X-API-Key": "nope"})

    assert response.status_code == 401


def test_x_api_key_header_is_accepted(bench):
    """``X-API-Key`` 正确 → 200。"""
    client, *_ = bench

    assert client.get("/servers", headers=HEADERS).status_code == 200


def test_bearer_token_is_accepted(bench):
    """``Authorization: Bearer <key>`` 也接受。"""
    client, *_ = bench

    response = client.get("/servers", headers={"Authorization": f"Bearer {KEY}"})

    assert response.status_code == 200


def test_bearer_scheme_is_case_insensitive(bench):
    """Bearer 大小写不敏感(curl / 浏览器行为不一致,这里放宽)。"""
    client, *_ = bench

    assert client.get("/servers", headers={"Authorization": f"bearer {KEY}"}).status_code == 200


def test_bearer_without_scheme_is_accepted(bench):
    """直接塞裸密钥也认。"""
    client, *_ = bench

    assert client.get("/servers", headers={"Authorization": KEY}).status_code == 200


def test_empty_configured_key_rejects_everything(tmp_path):
    """没配置密钥时 fail-closed:任何请求都 401(而不是放行)。"""
    config = _config(tmp_path, api_key="")
    store = Store(config.db_path)
    store.init()
    engine = Engine(store, FakeProvisioner(), config, archives=FakeArchiveStore(directory=tmp_path / "a"))
    client = TestClient(create_app(config, engine=engine))

    assert client.get("/servers").status_code == 401
    assert client.get("/servers", headers={"X-API-Key": "whatever"}).status_code == 401


def test_docs_are_disabled_by_default(bench):
    """默认不暴露 Swagger UI 与 openapi.json(它们不校验密钥,干脆不开)。"""
    client, *_ = bench

    assert client.get("/openapi.json").status_code == 404
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json", headers=HEADERS).status_code == 404


def test_docs_can_be_enabled_explicitly(tmp_path):
    """[api] docs = true 时才能访问,方便内网调试。"""
    config = _config(tmp_path, api_docs=True)
    client, *_ = _bench(tmp_path, config)

    assert client.get("/openapi.json").status_code == 200
    assert client.get("/docs").status_code == 200


def test_unauthorized_response_advertises_bearer(bench):
    """401 带上 ``WWW-Authenticate``,方便客户端知道该怎么带密钥。"""
    client, *_ = bench

    assert client.get("/servers").headers.get("www-authenticate") == "Bearer"


# ------------------------------------------------------------------ 基础接口
def test_healthz(bench):
    """健康检查:返回版本与数量统计。"""
    client, *_ = bench

    payload = client.get("/healthz", headers=HEADERS).json()

    assert payload["status"] == "ok"
    assert payload["servers"] == 0
    assert payload["archives"] == 0
    assert payload["lifecycle_enabled"] is False


def test_list_servers_starts_empty(bench):
    """初始状态没有实例。"""
    client, *_ = bench

    assert client.get("/servers", headers=HEADERS).json() == []


def test_create_server(bench):
    """创建实例:201 + 完整信息(含连接地址)。"""
    client, *_ = bench

    response = _create(client, "my server", memory="1G", version="1.20.4", type="fabric")

    assert response.status_code == 201
    payload = response.json()
    assert payload["name"] == "my server"
    assert payload["slug"] == "my-server"
    assert payload["state"] == "running"
    assert payload["endpoint"] == "my-server.mc.loc:25565"
    assert payload["memory_mb"] == 1024
    assert payload["mc_version"] == "1.20.4"
    assert payload["server_type"] == "fabric"
    assert payload["volume_name"] == "mc-my-server-data"


def test_create_server_falls_back_to_defaults(bench):
    """请求里没给的字段取配置文件 defaults 段。"""
    client, *_ = bench

    payload = _create(client).json()

    assert payload["server_type"] == "paper"
    assert payload["mc_version"] == "1.21"
    assert payload["memory_mb"] == 2048
    assert payload["online_mode"] is True


def test_create_server_rejects_bad_memory(bench):
    """内存写法不合法 → 400(配置层的错误,不是 500)。"""
    client, *_ = bench

    response = _create(client, memory="大一点")

    assert response.status_code == 400


def test_create_server_validates_name(bench):
    """名字必须能派生出 slug(至少一个字母/数字),否则 422。"""
    client, *_ = bench

    assert _create(client, "!!!").status_code == 422
    assert _create(client, "").status_code == 422


def test_create_duplicate_is_conflict(bench):
    """同名重复创建 → 409。"""
    client, *_ = bench
    _create(client)

    assert _create(client).status_code == 409


def test_get_server_includes_players(bench):
    """详情默认顺带探测在线人数。"""
    client, *_ = bench
    _create(client)

    payload = client.get("/servers/test", headers=HEADERS).json()

    assert payload["players"] == 0
    assert client.get("/servers/test?players=false", headers=HEADERS).json()["players"] is None


def test_get_unknown_server_is_404(bench):
    """不存在的实例 → 404。"""
    client, *_ = bench

    assert client.get("/servers/nope", headers=HEADERS).status_code == 404


def test_stop_and_start(bench):
    """启停接口都会回写状态。"""
    client, *_ = bench
    _create(client)

    assert client.post("/servers/test/stop", headers=HEADERS).json()["state"] == "stopped"
    assert client.post("/servers/test/start", headers=HEADERS).json()["state"] == "running"


def test_logs_endpoint(bench):
    """日志接口返回文本与行数。"""
    client, *_ = bench
    _create(client)

    payload = client.get("/servers/test/logs?tail=50", headers=HEADERS).json()

    assert payload["name"] == "test"
    assert payload["lines"] == 50
    assert payload["logs"] == "fake logs"


def test_logs_validates_tail_range(bench):
    """tail 越界 → 422(交给 pydantic 校验,不用自己写)。"""
    client, *_ = bench
    _create(client)

    assert client.get("/servers/test/logs?tail=0", headers=HEADERS).status_code == 422


# ------------------------------------------------------------ 删除与存档
def test_delete_server_archives(bench):
    """删除会先归档:响应里带回存档信息与保留时间。"""
    client, engine, _prov, archives, _config_ = bench
    _with_data(engine, archives)

    payload = client.delete("/servers/test", headers=HEADERS).json()

    assert payload["deleted"] is True
    assert payload["archive"]["filename"] == "test.tar.gz"
    assert payload["archive"]["download_url"] == "/servers/test/archive"
    assert payload["archive"]["expires_at"] > payload["archive"]["created_at"]
    assert client.get("/servers", headers=HEADERS).json() == []


def test_delete_unknown_server_is_404(bench):
    """删不存在的实例 → 404。"""
    client, *_ = bench

    assert client.delete("/servers/nope", headers=HEADERS).status_code == 404


def test_delete_is_idempotent_after_archiving(bench):
    """已经被删除过的实例再删一次不报错,直接返回那份存档。"""
    client, engine, _prov, archives, _config_ = bench
    _with_data(engine, archives)
    first = client.delete("/servers/test", headers=HEADERS).json()

    response = client.delete("/servers/test", headers=HEADERS)

    assert response.status_code == 200
    payload = response.json()
    assert payload["deleted"] is False
    assert payload["archive"]["filename"] == first["archive"]["filename"]


def test_delete_archived_name_suggests_purge(bench):
    """只剩存档(实例记录已不在)时删除 → 409,提示改用 DELETE /archives/{name}。"""
    client, engine, _prov, archives, _config_ = bench
    _with_data(engine, archives)
    client.delete("/servers/test", headers=HEADERS)
    engine.store.delete("test")  # 模拟实例记录先没了,但存档还在

    response = client.delete("/servers/test", headers=HEADERS)

    assert response.status_code == 409
    assert "/archives/test" in response.json()["detail"]


def test_create_is_blocked_while_archived(bench):
    """存档保留期内同名创建 → 409(名字被存档占用)。"""
    client, engine, _prov, archives, _config_ = bench
    _with_data(engine, archives)
    client.delete("/servers/test", headers=HEADERS)

    assert _create(client).status_code == 409


def test_deleted_instance_disappears_from_instance_endpoints(bench):
    """删除后实例本身就不存在了:详情/日志/启停都是 404,只有存档类接口认得这个名字。

    删除时会保留一条 ``state=destroyed`` 的记录来占住 slug 与数据卷名,所以这些接口
    不能直接把 DB 里的记录当成"实例还在",否则会返回一个和 ``GET /servers`` 矛盾的 200。
    """
    client, engine, _prov, archives, _config_ = bench
    _with_data(engine, archives)
    assert client.delete("/servers/test", headers=HEADERS).status_code == 200

    assert client.get("/servers/test", headers=HEADERS).status_code == 404
    assert client.get("/servers/test?players=true", headers=HEADERS).status_code == 404
    assert client.get("/servers/test/logs", headers=HEADERS).status_code == 404
    assert client.post("/servers/test/start", headers=HEADERS).status_code == 404
    assert client.post("/servers/test/stop", headers=HEADERS).status_code == 404
    # 存档接口照常可用
    assert client.get("/archives/test", headers=HEADERS).status_code == 200
    assert client.get("/servers/test/archive", headers=HEADERS).status_code == 200


def test_download_archive_after_deletion(bench):
    """实例删除后依然能按名字下载存档(核心需求之一)。"""
    client, engine, _prov, archives, _config_ = bench
    _with_data(engine, archives)
    client.delete("/servers/test", headers=HEADERS)

    response = client.get("/servers/test/archive", headers=HEADERS)

    assert response.status_code == 200
    assert response.content == b"world-data"
    assert response.headers["content-type"] == "application/gzip"
    assert 'filename="test.tar.gz"' in response.headers["content-disposition"]


def test_download_archive_from_live_instance(bench):
    """实例还在:没有存档就现场打包(不停止实例)。"""
    client, engine, _prov, archives, _config_ = bench
    _with_data(engine, archives)

    response = client.get("/servers/test/archive", headers=HEADERS)

    assert response.status_code == 200
    assert engine.get("test").state is State.running


def test_download_archive_for_unknown_name_is_404(bench):
    """既没实例也没存档 → 404。"""
    client, *_ = bench

    assert client.get("/servers/nope/archive", headers=HEADERS).status_code == 404


def test_pack_archive_endpoint(bench):
    """显式打包接口(不删除实例)。"""
    client, engine, _prov, archives, _config_ = bench
    _with_data(engine, archives)

    payload = client.post("/servers/test/archive", headers=HEADERS).json()

    assert payload["filename"] == "test.tar.gz"
    assert engine.get("test").state is State.running


def test_list_and_get_archives(bench):
    """存档列表与详情。"""
    client, engine, _prov, archives, _config_ = bench
    _with_data(engine, archives)
    client.delete("/servers/test", headers=HEADERS)

    assert [item["name"] for item in client.get("/archives", headers=HEADERS).json()] == ["test"]
    assert client.get("/archives/test", headers=HEADERS).json()["slug"] == "test"
    assert client.get("/archives/nope", headers=HEADERS).status_code == 404


def test_purge_archive_frees_name(bench):
    """丢弃存档 → 204,名字重新可用。"""
    client, engine, _prov, archives, _config_ = bench
    _with_data(engine, archives)
    client.delete("/servers/test", headers=HEADERS)

    assert client.delete("/archives/test", headers=HEADERS).status_code == 204
    assert client.get("/archives", headers=HEADERS).json() == []
    assert _create(client).status_code == 201


# ------------------------------------------------------------------ 重建
def test_restore_server(bench):
    """用存档重建:数据回来,存档被消费掉。"""
    client, engine, _prov, archives, _config_ = bench
    _with_data(engine, archives)
    client.delete("/servers/test", headers=HEADERS)

    response = client.post("/servers/test/restore", headers=HEADERS)

    assert response.status_code == 200
    assert response.json()["state"] == "running"
    assert archives.files["mc-test-data"] == "world-data"
    assert client.get("/archives/test", headers=HEADERS).status_code == 404


def test_restore_allows_overrides(bench):
    """重建时可以在请求体里覆盖配置。"""
    client, engine, _prov, archives, _config_ = bench
    _with_data(engine, archives)
    client.delete("/servers/test", headers=HEADERS)

    response = client.post(
        "/servers/test/restore", json={"memory": "3G", "online_mode": False}, headers=HEADERS
    )

    assert response.json()["memory_mb"] == 3072
    assert response.json()["online_mode"] is False


def test_restore_without_archive_is_404(bench):
    """没有存档 → 404。"""
    client, *_ = bench

    assert client.post("/servers/nope/restore", headers=HEADERS).status_code == 404


# ------------------------------------------------------------- 生命周期
def test_lifecycle_endpoint_reports_policy(bench):
    """策略接口把四条规则与保留时长都列出来。"""
    client, *_ = bench

    payload = client.get("/lifecycle", headers=HEADERS).json()

    assert payload["enabled"] is False
    assert payload["join_grace_minutes"] == 10
    assert payload["idle_minutes"] == 60
    assert payload["max_lifetime_minutes"] == 1440
    assert payload["stopped_minutes"] == 1440
    assert payload["archive_retention_minutes"] == 1440


def test_manual_reap_endpoint(bench):
    """手动巡检接口返回回收/清理/错误三份结果。"""
    client, *_ = bench

    payload = client.post("/lifecycle/reap", headers=HEADERS).json()

    assert payload == {"purged": [], "reaped": {}, "errors": {}}


def test_reap_endpoint_collects_expired_archives(tmp_path):
    """到期存档会被巡检清理,并出现在 purged 里(巡检开启时)。"""
    config = _config(tmp_path, lifecycle=LifecyclePolicy(enabled=True, interval_seconds=3600))
    client, engine, _prov, archives, _config_ = _bench(tmp_path, config)
    saved = archives.directory / "old.tar.gz"
    saved.write_text("old", encoding="utf-8")
    engine.store.save_archive(
        new_archive(
            name="old",
            slug="old",
            path=saved,
            size_bytes=3,
            server_type="paper",
            mc_version="1.21",
            memory_mb=1024,
            online_mode=True,
            java=None,
            retention_minutes=60,
            created=datetime.now(timezone.utc) - timedelta(hours=5),
        )
    )

    payload = client.post("/lifecycle/reap", headers=HEADERS).json()

    assert payload["purged"] == ["old"]
    assert not saved.exists()
