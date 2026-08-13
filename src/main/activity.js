'use strict';

/**
 * Dịch event NDJSON của opencode thành MỘT CÂU tiếng người.
 *
 * Bệ hạ chốt (2026-08-13): khi Alice gọi công cụ thì phải thấy nó đang gọi cái gì.
 * Trước đây mọi thứ Alice làm giữa lượt đều biến mất sau ba chấm nhấp nháy — một
 * lượt tra trí nhớ mất 40 giây nhìn y hệt một lượt bị treo, và người dùng bấm dừng
 * vì tưởng hỏng.
 *
 * Đọc PHÒNG THỦ: hình dạng event là của opencode, không phải của app này, và nó đổi
 * theo phiên bản. Không nhận ra thì trả `null` — thà không hiện gì còn hơn hiện sai
 * hoặc ném lỗi giữa lượt chat.
 */

/** Tên tool → câu người đọc được. Không có trong bảng thì dùng chính tên tool. */
const LABELS = {
  read: 'đang đọc file',
  write: 'đang ghi file',
  edit: 'đang sửa file',
  patch: 'đang sửa file',
  bash: 'đang chạy lệnh',
  glob: 'đang tìm file',
  grep: 'đang tìm trong file',
  list: 'đang xem thư mục',
  webfetch: 'đang đọc trang web',
  websearch: 'đang tra trên mạng',
  todowrite: 'đang ghi việc cần làm',
  task: 'đang giao việc cho trợ lý phụ',
  // Brain (MCP) — tên tool tới dạng `<server>_<tool>` hoặc `<server>.<tool>`.
  search: 'đang tra trí nhớ',
  grep_knowledge: 'đang tra trí nhớ',
  get_entity: 'đang tra trí nhớ',
  list_sources: 'đang xem nguồn tri thức',
  list_documents: 'đang xem tài liệu',
  sync: 'đang đồng bộ kiến thức',
};

function labelFor(rawName) {
  const name = String(rawName || '').toLowerCase();
  if (!name) return null;
  if (LABELS[name]) return LABELS[name];
  // `alice-brain_search`, `brain.search`… → lấy phần đuôi.
  const tail = name.split(/[._-]/).pop();
  if (LABELS[tail]) return LABELS[tail];
  return `đang dùng ${name}`;
}

/**
 * @returns {{key: string, label: string, status: string}|null}
 *   `key` để phía nhận khỏi hiện trùng cùng một lượt gọi hai lần.
 */
function toolActivity(ev) {
  if (!ev || ev.type !== 'tool' || !ev.part) return null;
  const part = ev.part;
  const name = part.tool || part.name || (part.state && part.state.tool);
  const label = labelFor(name);
  if (!label) return null;
  const status = (part.state && part.state.status) || 'running';
  const key = `${part.callID || part.id || name}:${status}`;
  return { key, label, status };
}

module.exports = { toolActivity, labelFor, LABELS };
