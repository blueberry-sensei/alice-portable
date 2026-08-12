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
    try {
      this.db.exec(FTS_SCHEMA);
      this.fts = true;
    } catch {
      this.fts = false; // bản SQLite không kèm FTS5 → /nhớ rơi về LIKE
    }
  }

  close() {
    this.db.close();
  }

  // ── conversation ─────────────────────────────────────────────────────────

  createConversation({ id, day, engineSession = null, rotatedFrom = null, summary = '', pendingSeed = null }) {
    this.db.prepare(
      `INSERT INTO conversations (id, created_at, engine_session, day, summary, rotated_from, pending_seed)
       VALUES (?, ?, ?, ?, ?, ?, ?)`
    ).run(id, Date.now(), engineSession, day, summary, rotatedFrom, pendingSeed);
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

  // ── ghi ──────────────────────────────────────────────────────────────────

  /**
   * Ghi một tin. `text` phải ĐÃ được che secret trước khi tới đây — store không biết
   * gì về credential, và đặt bộ che ở hai chỗ là cách chắc chắn để hai chỗ trôi khỏi
   * nhau (cùng lý do `_secrets.py` ở repo automation, D-0004).
   */
  add({ convId, role, text, tokensInput = null, engineSession = null, meta = null, ts = null }) {
    if (!text) return null;
    const res = this.db.prepare(
      `INSERT INTO messages (conv_id, ts, role, text, tokens_input, engine_session, meta)
       VALUES (?, ?, ?, ?, ?, ?, ?)`
    ).run(convId, ts ?? Date.now(), role, text, tokensInput, engineSession,
      meta ? JSON.stringify(meta) : null);
    return Number(res.lastInsertRowid);
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
