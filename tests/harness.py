"""テスト用ハーネス。

Redis=fakeredis、S3/SQS(FIFO+DLQ)=moto、外部HTTP（独自LLM・Confluence・Slack）=フェイクに置き換え、外部サービスなしで
「API → generation-queue → 生成 → review-queue → レビュー → …」の流れと、DLQ 経由の異常終了を動かす。
"""

import json
import os
import pathlib
import sys
from urllib.parse import parse_qs, urlparse

import requests
from moto import mock_aws  # noqa: F401  boto3クライアントの生成より先に読み込む

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
for k in [
    k for k in os.environ if k.startswith(("AWS_ENDPOINT_URL", "AWS_PROFILE", "LLM_", "CONFLUENCE_", "APP_ROOT"))
]:
    del os.environ[k]
os.environ.update(
    AWS_DEFAULT_REGION="ap-northeast-1",
    AWS_ACCESS_KEY_ID="test",
    AWS_SECRET_ACCESS_KEY="test",
    S3_BUCKET="test-docs",
    LLM_URL="http://llm.test/v1/generate",
    LLM_API_KEY="Bearer llm-secret",
    CONFLUENCE_URL="http://wiki.test/wiki",
    CONFLUENCE_AUTH="Bearer wiki-token",
    SLACK_WEBHOOK_URL="http://slack.test/hook",
    APP_URL="http://app.test",
)

import api  # noqa: E402,F401  conftest から利用
import common  # noqa: E402
import generator  # noqa: E402
import reviewer  # noqa: E402

KINDS = {
    generator.PROMPTS["plan"]: "plan",
    generator.PROMPTS["skills"]: "generation_skills",
    generator.PROMPTS["generate"]: "generate",
    generator.PROMPTS["revise"]: "revise",
    reviewer.PROMPTS["skills"]: "review_skills",
    reviewer.PROMPTS["review"]: "review",
}
ISSUE = {
    "severity": "high",
    "location": "概要",
    "comment": "目的が書かれていない",
    "suggestion": "冒頭に目的を追記する",
}
MAX_RECEIVES = 3  # キューから DLQ へ移すまでの受信回数（本番の maxReceiveCount と同じ）


def response(request, status, data):
    res = requests.Response()
    res.status_code, res.url, res.request, res.reason = status, request.url, request, "OK" if status < 400 else "ERROR"
    res._content = (data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)).encode()
    res.encoding = "utf-8"
    return res


class World(requests.adapters.BaseAdapter):
    """1テスト分の外部サービス（LLMの応答台本、Confluenceのページ、Slackの受信記録）。HTTPセッションのアダプターとして差し込む。"""

    def __init__(self):
        super().__init__()
        self.pages = {
            "101": {
                "title": "決済APIの仕様",
                "version": 1,
                "html": "<h1>決済API</h1><p>決済APIはPOST /paymentsで支払いを作成します。</p>",
            }
        }
        self.reviews = []  # レビューLLMが順に返す指摘リスト。尽きたら指摘なし
        self.always_issue = False  # True なら毎回指摘を返す（上限到達の検証用）
        self.skill_choice = {}  # "generation_skills" / "review_skills" -> LLMが選ぶSkill名
        self.llm_hook = None  # 異常系: (kind, user) -> 応答テキスト（例外送出も可）。Noneを返せば通常の応答
        self.status = {}  # URLの一部 -> 返すHTTPステータス（異常系）
        self.calls, self.requests, self.slack = [], [], []

    def llm(self, system, user):
        kind = KINDS[system]
        self.calls.append((kind, user))
        if self.llm_hook and (out := self.llm_hook(kind, user)) is not None:
            return out
        if kind == "plan":
            plan = {"instruction": "決済APIの利用手順書を作る", "outline": ["概要", "手順"], "queries": ["決済API"]}
            return f"はい。\n```json\n{json.dumps(plan, ensure_ascii=False)}\n```"
        if kind.endswith("skills"):
            return json.dumps(self.skill_choice.get(kind, []))
        if kind == "generate":
            return "```markdown\n# 決済API利用手順書\n\n初版の本文\n```"
        if kind == "revise":
            return f"# 決済API利用手順書\n\n修正版 {sum(k == 'revise' for k, _ in self.calls)}"
        issues = [ISSUE] if self.always_issue else (self.reviews.pop(0) if self.reviews else [])
        return json.dumps({"summary": "総評", "issues": issues}, ensure_ascii=False)

    def send(self, request, **kwargs):
        self.requests.append(request)
        for part, status in self.status.items():
            if part in request.url:
                return response(request, status, {"message": "error"})
        if request.url.startswith(os.environ["LLM_URL"]):
            body = json.loads(request.body)
            return response(request, 200, {"output": self.llm(body["system"], body["prompt"])})
        if request.url.startswith(os.environ["CONFLUENCE_URL"]):
            self.cql = parse_qs(urlparse(request.url).query)["cql"][0]
            return response(
                request,
                200,
                {
                    "results": [
                        {
                            "id": pid,
                            "title": p["title"],
                            "version": {"number": p["version"], "when": "2026-11-01T00:00:00Z"},
                            "body": {"storage": {"value": p["html"]}},
                        }
                        for pid, p in self.pages.items()
                    ]
                },
            )
        if request.url.startswith(os.environ["SLACK_WEBHOOK_URL"]):
            self.slack.append(json.loads(request.body)["text"])
            return response(request, 200, "ok")
        raise AssertionError(f"unexpected request: {request.url}")

    def close(self):
        pass

    def kinds(self):
        return [k for k, _ in self.calls]

    def prompts(self, kind):
        return [u for k, u in self.calls if k == kind]


