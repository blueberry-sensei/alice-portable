'use strict';

const fs = require('node:fs');
const path = require('node:path');

const config = require('./config');

/**
 * Ảnh của Alice — đổi được, và ảnh riêng của người dùng nằm ngoài bộ cài.
 *
 * Hai chỗ, thứ tự ưu tiên:
 *   1. `alice-data/avatar.<ext>`  — ảnh người dùng chọn. Nằm cạnh app nên đi theo
 *      bản portable, và **không** bị ghi đè khi cập nhật.
 *   2. `src/renderer/assets/img/alice-default.png` — ảnh mặc định trong bộ cài.
 *
 * Trả về **data URI** chứ không trả đường dẫn: renderer chạy dưới CSP
 * `img-src 'self' data:`, nên một file nằm ngoài thư mục app không load được bằng
 * `file://`. Đọc rồi nhúng thẳng vừa đúng CSP vừa khỏi phải mở thêm quyền.
 */

const MIME = {
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.webp': 'image/webp',
  '.gif': 'image/gif',
};

const ALLOWED = Object.keys(MIME);

// Ảnh nhét vào DOM dưới dạng data URI thì nó nằm luôn trong bộ nhớ của renderer.
// Một file 50MB sẽ làm app ì mà người dùng không hiểu vì sao — chặn sớm và nói rõ.
const MAX_BYTES = 8 * 1024 * 1024;

function customPath() {
  for (const ext of ALLOWED) {
    const p = path.join(config.DATA_DIR, `avatar${ext}`);
    if (fs.existsSync(p)) return p;
  }
  return null;
}

function defaultPath() {
  return path.join(__dirname, '..', 'renderer', 'assets', 'img', 'alice-default.png');
}

function toDataUri(file) {
  const ext = path.extname(file).toLowerCase();
  const mime = MIME[ext];
  if (!mime) return null;
  try {
    return `data:${mime};base64,${fs.readFileSync(file).toString('base64')}`;
  } catch {
    return null;
  }
}

/** Ảnh đang dùng, dạng data URI. `null` nếu không có ảnh nào đọc được. */
function current() {
  const custom = customPath();
  if (custom) {
    const uri = toDataUri(custom);
    if (uri) return uri;
  }
  return toDataUri(defaultPath());
}

/**
 * Đặt ảnh mới từ một file người dùng chọn.
 * Copy vào `alice-data/` chứ không giữ đường dẫn gốc: người dùng xoá hay đổi tên
 * file gốc thì avatar biến mất, mà họ sẽ không hiểu vì sao.
 */
function set(sourceFile) {
  const ext = path.extname(sourceFile).toLowerCase();
  if (!ALLOWED.includes(ext)) {
    throw new Error(`Chỉ nhận ${ALLOWED.join(', ')} — file này là ${ext || 'không rõ định dạng'}.`);
  }
  const stat = fs.statSync(sourceFile);
  if (stat.size > MAX_BYTES) {
    throw new Error(`Ảnh ${(stat.size / 1024 / 1024).toFixed(1)} MB, to quá — chọn ảnh dưới 8 MB nhé.`);
  }

  fs.mkdirSync(config.DATA_DIR, { recursive: true });
  // Dọn ảnh cũ trước: để lại `avatar.jpg` khi vừa ghi `avatar.png` thì thứ tự tìm
  // kiếm sẽ quyết định ảnh nào thắng, và kết quả trông như "đổi ảnh không ăn".
  for (const e of ALLOWED) {
    const old = path.join(config.DATA_DIR, `avatar${e}`);
    if (fs.existsSync(old)) fs.unlinkSync(old);
  }
  fs.copyFileSync(sourceFile, path.join(config.DATA_DIR, `avatar${ext}`));
  return current();
}

/** Về lại ảnh mặc định. */
function reset() {
  for (const e of ALLOWED) {
    const p = path.join(config.DATA_DIR, `avatar${e}`);
    if (fs.existsSync(p)) fs.unlinkSync(p);
  }
  return current();
}

module.exports = { current, set, reset, ALLOWED, isCustom: () => Boolean(customPath()) };
