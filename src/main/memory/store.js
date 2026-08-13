'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { DatabaseSync } = require('node:sqlite');

/**
 * Kho hội thoại của Alice — SQLite, nguyên văn, không bao giờ xoá.
 *
 * `D-0055` mục 1: kho NÀY là source-of-truth, không phải DB của opencode. Session
 * opencode chỉ là cache; mất nó thì dựng lại được từ đây, mất kho này thì mất thật.
 *
 * Port từ `tools/_chat_store.py` của repo automation, giữ nguyên hai quyết định đã
 * trả giá ở đó:
 *   - FTS5 `remove_diacritics 2` → gõ "nha hang" tìm ra "nhà hàng".
 *   - FTS5 tách riêng, hỏng thì rơi về LIKE chứ không làm chết cả kho.
 * Thêm hai cột mà bản Python không cần: `tokens_input` (số đo cửa sổ thật, D-0055
 * mục 3) và `engine_session` (biết tin này thuộc session opencode nào).
 */

const SCHEMA = `
CREATE TABLE IF NOT EXISTS conversations (
    id             TEXT PRIMARY KEY,
    created_at     INTEGER NOT NULL,
    engine_session TEXT,
    day            TEXT NOT NULL,
    summary        TEXT NOT NULL DEFAULT '',
    rotated_from   TEXT,
    closed_at      INTEGER,
    -- Mồi tiếp nối đang chờ được gửi. Phải nằm trên ĐĨA chứ không phải trong RAM:
    -- lượt xoay session và lượt tiêu thụ mồi là hai lượt khác nhau, và app có thể
    -- bị tắt giữa hai lượt đó. Giữ trong biến là mất mồi — mất trí nhớ đúng lúc vừa
    -- nén xong, mà bên ngoài nhìn vẫn thấy Alice trả lời trơn tru.
    pending_seed   TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    conv_id        TEXT NOT NULL,
    ts             INTEGER NOT NULL,
    role           TEXT NOT NULL,
    text           TEXT NOT NULL DEFAULT '',
    tokens_input   INTEGER,
    engine_session TEXT,
    meta           TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conv_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS schedules (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    enabled    INTEGER NOT NULL DEFAULT 1,
    hour       INTEGER NOT NULL,
    minute     INTEGER NOT NULL,
    task       TEXT NOT NULL,
    last_run   TEXT,
    created_at INTEGER NOT NULL
);
`;

const FTS_SCHEMA = `
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    text,
    content = 'messages',
    content_rowid = 'id',
    tokenize = 'unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
END;
-- Xoá tin thì phải xoá bản ghi FTS tương ứng, nếu không search vẫn ra rác
-- (bảng FTS content=external không tự biết bản ghi bị xoá).
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES('delete', old.id, old.text);
END;
`;

// Trần số dòng ĐỌC LÊN, không phải trần cửa sổ. Để archive 200k tin không kéo cả
// bảng vào RAM. Lấy từ ĐUÔI nên vượt trần chỉ mất phần cũ.
const MAX_ROWS = 600;

class Store {
  constructor(dbFile) {
    fs.mkdirSync(path.dirname(dbFile), { recursive: true });
    this.db = new DatabaseSync(dbFile);
    this.db.exec('PRAGMA journal_mode = WAL');
    this.db.exec(SCHEMA);
    this._migrate();
    try {
      this.db.exec(FTS_SCHEMA);
      this.fts = true;
    } catch {
      this.fts = false; // bản SQLite không kèm FTS5 → /nhớ rơi về LIKE
    }
  }

  /**
   * Nâng cấp schema cho kho ĐÃ CÓ.
   *
   * `CREATE TABLE IF NOT EXISTS` không đụng vào bảng đang tồn tại, nên cột mới phải
   * thêm bằng ALTER. Kho của người dùng đã chạy từ bản trước — thêm cột mà quên
   * migrate là app chết ngay câu SELECT đầu tiên.
   */
  _migrate() {
    const cols = this.db.prepare('PRAGMA table_info(messages)').all().map((c) => c.name);
    if (!cols.includes('delivered')) {
      // `delivered = 0`: tin đã LƯU nhưng Alice CHƯA đọc — sinh ra khi có luật
      // "@alice mới trả lời". Mọi tin cũ mặc định là đã đọc (1), vì chúng đều đã
      // đi qua engine ở thời điểm được gửi.
      this.db.exec('ALTER TABLE messages ADD COLUMN delivered INTEGER NOT NULL DEFAULT 1');
    }
  }

  close() {
    this.db.close();
  }

  // ── conversation ─────────────────────────────────────────────────────────

