'use strict';

const path = require('node:path');
const fs = require('node:fs');
const { app, BrowserWindow, ipcMain, shell, dialog } = require('electron');

const config = require('./config');
const log = require('./log');
const { Store } = require('./memory/store');
const { Memory } = require('./memory/memory');
const { OpencodeEngine } = require('./engine/opencode');
const { createTurnRunner } = require('./turn');
const { provisionWorkspace } = require('./alice');
const auth = require('./engine/auth');
const avatar = require('./avatar');
const { BrainSidecar } = require('./brain/sidecar');
const { Scheduler } = require('./scheduler');
const { Updater } = require('./updater');
const { PublicServer } = require('./public-server');
const registryModule = require('./registry');

let win = null;
let store = null;      // chat db của Alice ĐANG MỞ
let memory = null;
let engine = null;
let brain = null;      // brain của Alice ĐANG MỞ
let runTurn = null;
let settings = null;
let busy = false;
let scheduler = null;  // lịch hẹn của Alice ĐANG MỞ (bảng lịch nằm trong chat db)
let registry = { active: null, alices: [] };
// Alice đang được public làm máy chủ: id → PublicServer (chạy độc lập với
// Alice đang mở trên màn hình).
const publicServers = new Map();

// Cửa sổ đóng (bấm X) chỉ ẨN app — chat app phải sống tiếp để nhận lịch hẹn và
// không phải dựng lại brain mỗi lần. `isQuitting` đánh dấu lượt thoát THẬT
// (nút "Tắt Alice" trong app, hoặc quit hệ thống) để cho đóng hẳn.
let isQuitting = false;

// Kiểm tra bản mới chạy NỀN sau khi boot — lỗi mạng thì im lặng, không đứng hình.
const updater = new Updater();

/**
 * `boot()` và việc nạp trang chạy SONG SONG, nên không được giả định cái nào xong
 * trước. Bản đầu gửi `alice:ready` ngay khi boot xong; nếu boot nhanh hơn lúc trang
 * gắn listener thì tin đó bay mất và app đứng mãi ở "đang khởi động…" — đúng cái đã
 * thấy trong ảnh chụp lần đầu.
 *
 * Mọi IPC đều `await bootPromise` trước, và renderer chủ động hỏi trạng thái khi
 * nạp xong thay vì chỉ ngồi chờ được đẩy.
 */
let bootPromise = null;

function createWindow() {
  win = new BrowserWindow({
    title: config.appName(),
    width: 1180,
    height: 820,
    minWidth: 780,
    minHeight: 560,
    backgroundColor: '#080E1C', // nền DREAM, để lúc mở không loé trắng
    show: false,
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });

  win.loadFile(path.join(__dirname, '..', 'renderer', 'index.html'));
  win.once('ready-to-show', () => win.show());

  // Bấm X = ẩn cửa sổ, KHÔNG tắt app: engine và brain vẫn chạy để lịch hẹn còn
  // thực thi. Muốn tắt hẳn thì dùng nút "Tắt Alice" trong app (alice:shutdown).
  win.on('close', (e) => {
    if (!isQuitting) {
      e.preventDefault();
      win.hide();
    }
  });

  // Gõ một câu và bấm Gửi bằng chính UI, rồi chụp. Nghiệm thu đi qua ĐÚNG đường mà
  // người dùng đi (preload → IPC → engine → stream → DOM), thay vì gọi thẳng hàm
  // trong main và tin rằng phần còn lại chắc cũng chạy.
  if (process.env.ALICE_SMOKE) {
    win.webContents.once('did-finish-load', () => {
      setTimeout(() => {
        const msg = JSON.stringify(process.env.ALICE_SMOKE);
        win.webContents.executeJavaScript(
          `(() => { const i = document.getElementById('input'); i.value = ${msg};
             i.dispatchEvent(new Event('input')); document.getElementById('send').click(); })()`
        ).catch((e) => console.error('smoke hỏng:', e.message));
      }, 1500);
    });
  }

  // Chụp màn hình rồi thoát — để nghiệm thu giao diện bằng ẢNH THẬT của app, không
  // phải bằng bản xem thử trong trình duyệt. Chỉ bật khi có biến môi trường.
  if (process.env.ALICE_CAPTURE) {
    setTimeout(async () => {
      try {
        const img = await win.webContents.capturePage();
        fs.writeFileSync(process.env.ALICE_CAPTURE, img.toPNG());
      } catch (err) {
        console.error('capture hỏng:', err.message);
      }
      app.quit();
    }, Number(process.env.ALICE_CAPTURE_DELAY || 2500));
  }

  // Link ngoài mở bằng trình duyệt hệ thống, không mở trong cửa sổ app.
  win.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });
}

