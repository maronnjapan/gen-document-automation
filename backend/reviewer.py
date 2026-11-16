"""レビューアプリ（review-queueの保存イベントで起動）: Markdownをレビューし、結果に応じて再生成依頼または終了通知を行う。"""
import common as c

P = c.prompts("reviewer")

def review_ok(v):  # レビュー結果（LLM出力）の形式検証
    return isinstance(v, dict) and isinstance(v.get("issues"), list) and all(isinstance(i, dict) and isinstance(i.get("comment"), str) for i in v["issues"])

def process(msg):
    j, n = c.job(msg["job_id"]), int(msg["revision"])
    if j["revision"] != n or j.get("reviewed_revision", 0) >= n: return  # 古いリビジョン・レビュー済み（重複通知）
    skills = c.skills(P["skills"], j["review_skills"], j["viewpoints"] or "一般的なドキュメントレビュー")
    review = c.ask_json(P["review"], c.tag("viewpoints", j["viewpoints"]) + c.tag("comment", j.get("comment")) + skills
                        + c.tag("document", c.get_text(j["markdown_key"])), review_ok)
    j.update(review=review, reviewed_revision=n, review_count=j["review_count"] + 1)
    if review["issues"] and j["review_count"] < j["max_reviews"]:  # 指摘あり・上限未到達 → 保存後に再生成を通知
        return c.commit(j, "generation", {"job_id": j["job_id"], "revision": n + 1, "event": "revise"}, status="queued")
    c.save(j, status="limit_reached" if review["issues"] else "completed")
    c.slack(j, "レビュー上限到達" if review["issues"] else "自動レビュー完了")

handler = c.sqs_handler("review", process)
