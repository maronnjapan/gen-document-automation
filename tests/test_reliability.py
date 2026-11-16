"""通知の重複・通知送信の失敗・LLM異常・外部サービス異常・タイムアウト時の挙動の検証。"""
import json

import pytest
from harness import ISSUE, MAX_RECEIVES, c, deliver, drain, new_job, queue_url, receive


def fail(exc):
    raise exc


def test_duplicate_send_is_deduplicated_by_fifo(world, client):
    jid = new_job(client)
    client.post(f"/api/jobs/{jid}/enqueue")  # 同じ通知をもう一度送る（再送ボタンの二度押しなど）
    drain()
    assert c.job(jid)["status"] == "completed" and world.kinds().count("generate") == 1 and world.kinds().count("review") == 1


def test_redelivered_notification_is_not_processed_twice(world, client):
    world.reviews = [[ISSUE]]
    jid = new_job(client)
    drain()
    j, calls = c.job(jid), len(world.calls)
    assert (j["status"], j["revision"], j["review_count"]) == ("completed", 2, 2)

    # 重複排除の期間を過ぎてから、処理済みの通知が再配信された場合
    for queue, rev in (("generation", 1), ("generation", 2), ("review", 1), ("review", 2)):
        c.SQS.send_message(QueueUrl=queue_url(queue), MessageBody=json.dumps({"job_id": jid, "revision": rev, "event": queue}),
                           MessageGroupId=jid, MessageDeduplicationId=f"late-{queue}-{rev}")
    drain()
    assert c.job(jid) == j and len(world.calls) == calls and len(world.slack) == 1  # LLM呼び出し・通知・状態はそのまま


def test_queue_send_failure_is_retried(world, client, monkeypatch):
    jid = new_job(client)
    send, failures = c.SQS.send_message, iter([True])
    flaky = lambda **kw: fail(ConnectionError("SQS unavailable")) if "review" in kw["QueueUrl"] and next(failures, False) else send(**kw)  # noqa: E731
    monkeypatch.setattr(c.SQS, "send_message", flaky)
    assert deliver("generation", receive("generation")[0]) is False  # 生成・Redis保存は完了し、通知だけ失敗 → 再配信させる
    assert (c.job(jid)["revision"], c.job(jid)["status"], c.job(jid)["error"]) == (1, "reviewing", "ConnectionError: SQS unavailable")
    drain()  # 再配信された通知では生成し直さず、保存イベントの送信だけをやり直す
    assert c.job(jid)["status"] == "completed" and world.kinds().count("generate") == 1


def test_api_send_failure_can_be_resent(world, client, monkeypatch):
    jid = client.post("/api/jobs", json={"name": "doc", "instruction": "x"}).json()["job_id"]  # Redisへ保存
    send = c.SQS.send_message
    monkeypatch.setattr(c.SQS, "send_message", lambda **kw: fail(ConnectionError("SQS unavailable")))
    with pytest.raises(ConnectionError):
        client.post(f"/api/jobs/{jid}/enqueue")
    assert c.job(jid)["status"] == "queued" and receive("generation") == []  # ジョブは保存済み、通知は未送信

    monkeypatch.setattr(c.SQS, "send_message", send)
    assert client.post(f"/api/jobs/{jid}/enqueue").status_code == 200  # Web画面の「生成依頼を再送」
    drain()
    assert c.job(jid)["status"] == "completed"
    assert client.post(f"/api/jobs/{jid}/enqueue").json()["status"] == "completed" and receive("generation") == []  # 待機中以外は何もしない


def test_invalid_llm_output_is_retried_and_then_reported(world, client):
    world.llm_hook = lambda kind, user: "レビューできませんでした" if kind == "review" else None
    jid = new_job(client)
    drain()
    j = c.job(jid)
    # レビュー不能は正常完了扱いにしない: 受信回数の上限まで再試行した後、DLQ経由で異常終了として通知
    assert j["status"] == "failed" and j["error"].startswith("ValidationError") and world.kinds().count("review") == MAX_RECEIVES
    assert len(world.slack) == 1 and "異常終了: ValidationError" in world.slack[0] and jid in world.slack[0]


def test_invalid_llm_output_recovers_on_retry(world, client):
    outputs = iter(['```json\n{"issues": "なし"}\n```'])  # 1回目は形式不正、2回目以降は正常
    world.llm_hook = lambda kind, user: next(outputs, None) if kind == "review" else None
    jid = new_job(client)
    drain()
    assert c.job(jid)["status"] == "completed" and world.kinds().count("review") == 2


def test_llm_failure_is_reported_and_human_can_restart(world, client):
    world.status = {"llm.test": 503}  # LLMエンドポイントの障害（HTTPの再試行を使い切った状態）
    jid = new_job(client)
    drain()
    j = c.job(jid)
    assert j["status"] == "failed" and j["error"].startswith("HTTPError: 503") and "異常終了" in world.slack[0]

    world.status = {}  # 復旧後、人間の修正指示で再開できる
    assert client.post(f"/api/jobs/{jid}/revise", json={"comment": "再実行"}).status_code == 200
    drain()
    assert c.job(jid)["status"] == "completed" and c.job(jid)["error"] is None


def test_confluence_error_is_reported(world, client):
    world.status = {"wiki.test": 401}
    jid = new_job(client)
    drain()
    assert c.job(jid)["status"] == "failed" and "401" in c.job(jid)["error"] and "異常終了" in world.slack[0]


def test_timeout_is_reported_from_dead_letter_queue(world, client):
    jid = new_job(client)
    m = receive("generation")[0]  # Lambdaがタイムアウトで止まり、通知が削除されないまま上限を超えた場合
    c.dead_letter({"Records": [{"messageId": m["MessageId"], "body": m["Body"]}]})
    assert c.job(jid)["status"] == "failed" and "異常終了: タイムアウト等" in world.slack[0]


def test_slack_failure_does_not_break_job(world, client):
    world.status = {"slack.test": 500}
    jid = new_job(client)
    drain()
    assert c.job(jid)["status"] == "completed"
