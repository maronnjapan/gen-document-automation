"""RAGアダプター。RAG DBの仕様確定まではRedisを使ったMockで、実DBへはこの3関数の中身だけを差し替える（ベクトル変換はDB側の責務）。"""
import json
import common as c

def get_indexed_version(page_id):  # 登録済みの版（未登録ならNone）。RAG側に版管理がない前提でRedisで補完する
    return c.R.hget("rag:versions", page_id)

def upsert_page(page_id, version, chunks, metadata):  # ページ単位で旧版のチャンクを置き換え、版と同時に更新する
    docs = c.dumps([{**metadata, "text": t, "version": version} for t in chunks])
    c.R.pipeline().set(f"rag:chunks:{page_id}", docs).hset("rag:versions", page_id, version).execute()

def search(query, page_ids, k=5):  # 現行版のチャンクだけを対象に、類似度（Mockでは共通する文字の数）の高い順にk件返す
    docs = [d for p in page_ids for d in json.loads(c.R.get(f"rag:chunks:{p}") or "[]") if d["version"] == get_indexed_version(p)]
    return sorted(docs, key=lambda d: -len(set(query) & set(d["text"])))[:k]
