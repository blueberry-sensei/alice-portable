# Multi-engine support: Claude Code (subscription) bên cạnh opencode (API key)

**Ngày:** 2026-08-13 · **Trạng thái:** approved (Bệ hạ: "cứ tốt nhất mà làm thôi")

## Mục tiêu

Mỗi Alice chọn được **một trong hai engine**:

- **opencode** (hiện có) — auth bằng API key, mỗi Alice một key riêng.
- **Claude Code CLI** (mới) — auth bằng **subscription** (đăng nhập OAuth qua `claude
  login`), **ghim cứng vào một tài khoản cho từng Alice**, không bị ảnh hưởng khi Bệ hạ
  logout ở trình duyệt hay máy khác.

Hai engine cơ chế session hoàn toàn khác nhau (opencode: mỗi lượt một tiến trình
`opencode run --session <id>`; Claude: `claude --print --resume/--session-id <id>`), nhưng
phải cùng tuân theo **một bộ luật chung** ở tầng `Memory`, không đặt riêng theo engine.

Tham khảo: `D:\Work\kd-reserve\automation\tools\telegram_listener.py` (đã tự giải quyết
rotation 2 trục + hook nạp lại skill sau compact cho Claude Code từ trước).

## Luật chung (áp dụng cả hai engine)

### 1. Một session xuyên suốt lịch sử chat
Không đổi so với hiện tại: `conversation.engine_session` lưu trong `chat.db`, dùng lại cho
tới khi bị xoay.

### 2. Xoay session — BA điều kiện, thoả bất kỳ cái nào cũng xoay
Hiện đã có 2/3 (`memory.js`): hết ngày (`rotateDaily`), tràn context (`afterTurn`
threshold). **Thêm điều kiện thứ ba**, khác các trục còn lại ở chỗ nó là **AND, không phải
OR** — cố ý làm mềm hơn kiểu "tuổi thuần" bên `kd-reserve` (project đó dùng OR độc lập,
sau đó phải nới ngưỡng lên vì cắt ngang cuộc đang nói dở; Alice Portable dùng AND ngay từ
đầu để tránh lặp lại bài học đó):

```
tuổi session > 12h   VÀ   im lặng kể từ tin cuối > 1h   →  xoay
```

Lý do AND: một session 13 tiếng tuổi mà vẫn đang nhắn liên tục mỗi vài phút thì KHÔNG xoay
— tuổi một mình không phải tín hiệu đủ, phải có khoảng dừng thật kèm theo.

**Điểm kiểm tra:** đầu mỗi lượt, trong `Memory.ensureConversation()` — chỗ duy nhất TẤT
CẢ các đường gọi (`alice:send` từ app, `/v1/chat` từ trang public, `scheduler.js` chạy
routine) đều đi qua. Không cần sửa gì ở 3 nơi gọi.

### 3. Auto-compact khi tràn context
opencode: **đã có sẵn**, không cần thêm gì — `Memory.afterTurn` tự nén bằng model khi
chạm ngưỡng (`contextCeiling`/`windowRatio`).
Claude: dùng **chung logic đó** — `afterTurn`/rotation không phân biệt engine, engine chỉ
khác ở chỗ AI thực thi lượt tóm tắt (`engine.runWithFallback` với prompt tóm tắt, xem
`_summarizer()` trong `main.js`/`public-server.js` — không đổi, chỉ cần `engine` trỏ đúng
instance).

### 4. Nạp lại skill mỗi khi xoay session / sau auto-compact

- **opencode:** KHÔNG cần cơ chế mới. Mỗi lượt là một tiến trình `opencode run` mới,
  `workspace/opencode.json` khai `instructions: ["AGENTS.md"]` nên AGENTS.md được đọc lại
  từ đĩa ở MỌI lượt — "reload" xảy ra miễn phí theo kiến trúc hiện tại, kể cả compact nội
  bộ của opencode (nếu có) cũng không né được vì tiến trình khởi động lại từ đầu mỗi lần.
- **Claude:** cần hook thật, vì `claude --print --resume` là một tiến trình MỚI nhưng
  NỐI TIẾP transcript cũ — model không tự đọc lại AGENTS.md giữa chừng. Dùng đúng pattern
  đã chạy thật ở `kd-reserve/automation/.claude/settings.json` + `reminder.js`:
  - `provisionWorkspace()` (đã có, sinh `workspace/AGENTS.md` + `workspace/opencode.json`)
    sinh thêm `workspace/.claude/settings.json` với hook `SessionStart` — **không cần
    field `matcher`**, bỏ trống là bắt MỌI nguồn (`startup`, `resume`, `compact`, `clear`),
    xác minh thật bằng cách chạy `claude --print --output-format stream-json` và thấy hook
    của `kd-reserve/automation` (không có `matcher`) tự chạy ngay ở `SessionStart:startup`
    — và `workspace/.claude-hooks/reload-skill.js`.
  - Hook đọc `workspace/AGENTS.md`, in ra `stdout` bọc trong thẻ nhắc nhớ — Claude Code
    nạp thẳng output đó vào context ngay sau compact/resume.

### 5. Chọn & ghim engine theo từng Alice

- `registry.js`: `alice.provider` đổi từ hard-code `'opencode'` thành `'opencode' |
  'claude'`, chọn lúc tạo Alice, đổi được sau trong Settings (giống đổi model).
