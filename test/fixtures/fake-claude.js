#!/usr/bin/env node
'use strict';

/**
 * `claude` giả — in ra đúng khuôn `stream-json` đã đo thật từ CLI thật (xem spec
 * 2026-08-13), để test `ClaudeEngine` không tốn subscription thật và chạy được
 * trên máy không có `claude`.
 *
 * Điều khiển qua biến môi trường (test set trước khi spawn):
 *   FAKE_CLAUDE_TEXT     text trả về (mặc định "ổn")
 *   FAKE_CLAUDE_EXIT     mã thoát (mặc định 0)
 *   FAKE_CLAUDE_ERROR    có mặt (bất kỳ giá trị) → in dòng result lỗi thay vì thành công
 *   FAKE_CLAUDE_DELAY_MS trễ trước khi in xong, để test cancel() (mặc định 0)
 *   FAKE_CLAUDE_SESSION  session id giả (mặc định một uuid cố định)
 */

const sessionId = process.env.FAKE_CLAUDE_SESSION || '11111111-1111-1111-1111-111111111111';
const text = process.env.FAKE_CLAUDE_TEXT || 'ổn';
const exitCode = Number(process.env.FAKE_CLAUDE_EXIT || 0);
const isError = Boolean(process.env.FAKE_CLAUDE_ERROR);
const delayMs = Number(process.env.FAKE_CLAUDE_DELAY_MS || 0);

function line(obj) {
  process.stdout.write(JSON.stringify(obj) + '\n');
}

line({ type: 'system', subtype: 'init', session_id: sessionId, model: 'claude-sonnet-5' });
for (const ch of text) {
  line({
    type: 'stream_event',
    event: { type: 'content_block_delta', index: 0, delta: { type: 'text_delta', text: ch } },
    session_id: sessionId,
  });
}

setTimeout(() => {
  if (isError) {
    line({
      type: 'result', subtype: 'error_during_execution', is_error: true,
      result: 'lỗi giả lập', session_id: sessionId,
    });
  } else {
    line({
      type: 'result', subtype: 'success', is_error: false, result: text,
      session_id: sessionId,
      usage: { input_tokens: 10, output_tokens: text.length, cache_read_input_tokens: 0 },
    });
  }
  process.exit(exitCode);
}, delayMs);
