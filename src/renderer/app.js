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
    aliceSetProvider: async () => ({ ok: true }),
    claudeStatus: async () => ({ loggedIn: false }),
    claudeLogin: async () => ({ ok: true }),
    connectionInfo: async () => ({ provider: 'opencode', model: null, warning: null,
      opencode: { configured: false, keys: [], available: false, binary: null } }),
    removeMessage: async () => ({ ok: true }),
    brainOpen: async () => ({ error: 'chế độ xem thử' }),
    pickFolder: async () => ({ canceled: true }),
    testApiKey: async () => ({ error: 'chế độ xem thử' }),
    publicToggle: async () => ({ ok: true }),
    publicInfo: async () => ({
      enabled: false, mode: 'anyone', port: 8931, code: null, accounts: [],
      shareUrl: null, lanUrl: null, localUrl: '', lanUrls: [],
      tunnel: { running: false, starting: false, url: null, binary: null, error: null },
      online: 0, joined: 0,
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
    onBusy: noop, onBrainError: noop, onFatal: noop, onPublicMessage: noop, onPublicBusy: noop,
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
  // Hai rail là của Alice ĐANG MỞ — nạp lại mỗi lần vào màn chat, không nạp nền
  // theo chu kỳ (xem chú thích ở `alice:connection:info`).
  editingSchedId = null;
  renderRoutines();
  renderConnection();
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

/**
 * @param {number?} id  id trong kho của app. Có id thì bong bóng mọc nút ✕ xoá.
 *   Bong bóng lỗi và bong bóng đang chảy chữ không có id — chúng chưa (hoặc không)
 *   nằm trong kho, nên không có gì để xoá.
 */
function addMessage(role, text, { ts = null, error = false, id = null } = {}) {
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
  if (id != null && !error) attachDelete(bubble, row, id);
  row.appendChild(bubble);

  feed.appendChild(row);
  scrollDown();
  return bubble;
}

/** Nút ✕ trên một bong bóng — xoá tin khỏi kho của app. */
function attachDelete(bubble, row, id) {
  const x = document.createElement('button');
  x.className = 'msg-x';
  x.textContent = '✕';
  x.title = 'Xoá tin nhắn này';
  x.onclick = async (e) => {
    e.stopPropagation();
    if (!confirm(
      'Xoá tin nhắn này khỏi lịch sử?\n\n'
      + 'Nó biến mất khỏi màn hình và khỏi ô tìm kiếm, không lấy lại được.\n'
      + 'Lưu ý: Alice vẫn còn nhớ câu này trong phiên đang chạy, tới lần xoay phiên kế tiếp.'
    )) return;
    const r = await window.alice.removeMessage(id);
    if (r && r.error) { alert(r.error); return; }
    row.remove();
    // Xoá tin cuối cùng → trả lại màn hình chào, không để khung chat trống trơn.
    if (!feed.querySelector('.row')) await loadHistory();
  };
  bubble.appendChild(x);
}

function scrollDown() {
  feed.scrollTop = feed.scrollHeight;
}

function showTyping() {
  if ($('typing-row')) return; // đã hiện rồi — đừng tạo id trùng
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
  // Id chỉ biết được SAU khi main lưu xong — giữ lại bong bóng để gắn nút xoá vào.
  const mineBubble = addMessage('human', text);
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
    if (res.replyId != null) attachDelete(existing, existing.closest('.row'), res.replyId);
  } else {
    addMessage('alice', res.text || '(Alice không trả lời gì)', { id: res.replyId });
  }
  if (mineBubble && res.messageId != null) {
    attachDelete(mineBubble, mineBubble.closest('.row'), res.messageId);
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
/**
 * Chờ Bệ hạ đăng nhập xong trên trình duyệt — đăng nhập KHÔNG có sự kiện báo về
 * app, nên phải tự hỏi lại `claude auth status` theo chu kỳ tới khi thấy
 * `loggedIn`. Dừng sau `maxTries` (mặc định ~10 phút) — không chờ vô hạn nếu Bệ hạ
 * bỏ dở giữa chừng.
 */
async function pollClaudeLogin(aliceId, onLoggedIn, { intervalMs = 3000, maxTries = 200 } = {}) {
  for (let i = 0; i < maxTries; i += 1) {
    await new Promise((r) => setTimeout(r, intervalMs));
    let st;
    try { st = await window.alice.claudeStatus(aliceId); } catch { continue; }
    if (st && st.loggedIn) { onLoggedIn(st); return; }
  }
}

async function openCreateAlice(required = false) {
  openSheet('Tạo Alice mới', 'Mỗi Alice là một trợ lý riêng: trí nhớ riêng, chìa khoá riêng, lịch hẹn riêng.', `
    <div class="field">
      <label for="ca-name">Tên của Alice</label>
      <input id="ca-name" type="text" placeholder="ví dụ: Alice GoDine" autofocus>
      <div class="desc">Đặt tên dễ nhận ra — mỗi Alice trong app này một tên khác nhau.</div>
    </div>
    <div class="field">
      <label for="ca-provider">Chạy bằng</label>
      <select id="ca-provider">
        <option value="opencode">opencode (chìa khoá API riêng)</option>
        <option value="claude">Claude Code (subscription, đăng nhập bằng claude login)</option>
      </select>
      <div class="desc" id="ca-provider-desc">opencode: dán API key ở dưới. Claude: không cần key,
        chạy <code>claude login</code> trong terminal SAU KHI tạo Alice — app sẽ cho lệnh chính xác.</div>
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
    const isClaude = $('ca-provider').value === 'claude';
    const ready = isClaude
      ? Boolean($('ca-name').value.trim())
      : (keyOk && $('ca-model').value && $('ca-name').value.trim());
    saveBtn.disabled = !ready;
    saveBtn.title = ready ? 'Tạo Alice' : 'Nhập tên, kiểm tra chìa khoá và chọn model trước đã';
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
  // Claude không cần key/model lúc tạo — đăng nhập qua `claude login` sau, model
  // chọn mặc định. Ẩn hẳn hai field đó để khỏi gây hiểu nhầm là bắt buộc.
  $('ca-provider').addEventListener('change', () => {
    const isClaude = $('ca-provider').value === 'claude';
    $('ca-key').closest('.field').hidden = isClaude;
    $('ca-model').closest('.field').hidden = isClaude;
    if (isClaude) keyOk = true;
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
      $('ca-key-desc').textContent = `Chìa khoá dùng được — ${r.models.length} model khả dụng.`;
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
      const provider = $('ca-provider').value;
      const r = await window.alice.aliceCreate({
        name: $('ca-name').value,
        key: provider === 'claude' ? '' : $('ca-key').value,
        model: provider === 'claude' ? null : ($('ca-model').value || null),
        dir: $('ca-dir').value || null,
        provider,
      });
      if (r.error) { $('ca-msg').textContent = r.error; return; }
      closeSheet();
      // Tạo xong → vào chat với Alice vừa tạo.
      showChat();
      await refreshHeader();
      await loadHistory();
      if (provider === 'claude' && r.alice) {
        // Tự mở đăng nhập LUÔN — không bắt Bệ hạ tự gõ lệnh trong terminal.
        setPinned('Đang mở đăng nhập Claude…',
          'Trình duyệt sẽ tự mở để Bệ hạ đăng nhập. Đăng nhập xong quay lại app là dùng được ngay.');
        const lr = await window.alice.claudeLogin(r.alice.id);
        if (lr.error) {
          setPinned('Không mở được đăng nhập Claude', escapeHtml(lr.error)
            + ' — vào Cài đặt để thử lại.', true);
        } else if (lr.url) {
          setPinned('Đăng nhập Claude',
            `Máy không tự mở được trình duyệt — bấm link này: <a href="${escapeHtml(lr.url)}" target="_blank" rel="noreferrer">${escapeHtml(lr.url)}</a>`,
            true);
        } else {
          setPinned('Đang chờ đăng nhập Claude', 'Đăng nhập xong ở trình duyệt thì quay lại app, vào Cài đặt để kiểm tra trạng thái.');
        }
        if (!lr.error) {
          // Đăng nhập xong: gỡ pin và để panel Kết nối bên phải nói thay — chấm
          // xanh + email + gói ở đó đã đủ, một dòng pin "xong rồi, chat được luôn"
          // chỉ chiếm chỗ rồi nằm lại mãi (Bệ hạ chốt 2026-08-14).
          pollClaudeLogin(r.alice.id, () => {
            $('pinned').hidden = true;
            renderConnection();
          });
        }
      }
    } catch (err) {
      $('ca-msg').textContent = `Không tạo được: ${String(err && err.message || err)}`;
    } finally {
      saveBtn.textContent = 'Tạo';
      syncCreateBtn();
    }
  };
}

// ── Báo cáo tuần (trong Cài đặt) ───────────────────────────────────────────

const WEEKDAY_LABELS = ['CN', 'T2', 'T3', 'T4', 'T5', 'T6', 'T7'];

/** Đổ form cấu hình báo cáo vào khối `s-report` trong sheet Cài đặt. */
async function renderReportSettings(aliceId) {
  const el = $('s-report');
  if (!el || !aliceId) return;
  let cfg;
  try {
    const r = await window.alice.reportGet();
    if (r.error) { el.innerHTML = escapeHtml(r.error); return; }
    cfg = r.config || {};
  } catch (err) {
    el.innerHTML = `Không đọc được: ${escapeHtml(String(err && err.message || err))}`;
    return;
  }
  const repos = (cfg.gitRepos || []).join('\n');
  el.innerHTML = `
    <div style="display:grid; gap:8px; margin-top:4px">
      <div style="display:flex; gap:6px">
        <input id="rp-sa" type="text" placeholder="file service account, hoặc file từ scripts/chat-user-login.js" value="${escapeHtml(cfg.googleServiceAccount || '')}" style="flex:1">
        <button class="btn ghost" id="rp-sa-pick" style="padding:6px 12px; font-size:12px; white-space:nowrap">Chọn…</button>
      </div>
      <input id="rp-space" type="text" placeholder="Google Chat space id — ví dụ: spaces/AAAA… hoặc AAAA…" value="${escapeHtml(cfg.googleSpace || '')}">
      <div style="display:flex; gap:6px">
        <input id="rp-plane-url" type="text" placeholder="Plane API base (mặc định https://api.plane.so)" value="${escapeHtml(cfg.planeBaseUrl || 'https://api.plane.so')}" style="flex:1">
        <input id="rp-plane-ws" type="text" placeholder="Plane workspace slug" value="${escapeHtml(cfg.planeWorkspace || '')}" style="flex:1">
      </div>
      <input id="rp-plane-key" type="password" placeholder="${cfg.planeApiKey ? 'đã có key (••••) — gõ vào đây để thay' : 'Plane API key'}" autocomplete="off">
      <textarea id="rp-repos" rows="3" placeholder="Mỗi dòng một đường dẫn repo git, ví dụ:&#10;D:\\Work\\erp\\kos-erpnext&#10;D:\\Work\\erp\\kos-portal">${escapeHtml(repos)}</textarea>
      <div style="display:flex; gap:6px">
        <input id="rp-tpl" type="text" placeholder="template báo cáo (markdown) — để trống là Alice tự dựng khung" value="${escapeHtml(cfg.templatePath || '')}" style="flex:1">
      </div>
      <div style="display:flex; gap:6px">
        <input id="rp-outdir" type="text" placeholder="thư mục xuất PDF (trống = trong thư mục của Alice)" value="${escapeHtml(cfg.outputDir || '')}" style="flex:1">
        <button class="btn ghost" id="rp-outdir-pick" style="padding:6px 12px; font-size:12px; white-space:nowrap">Chọn…</button>
      </div>
      <input id="rp-outname" type="text" placeholder="tên file PDF (mặc định HRM_Weekly_Report)" value="${escapeHtml(cfg.outputName || 'HRM_Weekly_Report')}">
      <button class="btn ghost" id="rp-run" style="width:100%; padding:9px 14px; font-size:12.5px">Làm báo cáo tuần ngay</button>
      <div id="rp-msg" class="desc" hidden></div>
    </div>`;
  $('rp-sa-pick').onclick = async () => {
    const r = await window.alice.reportPick();
    if (r.path) $('rp-sa').value = r.path;
  };
  $('rp-outdir-pick').onclick = async () => {
    const r = await window.alice.pickFolder();
    if (r.canceled || r.error) return;
    $('rp-outdir').value = r.dir;
  };
  $('rp-run').onclick = async () => {
    const btn = $('rp-run');
    const msg = $('rp-msg');
    btn.disabled = true;
    btn.textContent = 'Alice đang làm báo cáo — có thể mất vài phút…';
    msg.hidden = true;
    try {
      const r = await window.alice.reportRun();
      if (r.error) {
        msg.hidden = false;
        msg.textContent = r.error;
      } else {
        msg.hidden = false;
        msg.textContent = `Xong: ${r.path} (${r.pages} trang)`;
      }
    } catch (err) {
      msg.hidden = false;
      msg.textContent = `Lỗi: ${String(err && err.message || err)}`;
    } finally {
      btn.disabled = false;
      btn.textContent = 'Làm báo cáo tuần ngay';
    }
  };
}

/** Thu giá trị form báo cáo và lưu (sheet Cài đặt bấm Lưu). */
async function saveReportSettings() {
  const el = $('s-report');
  if (!el || !$('rp-sa')) return;
  const repos = ($('rp-repos').value || '').split('\n').map((s) => s.trim()).filter(Boolean);
  await window.alice.reportSave({
    googleServiceAccount: $('rp-sa').value.trim(),
    googleSpace: $('rp-space').value.trim(),
    planeBaseUrl: $('rp-plane-url').value.trim() || 'https://api.plane.so',
    planeWorkspace: $('rp-plane-ws').value.trim(),
    planeApiKey: $('rp-plane-key').value.trim(),
    gitRepos: repos,
    templatePath: $('rp-tpl').value.trim(),
    outputDir: $('rp-outdir').value.trim(),
    outputName: $('rp-outname').value.trim() || 'HRM_Weekly_Report',
  });
}

async function openSettings() {
  const [status, av] = await Promise.all([window.alice.status(), window.alice.getAvatar()]);
  status.auth = status.auth || { configured: false, providers: [] };

  // Model là của RIÊNG Alice này — không phải cài đặt chung.
  const currentModel = status.model || null;
  const provider = status.provider || 'opencode';

  const authFieldHtml = provider === 'claude'
    ? `<div class="field">
        <label>Đăng nhập Claude</label>
        <div class="desc" id="s-claude-status">Đang kiểm tra…</div>
        <button class="btn ghost" id="s-claude-login" style="margin-top:8px; padding:8px 16px; font-size:12.5px">Đăng nhập / đổi tài khoản…</button>
      </div>`
    : `<div class="field">
        <label for="f-key">Chìa khoá (API key) của Alice này</label>
        <input id="f-key" type="password" placeholder="${status.auth.configured ? 'đã có key — gõ vào đây để thay' : 'dán key vào đây'}" autocomplete="off">
        <div class="desc">
          ${status.auth.configured
            ? `Đang dùng: <b>${status.auth.providers.map(escapeHtml).join(', ')}</b>.`
            : 'Chưa có chìa khoá. Alice chỉ chạy với chìa khoá dán vào ô trên.'}
        </div>
      </div>`;

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
    ${authFieldHtml}
    <div class="field">
      <label>Trí nhớ (Alice Brain)</label>
      <button class="btn ghost" id="s-brain-open" style="width:100%; padding:9px 14px; font-size:12.5px">Xem Alice Brain…</button>
      <div class="desc" id="s-brain-desc">Dashboard đầy đủ: model trích xuất tri thức, embedding, đồ thị tri thức, telemetry.</div>
    </div>
    <div class="field">
      <label>Báo cáo tuần</label>
      <div id="s-report" class="desc">Đang đọc cấu hình…</div>
      <div class="desc" style="margin-top:6px">Alice tự gom commit git + tasks Plane + tin nhắn Google Chat (chỉ đọc) từ thứ 5 tuần trước, viết báo cáo rồi in PDF.</div>
    </div>
    <div class="field">
      <label>Cuộc trò chuyện</label>
      <button class="btn ghost" id="s-clear" style="width:100%; padding:9px 14px; font-size:12.5px">Xoá cuộc trò chuyện này…</button>
      <div class="desc">Xoá hết những gì đã nói với Alice này. Không lấy lại được. Muốn xoá lẻ một tin thì rê chuột lên tin đó trong khung chat.</div>
    </div>
  `, async () => {
    if (provider === 'opencode') {
      const key = $('f-key').value.trim();
      if (key) {
        // Provider suy từ model đang chọn (`opencode/…` → `opencode`); không có thì
        // mặc định `opencode` vì đó là Zen.
        const p = ($('f-model').value.split('/')[0]) || 'opencode';
        await window.alice.setApiKey(p, key);
      }
    }
    const model = $('f-model').value || null;
    if (model !== currentModel) {
      await window.alice.aliceSetModel(status.active, model);
    }
    await saveReportSettings();
    await refreshHeader();
    renderConnection(); // model vừa đổi — panel bên phải phải nói đúng model mới
  });

  loadModelsInto($('f-model'), $('f-model-desc'), currentModel);
  if (provider === 'claude') {
    const refreshClaudeStatus = () => window.alice.claudeStatus(status.active).then((st) => {
      const el = $('s-claude-status');
      if (!el) return;
      el.innerHTML = st.loggedIn
        ? `Đã đăng nhập: <b>${escapeHtml(st.email || '')}</b> (${escapeHtml(st.subscriptionType || '')})`
        : 'Chưa đăng nhập — bấm nút bên dưới.';
    });
    refreshClaudeStatus();
    $('s-claude-login').onclick = async () => {
      const btn = $('s-claude-login');
      btn.disabled = true;
      btn.textContent = 'Đang mở trình duyệt…';
      const lr = await window.alice.claudeLogin(status.active);
      const el = $('s-claude-status');
      if (lr.error) {
        el.textContent = lr.error;
        btn.disabled = false;
        btn.textContent = 'Đăng nhập / đổi tài khoản…';
        return;
      }
      if (lr.url) {
        el.innerHTML = `Máy không tự mở được trình duyệt — bấm link này: <a href="${escapeHtml(lr.url)}" target="_blank" rel="noreferrer">${escapeHtml(lr.url)}</a>`;
      } else {
        el.textContent = 'Đã mở trình duyệt — đang chờ Bệ hạ đăng nhập xong…';
      }
      btn.textContent = 'Đang chờ đăng nhập…';
      // Đăng nhập KHÔNG có sự kiện báo về — tự hỏi lại tới khi thấy loggedIn. Sheet
      // có thể đã đóng lúc xong (Bệ hạ đăng nhập chậm) — `el`/`btn` khi đó là node
      // đã tách khỏi DOM, gán `.textContent` vào đó vô hại, chỉ đơn giản không ai
      // thấy; KHÔNG dùng lại biến `status`/`el` cũ để đọc, chỉ để ghi.
      await pollClaudeLogin(status.active, (st) => {
        el.innerHTML = `Đã đăng nhập: <b>${escapeHtml(st.email || '')}</b> (${escapeHtml(st.subscriptionType || '')})`;
      });
      btn.disabled = false;
      btn.textContent = 'Đăng nhập / đổi tài khoản…';
    };
  }

  $('s-brain-open').onclick = async () => {
    const btn = $('s-brain-open');
    const desc = $('s-brain-desc');
    btn.disabled = true;
    btn.textContent = 'Đang mở…';
    const r = await window.alice.brainOpen(status.active);
    btn.disabled = false;
    btn.textContent = 'Xem Alice Brain…';
    if (r.error) { desc.textContent = r.error; return; }
    desc.textContent = 'Đã mở trong trình duyệt. Lần đầu mở của Alice này thì tự tạo một tài khoản LOCAL trên đúng trang login.';
  };

  $('s-clear').onclick = clearChat;

  renderReportSettings(status.active);

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

    <div class="field" id="pu-stats-wrap" hidden>
      <label>Người đang chat</label>
      <div class="desc" id="pu-stats" style="margin-top:0"></div>
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
    $('pu-stats-wrap').hidden = !r.enabled;
    if (r.enabled) {
      const online = r.online || 0;
      const joined = r.joined || 0;
      $('pu-stats').textContent = joined
        ? `${joined} người đã tham gia · ${online} đang mở trang`
        : 'Chưa có ai vào — chia sẻ link hoặc mã QR bên dưới.';
    }
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

  // Số người đang chat đổi theo thời gian thực (khách vào/ra) — vòng lặp tự dừng
  // khi tấm sheet đã đóng, không cần một chỗ "onClose" chung cho mọi sheet.
  const statsTimer = setInterval(async () => {
    if (!$('sheet').classList.contains('open')) { clearInterval(statsTimer); return; }
    const r = await window.alice.publicInfo(id);
    if (!r.error && r.enabled) {
      const online = r.online || 0;
      const joined = r.joined || 0;
      $('pu-stats').textContent = joined
        ? `${joined} người đã tham gia · ${online} đang mở trang`
        : 'Chưa có ai vào — chia sẻ link hoặc mã QR bên dưới.';
    }
  }, 4000);

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

/** Xoá cả cuộc trò chuyện — gọi từ nút 🗑 trên header và từ Cài đặt. */
async function clearChat() {
  if (!confirm(
    'Xoá hết cuộc trò chuyện này?\n\n'
    + 'Toàn bộ tin nhắn biến mất khỏi lịch sử và khỏi ô tìm kiếm, không lấy lại được.\n'
    + 'Alice bắt đầu lại từ đầu ở lượt kế tiếp.'
  )) return;
  await window.alice.clearChat();
  const hello = $('hello');
  feed.innerHTML = '';
  if (hello) feed.appendChild(hello);
  await refreshHeader();
}

// ── rail trái: Routine ─────────────────────────────────────────────────────

/**
 * Lịch chạy của Alice, đặt THẲNG ngoài màn chat.
 *
 * Trước đây nó nằm sau hai lớp: Cài đặt → "Quản lý lịch hẹn…". Một thứ chạy nền
 * mỗi ngày mà phải bấm hai lần mới thấy thì không ai kiểm tra nó, và một lịch tắt
 * âm thầm hay một lịch chưa chạy lần nào sẽ không ai phát hiện. Ở đây nó luôn
 * trong tầm mắt và sửa được tại chỗ.
 */
let editingSchedId = null; // lịch đang mở ô sửa — chỉ một lúc một cái

async function renderRoutines() {
  const listEl = $('rt-list');
  if (!listEl) return;
  let rows;
  try {
    rows = await window.alice.schedList();
  } catch (err) {
    listEl.innerHTML = `<div class="rail-empty">Không đọc được lịch: ${escapeHtml(String(err && err.message || err))}</div>`;
    return;
  }
  if (!rows.length && editingSchedId !== 'new') {
    listEl.innerHTML = '<div class="rail-empty">Chưa có lịch nào.<br>Bấm <b>＋</b> ở trên để Alice tự làm một việc vào giờ cố định mỗi ngày.</div>';
    return;
  }

  const editor = (s) => {
    const hh = s ? String(s.hour).padStart(2, '0') : '';
    const mm = s ? String(s.minute).padStart(2, '0') : '';
    const wd = s && typeof s.weekday === 'number' ? s.weekday : '';
    const wdOptions = ['', '0', '1', '2', '3', '4', '5', '6']
      .map((v) => `<option value="${v}"${String(wd) === v ? ' selected' : ''}>${v === '' ? 'Mỗi ngày' : ['CN', 'T2', 'T3', 'T4', 'T5', 'T6', 'T7'][Number(v)]}</option>`)
      .join('');
    return `<div class="rt-edit">
      <div class="rt-times">
        <input class="rt-h" type="number" min="0" max="23" placeholder="giờ" value="${escapeHtml(hh)}">
        <span style="color:var(--muted)">:</span>
        <input class="rt-m" type="number" min="0" max="59" placeholder="phút" value="${escapeHtml(mm)}">
        <select class="rt-w" title="Chỉ chạy vào một ngày trong tuần">${wdOptions}</select>
      </div>
      <textarea class="rt-t" placeholder="Việc cần làm, ví dụ: Tóm tắt hôm nay rồi gợi ý việc mai">${escapeHtml(s ? s.task : '')}</textarea>
      <div class="rt-err" hidden></div>
      <div class="rail-actions">
        <button class="rt-mini primary rt-save">Lưu</button>
        <button class="rt-mini rt-cancel">Huỷ</button>
        ${s ? '<button class="rt-mini danger rt-del" style="margin-left:auto">Xoá</button>' : ''}
      </div>
    </div>`;
  };

  const parts = [];
  if (editingSchedId === 'new') {
    parts.push(`<div class="rt-item editing" data-id="new">${editor(null)}</div>`);
  }
  for (const s of rows) {
    if (editingSchedId === s.id) {
      parts.push(`<div class="rt-item editing" data-id="${s.id}">${editor(s)}</div>`);
      continue;
    }
    const hh = String(s.hour).padStart(2, '0');
    const mm = String(s.minute).padStart(2, '0');
    const wd = typeof s.weekday === 'number' ? ['CN', 'T2', 'T3', 'T4', 'T5', 'T6', 'T7'][s.weekday] : '';
    parts.push(`<div class="rt-item${s.enabled ? '' : ' off'}" data-id="${s.id}" title="Bấm để sửa">
      <div class="rt-top">
        <span class="rt-time">${hh}:${mm}${wd ? ` <span class="rt-wd">${wd}</span>` : ''}</span>
        <div class="rt-actions">
          <button class="rail-btn rt-toggle" data-on="${s.id}" title="${s.enabled ? 'Đang bật — bấm để tắt' : 'Đang tắt — bấm để bật'}">${s.enabled ? '⏸' : '▶'}</button>
        </div>
      </div>
      <div class="rt-task">${escapeHtml(s.task)}</div>
      <div class="rt-last">${s.last_run ? `chạy lần cuối ${escapeHtml(s.last_run)}` : 'chưa chạy lần nào'}</div>
    </div>`);
  }
  listEl.innerHTML = parts.join('');

  // Bật/tắt — chặn nổi bọt, không thì bấm nút này cũng mở luôn ô sửa.
  for (const b of listEl.querySelectorAll('[data-on]')) {
    b.onclick = async (e) => {
      e.stopPropagation();
      const s = rows.find((x) => String(x.id) === b.dataset.on);
      await window.alice.schedUpdate(Number(b.dataset.on), { enabled: !s.enabled });
      await renderRoutines();
    };
  }
  for (const item of listEl.querySelectorAll('.rt-item:not(.editing)')) {
    item.onclick = async () => {
      editingSchedId = Number(item.dataset.id);
      await renderRoutines();
    };
  }

  const box = listEl.querySelector('.rt-item.editing');
  if (!box) return;
  const idNow = box.dataset.id;
  const errEl = box.querySelector('.rt-err');
  box.querySelector('.rt-t').focus();

  box.querySelector('.rt-cancel').onclick = async () => {
    editingSchedId = null;
    await renderRoutines();
  };
  box.querySelector('.rt-save').onclick = async () => {
    const wv = box.querySelector('.rt-w').value;
    const patch = {
      hour: box.querySelector('.rt-h').value,
      minute: box.querySelector('.rt-m').value,
      task: box.querySelector('.rt-t').value,
      weekday: wv === '' ? '' : Number(wv),
    };
    const r = idNow === 'new'
      ? await window.alice.schedAdd(patch)
      : await window.alice.schedUpdate(Number(idNow), patch);
    if (r && r.error) {
      errEl.hidden = false;
      errEl.textContent = r.error;
      return;
    }
    editingSchedId = null;
    await renderRoutines();
  };
  const del = box.querySelector('.rt-del');
  if (del) {
    del.onclick = async () => {
      if (!confirm('Xoá lịch hẹn này?')) return;
      await window.alice.schedRemove(Number(idNow));
      editingSchedId = null;
      await renderRoutines();
    };
  }
}

// ── rail phải: Kết nối ─────────────────────────────────────────────────────

/**
 * Trạng thái kết nối của Alice đang mở.
 *
 * CỐ Ý không có "quota còn lại": dò thật 2026-08-14, cả `claude` lẫn `opencode`
 * đều không có lệnh nào trả về con số đó. Bệ hạ chốt — thấy chấm xanh là biết nối
 * được, thế là đủ; và nhờ vậy dòng "Đã đăng nhập Claude… chat được luôn" trong
 * thanh pin cũng không cần tồn tại nữa.
 */
async function renderConnection() {
  const el = $('cn-body');
  if (!el) return;
  const st = await window.alice.status();
  if (!st.active) {
    el.innerHTML = '<div class="rail-empty">Chưa mở Alice nào.</div>';
    return;
  }
  el.innerHTML = '<div class="rail-empty">Đang kiểm tra…</div>';

  let info;
  try {
    info = await window.alice.connectionInfo(st.active);
  } catch (err) {
    el.innerHTML = `<div class="cn-warn">Không kiểm tra được: ${escapeHtml(String(err && err.message || err))}</div>`;
    return;
  }
  if (info.error) {
    el.innerHTML = `<div class="cn-warn">${escapeHtml(info.error)}</div>`;
    return;
  }

  const row = (k, v, mono = false) =>
    `<div class="cn-row"><span class="cn-k">${escapeHtml(k)}</span><span class="cn-v${mono ? ' mono' : ''}">${escapeHtml(v)}</span></div>`;

  const parts = [];
  if (info.warning) parts.push(`<div class="cn-warn">${escapeHtml(info.warning)}</div>`);

  if (info.provider === 'claude') {
    const c = info.claude || {};
    parts.push(`<div class="cn-block">
      <div class="cn-head"><span class="cn-dot ${c.loggedIn ? 'ok' : 'bad'}"></span><span class="cn-name">Claude Code</span></div>
      <div class="cn-rows">
        ${c.loggedIn ? row('Tài khoản', c.email || '(không rõ)') : ''}
        ${c.loggedIn ? row('Gói', c.subscriptionType || '(không rõ)') : ''}
        ${c.loggedIn && c.orgName ? row('Tổ chức', c.orgName) : ''}
        ${c.loggedIn ? '' : row('Trạng thái', c.error || 'Chưa đăng nhập')}
      </div>
      ${c.loggedIn ? '' : '<div class="rail-actions"><button class="rt-mini primary" id="cn-login">Đăng nhập Claude…</button></div>'}
    </div>`);
  } else {
    const o = info.opencode || {};
    const keys = (o.keys || []).filter((k) => k.tail);
    parts.push(`<div class="cn-block">
      <div class="cn-head"><span class="cn-dot ${o.configured && o.available ? 'ok' : 'bad'}"></span><span class="cn-name">opencode</span></div>
      <div class="cn-rows">
        ${keys.length
          ? keys.map((k) => row(k.provider, `••••${k.tail}`, true)).join('')
          : row('Chìa khoá', 'chưa có — vào Cài đặt dán chìa khoá')}
        ${row('Phần chạy', o.available ? (o.binary || 'sẵn sàng') : 'thiếu binary opencode')}
      </div>
    </div>`);
  }

  parts.push(`<div class="cn-block">
    <div class="cn-head"><span class="cn-dot ${info.model ? 'ok' : ''}"></span><span class="cn-name">Model</span></div>
    <div class="cn-rows">
      ${row('Đang dùng', info.model || 'mặc định của engine', true)}
    </div>
  </div>`);

  el.innerHTML = parts.join('');

  const loginBtn = $('cn-login');
  if (loginBtn) {
    loginBtn.onclick = async () => {
      loginBtn.disabled = true;
      loginBtn.textContent = 'Đang mở trình duyệt…';
      const lr = await window.alice.claudeLogin(st.active);
      if (lr.error) {
        setPinned('Không mở được đăng nhập Claude', escapeHtml(lr.error), true);
      } else if (lr.url) {
        setPinned('Đăng nhập Claude',
          `Máy không tự mở được trình duyệt — bấm link này: <a href="${escapeHtml(lr.url)}" target="_blank" rel="noreferrer">${escapeHtml(lr.url)}</a>`,
          true);
      }
      if (!lr.error) {
        // Đăng nhập xong thì chấm chuyển xanh — không cần một dòng pin báo "xong rồi"
        // nữa, đó chính là dòng Bệ hạ bảo bỏ.
        await pollClaudeLogin(st.active, () => {
          $('pinned').hidden = true;
          renderConnection();
        });
      }
      loginBtn.disabled = false;
      loginBtn.textContent = 'Đăng nhập Claude…';
    };
  }
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
  // XOÁ TRƯỚC rồi mới nạp — không thì bất kỳ chỗ nào gọi lại `loadHistory()` trên
  // một feed đã có sẵn (bấm lại card Alice đang mở, đổi Alice rồi đổi lại…) là
  // NỐI THÊM một bản y hệt, tin nhắn nhân đôi (đo thật 2026-08-13: gõ 1 câu, quay
  // lại màn Dashboard rồi bấm vào card Alice đang mở lần nữa → thấy đúng câu đó
  // hiện hai lần). Giữ lại tham chiếu `hello` TRƯỚC khi xoá — `innerHTML = ''` gỡ
  // nó khỏi DOM nhưng biến JS vẫn dùng lại (append lại) được bình thường.
  const hello = $('hello');
  feed.innerHTML = '';
  const rows = await window.alice.history(80);
  if (!rows.length) {
    if (hello) feed.appendChild(hello);
    return;
  }
  for (const r of rows) addMessage(r.role, r.text, { ts: r.ts, id: r.id });
}

sendBtn.addEventListener('click', send);
input.addEventListener('input', autoGrow);
input.addEventListener('keydown', (e) => {
  // Enter gửi, Shift+Enter xuống dòng — thói quen của mọi app chat.
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
});
$('btn-settings').addEventListener('click', openSettings);
$('btn-debug').addEventListener('click', openDebug);
$('btn-clear').addEventListener('click', clearChat);
$('rt-add').addEventListener('click', async () => {
  editingSchedId = 'new';
  await renderRoutines();
});
$('cn-reload').addEventListener('click', async () => {
  const b = $('cn-reload');
  b.disabled = true;
  await renderConnection();
  b.disabled = false;
});
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
    editingSchedId = null;
    renderRoutines();    // lịch hẹn và kết nối là của RIÊNG Alice — nạp lại cả hai
    renderConnection();
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

// Khách vừa nhắn qua trang chat công khai (điện thoại quét mã) — main đã lọc
// đúng Alice đang mở mới gửi sự kiện này, nên ở đây chỉ cần đang ở màn chat.
window.alice.onPublicMessage(({ message }) => {
  if (!inChat || !message) return;
  addMessage(message.role, message.text, { ts: message.ts, id: message.id });
});

// Khách gõ @alice trên trang public — vẽ đúng ba chấm nhấp nháy trong app, y hệt
// lúc Bệ hạ tự chat. Không dùng `setBusy()`: đó là khoá nút gửi của Ô CHAT TRONG
// APP, hai lượt (app tự chat / khách qua public) chạy song song trên hai engine
// khác nhau, khoá nhầm là Bệ hạ không gõ được trong lúc khách đang chờ trả lời.
window.alice.onPublicBusy(({ busy, activity }) => {
  if (!inChat) return;
  if (busy) {
    showTyping();
    if (activity) showActivity(activity);
  } else {
    hideTyping();
  }
});

window.alice.onBrainError((msg) => {
  setPinned('Brain lỗi', escapeHtml(msg) + ' — Alice vẫn chat được nhưng recall kém đi.', true);
});

window.alice.onFatal((msg) => {
  setPinned('Hỏng lúc khởi động', `<code>${escapeHtml(String(msg).slice(0, 400))}</code>`, true);
});