def create_queues():
    urls = {}
    for name in ("generation", "review"):
        dlq = common.sqs.create_queue(QueueName=f"{name}-dlq.fifo", Attributes={"FifoQueue": "true"})["QueueUrl"]
        arn = common.sqs.get_queue_attributes(QueueUrl=dlq, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
        redrive = json.dumps({"deadLetterTargetArn": arn, "maxReceiveCount": str(MAX_RECEIVES)})
        urls[name] = common.sqs.create_queue(
            QueueName=f"{name}-queue.fifo", Attributes={"FifoQueue": "true", "RedrivePolicy": redrive}
        )["QueueUrl"]
        urls[f"{name}-dlq"] = dlq
    return urls


HANDLERS = {
    "generation": lambda e: generator.handler(e),
    "review": lambda e: reviewer.handler(e),
    "generation-dlq": common.dead_letter_handler,
    "review-dlq": common.dead_letter_handler,
}


def queue_url(queue):
    return os.environ[f"{queue.upper().replace('-', '_')}_QUEUE_URL"]


def receive(queue):
    return common.sqs.receive_message(QueueUrl=queue_url(queue), MaxNumberOfMessages=1).get("Messages", [])


def deliver(queue, m):
    """Lambda のイベントソースマッピングと同じく、成功したら削除し、例外なら再配信させる。

    受信回数の上限を超えた通知は、moto が DLQ へ移す。
    """
    try:
        HANDLERS[queue]({"Records": [{"messageId": m["MessageId"], "body": m["Body"]}]})
    except Exception:
        common.sqs.change_message_visibility(
            QueueUrl=queue_url(queue), ReceiptHandle=m["ReceiptHandle"], VisibilityTimeout=0
        )
        return False
    common.sqs.delete_message(QueueUrl=queue_url(queue), ReceiptHandle=m["ReceiptHandle"])
    return True


def drain(limit=300):
    """すべてのキューが空になるまで通知を処理する。"""
    for _ in range(limit):
        delivered = False
        for queue in HANDLERS:
            for m in receive(queue):
                deliver(queue, m)
                delivered = True
        if not delivered:
            return
    raise AssertionError("キューが空になりませんでした")


def new_job(client, **kw):
    """新規生成画面と同じく、ジョブを作成（Redisへ保存）してから生成依頼を送信する。"""
    res = client.post(
        "/api/jobs",
        json={"name": "決済API利用手順書", "instruction": "決済APIの使い方を説明する", "viewpoints": "正確性", **kw},
    )
    assert res.status_code == 200 and res.json()["status"] == "queued", res.text
    jid = res.json()["job_id"]
    assert client.post(f"/api/jobs/{jid}/enqueue").status_code == 200
    return jid