function currentAlice() {
  return registry.alices.find((a) => a.id === registry.active) || null;
}

/** Nén bằng chính model đang dùng — dùng chung cho mọi Alice. */
function makeSummarizer() {
  return async (messages) => {
    const transcript = messages
      .map((m) => `[${m.role === 'alice' ? 'Alice' : 'Bệ hạ'}]: ${m.text}`)
      .join('\n');
    try {
      const out = await engine.runWithFallback({
        message:
          'Tóm tắt đoạn hội thoại dưới đây thành ghi chú để CHÍNH BẠN đọc lại ở phiên sau. ' +
          'Giữ lại: quyết định đã chốt, con số, tên riêng, việc còn dở. Bỏ: lời chào, câu xã giao. ' +
          'Viết tiếng Việt, gạch đầu dòng, không mở bài.\n\n' + transcript,
        sessionId: null,
        model: settings.model || null,
        cwd: config.workDir(),
      });
      return out.text;
    } catch {
      return null; // → Memory dùng defaultSummarize
    }
  };
}

/**
 * Mở một Alice: teardown bản đang chạy (nếu có), dựng lại TOÀN BỘ tầng dữ liệu
 * của Alice đó — chat db, brain, auth, lịch hẹn. Mỗi Alice hoàn toàn độc lập.
 */
async function activateAlice(id) {
  const alice = registry.alices.find((a) => a.id === id);
  if (!alice) throw new Error('Không tìm thấy Alice.');

  // Teardown bản cũ.
  if (scheduler) scheduler.stop();
  if (brain) brain.stop();
  if (store) { store.close(); store = null; }
  scheduler = null;
  brain = null;
  memory = null;
  runTurn = null;

  registry.active = id;
  registryModule.save(registry);

  const base = config.aliceDir(id);
  engine.setBaseDir(base);
  // Model là của RIÊNG Alice (chọn lúc tạo, đổi trong Settings của nó).
  engine.settings = { ...settings, model: alice.model || null };

  store = new Store(path.join(base, 'chat.db'));
  memory = new Memory(store, settings, makeSummarizer());

  brain = new BrainSidecar(settings.brain || {}, { dataDir: path.join(base, 'brain') });
  if (brain.available) {
    try {
      // Lần đầu: dựng brain RỖNG (schema tự tạo). Alice bắt đầu không tri thức.
      brain.ensureSchema();
    } catch (err) {
      log.error(`brain.ensureSchema: ${err.message}`);
      if (win) win.webContents.send('alice:brain-error', `Không dựng được trí nhớ: ${err.message}`);
    }
  }

  const brainMcp = brain && brain.available ? brain.mcpConfig() : null;
  const workDir = provisionWorkspace(settings, { brainMcp });

  runTurn = createTurnRunner({ store, memory, engine, workDir, settings });
  scheduler = new Scheduler({ store, runTurn, log });
  scheduler.start();

  log.info(`alice active: ${alice.id} (${alice.name})`);
  if (win) {
    win.webContents.send('alice:alice-changed', {
      id: alice.id,
      name: alice.name,
      alices: registry.alices,
      active: registry.active,
      auth: auth.authStatus(base),
    });
  }
  return alice;
}

