"""RAGアダプター（版管理・差し替え）とSkills（解析・LLMによる選択・本文の再投入）の検証。"""
import generator
import rag
from harness import c, drain, new_job

SKILL_A = "---\nname: skill-a\ndescription: 手順書の書き方\n---\n# A\n手順は番号付きリストで書く"
SKILL_B = "---\nname: skill-b\ndescription: 設計書の書き方\n---\n# B\n代替案を比較する"
SKILL_R = "---\nname: security\ndescription: セキュリティ観点\n---\n秘密情報が含まれていないか確認する"
rag_upsert = rag.upsert_page  # 差し替え前の実装


def test_rag_replaces_old_version_and_excludes_stale_chunks(world):
    rag.upsert_page("p1", 1, ["旧版の決済API説明"], {"title": "決済"})
    assert rag.get_indexed_version("p1") == 1 and rag.get_indexed_version("p2") is None
    rag.upsert_page("p1", 2, ["新版の決済API説明", "決済の手順"], {"title": "決済"})
    hits = rag.search("決済API", ["p1"], 5)
    assert rag.get_indexed_version("p1") == 2 and {h["text"] for h in hits} == {"新版の決済API説明", "決済の手順"}
    assert hits[0]["text"] == "新版の決済API説明"  # 類似度順

    # RAG側に旧版のチャンクが残っていても、版数が一致しないものは検索結果から除外する
    c.R.set("rag:chunks:p1", c.dumps([{"page_id": "p1", "version": 1, "text": "旧版", "metadata": {}}]))
    assert rag.search("旧版", ["p1"], 5) == []
    assert rag.search("決済", ["p9"], 5) == []


def test_only_new_or_updated_wiki_pages_are_reindexed(world, client, monkeypatch):
    upserts = []
    monkeypatch.setattr(rag, "upsert_page", lambda *a: (upserts.append(a[:2]), rag_upsert(*a))[1])
    world.pages["102"] = {"title": "障害対応", "version": 3, "html": "<p>障害時は再試行します。</p>"}
    new_job(client)
    drain()
    assert sorted(upserts) == [("101", 1), ("102", 3)]

    world.pages["101"].update(version=2, html="<p>決済APIは<b>v2</b>でPUT /paymentsに変わりました。</p>")
    new_job(client)
    drain()
    assert sorted(upserts) == [("101", 1), ("101", 2), ("102", 3)]  # 同じ版の102は再登録しない
    assert "PUT /payments" in world.prompts("generate")[-1] and "POST /payments" not in world.prompts("generate")[-1]
    assert "**v2**" in c.R.get("rag:chunks:101")  # HTML(storage形式)をMarkdownに変換して登録


def test_rag_adapter_can_be_swapped(world, client, monkeypatch):
    store = {}  # 別実装（例: 実RAG DB）への差し替えを想定したインメモリ実装
    monkeypatch.setattr(rag, "get_indexed_version", lambda pid: store.get(pid, (None,))[0])
    monkeypatch.setattr(rag, "upsert_page", lambda pid, ver, chunks, meta: store.__setitem__(pid, (ver, chunks, meta)))
    monkeypatch.setattr(rag, "search", lambda q, ids, k: [{"text": t, "metadata": store[i][2]} for i in ids for t in store[i][1]][:k])
    jid = new_job(client)
    drain()
    assert c.job(jid)["status"] == "completed" and store["101"][0] == 1 and not c.R.exists("rag:chunks:101")
    assert "POST /payments" in world.prompts("generate")[0]


def test_chunking_keeps_paragraphs_and_limits_size():
    md = "# 見出し\n\n" + "あ" * 30 + "\n\n" + "い" * 30 + "\n\n" + "う" * 130
    chunks = generator.chunk(md, 50)
    assert chunks[0] == "# 見出し\n\n" + "あ" * 30 and all(len(ch) <= 50 for ch in chunks[1:])
    assert "".join(chunks).count("う") == 130


def test_llm_selects_skills_and_only_selected_bodies_are_used(world, client):
    world.skill_choice = {"generation_skills": ["skill-a", "unknown", "skill-a"], "review_skills": ["security"]}
    jid = new_job(client, generation_skills=[SKILL_A, SKILL_B], review_skills=[SKILL_R])

    j = c.job(jid)  # Skillsもオブジェクトストレージに保存し、ジョブには参照情報を持つ
    assert j["generation_skills"] == [f"jobs/{jid}/skills/generation/00.md", f"jobs/{jid}/skills/generation/01.md"]
    assert c.get_text(j["review_skills"][0]) == SKILL_R
    drain()

    menu = world.prompts("generation_skills")[0]  # まず名前と説明の一覧だけをLLMへ渡す
    assert "- skill-a: 手順書の書き方" in menu and "- skill-b: 設計書の書き方" in menu and "番号付きリスト" not in menu
    gen = world.prompts("generate")[0]  # 選ばれたSkillの本文だけを追加して再度LLMを呼ぶ
    assert gen.count("手順は番号付きリストで書く") == 1 and "代替案を比較する" not in gen and "unknown" not in gen
    assert "秘密情報が含まれていないか確認する" in world.prompts("review")[0]
    assert "skill-a" not in world.prompts("review_skills")[0]  # 生成用とレビュー用を区別する


def test_skill_selection_is_skipped_without_skills(world, client):
    new_job(client)
    drain()
    assert "generation_skills" not in world.kinds() and "review_skills" not in world.kinds()


def test_invalid_skill_is_rejected(world, client):
    res = client.post("/api/jobs", json={"name": "doc", "instruction": "x", "generation_skills": ["# no front matter"]})
    assert res.status_code == 400 and "SKILL.md" in res.json()["detail"]


def test_builtin_skills_are_listed(world, client):
    skills = client.get("/api/skills").json()
    assert {s["kind"] for s in skills} == {"generation", "review"}
    assert all(c.parse_skill(s["text"])[0]["description"] for s in skills)


def test_wiki_search_is_skipped_without_queries(world, client):
    world.llm_hook = lambda kind, user: '{"instruction": "x", "outline": [], "queries": ["", " \\"\\" "]}' if kind == "plan" else None
    jid = new_job(client)
    drain()
    assert c.job(jid)["status"] == "completed" and not [u for _, u, _ in world.http_log if "wiki.test" in u]
    assert "<reference>" not in world.prompts("generate")[0]
