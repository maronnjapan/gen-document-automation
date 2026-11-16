'use strict';

// ---- 共通 ----------------------------------------------------------------

const STATUS = {
  queued: ['待機中', 'running'],
  generating: ['生成中', 'running'],
  reviewing: ['レビュー中', 'running'],
  completed: ['自動レビュー完了', 'ok'],
  limit_reached: ['レビュー上限到達', 'warn'],
  failed: ['異常終了', 'ng'],
  approved: ['承認済み', 'ok'],
};
const RUNNING = ['queued', 'generating', 'reviewing'];
const SEVERITY = { high: '重大', medium: '中', low: '軽微' };

async function api(path, options = {}) {
  const init = { ...options };
  if (options.body !== undefined) {
    init.headers = { 'Content-Type': 'application/json' };
    init.body = JSON.stringify(options.body);
  }
  const res = await fetch(path, init);
  const text = await res.text();
  if (!res.ok) {
    let detail = text;
    try {
      const d = JSON.parse(text).detail;
      detail = typeof d === 'string' ? d : (d || []).map(e => `${(e.loc || []).slice(1).join('.')}: ${e.msg}`).join(' / ');
    } catch (_) { /* JSON以外の応答はそのまま表示 */ }
    throw new Error(`${res.status} ${detail || res.statusText}`);
  }
  return (res.headers.get('content-type') || '').includes('application/json') ? JSON.parse(text) : text;
}

// DOM要素を作る。文字列の子要素はテキストとして追加する（HTMLとして解釈しない）
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === 'class') node.className = value;
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (value === true) node.setAttribute(key, '');
    else if (value !== false && value !== null && value !== undefined) node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child !== null && child !== undefined && child !== false) node.append(child instanceof Node ? child : String(child));
  }
  return node;
}

function badge(status) {
  const [label, cls] = STATUS[status] || [status, ''];
  return el('span', { class: `badge ${cls}` }, label);
}

function formatDate(iso) {
  return iso ? new Date(iso).toLocaleString('ja-JP') : '-';
}

function showMessage(target, text, isError = false) {
  target.textContent = text;
  target.classList.toggle('error', isError);
}

// このブラウザで作成・表示したジョブのID（ジョブ一覧はサーバーに持たず、ブラウザに記録する）
const JOBS_KEY = 'doc-automation:jobs';

function rememberedJobs() {
  try {
    return JSON.parse(localStorage.getItem(JOBS_KEY)) || [];
  } catch (_) {
    return [];
  }
}

function saveRememberedJobs(ids) {
  try {
    localStorage.setItem(JOBS_KEY, JSON.stringify(ids.slice(0, 50)));
  } catch (_) { /* 保存できなくても画面の動作は続ける */ }
}

function rememberJob(id) {
  saveRememberedJobs([id, ...rememberedJobs().filter(x => x !== id)]);
}

