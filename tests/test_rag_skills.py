"""RAGアダプター（版管理・差し替え）とSkills（解析・LLMによる選択・本文の再投入）の検証。"""

import rag
from harness import common, drain, new_job

SKILL_A = "---\nname: skill-a\ndescription: 手順書の書き方\n---\n# A\n手順は番号付きリストで書く"
SKILL_B = "---\nname: skill-b\ndescription: 設計書の書き方\n---\n# B\n代替案を比較する"
SKILL_R = "---\nname: security\ndescription: セキュリティ観点\n---\n秘密情報が含まれていないか確認する"
rag_upsert = rag.upsert_page  # 差し替え前の実装


def test_rag_replaces_old_version_and_excludes_stale_chunks(world):
    rag.upsert_page("p1", "1", ["旧版の決済API説明"], {"title": "決済"})
    assert rag.get_indexed_version("p1") == "1" and rag.get_indexed_version("p2") is None
    rag.upsert_page("p1", "2", ["新版の決済API説明", "障害時の連絡先"], {"title": "決済"})
    hits = rag.search("決済APIの説明", ["p1"], 5)
    assert rag.get_indexed_version("p1") == "2" and [h["text"] for h in hits] == [
        "新版の決済API説明",
        "障害時の連絡先",
    ]  # 類似度順
    assert hits[0]["title"] == "決済"

    # RAG側に旧版のチャンクが残っていても、版が一致しないものは検索結果から除外する
    common.redis_client.set("rag:chunks:p1", common.to_json([{"text": "旧版", "version": "1"}]))
    assert rag.search("旧版", ["p1"], 5) == [] and rag.search("決済", ["p9"], 5) == []


def test_only_new_or_updated_wiki_pages_are_reindexed(world, client, monkeypatch):
    upserts = []
    monkeypatch.setattr(rag, "upsert_page", lambda *a: (upserts.append(a[:2]), rag_upsert(*a))[1])
    world.pages["102"] = {"title": "障害対応", "version": 3, "html": "<p>障害時は再試行します。</p>"}
    new_job(client)
    drain()
    assert sorted(upserts) == [("101", "1"), ("102", "3")]

    world.pages["101"].update(version=2, html="<h2>v2</h2><p>決済APIは<b>v2</b>でPUT /paymentsに変わりました。</p>")
    new_job(client)
    drain()
    assert sorted(upserts) == [("101", "1"), ("101", "2"), ("102", "3")]  # 同じ版の102は登録し直さない
    assert "PUT /payments" in world.prompts("generate")[-1] and "POST /payments" not in world.prompts("generate")[-1]
    assert "## v2" in common.redis_client.get("rag:chunks:101") and "**v2**" in common.redis_client.get(
        "rag:chunks:101"
    )  # storage形式(HTML)をMarkdownに変換して登録


def test_rag_adapter_can_be_swapped(world, client, monkeypatch):
    store = {}  # 別実装（例: 実RAG DB）への差し替えを想定したインメモリ実装
    monkeypatch.setattr(rag, "get_indexed_version", lambda pid: store.get(pid, (None,))[0])
    monkeypatch.setattr(rag, "upsert_page", lambda pid, ver, chunks, meta: store.__setitem__(pid, (ver, chunks, meta)))
    monkeypatch.setattr(
        rag, "search", lambda q, ids, k: [{**store[i][2], "text": t} for i in ids for t in store[i][1]][:k]
    )
    jid = new_job(client)
    drain()
    assert (
        common.load_job(jid)["status"] == "completed"
        and store["101"][0] == "1"
        and not common.redis_client.exists("rag:chunks:101")
    )
    assert "POST /payments" in world.prompts("generate")[0]


def test_long_pages_are_split_into_chunks(world, client):
    world.pages["101"]["html"] = "<p>" + "決済" * 1000 + "</p>"
    new_job(client)
    drain()
    chunks = [d["text"] for d in rag.search("決済", ["101"], 10)]
    assert len(chunks) == 3 and all(len(t) <= 1000 for t in chunks)


def test_wiki_search_is_skipped_without_queries(world, client):
    world.llm_hook = lambda kind, user: (
        '{"instruction": "x", "outline": [], "queries": ["", " \\"\\" "]}' if kind == "plan" else None
    )
    jid = new_job(client)
    drain()
    assert common.load_job(jid)["status"] == "completed" and not [r for r in world.requests if "wiki.test" in r.url]
    assert "<reference>" not in world.prompts("generate")[0]


def test_llm_selects_skills_and_only_selected_bodies_are_used(world, client):
    world.skill_choice = {"generation_skills": ["skill-a", "unknown", "skill-a"], "review_skills": ["security"]}
    jid = new_job(client, generation_skills=[SKILL_A, SKILL_B], review_skills=[SKILL_R])

    j = common.load_job(jid)  # Skillsもオブジェクトストレージに保存し、ジョブには参照情報を持つ
    assert j["generation_skills"] == [
        f"jobs/{jid}/skills/generation_skills/00.md",
        f"jobs/{jid}/skills/generation_skills/01.md",
    ]
    assert common.get_text(j["review_skills"][0]) == SKILL_R
    drain()

    menu = world.prompts("generation_skills")[0]  # まず名前と説明の一覧だけをLLMへ渡す
    assert "- skill-a: 手順書の書き方" in menu and "- skill-b: 設計書の書き方" in menu and "番号付きリスト" not in menu
    gen = world.prompts("generate")[0]  # 選ばれたSkillの本文だけを追加して、あらためてLLMを呼ぶ
    assert gen.count("手順は番号付きリストで書く") == 1 and "代替案を比較する" not in gen and "unknown" not in gen
    assert "秘密情報が含まれていないか確認する" in world.prompts("review")[0]
    assert "skill-a" not in world.prompts("review_skills")[0]  # 生成用とレビュー用を区別する


def test_skill_selection_is_skipped_without_valid_skills(world, client):
    new_job(client, generation_skills=["# フロントマターのないファイル"])
    drain()
    assert "generation_skills" not in world.kinds() and "review_skills" not in world.kinds()


def test_builtin_skills_are_listed(world, client):
    skills = client.get("/api/skills").json()
    assert {s["kind"] for s in skills} == {"generation", "review"} and len(skills) == 6
