'use strict';

/**
 * Lịch hẹn — Alice tự chạy một task vào giờ đã đặt, mỗi ngày một lần.
 *
 * Luật chạy:
 *   - App phải ĐANG MỞ (engine nằm trong app). Cửa sổ đóng chỉ là ẩn, nên lịch
 *     vẫn chạy khi cửa sổ bị thu nhỏ — đúng ý "hide khi bấm close".
 *   - Mỗi lần check (20 giây): tìm lịch đang bật, đúng giờ:phút, và hôm nay
 *     CHƯA chạy (so `last_run` với ngày hôm nay). Trúng thì chạy task như một
 *     tin nhắn thật của người dùng, kết quả lưu vào cuộc trò chuyện.
 *   - Chạy thành công mới ghi `last_run`; hỏng (hoặc đang bận) thì lần check sau
 *     thử lại — trong cùng phút, 20 giây là kịp.
 */

const DAY_RE = /^\d{4}-\d{2}-\d{2}$/;

function today(now = new Date()) {
  const p = (n) => String(n).padStart(2, '0');
  return `${now.getFullYear()}-${p(now.getMonth() + 1)}-${p(now.getDate())}`;
}

/**
 * Lịch có phải lúc phải chạy ngay bây giờ không — hàm thuần để test được.
 * `sched` là một dòng từ bảng schedules; `lastRun` so theo NGÀY.
 * `weekday`: 0=CN, 1=T2 … 6=T7 theo `Date.getDay()`. NULL = mọi ngày (lịch cũ).
 */
function isDue(sched, now, lastRunDay = null) {
  if (!sched || !sched.enabled) return false;
  if (sched.hour !== now.getHours() || sched.minute !== now.getMinutes()) return false;
  if (typeof sched.weekday === 'number' && sched.weekday !== now.getDay()) return false;
  if (lastRunDay === today(now)) return false;
  return true;
}

class Scheduler {
  /**
   * @param {object} deps
   * @param {import('./memory/store').Store} deps.store
   * @param {function(string): Promise}      deps.runTurn  chạy task như một lượt chat
   * @param {object}                         deps.log      logger (src/main/log.js)
   */
  constructor({ store, runTurn, log }) {
    this.store = store;
    this.runTurn = runTurn;
    this.log = log;
    this.timer = null;
    this.running = false;
  }

  start(intervalMs = 20000) {
    if (this.timer) return;
    this.timer = setInterval(() => this.check(), intervalMs);
    // Check NGAY một lần khi app mở: mở đúng giờ lịch thì không phải đợi 20 giây.
    this.check();
  }

  stop() {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }

  async check() {
    if (this.running) return; // một lượt chạy lịch còn dang dở thì không mở lượt khác
    this.running = true;
    try {
      const now = new Date();
      const todayStr = today(now);
      for (const sched of this.store.listSchedules()) {
        if (!isDue(sched, now, sched.last_run)) continue;
        this.log.info(`schedule #${sched.id} due at ${String(sched.hour).padStart(2, '0')}:${String(sched.minute).padStart(2, '0')} — running task`);
        try {
          await this.runTurn(`[Lịch hẹn tự động] ${sched.task}`);
          this.store.markScheduleRun(sched.id, todayStr);
          this.log.info(`schedule #${sched.id} done`);
        } catch (err) {
          // Không ghi last_run → lần check sau trong cùng phút thử lại.
          this.log.error(`schedule #${sched.id} failed: ${err.message}`);
        }
      }
    } finally {
      this.running = false;
    }
  }
}

module.exports = { Scheduler, isDue, today };
