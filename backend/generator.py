"""生成アプリ（generation-queue の通知で起動）。
Confluence・RAG・Skills を使って Markdown を生成・修正し、review-queue へ保存イベントを送る。"""
import re
from typing import TypedDict
from markdownify import markdownify
import common, rag

PROMPTS = common.load_prompts("generator")
CHUNK_SIZE = 1000
CHUNK_OVERLAP = 200

class Plan(TypedDict):  # 作成指示を整理した結果（LLM の出力形式）
    instruction: str
    outline: list[str]
    queries: list[str]

def split_into_chunks(markdown):
    """Markdown を CHUNK_SIZE 文字ずつ、前後を CHUNK_OVERLAP 文字重ねて分割する。"""
    step = CHUNK_SIZE - CHUNK_OVERLAP
    return [markdown[start:start + CHUNK_SIZE] for start in range(0, len(markdown), step)]

def search_wiki(queries):
    """Confluence を全文検索し、未登録・更新済みのページだけ Markdown 変換・チャンク分割して RAG へ登録する。"""
    terms = [re.sub(r'["\\]', " ", query).strip() for query in queries]
    conditions = " OR ".join(f'text ~ "{term}"' for term in terms if term)
    if not conditions:
        return []
    url = f"{common.env('CONFLUENCE_URL')}/rest/api/content/search"
    cql = f"{common.env('CONFLUENCE_CQL', 'type=page')} AND ({conditions})"
    params = {"cql": cql, "expand": "body.storage,version", "limit": 10}
    headers = {"Authorization": common.env("CONFLUENCE_AUTH", "")}
    pages = common.http_session.get(url, params=params, headers=headers, timeout=60).json()["results"]
    for page in pages:
        version = str(page["version"]["number"])
        if rag.get_indexed_version(page["id"]) == version:
            continue  # 登録済みの版と同じなら登録し直さない
        markdown = markdownify(page["body"]["storage"]["value"], heading_style="ATX")  # storage 形式（HTML）→ Markdown
        metadata = {"title": page["title"], "updated_at": page["version"].get("when")}
        rag.upsert_page(page["id"], version, split_into_chunks(markdown), metadata)
    return [page["id"] for page in pages]

def generate(job, revision):
    """作成指示・Wiki・Skills から Markdown を生成して S3 に保存する（修正時は前回の版とレビュー指摘も渡す）。"""
    instruction = common.tag("instruction", job["instruction"]) + common.tag("comment", job.get("comment"))
    if not job.get("plan"):  # 作成指示の整理（構成案・検索クエリ）は、レビューサイクルの最初に 1 回だけ行う
        common.save_job(job, plan=common.ask_json(PROMPTS["plan"], instruction, Plan))
    plan = job["plan"]
    last_review = job.get("review") or {}
    issues = last_review.get("issues", [])
    review_text = common.to_json(issues) if issues else ""
    page_ids = search_wiki(plan["queries"][:5])
    references = rag.search(plan["instruction"] + review_text, page_ids, k=5)
    user_prompt = (instruction
                   + common.tag("plan", common.to_json(plan))
                   + "".join(common.tag("reference", f"# {ref['title']}\n{ref['text']}") for ref in references)
                   + common.select_skills(PROMPTS["skills"], job["generation_skills"], plan["instruction"]))

    if job["revision"] == 0:
        markdown = common.call_llm(PROMPTS["generate"], user_prompt)
    else:  # 自動修正・人間の修正指示: 前回の Markdown とレビュー指摘も渡す
        previous = common.get_text(job["markdown_key"])
        user_prompt += common.tag("previous", previous) + common.tag("review", review_text)
        markdown = common.call_llm(PROMPTS["revise"], user_prompt)

    markdown = markdown.strip()
    fenced = re.fullmatch(r"```(?:markdown|md)\n(.*)\n```", markdown, re.DOTALL)
    if fenced:  # 全体がコードフェンスで囲まれていたら外す
        markdown = fenced[1]
    return common.put_text(f"jobs/{job['job_id']}/drafts/{revision:03d}.md", markdown)

def process(message):
    job = common.load_job(message["job_id"])
    revision = message["revision"]
    if job["revision"] < revision:  # 未生成なら生成する（生成済みの重複通知では、保存イベントの再送だけを行う）
        common.save_job(job, status="generating")
        markdown_key = generate(job, revision)
        common.save_job(job, revision=revision, markdown_key=markdown_key, status="reviewing")
    common.notify_queue("review", job, revision)  # Redis への保存後に通知する（失敗したら例外 → 再配信で送り直す）

handler = common.sqs_handler(process)
