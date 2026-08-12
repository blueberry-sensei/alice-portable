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
 * Alice dùng chìa khoá RIÊNG của nó, không dùng chung với ai.
 * Trả { state, alice }.
 */
function create({ name, key }, paths = {}) {
  const p = resolve(paths);
  const state = load(paths);
  const id = crypto.randomUUID();
  const alice = {
    id,
    name: String(name || '').trim() || 'Alice',
    provider: 'opencode',
    created_at: Date.now(),
  };
  state.alices.push(alice);
  if (!state.active) state.active = id;
  save(state, paths);

  const dir = aliceDirOf(p, id);
  fs.mkdirSync(dir, { recursive: true });
  fs.mkdirSync(path.join(dir, 'brain'), { recursive: true });
  if (key && String(key).trim()) {
    auth.setApiKey('opencode', String(key).trim(), dir);
  }
  return { state, alice };
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
  state.alices.splice(idx, 1);
  if (state.active === id) state.active = state.alices.length ? state.alices[0].id : null;
  save(state, paths);

  const dir = aliceDirOf(p, id);
  if (path.dirname(dir) === path.resolve(p.alicesDir) && fs.existsSync(dir)) {
    fs.rmSync(dir, { recursive: true, force: true });
  }
  return { state, removed: true };
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

module.exports = { load, save, create, remove, migrateLegacy, resolve, aliceDirOf };
