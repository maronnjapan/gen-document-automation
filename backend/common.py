"""生成・レビュー・Web APIの共通処理（設定、Redis、S3、SQS、独自LLM、Skills、Slack、Lambdaハンドラー）。"""
import json, logging, os, pathlib, re, signal, time
from datetime import UTC, datetime

import boto3, redis, requests, yaml
from redis.backoff import ExponentialBackoff
from redis.retry import Retry

E = lambda key, default=None: os.environ.get(key) or default  # noqa: E731  空文字も未設定として扱う
ROOT = pathlib.Path(E("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])  # prompts/ skills/ frontend/ の親
R = redis.Redis.from_url(E("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True, retry=Retry(ExponentialBackoff(), 3))
S3, SQS, BUCKET = boto3.client("s3"), boto3.client("sqs"), E("S3_BUCKET", "doc-automation")  # 接続先は AWS_ENDPOINT_URL_* で変更可
QUEUES = {"generation": E("GENERATION_QUEUE_URL"), "review": E("REVIEW_QUEUE_URL")}
TRIES, WAIT, ATTEMPTS, LOCK_TTL = int(E("RETRY_TRIES", 3)), float(E("RETRY_WAIT", 1)), int(E("MAX_ATTEMPTS", 3)), int(E("LOCK_TTL", 900))
TAGS = "instruction|comment|plan|skills?|task|reference|previous|review|viewpoints|document"  # プロンプト内でデータを囲むタグ
log = logging.getLogger("docgen")
log.setLevel(logging.INFO)
now = lambda: datetime.now(UTC).isoformat(timespec="seconds")  # noqa: E731
dumps = lambda v: json.dumps(v, ensure_ascii=False)  # noqa: E731

class NotFound(Exception): """ジョブ・ファイルが存在しない。"""
class Deadline(BaseException): """Lambdaのタイムアウト直前の打ち切り（except Exception の再試行で握り潰さない）。"""

def retry(fn, *a, **kw):
    """一時的な障害に備え、指数バックオフで再試行する。"""
    for i in range(TRIES):
        try: return fn(*a, **kw)
        except Exception as e:
            if i == TRIES - 1: raise
            log.warning("retry %s: %s", getattr(fn, "__name__", fn), e)
            time.sleep(WAIT * 2 ** i)

def http(method, url, **kw):
    """外部HTTP呼び出し（Confluence・LLM・Slack）。タイムアウト、HTTPエラー検査、再試行をまとめて行う。"""
    def request():
        res = requests.request(method, url, **{"timeout": 60, **kw})
        return res.raise_for_status() or res
    return retry(request)

def job(jid):
    if not (v := R.get(f"job:{jid}")): raise NotFound(f"job {jid}")
    return json.loads(v)

def save(j, **kw):
    j.update(kw, updated_at=now())
    R.set(f"job:{j['job_id']}", dumps(j))
    return j

def put_text(key, text):
    S3.put_object(Bucket=BUCKET, Key=key, Body=text.encode(), ContentType="text/markdown; charset=utf-8")
    return key

def get_text(key):
    try: return S3.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode()
    except S3.exceptions.NoSuchKey: raise NotFound(key) from None

def commit(j, queue, msg, **kw):
    """ジョブ状態と送信予定の通知(outbox)を1トランザクションでRedisへ保存し、保存成功後に通知する。"""
    j.update(kw, updated_at=now())
    R.pipeline().set(f"job:{j['job_id']}", dumps(j)).hset(f"outbox:{j['job_id']}", f"{queue}:{msg['revision']}", dumps(msg)).execute()
    flush(j["job_id"])
    return j

def flush(jid):
    """未送信の通知を送る。送れなかった分はoutboxに残り、次の受信時やAPIの再送で送り直す（重複は受信側で排除）。"""
    for field, msg in R.hgetall(f"outbox:{jid}").items():
        SQS.send_message(QueueUrl=QUEUES[field.split(":")[0]], MessageBody=msg)
        R.hdel(f"outbox:{jid}", field)

def call_llm(system_prompt: str, user_prompt: str) -> str:
    """独自LLMエンドポイントを素のHTTPで呼ぶ。リクエスト/レスポンス形式とAPIキーの付与方法は環境変数で接続先に合わせる。"""
    vals = {"system": system_prompt, "user": user_prompt}
    body = re.sub(r"\{\{(system|user)\}\}", lambda m: json.dumps(vals[m[1]])[1:-1], E("LLM_REQUEST_TEMPLATE", '{"system": "{{system}}", "prompt": "{{user}}"}'))
    res = http("POST", E("LLM_URL"), data=body.encode(), timeout=float(E("LLM_TIMEOUT", 120)), headers={"Content-Type": "application/json",
               E("LLM_API_KEY_HEADER", "Authorization"): E("LLM_API_KEY_FORMAT", "Bearer {}").format(E("LLM_API_KEY", ""))})
    keys = [] if (path := E("LLM_RESPONSE_PATH", "output")) == "." else path.split(".")  # 例: choices.0.message.content、"." は本文そのまま
    out = res.json() if keys else res.text
    try:
        for k in keys: out = out[int(k) if isinstance(out, list) else k]
    except (KeyError, IndexError, TypeError, ValueError): out = None
    if not isinstance(out, str) or not out.strip(): raise ValueError(f"LLMの応答形式が不正です: {res.text[:200]}")
    return out

def ask_json(system, user, check=lambda v: True):
    """JSONモードを仮定せず、応答からJSONを抜き出して検証する。不正なら形式を指摘して依頼し直す。"""
    for _ in range(TRIES):
        text = call_llm(system, user)
        try:
            if check(v := json.loads(re.search(r"[\[{].*[\]}]", text, re.S)[0])): return v
        except Exception: pass
        log.warning("LLMの出力が指定形式のJSONではありません: %.200s", text)
        user += "\n\n※前回の出力は指定形式のJSONではありませんでした。指定形式のJSONだけを出力してください。"
    raise ValueError(f"LLMが有効なJSONを返しませんでした: {text[:200]}")

def tag(name, text):
    """データをタグで囲む。中のタグ記法は除去し、Wiki本文等が区切りを破ってシステムプロンプトの指示を上書きできないようにする。"""
    return f"<{name}>\n{re.sub(rf'</?\s*(?:{TAGS})\b[^>]*>', '', str(text), flags=re.I)}\n</{name}>\n" if text else ""

def prompts(name):
    """prompts/<name>.md の「## 見出し」ごとに、共通の system と連結したシステムプロンプトを返す。"""
    text = (ROOT / "prompts" / f"{name}.md").read_text(encoding="utf-8")
    parts = {k.strip(): v.strip() for k, v in (s.split("\n", 1) for s in text.split("\n## ")[1:])}
    return {k: parts["system"] + "\n\n" + v for k, v in parts.items() if k != "system"}

def parse_skill(text):
    """SKILL.md をYAMLフロントマター（name, description）と本文に分ける。"""
    m = re.match(r"\s*---[ \t]*\n(.*?)\n---[ \t]*(?:\n|$)(.*)", text, re.S)
    meta = yaml.safe_load(m[1]) if m else None
    if not isinstance(meta, dict) or not meta.get("name"): raise ValueError("SKILL.md には name を含むYAMLフロントマターが必要です")
    return meta, m[2].strip()

def skills(system, keys, task):
    """Skillsの名前と説明をLLMに示して必要なものを選ばせ、選ばれたSKILL.mdの本文だけをプロンプト用に返す。"""
    found = {str(meta["name"]): (meta, body) for meta, body in (parse_skill(get_text(k)) for k in keys)}
    if not found: return ""
    menu = "\n".join(f"- {n}: {meta.get('description', '')}" for n, (meta, _) in found.items())
    names = ask_json(system, tag("task", task) + tag("skills", menu), check=lambda v: isinstance(v, list) and all(isinstance(n, str) for n in v))
    return "".join(tag("skill", f"# {n}\n{found[n][1]}") for n in dict.fromkeys(names) if n in found)

def slack(j, result):
    """自動レビュー完了・上限到達・異常終了をSlackへ通知する。失敗してもログに残し、ジョブ状態はWeb画面で確認できる。"""
    text = (f"*[{result}]* {j.get('name', '')}\nジョブID: {j['job_id']} / レビュー回数: {j.get('review_count', '-')}/{j.get('max_reviews', '-')}"
            + (f"\nエラー: {j['error'][:300]}" if j.get("error") else "") + f"\n{E('APP_URL', '')}/job.html?id={j['job_id']}")
    try: E("SLACK_WEBHOOK_URL") and http("POST", E("SLACK_WEBHOOK_URL"), json={"text": text})
    except Exception: log.exception("Slack通知に失敗しました: %s", j["job_id"])

def fail(jid, error):
    """継続不能なエラーをジョブに記録し、異常終了を通知する。"""
    try: j = save(job(jid), status="failed", error=error[:1000])
    except Exception: j = {"job_id": jid}  # Redis障害時も通知は試みる
    slack(j | {"error": error}, "異常終了")

def _deadline(*_): raise Deadline("Lambdaの実行時間上限が近づいたため中断しました")

def sqs_handler(stage, process):
    """SQS通知を処理するLambdaハンドラーを作る。重複通知は排他ロックと状態確認で除き、失敗した通知だけを再試行させる。"""
    def handler(event, context=None):
        if context:
            signal.signal(signal.SIGALRM, _deadline)
            signal.alarm(max(1, context.get_remaining_time_in_millis() // 1000 - 20))
        failed = []
        for rec in event.get("Records", []):
            msg, later = json.loads(rec["body"]), False
            lock = f"lock:{stage}:{msg['job_id']}:{msg['revision']}"
            try:
                if R.set(lock, 1, nx=True, ex=LOCK_TTL):
                    try: flush(msg["job_id"]); process(msg)  # noqa: E702
                    finally: R.delete(lock)
                else: later = True  # 同じ通知を別の実行が処理中のため、後で状態を確認し直す
            except (Exception, Deadline) as e:
                log.exception("%s failed: %s", stage, msg)
                later = int(rec.get("attributes", {}).get("ApproximateReceiveCount", 1)) < ATTEMPTS  # 上限までは再試行
                if not later: fail(msg["job_id"], f"{stage}: {type(e).__name__}: {e}")
            if later: failed.append({"itemIdentifier": rec["messageId"]})
        signal.alarm(0)
        return {"batchItemFailures": failed}
    return handler
