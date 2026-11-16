"""生成アプリ（generation-queueの通知で起動）: Confluence・RAG・Skillsを使ってMarkdownを生成/修正し、保存イベントを送る。"""
import re

from markdownify import markdownify

import common as c
import rag

P, CHUNK = c.prompts("generator"), int(c.E("CHUNK_SIZE", 1000))

def plan_ok(v):  # 作成指示の整理結果（LLM出力）の形式検証
    return isinstance(v, dict) and isinstance(v.get("instruction"), str) and isinstance(q := v.get("queries"), list) and all(isinstance(x, str) for x in q)

def chunk(md, size=CHUNK):
    """Markdownを段落単位でまとめ、size文字程度のチャンクに分割する（長すぎる段落は機械的に分割）。"""
    out = [""]
    for part in (p[i:i + size] for p in re.split(r"\n\s*\n", md) for i in range(0, len(p), size)):
        if out[-1] and len(out[-1]) + len(part) > size: out.append("")
        out[-1] += part + "\n\n"
    return [s.strip() for s in out if s.strip()]

def index_wiki(queries):
    """Confluenceを全文検索し、未登録・更新済みのページだけをMarkdown変換・チャンク分割してRAGへ登録する。"""
    if not (terms := [t for t in (re.sub(r'["\\]', " ", q).strip() for q in queries) if t]): return []
    cql = " OR ".join(f'text ~ "{t}"' for t in terms)
    pages = c.http("GET", f"{c.E('CONFLUENCE_URL')}/rest/api/content/search", params={
        "cql": f"{c.E('CONFLUENCE_CQL', 'type=page')} AND ({cql})", "expand": "body.storage,version", "limit": c.E("CONFLUENCE_LIMIT", 10)},
        headers={"Authorization": c.E("CONFLUENCE_AUTH_FORMAT", "Bearer {}").format(c.E("CONFLUENCE_TOKEN", ""))}).json()["results"]
    for p in pages:
        if rag.get_indexed_version(p["id"]) != p["version"]["number"]:
            rag.upsert_page(p["id"], p["version"]["number"], chunk(markdownify(p["body"]["storage"]["value"], heading_style="ATX")),
                            {"title": p["title"], "updated_at": p["version"].get("when")})
    return [p["id"] for p in pages]

def process(msg):
    j, n = c.job(msg["job_id"]), int(msg["revision"])
    if j["revision"] >= n: return  # 生成済み（重複通知）
    c.save(j, status="generating", error=None)
    if j.get("plan_cycle") != j["cycle"]:  # 作成指示の整理・構成案・検索クエリ作成（レビューサイクルごとに1回）
        c.save(j, plan_cycle=j["cycle"], plan=c.ask_json(P["plan"], c.tag("instruction", j["instruction"]) + c.tag("comment", j.get("comment")), plan_ok))
    plan, review = j["plan"], j.get("review") or {}
    issues = "\n".join(f"- [{i.get('location', '')}] {i['comment']} {i.get('suggestion', '')}" for i in review.get("issues", []))
    hits = rag.search(f"{plan['instruction']}\n{issues}", index_wiki(plan["queries"][:5]), int(c.E("RAG_TOP_K", 5)))
    user = (c.tag("instruction", j["instruction"]) + c.tag("comment", j.get("comment")) + c.tag("plan", c.dumps(plan))
            + c.skills(P["skills"], j["generation_skills"], plan["instruction"])
            + "".join(c.tag("reference", f"# {h['metadata']['title']}\n{h['text']}") for h in hits))
    if j["revision"]: user += c.tag("previous", c.get_text(j["markdown_key"])) + c.tag("review", issues)  # 自動修正・人間の修正指示
    md = c.call_llm(P["revise" if j["revision"] else "generate"], user).strip()
    if m := re.fullmatch(r"```(?:markdown|md)\n(.*)\n```", md, re.S): md = m[1]  # 全体がコードフェンスで囲まれていたら外す
    key = c.put_text(f"jobs/{j['job_id']}/drafts/{n:03d}.md", md)
    c.commit(j, "review", {"job_id": j["job_id"], "revision": n, "event": "saved"}, revision=n, markdown_key=key, status="reviewing")

handler = c.sqs_handler("generation", process)
