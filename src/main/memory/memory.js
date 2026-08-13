'use strict';

const { randomUUID } = require('node:crypto');

/**
 * Tầng trí nhớ — chính sách cửa sổ, nén và xoay session.
 *
 * `D-0055` thay `D-0054` mục 4 ở đúng một chỗ: opencode NỐI TIẾP ĐƯỢC session, nên
 * không phải dán lại cửa sổ mỗi lượt. Nhưng bốn thứ còn lại giữ nguyên, vì chúng
 * không phụ thuộc vào điều đó:
 *
 *   1. Kho của app là source-of-truth (`store.js`).
 *   2. Session opencode là *cache*: mất thì dựng lại, không mất dữ liệu.
 *   3. Ngưỡng tính trên `tokens.input` ĐO ĐƯỢC, không phải ước lượng ký tự.
 *   4. Nén = **xoay sang session opencode mới có mồi**, không phải compact tại chỗ.
 *
 * Mồi (`seed`) = bản compact của hội thoại cũ + N tin gần nhất nguyên văn. Đây là
 * chỗ duy nhất trong app mà "quên" có thể xảy ra, nên nó có test riêng.
 */
const ONE_HOUR_MS = 3600 * 1000;
const SESSION_MAX_AGE_MS = 12 * ONE_HOUR_MS;
const SESSION_IDLE_MS = ONE_HOUR_MS;

class Memory {
  /**
   * @param {import('./store').Store} store
   * @param {object} settings  từ config.loadSettings()
   * @param {(messages: object[]) => Promise<string>} [summarize]
   *        Hàm nén — mặc định nén cơ học (không gọi model). Truyền hàm gọi model vào
   *        để có bản tóm tắt tốt hơn; ký chữ ký này để tầng chính sách test được mà
   *        không cần mạng.
   */
  constructor(store, settings, summarize = null) {
    this.store = store;
    this.settings = settings;
    this.summarize = summarize || defaultSummarize;
  }

  /** Trần cửa sổ thực dụng: 60% trần model (D-0054 mục 4). */
  get windowBudget() {
    return Math.floor(this.settings.contextCeiling * this.settings.windowRatio);
  }

  /** Chạm mốc này thì xoay. 80% của cửa sổ, không phải 80% của trần model. */
  get compactThreshold() {
    return Math.floor(this.windowBudget * this.settings.compactRatio);
  }

  /** `YYYY-MM-DD` theo giờ máy — đơn vị của "rotate mỗi ngày". */
  static today(now = new Date()) {
    const p = (n) => String(n).padStart(2, '0');
    return `${now.getFullYear()}-${p(now.getMonth() + 1)}-${p(now.getDate())}`;
  }

  /**
   * Hội thoại đang dùng. Tự tạo nếu chưa có, tự xoay nếu sang ngày mới.
   * Trả `{ conversation, seed }` — `seed` khác null nghĩa là session opencode mới
   * và người gọi PHẢI gửi mồi này trước tin của người dùng.
   */
  async ensureConversation(now = new Date()) {
    const day = Memory.today(now);
    const current = this.store.currentConversation();

    if (!current) {
      return { conversation: this._fresh(day, null, '', null, now), seed: null, reason: 'first-run' };
    }

    const staleReason = this._staleReason(current, now, day);
    const conversation = staleReason
      ? (await this._rotate(current, day, staleReason, null, now)).conversation
      : current;

    // Mồi LUÔN đi qua đĩa, kể cả khi vừa sinh ra ở dòng trên. Một đường duy nhất
    // thì không có nhánh nào quên tiêu thụ nó — đó chính là lỗi mà test
    // "chạm ngưỡng → xoay session" bắt được: xoay ở `afterTurn` sinh mồi, nhưng
    // lượt sau đi nhánh khác nên mồi rơi mất, và Alice quên mà vẫn trả lời trơn.
    const seed = this.store.takePendingSeed(conversation.id);
    return {
      conversation,
      seed,
      reason: seed ? (conversation.rotated_from ? 'rotated' : 'seeded') : null,
    };
  }

  /**
   * Vì sao phải xoay TRƯỚC khi chạy lượt này — rỗng = dùng tiếp session cũ.
   *
   * Ba trục, CỘNG DỒN (không thay nhau): hết ngày (`daily`), tràn context (kiểm ở
   * `afterTurn`, không nằm ở đây), và MỚI — tuổi + im lặng (`stale`). Trục `stale`
   * dùng AND, khác hai trục kia: một session 13 tiếng tuổi mà vẫn đang nhắn liên
   * tục KHÔNG bị xoay — tuổi một mình không phải tín hiệu đủ, phải có khoảng dừng
   * thật kèm theo (2026-08-13, tham khảo bài học đã trả giá ở
   * `kd-reserve/automation`: OR thuần quá nhạy, phải nới ngưỡng sau khi cắt ngang
   * cuộc đang nói dở — AND ngay từ đầu để khỏi lặp lại).
   */
  _staleReason(current, now, day) {
    if (this.settings.rotateDaily && current.day !== day) return 'daily';
    const ageMs = now.getTime() - current.created_at;
    if (ageMs <= SESSION_MAX_AGE_MS) return null;
    const lastTs = this.store.lastMessageTs(current.id);
    const idleMs = lastTs ? now.getTime() - lastTs : 0;
    if (idleMs > SESSION_IDLE_MS) return 'stale';
    return null;
  }

