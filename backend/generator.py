"""生成アプリ（generation-queueの通知で起動）: Confluence・RAG・Skillsを使ってMarkdownを生成/修正し、保存イベントを送る。"""
import re, typing
from markdownify import markdownify
import common as c, rag

P = c.prompts("generator")
Plan = typing.TypedDict("Plan", {"instruction": str, "outline": list[str], "queries": list[str]})  # 作成指示の整理結果（LLM出力）の形式

def index_wiki(queries):  # Confluenceを全文検索し、未登録・更新済みのページだけMarkdown変換・チャンク分割してRAGへ登録する
    if not (cql := " OR ".join(f'text ~ "{t}"' for t in (re.sub(r'["\\]', " ", q).strip() for q in queries) if t)): return []
    pages = c.HTTP.get(f"{c.E('CONFLUENCE_URL')}/rest/api/content/search", headers={"Authorization": c.E("CONFLUENCE_AUTH", "")}, timeout=60,
                       params={"cql": f"{c.E('CONFLUENCE_CQL', 'type=page')} AND ({cql})", "expand": "body.storage,version", "limit": 10}).json()["results"]
    for p in pages:
        if rag.get_indexed_version(p["id"]) != (ver := str(p["version"]["number"])):
            md = markdownify(p["body"]["storage"]["value"], heading_style="ATX")
            rag.upsert_page(p["id"], ver, [md[i:i + 1000] for i in range(0, len(md), 800)], {"title": p["title"], "updated_at": p["version"].get("when")})
    return [p["id"] for p in pages]

def process(msg):
    j, n = c.job(msg["job_id"]), msg["revision"]
    if j["revision"] < n:  # 未生成なら生成する（生成済みの重複通知では、保存イベントの再送だけ行う）
        c.save(j, status="generating")
        plan = j.get("plan") or c.save(j, plan=c.ask_json(P["plan"], c.tag("instruction", j["instruction"]) + c.tag("comment", j.get("comment")), Plan))["plan"]
        issues = c.dumps(i) if (i := (j.get("review") or {}).get("issues")) else ""  # 前回のレビュー指摘
        refs = "".join(c.tag("reference", f"# {h['title']}\n{h['text']}") for h in rag.search(plan["instruction"] + issues, index_wiki(plan["queries"][:5]), 5))
        prev = c.tag("previous", c.get_text(j["markdown_key"])) + c.tag("review", issues) if j["revision"] else ""  # 自動修正・人間の修正指示
        user = c.tag("instruction", j["instruction"]) + c.tag("comment", j.get("comment")) + c.tag("plan", c.dumps(plan)) + refs + prev
        md = c.call_llm(P["revise" if prev else "generate"], user + c.skills(P["skills"], j["generation_skills"], plan["instruction"])).strip()
        md = m[1] if (m := re.fullmatch(r"```(?:markdown|md)\n(.*)\n```", md, re.S)) else md  # 全体を囲むコードフェンスは外す
        c.save(j, revision=n, status="reviewing", markdown_key=c.put_text(f"jobs/{j['job_id']}/drafts/{n:03d}.md", md))
    c.send("review", j, n)  # Redisへの保存後に保存イベントを通知する。送信に失敗したら例外→再配信で送り直す

handler = c.handler(process)