async function boot() {
  log.info(`boot: root=${config.ROOT}`);
  settings = config.loadSettings();
  fs.mkdirSync(config.DATA_DIR, { recursive: true });

  engine = new OpencodeEngine(settings);
  log.info(`boot: engine=${engine.binSource} ${engine.binPath || '(missing)'}`);

  registry = registryModule.load();
  if (!registry.alices.length) {
    // Lần đầu lên bản đa-Alice mà máy đang có dữ liệu cũ → gom thành Alice đầu tiên.
    const migrated = registryModule.migrateLegacy({ name: config.appName() });
    if (migrated) {
      registry = migrated;
      log.info(`migrated legacy data → Alice ${registry.active}`);
    }
  }
  if (registry.active) {
    await activateAlice(registry.active);
  }
  // Chưa có Alice nào → renderer mở màn hình tạo Alice đầu tiên.
}

// ── IPC ────────────────────────────────────────────────────────────────────

ipcMain.handle('alice:status', async () => {
  await bootPromise;
  const base = registry.active ? config.aliceDir(registry.active) : null;
  return {
    root: config.ROOT,
    dataDir: config.DATA_DIR,
    appName: config.appName(),
    alices: registry.alices,
    active: registry.active,
    activeName: (currentAlice() || {}).name || null,
    engine: { path: engine.binPath, source: engine.binSource, available: engine.available },
    // Chỉ tên provider — không bao giờ kèm giá trị key (D-0004).
    auth: base ? auth.authStatus(base) : { configured: false, providers: [] },
    brain: brain ? brain.status() : { enabled: false },
    settings,
    conversation: store ? store.currentConversation() : null,
    messageCount: store ? store.count() : 0,
    update: updater.status(),
    model: (currentAlice() || {}).model || null,
  };
});

// ── các Alice ──────────────────────────────────────────────────────────────

ipcMain.handle('alice:alice:list', async () => {
  await bootPromise;
  // Dashboard cần đủ: tên, đường dẫn folder, avatar, có key chưa, public chưa.
  const alices = registry.alices.map((a) => {
    const base = config.aliceDir(a.id);
    const pub = publicServers.get(a.id);
    return {
      ...a,
      dir: base,
      hasKey: auth.authStatus(base).configured,
      avatarUri: avatar.current(base),
      public: pub && pub.running ? { enabled: true, port: pub.port } : { enabled: false, port: null },
    };
  });
  return { alices, active: registry.active };
});

ipcMain.handle('alice:alice:create', async (_e, { name, key, model }) => {
  await bootPromise;
  const nameT = String(name || '').trim();
  const keyT = String(key || '').trim();
  if (!nameT) return { error: 'Nhập tên cho Alice.' };
  if (!keyT) return { error: 'Alice cần một chìa khoá riêng.' };
  const { state, alice } = registryModule.create({ name: nameT, key: keyT, model: model || null });
  registry = state;
  log.info(`alice created: ${alice.id} (${alice.name}) model=${alice.model || 'auto'}`);
  try {
    await activateAlice(alice.id);
  } catch (err) {
    return { error: String(err.message || err) };
  }
  return { alice };
});

ipcMain.handle('alice:alice:set-model', async (_e, id, model) => {
  await bootPromise;
  const { state, updated } = registryModule.update(id, { model });
  if (!updated) return { error: 'Không tìm thấy Alice.' };
  registry = state;
  if (registry.active === id) {
    engine.settings = { ...engine.settings, model: model || null };
  }
  return { ok: true };
});

ipcMain.handle('alice:alice:select', async (_e, id) => {
  await bootPromise;
  try {
    await activateAlice(id);
  } catch (err) {
    return { error: String(err.message || err) };
  }
  return { ok: true };
});