  createConversation({
    id, day, engineSession = null, rotatedFrom = null, summary = '', pendingSeed = null,
    createdAt = null,
  }) {
    this.db.prepare(
      `INSERT INTO conversations (id, created_at, engine_session, day, summary, rotated_from, pending_seed)
       VALUES (?, ?, ?, ?, ?, ?, ?)`
    ).run(id, createdAt || Date.now(), engineSession, day, summary, rotatedFrom, pendingSeed);
    return this.getConversation(id);
  }

  /**
   * Lấy mồi ra và xoá luôn — đọc-rồi-xoá trong một bước.
   *
   * Không tách "đọc" khỏi "xoá": tách ra thì một lượt lỗi giữa chừng sẽ để lại mồi
   * và lượt sau nạp mồi lần thứ hai, khiến Alice tưởng vừa nén hai lần.
   */
  takePendingSeed(convId) {
    const row = this.db.prepare('SELECT pending_seed FROM conversations WHERE id = ?').get(convId);
    const seed = row && row.pending_seed ? row.pending_seed : null;
    if (seed) {
      this.db.prepare('UPDATE conversations SET pending_seed = NULL WHERE id = ?').run(convId);
    }
    return seed;
  }

  getConversation(id) {
    return this.db.prepare('SELECT * FROM conversations WHERE id = ?').get(id) || null;
  }

  /** Hội thoại đang mở gần nhất (chưa `closed_at`). */
  currentConversation() {
    return this.db.prepare(
      'SELECT * FROM conversations WHERE closed_at IS NULL ORDER BY created_at DESC LIMIT 1'
    ).get() || null;
  }

  setEngineSession(convId, sessionId) {
    this.db.prepare('UPDATE conversations SET engine_session = ? WHERE id = ?').run(sessionId, convId);
  }

  closeConversation(convId, summary) {
    this.db.prepare('UPDATE conversations SET closed_at = ?, summary = ? WHERE id = ?')
      .run(Date.now(), summary || '', convId);
  }

  /**
   * Xoá CUỘC TRÒ CHUYỆN hiện tại: hết tin nhắn (kèm bản FTS qua trigger), đóng
   * hội thoại và vứt mồi tiếp nối còn treo. Hội thoại kế tiếp tự tạo khi có
   * lượt mới — Alice bắt đầu lại từ đầu, không còn nhớ gì của cuộc cũ.
   */
  clearConversation(convId) {
    this.db.prepare('DELETE FROM messages WHERE conv_id = ?').run(convId);
    this.db.prepare(
      `UPDATE conversations SET closed_at = ?, summary = '', pending_seed = NULL WHERE id = ?`
    ).run(Date.now(), convId);
  }

  // ── lịch hẹn (scheduler) ────────────────────────────────────────────────

  listSchedules() {
    return this.db.prepare('SELECT * FROM schedules ORDER BY hour, minute, id').all();
  }

  addSchedule({ hour, minute, task }) {
    const res = this.db.prepare(
      'INSERT INTO schedules (enabled, hour, minute, task, created_at) VALUES (1, ?, ?, ?, ?)'
    ).run(hour, minute, task, Date.now());
    return this.getSchedule(Number(res.lastInsertRowid));
  }

  getSchedule(id) {
    return this.db.prepare('SELECT * FROM schedules WHERE id = ?').get(id) || null;
  }

  updateSchedule(id, patch) {
    const cur = this.getSchedule(id);
    if (!cur) return null;
    const next = { ...cur, ...patch };
    this.db.prepare(
      'UPDATE schedules SET enabled = ?, hour = ?, minute = ?, task = ? WHERE id = ?'
    ).run(next.enabled ? 1 : 0, next.hour, next.minute, next.task, id);
    return this.getSchedule(id);
  }

  removeSchedule(id) {
    this.db.prepare('DELETE FROM schedules WHERE id = ?').run(id);
  }

  markScheduleRun(id, day) {
    this.db.prepare('UPDATE schedules SET last_run = ? WHERE id = ?').run(day, id);
  }

  // ── ghi ──────────────────────────────────────────────────────────────────

  /**
   * Ghi một tin. `text` phải ĐÃ được che secret trước khi tới đây — store không biết
   * gì về credential, và đặt bộ che ở hai chỗ là cách chắc chắn để hai chỗ trôi khỏi
   * nhau (cùng lý do `_secrets.py` ở repo automation, D-0004).
   */
  add({ convId, role, text, tokensInput = null, engineSession = null, meta = null, ts = null, delivered = true }) {
    if (!text) return null;
    const res = this.db.prepare(
      `INSERT INTO messages (conv_id, ts, role, text, tokens_input, engine_session, meta, delivered)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?)`
    ).run(convId, ts ?? Date.now(), role, text, tokensInput, engineSession,
      meta ? JSON.stringify(meta) : null, delivered ? 1 : 0);
    return Number(res.lastInsertRowid);
  }

