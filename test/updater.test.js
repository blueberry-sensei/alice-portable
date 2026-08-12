'use strict';

/**
 * So sánh phiên bản — nhầm một dấu là app kêu "có bản mới" cả đời hoặc không
 * bao giờ thấy bản mới. Test cả hai chiều + dữ liệu lạ (không được kết luận bừa).
 */

const test = require('node:test');
const assert = require('node:assert/strict');

const { parseVersion, isNewer } = require('../src/main/updater');

test('parseVersion: nhận "v" tiền tố, trả 3 số', () => {
  assert.deepEqual(parseVersion('v0.1.4'), [0, 1, 4]);
  assert.deepEqual(parseVersion('0.1.4'), [0, 1, 4]);
  assert.deepEqual(parseVersion('v1.2.3-rc.1'), [1, 2, 3], 'bỏ phần sau patch');
});

test('parseVersion: dữ liệu lạ trả null — không so, không kết luận', () => {
  assert.equal(parseVersion(''), null);
  assert.equal(parseVersion('abc'), null);
  assert.equal(parseVersion(null), null);
});

test('isNewer: so đúng từng thành phần', () => {
  assert.equal(isNewer([0, 1, 5], [0, 1, 4]), true);
  assert.equal(isNewer([1, 0, 0], [0, 9, 9]), true);
  assert.equal(isNewer([0, 2, 0], [0, 1, 99]), true);
  assert.equal(isNewer([0, 1, 4], [0, 1, 4]), false, 'bằng nhau không phải mới hơn');
  assert.equal(isNewer([0, 1, 3], [0, 1, 4]), false);
  assert.equal(isNewer([0, 1, 4], [1, 0, 0]), false);
});
