'use strict';

const path = require('node:path');
const fs = require('node:fs');
const { app, BrowserWindow, ipcMain, shell, dialog } = require('electron');

const config = require('./config');
const log = require('./log');
const { Store } = require('./memory/store');
const { Memory } = require('./memory/memory');
const { OpencodeEngine } = require('./engine/opencode');
const { ClaudeEngine } = require('./engine/claude');
const { modelFor } = require('./engine/model');
const { execFile } = require('node:child_process');
const { createTurnRunner } = require('./turn');
const { provisionWorkspace } = require('./alice');
const auth = require('./engine/auth');
const avatar = require('./avatar');
const { BrainSidecar } = require('./brain/sidecar');
const { NextDashboard } = require('./brain/webui');
const { Scheduler } = require('./scheduler');
const { Updater } = require('./updater');
const { PublicServer } = require('./public-server');
const { Tunnel } = require('./tunnel');
const { toolActivity } = require('./activity');
const registryModule = require('./registry');
const reportCfg = require('./report/config');
const { PdfExporter } = require('./report/pdf');

/**
 * Entry MCP "report" cho một Alice — config của riêng nó (`<alice-home>/report.json`).
 * Đường dẫn server là tuyệt đối tới mcp-server.js ngay trong main, chạy bằng chính
 * electron-as-node (D-0053 mục 3: không npx, không node trần trên PATH).
 */
function reportMcpFor(base) {
  return reportCfg.buildReportMcp({
    execPath: process.execPath,
    serverFile: path.join(__dirname, 'report', 'mcp-server.js'),
    configFile: reportCfg.fileFor(base),
  });
}

/** Cấu hình đã che bí mật để đẩy lên renderer — API key không bao giờ lộ. */
function reportConfigForUI(base) {
  const c = reportCfg.load(base);
  return {
    ...c,
    planeApiKey: c.planeApiKey ? `••••••${c.planeApiKey.slice(-4)}` : '',
  };
}

/**
 * Thư mục Chromium của app nằm CẠNH bản cài, không nằm ở %APPDATA% chung.
 *
 * Phải đặt TRƯỚC `app.whenReady()` thì Electron mới nhận.
 *
 * Hai lý do, và cái thứ hai là một bug thật:
 *   1. "Portable" nghĩa là mọi thứ đi theo thư mục — cache và cookie cũng vậy.
 *   2. `requestSingleInstanceLock()` khoá theo ĐƯỜNG DẪN userData. Để mặc định thì
 *      mọi bản cài Alice trên cùng một máy dùng chung `%APPDATA%/alice-portable`,
 *      nên chỉ MỘT bản chạy được: mở bản thứ hai là nó lặng lẽ thoát, không cửa sổ,
 *      không báo gì. Đúng ý đồ đã ghi ở cuối file ("nhiều app Alice ở các thư mục
 *      khác nhau") nhưng chưa từng được thực hiện — bản cài đang chạy chặn bản dev,
 *      và hai bản cài ở hai thư mục cũng chặn nhau.
 */
fs.mkdirSync(path.join(config.DATA_DIR, 'chromium'), { recursive: true });
app.setPath('userData', path.join(config.DATA_DIR, 'chromium'));

let win = null;
let pdfExporter = null; // PDF sidecar — dựng lười khi Alice lần đầu in báo cáo
let store = null;      // chat db của Alice ĐANG MỞ
let memory = null;
let engine = null;
let brain = null;      // brain của Alice ĐANG MỞ
let runTurn = null;
let settings = null;
let busy = false;
let scheduler = null;  // lịch hẹn của Alice ĐANG MỞ (bảng lịch nằm trong chat db)
let registry = { active: null, alices: [] };
// Model cấu hình không hợp với provider của Alice đang mở (xem `engine/model.js`).
// Giữ lại để panel Kết nối nói ra chỗ sai, thay vì để nó vỡ giữa một lượt chat.
let modelWarning = null;
// Alice đang được public làm máy chủ: id → PublicServer (chạy độc lập với
// Alice đang mở trên màn hình).
const publicServers = new Map();
// Alice đang được chia sẻ ra INTERNET: id → Tunnel (cloudflared). Tách khỏi
// publicServers vì máy chủ LAN sống được mà không cần tunnel.
const tunnels = new Map();

// Dashboard Alice Brain (Next.js) — MỘT tiến trình web cho cả app, vì
// `NEXT_PUBLIC_API_BASE` đóng cứng lúc build (xem `docs/superpowers/specs/
// 2026-08-13-brain-dashboard-design.md`). `dashboardSidecar` là `sag_api.desktop`
// HTTP RIÊNG của Alice đang được xem — khác `brain` (biến toàn cục ở trên, chỉ lo
// MCP stdio cho Alice đang mở trong app) để bấm "Xem Brain" của Alice B trong lúc
// đang chat với Alice A không đụng brain của A.
const dashboard = new NextDashboard();
let dashboardSidecar = null;
let dashboardAliceId = null;

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
// Boot hỏng thì GIỮ LẠI lý do, không ném tiếp.
//
// Bản trước `bootPromise` re-throw, mà mọi handler IPC đều mở đầu bằng
// `await bootPromise` — nên một lần boot hỏng là đầu độc VĨNH VIỄN mọi lệnh sau đó:
// `ipcRenderer.invoke` reject, renderer không bắt, nút bấm kẹt ở "Đang tạo…" và ô
// chọn model đứng mãi ở "(đang tải…)". App còn vẽ được nhưng không làm được gì.
let bootError = null;

/** Mọi handler gọi cái này thay cho `await bootPromise`. */
async function ready() {
  try {
    await bootPromise;
  } catch { /* lý do đã nằm trong bootError */ }
  return bootError;
}

