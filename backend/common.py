"""共通処理: 設定・Redis（ジョブ状態）・S3（成果物）・SQS（通知）・独自LLM・Skills・Slack・Lambdaハンドラー。"""
import datetime, json, logging, os, pathlib, re
import boto3, frontmatter, jmespath, pydantic, redis, requests, urllib3

E = lambda key, default=None: os.environ.get(key) or default  # noqa: E731  空文字は未設定として扱う
ROOT = pathlib.Path(E("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])  # prompts/ skills/ frontend/ の親
R = redis.Redis.from_url(E("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)
S3, SQS, BUCKET = boto3.client("s3"), boto3.client("sqs"), E("S3_BUCKET", "doc-automation")  # 接続先は AWS_ENDPOINT_URL_S3/SQS で変更可
HTTP, RETRY = requests.Session(), urllib3.Retry(3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504], allowed_methods=None)
for scheme in ("http://", "https://"): HTTP.mount(scheme, requests.adapters.HTTPAdapter(max_retries=RETRY))  # LLM・Confluence・Slack: 一時的な失敗は再試行
HTTP.hooks["response"].append(lambda res, *_, **__: res.raise_for_status())
TAGS = r"</?\s*(?:instruction|comment|plan|skills?|task|reference|previous|review|viewpoints|document)\b[^>]*>"  # プロンプトでデータを囲むタグ
now = lambda: datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")  # noqa: E731
dumps = lambda v: json.dumps(v, ensure_ascii=False)  # noqa: E731

def job(jid):  # ジョブ（Redisのフラットなjson）
    if v := R.get(f"job:{jid}"): return json.loads(v)
    raise LookupError(jid)

def save(j, **kw): j.update(kw, updated_at=now()); R.set(f"job:{j['job_id']}", dumps(j)); return j
def put_text(key, text): S3.put_object(Bucket=BUCKET, Key=key, Body=text.encode()); return key
def get_text(key): return S3.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode()

def send(queue, j, rev):  # 通知は識別情報だけ。FIFOキューの重複排除IDで二重送信を除き、同じジョブの通知は順に1件ずつ処理させる
    SQS.send_message(QueueUrl=E(f"{queue.upper()}_QUEUE_URL"), MessageBody=dumps({"job_id": j["job_id"], "revision": rev, "event": queue}),
                     MessageGroupId=j["job_id"], MessageDeduplicationId=f"{j['job_id']}-{j['cycle']}-{rev}")

def call_llm(system_prompt: str, user_prompt: str) -> str:  # 独自LLMエンドポイントの薄いアダプター。本文の形式と応答の位置は設定で合わせる
    body = re.sub(r"\{\{(system|user)\}\}", lambda m: json.dumps({"system": system_prompt, "user": user_prompt}[m[1]])[1:-1],
                  E("LLM_REQUEST_TEMPLATE", '{"system": "{{system}}", "prompt": "{{user}}"}'))
    res = HTTP.post(E("LLM_URL"), data=body.encode(), timeout=float(E("LLM_TIMEOUT", 120)),
                    headers={"Content-Type": "application/json", E("LLM_API_KEY_HEADER", "Authorization"): E("LLM_API_KEY", "")})
    if isinstance(out := jmespath.search(E("LLM_RESPONSE_PATH", "output"), res.json()), str) and out.strip(): return out
    raise ValueError(f"LLMの応答形式が不正です: {res.text[:200]}")

def ask_json(system, user, schema):  # JSONモードを仮定せず、応答からJSONを抜き出して型を検証する（不正なら例外→通知ごと再試行）
    pydantic.TypeAdapter(schema).validate_python(value := json.loads((re.search(r"[\[{].*[\]}]", call_llm(system, user), re.S) or ["null"])[0]))
    return value

def tag(name, text):  # データをタグで囲み、中のタグ記法を消す（Wiki本文などが区切りを破ってシステムプロンプトを上書きしないように）
    return f"<{name}>\n{re.sub(TAGS, '', str(text), flags=re.I)}\n</{name}>\n" if text else ""

def prompts(name):  # prompts/<name>.md の「## 見出し」ごとに、共通の system と連結したシステムプロンプト
    parts = dict(map(str.strip, s.split("\n", 1)) for s in (ROOT / "prompts" / f"{name}.md").read_text(encoding="utf-8").split("\n## ")[1:])
    return {k: parts["system"] + "\n\n" + v for k, v in parts.items() if k != "system"}

def skills(system, keys, task):  # SKILL.mdの名前と説明だけをLLMに示して選ばせ、選ばれたSkillの本文をプロンプト用に返す
    found = {p["name"]: p for p in (frontmatter.loads(get_text(k)) for k in keys) if p.get("name")}
    menu = "\n".join(f"- {n}: {p.get('description', '')}" for n, p in found.items())
    names = ask_json(system, tag("task", task) + tag("skills", menu), list[str]) if found else []
    return "".join(tag("skill", f"# {n}\n{found[n].content}") for n in dict.fromkeys(names) if n in found)

def slack(j, result):  # 完了・上限到達・異常終了の通知。失敗しても状態はRedisに残りWeb画面で確認できるため、ログだけ残す
    text = f"[{result}] {j['name']}\nジョブ {j['job_id']} レビュー {j['review_count']}/{j['max_reviews']}\n{E('APP_URL', '')}/job.html?id={j['job_id']}"
    try: E("SLACK_WEBHOOK_URL") and HTTP.post(E("SLACK_WEBHOOK_URL"), json={"text": text}, timeout=30)
    except requests.RequestException: logging.exception("Slack通知に失敗しました")

def handler(process):  # SQS通知のLambdaハンドラー。失敗はジョブに記録して例外を投げ、SQSの再配信に任せる（上限超過はDLQへ）
    def run(event, context=None):
        for msg in (json.loads(rec["body"]) for rec in event["Records"]):
            try: process(msg)
            except Exception as e: save(job(msg["job_id"]), error=f"{type(e).__name__}: {e}"[:500]); raise
    return run

def dead_letter(event, context=None):  # 再試行の上限を超えた通知（DLQ）: 異常終了として記録し、Slackへ通知する
    for rec in event["Records"]:
        j = save(job(json.loads(rec["body"])["job_id"]), status="failed")
        slack(j, f"異常終了: {j.get('error') or 'タイムアウト等'}")
