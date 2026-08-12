'use strict';

/**
 * Nhật ký của app — ghi vào `alice-data/logs/app.log`, cạnh mọi dữ liệu khác.
 *
 * Lý do có file riêng: trước đây mọi lỗi chỉ nằm trong stderr của tiến trình
 * Electron, mà người dùng cuối không bao giờ thấy. Khi có trục trặc, người đưa
 * app phải hỏi vặn vẹo mới đoán được chuyện gì xảy ra. Nay người dùng bấm nút
 * chẩn đoán trong app là đọc thẳng nhật ký này, hoặc gửi cả thư mục `logs`.
 *
 * Không bao giờ ghi giá trị secret vào đây: API key, token, `.secret_key` —
 * D-0004. Chỗ nào log tham số lệnh spawn phải rà soát trước.
 */

const fs = require('node:fs');
const path = require('node:path');

const config = require('./config');

const LOG_DIR = path.join(config.DATA_DIR, 'logs');
const LOG_FILE = path.join(LOG_DIR, 'app.log');
const MAX_BYTES = 5 * 1024 * 1024; // 5 MB — đủ cho hàng trăm lượt lỗi, xoay sớm kẻo phình

function log(level, msg) {
  const line = `[${new Date().toISOString()}] ${level} ${msg}\n`;
  try {
    fs.mkdirSync(LOG_DIR, { recursive: true });
    let stat = null;
    try { stat = fs.statSync(LOG_FILE); } catch { /* chưa có file */ }
    if (stat && stat.size > MAX_BYTES) {
      try { fs.renameSync(LOG_FILE, `${LOG_FILE}.1`); } catch { /* không xoay được thì ghi đè tiếp */ }
    }
    fs.appendFileSync(LOG_FILE, line, 'utf8');
  } catch {
    // Log hỏng KHÔNG được làm chết app — đây là đường phụ trợ, không phải đường chính.
  }
}

function info(msg) { log('INFO', msg); }
function warn(msg) { log('WARN', msg); }
function error(msg) { log('ERROR', msg); }

/** N dòng cuối của nhật ký, thứ tự cũ → mới. Về sau nối thêm dòng mới. */
function tail(n = 300) {
  try {
    const raw = fs.readFileSync(LOG_FILE, 'utf8');
    const lines = raw.split(/\r?\n/).filter(Boolean);
    return lines.slice(-n);
  } catch {
    return [];
  }
}

module.exports = { LOG_DIR, LOG_FILE, log, info, warn, error, tail };
