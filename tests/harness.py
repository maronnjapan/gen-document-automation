"""テスト用ハーネス。Redis=fakeredis、S3/SQS=moto、外部HTTP（独自LLM・Confluence・Slack）=フェイクに置き換え、
外部サービスなしで「API → generation-queue → 生成 → review-queue → レビュー → …」の一連の流れを動かす。"""
import json
import os
import pathlib
import sys
import time

import requests
from moto import mock_aws  # noqa: F401  boto3クライアントの生成より先に読み込む

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
for k in [k for k in os.environ if k.startswith(("AWS_ENDPOINT_URL", "AWS_PROFILE", "LLM_", "CONFLUENCE_"))]:
    del os.environ[k]
os.environ.update(
    AWS_DEFAULT_REGION="ap-northeast-1", AWS_ACCESS_KEY_ID="test", AWS_SECRET_ACCESS_KEY="test", S3_BUCKET="test-docs",
    RETRY_WAIT="0", LLM_URL="http://llm.test/v1/generate", LLM_API_KEY="llm-secret",
    CONFLUENCE_URL="http://wiki.test/wiki", CONFLUENCE_TOKEN="wiki-token",
    SLACK_WEBHOOK_URL="http://slack.test/hook", APP_URL="http://app.test",
)

import api  # noqa: E402,F401  conftest から利用
import common as c  # noqa: E402
import generator  # noqa: E402
import reviewer  # noqa: E402

KINDS = {generator.P["plan"]: "plan", generator.P["skills"]: "generation_skills", generator.P["generate"]: "generate",
         generator.P["revise"]: "revise", reviewer.P["skills"]: "review_skills", reviewer.P["review"]: "review"}
ISSUE = {"severity": "high", "location": "概要", "comment": "目的が書かれていない", "suggestion": "冒頭に目的を追記する"}


def response(status, data, url):
    res = requests.Response()
    res.status_code, res.url, res.reason = status, url, "OK" if status < 400 else "ERROR"
    res._content = (data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)).encode()
    res.encoding = "utf-8"
    return res


class World:
    """1テスト分の外部サービスの状態（LLMの応答台本、Confluenceのページ、Slackの受信記録）。"""

    def __init__(self):
        self.pages = {"101": {"title": "決済APIの仕様", "version": 1, "html": "<h1>決済API</h1><p>決済APIはPOST /paymentsで支払いを作成します。</p>"}}
        self.reviews = []          # レビューLLMが順に返す指摘リスト。尽きたら指摘なし
        self.always_issue = False  # True なら毎回指摘を返す（上限到達の検証用）
        self.skill_choice = {}     # "generation_skills" / "review_skills" -> LLMが選ぶSkill名
        self.llm_hook = None       # 異常系: (kind, user) -> 応答テキスト（例外送出も可）。Noneなら通常の応答
        self.calls, self.http_log, self.slack = [], [], []

    def llm(self, system, user):
        kind = KINDS[system]
        self.calls.append((kind, user))
        if self.llm_hook and (out := self.llm_hook(kind, user)) is not None:
            return out
        if kind == "plan":
            return 'はい。\n```json\n{"instruction": "決済APIの利用手順書を作る", "outline": ["概要", "手順"], "queries": ["決済API"]}\n```'
        if kind.endswith("skills"):
            return json.dumps(self.skill_choice.get(kind, []))
        if kind == "generate":
            return "```markdown\n# 決済API利用手順書\n\n初版の本文\n```"
        if kind == "revise":
            return f"# 決済API利用手順書\n\n修正版 {sum(k == 'revise' for k, _ in self.calls)}"
        issues = [ISSUE] if self.always_issue else (self.reviews.pop(0) if self.reviews else [])
        return json.dumps({"summary": "総評", "issues": issues}, ensure_ascii=False)

    def request(self, method, url, **kw):
        self.http_log.append((method, url, kw))
        if url == os.environ["LLM_URL"]:
            body = json.loads(kw["data"])
            return response(200, {"output": self.llm(body["system"], body["prompt"])}, url)
        if url.startswith(os.environ["CONFLUENCE_URL"]):
            results = [{"id": pid, "title": p["title"], "version": {"number": p["version"], "when": "2026-11-01T00:00:00Z"},
                        "body": {"storage": {"value": p["html"]}}} for pid, p in self.pages.items()]
            return response(200, {"results": results}, url)
        if url == os.environ["SLACK_WEBHOOK_URL"]:
            self.slack.append(kw["json"]["text"])
            return response(200, "ok", url)
        raise AssertionError(f"unexpected request: {url}")

    def kinds(self):
        return [k for k, _ in self.calls]

    def prompts(self, kind):
        return [u for k, u in self.calls if k == kind]


HANDLERS = {"generation": lambda e, ctx=None: generator.handler(e, ctx), "review": lambda e, ctx=None: reviewer.handler(e, ctx)}


def receive(queue):
    res = c.SQS.receive_message(QueueUrl=c.QUEUES[queue], MaxNumberOfMessages=1, MessageSystemAttributeNames=["ApproximateReceiveCount"])
    return res.get("Messages", [])


def deliver(queue, m, context=None):
    """SQSメッセージを、Lambdaのイベントソースマッピングと同じ形式でハンドラーへ渡す。失敗分は再配信、成功分は削除する。"""
    rec = {"messageId": m["MessageId"], "body": m["Body"], "attributes": m.get("Attributes", {})}
    failed = {f["itemIdentifier"] for f in HANDLERS[queue]({"Records": [rec]}, context)["batchItemFailures"]}
    if m["MessageId"] in failed:
        c.SQS.change_message_visibility(QueueUrl=c.QUEUES[queue], ReceiptHandle=m["ReceiptHandle"], VisibilityTimeout=0)
    else:
        c.SQS.delete_message(QueueUrl=c.QUEUES[queue], ReceiptHandle=m["ReceiptHandle"])
    return not failed


def drain(limit=300):
    """generation-queue と review-queue が空になるまで通知を処理する。"""
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
    res = client.post("/api/jobs", json={"name": "決済API利用手順書", "instruction": "決済APIの使い方を説明する", "viewpoints": "正確性", **kw})
    assert res.status_code == 200, res.text
    return res.json()["job_id"]


class Context:
    """Lambdaのcontext（残り実行時間だけを持つ）。"""

    def __init__(self, seconds):
        self.deadline = time.time() + seconds

    def get_remaining_time_in_millis(self):
        return int((self.deadline - time.time()) * 1000)
