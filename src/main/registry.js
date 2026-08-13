'use strict';

/**
 * Registry — danh sách các Alice trong MỘT app cài.
 *
 * Bệ hạ chốt (2026-08-13): một app duy nhất, bên trong có NHIỀU Alice. Mỗi Alice
 * có: tên, chìa khoá riêng, chat db riêng, brain riêng — sống trong thư mục
 * `alice-data/alices/<id>/`. App chỉ giữ danh sách + Alice đang mở.
 *
 * Dữ liệu cũ (bản < 0.2.0, mọi thứ nằm thẳng trong alice-data/) được MIGRATE một
 * lần thành Alice đầu tiên khi app nâng cấp — người dùng không mất gì.
 *
 * `paths` cho phép test chạy trên thư mục tạm mà không đụng máy thật.
 */

const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

const config = require('./config');
const auth = require('./engine/auth');

function resolve(paths = {}) {
  return {
    registryPath: paths.registryPath || path.join(config.DATA_DIR, 'alices.json'),
    dataDir: paths.dataDir || config.DATA_DIR,
    alicesDir: paths.alicesDir || config.alicesDir(),
  };
}

function aliceDirOf(p, id) {
  return path.join(p.alicesDir, id);
}

/**
 * Thư mục THẬT của một Alice.
 *
 * Mặc định `alices/<id>/`, nhưng người dùng chọn được thư mục riêng lúc tạo (vd
 * một thư mục dự án, hoặc ổ khác cho đỡ chật). Khi đó `alice.dir` là đường dẫn
 * tuyệt đối và mọi thứ của Alice — chat.db, brain, chìa khoá, workspace — nằm
 * trong đó.
 */
function dirOf(alice, paths = {}) {
  if (alice && alice.dir) return alice.dir;
  return aliceDirOf(resolve(paths), alice.id);
}

function load(paths = {}) {
  const p = resolve(paths);
  try {
    const raw = JSON.parse(fs.readFileSync(p.registryPath, 'utf8'));
    return { active: raw.active || null, alices: Array.isArray(raw.alices) ? raw.alices : [] };
  } catch {
    return { active: null, alices: [] };
  }
}

function save(state, paths = {}) {
  const p = resolve(paths);
  fs.mkdirSync(p.dataDir, { recursive: true });
  fs.writeFileSync(p.registryPath, JSON.stringify(state, null, 2), 'utf8');
}

/**
 * Tạo Alice mới. Key bắt buộc do Bệ hạ đặt từ đầu ("User tạo thì bỏ key") — mỗi
 * Alice dùng chìa khoá RIÊNG của nó, không dùng chung với ai. Model cũng riêng
 * (chọn lúc tạo, đổi được sau) — không lẫn với Alice khác.
 * Trả { state, alice }.
 */
function create({ name, key, model = null, dir = null }, paths = {}) {
  const p = resolve(paths);
  const state = load(paths);
  const id = crypto.randomUUID();

  // Thư mục do người dùng chọn: giữ nguyên đường dẫn họ chỉ, nhưng đặt Alice vào
  // một thư mục con mang TÊN của nó — chọn "D:\Work" mà đổ thẳng chat.db vào đó là
  // rải rác file vào thư mục đang có việc khác.
  const custom = String(dir || '').trim();
  const home = custom
    ? path.resolve(custom, slug(name) || id.slice(0, 8))
    : aliceDirOf(p, id);

  const alice = {
    id,
    name: String(name || '').trim() || 'Alice',
    provider: 'opencode',
    model: model || null,
    created_at: Date.now(),
  };
  if (custom) alice.dir = home;
  state.alices.push(alice);
  if (!state.active) state.active = id;
  save(state, paths);

  fs.mkdirSync(home, { recursive: true });
  fs.mkdirSync(path.join(home, 'brain'), { recursive: true });
  if (key && String(key).trim()) {
    auth.setApiKey('opencode', String(key).trim(), home);
  }
  return { state, alice };
}

