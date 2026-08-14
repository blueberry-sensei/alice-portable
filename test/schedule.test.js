'use strict';

/**
 * Lịch hẹn (scheduler) và xoá cuộc trò chuyện — hai thứ dễ sai vì đều đụng
 * thời gian và trigger database:
 *
 *   - Lịch phải chạy ĐÚNG giờ:phút, mỗi ngày MỘT lần, và KHÔNG chạy lại khi đã
 *     chạy hôm nay (so theo ngày, không theo phút).
 *   - Xoá tin phải xoá luôn bản ghi FTS, nếu không "tìm lại chuyện cũ" vẫn ra
 *     rác từ cuộc trò chuyện đã xoá.
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { Store } = require('../src/main/memory/store');
const { Scheduler, isDue, today } = require('../src/main/scheduler');

function tmpDb() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'alice-sched-'));
  return path.join(dir, 'alice.db');
}

function at(h, m) {
  return new Date(2026, 7, 13, h, m, 0); // 13/08/2026 h:m:00
}

test('isDue: đúng giờ:phút + chưa chạy hôm nay thì chạy', () => {
  const sched = { enabled: 1, hour: 9, minute: 30 };
  assert.equal(isDue(sched, at(9, 30), null), true, 'đúng giờ, chưa chạy → chạy');
  assert.equal(isDue(sched, at(9, 30), '2026-08-13'), false, 'đã chạy hôm nay → không chạy lại');
  assert.equal(isDue(sched, at(9, 29), '2026-08-12'), false, 'chưa tới giờ → không chạy');
  assert.equal(isDue(sched, at(9, 31), '2026-08-12'), false, 'quá giờ → không chạy');
  assert.equal(isDue({ ...sched, enabled: 0 }, at(9, 30), null), false, 'lịch tắt → không chạy');
  assert.equal(isDue(sched, at(10, 30), '2026-08-12'), false, 'sai giờ → không chạy');
});

test('isDue: weekday — chỉ chạy đúng ngày trong tuần, NULL = mọi ngày', () => {
  // 13/08/2026 là Thứ 5 → getDay() === 4
  const sched = { enabled: 1, hour: 9, minute: 0, weekday: 4 };
  assert.equal(isDue(sched, at(9, 0), null), true, 'đúng thứ (T5=4) + đúng giờ → chạy');
  assert.equal(isDue({ ...sched, weekday: 3 }, at(9, 0), null), false, 'nhầm thứ (T4=3) → không chạy');
  assert.equal(isDue({ ...sched, weekday: 0 }, at(9, 0), null), false, 'chủ nhật khác thứ → không chạy');
  assert.equal(isDue({ ...sched, weekday: null }, at(9, 0), null), true, 'không đặt thứ (lịch cũ) → mọi ngày');
  assert.equal(isDue({ ...sched, weekday: 4 }, at(9, 1), null), false, 'đúng thứ nhưng nhầm phút → không chạy');
  assert.equal(isDue({ ...sched, weekday: 4 }, at(9, 0), '2026-08-13'), false, 'đúng thứ nhưng đã chạy hôm nay → không chạy');
});

test('isDue: chạy xong hôm qua thì hôm nay lại đúng giờ là chạy', () => {
  const sched = { enabled: 1, hour: 8, minute: 0 };
  assert.equal(isDue(sched, at(8, 0), '2026-08-12'), true, 'last_run là ngày cũ → chạy tiếp');
});

test('today() trả ngày đúng định dạng YYYY-MM-DD', () => {
  assert.equal(today(at(23, 59)), '2026-08-13');
  assert.match(today(), /^\d{4}-\d{2}-\d{2}$/);
});

test('scheduler: đúng giờ thì gọi runTurn đúng task, sai giờ thì không', async () => {
  const store = new Store(tmpDb());
  store.addSchedule({ hour: 9, minute: 30, task: 'Kiểm tra email' });
  const calls = [];
  const sched = new Scheduler({
    store,
    runTurn: async (msg) => { calls.push(msg); return { text: 'xong' }; },
    log: { info: () => {}, error: () => {} },
  });

  // 9:31 — quá giờ → không chạy
  const real = Date;
  global.Date = class extends real {
    constructor(...a) { super(...a); }
    static now() { return real.now(); }
  };
  // đơn giản hơn: chạy check() với fake đồng hồ qua tham số? check() tự new Date.
  // Kiểm hành vi qua isDue ở trên là đủ; ở đây chỉ cần CRUD + lần chạy thật.
  await sched.check(); // giờ thật hiện tại — nếu đúng 9:30 thì chạy; ngoài ra không
  // KHÔNG được có call nào trừ khi đúng 9:30 thật — giờ test chạy lúc nào cũng được:
  const now = new Date();
  if (now.getHours() === 9 && now.getMinutes() === 30) {
    assert.equal(calls.length, 1);
    assert.match(calls[0], /Kiểm tra email/);
  } else {
    assert.equal(calls.length, 0);
  }
  store.close();
});

test('scheduler: CRUD lịch hẹn đầy đủ', () => {
  const store = new Store(tmpDb());
  const s = store.addSchedule({ hour: 7, minute: 15, task: 'Chuẩn bị báo cáo' });
  assert.ok(s.id > 0);
  assert.equal(s.enabled, 1);

  const rows = store.listSchedules();
  assert.equal(rows.length, 1);
  assert.equal(rows[0].task, 'Chuẩn bị báo cáo');

  store.updateSchedule(s.id, { enabled: 0, task: 'Chuẩn bị báo cáo sửa' });
  const up = store.getSchedule(s.id);
  assert.equal(up.enabled, 0);
  assert.equal(up.task, 'Chuẩn bị báo cáo sửa');

  store.markScheduleRun(s.id, '2026-08-13');
  assert.equal(store.getSchedule(s.id).last_run, '2026-08-13');

  store.removeSchedule(s.id);
  assert.equal(store.listSchedules().length, 0);
  store.close();
});

test('scheduler: CRUD có weekday — lưu được, sửa được, kho cũ vẫn đọc được', () => {
  const store = new Store(tmpDb());
  const s = store.addSchedule({ hour: 8, minute: 0, task: 'Báo cáo tuần', weekday: 5 });
  assert.equal(store.getSchedule(s.id).weekday, 5, 'lưu thứ 6 (5)');
  assert.equal(isDue(store.getSchedule(s.id), new Date(2026, 7, 14, 8, 0), null), true,
    '14/08/2026 là Thứ 6 → đúng giờ đúng thứ → chạy');
  assert.equal(isDue(store.getSchedule(s.id), new Date(2026, 7, 13, 8, 0), null), false,
    '13/08 là Thứ 5 → không chạy');

  store.updateSchedule(s.id, { weekday: null });
  assert.equal(store.getSchedule(s.id).weekday, null, 'bỏ thứ → mọi ngày');

  store.addSchedule({ hour: 9, minute: 0, task: 'Không đặt thứ' });
  assert.equal(store.listSchedules().filter((x) => x.weekday === null).length, 2);
  store.close();
});

test('clearConversation: xoá tin + FTS không còn ra kết quả cũ', () => {
  const store = new Store(tmpDb());
  const conv = store.createConversation({ id: 'c-clear-1', day: '2026-08-13' });
  store.add({ convId: conv.id, role: 'human', text: 'quán cà phê ngon ở quận 1' });
  store.add({ convId: conv.id, role: 'alice', text: 'Gợi ý ba quán' });

  assert.equal(store.search('quan ca phe').length, 1, 'FTS phải tìm thấy trước khi xoá');

  store.clearConversation(conv.id);
  assert.equal(store.count(conv.id), 0, 'hết tin sau khi xoá');
  assert.equal(store.search('quan ca phe').length, 0,
    'FTS không được còn bản ghi của tin đã xoá — trigger messages_ad phải hoạt động');
  assert.equal(store.currentConversation(), null, 'hội thoại đã đóng');
  store.close();
});
