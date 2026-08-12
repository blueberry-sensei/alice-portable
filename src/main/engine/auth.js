'use strict';

const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');

const config = require('../config');

/**
 * Làm cho opencode thật sự PORTABLE.
 *
 * Mặc định opencode để dữ liệu ở home của máy theo chuẩn XDG:
 *   - `~/.local/share/opencode` — `auth.json`, `account.json`, và `opencode.db`
 *     (trên máy này DB đó đã phình tới **3,4 GB**)
 *   - `~/.config/opencode` — cấu hình
 *
 * Nghĩa là mang thư mục app sang máy khác thì mất sạch auth và session. Nên app trỏ
 * `XDG_DATA_HOME`/`XDG_CONFIG_HOME` vào chính thư mục dữ liệu của mình.
 *
 * Đổi lại: lần đầu chạy sẽ KHÔNG có API key. Không im lặng mượn key của máy — nhân
 * bản credential sau lưng người dùng là việc phải hỏi, kể cả khi tiện. UI có ô nhập
 * key và một nút "mượn key từ máy này" bấm thì mới chạy.
 */

function portableDirs() {
  const base = path.join(config.DATA_DIR, 'opencode');
  return {
    base,
    data: path.join(base, 'data'),
    configDir: path.join(base, 'config'),
    authFile: path.join(base, 'data', 'opencode', 'auth.json'),
  };
}

/**
 * Mồi sẵn thư mục config cho lần chạy đầu.
 *
 * Đo được trong phiên build: gặp `XDG_CONFIG_HOME` trống, opencode **tự chạy npm
 * install** `@opencode-ai/plugin` vào đó. Hệ quả: lượt chat đầu tiên của người dùng
 * treo vài phút không rõ lý do, và trên máy không có mạng thì hỏng hẳn — một app
 * "portable" mà cần npm lúc chạy thì không portable.
 *
 * Nên bản đóng gói mang sẵn `runtime/opencode-config/`, và ở lần chạy đầu app chép
 * nó vào chỗ opencode sẽ tìm.
 */
function seedConfigDir(configDir) {
  const seed = path.join(config.RESOURCES_DIR, 'opencode-config');
  const target = path.join(configDir, 'opencode');
  if (fs.existsSync(target) || !fs.existsSync(seed)) return false;
  fs.cpSync(seed, target, { recursive: true });
  return true;
}

/** Env để spawn opencode sao cho mọi thứ nó ghi đều nằm cạnh app. */
function portableEnv() {
  const d = portableDirs();
  fs.mkdirSync(path.join(d.data, 'opencode'), { recursive: true });
  fs.mkdirSync(d.configDir, { recursive: true });
  seedConfigDir(d.configDir);
  return {
    XDG_DATA_HOME: d.data,
    XDG_CONFIG_HOME: d.configDir,
    XDG_CACHE_HOME: path.join(d.base, 'cache'),
    XDG_STATE_HOME: path.join(d.base, 'state'),
  };
}

function hostAuthFile() {
  return path.join(os.homedir(), '.local', 'share', 'opencode', 'auth.json');
}

/** Đã có key chưa — chỉ trả tên provider, KHÔNG bao giờ trả giá trị key (D-0004). */
function authStatus() {
  const { authFile } = portableDirs();
  const out = { configured: false, providers: [], authFile, hostAvailable: fs.existsSync(hostAuthFile()) };
  try {
    const data = JSON.parse(fs.readFileSync(authFile, 'utf8'));
    out.providers = Object.keys(data);
    out.configured = out.providers.length > 0;
  } catch { /* chưa có file — đúng ở lần chạy đầu */ }
  return out;
}

/**
 * Ghi API key. Giá trị đi thẳng từ ô nhập vào file, KHÔNG qua shell, không qua env,
 * không vào log — `D-0004`/`M-0003`: secret không được xuất hiện trong bất kỳ lệnh nào.
 */
function setApiKey(provider, key) {
  const { authFile } = portableDirs();
  fs.mkdirSync(path.dirname(authFile), { recursive: true });
  let data = {};
  try { data = JSON.parse(fs.readFileSync(authFile, 'utf8')); } catch { /* file mới */ }
  data[provider] = { type: 'api', key };
  fs.writeFileSync(authFile, JSON.stringify(data, null, 2), { encoding: 'utf8', mode: 0o600 });
  return authStatus();
}

/** Copy auth của máy sang thư mục portable. Chỉ chạy khi người dùng bấm nút. */
function importFromHost() {
  const src = hostAuthFile();
  if (!fs.existsSync(src)) throw new Error(`Máy này chưa có ${src} — chạy "opencode auth login" trước.`);
  const { authFile } = portableDirs();
  fs.mkdirSync(path.dirname(authFile), { recursive: true });
  fs.copyFileSync(src, authFile);
  fs.chmodSync(authFile, 0o600);
  return authStatus();
}

module.exports = {
  portableDirs, portableEnv, authStatus, setApiKey, importFromHost, hostAuthFile, seedConfigDir,
};
