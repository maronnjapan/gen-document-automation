"""RAG アダプター。RAG データベースの仕様が確定するまでは Redis を使った Mock 実装。
実 DB へ切り替えるときは、この 3 関数の中身だけを差し替える（ベクトル変換は RAG データベース側の責務）。"""
import json
import common

VERSIONS_KEY = "rag:versions"  # ページID → 登録済みの版（RAG 側に版の管理機能がない前提で Redis で補完する）

def get_indexed_version(page_id):
    """登録済みの版を返す（未登録なら None）。"""
    return common.redis_client.hget(VERSIONS_KEY, page_id)

def upsert_page(page_id, version, chunks, metadata):
    """ページ単位で旧版のチャンクを丸ごと置き換え、版と同時に更新する。"""
    documents = [{**metadata, "text": chunk, "version": version} for chunk in chunks]
    pipeline = common.redis_client.pipeline()
    pipeline.set(f"rag:chunks:{page_id}", common.to_json(documents))
    pipeline.hset(VERSIONS_KEY, page_id, version)
    pipeline.execute()

def search(query, page_ids, k=5):
    """指定ページの現行版のチャンクから、類似度の高い順に k 件を返す（Mock の類似度は共通する文字の数）。"""
    candidates = []
    for page_id in page_ids:
        current_version = get_indexed_version(page_id)
        documents = json.loads(common.redis_client.get(f"rag:chunks:{page_id}") or "[]")
        candidates += [doc for doc in documents if doc["version"] == current_version]  # 旧版のチャンクは除外する
    return sorted(candidates, key=lambda doc: len(set(query) & set(doc["text"])), reverse=True)[:k]
