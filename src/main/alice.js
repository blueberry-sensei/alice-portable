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
/**
 * `model` là của RIÊNG Alice đang mở và phải được TRUYỀN VÀO — không đọc lén
 * `settings.model`.
 *
 * Đó chính là chỗ rò thứ tư của bug model mồ côi: ba chỗ đọc kia đã được vá ở
 * `edc3f4b`, còn dòng này vẫn lặng lẽ ghi model toàn cục cũ vào `opencode.json`
 * của MỌI workspace, mỗi lần boot. `null` = không ghi gì, để opencode tự xoay
 * vòng theo `modelPreference`.
 */
function buildOpencodeJson(settings, { brainMcp = null, reportMcp = null, model = null } = {}) {
  const cfg = {
    $schema: 'https://opencode.ai/config.json',
    // Nhắc lại cho rõ: `instructions` trỏ vào AGENTS.md cùng thư mục.
    instructions: ['AGENTS.md'],
    mcp: {},
  };
  if (brainMcp) cfg.mcp.brain = brainMcp;
  if (reportMcp) cfg.mcp.report = reportMcp;
  if (model) cfg.model = model;
  return cfg;
}

/**
 * Bản `.mcp.json` cho Claude Code — đổi từ định dạng opencode sang định dạng Claude.
 *
 * Vì sao cần riêng: Claude Code KHÔNG đọc `opencode.json` — nó đọc `.mcp.json`
 * trong thư mục làm việc. Alice engine `claude` (Alice K-OS) chỉ nhìn thấy brain
 * MCP khi file này được sinh kèm; đo thật 2026-08-14: chưa có file này nên brain
 * không tới tay Alice K-OS. Đường dẫn vẫn là tuyệt đối tới runtime NHÚNG (`D-0053`
 * mục 3) — không `npx`, không `python` trần trên PATH.
 *
 * Chỉ ghi khi có ít nhất một server — file rỗng làm Claude Code quét vô ích.
 */
function buildClaudeMcpJson({ brainMcp = null, reportMcp = null } = {}) {
  const servers = {};
  const entries = [
    ['brain', brainMcp],
    ['report', reportMcp],
  ];
  for (const [name, e] of entries) {
    if (!e || e.type !== 'local' || !Array.isArray(e.command) || e.command.length === 0) continue;
    const [command, ...args] = e.command;
    servers[name] = {
      command,
      args,
      ...(e.environment && Object.keys(e.environment).length ? { env: e.environment } : {}),
    };
  }
  if (Object.keys(servers).length === 0) return null;
  return { mcpServers: servers };
}

/**
 * Hook `SessionStart` cho Claude Code — bỏ trống `matcher` để bắt MỌI nguồn
 * (`startup`, `resume`, `compact`, `clear`). Đã xác minh thật 2026-08-13: hook không
 * `matcher` của `kd-reserve/automation` tự chạy đúng lúc `SessionStart:startup`.
 *
 * Output của hook được Claude Code nạp thẳng vào context — đây là lớp phòng thủ DUY
 * NHẤT sống ngoài model, nên auto-compact không xoá được, kể cả khi nó xoá sạch phần
 * còn lại của context.
 */
function buildClaudeSettings() {
  return {
    hooks: {
      SessionStart: [
        {
          hooks: [
            { type: 'command', command: 'node .claude-hooks/reload-skill.js' },
          ],
        },
      ],
    },
  };
}

/**
 * Script hook — đọc AGENTS.md CÙNG THƯ MỤC workspace, in ra stdout. Không tự dựng
 * nội dung: nhờ vậy không có bản luật thứ hai trôi khác AGENTS.md (tham khảo
 * `kd-reserve/automation/knowledge/tools/reminder.js`, đã chạy thật).
 */
function buildReloadSkillHook() {
  return `#!/usr/bin/env node
'use strict';
const fs = require('fs');
const path = require('path');
const agentsPath = path.join(__dirname, '..', 'AGENTS.md');
if (!fs.existsSync(agentsPath)) { process.exit(0); }
const content = fs.readFileSync(agentsPath, 'utf8').trim();
if (!content) { process.exit(0); }
process.stdout.write([
  '<alice-workspace-reload>',
  'Luật của workspace này, nạp lại tự động vì phiên vừa khởi động hoặc vừa auto-compact.',
  'Ký ức trong context sau compaction KHÔNG đáng tin.',
  '',
  content,
  '</alice-workspace-reload>',
  '',
].join('\\n'));
`;
}

/**
 * Tạo/refresh workspace. Trả về đường dẫn.
 * `dir` mặc định là workspace chung; public server dùng workspace RIÊNG của từng
 * Alice (`alices/<id>/workspace`) để mỗi máy chủ chạy đúng AGENTS.md + MCP của nó.
 */
function provisionWorkspace(settings, { brainMcp = null, reportMcp = null, dir = null, model = null } = {}) {
  const target = dir || config.workDir();
  fs.mkdirSync(target, { recursive: true });

  fs.writeFileSync(path.join(target, 'AGENTS.md'), buildAgentsMd(config.knowledgeDir()), 'utf8');
  fs.writeFileSync(
    path.join(target, 'opencode.json'),
    JSON.stringify(buildOpencodeJson(settings, { brainMcp, reportMcp, model }), null, 2),
    'utf8'
  );
  // Claude Code không đọc opencode.json — nó đọc .mcp.json (xem buildClaudeMcpJson).
  // Có file mới khi có server thật; Alice engine `claude` sẽ thấy brain + report.
  const claudeMcp = buildClaudeMcpJson({ brainMcp, reportMcp });
  if (claudeMcp) {
    fs.writeFileSync(path.join(target, '.mcp.json'), JSON.stringify(claudeMcp, null, 2), 'utf8');
  } else {
    fs.rmSync(path.join(target, '.mcp.json'), { force: true });
  }

  // Hook cho Claude Code — sinh LUÔN dù Alice đang dùng opencode: vô hại (opencode
  // không đọc `.claude/`), và Alice đổi provider sau này thì hook đã sẵn sàng.
  const claudeDir = path.join(target, '.claude');
  const hooksDir = path.join(target, '.claude-hooks');
  fs.mkdirSync(claudeDir, { recursive: true });
  fs.mkdirSync(hooksDir, { recursive: true });
  fs.writeFileSync(
    path.join(claudeDir, 'settings.json'),
    JSON.stringify(buildClaudeSettings(), null, 2),
    'utf8'
  );
  fs.writeFileSync(path.join(hooksDir, 'reload-skill.js'), buildReloadSkillHook(), 'utf8');

  return target;
}

module.exports = {
  provisionWorkspace, buildAgentsMd, buildOpencodeJson, buildClaudeMcpJson, buildClaudeSettings, buildReloadSkillHook,
  NO_NEXT_TURN, MEMORY_NOTE,
};
