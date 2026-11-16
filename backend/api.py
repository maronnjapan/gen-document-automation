"""Web API: ジョブの作成・参照、人間レビュー（修正指示・承認）、通知の再送、フロントエンドの配信。"""
import json, time, uuid

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import common as c

app = FastAPI(title="ドキュメント自動生成・自動レビュー")
FINISHED = {"completed", "limit_reached", "failed", "approved"}  # 人間の操作を受け付ける状態
app.exception_handler(c.NotFound)(lambda req, e: JSONResponse({"detail": f"見つかりません: {e}"}, 404))

class NewJob(BaseModel):
    name: str = Field(min_length=1)
    doc_type: str = ""
    instruction: str = Field(min_length=1)
    viewpoints: str = ""
    generation_skills: list[str] = []  # SKILL.md の本文（ファイルアップロード・貼り付け・同梱Skillsの選択）
    review_skills: list[str] = []
    max_reviews: int = Field(10, ge=1, le=100)

class Revise(BaseModel):
    comment: str = Field(min_length=1)

@app.get("/api/skills")
def builtin_skills():  # 同梱Skills（skills/generation/*/SKILL.md, skills/review/*/SKILL.md）
    return [{"kind": p.parts[-3], "text": p.read_text(encoding="utf-8")} for p in sorted((c.ROOT / "skills").glob("*/*/SKILL.md"))]

@app.post("/api/jobs")
def create_job(req: NewJob):
    try: [c.parse_skill(s) for s in req.generation_skills + req.review_skills]
    except Exception as e: raise HTTPException(400, f"Skillsの形式が不正です: {e}") from None
    jid = uuid.uuid4().hex[:12]
    skills = {k: [c.put_text(f"jobs/{jid}/skills/{k.split('_')[0]}/{i:02d}.md", s) for i, s in enumerate(getattr(req, k))]
              for k in ("generation_skills", "review_skills")}  # Skillsもオブジェクトストレージに保存し、ジョブには参照を持つ
    j = req.model_dump() | skills | {"job_id": jid, "status": "queued", "revision": 0, "cycle": 1, "review_count": 0, "created_at": c.now()}
    c.R.zadd("jobs", {jid: time.time()})
    return c.commit(j, "generation", {"job_id": jid, "revision": 1, "event": "generate"})

@app.get("/api/jobs")
def list_jobs(limit: int = Query(30, ge=1, le=200)):
    keys = [f"job:{jid}" for jid in c.R.zrevrange("jobs", 0, limit - 1)]
    return [json.loads(v) for v in c.R.mget(keys) if v] if keys else []

@app.get("/api/jobs/{jid}")
def get_job(jid: str):
    return c.job(jid) | {"pending_notifications": c.R.hlen(f"outbox:{jid}")}

@app.get("/api/jobs/{jid}/drafts/{rev}", response_class=PlainTextResponse)
def get_draft(jid: str, rev: int):  # 過去バージョンを含む各リビジョンのMarkdown
    return c.get_text(f"jobs/{jid}/drafts/{rev:03d}.md")

@app.post("/api/jobs/{jid}/revise")
def revise(jid: str, req: Revise):  # 人間の修正指示: 新しいレビューサイクルを開始し、レビュー回数をリセット
    j = c.job(jid)
    if j["status"] not in FINISHED: raise HTTPException(409, "処理中のジョブには修正指示を出せません")
    return c.commit(j, "generation", {"job_id": jid, "revision": j["revision"] + 1, "event": "revise"},
                    status="queued", comment=req.comment, cycle=j["cycle"] + 1, review_count=0, error=None)

@app.post("/api/jobs/{jid}/approve")
def approve(jid: str):  # 承認: 最新リビジョンを final/final.md として確定
    j = c.job(jid)
    if j["status"] not in FINISHED or not j.get("markdown_key"): raise HTTPException(409, "承認できる状態ではありません")
    return c.save(j, status="approved", final_key=c.put_text(f"jobs/{jid}/final/final.md", c.get_text(j["markdown_key"])))

@app.post("/api/jobs/{jid}/resend")
def resend(jid: str):  # 通知送信に失敗して残っている通知を再送
    c.flush(jid)
    return get_job(jid)

app.mount("/", StaticFiles(directory=c.ROOT / "frontend", html=True))