ipcMain.handle('alice:alice:remove', async (_e, id) => {
  await bootPromise;
  // Tắt máy chủ của Alice bị xoá trước khi xoá dữ liệu.
  const pub = publicServers.get(id);
  if (pub) {
    pub.stop();
    publicServers.delete(id);
  }
  const { state, removed } = registryModule.remove(id);
  registry = state;
  if (!removed) return { error: 'Không tìm thấy Alice.' };
  log.info(`alice removed: ${id}`);
  if (registry.active) {
    await activateAlice(registry.active);
  } else {
    // Xoá Alice cuối cùng → teardown hết, renderer mở màn hình tạo Alice mới.
    if (scheduler) scheduler.stop();
    if (brain) brain.stop();
    if (store) { store.close(); store = null; }
    scheduler = null;
    brain = null;
    runTurn = null;
    if (win) win.webContents.send('alice:alice-changed', { id: null, alices: [], active: null });
  }
  return { ok: true };
});

// ── public: biến Alice thành máy chủ ───────────────────────────────────────

/** Lấy PublicServer của Alice (tạo mới nếu chưa có — chưa start). */
function publicServerFor(id) {
  let pub = publicServers.get(id);
  if (!pub) {
    const alice = registry.alices.find((a) => a.id === id);
    if (!alice) throw new Error('Không tìm thấy Alice.');
    const base = config.aliceDir(id);
    const bs = new BrainSidecar(settings.brain || {}, { dataDir: path.join(base, 'brain') });
    pub = new PublicServer({
      alice,
      baseDir: base,
      settings,
      engine,
      brainMcp: bs.available ? bs.mcpConfig() : null,
      log,
    });
    publicServers.set(id, pub);
  }
  return pub;
}

ipcMain.handle('alice:public:toggle', async (_e, id, { enabled, port }) => {
  await bootPromise;
  try {
    const pub = publicServerFor(id);
    if (enabled) {
      if (pub.running) return { ok: true };
      const cfg = pub.config();
      if (cfg.mode === 'account' && !cfg.accounts.length) {
        return { error: 'Chế độ tài khoản cần ít nhất một tài khoản — thêm username + password trước.' };
      }
      const p = Number(port) || cfg.port || 8931;
      await pub.start(p);
      const newCfg = pub.config();
      pub.saveConfig({ ...newCfg, enabled: true, port: p });
    } else {
      pub.stop();
      const cfg = pub.config();
      pub.saveConfig({ ...cfg, enabled: false });
    }
    return { ok: true };
  } catch (err) {
    return { error: String(err.message || err) };
  }
});

ipcMain.handle('alice:public:set-mode', async (_e, id, mode) => {
  await bootPromise;
  try {
    const pub = publicServerFor(id);
    const cfg = pub.config();
    cfg.mode = mode === 'account' ? 'account' : 'anyone';
    pub.saveConfig(cfg);
    return { ok: true };
  } catch (err) {
    return { error: String(err.message || err) };
  }
});

ipcMain.handle('alice:public:account:add', async (_e, id, { username, password }) => {
  await bootPromise;
  try {
    const pub = publicServerFor(id);
    const cfg = pub.config();
    const name = String(username || '').trim();
    if (!name) return { error: 'Nhập tên đăng nhập.' };
    if (String(password || '').length < 6) return { error: 'Mật khẩu ít nhất 6 ký tự.' };
    if (cfg.accounts.some((a) => a.username === name)) return { error: 'Tên đăng nhập đã có.' };
    const { hashPassword } = require('./public-server');
    cfg.accounts.push({ username: name, ...hashPassword(password) });
    pub.saveConfig(cfg);
    log.info(`public account added: ${name}`);
    return { ok: true };
  } catch (err) {
    return { error: String(err.message || err) };
  }
});

ipcMain.handle('alice:public:account:remove', async (_e, id, username) => {
  await bootPromise;
  try {
    const pub = publicServerFor(id);
    const cfg = pub.config();
    cfg.accounts = cfg.accounts.filter((a) => a.username !== username);
    pub.saveConfig(cfg);
    return { ok: true };
  } catch (err) {
    return { error: String(err.message || err) };
  }
});

ipcMain.handle('alice:public:info', async (_e, id) => {
  await bootPromise;
  try {
    const pub = publicServerFor(id);
    const cfg = pub.config();
    const port = pub.port || cfg.port;
    return {
      enabled: pub.running,
      mode: cfg.mode,
      port,
      accounts: cfg.accounts.map((a) => ({ username: a.username })),
      shareUrl: shareableUrl(port),
      localUrl: `http://127.0.0.1:${port}`,
      lanUrls: shareableIps().map((ip) => `http://${ip}:${port}`),
    };
  } catch (err) {
    return { error: String(err.message || err) };
  }
});

