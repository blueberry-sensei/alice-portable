'use strict';

const fs = require('node:fs');
const path = require('node:path');

const config = require('../config');

/**
 * Làm cho opencode thật sự PORTABLE và MỘT ALICE MỘT KEY.
 *
 * Mặc định opencode để dữ liệu ở home của máy theo chuẩn XDG:
 * `~/.local/share/opencode` (auth, account, DB phình tới GB) và `~/.config/opencode`.
 * App trỏ `XDG_DATA_HOME`/`XDG_CONFIG_HOME` vào thư mục của TỪNG Alice
 * (`alices/<id>/opencode/`) — mang thư mục app đi đâu là auth + session đi theo,
 * và mỗi Alice có auth RIÊNG, không lẫn với Alice khác.
 *
 * Không có đường "mượn key từ máy" — feedback khách chốt 2026-08-12: LUÔN phải
 * dán key mới, không lấy từ máy (D-0004).
 */

/** `baseDir` = thư mục dữ liệu của Alice (alices/<id>/). */
function portableDirs(baseDir) {
  const base = path.join(baseDir, 'opencode');
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

/** Env để spawn opencode sao cho mọi thứ nó ghi đều nằm trong thư mục của Alice. */
function portableEnv(baseDir) {
  const d = portableDirs(baseDir);
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

/** Đã có key chưa — chỉ trả tên provider, KHÔNG bao giờ trả giá trị key (D-0004). */
function authStatus(baseDir) {
  const { authFile } = portableDirs(baseDir);
  const out = { configured: false, providers: [], authFile };
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
function setApiKey(provider, key, baseDir) {
  const { authFile } = portableDirs(baseDir);
  fs.mkdirSync(path.dirname(authFile), { recursive: true });
  let data = {};
  try { data = JSON.parse(fs.readFileSync(authFile, 'utf8')); } catch { /* file mới */ }
  data[provider] = { type: 'api', key };
  fs.writeFileSync(authFile, JSON.stringify(data, null, 2), { encoding: 'utf8', mode: 0o600 });
  return authStatus(baseDir);
}

module.exports = {
  portableDirs, portableEnv, authStatus, setApiKey, seedConfigDir,
};
