"""RAGアダプター。RAGデータベースの仕様が未確定のため、現在はRedisを使ったMock実装。
実DBへ切り替えるときは、この3関数の中身だけを差し替える（ベクトル変換はRAG DB側の責務）。"""
import json, math

import common as c

def get_indexed_version(page_id):
    """登録済みページのバージョン（未登録ならNone）。RAG側に版管理がない場合に備えRedisで補完する。"""
    return int(v) if (v := c.R.get(f"rag:version:{page_id}")) else None

def upsert_page(page_id, version, chunks, metadata):
    """ページ単位で旧版のチャンクを丸ごと置き換え、版数と同時に更新する。"""
    docs = [{"page_id": page_id, "version": version, "text": t, "metadata": metadata} for t in chunks]
    c.R.pipeline().set(f"rag:chunks:{page_id}", c.dumps(docs)).set(f"rag:version:{page_id}", version).execute()

def _grams(text):  # Mock用の簡易類似度: 文字bigram（日本語でも分かち書き不要）
    t = text.lower()
    return {t[i:i + 2] for i in range(len(t) - 1)}

def search(query, page_ids, k=5):
    """指定ページの現行版チャンクから類似度の高い順にk件返す。旧版が混在しないよう版数で除外する。"""
    q, hits = _grams(query), []
    for pid in page_ids:
        current = get_indexed_version(pid)
        for d in json.loads(c.R.get(f"rag:chunks:{pid}") or "[]"):
            if d["version"] == current: hits.append({**d, "score": len(q & (g := _grams(d["text"]))) / math.sqrt(len(g) + 1)})
    return sorted(hits, key=lambda h: -h["score"])[:k]
