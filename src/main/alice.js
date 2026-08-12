'use strict';

const fs = require('node:fs');
const path = require('node:path');

const config = require('./config');

/**
 * Dựng "chỗ làm việc" cho Alice: thư mục mà `opencode run --dir` trỏ vào.
 *
 * Hai file được SINH LÚC CHẠY, không commit sẵn:
 *
 *   - `AGENTS.md`   — hiến pháp Alice. opencode tự đọc file này làm chỉ dẫn đứng.
 *   - `opencode.json` — cấu hình MCP, đường dẫn TUYỆT ĐỐI tới runtime nhúng.
 *
 * Sinh lúc chạy vì đường dẫn chỉ biết được lúc đó: bản portable có thể nằm ở
 * `D:\`, ở USB, ở `C:\Program Files`. Ghi cứng đường dẫn vào repo là đúng cái bẫy
 * `M-0035` (npx đi tìm node trong PATH) và `D-0040` (đường dẫn tuyệt đối tới runtime).
 */

const NO_NEXT_TURN = `
## Hình dạng vòng đời của môi trường này

Bạn đang chạy trong app Alice portable. Mỗi lượt là **một tiến trình \`opencode run\`**
chạy rồi thoát. Không có ai đánh thức bạn lại giữa hai lượt, không có ai đọc hộ
notification của task nền.

Hệ quả bắt buộc phải nhớ: **văn bản cuối cùng bạn in ra CHÍNH LÀ câu trả lời gửi tới
người dùng.** Nên không bao giờ được kết thúc lượt bằng câu kiểu *"để tôi chờ task nền
báo về rồi làm tiếp"* — không có "tiếp" nào cả, và toàn bộ việc đã làm trong lượt sẽ
bốc hơi cùng tiến trình. Cần chờ thì chờ ngay trong lượt. Không chờ được thì trả lời
bằng cái đã biết: tìm ra gì, kẹt ở đâu, cần người dùng làm gì.
`.trim();

const MEMORY_NOTE = `
## Trí nhớ của bạn

App giữ **toàn bộ** hội thoại nguyên văn trong SQLite của nó — đó là source-of-truth.
Session của engine chỉ là cache. Khi cửa sổ ngữ cảnh gần đầy, app tự xoay sang session
mới và **nạp mồi** gồm bản tóm tắt phần cũ + các tin gần nhất nguyên văn.

Nếu một lượt mở đầu bằng khối \`[TIẾP NỐI HỘI THOẠI]\`: đó là phần đầu cuộc trò chuyện
đã bị nén, coi như bạn đã trải qua nó. **Đừng chào lại từ đầu, đừng nói mình vừa mất
trí nhớ.** Không nhớ một chi tiết cụ thể thì tra bằng công cụ tìm kiếm hội thoại, hoặc
hỏi thẳng — đừng bịa.
`.trim();

/**
 * Ghép hiến pháp Alice.
 *
 * Chỉ lấy `knowledge/ALICE.md` — hiến pháp **chung** về cách Alice làm việc. Cố ý
 * KHÔNG lấy `ALICE.project.md`: đó là đặc tả nghiệp vụ của một project cụ thể, và
 * app này là vỏ chạy được cho mọi Alice, không phải cho riêng một project.
 */
function buildAgentsMd(knowledgeDir) {
  const parts = [];
  parts.push('<!-- Sinh tự động bởi Alice portable. Sửa file này là vô ích: nó bị ghi đè mỗi lần khởi động. -->');
  parts.push('');

  const constitution = path.join(knowledgeDir, 'ALICE.md');
  if (fs.existsSync(constitution)) {
    parts.push(fs.readFileSync(constitution, 'utf8').trim());
  } else {
    parts.push('# Alice');
    parts.push('');
    parts.push('Bạn là Alice. Chưa nạp được hiến pháp đầy đủ (`knowledge/ALICE.md` không có mặt) —');
    parts.push('nói thẳng điều đó với người dùng thay vì diễn như thể mình có đủ luật.');
  }

  parts.push('');
  parts.push('---');
  parts.push('');
  parts.push(NO_NEXT_TURN);
  parts.push('');
  parts.push(MEMORY_NOTE);
  parts.push('');

  return parts.join('\n');
}

/**
 * Cấu hình opencode cho workspace.
 *
 * MCP khai dưới key `"mcp"`, `type: "local"` + `command: [...]`. Mọi phần tử của
 * `command` là đường dẫn tuyệt đối tới runtime NHÚNG — không `npx`, không `python`
 * trần trên PATH (`D-0053` mục 3).
 */
function buildOpencodeJson(settings, { brainMcp = null } = {}) {
  const cfg = {
    $schema: 'https://opencode.ai/config.json',
    // Nhắc lại cho rõ: `instructions` trỏ vào AGENTS.md cùng thư mục.
    instructions: ['AGENTS.md'],
    mcp: {},
  };
  if (brainMcp) cfg.mcp.brain = brainMcp;
  if (settings.model) cfg.model = settings.model;
  return cfg;
}

/**
 * Tạo/refresh workspace. Trả về đường dẫn.
 * `dir` mặc định là workspace chung; public server dùng workspace RIÊNG của từng
 * Alice (`alices/<id>/workspace`) để mỗi máy chủ chạy đúng AGENTS.md + MCP của nó.
 */
function provisionWorkspace(settings, { brainMcp = null, dir = null } = {}) {
  const target = dir || config.workDir();
  fs.mkdirSync(target, { recursive: true });

  fs.writeFileSync(path.join(target, 'AGENTS.md'), buildAgentsMd(config.knowledgeDir()), 'utf8');
  fs.writeFileSync(
    path.join(target, 'opencode.json'),
    JSON.stringify(buildOpencodeJson(settings, { brainMcp }), null, 2),
    'utf8'
  );
  return target;
}

module.exports = { provisionWorkspace, buildAgentsMd, buildOpencodeJson, NO_NEXT_TURN, MEMORY_NOTE };
