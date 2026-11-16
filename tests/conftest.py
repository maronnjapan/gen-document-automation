"""pytestフィクスチャ。外部サービスはすべて harness のフェイクに置き換える。"""
import fakeredis
import pytest
from fastapi.testclient import TestClient
from harness import World, api, c, create_queues, mock_aws


@pytest.fixture
def world(monkeypatch):
    with mock_aws():
        c.S3.create_bucket(Bucket=c.BUCKET, CreateBucketConfiguration={"LocationConstraint": "ap-northeast-1"})
        for name, url in create_queues().items():
            monkeypatch.setenv(f"{name.upper().replace('-', '_')}_QUEUE_URL", url)
        monkeypatch.setattr(c, "R", fakeredis.FakeRedis(decode_responses=True))
        w = World()
        monkeypatch.setitem(c.HTTP.adapters, "http://", w)  # 外部HTTPはすべてフェイクへ
        yield w


@pytest.fixture
def client(world):
    return TestClient(api.app)
