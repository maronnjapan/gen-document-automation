"""通知の重複・通知送信失敗・LLM異常・タイムアウト時の挙動の検証。"""
import json
import time

import pytest
import requests
from harness import ISSUE, Context, c, deliver, drain, new_job, receive


def fail(exc):
    raise exc


def test_duplicate_notifications_are_processed_once(world, client):
    world.reviews = [[ISSUE]]
    jid = new_job(client)
    body = receive("generation")[0]["Body"]
    for _ in range(2):  # 同じ生成通知が重複して届く
        c.SQS.send_message(QueueUrl=c.QUEUES["generation"], MessageBody=body)
    drain()
    j = c.job(jid)
    assert (j["status"], j["revision"], j["review_count"]) == ("completed", 2, 2)
    assert world.kinds().count("generate") == 1 and world.kinds().count("revise") == 1 and world.kinds().count("review") == 2

    # 完了後に古い保存イベント・生成通知が再配信されても状態は変わらない
    for queue, rev in (("review", 1), ("review", 2), ("generation", 2)):
        c.SQS.send_message(QueueUrl=c.QUEUES[queue], MessageBody=json.dumps({"job_id": jid, "revision": rev, "event": "x"}))
    drain()
    assert c.job(jid) == j and world.kinds().count("review") == 2 and len(world.slack) == 1


def test_concurrent_duplicate_is_retried_later(world, client):
    jid = new_job(client)
    c.R.set(f"lock:generation:{jid}:1", 1)  # 同じ通知を別の実行が処理中
    m = receive("generation")[0]
    assert deliver("generation", m) is False and world.calls == []  # 処理せず、後で再確認させる
    c.R.delete(f"lock:generation:{jid}:1")
    drain()
    assert c.job(jid)["status"] == "completed" and world.kinds().count("generate") == 1


def test_queue_send_failure_is_retried_from_outbox(world, client, monkeypatch):
    jid = new_job(client)
    send, failures = c.SQS.send_message, iter([True])

    def flaky_send(**kw):  # Redis保存後の通知送信が1回だけ失敗する
        if "review" in kw["QueueUrl"] and next(failures, False):
            raise ConnectionError("SQS unavailable")
        return send(**kw)

    monkeypatch.setattr(c.SQS, "send_message", flaky_send)
    assert deliver("generation", receive("generation")[0]) is False  # 生成・保存は完了、通知だけ失敗 → 再試行
    j = c.job(jid)
    assert (j["revision"], j["status"]) == (1, "reviewing") and c.R.hlen(f"outbox:{jid}") == 1
    drain()  # 再配信された生成通知で未送信の通知を送り直す（LLMで再生成はしない）
    assert c.job(jid)["status"] == "completed" and world.kinds().count("generate") == 1 and c.R.hlen(f"outbox:{jid}") == 0


def test_api_notification_failure_can_be_resent(world, client, monkeypatch):
    send = c.SQS.send_message
    monkeypatch.setattr(c.SQS, "send_message", lambda **kw: fail(ConnectionError("SQS unavailable")))
    with pytest.raises(ConnectionError):
        client.post("/api/jobs", json={"name": "doc", "instruction": "x"})
    jid = client.get("/api/jobs").json()[0]["job_id"]
    assert client.get(f"/api/jobs/{jid}").json()["pending_notifications"] == 1  # ジョブは保存済み、通知は未送信

    monkeypatch.setattr(c.SQS, "send_message", send)
    assert client.post(f"/api/jobs/{jid}/resend").json()["pending_notifications"] == 0
    drain()
    assert c.job(jid)["status"] == "completed"


def test_invalid_llm_output_is_retried_and_then_reported(world, client):
    world.llm_hook = lambda kind, user: "レビューできませんでした" if kind == "review" else None
    jid = new_job(client)
    drain()
    j = c.job(jid)
    # レビュー不能は正常完了扱いにしない: 再依頼(3回)×再試行(3回)の後、異常終了として通知
    assert j["status"] == "failed" and "ValueError" in j["error"] and "review" in j["error"]
    assert world.kinds().count("review") == c.TRIES * c.ATTEMPTS
    assert "指定形式のJSONだけを出力してください" in world.prompts("review")[1]
    assert len(world.slack) == 1 and "*[異常終了]*" in world.slack[0] and "エラー: review: ValueError" in world.slack[0]


def test_invalid_llm_output_recovers_on_retry(world, client):
    outputs = iter(["```json\n{\"issues\": \"なし\"}\n```"])  # 1回目は形式不正、2回目以降は正常
    world.llm_hook = lambda kind, user: next(outputs, None) if kind == "review" else None
    jid = new_job(client)
    drain()
    assert c.job(jid)["status"] == "completed" and world.kinds().count("review") == 2


def test_llm_connection_failure_is_retried_and_then_reported(world, client):
    def down(kind, user):
        raise requests.ConnectionError("LLM endpoint unreachable")

    world.llm_hook = down
    jid = new_job(client)
    drain()
    j = c.job(jid)
    assert j["status"] == "failed" and "ConnectionError" in j["error"] and "generation" in j["error"]
    assert len(world.calls) == c.TRIES * c.ATTEMPTS and "異常終了" in world.slack[0]

    world.llm_hook = None  # 復旧後、人間の再修正指示で再開できる
    assert client.post(f"/api/jobs/{jid}/revise", json={"comment": "再実行"}).status_code == 200
    drain()
    assert c.job(jid)["status"] == "completed" and c.job(jid)["error"] is None


def test_confluence_error_is_reported(world, client, monkeypatch):
    request = world.request

    def broken(method, url, **kw):
        return request(method, url, **kw) if "wiki.test" not in url else _error(url)

    def _error(url):
        res = requests.Response()
        res.status_code, res.url, res.reason, res._content = 503, url, "Service Unavailable", b"{}"
        return res

    monkeypatch.setattr(requests, "request", broken)
    jid = new_job(client)
    drain()
    assert c.job(jid)["status"] == "failed" and "HTTPError" in c.job(jid)["error"] and "異常終了" in world.slack[0]


def test_processing_timeout_is_reported(world, client):
    world.llm_hook = lambda kind, user: time.sleep(3)
    jid = new_job(client)
    m = receive("generation")[0]
    m["Attributes"]["ApproximateReceiveCount"] = str(c.ATTEMPTS)  # 最後の試行
    deliver("generation", m, Context(21))  # 残り21秒 → 1秒後に打ち切る
    j = c.job(jid)
    assert j["status"] == "failed" and "Deadline" in j["error"] and not c.R.exists(f"lock:generation:{jid}:1")
    assert "異常終了" in world.slack[0]


def test_slack_failure_does_not_break_job(world, client, monkeypatch):
    request = world.request
    monkeypatch.setattr(requests, "request", lambda m, url, **kw: fail(requests.ConnectionError()) if "slack" in url else request(m, url, **kw))
    jid = new_job(client)
    drain()
    assert c.job(jid)["status"] == "completed"