/** Tên thư mục an toàn từ tên Alice: bỏ dấu, bỏ ký tự Windows không nhận. */
function slug(name) {
  return String(name || '')
    .normalize('NFD').replace(/[\u0300-\u036f]/g, '')
    .replace(/đ/g, 'd').replace(/Đ/g, 'D')
    .replace(/[^a-zA-Z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 40);
}

/** Cập nhật một số trường của Alice (vd model). Trả state mới. */
function update(id, patch, paths = {}) {
  const state = load(paths);
  const alice = state.alices.find((a) => a.id === id);
  if (!alice) return { state, updated: false };
  if (patch.name !== undefined) alice.name = String(patch.name).trim() || alice.name;
  if (patch.model !== undefined) alice.model = patch.model || null;
  save(state, paths);
  return { state, updated: true };
}

/**
 * Xoá Alice: khỏi danh sách + xoá cả thư mục dữ liệu của nó (mất vĩnh viễn —
 * gọi nơi khác phải hỏi lại người dùng trước). Chỉ xoá thư mục nằm trong
 * `alices/` — guard đường dẫn, không bao giờ xoá lung tung.
 */
function remove(id, paths = {}) {
  const p = resolve(paths);
  const state = load(paths);
  const idx = state.alices.findIndex((a) => a.id === id);
  if (idx < 0) return { state, removed: false };
  const alice = state.alices[idx];
  state.alices.splice(idx, 1);
  if (state.active === id) state.active = state.alices.length ? state.alices[0].id : null;
  save(state, paths);

  // Thư mục do CHÍNH APP tạo (`alices/<id>/`) thì xoá. Thư mục do NGƯỜI DÙNG chỉ
  // thì không đụng vào: nó nằm ngoài vùng app quản, có thể là thư mục dự án đang
  // có việc khác, và xoá nhầm ở đó là mất dữ liệu không lấy lại được.
  const auto = aliceDirOf(p, id);
  const home = alice.dir || auto;
  let keptDir = null;
  if (!alice.dir && path.dirname(auto) === path.resolve(p.alicesDir) && fs.existsSync(auto)) {
    // `maxRetries`: trên Windows, chat.db/lancedb vừa đóng vẫn còn handle treo vài
    // trăm ms — xoá phát đầu ăn EBUSY và trước đây nó ném thẳng ra làm đơ UI.
    fs.rmSync(auto, { recursive: true, force: true, maxRetries: 8, retryDelay: 150 });
  } else if (alice.dir) {
    keptDir = home;
  }
  return { state, removed: true, keptDir };
}

/**
 * Lần đầu chạy bản đa-Alice trên dữ liệu CŨ (alice.db nằm thẳng trong
 * alice-data/): gom hết vào Alice đầu tiên — chat db, brain, auth opencode.
 * Trả state mới, hoặc null nếu không có gì để migrate (máy mới tinh).
 */
function migrateLegacy({ name }, paths = {}) {
  const p = resolve(paths);
  const state = load(paths);
  if (state.alices.length) return null;

  const legacyDb = path.join(p.dataDir, 'alice.db');
  if (!fs.existsSync(legacyDb)) return null;

  const id = crypto.randomUUID();
  const dir = aliceDirOf(p, id);
  fs.mkdirSync(dir, { recursive: true });

  // Chat db — copy cả -wal/-shm nếu còn (app có thể bị tắt giữa chừng).
  for (const suffix of ['', '-wal', '-shm']) {
    const f = legacyDb + suffix;
    if (fs.existsSync(f)) fs.copyFileSync(f, path.join(dir, 'chat.db' + suffix));
  }

  // Brain (sag.db + lance) — nếu có.
  const legacyBrain = path.join(p.dataDir, 'brain');
  if (fs.existsSync(path.join(legacyBrain, 'sag.db'))) {
    fs.mkdirSync(path.join(dir, 'brain'), { recursive: true });
    fs.cpSync(legacyBrain, path.join(dir, 'brain'), { recursive: true });
  }

  // Auth + session opencode.
  const legacyOc = path.join(p.dataDir, 'opencode');
  if (fs.existsSync(legacyOc)) {
    fs.cpSync(legacyOc, path.join(dir, 'opencode'), { recursive: true });
  }

  // Avatar cũ (ảnh chung của bản một-Alice).
  for (const ext of ['.png', '.jpg', '.jpeg', '.webp', '.gif']) {
    const f = path.join(p.dataDir, `avatar${ext}`);
    if (fs.existsSync(f)) fs.copyFileSync(f, path.join(dir, `avatar${ext}`));
  }

  const alice = {
    id,
    name: name || 'Alice',
    provider: 'opencode',
    created_at: Date.now(),
    migrated: true,
  };
  state.alices.push(alice);
  state.active = id;
  save(state, paths);
  return state;
}

module.exports = { load, save, create, update, remove, migrateLegacy, resolve, aliceDirOf, dirOf, slug };
