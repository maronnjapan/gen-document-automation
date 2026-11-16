"""ローカル動作確認用のモックサーバー（本番では使わない）。標準ライブラリだけで動く。

- POST /llm                          独自LLMエンドポイント（既定のリクエスト/レスポンス形式）
- GET  /wiki/rest/api/content/search Confluence 検索API（サンプルWiki 3ページ）
- POST /wiki/pages/<id>              サンプルWikiの本文を更新して版数を上げる（RAG再登録の確認用）
- POST /slack                        Slack Webhook（受信内容を標準出力に表示）
- GET  /slack                        受信したSlack通知の一覧

LLMはプロンプトの種類（作成指示の整理・Skills選択・生成・修正・レビュー）を判別し、決まった応答を返す。
初回レビューで1件指摘し、修正版で指摘が解消される流れを再現する。
"""
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PAGES = {
    "1001": {"title": "決済API 概要", "version": 1, "html": (
        "<h1>決済API</h1><p>決済APIは <code>POST /v1/payments</code> で支払いを作成します。"
        "認証はAPIキーを <code>X-Api-Key</code> ヘッダーに指定します。</p>"
        "<ul><li>タイムアウトは30秒です。</li><li>再試行時は <code>Idempotency-Key</code> ヘッダーで冪等性を保証します。</li></ul>")},
    "1002": {"title": "決済API エラーコード一覧", "version": 3, "html": (
        "<table><tr><th>コード</th><th>意味</th><th>対応</th></tr>"
        "<tr><td>400</td><td>パラメータ不正</td><td>リクエストを修正する</td></tr>"
        "<tr><td>409</td><td>冪等キーの重複</td><td>前回の結果を参照する</td></tr>"
        "<tr><td>503</td><td>一時的な障害</td><td>指数バックオフで再試行する</td></tr></table>")},
    "1003": {"title": "障害対応フロー", "version": 2, "html": (
        "<p>障害を検知したら #incident チャンネルに報告し、一次対応者がエスカレーションを判断します。</p>")},
}
SLACK = []


def section(tag, text):
    m = re.search(rf"<{tag}>\n(.*?)\n</{tag}>", text, re.S)
    return m[1] if m else ""


def llm(system, user):
    if "検索キーワード" in system:  # 作成指示の整理
        instruction = section("instruction", user)
        return json.dumps({"instruction": f"{instruction}（想定読者: 社内開発者）", "outline": ["概要", "利用手順", "エラー時の対応", "参考資料"],
                           "queries": ["決済API"] + (["障害"] if "障害" in user else [])}, ensure_ascii=False)
    if "Skill名のJSON配列" in system:  # Skillsの選択（一覧の先頭2件を選ぶ）
        return json.dumps(re.findall(r"^- ([^:\n]+):", section("skills", user), re.M)[:2], ensure_ascii=False)
    if '"issues"' in system:  # レビュー
        doc = section("document", user)
        issues = [] if "## 改訂履歴" in doc else [{"severity": "medium", "location": "文書末尾", "comment": "改訂履歴の節がありません",
                                                    "suggestion": "末尾に「## 改訂履歴」を追加し、変更内容を記載してください"}]
        return json.dumps({"summary": "構成は妥当です。" if not issues else "改訂履歴の追記が必要です。", "issues": issues}, ensure_ascii=False)
    if "修正後のMarkdown全文" in system:  # 修正
        comment = section("comment", user)
        extra = f"\n\n## 追記（修正指示への対応）\n\n{comment}\n" if comment and comment not in section("previous", user) else ""
        body = section("previous", user).split("\n## 改訂履歴")[0].rstrip()
        return f"{body}{extra}\n\n## 改訂履歴\n\n- レビュー指摘を反映しました。\n"
    refs = re.findall(r"<reference>\n# (.*?)\n(.*?)\n</reference>", user, re.S)  # 生成
    skills = re.findall(r"<skill>\n# (.*?)\n", user)
    lines = [f"# {section('instruction', user).splitlines()[0][:40] or 'ドキュメント'}", "", "## 概要", "", section("instruction", user), "",
             "## 利用手順", ""]
    lines += [f"{i}. {t}: {re.sub(r'\s+', ' ', body)[:120]}" for i, (t, body) in enumerate(refs, 1)] or ["1. 要確認"]
    lines += ["", "## 参考資料", ""] + [f"- {t}" for t, _ in refs] + ["", f"<!-- 使用したSkills: {', '.join(skills) or 'なし'} -->"]
    return "```markdown\n" + "\n".join(lines) + "\n```"


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/wiki/rest/api/content/search":
            if not self.headers.get("Authorization"):
                return self.reply(401, {"message": "Authorization header is required"})
            terms = re.findall(r'text ~ "([^"]*)"', parse_qs(url.query).get("cql", [""])[0])
            hits = [{"id": pid, "type": "page", "title": p["title"], "version": {"number": p["version"], "when": "2026-11-01T09:00:00.000Z"},
                     "body": {"storage": {"value": p["html"], "representation": "storage"}}}
                    for pid, p in PAGES.items() if any(t.strip() and t.strip() in p["title"] + p["html"] for t in terms)]
            return self.reply(200, {"results": hits, "size": len(hits)})
        if url.path == "/slack":
            return self.reply(200, SLACK)
        self.reply(404, {"message": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/llm":
            if not self.headers.get(os.environ.get("LLM_API_KEY_HEADER", "Authorization")):
                return self.reply(401, {"message": "API key is required"})
            data = self.read_json()
            return self.reply(200, {"output": llm(data.get("system", ""), data.get("prompt", ""))})
        if (m := re.fullmatch(r"/wiki/pages/(\w+)", path)) and m[1] in PAGES:
            PAGES[m[1]].update(html=self.read_json().get("html", ""), version=PAGES[m[1]]["version"] + 1)
            return self.reply(200, {"id": m[1], "version": PAGES[m[1]]["version"]})
        if path == "/slack":
            SLACK.append(text := self.read_json().get("text", ""))
            print(f"[slack] {text}", flush=True)
            return self.reply(200, {"ok": True})
        self.reply(404, {"message": "not found"})

    def log_message(self, fmt, *args):
        print(f"[mock] {self.command} {self.path.split('?')[0]} {args[1] if len(args) > 1 else ''}", flush=True)


if __name__ == "__main__":
    port = int(os.environ.get("MOCK_PORT", 8090))
    print(f"mock services listening on :{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