function createWindow() {
  win = new BrowserWindow({
    title: config.appName(),
    // Màn chat là BA cột (Routine · trò chuyện · Kết nối). `minWidth` phải nằm trên
    // ngưỡng 1180px mà CSS dùng để bỏ hai rail đi, nếu không cửa sổ co xuống mức
    // tối thiểu là hai rail biến mất mà người dùng không hiểu vì sao.
    width: 1440,
    height: 860,
    minWidth: 1200,
    minHeight: 600,
    backgroundColor: '#080E1C', // nền DREAM, để lúc mở không loé trắng
    show: false,
    autoHideMenuBar: true,
    // Bản đóng gói lấy icon từ file exe; lúc `npm start` thì không, nên trỏ tay vào
    // đúng ảnh để cửa sổ dev cũng mang mặt Alice chứ không phải nguyên tử Electron.
    icon: path.join(__dirname, '..', 'renderer', 'assets', 'img', 'alice-default.png'),
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

/**
 * Thư mục của một Alice — KHÔNG suy thẳng từ id nữa.
 *
 * Alice tạo với thư mục do người dùng chọn sống ở đường dẫn riêng (`alice.dir`);
 * `config.aliceDir(id)` chỉ đúng cho Alice mặc định. Dùng nhầm hàm cũ là mở nhầm
 * chat.db — Alice mất trí nhớ mà nhìn ngoài vẫn trả lời trơn tru.
 */
function aliceDirFor(id) {
  const a = registry.alices.find((x) => x.id === id);
  return a ? registryModule.dirOf(a) : config.aliceDir(id);
}

/** Engine ĐÚNG cho một Alice, theo `provider` của nó — mỗi Alice một instance
 * riêng, không dùng chung: đổi provider giữa các Alice không lẫn state
 * (`_cancelled`/`_child`) của nhau. */
function engineFor(alice, forSettings) {
  return alice.provider === 'claude' ? new ClaudeEngine(forSettings) : new OpencodeEngine(forSettings);
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
        model: engine.settings?.model || null,
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

  // Có lượt đang chạy dở (Alice đang trả lời) thì KHÔNG được đóng store — lượt đó
  // vẫn đang cầm `store`/`runTurn` cũ qua closure, đóng giữa chừng là đúng lỗi
  // "database is not open" (đo thật 2026-08-13: gõ một câu, trong lúc đang chờ
  // trả lời thì bấm quay lại Dashboard rồi chọn lại đúng Alice đó/Alice khác —
  // `activateAlice` chạy lại, đóng store cũ, lượt cũ ghi câu trả lời vào store đã
  // đóng thì vỡ). Bắt lỗi rõ ràng ở đây thay vì để nó vỡ âm thầm giữa chừng.
  if (busy) throw new Error('Alice đang bận trả lời một lượt khác — đợi xong rồi đổi Alice nhé.');

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

  const base = aliceDirFor(id);
  // Engine RIÊNG cho Alice này, đúng provider — không tái dùng instance của Alice
  // trước đó, vì đổi Alice có thể đồng thời đổi cả CLASS engine (opencode ↔ claude).
  engine = engineFor(alice, settings);
  engine.setBaseDir(base);
  // Model là của RIÊNG Alice (chọn lúc tạo, đổi trong Settings của nó) — và phải đi
  // qua `modelFor`, chốt chặn không cho model của họ này rơi xuống engine của họ kia.
  const picked = modelFor(alice);
  modelWarning = picked.warning;
  if (picked.warning) log.warn(`model không hợp provider: alice=${alice.id} ${picked.warning}`);
  engine.settings = { ...settings, model: picked.model };

  store = new Store(path.join(base, 'chat.db'));
  memory = new Memory(store, settings, makeSummarizer());

  brain = new BrainSidecar(settings.brain || {}, { dataDir: path.join(base, 'brain') });
  if (brain.available) {
    try {
      // Lần đầu: dựng brain RỖNG (schema tự tạo). Alice bắt đầu không tri thức.
      // BẢN ASYNC — bản `spawnSync` đóng băng cả app trong lúc python nạp thư viện.
      await brain.ensureSchemaAsync();
    } catch (err) {
      log.error(`brain.ensureSchema: ${err.message}`);
      if (win) win.webContents.send('alice:brain-error', `Không dựng được trí nhớ: ${err.message}`);
    }
  }

  const brainMcp = brain && brain.available ? brain.mcpConfig() : null;
  // Chỗ làm việc RIÊNG của Alice này, nằm trong thư mục của chính nó. Trước đây
  // mọi Alice dùng chung `alice-data/workspace`, nên file Alice này tạo ra lại nằm
  // trong tầm với của Alice khác — và người dùng chọn thư mục riêng cho Alice thì
  // nó vẫn đi làm việc ở một nơi hoàn toàn khác.
  // Model chỉ có nghĩa với opencode — `opencode.json` là file cấu hình của nó.
  // Alice chạy Claude Code thì để trống, không nhét tên model Claude vào đó.
  // `reportMcp` đi kèm cho MỌI engine: opencode đọc trong opencode.json, Claude Code
  // đọc trong .mcp.json (xem buildClaudeMcpJson).
  const workDir = provisionWorkspace(settings, {
    brainMcp,
    reportMcp: reportMcpFor(base),
    dir: path.join(base, 'workspace'),
    model: alice.provider === 'claude' ? null : picked.model,
  });

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
    const migrated = await registryModule.migrateLegacy({ name: config.appName() });
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
  const boom = await ready();
  if (boom) {
    // Trả một status TỐI THIỂU nhưng ĐÚNG HÌNH DẠNG: renderer đọc thẳng
    // `st.alices.length`, `st.engine.available`… — thiếu trường là nó ném ngay ở
    // dòng đầu và người dùng nhận màn hình trắng thay vì lý do thật.
    return {
      root: config.ROOT, dataDir: config.DATA_DIR, appName: config.appName(),
      alices: [], active: null, activeName: null,
      engine: { path: null, source: 'missing', available: false },
      auth: { configured: false, providers: [] },
      brain: { enabled: false }, settings: settings || {},
      conversation: null, messageCount: 0, update: updater.status(), model: null,
      bootError: boom,
    };
  }
  const base = registry.active ? aliceDirFor(registry.active) : null;
  const activeProvider = (currentAlice() || {}).provider || 'opencode';
  return {
    root: config.ROOT,
    dataDir: config.DATA_DIR,
    appName: config.appName(),
    alices: registry.alices,
    active: registry.active,
    activeName: (currentAlice() || {}).name || null,
    provider: activeProvider,
    engine: { path: engine.binPath, source: engine.binSource, available: engine.available },
    // Chỉ tên provider — không bao giờ kèm giá trị key (D-0004).
    // Claude không có "chìa khoá" kiểu opencode — trạng thái đăng nhập thật nằm ở
    // `alice:claude:status`, riêng. Ở đây báo `configured:true` để không hiện nhầm
    // banner "thiếu chìa khoá" của luồng opencode.
    auth: activeProvider === 'claude'
      ? { configured: true, providers: ['claude'] }
      : (base ? auth.authStatus(base) : { configured: false, providers: [] }),
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
  const boom = await ready();
  if (boom) return { error: `App chưa khởi động được: ${boom}` };
  // Dashboard cần đủ: tên, đường dẫn folder, avatar, có key chưa, public chưa.
  const alices = registry.alices.map((a) => {
    const base = aliceDirFor(a.id);
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

/**
 * Kiểm tra một chìa khoá NGƯỜI DÙNG VỪA DÁN, trước khi tạo Alice nào.
 *
 * Vì sao không chỉ gọi `opencode models`: lệnh đó chạy được cả khi KHÔNG có key —
 * đã đo, nó trả về đủ 7 model với thư mục auth trống. Nên nó chứng minh được "máy
 * chủ Zen còn sống", chứ không chứng minh được "chìa khoá này dùng được". Muốn biết
 * chắc thì phải GỌI THẬT một lượt: một chữ, model free, timeout ngắn.
 *
 * Chạy trong thư mục tạm, xoá ngay sau khi xong — không đụng vào Alice nào.
 */
ipcMain.handle('alice:auth:test', async (_e, key) => {
  const boom = await ready();
  if (boom) return { error: `App chưa khởi động được: ${boom}` };
  const k = String(key || '').trim();
  if (!k) return { error: 'Dán chìa khoá vào ô trên đã.' };
  if (!engine.available) return { error: 'Bản cài này thiếu phần chạy bên trong (opencode).' };

  const probe = path.join(config.DATA_DIR, '.keytest');
  try {
    fs.rmSync(probe, { recursive: true, force: true });
    auth.setApiKey('opencode', k, probe);

    // CHỈ hỏi danh sách model — không chạy thử một lượt chat thật (Bệ hạ chốt
    // 2026-08-13: "kiểm tra" không được gọi model nào, tốn tiền/quota vô ích).
    // Đọc được danh sách nghĩa là chìa khoá dùng được — cần đúng quyền mới truy
    // vấn được danh sách của tài khoản, sai chìa khoá thì lệnh này tự hỏng.
    const models = await engine.listModels({ baseDir: probe, timeout: 45000 });
    if (!models.length) {
      return { error: 'Không đọc được danh sách model — kiểm tra kết nối mạng hoặc chìa khoá rồi thử lại.' };
    }
    log.info(`auth test ok: ${models.length} model`);
    return { ok: true, models };
  } catch (err) {
    const why = String(err.message || err);
    // Lỗi xác thực nói rõ là chìa khoá sai; lỗi khác thì nói nguyên văn, đừng đổ
    // oan cho cái key khi thật ra là mạng hỏng.
    if (/401|403|unauthor|invalid.*key|api key|forbidden/i.test(why)) {
      return { error: 'Chìa khoá không dùng được — kiểm tra lại rồi dán lần nữa.' };
    }
    return { error: why };
  } finally {
    fs.rmSync(probe, { recursive: true, force: true });
  }
});

ipcMain.handle('alice:folder:pick', async () => {
  const res = await dialog.showOpenDialog(win, {
    title: 'Chọn thư mục cho Alice',
    properties: ['openDirectory', 'createDirectory'],
  });
  if (res.canceled || !res.filePaths.length) return { canceled: true };
  return { dir: res.filePaths[0] };
});

ipcMain.handle('alice:alice:create', async (_e, { name, key, model, dir, provider }) => {
  const boom = await ready();
  if (boom) return { error: `App chưa khởi động được: ${boom}` };
  const nameT = String(name || '').trim();
  const providerT = provider === 'claude' ? 'claude' : 'opencode';
  const keyT = String(key || '').trim();
  if (!nameT) return { error: 'Nhập tên cho Alice.' };
  // Claude dùng subscription (đăng nhập bằng `claude login`, cô lập theo Alice) —
  // không có API key để bắt buộc như opencode.
  if (providerT === 'opencode' && !keyT) return { error: 'Alice cần một chìa khoá riêng.' };
  const dirT = String(dir || '').trim();
  if (dirT && !fs.existsSync(dirT)) return { error: `Không thấy thư mục: ${dirT}` };
  let state;
  let alice;
  try {
    ({ state, alice } = registryModule.create({
      name: nameT, key: keyT, model: model || null, dir: dirT || null, provider: providerT,
    }));
  } catch (err) {
    // Trùng thư mục với Alice khác (cùng danh sách, hoặc dấu vết của bản cài
    // khác) — registry.create() từ chối thẳng, không âm thầm tạo đè lên.
    return { error: String(err.message || err) };
  }
  registry = state;
  log.info(`alice created: ${alice.id} (${alice.name}) provider=${alice.provider} model=${alice.model || 'auto'} dir=${alice.dir || 'mặc định'}`);
  try {
    await activateAlice(alice.id);
  } catch (err) {
    return { error: String(err.message || err) };
  }
  return { alice };
});

ipcMain.handle('alice:alice:set-model', async (_e, id, model) => {
  const boom = await ready();
  if (boom) return { error: `App chưa khởi động được: ${boom}` };
  const { state, updated } = registryModule.update(id, { model });
  if (!updated) return { error: 'Không tìm thấy Alice.' };
  registry = state;
  if (registry.active === id) {
    const alice = currentAlice();
    const picked = modelFor(alice);
    modelWarning = picked.warning;
    engine.settings = { ...engine.settings, model: picked.model };
    // `opencode.json` trong workspace cũng mang tên model — không ghi lại thì lượt
    // sau vẫn chạy model cũ dù ô chọn đã đổi.
    provisionWorkspace(settings, {
      brainMcp: brain && brain.available ? brain.mcpConfig() : null,
      reportMcp: reportMcpFor(aliceDirFor(id)),
      dir: path.join(aliceDirFor(id), 'workspace'),
      model: alice.provider === 'claude' ? null : picked.model,
    });
  }
  return { ok: true, warning: modelFor(registry.alices.find((a) => a.id === id)).warning };
});

ipcMain.handle('alice:alice:set-provider', async (_e, id, provider) => {
  const boom = await ready();
  if (boom) return { error: `App chưa khởi động được: ${boom}` };
  const providerT = provider === 'claude' ? 'claude' : 'opencode';
  const { state, updated } = registryModule.update(id, { provider: providerT });
  if (!updated) return { error: 'Không tìm thấy Alice.' };
  registry = state;
  if (registry.active === id) await activateAlice(id); // đổi engine ngay, không đợi restart
  return { ok: true };
});

/** Trạng thái đăng nhập Claude của MỘT Alice — đọc qua `claude auth status` với
 * `CLAUDE_CONFIG_DIR` cô lập của chính Alice đó, không đụng đăng nhập global. */
ipcMain.handle('alice:claude:status', async (_e, id) => {
  const alice = registry.alices.find((a) => a.id === id);
  if (!alice) return { error: 'Không tìm thấy Alice.' };
  return claudeAuthStatus(id);
});

/**
 * Tất cả những gì panel "Kết nối" bên phải cần, trong MỘT lượt gọi.
 *
 * Cố ý KHÔNG có con số quota còn lại: đã dò thật 2026-08-14, cả `claude` lẫn
 * `opencode` đều không có lệnh nào trả về số đó (`/usage` chỉ sống trong phiên
 * tương tác của Claude Code; `opencode stats` chỉ đếm mức dùng cục bộ). Bịa ra một
 * con số từ số liệu cục bộ rồi gọi nó là "quota" còn tệ hơn không hiện gì — Bệ hạ
 * chốt: chỉ cần thấy tích xanh là biết kết nối được.
 *
 * Không tự gọi theo chu kỳ: renderer nạp khi mở màn chat, khi đổi Alice, sau khi
 * đăng nhập xong, và khi bấm nút Làm mới. `claude auth status` là một tiến trình
 * con — gọi mỗi vài giây là đốt CPU cho một thứ gần như không bao giờ đổi.
 */
ipcMain.handle('alice:connection:info', async (_e, id) => {
  const boom = await ready();
  if (boom) return { error: `App chưa khởi động được: ${boom}` };
  const alice = registry.alices.find((a) => a.id === id) || currentAlice();
  if (!alice) return { error: 'Chưa có Alice nào.' };
  const base = aliceDirFor(alice.id);
  const picked = modelFor(alice);

  const out = {
    provider: alice.provider || 'opencode',
    model: picked.model,
    configuredModel: alice.model || null,
    warning: picked.warning,
    claude: null,
    opencode: null,
  };

  if (out.provider === 'claude') {
    out.claude = await claudeAuthStatus(alice.id);
  } else {
    const st = auth.authStatus(base);
    out.opencode = {
      configured: st.configured,
      // Chỉ tên provider + 4 số cuối — xem chú thích ở `auth.keyTails`.
      keys: auth.keyTails(base),
      binary: engine && engine.binSource === 'bundled' ? 'kèm theo app' : 'cài trên máy',
      available: Boolean(engine && engine.available),
    };
  }
  return out;
});

/** `claude auth status` với `CLAUDE_CONFIG_DIR` cô lập của một Alice. Tách ra vì
 * cả IPC `alice:claude:status` lẫn panel Kết nối đều cần đúng một thứ. */
function claudeAuthStatus(id) {
  const configDir = registryModule.claudeConfigDir(aliceDirFor(id));
  return new Promise((resolve) => {
    execFile('claude', ['auth', 'status'], {
      env: { ...process.env, CLAUDE_CONFIG_DIR: configDir }, timeout: 10000,
    }, (err, stdout) => {
      if (err) { resolve({ loggedIn: false, error: 'Chưa cài `claude` hoặc chưa đăng nhập.' }); return; }
      try { resolve(JSON.parse(stdout)); } catch { resolve({ loggedIn: false }); }
    });
  });
}

/**
 * Mở luồng đăng nhập Claude CHO Bệ hạ — tự spawn `claude auth login`, tự mở trình
 * duyệt (CLI tự làm việc đó), không bắt gõ lệnh trong terminal.
 *
 * Không đợi tiến trình con thoát: đăng nhập cần Bệ hạ bấm "Cho phép" trên trình
 * duyệt, có thể mất cả phút. Chỉ đợi vài giây đầu để bắt URL PHÒNG KHI máy không tự
 * mở được trình duyệt mặc định — có URL thì UI vẫn đưa ra để Bệ hạ tự bấm.
 */
ipcMain.handle('alice:claude:login', async (_e, id) => {
  const alice = registry.alices.find((a) => a.id === id);
  if (!alice) return { error: 'Không tìm thấy Alice.' };
  const base = aliceDirFor(id);
  const configDir = registryModule.claudeConfigDir(base);
  fs.mkdirSync(configDir, { recursive: true });

  return new Promise((resolve) => {
    let settled = false;
    let child;
    try {
      child = require('node:child_process').spawn('claude', ['auth', 'login', '--claudeai'], {
        env: { ...process.env, CLAUDE_CONFIG_DIR: configDir },
        windowsHide: true,
      });
    } catch (err) {
      resolve({ error: `Không chạy được claude: ${err.message}` });
      return;
    }
    child.on('error', (err) => {
      if (!settled) { settled = true; resolve({ error: `Không chạy được claude: ${err.message}` }); }
    });
    let buf = '';
    let url = null;
    child.stdout.on('data', (d) => {
      buf += d.toString('utf8');
      const m = buf.match(/https?:\/\/\S+/);
      if (m && !url) url = m[0];
    });
    setTimeout(() => {
      if (!settled) { settled = true; resolve({ ok: true, url }); }
    }, 2000);
    child.unref();
  });
});

/**
 * Mở dashboard Alice Brain THẬT (`apps/web` gốc, vendor sẵn) cho một Alice.
 *
 * `NEXT_PUBLIC_API_BASE` đóng cứng lúc build vào cổng 8932 (xem
 * `docs/superpowers/specs/2026-08-13-brain-dashboard-design.md`) — nên chỉ CHỈNH
 * `sag_api.desktop` đang phục vụ cổng đó là Alice nào, KHÔNG đổi cổng của Next.js.
 * Đổi Alice đang xem = tắt sidecar cũ, bật sidecar mới của Alice khác trên
 * ĐÚNG cổng đó; Next.js chỉ khởi một lần, dùng lại cho mọi Alice.
 */
ipcMain.handle('alice:brain:open', async (_e, id) => {
  const boom = await ready();
  if (boom) return { error: `App chưa khởi động được: ${boom}` };
  if (!dashboard.available) {
    return { error: 'Bản cài này thiếu dashboard (runtime/webui) — build lại bằng npm run bundle:webui.' };
  }
  const alice = registry.alices.find((a) => a.id === id);
  if (!alice) return { error: 'Không tìm thấy Alice.' };
  const base = aliceDirFor(id);

  try {
    if (dashboardSidecar && dashboardAliceId !== id) {
      dashboardSidecar.stop();
      dashboardSidecar = null;
    }
    if (!dashboardSidecar) {
      const bs = new BrainSidecar(
        { ...(settings.brain || {}), http: true, port: 8932 },
        { dataDir: path.join(base, 'brain') }
      );
      // Lần đầu mở brain của Alice này: python còn mới, Windows Defender quét cả
      // cây thư viện nên startup lâu — cho 180s thay vì 30s mặc định (30s hết giờ
      // bỏ cuộc trong khi tiến trình vẫn sống chính là gốc bug "bấm mãi không mở",
      // đo thật 2026-08-14: sidecar lên sau ~5s lần thường, lâu hơn nhiều lần đầu).
      const firstRun = !fs.existsSync(path.join(base, 'brain', 'sag.db'));
      await bs.start({ timeoutMs: firstRun ? 180000 : 60000 });
      // CHỈ gán khi start() THÀNH CÔNG — gán trước rồi bị throw là lần bấm sau
      // stop() giết đúng tiến trình đang khoẻ (đã fix ở sidecar.start, đây là nửa
      // còn lại: main không được giữ tham chiếu tới thứ chưa sống).
      dashboardSidecar = bs;
      dashboardAliceId = id;
    }
    await dashboard.start();
    shell.openExternal(dashboard.url);
    return { ok: true, url: dashboard.url };
  } catch (err) {
    // Sidecar lỗi thì KHÔNG giữ tham chiếu — lần bấm sau phải được thử lại từ đầu.
    dashboardSidecar = null;
    dashboardAliceId = null;
    return { error: String(err.message || err) };
  }
});

/**
 * ── Báo cáo tuần ───────────────────────────────────────────────────────────
 * Config sống ở `<alice-home>/report.json` — RIÊNG từng Alice (mỗi Alice một
 * folder, một brain, một session). Alice gọi tool MCP `report` để thu dữ liệu
 * (git_commits / plane_issues / chat_messages) và in PDF (export_pdf).
 */

ipcMain.handle('alice:report:get', async () => {
  const boom = await ready();
  if (boom) return { error: `App chưa khởi động được: ${boom}` };
  if (!registry.active) return { config: reportConfigForUI('') };
  return { config: reportConfigForUI(aliceDirFor(registry.active)) };
});

ipcMain.handle('alice:report:save', async (_e, patch) => {
  const boom = await ready();
  if (boom) return { error: `App chưa khởi động được: ${boom}` };
  if (!registry.active) return { error: 'Chưa có Alice nào.' };
  const base = aliceDirFor(registry.active);
  // API key đã bị che trong UI ("••••" = không đổi) — chuỗi che thì GIỮ nguyên
  // bản cũ, không được ghi chuỗi che vào file cấu hình.
  if (typeof patch.planeApiKey === 'string' && /^•/.test(patch.planeApiKey)) {
    delete patch.planeApiKey;
  }
  reportCfg.save(base, patch);
  // Ghi lại workspace: opencode.json + .mcp.json cần mang server report mới (đường
  // dẫn config không đổi nên thực chất là refresh cho chắc — rẻ mà không hại gì).
  try {
    provisionWorkspace(settings, {
      brainMcp: brain && brain.available ? brain.mcpConfig() : null,
      reportMcp: reportMcpFor(base),
      dir: path.join(base, 'workspace'),
      model: currentAlice() && currentAlice().provider === 'claude' ? null : modelFor(currentAlice()).model,
    });
  } catch (err) {
    log.warn(`report:save re-provision: ${err.message}`);
  }
  return { config: reportConfigForUI(base) };
});

ipcMain.handle('alice:report:pick', async () => {
  const r = await dialog.showOpenDialog(win, {
    title: 'Chọn file service account Google (JSON)',
    filters: [{ name: 'Service account JSON', extensions: ['json'] }],
    properties: ['openFile'],
  });
  if (r.canceled || !r.filePaths.length) return { path: '' };
  return { path: r.filePaths[0] };
});

/**
 * "Làm báo cáo tuần ngay" từ UI: nhờ Alice chạy MỘT lượt có sẵn prompt, các tool
 * report thu dữ liệu từ mốc thứ 5 tuần trước, viết báo cáo theo template — rồi app
 * in nguyên văn câu trả lời ra PDF. Không gửi GÌ lên Google Chat (chỉ đọc).
 */
ipcMain.handle('alice:report:run', async (event) => {
  const boom = await ready();
  if (boom) return { error: `App chưa khởi động được: ${boom}` };
  if (!registry.active) return { error: 'Chưa có Alice nào.' };
  if (!runTurn) return { error: 'Chưa có Alice nào đang chạy.' };
  if (busy) return { error: 'Đang bận một lượt khác — chờ Alice trả lời xong đã.' };
  if (!engine.available) return { error: 'Engine của Alice chưa sẵn sàng.' };

  const base = aliceDirFor(registry.active);
  const c = reportCfg.load(base);
  if (!c.googleServiceAccount && !c.planeApiKey && !c.gitRepos.length) {
    return { error: 'Chưa cấu hình nguồn nào (Google Chat / Plane / git repos) trong Báo cáo tuần.' };
  }
  const since = reportCfg.lastThursday();
  const template = c.templatePath && fs.existsSync(c.templatePath)
    ? fs.readFileSync(c.templatePath, 'utf8')
    : '';
  const prompt = [
    '[YÊU CẦU TỰ ĐỘNG — BÁO CÁO TUẦN]',
    `Bệ hạ cần báo cáo tuần từ THỨ 5 TUẦN TRƯỚC (${since}) tới hôm nay.`,
    '',
    'Các bước, làm theo ĐÚNG thứ tự:',
    '1. Gọi tool `git_commits` (các repo đã cấu hình sẵn).',
    '2. Gọi tool `plane_issues`.',
    '3. Gọi tool `chat_messages` (CHỈ ĐỌC — tuyệt đối không gửi tin nhắn lên Google Chat).',
    '4. Viết báo cáo markdown đầy đủ theo template bên dưới, điền số liệu thật từ 3 bước trên.',
    '5. Gọi tool `export_pdf` với `markdown` là báo cáo vừa viết — `outPath` để trống để app tự đặt tên.',
    '',
    `Template báo cáo${template ? '' : ' (chưa có file template — tự dựng khung hợp lý, giữ mục “Tổng kết tuần”, “Plane tasks”, “Commits”, “Google Chat nổi bật”, “Vướng mắc”, “Tuần tới”)'}:`,
    template || '',
    '',
    'QUY TẮC:',
    '- Câu trả lời CUỐI CÙNG của em là NGUYÊN VĂN báo cáo markdown (không lời chào, không giải thích bên ngoài) — app sẽ lấy nó in PDF.',
    '- Báo cáo bằng tiếng Việt.',
  ].join('\n');

  busy = true;
  const sender = event.sender;
  try {
    pdfExporter = pdfExporter || new PdfExporter();
    await pdfExporter.start();
    const seen = new Set();
    const res = await runTurn(prompt, (partial, ev) => {
      const act = toolActivity(ev);
      if (act && !seen.has(act.key)) {
        seen.add(act.key);
        sender.send('alice:stream', { activity: act.label, type: 'tool' });
      }
      if (partial) sender.send('alice:stream', { text: partial, type: 'text' });
    });
    const markdown = String(res && res.text || '');
    if (!markdown.trim()) return { error: 'Alice trả về báo cáo rỗng — thử lại.' };
    const today = new Date().toISOString().slice(0, 10);
    const dir = c.outputDir || base;
    const safeName = (c.outputName || 'HRM_Weekly_Report').replace(/[\\/:*?"<>|]/g, '_');
    const outPath = path.join(dir, `${safeName} ${today}.pdf`);
    const printed = await pdfExporter.print(markdown, c.outputName || 'Báo cáo tuần', outPath);
    log.info(`weekly report: ${printed.path} (${printed.pages} trang)`);
    return { ok: true, path: printed.path, pages: printed.pages, text: markdown };
  } catch (err) {
    return { error: `Làm báo cáo thất bại: ${err.message}` };
  } finally {
    busy = false;
  }
});

ipcMain.handle('alice:alice:select', async (_e, id) => {
  const boom = await ready();
  if (boom) return { error: `App chưa khởi động được: ${boom}` };
  try {
    await activateAlice(id);
  } catch (err) {
    return { error: String(err.message || err) };
  }
  return { ok: true };
});

/**
 * Tắt RIÊNG một Alice: buông chat db, brain, lịch hẹn, máy chủ public và tunnel
 * của nó — nhưng app vẫn chạy và các Alice khác không bị đụng tới.
 *
 * Trước đây chỉ có "Tắt Alice hẳn" (tắt cả app). Muốn một Alice ngừng ăn tài nguyên
 * hoặc ngừng phục vụ web thì phải tắt tất — hoặc xoá nó đi, mất sạch dữ liệu.
 */
ipcMain.handle('alice:alice:stop', async (_e, id) => {
  const boom = await ready();
  if (boom) return { error: `App chưa khởi động được: ${boom}` };
  try {
    const pub = publicServers.get(id);
    if (pub) { pub.stop(); publicServers.delete(id); }
    const tun = tunnels.get(id);
    if (tun) { tun.stop(); tunnels.delete(id); }

    if (registry.active === id) {
      if (scheduler) scheduler.stop();
      if (brain) brain.stop();
      if (store) { store.close(); store = null; }
      scheduler = null;
      brain = null;
      memory = null;
      runTurn = null;
      registry.active = null;
      registryModule.save(registry);
      if (win) {
        win.webContents.send('alice:alice-changed', {
          id: null, alices: registry.alices, active: null, stopped: id,
        });
      }
    }
    log.info(`alice stopped: ${id}`);
    return { ok: true };
  } catch (err) {
    return { error: String(err.message || err) };
  }
});

ipcMain.handle('alice:alice:remove', async (_e, id) => {
  const boom = await ready();
  if (boom) return { error: `App chưa khởi động được: ${boom}` };
  // Tắt máy chủ của Alice bị xoá trước khi xoá dữ liệu.
  const pub = publicServers.get(id);
  if (pub) {
    pub.stop();
    publicServers.delete(id);
  }
  const tun = tunnels.get(id);
  if (tun) {
    tun.stop();
    tunnels.delete(id);
  }

  // Xoá Alice ĐANG MỞ: phải buông hết file handle TRƯỚC khi xoá thư mục. Trên
  // Windows, chat.db (WAL) và lancedb của brain đang mở là `fs.rm` ăn EBUSY —
  // ngoại lệ ném ra giữa IPC, renderer chờ một promise không bao giờ về, và app
  // trông như treo. Đúng triệu chứng "xoá Alice thì lag, đơ, UI không cập nhật".
  if (registry.active === id) {
    if (scheduler) scheduler.stop();
    if (brain) brain.stop();
    if (store) { store.close(); store = null; }
    scheduler = null;
    brain = null;
    memory = null;
    runTurn = null;
  }

  let removed;
  let state;
  let keptDir = null;
  try {
    ({ state, removed, keptDir } = await registryModule.remove(id));
  } catch (err) {
    // Xoá file hỏng thì Alice đã rời danh sách rồi — nói thẳng chỗ còn sót thay vì
    // để người dùng ngồi nhìn màn hình đứng im.
    log.error(`alice remove: xoá thư mục hỏng: ${err.message}`);
    registry = registryModule.load();
    return { error: `Đã gỡ Alice khỏi danh sách nhưng chưa xoá được thư mục: ${err.message}` };
  }
  registry = state;
  if (!removed) return { error: 'Không tìm thấy Alice.' };
  log.info(`alice removed: ${id}${keptDir ? ` (giữ thư mục ${keptDir})` : ''}`);
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
  return { ok: true, keptDir };
});

// ── public: biến Alice thành máy chủ ───────────────────────────────────────

/** Lấy PublicServer của Alice (tạo mới nếu chưa có — chưa start). */
function publicServerFor(id) {
  let pub = publicServers.get(id);
  if (!pub) {
    const alice = registry.alices.find((a) => a.id === id);
    if (!alice) throw new Error('Không tìm thấy Alice.');
    const base = aliceDirFor(id);
    const bs = new BrainSidecar(settings.brain || {}, { dataDir: path.join(base, 'brain') });
    // Engine RIÊNG của máy chủ public, độc lập với engine của cửa sổ app — cùng
    // Alice nhưng hai tiến trình engine khác nhau chạy song song (app + public).
    const pubEngine = engineFor(alice, settings);
    pubEngine.setBaseDir(base);
    pubEngine.settings = { ...settings, model: modelFor(alice).model };
    pub = new PublicServer({
      alice,
      baseDir: base,
      settings,
      engine: pubEngine,
      brainMcp: bs.available ? bs.mcpConfig() : null,
      reportMcp: reportMcpFor(base),
      log,
      // Trang chat của khách hiện đúng ảnh của Alice này, không phải ngôi sao chung.
      avatar: () => avatar.current(base),
      // Khách nhắn qua trang web công khai → nếu Alice này ĐANG MỞ trong app thì
      // đẩy luôn vào cửa sổ, không thì Bệ hạ chỉ thấy sau khi tự đổi Alice đi rồi
      // quay lại (đúng triệu chứng "chat trên điện thoại, app không thấy").
      onMessage: (row) => {
        if (registry.active === id && win && !win.isDestroyed()) {
          win.webContents.send('alice:public-message', { aliceId: id, message: row });
        }
      },
      // Khách nhắn qua trang public: app phải thấy "Alice đang trả lời…" y hệt lúc
      // Bệ hạ tự chat trong app, không thì cửa sổ đứng im cho tới khi câu trả lời
      // hiện ra — đúng triệu chứng "không thấy typing" (đo thật 2026-08-13).
      onBusy: (busy, activity) => {
        if (registry.active === id && win && !win.isDestroyed()) {
          win.webContents.send('alice:public-busy', { aliceId: id, busy, activity });
        }
      },
    });
    publicServers.set(id, pub);
  }
  return pub;
}

/** Tunnel của Alice (tạo mới nếu chưa có — chưa mở). */
function tunnelFor(id) {
  let t = tunnels.get(id);
  if (!t) {
    t = new Tunnel({
      resourcesDir: config.RESOURCES_DIR,
      toolsDir: path.join(config.DATA_DIR, 'tools'),
      log,
    });
    tunnels.set(id, t);
  }
  return t;
}

ipcMain.handle('alice:public:toggle', async (_e, id, { enabled, port }) => {
  const boom = await ready();
  if (boom) return { error: `App chưa khởi động được: ${boom}` };
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
      // Tắt máy chủ thì tunnel trỏ vào hư không — đóng luôn cho khỏi treo một
      // link công khai không dẫn tới đâu.
      const t = tunnels.get(id);
      if (t) t.stop();
      const cfg = pub.config();
      pub.saveConfig({ ...cfg, enabled: false });
    }
    return { ok: true };
  } catch (err) {
    return { error: String(err.message || err) };
  }
});

ipcMain.handle('alice:public:set-mode', async (_e, id, mode) => {
  const boom = await ready();
  if (boom) return { error: `App chưa khởi động được: ${boom}` };
  try {
    const pub = publicServerFor(id);
    const next = ['anyone', 'code', 'account'].includes(mode) ? mode : 'anyone';

    // Đang mở ra Internet mà hạ xuống `anyone` = phơi Alice cho cả thế giới.
    const t = tunnels.get(id);
    if (next === 'anyone' && t && t.running) {
      return { error: 'Đang chia sẻ ra Internet — chế độ "ai có link cũng vào" chỉ an toàn trong mạng nội bộ. Tắt chia sẻ Internet trước, hoặc chọn mã truy cập / tài khoản.' };
    }

    const cfg = pub.config();
    cfg.mode = next;
    if (next === 'code' && !cfg.code) cfg.code = require('./public-server').newAccessCode();
    pub.saveConfig(cfg);
    log.info(`public mode: ${id} → ${next}`);
    return { ok: true };
  } catch (err) {
    return { error: String(err.message || err) };
  }
});

ipcMain.handle('alice:public:code:rotate', async (_e, id) => {
  const boom = await ready();
  if (boom) return { error: `App chưa khởi động được: ${boom}` };
  try {
    const code = publicServerFor(id).rotateCode();
    log.info(`public access code rotated: ${id}`);
    return { code };
  } catch (err) {
    return { error: String(err.message || err) };
  }
});

ipcMain.handle('alice:public:account:add', async (_e, id, { username, password }) => {
  const boom = await ready();
  if (boom) return { error: `App chưa khởi động được: ${boom}` };
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
  const boom = await ready();
  if (boom) return { error: `App chưa khởi động được: ${boom}` };
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
  const boom = await ready();
  if (boom) return { error: `App chưa khởi động được: ${boom}` };
  try {
    const pub = publicServerFor(id);
    const cfg = pub.config();
    const port = pub.port || cfg.port;
    const tunnel = tunnelFor(id).status();
    const lanUrl = shareableUrl(port);
    return {
      enabled: pub.running,
      mode: cfg.mode,
      port,
      // Mã chỉ có nghĩa ở mode `code` — không đẩy ra UI ở mode khác.
      code: cfg.mode === 'code' ? cfg.code : null,
      accounts: cfg.accounts.map((a) => ({ username: a.username })),
      // Link để chia sẻ: có tunnel thì đó mới là link người ngoài mạng vào được.
      shareUrl: tunnel.url || lanUrl,
      lanUrl,
      localUrl: `http://127.0.0.1:${port}`,
      lanUrls: shareableIps().map((ip) => `http://${ip}:${port}`),
      tunnel,
      ...pub.stats(),
    };
  } catch (err) {
    return { error: String(err.message || err) };
  }
});

// ── chia sẻ ra Internet (cloudflared) ──────────────────────────────────────

ipcMain.handle('alice:tunnel:status', async (_e, id) => {
  const boom = await ready();
  if (boom) return { error: `App chưa khởi động được: ${boom}` };
  try {
    return tunnelFor(id).status();
  } catch (err) {
    return { error: String(err.message || err) };
  }
});

ipcMain.handle('alice:tunnel:download', async (event, id) => {
  const boom = await ready();
  if (boom) return { error: `App chưa khởi động được: ${boom}` };
  try {
    const t = tunnelFor(id);
    const p = await t.download((pct) => {
      event.sender.send('alice:tunnel-progress', { id, pct });
    });
    return { ok: true, binary: p };
  } catch (err) {
    log.error(`tunnel download failed: ${err.message}`);
    return { error: String(err.message || err) };
  }
});

ipcMain.handle('alice:tunnel:toggle', async (_e, id, enabled) => {
  const boom = await ready();
  if (boom) return { error: `App chưa khởi động được: ${boom}` };
  try {
    const t = tunnelFor(id);
    if (!enabled) {
      t.stop();
      return { ok: true, tunnel: t.status() };
    }
    const pub = publicServerFor(id);
    if (!pub.running) {
      return { error: 'Bật máy chủ trước đã — tunnel chỉ chuyển tiếp vào một máy chủ đang chạy.' };
    }
    // Luật cứng: link tunnel là link CÔNG KHAI trên Internet. Mode `anyone` cộng
    // với tunnel = ai dò trúng URL cũng chat được và đốt API key của chủ máy.
    if (pub.config().mode === 'anyone') {
      return { error: 'Ra Internet thì phải có cửa: chọn "mã truy cập" hoặc "tài khoản" trước khi chia sẻ.' };
    }
    const { url } = await t.start(pub.port);
    log.info(`tunnel for ${id}: ${url}`);
    return { ok: true, tunnel: t.status() };
  } catch (err) {
    log.error(`tunnel toggle failed: ${err.message}`);
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
  const base = registry.active ? aliceDirFor(registry.active) : null;
  return { uri: avatar.current(base), custom: avatar.isCustom(base) };
});

ipcMain.handle('alice:avatar:pick', async () => {
  const res = await dialog.showOpenDialog(win, {
    title: 'Chọn ảnh cho Alice',
    properties: ['openFile'],
    filters: [{ name: 'Ảnh', extensions: avatar.ALLOWED.map((e) => e.slice(1)) }],
  });
  if (res.canceled || !res.filePaths.length) return { canceled: true };
  const base = registry.active ? aliceDirFor(registry.active) : null;
  if (!base) return { error: 'Chưa có Alice nào.' };
  try {
    return { uri: avatar.set(res.filePaths[0], base), custom: true };
  } catch (err) {
    return { error: String(err.message || err) };
  }
});

ipcMain.handle('alice:avatar:reset', async () => {
  const base = registry.active ? aliceDirFor(registry.active) : null;
  if (!base) return { uri: avatar.current(null), custom: false };
  return { uri: avatar.reset(base), custom: false };
});

ipcMain.handle('alice:auth:set', async (_e, { provider, key }) => {
  if (!provider || !key) return { error: 'Thiếu provider hoặc key.' };
  const base = registry.active ? aliceDirFor(registry.active) : null;
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
  if (await ready()) return [];
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
  if (await ready()) return [];
  const conv = store ? store.currentConversation() : null;
  if (!conv) return [];
  return store.recent(conv.id, limit).map((m) => ({
    id: m.id, role: m.role, text: m.text, ts: m.ts,
  }));
});

ipcMain.handle('alice:search', async (_e, query) => {
  if (await ready()) return [];
  if (!store) return [];
  return store.search(query, 20).map((m) => ({ id: m.id, role: m.role, text: m.text, ts: m.ts }));
});

ipcMain.handle('alice:models', async () => {
  const boom = await ready();
  if (boom) return { models: [], error: `App chưa khởi động được: ${boom}` };
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
  const boom = await ready();
  if (boom) return { error: `App chưa khởi động được: ${boom}` };
  const conv = store ? store.currentConversation() : null;
  if (conv) {
    log.info(`clear chat: conversation ${conv.id} — ${store.count(conv.id)} messages`);
    store.clearConversation(conv.id);
  }
  return { ok: true };
});

/**
 * Xoá ĐÚNG MỘT tin khỏi kho của app.
 *
 * Chỉ trong phạm vi cuộc trò chuyện ĐANG MỞ — một id lạc từ renderer không xoá
 * được tin của cuộc khác. Session engine vẫn còn nhớ câu này tới lần xoay session
 * kế tiếp; UI phải nói ra điều đó trước khi xoá.
 */
ipcMain.handle('alice:message:remove', async (_e, id) => {
  const boom = await ready();
  if (boom) return { error: `App chưa khởi động được: ${boom}` };
  if (!store) return { error: 'Chưa có Alice nào.' };
  const conv = store.currentConversation();
  if (!conv) return { error: 'Chưa có cuộc trò chuyện nào.' };
  const removed = store.removeMessage(conv.id, id);
  if (!removed) return { error: 'Không tìm thấy tin nhắn đó trong cuộc trò chuyện này.' };
  log.info(`message removed: #${id} (conversation ${conv.id})`);
  return { ok: true };
});

// ── lịch hẹn (của Alice đang mở) ───────────────────────────────────────────

ipcMain.handle('alice:sched:list', async () => {
  if (await ready()) return [];
  return store ? store.listSchedules() : [];
});

/** Giờ:phút hợp lệ chưa — dùng chung cho `add` và `update`. Bản trước chỉ kiểm ở
 * `add`, nên sửa một lịch đang chạy thành `99:99` là nó im lặng không bao giờ tới
 * giờ nữa (`isDue` so bằng `===`), mà UI vẫn hiện như một lịch bình thường. */
function badTime(hour, minute) {
  const h = Number(hour);
  const m = Number(minute);
  if (!Number.isInteger(h) || h < 0 || h > 23 || !Number.isInteger(m) || m < 0 || m > 59) {
    return 'Giờ phải là số từ 0–23, phút từ 0–59.';
  }
  return null;
}

/** Ngày trong tuần hợp lệ chưa: NULL (mọi ngày) hoặc số 0–6 theo Date.getDay() (0=CN). */
function badWeekday(weekday) {
  if (weekday === null || weekday === undefined || weekday === '') return null;
  const w = Number(weekday);
  if (!Number.isInteger(w) || w < 0 || w > 6) return 'Ngày trong tuần phải từ CN (0) tới T7 (6).';
  return null;
}

ipcMain.handle('alice:sched:add', async (_e, { hour, minute, task, weekday = null }) => {
  const boom = await ready();
  if (boom) return { error: `App chưa khởi động được: ${boom}` };
  if (!store) return { error: 'Chưa có Alice nào.' };
  const h = Number(hour);
  const m = Number(minute);
  const bad = badTime(hour, minute);
  if (bad) return { error: bad };
  const badW = badWeekday(weekday);
  if (badW) return { error: badW };
  const text = String(task || '').trim();
  if (!text) return { error: 'Chưa nhập việc cần làm.' };
  const w = weekday === '' || weekday === null || weekday === undefined ? null : Number(weekday);
  const sched = store.addSchedule({ hour: h, minute: m, task: text, weekday: w });
  log.info(`schedule added #${sched.id} at ${h}:${String(m).padStart(2, '0')}${w === null ? '' : ` weekday=${w}`}`);
  return { sched };
});

ipcMain.handle('alice:sched:update', async (_e, id, patch) => {
  const boom = await ready();
  if (boom) return { error: `App chưa khởi động được: ${boom}` };
  if (!store) return { error: 'Chưa có Alice nào.' };
  if (patch.task !== undefined) patch.task = String(patch.task || '').trim();
  if (patch.task === '') return { error: 'Việc cần làm không được để trống.' };
  const cur = store.getSchedule(Number(id));
  if (!cur) return { error: 'Không tìm thấy lịch hẹn.' };
  if (patch.hour !== undefined || patch.minute !== undefined) {
    const bad = badTime(
      patch.hour !== undefined ? patch.hour : cur.hour,
      patch.minute !== undefined ? patch.minute : cur.minute
    );
    if (bad) return { error: bad };
    if (patch.hour !== undefined) patch.hour = Number(patch.hour);
    if (patch.minute !== undefined) patch.minute = Number(patch.minute);
  }
  if (patch.weekday !== undefined) {
    const badW = badWeekday(patch.weekday);
    if (badW) return { error: badW };
    patch.weekday = patch.weekday === '' || patch.weekday === null ? null : Number(patch.weekday);
  }
  const sched = store.updateSchedule(Number(id), patch);
  if (!sched) return { error: 'Không tìm thấy lịch hẹn.' };
  return { sched };
});

ipcMain.handle('alice:sched:remove', async (_e, id) => {
  const boom = await ready();
  if (boom) return { error: `App chưa khởi động được: ${boom}` };
  if (!store) return { error: 'Chưa có Alice nào.' };
  store.removeSchedule(Number(id));
  return { ok: true };
});

// ── tắt hẳn ────────────────────────────────────────────────────────────────

ipcMain.handle('alice:shutdown', async () => {
  log.info('shutdown requested from UI');
  isQuitting = true;
  for (const t of tunnels.values()) t.stop();
  tunnels.clear();
  for (const pub of publicServers.values()) pub.stop();
  publicServers.clear();
  if (dashboardSidecar) { dashboardSidecar.stop(); dashboardSidecar = null; }
  dashboard.stop();
  if (pdfExporter) { pdfExporter.stop(); pdfExporter = null; }
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
  const picked = modelFor(alice);
  modelWarning = picked.warning;
  engine.settings = { ...settings, model: picked.model };
  if (memory) memory.settings = settings;
  provisionWorkspace(settings, {
    brainMcp: brain && brain.available ? brain.mcpConfig() : null,
    reportMcp: registry.active ? reportMcpFor(aliceDirFor(registry.active)) : null,
    dir: registry.active ? path.join(aliceDirFor(registry.active), 'workspace') : null,
    model: alice && alice.provider === 'claude' ? null : picked.model,
  });
  return settings;
});

ipcMain.handle('alice:send', async (event, text) => {
  const boom = await ready();
  if (boom) return { error: `App chưa khởi động được: ${boom}` };
  if (!runTurn) return { error: 'Chưa có Alice nào. Tạo Alice trước đã.' };
  if (busy) return { error: 'Đang bận một lượt khác — chờ em trả lời xong đã ạ.' };
  if (!engine.available) {
    return { error: 'Chưa có binary opencode. Đặt vào runtime/opencode/ hoặc cài opencode trên máy.' };
  }
  busy = true;
  const sender = event.sender;
  try {
    const seen = new Set();
    const res = await runTurn(text, (partial, ev) => {
      // Alice gọi công cụ thì phải THẤY nó đang gọi cái gì: một lượt tra trí nhớ
      // mất 40 giây nhìn y hệt một lượt treo, và người dùng bấm dừng vì tưởng hỏng.
      const act = toolActivity(ev);
      if (act && !seen.has(act.key)) {
        seen.add(act.key);
        sender.send('alice:stream', { activity: act.label, type: 'tool' });
      }
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
    // Alice này đang public thì tin vừa chat trong app phải lên trang web NGAY —
    // không thì máy đang xem trang public chỉ thấy sau khi tự tải lại (mất
    // "realtime", đúng triệu chứng "chat trên app, điện thoại không thấy").
    if (registry.active) {
      const pub = publicServers.get(registry.active);
      if (pub && pub.running) {
        try {
          pub.broadcastFromDesktop(res.conversationId, res.messageId - 1);
        } catch (err) {
          log.error(`public broadcast (app→web) failed: ${err.message}`);
        }
      }
    }
    return res;
  } catch (err) {
    // Người dùng bấm dừng không phải một lỗi — UI không được vẽ nó thành bong bóng đỏ.
    if (err.cancelled) {
      log.info('turn cancelled by user');
      return { canceled: true };
    }
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
  // Bình thường: bản đang chạy sẽ hiện cửa sổ lên (xem `second-instance` bên dưới)
  // và bản này thoát. Nhưng nếu lần trước app bị GIẾT CỨNG, khoá có thể còn treo mà
  // không tiến trình nào giữ — khi đó bấm mở Alice không ra gì, không cửa sổ, không
  // báo lỗi, không một dòng log. Đã tốn một lượt chẩn đoán vì đúng chỗ này im lặng.
  log.info(`không lấy được khoá single-instance (userData=${app.getPath('userData')}) — thoát. `
    + 'Nếu không có Alice nào đang chạy, xoá file "lockfile" trong thư mục đó rồi mở lại.');
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
      bootError = String(err.message || err);
      log.error(`fatal: ${err.stack || err}`);
      if (win) win.webContents.send('alice:fatal', String(err.stack || err));
      // KHÔNG re-throw: xem chú thích ở `bootError`. Handler nào cần thì đọc
      // `bootError` và trả lỗi thành câu nói được, thay vì reject im lặng.
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
    for (const t of tunnels.values()) t.stop();
    tunnels.clear();
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
