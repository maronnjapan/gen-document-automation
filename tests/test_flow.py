"""生成 → 保存イベント → レビュー → 再生成 の一連の流れと、終了条件・人間レビュー・履歴取得の検証。"""
import json

from harness import ISSUE, c, drain, new_job, receive


def test_generate_review_revise_until_no_issues(world, client):
    world.reviews = [[ISSUE], [dict(ISSUE, comment="手順の順序が誤っている")]]
    jid = new_job(client, doc_type="手順書")

    # 通知には識別情報だけを含め、本文などはストレージ・Redisから取得する
    msg = json.loads(receive("generation")[0]["Body"])
    assert msg == {"job_id": jid, "revision": 1, "event": "generate"}
    c.SQS.purge_queue(QueueUrl=c.QUEUES["generation"])
    c.SQS.send_message(QueueUrl=c.QUEUES["generation"], MessageBody=json.dumps(msg))
    drain()

    j = client.get(f"/api/jobs/{jid}").json()
    assert (j["status"], j["revision"], j["review_count"], j["cycle"]) == ("completed", 3, 3, 1)
    assert j["review"]["issues"] == [] and j["markdown_key"] == f"jobs/{jid}/drafts/003.md"
    assert world.kinds().count("plan") == 1  # 作成指示の整理はサイクルごとに1回
    assert world.kinds().count("generate") == 1 and world.kinds().count("revise") == 2 and world.kinds().count("review") == 3

    # Confluenceの検索結果をRAGに登録し、検索結果を参考情報として生成プロンプトへ渡す
    gen_prompt = world.prompts("generate")[0]
    assert "<reference>" in gen_prompt and "POST /payments" in gen_prompt
    confluence = [kw for _, url, kw in world.http_log if "wiki.test" in url][0]
    assert confluence["headers"]["Authorization"] == "Bearer wiki-token" and 'text ~ "決済API"' in confluence["params"]["cql"]

    # 自動修正では、元の作成指示・レビュー指摘・前回のMarkdownを渡す
    revise_prompt = world.prompts("revise")[0]
    assert "決済APIの使い方を説明する" in revise_prompt and "目的が書かれていない" in revise_prompt
    assert "<previous>\n# 決済API利用手順書\n\n初版の本文\n</previous>" in revise_prompt  # 全体を囲むコードフェンスは除去して保存

    # 過去バージョンを含む履歴を取得できる
    assert client.get(f"/api/jobs/{jid}/drafts/1").text.endswith("初版の本文")
    assert client.get(f"/api/jobs/{jid}/drafts/3").text.endswith("修正版 2")
    assert client.get(f"/api/jobs/{jid}/drafts/4").status_code == 404

    # 自動レビュー完了をSlackへ通知（ジョブID・ドキュメント名・結果・レビュー回数・画面リンク）
    assert len(world.slack) == 1
    assert all(s in world.slack[0] for s in ("自動レビュー完了", "決済API利用手順書", jid, "3/10", f"http://app.test/job.html?id={jid}"))


def test_stops_when_max_reviews_reached(world, client):
    world.always_issue = True
    jid = new_job(client)  # 最大レビュー回数の初期値は10
    drain()
    j = client.get(f"/api/jobs/{jid}").json()
    assert (j["status"], j["review_count"], j["revision"], j["max_reviews"]) == ("limit_reached", 10, 10, 10)
    assert world.kinds().count("review") == 10 and world.kinds().count("revise") == 9
    assert len(world.slack) == 1 and "レビュー上限到達" in world.slack[0] and "10/10" in world.slack[0]


def test_max_reviews_is_configurable(world, client):
    world.always_issue = True
    jid = new_job(client, max_reviews=2)
    drain()
    j = client.get(f"/api/jobs/{jid}").json()
    assert (j["status"], j["review_count"], j["revision"]) == ("limit_reached", 2, 2)


def test_human_revise_starts_new_cycle_and_approve(world, client):
    world.always_issue = True
    jid = new_job(client, max_reviews=2)
    assert client.post(f"/api/jobs/{jid}/revise", json={"comment": "x"}).status_code == 409  # 処理中は受け付けない
    drain()
    assert client.get(f"/api/jobs/{jid}").json()["status"] == "limit_reached"

    world.always_issue = False
    res = client.post(f"/api/jobs/{jid}/revise", json={"comment": "FAQの章を追加してください"})
    assert res.status_code == 200 and (res.json()["cycle"], res.json()["review_count"], res.json()["status"]) == (2, 0, "queued")
    drain()

    j = client.get(f"/api/jobs/{jid}").json()
    assert (j["status"], j["cycle"], j["review_count"], j["revision"]) == ("completed", 2, 1, 3)
    assert world.kinds().count("plan") == 2  # 新しいサイクルでは修正指示を含めて作成指示を整理し直す
    assert "FAQの章を追加してください" in world.prompts("plan")[1]
    last_revise = world.prompts("revise")[-1]
    assert "<comment>\nFAQの章を追加してください" in last_revise and "<previous>" in last_revise
    assert "FAQの章を追加してください" in world.prompts("review")[-1]  # 修正指示が反映されたかもレビューする

    res = client.post(f"/api/jobs/{jid}/approve")
    assert res.status_code == 200 and res.json()["status"] == "approved"
    assert c.get_text(f"jobs/{jid}/final/final.md") == client.get(f"/api/jobs/{jid}/drafts/3").text


def test_job_list_and_not_found(world, client):
    jid = new_job(client)
    assert [j["job_id"] for j in client.get("/api/jobs").json()] == [jid]
    assert client.get("/api/jobs?limit=0").status_code == 422
    assert client.get("/api/jobs/unknown").status_code == 404
    assert client.post("/api/jobs/unknown/approve").status_code == 404
    assert client.post("/api/jobs", json={"name": "", "instruction": "x"}).status_code == 422
    assert client.post(f"/api/jobs/{jid}/approve").status_code == 409  # まだ原稿がない
