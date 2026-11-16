"""レビューアプリ（review-queueの保存イベントで起動）: Markdownをレビューし、結果に応じて修正依頼または終了通知を行う。"""
import typing
import common as c

P = c.prompts("reviewer")
Review = typing.TypedDict("Review", {"issues": list[typing.TypedDict("Issue", {"comment": str})]})  # レビュー結果の形式（summary等の項目もそのまま保存）
RESULTS = {"completed": "自動レビュー完了", "limit_reached": "レビュー上限到達"}

def process(msg):
    j, n = c.job(msg["job_id"]), msg["revision"]
    if j["revision"] != n: return  # 古いリビジョンの通知
    if j.get("reviewed_revision", 0) < n:  # 未レビューならレビューする（レビュー済みの重複通知では、修正依頼の再送だけ行う）
        user = c.tag("viewpoints", j["viewpoints"]) + c.tag("comment", j.get("comment")) + c.tag("document", c.get_text(j["markdown_key"]))
        review = c.ask_json(P["review"], user + c.skills(P["skills"], j["review_skills"], j["viewpoints"] or "ドキュメントレビュー"), Review)
        count = j["review_count"] + 1
        status = ("queued" if count < j["max_reviews"] else "limit_reached") if review["issues"] else "completed"
        c.save(j, review=review, reviewed_revision=n, review_count=count, status=status)
        if status in RESULTS: c.slack(j, RESULTS[status])
    if j["status"] == "queued": c.send("generation", j, n + 1)  # 指摘あり・上限未到達: Redisへの保存後に修正を依頼する

handler = c.handler(process)