/**
 * IP mà MÁY KHÁC cùng mạng vào được — lọc rác:
 *   - Tailscale/CGNAT 100.64/10 — chỉ máy trong mạng Tailscale vào được;
 *   - Docker bridge 172.17/16, VirtualBox host-only 192.168.56/24, link-local;
 *   - interface mang tên docker/virtualbox/vEthernet/Tailscale/WSL…
 */
function shareableIps() {
  const ips = require('node:os').networkInterfaces();
  const badName = /docker|virtualbox|vethernet|tailscale|zerotier|hamachi|default switch|wsl/i;
  const badRange = (ip) => {
    const p = ip.split('.').map(Number);
    if (p[0] === 127 || (p[0] === 169 && p[1] === 254)) return true;   // loopback, link-local
    if (p[0] === 100) return true;                                     // Tailscale/CGNAT
    if (p[0] === 172 && p[1] === 17) return true;                      // Docker bridge mặc định
    if (p[0] === 192 && p[1] === 168 && p[2] === 56) return true;       // VirtualBox host-only
    return false;
  };
  const out = [];
  for (const [name, list] of Object.entries(ips)) {
    if (badName.test(name)) continue;
    for (const a of list || []) {
      if (a.family === 'IPv4' && !a.internal && !badRange(a.address)) out.push(a.address);
    }
  }
  return out;
}

function shareableUrl(port) {
  const ips = shareableIps();
  return ips.length ? `http://${ips[0]}:${port}` : null;
}

// ── tiện ích ───────────────────────────────────────────────────────────────

ipcMain.handle('alice:clipboard:write', async (_e, text) => {
  const { clipboard } = require('electron');
  clipboard.writeText(String(text || ''));
  return { ok: true };
});

ipcMain.handle('alice:avatar:get', async () => {
  const base = registry.active ? config.aliceDir(registry.active) : null;
  return { uri: avatar.current(base), custom: avatar.isCustom(base) };
});

ipcMain.handle('alice:avatar:pick', async () => {
  const res = await dialog.showOpenDialog(win, {
    title: 'Chọn ảnh cho Alice',
    properties: ['openFile'],
    filters: [{ name: 'Ảnh', extensions: avatar.ALLOWED.map((e) => e.slice(1)) }],
  });
  if (res.canceled || !res.filePaths.length) return { canceled: true };
  const base = registry.active ? config.aliceDir(registry.active) : null;
  if (!base) return { error: 'Chưa có Alice nào.' };
  try {
    return { uri: avatar.set(res.filePaths[0], base), custom: true };
  } catch (err) {
    return { error: String(err.message || err) };
  }
});

ipcMain.handle('alice:avatar:reset', async () => {
  const base = registry.active ? config.aliceDir(registry.active) : null;
  if (!base) return { uri: avatar.current(null), custom: false };
  return { uri: avatar.reset(base), custom: false };
});

ipcMain.handle('alice:auth:set', async (_e, { provider, key }) => {
  if (!provider || !key) return { error: 'Thiếu provider hoặc key.' };
  const base = registry.active ? config.aliceDir(registry.active) : null;
  if (!base) return { error: 'Chưa có Alice nào.' };
  try {
    return auth.setApiKey(provider, key, base);
  } catch (err) {
    return { error: String(err.message || err) };
  }
});

// ── chẩn đoán ──────────────────────────────────────────────────────────────

ipcMain.handle('alice:debug:log', async () => ({
  file: log.LOG_FILE,
  lines: log.tail(400),
}));

ipcMain.handle('alice:debug:open', async () => shell.openPath(log.LOG_DIR));

