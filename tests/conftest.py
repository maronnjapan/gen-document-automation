"""pytestフィクスチャ。外部サービスはすべて harness のフェイクに置き換える。"""
import fakeredis
import pytest
import requests
from fastapi.testclient import TestClient
from harness import World, api, c, mock_aws


@pytest.fixture
def world(monkeypatch):
    with mock_aws():
        c.S3.create_bucket(Bucket=c.BUCKET, CreateBucketConfiguration={"LocationConstraint": "ap-northeast-1"})
        monkeypatch.setattr(c, "QUEUES", {q: c.SQS.create_queue(QueueName=f"{q}-queue")["QueueUrl"] for q in ("generation", "review")})
        monkeypatch.setattr(c, "R", fakeredis.FakeRedis(decode_responses=True))
        w = World()
        monkeypatch.setattr(requests, "request", w.request)
        yield w


@pytest.fixture
def client(world):
    return TestClient(api.app)
