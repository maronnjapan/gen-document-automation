"""Web API: ジョブの作成・参照、人間レビュー（修正指示・承認）、生成依頼の再送、フロントエンドの配信。"""
import uuid
from fastapi import Body, FastAPI, HTTPException, responses, staticfiles
from pydantic import BaseModel, Field
import common as c

app, DONE = FastAPI(title="ドキュメント自動生成"), {"completed", "limit_reached", "failed", "approved"}  # DONE: 人間の操作を受け付ける状態
for missing in (LookupError, c.S3.exceptions.NoSuchKey): app.exception_handler(missing)(lambda *_: responses.JSONResponse({"detail": "見つかりません"}, 404))

class NewJob(BaseModel):  # 新規生成画面の入力。Skillsは SKILL.md の本文（同梱Skillsの選択・アップロード・貼り付け）
    name: str = Field(min_length=1); doc_type: str = ""; instruction: str = Field(min_length=1); viewpoints: str = ""
    generation_skills: list[str] = []; review_skills: list[str] = []; max_reviews: int = Field(10, ge=1, le=100)

@app.get("/api/skills")  # 同梱Skills（skills/generation, skills/review）
def builtin_skills(): return [{"kind": p.parts[-3], "text": p.read_text(encoding="utf-8")} for p in sorted((c.ROOT / "skills").glob("*/*/SKILL.md"))]

@app.post("/api/jobs")
def create_job(req: NewJob):  # Skillsの本文はストレージに保存し、ジョブには参照（キー）を持たせる
    jid = uuid.uuid4().hex[:12]
    skills = {k: [c.put_text(f"jobs/{jid}/skills/{k}/{i:02d}.md", s) for i, s in enumerate(getattr(req, k))] for k in ("generation_skills", "review_skills")}
    return c.save(req.model_dump() | skills | {"job_id": jid, "status": "queued", "revision": 0, "cycle": 1, "review_count": 0, "created_at": c.now()})

@app.get("/api/jobs/{jid}")
def get_job(jid: str): return c.job(jid)

@app.get("/api/jobs/{jid}/drafts/{rev}", response_class=responses.PlainTextResponse)  # 各リビジョン（過去の版を含む）のMarkdown
def get_draft(jid: str, rev: int): return c.get_text(f"jobs/{jid}/drafts/{rev:03d}.md")

@app.post("/api/jobs/{jid}/revise")  # 人間の修正指示: 新しいレビューサイクルを始め、レビュー回数をリセットする
def revise(jid: str, comment: str = Body(embed=True, min_length=1)):
    if (j := c.job(jid))["status"] not in DONE: raise HTTPException(409, "処理中のジョブには修正指示を出せません")
    c.save(j, status="queued", comment=comment, cycle=j["cycle"] + 1, review_count=0, plan=None, error=None)
    return enqueue(jid)

@app.post("/api/jobs/{jid}/enqueue")  # 待機中のジョブの生成依頼を送信する（Redisへの保存後に通知。失敗したら画面から再送できる）
def enqueue(jid: str):
    if (j := c.job(jid))["status"] == "queued": c.send("generation", j, j["revision"] + 1)
    return j

@app.post("/api/jobs/{jid}/approve")  # 承認: 最新版を final/final.md として確定する
def approve(jid: str):
    if (j := c.job(jid))["status"] not in DONE or not j.get("markdown_key"): raise HTTPException(409, "承認できる状態ではありません")
    return c.save(j, status="approved", final_key=c.put_text(f"jobs/{jid}/final/final.md", c.get_text(j["markdown_key"])))

app.mount("/", staticfiles.StaticFiles(directory=c.ROOT / "frontend", html=True))
