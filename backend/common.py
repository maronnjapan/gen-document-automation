"""生成・レビュー・Web API の共通処理（設定、Redis、S3、SQS、独自LLM、Skills、Slack、Lambda ハンドラー）。"""
import datetime, json, logging, os, pathlib, re
import boto3, frontmatter, jmespath, pydantic, redis, requests, urllib3

def env(name, default=None):
    """環境変数を読む（空文字は未設定として扱う）。"""
    return os.environ.get(name) or default

# prompts/ skills/ frontend/ の場所（Lambda のコンテナでは APP_ROOT で指定する）
APP_ROOT = pathlib.Path(env("APP_ROOT") or pathlib.Path(__file__).resolve().parents[1])
BUCKET = env("S3_BUCKET", "doc-automation")
redis_client = redis.Redis.from_url(env("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)
s3 = boto3.client("s3")  # 接続先は AWS_ENDPOINT_URL_S3 で変更できる
sqs = boto3.client("sqs")  # 接続先は AWS_ENDPOINT_URL_SQS で変更できる

# LLM・Confluence・Slack への HTTP。接続失敗・429・5xx は指数バックオフで再試行し、エラー応答は例外にする
http_session = requests.Session()
retry_policy = urllib3.Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504],
                             allowed_methods=None)  # POST も再試行する
for scheme in ("http://", "https://"):
    http_session.mount(scheme, requests.adapters.HTTPAdapter(max_retries=retry_policy))
http_session.hooks["response"].append(lambda response, *args, **kwargs: response.raise_for_status())

# プロンプトでデータを囲むタグ。外部情報に含まれるこれらのタグ記法は取り除く
TAG_NAMES = "instruction|comment|plan|skills?|task|reference|previous|review|viewpoints|document"
DATA_TAGS = re.compile(rf"</?\s*(?:{TAG_NAMES})\b[^>]*>", re.IGNORECASE)

def now():
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")

def to_json(value):
    return json.dumps(value, ensure_ascii=False)

def load_job(job_id):
    """Redis からジョブ（フラットな JSON）を読む。"""
    data = redis_client.get(f"job:{job_id}")
    if data is None:
        raise LookupError(f"job not found: {job_id}")
    return json.loads(data)

def save_job(job, **changes):
    """ジョブの項目を更新して Redis に保存する。"""
    job.update(changes, updated_at=now())
    redis_client.set(f"job:{job['job_id']}", to_json(job))
    return job

def put_text(key, text):
    s3.put_object(Bucket=BUCKET, Key=key, Body=text.encode())
    return key

def get_text(key):
    return s3.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode()

def notify_queue(queue, job, revision):
    """キューへ通知する（識別情報だけ）。FIFO の重複排除IDで二重送信を除き、同じジョブの通知は順に処理させる。"""
    body = to_json({"job_id": job["job_id"], "revision": revision, "event": queue})
    deduplication_id = f"{job['job_id']}-{job['cycle']}-{revision}"
    sqs.send_message(QueueUrl=env(f"{queue.upper()}_QUEUE_URL"), MessageBody=body,
                     MessageGroupId=job["job_id"], MessageDeduplicationId=deduplication_id)

def call_llm(system_prompt: str, user_prompt: str) -> str:
    """独自LLMエンドポイントを呼ぶ薄いアダプター。本文の形式と応答の取り出し位置は設定で接続先に合わせる。"""
    template = env("LLM_REQUEST_TEMPLATE", '{"system": "{{system}}", "prompt": "{{user}}"}')
    prompts = {"system": system_prompt, "user": user_prompt}
    # {{system}} と {{user}} を、JSON 文字列としてエスケープしたプロンプトで置き換える（前後の引用符は除く）
    body = re.sub(r"\{\{(system|user)\}\}", lambda match: json.dumps(prompts[match[1]])[1:-1], template)
    api_key_header = env("LLM_API_KEY_HEADER", "Authorization")
    headers = {"Content-Type": "application/json", api_key_header: env("LLM_API_KEY", "")}
    timeout = float(env("LLM_TIMEOUT", 120))
    response = http_session.post(env("LLM_URL"), data=body.encode(), headers=headers, timeout=timeout)
    text = jmespath.search(env("LLM_RESPONSE_PATH", "output"), response.json())
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"LLMの応答形式が不正です: {response.text[:200]}")
    return text