ipcMain.handle('alice:debug:transcript', async (_e, limit = 30) => {
  await bootPromise;
  const conv = store ? store.currentConversation() : null;
  if (!conv) return [];
  return store.recent(conv.id, limit).map((m) => ({
    id: m.id, role: m.role, text: m.text, ts: m.ts,
    tokensInput: m.tokens_input,
    engineSession: m.engine_session,
    meta: (() => {
      try { return JSON.parse(m.meta || 'null'); } catch { return null; }
    })(),
  }));
});

ipcMain.handle('alice:history', async (_e, limit = 80) => {
  await bootPromise;
  const conv = store ? store.currentConversation() : null;
  if (!conv) return [];
  return store.recent(conv.id, limit).map((m) => ({
    id: m.id, role: m.role, text: m.text, ts: m.ts,
  }));
});

ipcMain.handle('alice:search', async (_e, query) => {
  await bootPromise;
  if (!store) return [];
  return store.search(query, 20).map((m) => ({ id: m.id, role: m.role, text: m.text, ts: m.ts }));
});

ipcMain.handle('alice:models', async () => {
  await bootPromise;
  try {
    return { models: await engine.listModels(), error: null };
  } catch (err) {
    return { models: [], error: String(err.message || err) };
  }
});

// ── cập nhật ───────────────────────────────────────────────────────────────

ipcMain.handle('alice:update:check', async () => updater.check(app.getVersion()));

ipcMain.handle('alice:update:open', async (_e, url) => {
  const target = url || updater.status().url || 'https://github.com/blueberry-sensei/alice-portable/releases/latest';
  shell.openExternal(target);
  return { ok: true };
});

// ── cuộc trò chuyện ────────────────────────────────────────────────────────

ipcMain.handle('alice:chat:clear', async () => {
  await bootPromise;
  const conv = store ? store.currentConversation() : null;
  if (conv) {
    log.info(`clear chat: conversation ${conv.id} — ${store.count(conv.id)} messages`);
    store.clearConversation(conv.id);
  }
  return { ok: true };
});

// ── lịch hẹn (của Alice đang mở) ───────────────────────────────────────────

ipcMain.handle('alice:sched:list', async () => {
  await bootPromise;
  return store ? store.listSchedules() : [];
});

ipcMain.handle('alice:sched:add', async (_e, { hour, minute, task }) => {
  await bootPromise;
  if (!store) return { error: 'Chưa có Alice nào.' };
  const h = Number(hour);
  const m = Number(minute);
  if (!Number.isInteger(h) || h < 0 || h > 23 || !Number.isInteger(m) || m < 0 || m > 59) {
    return { error: 'Giờ phải là số từ 0–23, phút từ 0–59.' };
  }
  const text = String(task || '').trim();
  if (!text) return { error: 'Chưa nhập việc cần làm.' };
  const sched = store.addSchedule({ hour: h, minute: m, task: text });
  log.info(`schedule added #${sched.id} at ${h}:${String(m).padStart(2, '0')}`);
  return { sched };
});

ipcMain.handle('alice:sched:update', async (_e, id, patch) => {
  await bootPromise;
  if (!store) return { error: 'Chưa có Alice nào.' };
  if (patch.task !== undefined) patch.task = String(patch.task || '').trim();
  if (patch.task === '') return { error: 'Việc cần làm không được để trống.' };
  const sched = store.updateSchedule(Number(id), patch);
  if (!sched) return { error: 'Không tìm thấy lịch hẹn.' };
  return { sched };
});

ipcMain.handle('alice:sched:remove', async (_e, id) => {
  await bootPromise;
  if (!store) return { error: 'Chưa có Alice nào.' };
  store.removeSchedule(Number(id));
  return { ok: true };
});

// ── tắt hẳn ────────────────────────────────────────────────────────────────

ipcMain.handle('alice:shutdown', async () => {
  log.info('shutdown requested from UI');
  isQuitting = true;
  for (const pub of publicServers.values()) pub.stop();
  publicServers.clear();
  if (scheduler) scheduler.stop();
  if (brain) brain.stop();
  if (store) {
    store.close();
    store = null; // window-all-closed sẽ chạy lại — close() lần hai ném lỗi
  }
  app.quit();
  return { ok: true };
});