  /**
   * Gọi SAU mỗi lượt, với `tokens.input` mà engine vừa trả về. Nếu chạm ngưỡng thì
   * xoay ngay để lượt SAU bắt đầu ở session mới — không đợi tới lúc vỡ.
   */
  async afterTurn(conversation, tokensInput, now = new Date()) {
    if (!tokensInput || tokensInput < this.compactThreshold) return null;
    return this._rotate(conversation, Memory.today(now), 'threshold', tokensInput, now);
  }

  /** Mồi cho một session opencode mới: bản compact + N tin cuối nguyên văn. */
  buildSeed(summary, verbatim) {
    const lines = [];
    lines.push('[TIẾP NỐI HỘI THOẠI — không phải tin nhắn của người dùng]');
    lines.push('');
    lines.push('Đây là phần đầu cuộc trò chuyện đã bị nén lại để vừa cửa sổ ngữ cảnh.');
    lines.push('Coi như bạn đã trải qua nó. Đừng chào lại từ đầu, đừng nói là bạn vừa mất trí nhớ.');
    lines.push('');
    if (summary && summary.trim()) {
      lines.push('## Tóm tắt phần đã nén');
      lines.push(summary.trim());
      lines.push('');
    }
    if (verbatim.length) {
      lines.push(`## ${verbatim.length} tin gần nhất, nguyên văn`);
      for (const m of verbatim) {
        lines.push(`[${m.role === 'alice' ? 'Alice' : 'Bệ hạ'}]: ${m.text}`);
      }
      lines.push('');
    }
    lines.push('[HẾT PHẦN TIẾP NỐI — tin tiếp theo là tin thật của người dùng]');
    return lines.join('\n');
  }

  // ── nội bộ ───────────────────────────────────────────────────────────────

  _fresh(day, rotatedFrom, summary, pendingSeed = null, now = new Date()) {
    return this.store.createConversation({
      id: randomUUID(),
      day,
      rotatedFrom,
      summary,
      pendingSeed,
      createdAt: now.getTime(),
    });
  }

  async _rotate(current, day, reason, tokensInput = null, now = new Date()) {
    const all = this.store.recent(current.id, 600);
    const keep = this.settings.keepVerbatim;
    const older = all.slice(0, Math.max(0, all.length - keep));
    const verbatim = all.slice(-keep);

    // Nén phần CŨ thôi. Phần đuôi đã đi nguyên văn rồi, nén nữa là mất hai lần.
    const summary = older.length ? await this.summarize(older) : (current.summary || '');

    const seed = this.buildSeed(summary, verbatim);
    this.store.closeConversation(current.id, summary);
    const next = this._fresh(day, current.id, summary, seed, now);
    return {
      conversation: next,
      seed,
      reason,
      tokensInput,
      compactedCount: older.length,
      keptCount: verbatim.length,
    };
  }
}

/**
 * Nén cơ học — không gọi model.
 *
 * Dùng khi chưa cấu hình model nén, hoặc khi lượt nén bằng model lỗi. Thà có một bản
 * tóm tắt thô còn hơn xoay session với mồi rỗng: mồi rỗng chính là ca "quên từ lượt
 * thứ HAI mà vẫn trả lời trơn tru" mà `D-0054` cảnh báo.
 */
async function defaultSummarize(messages) {
  const lines = ['(Bản nén cơ học — chưa qua model.)'];
  const byRole = { human: 0, alice: 0 };
  for (const m of messages) byRole[m.role] = (byRole[m.role] || 0) + 1;
  lines.push(`Đã trao đổi ${messages.length} tin (${byRole.human || 0} của Bệ hạ, ${byRole.alice || 0} của Alice).`);
  lines.push('');
  lines.push('Các tin của Bệ hạ, rút gọn:');
  for (const m of messages) {
    if (m.role !== 'human') continue;
    const t = m.text.length > 200 ? `${m.text.slice(0, 200)}…` : m.text;
    lines.push(`- ${t.replace(/\s+/g, ' ')}`);
  }
  return lines.join('\n');
}

module.exports = { Memory, defaultSummarize };
