"""レビューアプリ（review-queue の保存イベントで起動）。
Markdown をレビューし、結果に応じて generation-queue へ修正を依頼するか、Slack へ終了を通知する。"""
from typing import TypedDict
import common

PROMPTS = common.load_prompts("reviewer")

class Issue(TypedDict):  # レビュー指摘（severity・location・suggestion などの項目もそのまま保存する）
    comment: str

class Review(TypedDict):  # レビュー結果（LLM の出力形式）
    issues: list[Issue]

def run_review(job):
    """レビュー観点・人間の修正指示・レビュー用 Skills を使って Markdown をレビューし、結果を返す。"""
    task = job["viewpoints"] or "ドキュメントレビュー"
    user_prompt = (common.tag("viewpoints", job["viewpoints"])
                   + common.tag("comment", job.get("comment"))
                   + common.tag("document", common.get_text(job["markdown_key"]))
                   + common.select_skills(PROMPTS["skills"], job["review_skills"], task))
    return common.ask_json(PROMPTS["review"], user_prompt, Review)

def process(message):
    job = common.load_job(message["job_id"])
    revision = message["revision"]
    if job["revision"] != revision:
        return  # 古いリビジョンの通知

    if job.get("reviewed_revision", 0) < revision:  # 未レビューならレビューする（重複通知では修正依頼の再送だけ）
        review = run_review(job)
        review_count = job["review_count"] + 1
        if not review["issues"]:
            status = "completed"
        elif review_count < job["max_reviews"]:
            status = "queued"  # 指摘あり・上限未到達: 修正版を生成する
        else:
            status = "limit_reached"
        common.save_job(job, review=review, reviewed_revision=revision, review_count=review_count, status=status)

        if status == "completed":
            common.notify_slack(job, "自動レビュー完了")
        elif status == "limit_reached":
            common.notify_slack(job, "レビュー上限到達")

    if job["status"] == "queued":
        common.notify_queue("generation", job, revision + 1)  # Redis への保存後に修正を依頼する

handler = common.sqs_handler(process)