- **opencode:** giữ nguyên — API key qua `auth.setApiKey('opencode', key, home)`.
- **Claude — ghim subscription theo Alice:** set `CLAUDE_CONFIG_DIR` trỏ vào
  `<alice-home>/claude-config/` khi spawn `claude` cho Alice đó (tương tự cách
  `portableEnv(baseDir)` đã cô lập `XDG_CONFIG_HOME` cho opencode — xem
  `engine/auth.js`). Mỗi Alice có thư mục config Claude RIÊNG → đăng nhập bằng
  `claude login` (redirect qua env này) là ghim cứng, logout ở máy/trình duyệt khác
  không đụng tới file trong thư mục đó.
  **Đã xác minh thật (2026-08-13, máy dev):** `CLAUDE_CONFIG_DIR=<thư mục tạm>` +
  `claude auth status` trả `{"loggedIn": false}` — hoàn toàn tách khỏi phiên đăng nhập
  thật (`dat.phan@kyanon.digital`, team) khi KHÔNG set biến này. Cô lập THẬT, kể cả token
  OAuth — `kd-reserve/automation` không dùng cách này (auth thẳng `~/.claude.json` chung)
  nhưng đây là cơ chế đúng, không cần fallback.
  `claude auth status` trả JSON: `{loggedIn, authMethod, apiProvider, email, orgId,
  orgName, subscriptionType}` — dùng để hiện trạng thái đăng nhập của Alice trong Settings.
- UI: Settings → chọn Provider (opencode / claude). Chọn `claude` thì KHÔNG có ô API key —
  hiện hướng dẫn "chạy `claude login` trong terminal, dùng đúng tài khoản muốn ghim cho
  Alice này" (không tự dựng luồng OAuth trong Electron — ngoài phạm vi, CLI đã lo phần đó).

## Kiến trúc: `ClaudeEngine` cùng interface với `OpencodeEngine`

`turn.js`, `memory.js`, `main.js`, `public-server.js` hiện chỉ gọi qua một biến `engine`
(dependency injected). Interface tối thiểu cần khớp (đọc từ `engine/opencode.js`):

```
class ClaudeEngine {
  constructor(settings)
  setBaseDir(dir)
  get available                              // binary `claude` có trên PATH không
  async listModels({ baseDir, timeout })      // claude không có API liệt kê model kiểu
                                               // opencode — trả danh sách CỐ ĐỊNH, không
                                               // hỏi mạng: ['claude-opus-5', 'claude-sonnet-5',
                                               // 'claude-haiku-4-5-20251001', 'claude-fable-5']
                                               // (đúng model id CLI `claude --model` nhận)
  async runWithFallback(opts)                 // KHÔNG xoay nhiều model như opencode (không
                                               // có khái niệm "free model hết quota, thử
                                               // model khác") — gọi run() một lần, trả
                                               // {attempts: []} để turn.js không phải rẽ
                                               // nhánh theo engine
  run({ message, sessionId, model, cwd, onEvent, signal, baseDir, idleMs })
                                               // spawn `claude --print
                                               // --output-format stream-json --verbose
                                               // --include-partial-messages
                                               // --dangerously-skip-permissions
                                               // --resume <id> | --session-id <id> [--model]
                                               // --append-system-prompt <mention rule
                                               // nếu multi-user> <message>`
  cancel()                                    // kill cây tiến trình (taskkill /T trên
                                               // Windows — xem `_kill_tree` tham khảo)
}
```

### Schema `stream-json`, đo THẬT (2026-08-13, `claude --print --output-format
stream-json --verbose --include-partial-messages "Say exactly: hi"`) — mỗi dòng một JSON:

- `{"type":"stream_event","event":{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"h"}},...}`
  → chữ chảy dần, dùng cho `onEvent(partial, ev)` (animation typing).
- `{"type":"result","subtype":"success","result":"hi","session_id":"...",
  "usage":{"input_tokens":9566,"output_tokens":4,...},"total_cost_usd":0.294,...}`
  → dòng CUỐI, lấy `text = result`, `sessionId = session_id`,
  `tokens = {input: usage.input_tokens, output: usage.output_tokens}`.
- `{"type":"result","subtype":"error_*",...}` hoặc `is_error: true` → lỗi, ném `Error`.
- Tool-use: theo đúng schema streaming chuẩn của Anthropic Messages API
  (`content_block_start` với `content_block.type === "tool_use"`) — CHƯA đo thật với một
  prompt có gọi tool; xác minh lại ở Task 1 trước khi tin vào field name.

File mới: `src/main/engine/claude.js`. `main.js`/`public-server.js` chọn
`new OpencodeEngine(settings)` hay `new ClaudeEngine(settings)` theo `alice.provider` lúc
`activateAlice()`/`publicServerFor()`.

`sessionId` cho Claude LÀ session id thật của Claude Code (không phải `ses_xxx` như
opencode) — `store.setEngineSession` đã lưu chuỗi tuỳ ý, không cần đổi schema.

## Không làm trong lần này (YAGNI)

- Không tự dựng UI đăng nhập OAuth cho Claude trong Electron.
- Không cho một Alice dùng CẢ HAI engine cùng lúc hay auto-fallback opencode↔claude.
- Không đổi cơ chế multi-model-fallback của opencode.

## Testing

- Unit test `ClaudeEngine` với `claude` giả (spawn một script giả lập, giống cách
  `test/e2e-engine.test.js` test `OpencodeEngine` thật) — test riêng `run()`/`cancel()`.
- Unit test rule xoay session mới (điều kiện AND 12h/1h) trong `memory.test.js`, tách khỏi
  2 điều kiện cũ.
- Hook `reload-skill.js`: test tương tự `reminder.js` — đọc file giả, kiểm tra output.
- `CLAUDE_CONFIG_DIR` isolation: xác minh THỦ CÔNG trước khi tin vào nó (không có cách
  unit-test một OAuth flow thật) — ghi rõ trong PR review, không auto-claim đã cô lập nếu
  chưa tận tay xác minh.
