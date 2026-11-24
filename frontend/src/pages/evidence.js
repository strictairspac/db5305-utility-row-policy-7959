import {heading, panel} from '../components/common/heading.js';
import {records} from '../components/common/records.js';
import {topic} from '../constants/project.js';
import {date, el, esc} from '../utils/dom.js';
import {submit} from '../hooks/submit.js';

// Page size only controls how many rows are fetched per request.  It never
// changes the stored report or its digest, and every row is reachable by
// walking the offsets, so paging is complete and duplicate-free.
const PAGE_SIZE = 100;

export async function loadEvidencePage(api, evidence, section, query) {
  const size = Math.min(Math.max(Number(query.size) || PAGE_SIZE, 1), 1000);
  const offset = Math.max(Number(query.offset) || 0, 0);
  const page = await api(`/evidence/${evidence.id}/page?section=${encodeURIComponent(section)}&size=${size}&offset=${offset}`);
  return {...page, section, size, offset};
}

export function evidencePage(state, identity, view) {
  const current = state.evidence.find(row=>row.id===identity) || state.evidence[0];
  if (!current) return heading(topic.modules[3]) + '<p class="empty">执行完成后，可查看、复核和导出证据。</p>';
  const metrics = Object.entries(current.metrics || {}).map(([label,value])=>`<div><dt>${esc(label)}</dt><dd>${esc(value)}</dd></div>`).join('');
  const review = current.reviewer ? `<p data-testid="reviewed">${esc(current.reviewer)} · ${esc(date(current.reviewed_at))}</p><p>${esc(current.review_note)}</p>`
    : !current.complete ? '<p class="note">证据未覆盖全部核对组合，不能签署。</p>'
    : state.session.role==='reviewer'?'<form id="review-form"><label>复核意见<textarea name="note" maxlength="500" required></textarea></label><button>签署复核</button></form>'
    : '<p class="note">待复核员签署</p>';
  const history = state.evidence.map(row=>[`<a href="#evidence/${row.id}">执行 ${row.run_id}</a>`, esc(row.summary), esc(row.reviewer || '待复核')]);
  const sections = current.sections.map(item=>{
    if (view && view.section===item.title) return panel(item.title, pager(view, current));
    return panel(item.title, `<p class="note">共 ${item.count} 行 · <a href="#evidence/${current.id}/${encodeURIComponent(item.title)}">翻查全部</a></p>`);
  }).join('');
  return `${heading(topic.modules[3], current.summary, `<button id="download" data-id="${current.id}">导出证据</button>`)}
    <div class="spread evidence-top"><dl class="metrics">${metrics}</dl><aside class="brief"><h2>复核签署</h2>${review}<details><summary>结果校验码</summary><code class="digest">${esc(current.digest)}</code></details></aside></div>
    ${sections}<details><summary>历史证据</summary>${records(['执行','结论','复核'], history)}</details>`;
}

function entries(rows) {
  if (!rows.length) return '<p class="empty">当前没有记录。</p>';
  const keys = Array.from(new Set(rows.flatMap(Object.keys)));
  return records(keys, rows.map(row=>keys.map(key=>esc(row[key]))));
}

function pager(view, current) {
  const {rows, total, offset, size, section} = view;
  const start = total ? offset + 1 : 0;
  const end = Math.min(offset + rows.length, total);
  const base = `#evidence/${current.id}/${encodeURIComponent(section)}`;
  const link = (next, label, available)=>available
    ? `<a href="${base}?size=${size}&offset=${next}">${label}</a>`
    : `<span aria-disabled="true">${label}</span>`;
  return `${entries(rows)}
    <nav class="pager" aria-label="翻页">${link(Math.max(0, offset - size), '上一页', offset > 0)}
    <span>第 ${start}–${end} 行，共 ${total} 行 · 每页 ${size}</span>
    ${link(offset + size, '下一页', end < total)}</nav>`;
}

export function bindEvidence(api, update, fail) {
  const button = document.getElementById('download');
  if (!button) return;
  const identity = button.dataset.id;
  button.addEventListener('click', async()=>{
    try {
      const response = await api(`/evidence/${identity}/download`, {response:true});
      const url = URL.createObjectURL(await response.blob());
      const link = el('a');
      link.href = url;
      link.download = `evidence-${identity}.json`;
      link.click();
      setTimeout(()=>URL.revokeObjectURL(url), 1000);
    } catch (error) { fail(error); }
  });
  submit('review-form', async data=>{
    await api(`/evidence/${identity}/review`, {method:'POST', data:{note:data.get('note')}});
    await update();
  }, fail);
}
