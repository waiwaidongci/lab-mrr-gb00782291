import {api,download} from '../api/client.js';
import {empty,errorSlot} from '../components/common/layout.js';
import {table} from '../components/common/table.js';
import {bindForm} from '../hooks/forms.js';
import {state} from '../stores/session.js';
import {esc,time} from '../utils/html.js';

const issueTable = issues => issues.length
  ? table(issues, [{key:'级别',label:'级别'},{key:'订阅编号',label:'订阅编号'},{key:'说明',label:'说明'}])
  : '<p class="hint">暂无需要修正的问题。</p>';

const filesTable = files => table(files.map(row => ({
  类型: row.kind === 'site' ? '场地台账' : '移交单',
  场地: row.site || '—',
  文件: row.name,
  校验: row.checksum.slice(0,12),
  导入人: row.owner,
  导入时间: time(row.created),
})));

export async function ledger() {
  const data = await api('/ledger');
  const analyst = state.account.role === 'analyst';
  const forms = analyst ? `<section><h2>上传场地台账</h2><p>每处场地上传自己完整的订阅生效台账。同一场地再次上传会以新文件替换旧版本，原始文件按规范化内容去重。</p><form id="site-upload" class="inline-form"><label>场地名称<input name="site" required maxlength="80" placeholder="例如 澄湾一号实验空间"></label><label>台账 CSV<input name="file" type="file" accept=".csv,text/csv" required></label><button type="submit">保存场地台账</button>${errorSlot}</form></section><section><h2>上传移交单</h2><p>移交单记录某处场地自生效日起把订阅移交给另一处场地。订阅编号填 <code>*</code> 表示整处场地。移交单可以晚于台账到达。</p><form id="handover-upload" class="inline-form"><label>移交单 CSV<input name="file" type="file" accept=".csv,text/csv" required></label><button type="submit">保存移交单</button>${errorSlot}</form><p><button type="button" id="handover-example" class="secondary">下载移交单样例</button></p><p id="handover-error" role="alert" class="error"></p></section>` : '';
  const snapshot = data.snapshot;
  const summary = snapshot ? `<section><h2>合并台账</h2><dl class="metrics"><div><dt>已采纳生效记录</dt><dd>${summaryValue(snapshot.summary.adopted)}</dd></div><div><dt>撤下越界</dt><dd>${summaryValue(snapshot.summary.withdrawn)}</dd></div><div><dt>暂扣待移交</dt><dd>${summaryValue(snapshot.summary.withheld)}</dd></div><div><dt>参与场地</dt><dd>${esc((snapshot.summary.sites || []).join('、') || '—')}</dd></div></dl><p class="hint">数据版本 #${snapshot.dataset_id ?? '—'} · 合并文件 ${snapshot.files} 份 · ${time(snapshot.created)} · ${snapshot.summary.ready ? '可用于分析' : '存在需修正的问题'}</p>${snapshot.dataset_id ? `<a class="button" href="#2">用此版本运行收入桥</a>` : ''}</section>` : empty('先导入各地场地台账。');
  const entries = snapshot && data.entries.length ? `<section><h2>生效明细与采纳结果</h2>${table(data.entries)}</section>` : '';
  const issues = snapshot ? `<section><h2>核对问题</h2>${issueTable(data.issues)}</section>` : '';
  return `${forms}${summary}${entries}${issues}<section><h2>已导入文件</h2>${filesTable(data.files)}</section>`;
}

function summaryValue(value) {
  return esc(String(value ?? 0));
}

export function bindLedger(root, refresh) {
  bindForm(root, '#site-upload', async form => {
    const file = form.elements.file.files[0];
    const result = await api('/ledger/sites', {site: form.elements.site.value, name: file.name, csv: await file.text()});
    const summary = result.summary;
    state.status = result.file_created
      ? `已保存 ${file.name}。采纳 ${summary.adopted} 条，撤下越界 ${summary.withdrawn} 条，暂扣 ${summary.withheld} 条。`
      : '该文件与已导入版本一致，未重复入库。';
    if (result.dataset_id) state.datasetId = result.dataset_id;
  }, refresh);
  bindForm(root, '#handover-upload', async form => {
    const file = form.elements.file.files[0];
    const result = await api('/ledger/handovers', {name: file.name, csv: await file.text()});
    const summary = result.summary;
    state.status = result.file_created
      ? `已保存移交单 ${file.name}。采纳 ${summary.adopted} 条，撤下越界 ${summary.withdrawn} 条，暂扣 ${summary.withheld} 条。`
      : '该移交单已导入，未重复入库。';
    if (result.dataset_id) state.datasetId = result.dataset_id;
  }, refresh);
  root.querySelector('#handover-example')?.addEventListener('click', async () => {
    try {await download('/handover-example.csv', 'chengwan-handover-example.csv');}
    catch (error) {root.querySelector('#handover-error').textContent = error.message;}
  });
}