ipcMain.handle('alice:settings:get', async () => settings);

ipcMain.handle('alice:settings:set', async (_e, patch) => {
  settings = config.saveSettings({ ...settings, ...patch });
  // Model là của RIÊNG Alice — settings chung không được ghi đè model đang dùng.
  const alice = currentAlice();
  engine.settings = { ...settings, model: (alice && alice.model) || null };
  if (memory) memory.settings = settings;
  provisionWorkspace(settings, { brainMcp: brain && brain.available ? brain.mcpConfig() : null });
  return settings;
});

ipcMain.handle('alice:send', async (event, text) => {
  await bootPromise;
  if (!runTurn) return { error: 'Chưa có Alice nào. Tạo Alice trước đã.' };
  if (busy) return { error: 'Đang bận một lượt khác — chờ em trả lời xong đã ạ.' };
  if (!engine.available) {
    return { error: 'Chưa có binary opencode. Đặt vào runtime/opencode/ hoặc cài opencode trên máy.' };
  }
  busy = true;
  const sender = event.sender;
  try {
    const res = await runTurn(text, (partial, ev) => {
      sender.send('alice:stream', { partial, type: ev.type });
    });
    // Lượt thành công: ghi chẩn đoán gọn — model nào chạy, thử mấy model hỏng,
    // session nào, có xoay không. Không bao giờ kèm nội dung tin hay secret.
    log.info([
      'turn ok',
      `alice=${registry.active || '-'}`,
      `model=${res.model || '-'}`,
      res.attempts && res.attempts.length ? `failed=${res.attempts.map((a) => `${a.model}(${a.error.slice(0, 120)})`).join('|')}` : null,
      `session=${res.engineSession || '-'}`,
      res.rotated ? `rotated=${res.rotated.reason}` : null,
      res.seeded ? 'seeded' : null,
    ].filter(Boolean).join(' '));
    return res;
  } catch (err) {
    log.error(`turn failed: ${err.message}`);
    return { error: String(err.message || err) };
  } finally {
    busy = false;
  }
});

ipcMain.handle('alice:cancel', async () => engine.cancel());

// ── vòng đời ───────────────────────────────────────────────────────────────

// Một máy có thể chạy nhiều app Alice ở các thư mục khác nhau — nhưng cùng một
// thư mục thì chỉ một tiến trình. Mở exe lần nữa khi app đang ẩn = hiện cửa sổ lên.
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (win) {
      if (win.isMinimized()) win.restore();
      win.show();
      win.focus();
    }
  });

  app.whenReady().then(async () => {
    createWindow();
    bootPromise = boot().catch((err) => {
      log.error(`fatal: ${err.stack || err}`);
      if (win) win.webContents.send('alice:fatal', String(err.stack || err));
      throw err;
    });
    try {
      await bootPromise;
      log.info('boot done — ready');
      if (win) win.webContents.send('alice:ready');
      // Check bản mới KHÔNG chặn boot: chạy nền, banner hiện khi nào có kết quả.
      updater.check(app.getVersion()).then((u) => {
        log.info(`update check: ${u.hasUpdate ? `new version ${u.latest}` : 'up to date'}`);
        if (win) win.webContents.send('alice:update', u);
      });
    } catch { /* đã báo ra UI ở trên rồi */ }
  });

  app.on('before-quit', () => { isQuitting = true; });

  app.on('window-all-closed', () => {
    // Cửa sổ chỉ bị ẩn chứ không đóng, nên sự kiện này chỉ tới khi thoát THẬT.
    for (const pub of publicServers.values()) pub.stop();
    publicServers.clear();
    if (scheduler) scheduler.stop();
    if (brain) brain.stop();
    if (store) store.close();
    app.quit();
  });

  app.on('activate', () => {
    // macOS: bấm icon dock khi cửa sổ đang ẩn → hiện lại.
    if (win) {
      win.show();
      win.focus();
    } else {
      createWindow();
    }
  });
}