def ask_json(system_prompt, user_prompt, schema):
    """JSON モードを仮定せず、応答から JSON を抜き出して型を検証する（不正なら例外にして通知ごと再試行させる）。"""
    answer = call_llm(system_prompt, user_prompt)
    json_part = re.search(r"[\[{].*[\]}]", answer, re.DOTALL)
    if json_part is None:
        raise ValueError(f"LLMの応答にJSONが含まれていません: {answer[:200]}")
    value = json.loads(json_part[0])
    pydantic.TypeAdapter(schema).validate_python(value)  # 定義外の項目（summary など）は残したまま検証する
    return value

def tag(name, text):
    """データをタグで囲む。中のタグ記法は取り除き、Wiki 本文などが区切りを破って指示を上書きできないようにする。"""
    if not text:
        return ""
    return f"<{name}>\n{DATA_TAGS.sub('', str(text))}\n</{name}>\n"

def load_prompts(name):
    """prompts/<name>.md を「## 見出し」ごとに分け、共通の system と連結したシステムプロンプトを用途別に返す。"""
    text = (APP_ROOT / "prompts" / f"{name}.md").read_text(encoding="utf-8")
    sections = {}
    for section in text.split("\n## ")[1:]:  # 最初の見出しより前は説明なので使わない
        title, body = section.split("\n", 1)
        sections[title.strip()] = body.strip()
    system_rules = sections.pop("system")
    return {title: f"{system_rules}\n\n{body}" for title, body in sections.items()}

def select_skills(system_prompt, skill_keys, task):
    """Skill の名前と説明だけを LLM に示して選ばせ、選ばれた SKILL.md の本文をプロンプト用に返す。"""
    skills = {}
    for key in skill_keys:
        skill = frontmatter.loads(get_text(key))
        if skill.get("name"):  # name のない SKILL.md は使わない
            skills[skill["name"]] = skill
    if not skills:
        return ""
    menu = "\n".join(f"- {name}: {skill.get('description', '')}" for name, skill in skills.items())
    selected_names = ask_json(system_prompt, tag("task", task) + tag("skills", menu), list[str])
    selected = [name for name in dict.fromkeys(selected_names) if name in skills]
    return "".join(tag("skill", f"# {name}\n{skills[name].content}") for name in selected)

def notify_slack(job, result):
    """完了・上限到達・異常終了を Slack へ通知する。失敗しても状態は Web 画面で確認できるため、ログだけ残す。"""
    webhook_url = env("SLACK_WEBHOOK_URL")
    if not webhook_url:
        return
    text = (f"[{result}] {job['name']}\n"
            f"ジョブ {job['job_id']} レビュー {job['review_count']}/{job['max_reviews']}\n"
            f"{env('APP_URL', '')}/job.html?id={job['job_id']}")
    try:
        http_session.post(webhook_url, json={"text": text}, timeout=30)
    except requests.RequestException:
        logging.exception("Slack通知に失敗しました: %s", job["job_id"])

def sqs_handler(process):
    """SQS の通知を処理する Lambda ハンドラーを作る。失敗はジョブに記録して例外を投げ、SQS の再配信に任せる。"""
    def handler(event, context=None):
        for record in event["Records"]:
            message = json.loads(record["body"])
            try:
                process(message)
            except Exception as error:  # 再試行の上限を超えた通知は DLQ へ移り、dead_letter_handler が処理する
                save_job(load_job(message["job_id"]), error=f"{type(error).__name__}: {error}"[:500])
                raise
    return handler

def dead_letter_handler(event, context=None):
    """再試行の上限を超えた通知（DLQ）を受け、ジョブを異常終了にして Slack へ通知する。"""
    for record in event["Records"]:
        message = json.loads(record["body"])
        job = save_job(load_job(message["job_id"]), status="failed")
        notify_slack(job, f"異常終了: {job.get('error') or 'タイムアウト等'}")