  /** Tin đã lưu mà Alice CHƯA đọc, cũ → mới. */
  undelivered(convId) {
    return this.db.prepare(
      'SELECT * FROM messages WHERE conv_id = ? AND delivered = 0 ORDER BY id'
    ).all(convId);
  }

  /** Đánh dấu đã đưa vào ngữ cảnh của Alice — gọi SAU khi lượt chạy xong. */
  markDelivered(ids) {
    if (!ids || !ids.length) return;
    const q = this.db.prepare('UPDATE messages SET delivered = 1 WHERE id = ?');
    for (const id of ids) q.run(id);
  }

  /** Tin có id LỚN HƠN `sinceId`, cũ → mới — để trang web lấy phần còn thiếu. */
  since(convId, sinceId, limit = 200) {
    return this.db.prepare(
      'SELECT * FROM messages WHERE conv_id = ? AND id > ? ORDER BY id LIMIT ?'
    ).all(convId, Number(sinceId) || 0, Math.min(limit, MAX_ROWS));
  }

  // ── đọc ──────────────────────────────────────────────────────────────────

  count(convId = null) {
    const row = convId
      ? this.db.prepare('SELECT COUNT(*) c FROM messages WHERE conv_id = ?').get(convId)
      : this.db.prepare('SELECT COUNT(*) c FROM messages').get();
    return Number(row.c);
  }

  /**
   * `limit` tin MỚI NHẤT, trả về thứ tự CŨ → MỚI.
   *
   * `ORDER BY id DESC` rồi đảo lại, KHÔNG phải `ASC LIMIT`: cái sau cắt mất phần
   * đuôi, tức là cắt đúng phần đang được hỏi.
   */
  recent(convId, limit = 50) {
    const rows = this.db.prepare(
      'SELECT * FROM messages WHERE conv_id = ? ORDER BY id DESC LIMIT ?'
    ).all(convId, Math.min(limit, MAX_ROWS));
    return rows.reverse();
  }

  /** `ts` của tin GẦN NHẤT trong hội thoại — dùng để tính "đã im lặng bao lâu" cho
   * rotation. `null` nếu hội thoại chưa có tin nào. */
  lastMessageTs(convId) {
    const row = this.db.prepare(
      'SELECT ts FROM messages WHERE conv_id = ? ORDER BY id DESC LIMIT 1'
    ).get(convId);
    return row ? row.ts : null;
  }

  /** `tokens_input` đo được ở lượt gần nhất của hội thoại — độ đầy cửa sổ THẬT. */
  lastTokens(convId) {
    const row = this.db.prepare(
      `SELECT tokens_input FROM messages
       WHERE conv_id = ? AND tokens_input IS NOT NULL
       ORDER BY id DESC LIMIT 1`
    ).get(convId);
    return row ? Number(row.tokens_input) : 0;
  }

  /**
   * Tra toàn văn — đường trả lời "tuần trước Bệ hạ nói gì".
   *
   * FTS5 có cú pháp riêng và chuỗi người dùng gõ có thể làm vỡ parser; bọc từng từ
   * trong nháy kép là cách rẻ nhất để vừa an toàn vừa giữ nghĩa "tìm mọi từ này".
   */
  search(query, limit = 8) {
    const terms = String(query).replace(/"/g, ' ').split(/\s+/).filter(Boolean);
    if (!terms.length) return [];
    if (this.fts) {
      try {
        return this.db.prepare(
          `SELECT m.* FROM messages_fts f JOIN messages m ON m.id = f.rowid
           WHERE messages_fts MATCH ? ORDER BY m.id DESC LIMIT ?`
        ).all(terms.map((t) => `"${t}"`).join(' '), limit);
      } catch {
        /* chuỗi vẫn làm vỡ parser → rơi về LIKE */
      }
    }
    const where = terms.map(() => 'text LIKE ?').join(' AND ');
    return this.db.prepare(
      `SELECT * FROM messages WHERE ${where} ORDER BY id DESC LIMIT ?`
    ).all(...terms.map((t) => `%${t}%`), limit);
  }

  getKV(k, fallback = null) {
    const row = this.db.prepare('SELECT v FROM kv WHERE k = ?').get(k);
    return row ? row.v : fallback;
  }

  setKV(k, v) {
    this.db.prepare('INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v')
      .run(k, String(v));
  }
}

module.exports = { Store, MAX_ROWS };
