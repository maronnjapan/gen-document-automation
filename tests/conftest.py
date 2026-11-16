"""pytestフィクスチャ。外部サービスはすべて harness のフェイクに置き換える。"""

import fakeredis
import pytest
from fastapi.testclient import TestClient
from harness import World, api, common, create_queues, mock_aws


@pytest.fixture
def world(monkeypatch):
    with mock_aws():
        common.s3.create_bucket(
            Bucket=common.BUCKET, CreateBucketConfiguration={"LocationConstraint": "ap-northeast-1"}
        )
        for name, url in create_queues().items():
            monkeypatch.setenv(f"{name.upper().replace('-', '_')}_QUEUE_URL", url)
        monkeypatch.setattr(common, "redis_client", fakeredis.FakeRedis(decode_responses=True))
        fake_services = World()
        monkeypatch.setitem(common.http_session.adapters, "http://", fake_services)  # 外部HTTPはすべてフェイクへ
        yield fake_services


@pytest.fixture
def client(world):
    return TestClient(api.app)
