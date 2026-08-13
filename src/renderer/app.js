'use strict';

/* UI của Alice portable.
 *
 * Renderer không có `require`, không có node — nó chỉ gọi được `window.alice` do
 * preload phơi ra. Mọi thứ hiển thị ở đây (câu trả lời của model, output của tool)
 * là nội dung KHÔNG đáng tin, nên đường duy nhất vào DOM đi qua `escapeHtml`. */

/* Stub cho lúc mở index.html bằng trình duyệt thường để soi giao diện.
 * Trong app thật, preload đã gán `window.alice` trước khi file này chạy, nên khối
 * này không bao giờ chạy — nó chỉ tồn tại để xem UI mà không phải bật cả Electron. */
if (!window.alice) {
  const noop = () => {};
  window.alice = {
    status: async () => ({
      dataDir: '(xem thử)', messageCount: 0,
      engine: { available: true, source: 'preview', path: '(preview)' },
      brain: { available: false }, settings: { model: null, contextCeiling: 128000, windowRatio: .6, compactRatio: .8, keepVerbatim: 40 },
      conversation: null,
    }),
    history: async () => [],
    search: async () => [],
    getAvatar: async () => ({ uri: null, custom: false }),
    pickAvatar: async () => ({ canceled: true }),
    resetAvatar: async () => ({ uri: null, custom: false }),
    setApiKey: async () => ({ ok: true }),
    models: async () => ({ models: [], error: 'chế độ xem thử' }),
    debugLog: async () => ({ file: '(xem thử)', lines: [] }),
    debugOpen: async () => false,
    debugTranscript: async () => [],
    clearChat: async () => ({ ok: true }),
    aliceList: async () => ({ alices: [], active: null }),
    aliceCreate: async () => ({ error: 'chế độ xem thử' }),
    aliceSelect: async () => ({ ok: true }),
    aliceRemove: async () => ({ ok: true }),
    aliceSetModel: async () => ({ ok: true }),
    pickFolder: async () => ({ canceled: true }),
    testApiKey: async () => ({ error: 'chế độ xem thử' }),
    publicToggle: async () => ({ ok: true }),
    publicInfo: async () => ({
      enabled: false, mode: 'anyone', port: 8931, code: null, accounts: [],
      shareUrl: null, lanUrl: null, localUrl: '', lanUrls: [],
      tunnel: { running: false, starting: false, url: null, binary: null, error: null },
    }),
    publicSetMode: async () => ({ ok: true }),
    publicCodeRotate: async () => ({ code: '12345678' }),
    publicAccountAdd: async () => ({ ok: true }),
    publicAccountRemove: async () => ({ ok: true }),
    tunnelStatus: async () => ({ running: false, starting: false, url: null, binary: null }),
    tunnelDownload: async () => ({ error: 'chế độ xem thử' }),
    tunnelToggle: async () => ({ error: 'chế độ xem thử' }),
    onTunnelProgress: () => {},
    clipboardWrite: async () => ({ ok: true }),
    onAliceChanged: () => {},
    schedList: async () => [],
    schedAdd: async () => ({ error: 'chế độ xem thử' }),
    schedUpdate: async () => ({ error: 'chế độ xem thử' }),
    schedRemove: async () => ({ ok: true }),
    shutdown: async () => ({ ok: true }),
    updateCheck: async () => ({ checked: true, hasUpdate: false, current: '0.1.4', latest: null }),
    updateOpen: async () => ({ ok: true }),
    onUpdate: () => {},
    send: async () => ({ error: 'Đây là bản xem thử giao diện — chưa nối engine.' }),
    cancel: async () => false,
    getSettings: async () => ({}),
    setSettings: async () => ({}),
      onStream: noop, onReady: (cb) => setTimeout(cb, 0),
    onBusy: noop, onBrainError: noop, onFatal: noop,
  };
}

const $ = (id) => document.getElementById(id);
const feed = $('feed');
const input = $('input');
const sendBtn = $('send');

let busy = false;
let liveBubble = null;
let avatarUri = null;
let appName = 'Alice';
let inChat = false;

function showDashboard() {
  inChat = false;
  $('view-dashboard').hidden = false;
  $('view-chat').hidden = true;
  renderDashboard();
}

function showChat() {
  inChat = true;
  $('view-dashboard').hidden = true;
  $('view-chat').hidden = false;
}

/** Dashboard — danh sách các Alice: ảnh, tên, thư mục, tình trạng. */
async function renderDashboard() {
  const { alices, active } = await window.alice.aliceList();
  $('dash-sub').textContent = alices.length
    ? 'Chọn một Alice để trò chuyện'
    : 'Chưa có Alice nào — tạo Alice đầu tiên để bắt đầu';
  $('dash-grid').innerHTML = alices.map((a) => {
    const status = a.id === active
      ? '<span class="card-status active">đang mở</span>'
      : (a.hasKey
          ? '<span class="card-status ok">sẵn sàng</span>'
          : '<span class="card-status warn">thiếu chìa khoá</span>');
    const pub = a.public && a.public.enabled
      ? `<span class="card-status active">● máy chủ :${a.public.port}</span>`
      : '';
    return `<div class="dash-card" data-id="${escapeHtml(a.id)}">
      <div class="card-x" data-x="${escapeHtml(a.id)}" title="Xoá Alice">✕</div>
      <div class="card-ava">${a.avatarUri ? `<img src="${a.avatarUri}" alt="">` : '★'}</div>
      <div class="card-name">${escapeHtml(a.name)}</div>
      <div class="card-dir" title="${escapeHtml(a.dir)}">${escapeHtml(a.dir)}</div>
      ${status}
      ${pub}
      <div style="display:flex; gap:6px; flex-wrap:wrap; justify-content:center">
        <button class="btn ghost card-pub" data-pub="${escapeHtml(a.id)}" style="padding:6px 14px; font-size:11.5px">${a.public && a.public.enabled ? 'Quản lý máy chủ…' : 'Public Alice…'}</button>
        ${(a.id === active || (a.public && a.public.enabled))
          ? `<button class="btn ghost card-stop" data-stop="${escapeHtml(a.id)}" style="padding:6px 14px; font-size:11.5px">Tắt Alice này</button>`
          : ''}
      </div>
    </div>`;
  }).join('');
  for (const card of $('dash-grid').querySelectorAll('.dash-card')) {
    const id = card.dataset.id;
    card.onclick = async () => {
      const r = await window.alice.aliceSelect(id);
      if (r.error) { alert(r.error); return; }
      showChat();
      await refreshHeader();
      await loadHistory();
    };
  }
  for (const x of $('dash-grid').querySelectorAll('.card-x')) {
    x.onclick = async (e) => {
      e.stopPropagation();
      const alice = alices.find((a) => a.id === x.dataset.x);
      if (!confirm(`Xoá Alice ${alice ? alice.name : 'này'}? Toàn bộ trí nhớ, lịch hẹn, chìa khoá và máy chủ của Alice đó sẽ mất hẳn.`)) return;
      // Xoá phải đóng brain + chat db rồi mới xoá được thư mục — mất một lúc. Không
      // nói gì thì người dùng bấm tiếp vào card và tưởng app treo.
      const card = x.closest('.dash-card');
      card.classList.add('busy');
      card.querySelector('.card-name').textContent = 'Đang xoá…';
      const r = await window.alice.aliceRemove(x.dataset.x);
      await renderDashboard();
      await refreshHeader();
      if (r.error) { alert(r.error); return; }
      if (r.keptDir) {
        alert(`Đã gỡ Alice khỏi danh sách.\n\nThư mục của Alice do bạn tự chọn nên app KHÔNG tự xoá:\n${r.keptDir}\n\nMuốn xoá hẳn thì xoá thư mục đó bằng tay.`);
      }
    };
  }
  for (const b of $('dash-grid').querySelectorAll('.card-pub')) {
    b.onclick = async (e) => {
      e.stopPropagation();
      const alice = alices.find((a) => a.id === b.dataset.pub);
      await openPublicSheet(b.dataset.pub, alice ? alice.name : 'Alice');
    };
  }
  for (const b of $('dash-grid').querySelectorAll('.card-stop')) {
    b.onclick = async (e) => {
      e.stopPropagation();
      const alice = alices.find((a) => a.id === b.dataset.stop);
      if (!confirm(`Tắt Alice ${alice ? alice.name : 'này'}?\n\nAlice đó ngừng chạy lịch hẹn và ngừng phục vụ trang web công khai. Dữ liệu giữ nguyên — bấm vào thẻ là mở lại.`)) return;
      b.disabled = true;
      b.textContent = 'Đang tắt…';
      const r = await window.alice.aliceStop(b.dataset.stop);
      if (r.error) { alert(r.error); }
      await renderDashboard();
      await refreshHeader();
    };
  }
}

