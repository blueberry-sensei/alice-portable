'use strict';

const path = require('node:path');
const fs = require('node:fs');
const os = require('node:os');

/**
 * Đường dẫn của bản portable.
 *
 * Luật: dữ liệu nằm CẠNH file exe, không nằm trong %APPDATA%. Đó là ý nghĩa của
 * "portable" — cắm USB sang máy khác thì lịch sử chat, cấu hình và brain đi theo.
 * `PORTABLE_EXECUTABLE_DIR` do chính electron-builder target `portable` set lúc chạy.
 */
function rootDir() {
  if (process.env.ALICE_PORTABLE_ROOT) return process.env.ALICE_PORTABLE_ROOT;

  // Target `portable` (một .exe tự giải nén) tự set biến này.
  if (process.env.PORTABLE_EXECUTABLE_DIR) return process.env.PORTABLE_EXECUTABLE_DIR;

  // Bản đã đóng gói (`--win dir`): KHÔNG suy từ `__dirname`. Ở đó `__dirname` nằm
  // trong `resources/app.asar/src/main`, nên `../..` ra thành đường dẫn BÊN TRONG
  // asar — một nơi không ghi được, và app sẽ chết lúc tạo `alice-data`.
  // Neo vào thư mục chứa file exe mới đúng nghĩa "dữ liệu nằm cạnh app".
  try {
    const { app } = require('electron');
    if (app && app.isPackaged) return path.dirname(app.getPath('exe'));
  } catch {
    // Chạy dưới ELECTRON_RUN_AS_NODE (test): `require('electron')` trả về một chuỗi
    // đường dẫn chứ không phải module, `app` không tồn tại — rơi xuống nhánh dev.
  }

  // Dev (`npm start`) và khi chạy test: đứng cạnh package.json.
  return path.resolve(__dirname, '..', '..');
}

const ROOT = rootDir();
const DATA_DIR = path.join(ROOT, 'alice-data');
const RESOURCES_DIR = process.resourcesPath && fs.existsSync(path.join(process.resourcesPath, 'runtime'))
  ? path.join(process.resourcesPath, 'runtime')
  : path.join(ROOT, 'runtime');

/**
 * Tên hiển thị của Alice = "Alice" + tên thư mục cài.
 *
 * Một máy có thể cài nhiều Alice độc lập (mỗi bản một thư mục, dữ liệu riêng),
 * nên tên folder là cách phân biệt dễ nhất với người không rành kỹ thuật:
 * folder "GoDine" → "Alice GoDine", folder "PHUONG" → "Alice PHUONG".
 * Folder đã có sẵn chữ "Alice" thì không lặp: "alice-godine" → "Alice GoDine".
 */
function appName() {
  const base = path.basename(ROOT);
  if (!base) return 'Alice';
  const cleaned = base.replace(/^alice[\s._-]*/i, '');
  if (!cleaned) return 'Alice';
  return `Alice ${cleaned}`;
}

/**
 * Tìm binary opencode.
 *
 * D-0053 mục 3: gọi bằng ĐƯỜNG DẪN TUYỆT ĐỐI tới runtime nhúng — không `npx`, không
 * dựa vào PATH. `M-0035` đã tốn một lượt routine mù vì đúng cái bẫy PATH này.
 * Chỉ khi không có bản nhúng (đang dev) mới rơi về máy, và khi đó phải nói rõ ra.
 */
function resolveOpencode() {
  const bundled = path.join(RESOURCES_DIR, 'opencode', process.platform === 'win32' ? 'opencode.exe' : 'opencode');
  if (fs.existsSync(bundled)) return { path: bundled, source: 'bundled' };

  const candidates = [];
  if (process.platform === 'win32') {
    const appdata = process.env.APPDATA || path.join(os.homedir(), 'AppData', 'Roaming');
    const nvmRoot = path.join(appdata, 'nvm');
    if (fs.existsSync(nvmRoot)) {
      for (const ver of fs.readdirSync(nvmRoot)) {
        candidates.push(path.join(nvmRoot, ver, 'node_modules', 'opencode-ai', 'bin', 'opencode.exe'));
      }
    }
    candidates.push(path.join(os.homedir(), '.opencode', 'bin', 'opencode.exe'));
  } else {
    candidates.push(path.join(os.homedir(), '.opencode', 'bin', 'opencode'));
    candidates.push('/usr/local/bin/opencode');
  }
  for (const c of candidates) {
    if (fs.existsSync(c)) return { path: c, source: 'host' };
  }
  return { path: null, source: 'missing' };
}

const DEFAULTS = {
  // D-0054 mục 5 + D-0055: chuỗi model là GỢI Ý thứ tự ưu tiên, danh sách thật
  // luôn duyệt từ `opencode models` lúc chạy. Model nào không còn thì bỏ qua,
  // không chết — đó là lý do không hard-code cứng.
  modelPreference: [
    'opencode/deepseek-v4-flash-free',
    'opencode/nemotron-3-ultra-free',
    'opencode/laguna-s-2.1-free',
    'opencode/mimo-v2.5-free',
    'opencode/ling-3.0-flash-free',
    'opencode/north-mini-code-free',
  ],
  // Trần cửa sổ của model, đơn vị token. Không có API nào của opencode trả số này
  // nên nó là cấu hình, sửa được trong Settings.
  contextCeiling: 128000,
  // D-0054 mục 4: 60% trần, chừa chỗ cho system prompt + tool + file đọc giữa lượt
  // + câu trả lời. Đặt bằng đúng trần là vỡ lượt.
  windowRatio: 0.6,
  // Chạm ngưỡng này thì xoay session (D-0055 mục 4).
  compactRatio: 0.8,
  // Số tin gần nhất mang nguyên văn sang session mới.
  keepVerbatim: 40,
  // Bao lâu KHÔNG có event nào từ engine thì coi model đó là treo và xoay sang model
  // kế tiếp. Đo thời gian IM LẶNG chứ không phải tổng thời gian: một lượt dùng nhiều
  // tool có quyền chạy lâu. Đã dính thật: một model free treo 13 phút không nhả một
  // event nào.
  idleTimeoutMs: 120000,
  // Xoay session mỗi ngày kể cả chưa chạm ngưỡng.
  rotateDaily: true,
  brain: {
    enabled: true,
    host: '127.0.0.1',
    port: 8931,
    embedding: null, // { mode: 'local' | 'api', baseUrl, model, apiKey }
  },
};

const SETTINGS_PATH = path.join(DATA_DIR, 'settings.json');

function loadSettings() {
  try {
    const raw = JSON.parse(fs.readFileSync(SETTINGS_PATH, 'utf8'));
    return { ...DEFAULTS, ...raw, brain: { ...DEFAULTS.brain, ...(raw.brain || {}) } };
  } catch {
    return { ...DEFAULTS, brain: { ...DEFAULTS.brain } };
  }
}

function saveSettings(next) {
  fs.mkdirSync(DATA_DIR, { recursive: true });
  fs.writeFileSync(SETTINGS_PATH, JSON.stringify(next, null, 2), 'utf8');
  return next;
}

module.exports = {
  ROOT,
  DATA_DIR,
  RESOURCES_DIR,
  SETTINGS_PATH,
  DEFAULTS,
  resolveOpencode,
  loadSettings,
  saveSettings,
  appName,
  dbPath: () => path.join(DATA_DIR, 'alice.db'),
  workDir: () => path.join(DATA_DIR, 'workspace'),
  knowledgeDir: () => path.join(ROOT, 'knowledge'),
};
