'use strict';

/**
 * Xoá lẻ MỘT tin nhắn.
 *
 * Chỗ dễ sai nhất không phải câu DELETE mà là bảng FTS: `messages_fts` dùng
 * `content='messages'` (external content), nó KHÔNG tự biết một dòng vừa bị xoá.
 * Thiếu trigger `messages_ad` thì tin đã xoá biến khỏi khung chat nhưng vẫn hiện
 * nguyên văn trong ô tìm kiếm — đúng kiểu hỏng im lặng: người dùng tưởng đã xoá.
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { Store } = require('../src/main/memory/store');

function freshStore() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'alice-del-'));
  const store = new Store(path.join(dir, 'chat.db'));
  store.createConversation({ id: 'conv-a', day: '2026-08-14' });
  store.createConversation({ id: 'conv-b', day: '2026-08-14' });
  return store;
}

test('removeMessage: xoá khỏi kho VÀ khỏi tìm kiếm', () => {
  const store = freshStore();
  const keep = store.add({ convId: 'conv-a', role: 'human', text: 'giữ lại câu bánh mì này' });
  const drop = store.add({ convId: 'conv-a', role: 'alice', text: 'xoá giùm câu phở này' });

  assert.equal(store.search('phở').length, 1, 'trước khi xoá thì tìm ra');

  assert.equal(store.removeMessage('conv-a', drop), true);
  assert.equal(store.count('conv-a'), 1);
  assert.deepEqual(store.recent('conv-a', 10).map((m) => m.id), [keep]);
  assert.equal(store.search('phở').length, 0, 'xoá rồi thì tìm KHÔNG ra nữa');
  assert.equal(store.search('bánh mì').length, 1, 'tin còn lại vẫn tìm ra bình thường');

  store.close();
});

test('removeMessage: không xoá được tin của cuộc trò chuyện khác', () => {
  const store = freshStore();
  const other = store.add({ convId: 'conv-b', role: 'human', text: 'tin của cuộc khác' });

  assert.equal(store.removeMessage('conv-a', other), false, 'id lạc không được xoá xuyên cuộc');
  assert.equal(store.count('conv-b'), 1, 'tin của cuộc kia phải còn nguyên');

  assert.equal(store.removeMessage('conv-a', 999999), false, 'id không tồn tại → false, không ném');

  store.close();
});