/** Nạp ảnh Alice và gắn vào mọi chỗ đang hiển thị nó. */
async function loadAvatar() {
  const a = await window.alice.getAvatar();
  avatarUri = a && a.uri ? a.uri : null;
  if (!avatarUri) return;
  for (const id of ['ava-img', 'hello-img']) {
    const el = $(id);
    if (el) el.src = avatarUri;
  }
  // Ảnh trong các bong bóng đã vẽ rồi thì cập nhật luôn, khỏi phải khởi động lại.
  for (const img of document.querySelectorAll('.row .pic img')) img.src = avatarUri;
}

// ── an toàn ────────────────────────────────────────────────────────────────

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/**
 * Markdown tối giản — Alice viết markdown, mà nhét thẳng vào `textContent` thì
 * bảng và code block thành một khối chữ không đọc được.
 *
 * Escape TRƯỚC rồi mới dựng thẻ: thứ tự ngược lại là mở cửa cho một câu trả lời
 * chứa `<img onerror=…>` chạy code trong cửa sổ app.
 */
function renderMarkdown(src) {
  const blocks = [];
  let text = escapeHtml(src);

  // code block: giữ chỗ trước để dấu * bên trong không bị hiểu là in đậm
  text = text.replace(/```([a-zA-Z0-9_-]*)\n([\s\S]*?)```/g, (_m, _lang, body) => {
    blocks.push(`<pre><code>${body.replace(/\n$/, '')}</code></pre>`);
    return ` ${blocks.length - 1} `;
  });
  text = text.replace(/`([^`\n]+)`/g, (_m, body) => {
    blocks.push(`<code>${body}</code>`);
    return ` ${blocks.length - 1} `;
  });

  text = text.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
  // link: chỉ nhận http/https. `javascript:` trong href là đường chạy code.
  text = text.replace(/\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');

  const lines = text.split('\n');
  const out = [];
  let list = null;
  for (const line of lines) {
    const bullet = line.match(/^\s*[-*]\s+(.*)$/);
    const numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (bullet || numbered) {
      const tag = bullet ? 'ul' : 'ol';
      if (list !== tag) { if (list) out.push(`</${list}>`); out.push(`<${tag}>`); list = tag; }
      out.push(`<li>${(bullet || numbered)[1]}</li>`);
      continue;
    }
    if (list) { out.push(`</${list}>`); list = null; }
    if (line.trim()) out.push(`<p>${line}</p>`);
  }
  if (list) out.push(`</${list}>`);

  return out.join('').replace(/ (\d+) /g, (_m, i) => blocks[Number(i)]);
}

// ── vẽ ─────────────────────────────────────────────────────────────────────

function clock(ts) {
  const d = new Date(ts || Date.now());
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

function addMessage(role, text, { ts = null, error = false } = {}) {
  const hello = $('hello');
  if (hello) hello.remove();

  const row = document.createElement('div');
  row.className = `row ${role === 'human' ? 'mine' : 'theirs'}`;

  if (role !== 'human') {
    const pic = document.createElement('div');
    pic.className = 'pic';
    if (avatarUri) {
      const img = document.createElement('img');
      img.src = avatarUri;
      img.alt = '';
      pic.appendChild(img);
    } else {
      pic.textContent = '★'; // chưa đọc được ảnh nào — vẫn phải có gì đó để nhìn
    }
    row.appendChild(pic);
  }

  const bubble = document.createElement('div');
  bubble.className = 'bubble' + (error ? ' err' : '');
  bubble.innerHTML = renderMarkdown(text);
  const meta = document.createElement('div');
  meta.className = 'meta';
  meta.textContent = clock(ts) + (role === 'human' ? ' ✓✓' : '');
  bubble.appendChild(meta);
  row.appendChild(bubble);

  feed.appendChild(row);
  scrollDown();
  return bubble;
}

function scrollDown() {
  feed.scrollTop = feed.scrollHeight;
}

function showTyping() {
  const row = document.createElement('div');
  row.className = 'row theirs';
  row.id = 'typing-row';
  row.innerHTML =
    `<div class="pic">${avatarUri ? `<img src="${avatarUri}" alt="">` : '★'}</div>` +
    '<div class="typing"><i></i><i></i><i></i></div>' +
    '<div class="act" id="act-line" hidden></div>';
  feed.appendChild(row);
  scrollDown();
}

/**
 * Alice đang gọi công cụ gì.
 *
 * Ba chấm nhấp nháy không phân biệt được "đang nghĩ" với "đang chạy một lệnh 40
 * giây" — và một lượt tra trí nhớ lâu nhìn y hệt một lượt treo, nên người dùng bấm
 * dừng vì tưởng hỏng.
 */
function showActivity(label) {
  const el = $('act-line');
  if (!el) return;
  el.hidden = false;
  el.textContent = label;
  scrollDown();
}

function hideTyping() {
  const row = $('typing-row');
  if (row) row.remove();
}

function setPinned(title, bodyHtml, warn = false) {
  const p = $('pinned');
  p.hidden = false;
  p.classList.toggle('warn', warn);
  $('pinned-title').textContent = title;
  $('pinned-body').innerHTML = bodyHtml;
}

// ── gửi ────────────────────────────────────────────────────────────────────

function setBusy(v) {
  busy = v;
  sendBtn.disabled = false;
  sendBtn.textContent = v ? '■' : '➤';
  sendBtn.classList.toggle('stop', v);
  sendBtn.title = v ? 'Dừng lượt này' : 'Gửi (Enter)';
}

async function send() {
  if (busy) {
    // Chỉ RA LỆNH dừng. Lượt đang chạy sẽ tự kết thúc với `canceled: true` và dọn
    // UI ở dưới — dọn ngay tại đây là hai chỗ cùng sửa một khung hình.
    sendBtn.disabled = true;
    sendBtn.title = 'Đang dừng…';
    await window.alice.cancel();
    return;
  }
  const text = input.value.trim();
  if (!text) return;

  input.value = '';
  autoGrow();
  addMessage('human', text);
  setBusy(true);
  showTyping();
  liveBubble = null;

  const res = await window.alice.send(text);

  hideTyping();
  liveBubble = null;
  setBusy(false);

  if (res && res.canceled) {
    // Đã dừng: giữ lại phần chữ đã chảy ra (nó có thật), chỉ gỡ con trỏ nhấp nháy.
    const partial = document.querySelector('.bubble.live');
    if (partial) partial.classList.remove('live');
    $('subtitle').textContent = 'Đã dừng lượt này';
    return;
  }
  if (res && res.error) {
    addMessage('alice', res.error, { error: true });
    return;
  }
  // Chữ đã chảy sẵn vào bong bóng live thì thay bằng bản cuối, không thêm bong bóng
  // thứ hai — nếu không một câu trả lời sẽ hiện hai lần.
  const existing = document.querySelector('.bubble.live');
  if (existing) {
    existing.classList.remove('live');
    existing.innerHTML = renderMarkdown(res.text || '(rỗng)');
    const meta = document.createElement('div');
    meta.className = 'meta';
    meta.textContent = clock();
    existing.appendChild(meta);
  } else {
    addMessage('alice', res.text || '(Alice không trả lời gì)');
  }

  refreshStatusLine(res);
  scrollDown();
}

function refreshStatusLine(res) {
  const bits = [];
  if (res.model) bits.push(res.model.replace(/^opencode\//, ''));
  if (res.tokens) bits.push(`${res.tokens.input.toLocaleString('vi-VN')} tok`);
  if (res.attempts && res.attempts.length) bits.push(`đã bỏ ${res.attempts.length} model lỗi`);
  if (res.seeded) bits.push('vừa nạp mồi tiếp nối');
  if (res.rotated) bits.push(`đã xoay session (${res.rotated.reason}, nén ${res.rotated.compacted} tin)`);
  if (bits.length) $('subtitle').textContent = bits.join(' · ');
}

function autoGrow() {
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 160) + 'px';
}

// ── panel ──────────────────────────────────────────────────────────────────

function openSheet(title, hint, bodyHtml, onSave = null) {
  $('sheet-title').textContent = title;
  $('sheet-hint').textContent = hint;
  $('sheet-body').innerHTML = bodyHtml;
  const saveBtn = $('sheet-save');
  saveBtn.hidden = !onSave;
  saveBtn.textContent = 'Lưu';
  saveBtn.onclick = onSave ? async () => { await onSave(); closeSheet(); } : null;
  // Màn hình chào đổi nhãn nút này thành "Để sau"; trả lại mặc định để lần mở sau
  // không thừa hưởng nhãn của màn trước.
  $('sheet-close').textContent = 'Đóng';
  $('sheet').classList.add('open');
}

function closeSheet() {
  $('sheet').classList.remove('open');
}

/**
 * Màn hình lần đầu.
 *
 * Không có key thì Alice không nói được câu nào, và lỗi đó hiện ra dưới dạng một
 * dòng đỏ khó hiểu ở giữa cuộc trò chuyện. Người ít rành kỹ thuật sẽ không tự nghĩ
 * ra là phải vào Cài đặt dán một chuỗi ký tự. Nên chặn ngay từ đầu, nói bằng tiếng
 * người, và cho đúng MỘT việc phải làm.
 */
async function openCreateAlice(required = false) {
  openSheet('Tạo Alice mới', 'Mỗi Alice là một trợ lý riêng: trí nhớ riêng, chìa khoá riêng, lịch hẹn riêng.', `
    <div class="field">
      <label for="ca-name">Tên của Alice</label>
      <input id="ca-name" type="text" placeholder="ví dụ: Alice GoDine" autofocus>
      <div class="desc">Đặt tên dễ nhận ra — mỗi Alice trong app này một tên khác nhau.</div>
    </div>
    <div class="field">
      <label for="ca-key">Chìa khoá (API key)</label>
      <div style="display:flex; gap:8px">
        <input id="ca-key" type="password" placeholder="dán chìa khoá vào đây" autocomplete="off" style="flex:1">
        <button class="btn ghost" id="ca-key-test" style="padding:8px 16px; font-size:12px; white-space:nowrap">Kiểm tra</button>
      </div>
      <div class="desc" id="ca-key-desc">Chìa khoá của riêng Alice này — không dùng chung với Alice khác. Bấm <b>Kiểm tra</b> để xem nó chạy được không và lấy danh sách model.</div>
    </div>
    <div class="field">
      <label for="ca-model">Model</label>
      <select id="ca-model" disabled><option value="">(kiểm tra chìa khoá trước đã)</option></select>
      <div class="desc" id="ca-model-desc">Chọn model cho Alice này — đổi được sau trong Cài đặt.</div>
    </div>
    <div class="field">
      <label for="ca-dir">Thư mục của Alice</label>
      <div style="display:flex; gap:8px">
        <input id="ca-dir" type="text" placeholder="(mặc định — nằm trong alice-data)" readonly style="flex:1">
        <button class="btn ghost" id="ca-dir-pick" style="padding:8px 14px; font-size:12px; white-space:nowrap">Chọn…</button>
        <button class="btn ghost" id="ca-dir-clear" style="padding:8px 12px; font-size:12px">✕</button>
      </div>
      <div class="desc">Nơi Alice này sống và làm việc — trí nhớ, chìa khoá, brain và thư mục làm việc đều nằm trong đó. Để trống thì Alice ở cạnh app.</div>
    </div>
    <div id="ca-msg" class="desc"></div>
  `, null);
  $('sheet-close').textContent = required ? 'Để sau' : 'Đóng';
  const saveBtn = $('sheet-save');
  saveBtn.hidden = false;
  saveBtn.textContent = 'Tạo';

  // KHÔNG tự đi lấy danh sách model lúc mở màn hình: chưa có chìa khoá nào thì
  // danh sách đó không nói lên được gì, và nếu engine chậm/hỏng thì ô chọn đứng
  // mãi ở "(đang tải…)" mà không ai biết vì sao. Người dùng dán key, bấm Kiểm tra.
  let keyOk = false;

  const syncCreateBtn = () => {
    saveBtn.disabled = !(keyOk && $('ca-model').value && $('ca-name').value.trim());
    saveBtn.title = saveBtn.disabled
      ? 'Nhập tên, kiểm tra chìa khoá và chọn model trước đã'
      : 'Tạo Alice';
  };
  $('ca-name').addEventListener('input', syncCreateBtn);
  $('ca-model').addEventListener('change', syncCreateBtn);
  // Sửa key thì kết quả kiểm tra cũ hết giá trị.
  $('ca-key').addEventListener('input', () => {
    keyOk = false;
    $('ca-model').disabled = true;
    $('ca-model').innerHTML = '<option value="">(kiểm tra chìa khoá trước đã)</option>';
    syncCreateBtn();
  });
  syncCreateBtn();

  $('ca-key-test').onclick = async () => {
    const btn = $('ca-key-test');
    btn.disabled = true;
    btn.textContent = 'Đang thử…';
    $('ca-key-desc').textContent = 'Đang gọi thử một lượt bằng chìa khoá này — mất vài giây.';
    try {
      const r = await window.alice.testApiKey($('ca-key').value);
      if (r.error) {
        keyOk = false;
        $('ca-key-desc').textContent = r.error;
        return;
      }
      keyOk = true;
      $('ca-key-desc').textContent = `Chìa khoá dùng được — ${r.models.length} model khả dụng (đã thử bằng ${r.tested}).`;
      const sel = $('ca-model');
      sel.disabled = false;
      sel.innerHTML = [
        '<option value="">Tự chọn (xoay vòng model free khi lỗi)</option>',
        ...r.models.map((m) => `<option value="${escapeHtml(m)}">${escapeHtml(m)}</option>`),
      ].join('');
      $('ca-model-desc').textContent = 'Chọn một model rồi bấm Tạo. Đổi được sau trong Cài đặt.';
    } catch (err) {
      // IPC reject (app chưa boot xong, main hỏng…) — phải hiện ra, không được
      // để nút kẹt ở "Đang thử…" như bản trước kẹt ở "Đang tạo…".
      $('ca-key-desc').textContent = `Không kiểm tra được: ${String(err && err.message || err)}`;
    } finally {
      btn.disabled = false;
      btn.textContent = 'Kiểm tra';
      syncCreateBtn();
    }
  };

  $('ca-dir-pick').onclick = async () => {
    const r = await window.alice.pickFolder();
    if (r.canceled || r.error) return;
    $('ca-dir').value = r.dir;
  };
  $('ca-dir-clear').onclick = () => { $('ca-dir').value = ''; };

  saveBtn.onclick = async () => {
    saveBtn.disabled = true;
    saveBtn.textContent = 'Đang tạo…';
    $('ca-msg').textContent = 'Đang dựng thư mục và trí nhớ cho Alice…';
    try {
      const r = await window.alice.aliceCreate({
        name: $('ca-name').value,
        key: $('ca-key').value,
        model: $('ca-model').value || null,
        dir: $('ca-dir').value || null,
      });
      if (r.error) { $('ca-msg').textContent = r.error; return; }
      closeSheet();
      // Tạo xong → vào chat với Alice vừa tạo.
      showChat();
      await refreshHeader();
      await loadHistory();
    } catch (err) {
      $('ca-msg').textContent = `Không tạo được: ${String(err && err.message || err)}`;
    } finally {
      saveBtn.textContent = 'Tạo';
      syncCreateBtn();
    }
  };
}

async function openSettings() {
  const [status, av] = await Promise.all([window.alice.status(), window.alice.getAvatar()]);
  status.auth = status.auth || { configured: false, providers: [] };

  // Model là của RIÊNG Alice này — không phải cài đặt chung.
  const currentModel = status.model || null;

  openSheet('Cài đặt', 'Đổi ảnh, chọn model, quản lý lịch hẹn — tất cả là của riêng Alice này.', `
    <div class="field">
      <label>Ảnh của Alice</label>
      <div style="display:flex; align-items:center; gap:14px">
        <div style="width:64px; height:64px; border-radius:18px; overflow:hidden; border:1px solid var(--border-2); flex-shrink:0; background:var(--deep)">
          ${av.uri ? `<img id="s-ava" src="${av.uri}" alt="" style="width:100%;height:100%;object-fit:cover">` : ''}
        </div>
        <div style="display:flex; gap:8px; flex-wrap:wrap">
          <button class="btn ghost" id="s-pick" style="padding:8px 16px; font-size:12.5px">Đổi ảnh…</button>
          <button class="btn ghost" id="s-reset" style="padding:8px 16px; font-size:12.5px"${av.custom ? '' : ' disabled'}>Về ảnh mặc định</button>
        </div>
      </div>
      <div class="desc" id="s-ava-msg">PNG, JPG, WEBP hoặc GIF, dưới 8 MB.</div>
    </div>
    <div class="field">
      <label for="f-model">Model của Alice này</label>
      <select id="f-model"><option value="">(đang tải danh sách model…)</option></select>
      <div class="desc" id="f-model-desc">Để trống, Alice tự chọn model tốt nhất còn dùng được.</div>
    </div>
    <div class="field">
      <label for="f-key">Chìa khoá (API key) của Alice này</label>
      <input id="f-key" type="password" placeholder="${status.auth.configured ? 'đã có key — gõ vào đây để thay' : 'dán key vào đây'}" autocomplete="off">
      <div class="desc">
        ${status.auth.configured
          ? `Đang dùng: <b>${status.auth.providers.map(escapeHtml).join(', ')}</b>.`
          : 'Chưa có chìa khoá. Alice chỉ chạy với chìa khoá dán vào ô trên.'}
      </div>
    </div>
    <div class="field">
      <label>Lịch hẹn</label>
      <button class="btn ghost" id="s-sched" style="width:100%; padding:9px 14px; font-size:12.5px">Quản lý lịch hẹn…</button>
      <div class="desc">Đặt giờ để Alice tự làm một việc mỗi ngày (kể cả khi cửa sổ đang thu nhỏ).</div>
    </div>
    <div class="field">
      <label>Cuộc trò chuyện</label>
      <button class="btn ghost" id="s-clear" style="width:100%; padding:9px 14px; font-size:12.5px">Xoá cuộc trò chuyện này…</button>
      <div class="desc">Xoá hết những gì đã nói với Alice này. Không lấy lại được.</div>
    </div>
  `, async () => {
    const key = $('f-key').value.trim();
    if (key) {
      // Provider suy từ model đang chọn (`opencode/…` → `opencode`); không có thì
      // mặc định `opencode` vì đó là Zen.
      const provider = ($('f-model').value.split('/')[0]) || 'opencode';
      await window.alice.setApiKey(provider, key);
    }
    const model = $('f-model').value || null;
    if (model !== currentModel) {
      await window.alice.aliceSetModel(status.active, model);
    }
    await refreshHeader();
  });

  loadModelsInto($('f-model'), $('f-model-desc'), currentModel);

  $('s-sched').onclick = openSchedules;

  $('s-clear').onclick = async () => {
    if (!confirm('Xoá hết cuộc trò chuyện này? Alice sẽ không còn nhớ gì đã nói, và không lấy lại được.')) return;
    await window.alice.clearChat();
    const hello = $('hello');
    feed.innerHTML = '';
    if (hello) feed.appendChild(hello);
    await refreshHeader();
  };

  $('s-pick').onclick = async () => {
    const r = await window.alice.pickAvatar();
    if (r.canceled) return;
    if (r.error) { $('s-ava-msg').textContent = r.error; return; }
    $('s-ava').src = r.uri;
    $('s-reset').disabled = false;
    $('s-ava-msg').textContent = 'Đổi rồi ạ.';
    await loadAvatar();
  };
  $('s-reset').onclick = async () => {
    const r = await window.alice.resetAvatar();
    if (r.uri) $('s-ava').src = r.uri;
    $('s-reset').disabled = true;
    $('s-ava-msg').textContent = 'Đã về ảnh mặc định.';
    await loadAvatar();
  };
}

/** Cài đặt CHUNG của app — đặt ở Dashboard, không nằm trong Settings của Alice. */
async function openAppSettings() {
  const st = await window.alice.status();
  const ver = (st.update && st.update.current) || '';
  openSheet('Cài đặt chung', 'Những thứ dùng chung cho cả app — không riêng Alice nào.', `
    <div class="field">
      <label>Cập nhật</label>
      <div class="desc" id="g-upd-desc">Phiên bản hiện tại: ${escapeHtml(ver)}</div>
      <div style="display:flex; gap:8px; margin-top:8px">
        <button class="btn ghost" id="g-upd-check" style="padding:6px 14px; font-size:12px">Kiểm tra cập nhật</button>
        <button class="btn ghost" id="g-upd-open" style="padding:6px 14px; font-size:12px" hidden>Tải bản mới</button>
      </div>
    </div>
    <div class="field">
      <label>Tắt Alice</label>
      <button class="btn ghost" id="g-shutdown" style="width:100%; padding:9px 14px; font-size:12.5px">Tắt Alice hẳn…</button>
      <div class="desc">Đóng cửa sổ chỉ ẩn Alice đi; bấm nút này mới tắt hẳn (mọi Alice ngừng chạy lịch hẹn và máy chủ).</div>
    </div>
  `, null);
  $('sheet-save').hidden = true;
  $('sheet-close').textContent = 'Đóng';

  $('g-upd-check').onclick = async () => {
    const r = await window.alice.updateCheck();
    $('g-upd-desc').textContent = r.hasUpdate
      ? `Có bản mới: ${r.latest} (bạn đang dùng ${r.current}).`
      : (r.checked ? 'Alice đang dùng bản mới nhất.' : `Chưa kiểm tra được: ${r.error || 'không có mạng?'}`);
    $('g-upd-open').hidden = !r.hasUpdate;
  };
  $('g-upd-open').onclick = async () => {
    const r = await window.alice.updateCheck();
    window.alice.updateOpen(r.url);
  };
  $('g-shutdown').onclick = async () => {
    if (!confirm('Tắt Alice hẳn? Cửa sổ sẽ đóng và mọi Alice ngừng chạy lịch hẹn và máy chủ.')) return;
    await window.alice.shutdown();
  };
}

/**
 * Public: biến Alice thành máy chủ có trang web chat.
 *
 * Hai trục tách bạch, vì trộn chúng vào nhau là chỗ người dùng hiểu sai và tự phơi
 * Alice ra Internet:
 *   - AI ĐƯỢC VÀO   — không hỏi gì / hỏi mã truy cập / hỏi tài khoản;
 *   - VÀO TỪ ĐÂU    — chỉ trong nhà (cùng wifi) hay cả Internet (cloudflared).
 * "Không hỏi gì" + Internet là tổ hợp bị chặn thẳng ở main.
 */
async function openPublicSheet(id, name) {
  const info = await window.alice.publicInfo(id);
  openSheet(`Public — ${name}`, 'Bật máy chủ rồi đưa link hoặc mã QR cho người khác — họ mở ra là chat với Alice này được luôn.', `
    <div class="field">
      <label>Máy chủ</label>
      <div style="display:flex; gap:8px; align-items:center">
        <input id="pu-port" type="number" min="1" max="65535" value="${info.port || 8931}" style="width:120px" title="Cổng (port)">
        <button class="btn" id="pu-toggle" style="padding:9px 16px; font-size:12.5px; white-space:nowrap">${info.enabled ? 'Tắt máy chủ' : 'Bật máy chủ'}</button>
      </div>
      <div class="desc" id="pu-msg"></div>
    </div>

    <div class="field">
      <label>Ai được vào chat?</label>
      <label class="pu-radio"><input type="radio" name="pu-mode" value="anyone"> <span>Ai có link hoặc mã QR đều vào được <i>— chỉ nên dùng trong nhà, cùng wifi</i></span></label>
      <label class="pu-radio"><input type="radio" name="pu-mode" value="code"> <span>Hỏi một mã truy cập dùng chung <i>— phát cho cả phòng, ai không có mã thì không vào được</i></span></label>
      <label class="pu-radio"><input type="radio" name="pu-mode" value="account"> <span>Mỗi người một tài khoản riêng <i>— username + password bạn tự tạo</i></span></label>
    </div>

    <div class="field" id="pu-code-wrap" hidden>
      <label>Mã truy cập</label>
      <div style="display:flex; gap:8px; align-items:center">
        <div id="pu-code" class="pu-code">--------</div>
        <button class="btn ghost" id="pu-code-copy" style="padding:8px 14px; font-size:12px">Copy</button>
        <button class="btn ghost" id="pu-code-new" style="padding:8px 14px; font-size:12px">Đổi mã</button>
      </div>
      <div class="desc">Đưa mã này cho ai được phép vào. Đổi mã là mọi người đang mở bị đăng xuất hết.</div>
    </div>

    <div class="field" id="pu-acc-wrap" hidden>
      <label>Tài khoản được phép vào</label>
      <div id="pu-accounts"></div>
      <div style="display:flex; gap:8px; margin-top:8px">
        <input id="pu-acc-user" type="text" placeholder="Tên đăng nhập" style="flex:1">
        <input id="pu-acc-pass" type="password" placeholder="Mật khẩu (>= 6 ký tự)" style="flex:1">
        <button class="btn ghost" id="pu-acc-add" style="padding:8px 14px; font-size:12px">Thêm</button>
      </div>
    </div>

    <div class="field" id="pu-net-wrap" hidden>
      <label>Vào được từ đâu?</label>
      <div class="desc" style="margin-top:0; margin-bottom:8px">
        Mặc định chỉ máy <b>cùng wifi</b> vào được. Bật chia sẻ Internet thì người ở
        bất kỳ đâu cũng vào được, qua đường https của Cloudflare — máy bạn không phải
        mở một cổng nào trên router.
      </div>
      <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap">
        <button class="btn ghost" id="pu-tun-toggle" style="padding:8px 16px; font-size:12.5px">Bật chia sẻ Internet</button>
        <button class="btn ghost" id="pu-tun-get" style="padding:8px 16px; font-size:12.5px" hidden>Tải cloudflared (một lần)</button>
      </div>
      <div class="desc" id="pu-tun-msg"></div>
    </div>

    <div class="field" id="pu-link-wrap" hidden>
      <label>Đường dẫn &amp; mã QR</label>
      <div style="display:flex; gap:16px; align-items:flex-start">
        <div style="flex:1; min-width:0">
          <a id="pu-link" href="#" style="font-size:13px; color:var(--primary); overflow-wrap:anywhere; display:block; margin-bottom:6px"></a>
          <div class="desc" id="pu-link-desc"></div>
          <button class="btn ghost" id="pu-copy-link" style="margin-top:6px; padding:6px 14px; font-size:12px">Copy link</button>
        </div>
        <div id="pu-qr" style="flex-shrink:0"></div>
      </div>
    </div>
  `, null);
  $('sheet-save').hidden = true;
  $('sheet-close').textContent = 'Đóng';

  let shareNow = '';

  const drawQr = (text) => {
    $('pu-qr').innerHTML = '';
    if (!text || typeof qrcode !== 'function') return;
    const qr = qrcode(0, 'M');
    qr.addData(text);
    qr.make();
    const img = document.createElement('img');
    img.src = qr.createDataURL(4, 4);
    img.width = 128;
    img.height = 128;
    img.alt = 'Mã QR để vào chat';
    img.style.borderRadius = '10px';
    img.style.border = '1px solid var(--border-2)';
    $('pu-qr').appendChild(img);
  };

  const refresh = async () => {
    const r = await window.alice.publicInfo(id);
    if (r.error) { $('pu-msg').textContent = r.error; return; }

    $('pu-toggle').textContent = r.enabled ? 'Tắt máy chủ' : 'Bật máy chủ';
    for (const radio of document.querySelectorAll('input[name="pu-mode"]')) {
      radio.checked = radio.value === r.mode;
    }
    $('pu-code-wrap').hidden = r.mode !== 'code';
    $('pu-acc-wrap').hidden = r.mode !== 'account';
    $('pu-net-wrap').hidden = !r.enabled;
    if (r.mode === 'code') $('pu-code').textContent = r.code || '--------';

    $('pu-accounts').innerHTML = (r.accounts || []).length
      ? r.accounts.map((a) => `<div class="tx-item">
          <div class="tx-who">${escapeHtml(a.username)}</div>
          <div class="tx-meta"><button class="btn ghost" data-user="${escapeHtml(a.username)}" style="padding:4px 10px; font-size:11px">Xoá</button></div>
        </div>`).join('')
      : (r.mode === 'account' ? 'Chưa có tài khoản nào — thêm ở trên.' : '');
    for (const b of $('pu-accounts').querySelectorAll('[data-user]')) {
      b.onclick = async () => {
        await window.alice.publicAccountRemove(id, b.dataset.user);
        await refresh();
      };
    }

    // Chia sẻ Internet.
    const t = r.tunnel || {};
    $('pu-tun-toggle').textContent = t.running ? 'Tắt chia sẻ Internet' : 'Bật chia sẻ Internet';
    $('pu-tun-toggle').disabled = Boolean(t.starting);
    $('pu-tun-get').hidden = Boolean(t.binary);
    if (!$('pu-tun-msg').dataset.busy) {
      $('pu-tun-msg').textContent = t.running
        ? 'Đang chia sẻ ra Internet — link bên dưới ai ở đâu cũng mở được.'
        : (t.binary
            ? 'Đang ở chế độ trong nhà: chỉ máy cùng wifi vào được.'
            : 'Cần tải cloudflared một lần (~35 MB) trước khi chia sẻ ra Internet.');
    }

    $('pu-link-wrap').hidden = !r.enabled;
    if (r.enabled) {
      shareNow = r.shareUrl || r.localUrl || '';
      const link = $('pu-link');
      link.href = shareNow;
      link.textContent = shareNow;
      link.onclick = (e) => { e.preventDefault(); window.alice.updateOpen(shareNow); };
      $('pu-link-desc').textContent = t.running
        ? 'Link Internet — gửi cho ai cũng mở được. Nhớ đưa kèm mã truy cập / tài khoản.'
        : (r.lanUrl
            ? 'Link trong nhà — chỉ máy cùng wifi mở được. Quét mã QR bên cạnh là vào thẳng.'
            : 'Máy này chưa thấy địa chỉ mạng nội bộ nào — chỉ mở được ngay trên máy chủ.');
      drawQr(shareNow);
    }
  };
  await refresh();

  $('pu-toggle').onclick = async () => {
    const infoNow = await window.alice.publicInfo(id);
    const r = await window.alice.publicToggle(id, { enabled: !infoNow.enabled, port: Number($('pu-port').value) || 8931 });
    if (r.error) { $('pu-msg').textContent = r.error; return; }
    $('pu-msg').textContent = '';
    await refresh();
    renderDashboard();
  };

  for (const radio of document.querySelectorAll('input[name="pu-mode"]')) {
    radio.onchange = async () => {
      const r = await window.alice.publicSetMode(id, radio.value);
      // Bị từ chối (ví dụ hạ xuống "ai cũng vào" khi đang mở ra Internet) thì phải
      // trả nút bấm về đúng trạng thái THẬT, không để nó đứng ở lựa chọn chưa lưu.
      if (r.error) $('pu-msg').textContent = r.error;
      else $('pu-msg').textContent = '';
      await refresh();
    };
  }

  $('pu-code-copy').onclick = async () => {
    await window.alice.clipboardWrite($('pu-code').textContent);
    $('pu-code-copy').textContent = 'Đã copy ✓';
    setTimeout(() => { $('pu-code-copy').textContent = 'Copy'; }, 1500);
  };
  $('pu-code-new').onclick = async () => {
    if (!confirm('Đổi mã truy cập? Ai đang mở chat sẽ bị đăng xuất và phải nhập mã mới.')) return;
    const r = await window.alice.publicCodeRotate(id);
    if (r.error) { $('pu-msg').textContent = r.error; return; }
    await refresh();
  };

  $('pu-tun-get').onclick = async () => {
    const msgEl = $('pu-tun-msg');
    msgEl.dataset.busy = '1';
    msgEl.textContent = 'Đang tải cloudflared… 0%';
    $('pu-tun-get').disabled = true;
    const r = await window.alice.tunnelDownload(id);
    delete msgEl.dataset.busy;
    $('pu-tun-get').disabled = false;
    if (r.error) { msgEl.textContent = r.error; return; }
    await refresh();
  };

  $('pu-tun-toggle').onclick = async () => {
    const st = await window.alice.tunnelStatus(id);
    const turningOn = !st.running;
    const msgEl = $('pu-tun-msg');
    msgEl.dataset.busy = '1';
    msgEl.textContent = turningOn ? 'Đang mở đường ra Internet…' : 'Đang đóng…';
    $('pu-tun-toggle').disabled = true;
    const r = await window.alice.tunnelToggle(id, turningOn);
    delete msgEl.dataset.busy;
    $('pu-tun-toggle').disabled = false;
    if (r.error) { msgEl.textContent = r.error; await refresh(); return; }
    await refresh();
  };

  $('pu-copy-link').onclick = async () => {
    await window.alice.clipboardWrite(shareNow);
    $('pu-copy-link').textContent = 'Đã copy ✓';
    setTimeout(() => { $('pu-copy-link').textContent = 'Copy link'; }, 1500);
  };
}

async function loadModelsInto(selectEl, descEl, selectedModel) {
  let info;
  try {
    info = await window.alice.models();
  } catch (err) {
    // IPC reject: trước đây không ai bắt, nên ô chọn đứng mãi ở "(đang tải…)" và
    // người dùng không có một chữ nào để biết chuyện gì đang xảy ra.
    descEl.textContent = `Không đọc được danh sách model: ${String(err && err.message || err)}`;
    selectEl.innerHTML = '<option value="">Tự chọn (xoay vòng model free khi lỗi)</option>';
    return;
  }
  if (!info.models.length) {
    descEl.textContent = info.error
      ? `Không đọc được danh sách model: ${escapeHtml(info.error)}`
      : 'Không có model nào khả dụng.';
    selectEl.innerHTML = '<option value="">Tự chọn (xoay vòng model free khi lỗi)</option>';
    return;
  }
  selectEl.innerHTML = [
    '<option value="">Tự chọn (xoay vòng model free khi lỗi)</option>',
    ...info.models.map((m) =>
      `<option value="${escapeHtml(m)}"${m === selectedModel ? ' selected' : ''}>${escapeHtml(m)}</option>`),
  ].join('');
  descEl.textContent = `${info.models.length} model khả dụng — duyệt từ opencode lúc mở panel.`;
}

/** Màn hình lịch hẹn — Alice tự làm một việc vào giờ đã đặt, mỗi ngày một lần.
 * Chạy được khi Alice ĐANG MỞ (kể cả khi cửa sổ thu nhỏ). */
async function openSchedules() {
  openSheet('Lịch hẹn', 'Đặt giờ, Alice tự làm việc đó mỗi ngày (khi Alice đang mở). Kết quả hiện ngay trong cuộc trò chuyện.', `
    <div class="field">
      <label>Thêm lịch mới</label>
      <div style="display:flex; gap:8px">
        <input id="sd-hour" type="number" min="0" max="23" placeholder="Giờ (0–23)" style="width:110px">
        <input id="sd-minute" type="number" min="0" max="59" placeholder="Phút (0–59)" style="width:110px">
      </div>
      <input id="sd-task" type="text" placeholder="Việc cần làm, ví dụ: Tóm tắt hôm nay rồi gợi ý việc mai" style="margin-top:8px">
      <div id="sd-msg" class="desc"></div>
      <div style="display:flex; gap:8px; margin-top:8px">
        <button class="btn" id="sd-add" style="padding:8px 16px; font-size:12.5px">Thêm</button>
      </div>
    </div>
    <div class="field">
      <label>Lịch đang có</label>
      <div id="sd-list"></div>
    </div>
  `, null);
  $('sheet-save').hidden = true;
  $('sheet-close').textContent = 'Đóng';

  const listEl = $('sd-list');
  const renderList = async () => {
    const rows = await window.alice.schedList();
    listEl.innerHTML = rows.length
      ? rows.map((s) => {
          const hh = String(s.hour).padStart(2, '0');
          const mm = String(s.minute).padStart(2, '0');
          const last = s.last_run ? ` · đã chạy ${s.last_run}` : '';
          return `<div class="tx-item">
            <div class="tx-who">${hh}:${mm} mỗi ngày${last}</div>
            <div class="tx-text">${escapeHtml(s.task)}</div>
            <div class="tx-meta">
              <label style="font-size:11px; color:var(--body-text)">
                <input type="checkbox" id="sd-on-${s.id}" data-id="${s.id}" ${s.enabled ? 'checked' : ''}> Bật
              </label>
              <button class="btn ghost" id="sd-del-${s.id}" data-id="${s.id}" style="margin-left:10px; padding:4px 10px; font-size:11px">Xoá</button>
            </div>
          </div>`;
        }).join('')
      : 'Chưa có lịch nào. Thêm một lịch ở trên.';
    for (const el of listEl.querySelectorAll('[data-id]')) {
      const id = Number(el.dataset.id);
      if (el.type === 'checkbox') {
        el.onchange = async () => { await window.alice.schedUpdate(id, { enabled: el.checked }); };
      } else {
        el.onclick = async () => {
          if (!confirm('Xoá lịch hẹn này?')) return;
          await window.alice.schedRemove(id);
          await renderList();
        };
      }
    }
  };
  await renderList();

  $('sd-add').onclick = async () => {
    const r = await window.alice.schedAdd({
      hour: $('sd-hour').value,
      minute: $('sd-minute').value,
      task: $('sd-task').value,
    });
    if (r.error) { $('sd-msg').textContent = r.error; return; }
    $('sd-hour').value = '';
    $('sd-minute').value = '';
    $('sd-task').value = '';
    $('sd-msg').textContent = 'Đã thêm.';
    await renderList();
  };
}

/** Màn hình chẩn đoán: nhật ký lỗi (file trên đĩa, hiển thị đuôi) + transcript
 * gần đây kèm meta model/lỗi. Là đường duy nhất khách phải đi qua khi "có gì đó
 * sai mà không hiểu" — thay cho việc đoán mò và gửi ảnh chụp không có log. */
async function openDebug() {
  openSheet('Chẩn đoán', 'Nhật ký lỗi và transcript gần đây. API key KHÔNG bao giờ nằm trong nhật ký.', `
    <div class="field">
      <label>Nhật ký lỗi</label>
      <div class="logbox" id="d-log">(đang đọc…)</div>
      <div class="desc" id="d-log-file"></div>
      <div style="display:flex; gap:8px; margin-top:8px">
        <button class="btn ghost" id="d-refresh" style="padding:6px 14px; font-size:12px">Làm mới</button>
        <button class="btn ghost" id="d-open" style="padding:6px 14px; font-size:12px">Mở thư mục nhật ký</button>
      </div>
    </div>
    <div class="field">
      <label>Transcript gần đây</label>
      <div id="d-tx"></div>
    </div>
  `, null);
  $('sheet-save').hidden = true;
  $('sheet-close').textContent = 'Đóng';

  const logbox = $('d-log');
  const fillLog = async () => {
    const r = await window.alice.debugLog();
    $('d-log-file').textContent = r.file || '';
    logbox.innerHTML = (r.lines || []).map((l) => {
      const esc = escapeHtml(l);
      return esc.replace(/^(\[.*?\])( ERROR| WARN| INFO)(.*)$/, (m, ts, lvl, rest) =>
        `<span class="l-${lvl.trim().toLowerCase()}">${ts}${lvl}</span>${rest}`);
    }).join('\n');
  };
  await fillLog();
  $('d-refresh').onclick = fillLog;
  $('d-open').onclick = async () => { await window.alice.debugOpen(); };

  const tx = $('d-tx');
  const rows = await window.alice.debugTranscript(20);
  tx.innerHTML = rows.length
    ? rows.map((m) => {
        const d = new Date(m.ts);
        const when = `${String(d.getDate()).padStart(2, '0')}/${String(d.getMonth() + 1).padStart(2, '0')} ${clock(m.ts)}`;
        const who = m.role === 'alice' ? 'Alice' : 'Bệ hạ';
        const metaBits = [];
        if (m.meta && m.meta.model) metaBits.push(`model=${escapeHtml(m.meta.model)}`);
        if (m.meta && m.meta.attempts && m.meta.attempts.length) {
          metaBits.push(`<span class="tx-err">thử ${escapeHtml(m.meta.attempts.map((a) => a.model).join(', '))} — hỏng</span>`);
        }
        if (m.tokensInput) metaBits.push(`${Number(m.tokensInput).toLocaleString('vi-VN')} tok`);
        return `<div class="tx-item"><div class="tx-who">${when} — ${who}</div>` +
               `<div class="tx-text">${renderMarkdown(m.text)}</div>` +
               (metaBits.length ? `<div class="tx-meta">${metaBits.join(' · ')}</div>` : '') + '</div>';
      }).join('')
    : 'Chưa có tin nào.';
}

async function openSearch() {
  openSheet('Tìm trong lịch sử', 'Tra thẳng SQLite, khớp cả khi gõ không dấu. Trả về nguyên văn — không đưa qua model tóm tắt lại, vì giá trị của nó nằm ở chỗ nó là bằng chứng.', `
    <div class="field">
      <input id="f-q" type="text" placeholder="gõ từ khoá rồi Enter…" autofocus>
    </div>
    <div id="f-hits" class="kv"></div>
  `);
  const q = $('f-q');
  q.focus();
  q.addEventListener('keydown', async (e) => {
    if (e.key !== 'Enter') return;
    const hits = await window.alice.search(q.value.trim());
    $('f-hits').innerHTML = hits.length
      ? hits.map((h) => {
          const d = new Date(h.ts);
          const when = `${String(d.getDate()).padStart(2, '0')}/${String(d.getMonth() + 1).padStart(2, '0')} ${clock(h.ts)}`;
          const who = h.role === 'alice' ? 'Alice' : 'Bệ hạ';
          const t = h.text.length > 240 ? h.text.slice(0, 240) + '…' : h.text;
          return `<b>${when} — ${who}</b><br>${escapeHtml(t)}<br><br>`;
        }).join('')
      : 'Không thấy gì ạ.';
  });
}

// ── khởi động ──────────────────────────────────────────────────────────────

async function refreshHeader() {
  const st = await window.alice.status();
  st.auth = st.auth || { configured: false, providers: [] };
  st.alices = st.alices || [];
  const activeName = st.activeName || (st.alices.length ? 'Alice' : 'Alice');
  document.title = activeName;

  // Switcher: mỗi Alice một lựa chọn, chọn là đổi luôn. Chỉ MỘT Alice thì giấu ô
  // chọn đi — nhưng khi đó phải hiện tên bằng chữ, không thì thanh tiêu đề chỉ còn
  // "★ ＋" và người dùng không biết mình đang nói chuyện với ai.
  const sw = $('alice-switch');
  const many = st.alices.length >= 2;
  sw.innerHTML = st.alices.map((a) =>
    `<option value="${escapeHtml(a.id)}"${a.id === st.active ? ' selected' : ''}>${escapeHtml(a.name)}</option>`
  ).join('');
  sw.hidden = !many;
  $('alice-name-text').hidden = many;
  $('alice-name-text').textContent = activeName;

  // Model là của RIÊNG Alice (`st.model`), không phải cài đặt chung — lấy nhầm
  // `st.settings.model` thì dòng này luôn nói "Sẵn sàng trò chuyện" dù đã chọn model.
  $('subtitle').textContent = st.model
    ? `Đang dùng ${st.model.replace(/^opencode\//, '')}`
    : 'Sẵn sàng trò chuyện';

  if (!st.alices.length) {
    setPinned('Chưa có Alice nào',
      'Bấm <b>＋</b> ở trên (hoặc nút bên dưới) để tạo Alice đầu tiên — nhập tên và chìa khoá riêng cho Alice đó.',
      false);
    return st;
  }
  if (!st.engine.available) {
    setPinned('Chưa chạy được',
      'Bản này chưa có phần chạy bên trong. Tải bản mới nhất ở trang tải rồi cài lại nhé.', true);
  } else if (!st.brain.available) {
    // Bản phát hành chính thức đã có sẵn brain — pin này chỉ xuất hiện ở bản dev
    // hoặc bản build cũ chưa đóng gói lại (feedback khách 2026-08-12: bản cho khách
    // phải có recall sẵn, không bắt họ tự đóng gói).
    setPinned('Bản này còn thiếu một phần',
      'Tải bản mới nhất ở trang tải rồi cài lại để Alice nhớ được lâu dài.', true);
  } else if (!st.auth.configured) {
    setPinned(`Thiếu chìa khoá của ${escapeHtml(activeName)}`,
      'Alice này chưa có chìa khoá nên chưa nói chuyện được. Bấm <b>⚙</b> rồi dán chìa khoá vào ô ' +
      '<b>API key OpenCode</b>.', true);
  } else {
    setPinned('Sẵn sàng',
      'Alice sẵn sàng trò chuyện. Hỏi gì cũng được — nếu không biết, Alice sẽ nói thẳng.',
      false);
  }

  // Có bản mới thì banner cập nhật đè lên mọi trạng thái — thông tin, không cảnh báo.
  if (st.update && st.update.hasUpdate) {
    setPinned(`Có phiên bản mới: ${escapeHtml(st.update.latest)}`,
      `Bạn đang dùng ${escapeHtml(st.update.current || '')}. Tải bản mới về là dữ liệu ` +
      'của bạn giữ nguyên — chỉ cần cài đè lên.' +
      '<br><button class="btn" id="upd-btn" style="margin-top:8px; padding:6px 14px; font-size:12px">Tải bản mới</button>',
      false);
    const b = $('upd-btn');
    if (b) b.onclick = () => window.alice.updateOpen(st.update.url);
  }
  return st;
}

async function loadHistory() {
  const rows = await window.alice.history(80);
  if (!rows.length) return;
  const hello = $('hello');
  if (hello) hello.remove();
  for (const r of rows) addMessage(r.role, r.text, { ts: r.ts });
}

sendBtn.addEventListener('click', send);
input.addEventListener('input', autoGrow);
input.addEventListener('keydown', (e) => {
  // Enter gửi, Shift+Enter xuống dòng — thói quen của mọi app chat.
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
});
$('btn-settings').addEventListener('click', openSettings);
$('btn-debug').addEventListener('click', openDebug);
$('btn-back').addEventListener('click', () => { showDashboard(); });
$('btn-add-alice').addEventListener('click', () => openCreateAlice(false));
$('dash-add').addEventListener('click', () => openCreateAlice(false));
$('dash-settings').addEventListener('click', openAppSettings);
$('alice-switch').addEventListener('change', async (e) => {
  const r = await window.alice.aliceSelect(e.target.value);
  if (r.error) { alert(r.error); return; }
  await refreshHeader();
  await loadHistory();
});
$('btn-search').addEventListener('click', openSearch);
$('sheet-close').addEventListener('click', closeSheet);
$('sheet').addEventListener('click', (e) => { if (e.target === $('sheet')) closeSheet(); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeSheet(); });

window.alice.onStream(({ partial, activity }) => {
  if (activity) { showActivity(activity); return; }
  if (!partial) return;
  hideTyping();
  if (!liveBubble) {
    liveBubble = addMessage('alice', partial);
    liveBubble.classList.add('live');
  } else {
    liveBubble.innerHTML = renderMarkdown(partial);
    liveBubble.classList.add('live');
  }
  scrollDown();
});

/* Chủ động hỏi khi trang nạp xong, KHÔNG chỉ ngồi chờ main đẩy `alice:ready`:
 * boot của main có thể xong trước lúc file này chạy, và khi đó tin đẩy bay mất.
 * `alice:status` bên main đã `await bootPromise` nên gọi sớm vẫn an toàn. */
let started = false;
async function start() {
  if (started) return;
  started = true;
  await loadAvatar();
  const status = await refreshHeader();
  await loadHistory();

  // Màn hình chính là Dashboard — chọn Alice rồi mới vào chat.
  showDashboard();
  if (status && (!status.alices || !status.alices.length)) {
    await openCreateAlice(true); // bắt buộc tạo Alice đầu tiên
  }
}

start();
// Ready tới sau thì chỉ làm mới header — gọi lại `start()` sẽ nạp lịch sử lần hai
// và mọi tin hiện hai lần.
window.alice.onReady(() => { refreshHeader(); });

window.alice.onAliceChanged(async (payload) => {
  if (payload && payload.id === null) {
    // Không còn Alice nào ĐANG MỞ. Hai trường hợp rất khác nhau:
    //   - danh sách rỗng  → xoá hết rồi, bắt buộc tạo Alice mới;
    //   - danh sách còn   → chỉ vừa TẮT một Alice, về dashboard là đủ. Bắt tạo mới
    //     ở đây là dí một hộp thoại vào mặt người vừa bấm "tắt".
    showDashboard();
    await refreshHeader();
    if (!payload.alices || !payload.alices.length) openCreateAlice(true);
    return;
  }
  const hello = $('hello');
  feed.innerHTML = '';
  if (hello) feed.appendChild(hello);
  await refreshHeader();
  if (inChat) {
    await loadHistory(); // đổi Alice từ switcher/Settings → nạp lịch sử mới
  } else {
    renderDashboard();   // đổi từ nơi khác khi đang ở dashboard → cập nhật card
  }
});

window.alice.onTunnelProgress(({ pct }) => {
  const el = $('pu-tun-msg');
  if (el && el.dataset.busy) el.textContent = `Đang tải cloudflared… ${pct}%`;
});

window.alice.onUpdate((status) => {
  // Main báo có bản mới sau khi check nền xong — làm mới header để banner lên.
  refreshHeader();
});

window.alice.onBusy((msg) => {
  if (msg) setPinned('Đang chuẩn bị', escapeHtml(msg));
  else refreshHeader();
});

window.alice.onBrainError((msg) => {
  setPinned('Brain lỗi', escapeHtml(msg) + ' — Alice vẫn chat được nhưng recall kém đi.', true);
});

window.alice.onFatal((msg) => {
  setPinned('Hỏng lúc khởi động', `<code>${escapeHtml(String(msg).slice(0, 400))}</code>`, true);
});