// SKILL.md のYAMLフロントマターから name と description を取り出す（表示・事前チェック用）
function frontMatter(text) {
  const m = /^\s*---[ \t]*\n([\s\S]*?)\n---[ \t]*(\n|$)/.exec(text);
  if (!m) return null;
  const get = key => {
    const r = new RegExp(`^${key}:[ \\t]*(.*)$`, 'm').exec(m[1]);
    return r ? r[1].trim().replace(/^(["'])(.*)\1$/, '$2') : '';
  };
  return get('name') ? { name: get('name'), description: get('description') } : null;
}

// ---- Markdownの簡易レンダラー（HTMLはすべてエスケープし、http(s)のリンクだけを許可） ----

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function inlineMd(text) {
  return text.split(/(`[^`\n]+`)/).map((part, i) => {
    if (i % 2) return `<code>${escapeHtml(part.slice(1, -1))}</code>`;
    return escapeHtml(part)
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/~~(.+?)~~/g, '<del>$1</del>')
      .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  }).join('');
}

const LIST_ITEM = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
const HR = /^\s*([-*_])(\s*\1){2,}\s*$/;
const TABLE_SEP = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;

function isBlockStart(line) {
  return /^(#{1,6}\s|```|~~~|\s*>|\s*\|)/.test(line) || LIST_ITEM.test(line) || HR.test(line);
}

function renderMarkdown(src) {
  const lines = src.replace(/\r\n?/g, '\n').replace(/<!--[\s\S]*?-->/g, '').split('\n');  // HTMLコメントは表示しない
  const html = [];
  let i = 0;
  let m;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }

    if ((m = /^\s*(```|~~~)/.exec(line))) {  // コードブロック
      const fence = m[1];
      const buf = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith(fence)) buf.push(lines[i++]);
      i++;
      html.push(`<pre><code>${escapeHtml(buf.join('\n'))}</code></pre>`);
      continue;
    }
    if ((m = /^(#{1,6})\s+(.*?)\s*#*\s*$/.exec(line))) {  // 見出し
      html.push(`<h${m[1].length}>${inlineMd(m[2])}</h${m[1].length}>`);
      i++;
      continue;
    }
    if (HR.test(line)) { html.push('<hr>'); i++; continue; }

    if (/^\s*>/.test(line)) {  // 引用
      const buf = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) buf.push(lines[i++].replace(/^\s*>\s?/, ''));
      html.push(`<blockquote>${renderMarkdown(buf.join('\n'))}</blockquote>`);
      continue;
    }
    if (/^\s*\|/.test(line) && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1])) {  // 表
      const cells = row => row.trim().replace(/^\||\|$/g, '').split('|').map(c => inlineMd(c.trim()));
      const head = cells(line);
      const rows = [];
      i += 2;
      while (i < lines.length && /^\s*\|/.test(lines[i])) rows.push(cells(lines[i++]));
      html.push('<div class="table-wrap"><table><thead><tr>' + head.map(c => `<th>${c}</th>`).join('') + '</tr></thead><tbody>'
        + rows.map(r => '<tr>' + r.map(c => `<td>${c}</td>`).join('') + '</tr>').join('') + '</tbody></table></div>');
      continue;
    }
    if (LIST_ITEM.test(line)) {  // 箇条書き（インデントで入れ子）
      const stack = [];
      let out = '';
      while (i < lines.length && (m = LIST_ITEM.exec(lines[i]))) {
        const indent = m[1].replace(/\t/g, '  ').length;
        const tag = /\d/.test(m[2]) ? 'ol' : 'ul';
        if (!stack.length || indent > stack[stack.length - 1].indent) {
          out += `<${tag}>`;
          stack.push({ indent, tag });
        } else {
          while (stack.length > 1 && indent < stack[stack.length - 1].indent) out += `</li></${stack.pop().tag}>`;
          out += '</li>';
        }
        out += `<li>${inlineMd(m[3])}`;
        i++;
        while (i < lines.length && /^\s+\S/.test(lines[i]) && !LIST_ITEM.test(lines[i])) out += ' ' + inlineMd(lines[i++].trim());
      }
      while (stack.length) out += `</li></${stack.pop().tag}>`;
      html.push(out);
      continue;
    }
    const buf = [lines[i++].trim()];  // 段落
    while (i < lines.length && lines[i].trim() && !isBlockStart(lines[i])) buf.push(lines[i++].trim());
    html.push(`<p>${buf.map(inlineMd).join('<br>')}</p>`);
  }
  return html.join('\n');
}

// ---- 新規生成画面 --------------------------------------------------------

async function initNewJobPage() {
  const form = document.getElementById('new-job');
  const message = document.getElementById('form-message');
  const added = { generation: [], review: [] };  // アップロード・貼り付けで追加したSKILL.md

  function renderAdded(kind) {
    const list = document.querySelector(`[data-kind="${kind}"] .chips`);
    list.replaceChildren(...added[kind].map((skill, idx) => el('li', {},
      `${skill.name}（${skill.source}）`,
      el('button', { type: 'button', title: '削除', 'aria-label': `${skill.name} を削除`, onclick: () => { added[kind].splice(idx, 1); renderAdded(kind); } }, '×'),
    )));
  }

  function addSkill(kind, text, source) {
    const meta = frontMatter(text);
    if (!meta) {
      showMessage(message, `${source}: SKILL.md には name を含むYAMLフロントマター（--- で囲む）が必要です`, true);
      return false;
    }
    added[kind].push({ name: meta.name, text, source });
    renderAdded(kind);
    showMessage(message, '');
    return true;
  }

  for (const fieldset of document.querySelectorAll('fieldset[data-kind]')) {
    const kind = fieldset.dataset.kind;
    fieldset.querySelector('input[type="file"]').addEventListener('change', async event => {
      for (const file of event.target.files) addSkill(kind, await file.text(), file.name);
      event.target.value = '';
    });
    const paste = fieldset.querySelector('textarea');
    fieldset.querySelector('.add-pasted').addEventListener('click', () => {
      if (paste.value.trim() && addSkill(kind, paste.value, '貼り付け')) paste.value = '';
    });
  }

  try {
    const skills = await api('/api/skills');
    for (const kind of ['generation', 'review']) {
      const items = skills.filter(s => s.kind === kind).map(s => {
        const meta = frontMatter(s.text) || { name: '(名前なし)', description: '' };
        return el('label', { class: 'skill-option' },
          el('input', { type: 'checkbox', 'data-text': s.text }),
          el('span', {}, meta.name, el('small', {}, meta.description)));
      });
      document.querySelector(`[data-kind="${kind}"] .builtin`).replaceChildren(...(items.length ? items : [el('p', { class: 'empty' }, '同梱のSkillsはありません')]));
    }
  } catch (e) {
    showMessage(message, `同梱Skillsを取得できませんでした: ${e.message}`, true);
  }

  form.addEventListener('submit', async event => {
    event.preventDefault();
    const data = new FormData(form);
    const selected = kind => [
      ...[...document.querySelectorAll(`[data-kind="${kind}"] .builtin input:checked`)].map(input => input.dataset.text),
      ...added[kind].map(s => s.text),
    ];
    const body = {
      name: data.get('name').trim(),
      doc_type: data.get('doc_type').trim(),
      instruction: data.get('instruction').trim(),
      viewpoints: data.get('viewpoints').trim(),
      generation_skills: selected('generation'),
      review_skills: selected('review'),
      max_reviews: Number(data.get('max_reviews') || 10),
    };
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    showMessage(message, '送信中…');
    try {
      const job = await api('/api/jobs', { method: 'POST', body });
      rememberJob(job.job_id);
      try {
        await api(`/api/jobs/${encodeURIComponent(job.job_id)}/enqueue`, { method: 'POST' });  // 生成用キューへ通知
      } catch (_) { /* 通知に失敗したジョブは、ジョブ詳細画面から生成依頼を再送できる */ }
      location.href = `job.html?id=${encodeURIComponent(job.job_id)}`;
    } catch (e) {
      showMessage(message, `実行できませんでした: ${e.message}`, true);
      button.disabled = false;
    }
  });

  async function loadJobs() {
    const tbody = document.querySelector('#jobs tbody');
    try {
      const ids = rememberedJobs();
      const results = await Promise.all(ids.map(id => api(`/api/jobs/${encodeURIComponent(id)}`).catch(e => (e.message.startsWith('404') ? null : undefined))));
      saveRememberedJobs(ids.filter((_, i) => results[i] !== null));  // 削除されたジョブは一覧から外す
      const jobs = results.filter(Boolean);
      tbody.replaceChildren(...(jobs.length ? jobs.map(job => el('tr', {},
        el('td', {}, el('a', { href: `job.html?id=${encodeURIComponent(job.job_id)}` }, job.name || job.job_id)),
        el('td', {}, job.doc_type || '-'),
        el('td', {}, badge(job.status)),
        el('td', {}, job.revision ? `第${job.revision}版` : '-'),
        el('td', {}, `${job.review_count}/${job.max_reviews}`),
        el('td', {}, formatDate(job.updated_at)),
      )) : [el('tr', {}, el('td', { colspan: 6, class: 'empty' }, 'このブラウザで作成・表示したジョブはまだありません'))]));
    } catch (e) {
      tbody.replaceChildren(el('tr', {}, el('td', { colspan: 6, class: 'error' }, `一覧を取得できませんでした: ${e.message}`)));
    }
  }
  await loadJobs();
  setInterval(loadJobs, 5000);
}

// ---- ジョブ詳細画面 ------------------------------------------------------

async function initJobPage() {
  const id = new URLSearchParams(location.search).get('id');
  const $ = sel => document.querySelector(sel);
  const message = $('#message');
  if (!id) {
    showMessage(message, 'ジョブIDが指定されていません', true);
    return;
  }
  rememberJob(id);
  let job = null;
  let shown = { revision: null, text: '' };
  let followLatest = true;
  let rawView = false;
  let timer = null;

  function renderSummary() {
    document.title = `${job.name} - ドキュメント自動生成`;
    $('#title').textContent = job.name;
    const rows = [
      ['ジョブID', job.job_id],
      ['種類', job.doc_type || '-'],
      ['実行状態', badge(job.status)],
      ['最新版', job.revision ? `第${job.revision}版` : 'まだありません'],
      ['レビューサイクル', job.cycle],
      ['レビュー回数', `${job.review_count} / ${job.max_reviews}`],
      ['作成日時', formatDate(job.created_at)],
      ['更新日時', formatDate(job.updated_at)],
    ];
    if (job.comment) rows.push(['人間の修正指示', job.comment]);
    if (job.final_key) rows.push(['確定版', job.final_key]);
    if (job.error) rows.push(['エラー', el('span', { class: 'error' }, job.error)]);
    $('#meta').replaceChildren(...rows.flatMap(([k, v]) => [el('dt', {}, k), el('dd', {}, v)]));
    $('#instruction').textContent = job.instruction;
    $('#viewpoints').textContent = job.viewpoints || '（指定なし）';
    $('#pending').hidden = job.status !== 'queued';
    $('#resend').disabled = false;
  }

  function renderReview() {
    const review = job.review;
    const box = $('#review');
    if (!review) {
      box.replaceChildren(el('p', { class: 'empty' }, RUNNING.includes(job.status) ? '自動レビュー待ちです' : 'レビュー結果はありません'));
      return;
    }
    const issues = review.issues || [];
    box.replaceChildren(...[
      el('p', {}, el('strong', {}, `第${job.reviewed_revision}版のレビュー: `), issues.length ? `指摘 ${issues.length} 件` : '指摘なし'),
      review.summary ? el('p', {}, review.summary) : null,
      issues.length ? el('ul', { class: 'issues' }, issues.map(issue => el('li', {},
        el('div', {}, el('span', { class: `sev ${issue.severity || ''}` }, `[${SEVERITY[issue.severity] || issue.severity || '-'}]`), issue.location || ''),
        el('div', {}, issue.comment),
        issue.suggestion ? el('div', { class: 'hint' }, `修正案: ${issue.suggestion}`) : null,
      ))) : null,
    ].filter(Boolean));
  }

  function renderVersions() {
    const select = $('#version');
    const current = select.value;
    select.replaceChildren(...Array.from({ length: job.revision }, (_, k) => job.revision - k).map(rev =>
      el('option', { value: rev }, rev === job.revision ? `第${rev}版（最新）` : `第${rev}版`)));
    select.disabled = !job.revision;
    if (!followLatest && current) select.value = current;
  }

  function renderDocument() {
    const preview = $('#preview');
    const raw = $('#raw');
    if (!shown.revision) {
      preview.replaceChildren(el('p', { class: 'empty' }, 'ドキュメントはまだ生成されていません'));
      raw.textContent = '';
    } else {
      preview.innerHTML = renderMarkdown(shown.text);  // renderMarkdownはHTMLをエスケープ済み
      raw.textContent = shown.text;
    }
    preview.hidden = rawView;
    raw.hidden = !rawView;
    $('#toggle').textContent = rawView ? 'プレビュー表示' : '原文表示';
    $('#download').disabled = !shown.revision;
  }

  async function showRevision(rev) {
    try {
      shown = { revision: rev, text: await api(`/api/jobs/${encodeURIComponent(id)}/drafts/${rev}`) };
      renderDocument();
    } catch (e) {
      showMessage(message, `第${rev}版を取得できませんでした: ${e.message}`, true);
    }
  }

  function updateButtons() {
    const finished = !RUNNING.includes(job.status);
    $('#revise').disabled = !finished || !$('#comment').value.trim();
    $('#approve').disabled = !finished || !job.markdown_key;
    $('#human-hint').textContent = finished ? '' : '自動生成・自動レビューの実行中は操作できません。';
  }

  async function refresh() {
    clearTimeout(timer);
    try {
      job = await api(`/api/jobs/${encodeURIComponent(id)}`);
    } catch (e) {
      showMessage(message, `ジョブを取得できませんでした: ${e.message}`, true);
      timer = setTimeout(refresh, 5000);
      return;
    }
    renderSummary();
    renderReview();
    renderVersions();
    updateButtons();
    if (followLatest && job.revision && shown.revision !== job.revision) await showRevision(job.revision);
    if (!job.revision) renderDocument();
    if (RUNNING.includes(job.status)) timer = setTimeout(refresh, 3000);
  }

  async function act(button, path, body, done) {
    button.disabled = true;
    try {
      await api(path, { method: 'POST', body });
      showMessage(message, done);
      followLatest = true;
    } catch (e) {
      showMessage(message, e.message, true);
    }
    await refresh();
  }

  $('#version').addEventListener('change', event => {
    const rev = Number(event.target.value);
    followLatest = rev === job.revision;
    showRevision(rev);
  });
  $('#toggle').addEventListener('click', () => { rawView = !rawView; renderDocument(); });
  $('#download').addEventListener('click', () => {
    const url = URL.createObjectURL(new Blob([shown.text], { type: 'text/markdown;charset=utf-8' }));
    const link = el('a', { href: url, download: `${(job.name || 'document').replace(/[\\/:*?"<>|]/g, '_')}_v${shown.revision}.md` });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);  // ダウンロード開始前に破棄しない
  });
  $('#comment').addEventListener('input', () => job && updateButtons());
  $('#revise').addEventListener('click', async event => {
    const comment = $('#comment').value.trim();
    await act(event.target, `/api/jobs/${encodeURIComponent(id)}/revise`, { comment },
      '修正指示を受け付けました。新しいレビューサイクルで再修正します。');
    if (!message.classList.contains('error')) $('#comment').value = '';
    updateButtons();
  });
  $('#approve').addEventListener('click', async event => {
    if (!confirm(`第${job.revision}版を承認して確定版にしますか？`)) return;
    await act(event.target, `/api/jobs/${encodeURIComponent(id)}/approve`, undefined, '承認しました。最新版を確定版として保存しました。');
  });
  $('#resend').addEventListener('click', event => act(event.target, `/api/jobs/${encodeURIComponent(id)}/enqueue`, undefined, '生成依頼を送信しました。'));

  await refresh();
}
