"""Web API: ジョブの作成・参照、人間レビュー（修正指示・承認）、生成依頼の送信、フロントエンドの配信。"""
import uuid
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import common

app = FastAPI(title="ドキュメント自動生成・自動レビュー")
FINISHED = {"completed", "limit_reached", "failed", "approved"}  # 人間の操作（修正指示・承認）を受け付ける状態

@app.exception_handler(LookupError)
@app.exception_handler(common.s3.exceptions.NoSuchKey)
def not_found(request, error):
    return JSONResponse({"detail": "見つかりません"}, status_code=404)

class NewJob(BaseModel):
    """新規生成画面の入力。Skills は SKILL.md の本文（同梱 Skills の選択・ファイルアップロード・貼り付け）。"""
    name: str = Field(min_length=1)
    doc_type: str = ""
    instruction: str = Field(min_length=1)
    viewpoints: str = ""
    generation_skills: list[str] = []
    review_skills: list[str] = []
    max_reviews: int = Field(10, ge=1, le=100)

@app.get("/api/skills")
def list_builtin_skills():
    """同梱 Skills（skills/generation/*/SKILL.md, skills/review/*/SKILL.md）。"""
    paths = sorted((common.APP_ROOT / "skills").glob("*/*/SKILL.md"))
    return [{"kind": path.parent.parent.name, "text": path.read_text(encoding="utf-8")} for path in paths]

@app.post("/api/jobs")
def create_job(new_job: NewJob):
    """ジョブを作成して Redis に保存する。生成依頼は保存後に /enqueue で送る（失敗しても画面から再送できる）。"""
    job_id = uuid.uuid4().hex[:12]
    job = new_job.model_dump()
    for kind in ("generation_skills", "review_skills"):  # Skills の本文はストレージに保存し、ジョブには参照を持たせる
        texts = job[kind]
        job[kind] = [common.put_text(f"jobs/{job_id}/skills/{kind}/{i:02d}.md", text) for i, text in enumerate(texts)]
    job.update(job_id=job_id, status="queued", revision=0, cycle=1, review_count=0, created_at=common.now())
    return common.save_job(job)

@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    return common.load_job(job_id)

@app.get("/api/jobs/{job_id}/drafts/{revision}", response_class=PlainTextResponse)
def get_draft(job_id: str, revision: int):
    """各リビジョン（過去の版を含む）の Markdown。"""
    return common.get_text(f"jobs/{job_id}/drafts/{revision:03d}.md")

@app.post("/api/jobs/{job_id}/enqueue")
def enqueue(job_id: str):
    """待機中のジョブの生成依頼を generation-queue へ送る（送信に失敗したときの再送にも使う）。"""
    job = common.load_job(job_id)
    if job["status"] == "queued":
        common.notify_queue("generation", job, job["revision"] + 1)
    return job

@app.post("/api/jobs/{job_id}/revise")
def revise(job_id: str, comment: str = Body(embed=True, min_length=1)):
    """人間の修正指示: 新しいレビューサイクルを始め、レビュー回数をリセットして修正版を生成する。"""
    job = common.load_job(job_id)
    if job["status"] not in FINISHED:
        raise HTTPException(409, "処理中のジョブには修正指示を出せません")
    common.save_job(job, status="queued", comment=comment, cycle=job["cycle"] + 1, review_count=0,
                    plan=None, error=None)
    return enqueue(job_id)

@app.post("/api/jobs/{job_id}/approve")
def approve(job_id: str):
    """承認: 最新版を final/final.md として確定する。"""
    job = common.load_job(job_id)
    if job["status"] not in FINISHED or not job.get("markdown_key"):
        raise HTTPException(409, "承認できる状態ではありません")
    final_key = common.put_text(f"jobs/{job_id}/final/final.md", common.get_text(job["markdown_key"]))
    return common.save_job(job, status="approved", final_key=final_key)

app.mount("/", StaticFiles(directory=common.APP_ROOT / "frontend", html=True))
