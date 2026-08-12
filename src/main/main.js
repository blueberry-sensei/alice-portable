'use strict';

const path = require('node:path');
const fs = require('node:fs');
const { app, BrowserWindow, ipcMain, shell, dialog } = require('electron');

const config = require('./config');
const { Store } = require('./memory/store');
const { Memory } = require('./memory/memory');
const { OpencodeEngine } = require('./engine/opencode');
const { createTurnRunner } = require('./turn');
const { provisionWorkspace } = require('./alice');
const auth = require('./engine/auth');
const avatar = require('./avatar');
const { BrainSidecar } = require('./brain/sidecar');

let win = null;
let store = null;
let memory = null;
let engine = null;
let brain = null;
let runTurn = null;
let settings = null;
let busy = false;

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

async function boot() {
  settings = config.loadSettings();
  fs.mkdirSync(config.DATA_DIR, { recursive: true });

  store = new Store(config.dbPath());
  engine = new OpencodeEngine(settings);

  // Nén bằng chính model đang dùng. Hỏng thì `Memory` tự rơi về nén cơ học —
  // thà tóm tắt thô còn hơn xoay session với mồi rỗng.
  memory = new Memory(store, settings, async (messages) => {
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
  });

  const brainMcp = await startBrain();
  const workDir = provisionWorkspace(settings, { brainMcp });

  runTurn = createTurnRunner({ store, memory, engine, workDir, settings });
}

async function startBrain() {
  if (!settings.brain || settings.brain.enabled === false) return null;
  brain = new BrainSidecar(settings.brain);
  if (!brain.available) return null;

  // Lần đầu chạy sau khi cài: bung tri thức từ bản seed trong bộ cài. Vài trăm MB
  // nên phải nói ra — im lặng vài chục giây ở lần mở đầu tiên thì người dùng tưởng
  // app hỏng và tắt đi giữa chừng.
  try {
    if (win) win.webContents.send('alice:busy', 'Lần đầu chạy — Alice đang dọn trí nhớ vào máy, chờ chút nhé…');
    const r = brain.seedData();
    if (win) win.webContents.send('alice:busy', null);
    if (r.seeded) console.log(`[brain] đã bung ${r.files} file tri thức`);
  } catch (err) {
    if (win) win.webContents.send('alice:busy', null);
    if (win) win.webContents.send('alice:brain-error', `Không bung được tri thức: ${err.message}`);
  }

  try {
    await brain.start();
    return brain.mcpConfig();
  } catch (err) {
    // Brain hỏng KHÔNG được làm chết app — nhưng phải nói ra, không im lặng chạy
    // tiếp với recall kém đi (D-0053 mục 2 cấm giảm năng lực recall trong im lặng).
    if (win) win.webContents.send('alice:brain-error', String(err.message || err));
    return null;
  }
}

// ── IPC ────────────────────────────────────────────────────────────────────

ipcMain.handle('alice:status', async () => {
  await bootPromise;
  return {
    root: config.ROOT,
    dataDir: config.DATA_DIR,
    engine: { path: engine.binPath, source: engine.binSource, available: engine.available },
    // Chỉ tên provider — không bao giờ kèm giá trị key (D-0004).
    auth: auth.authStatus(),
    brain: brain ? brain.status() : { enabled: false },
    settings,
    conversation: store.currentConversation(),
    messageCount: store.count(),
  };
});

ipcMain.handle('alice:avatar:get', async () => ({
  uri: avatar.current(),
  custom: avatar.isCustom(),
}));

ipcMain.handle('alice:avatar:pick', async () => {
  const res = await dialog.showOpenDialog(win, {
    title: 'Chọn ảnh cho Alice',
    properties: ['openFile'],
    filters: [{ name: 'Ảnh', extensions: avatar.ALLOWED.map((e) => e.slice(1)) }],
  });
  if (res.canceled || !res.filePaths.length) return { canceled: true };
  try {
    return { uri: avatar.set(res.filePaths[0]), custom: true };
  } catch (err) {
    return { error: String(err.message || err) };
  }
});

ipcMain.handle('alice:avatar:reset', async () => ({ uri: avatar.reset(), custom: false }));

ipcMain.handle('alice:auth:set', async (_e, { provider, key }) => {
  if (!provider || !key) return { error: 'Thiếu provider hoặc key.' };
  try {
    return auth.setApiKey(provider, key);
  } catch (err) {
    return { error: String(err.message || err) };
  }
});

ipcMain.handle('alice:auth:import', async () => {
  try {
    return auth.importFromHost();
  } catch (err) {
    return { error: String(err.message || err) };
  }
});

ipcMain.handle('alice:history', async (_e, limit = 80) => {
  await bootPromise;
  const conv = store.currentConversation();
  if (!conv) return [];
  return store.recent(conv.id, limit).map((m) => ({
    id: m.id, role: m.role, text: m.text, ts: m.ts,
  }));
});

ipcMain.handle('alice:search', async (_e, query) => {
  await bootPromise;
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

ipcMain.handle('alice:settings:get', async () => settings);

ipcMain.handle('alice:settings:set', async (_e, patch) => {
  settings = config.saveSettings({ ...settings, ...patch });
  engine.settings = settings;
  memory.settings = settings;
  provisionWorkspace(settings, { brainMcp: brain ? brain.mcpConfig() : null });
  return settings;
});

ipcMain.handle('alice:send', async (event, text) => {
  await bootPromise;
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
    return res;
  } catch (err) {
    return { error: String(err.message || err) };
  } finally {
    busy = false;
  }
});

ipcMain.handle('alice:cancel', async () => engine.cancel());

// ── vòng đời ───────────────────────────────────────────────────────────────

app.whenReady().then(async () => {
  createWindow();
  bootPromise = boot().catch((err) => {
    if (win) win.webContents.send('alice:fatal', String(err.stack || err));
    throw err;
  });
  try {
    await bootPromise;
    if (win) win.webContents.send('alice:ready');
  } catch { /* đã báo ra UI ở trên rồi */ }
});

app.on('window-all-closed', () => {
  if (brain) brain.stop();
  if (store) store.close();
  if (process.platform !== 'darwin') app.quit();
});

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});
