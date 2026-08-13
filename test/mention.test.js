'use strict';

/**
 * Luật "@alice mới trả lời" — chỉ áp cho chat public nhiều người.
 *
 * Sai ở đây tốn tiền theo hai chiều: bắt nhầm thì Alice chen vào mọi câu người ta
 * nói với nhau và đốt API key; bỏ sót thì người dùng gọi mà Alice im, và họ sẽ
 * tưởng máy chủ hỏng chứ không nghĩ là mình gõ thiếu.
 */

const test = require('node:test');
const assert = require('node:assert/strict');

const { isMention, handlesFor, fold } = require('../src/main/mention');
const { toolActivity, labelFor } = require('../src/main/activity');

test('gọi Alice: các cách gõ thường gặp đều nhận', () => {
  for (const t of ['@alice giúp tôi', '@Alice ơi', 'ê @alice xem hộ cái',
    'hỏi @alice-k-os đi', '@kos check giúp', '@alice, mai họp mấy giờ?',
    '@ALICE\nxuống dòng vẫn tính']) {
    assert.equal(isMention(t, 'Alice K-OS'), true, `phải nhận: ${JSON.stringify(t)}`);
  }
});

test('gọi Alice: tên có dấu gõ không dấu vẫn nhận', () => {
  assert.equal(isMention('@alice-phuong xem giúp', 'Alice Phượng'), true);
  assert.equal(isMention('@alicephuong xem giúp', 'Alice Phượng'), true);
  assert.equal(isMention('@phuong xem giúp', 'Alice Phượng'), true);
  assert.equal(fold('Alice Phượng'), 'alice phuong');
});

test('gọi Alice: KHÔNG bắt nhầm', () => {
  const no = [
    'mai họp lúc mấy giờ',                 // câu thường
    'gửi mail@alice.com nhé',              // email — `@` dính vào chữ trước
    'nói với @nga đi',                     // gọi người khác
    'alice ơi',                            // không có @
    'giá 100@đơn vị',                      // @ giữa từ
  ];
  for (const t of no) {
    assert.equal(isMention(t, 'Alice K-OS'), false, `KHÔNG được nhận: ${JSON.stringify(t)}`);
  }
});

test('gọi Alice: tin rỗng / rác không làm ném lỗi', () => {
  for (const t of [null, undefined, '', 123, {}]) {
    assert.equal(isMention(t, 'Alice K-OS'), false);
  }
  assert.ok(handlesFor('').has('alice'), 'luôn nhận @alice kể cả khi Alice chưa có tên');
});

// ── hiện tool đang chạy ────────────────────────────────────────────────────

test('tool: dịch event của opencode thành câu tiếng người', () => {
  const act = toolActivity({ type: 'tool', part: { tool: 'read', callID: 'c1', state: { status: 'running' } } });
  assert.equal(act.label, 'đang đọc file');
  assert.equal(act.status, 'running');
  assert.ok(act.key.includes('c1'), 'key phải gắn với lượt gọi để không hiện trùng');
});

test('tool: tool của brain nhận ra qua phần đuôi tên', () => {
  assert.equal(labelFor('alice-brain_search'), 'đang tra trí nhớ');
  assert.equal(labelFor('brain.sync'), 'đang đồng bộ kiến thức');
});

test('tool: tool lạ vẫn nói được tên, không im lặng', () => {
  assert.equal(labelFor('cai_gi_do_moi'), 'đang dùng cai_gi_do_moi');
});

test('tool: event KHÔNG phải tool thì trả null, không đoán bừa', () => {
  assert.equal(toolActivity({ type: 'text', part: { text: 'chào' } }), null);
  assert.equal(toolActivity({ type: 'step_finish', part: {} }), null);
  assert.equal(toolActivity(null), null);
  assert.equal(toolActivity({ type: 'tool' }), null, 'thiếu part thì bỏ qua, không ném lỗi');
});
