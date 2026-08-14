'use strict';

const { CLAUDE_MODELS } = require('./claude');

/**
 * Model NÀO thật sự được đẩy xuống engine của một Alice.
 *
 * Đây là lớp chặn cuối cùng giữa cấu hình và CLI. Lý do nó tồn tại: hai họ model
 * hoàn toàn không thay thế được cho nhau — `claude` chỉ hiểu `claude-sonnet-5`,
 * `opencode` chỉ hiểu `nhàcungcấp/model` — nhưng cả hai đều nhận model qua ĐÚNG
 * MỘT trường `alice.model`. Ghép sai thì thứ người dùng nhận được là câu lỗi thô
 * bằng tiếng Anh của CLI, hiện lên như một bong bóng đỏ giữa cuộc trò chuyện, mà
 * thanh tiêu đề ngay phía trên vẫn khoe đúng model họ đã chọn (đo thật 2026-08-14).
 *
 * Ghép sai KHÔNG phải lỗi chết người: bỏ model đi thì CLI tự chọn mặc định và Alice
 * vẫn nói chuyện được. Nên ở đây hạ về `null` và trả kèm một câu nói được bằng
 * tiếng người, để panel Kết nối nói ra chỗ sai thay vì để nó vỡ giữa lượt chat.
 */

// `opencode models` in ra đúng hình dạng này — cùng biểu thức đang lọc ở
// `OpencodeEngine.listModels()`.
const OPENCODE_MODEL_RE = /^[\w.-]+\/[\w.:-]+$/;

/**
 * @param {object|null} alice  một dòng trong `alices.json`
 * @returns {{ model: string|null, warning: string|null }}
 *   `model` = giá trị an toàn để đưa cho engine (`null` = để CLI tự chọn).
 */
function modelFor(alice) {
  const wanted = (alice && alice.model) || null;
  if (!wanted) return { model: null, warning: null };

  const provider = alice && alice.provider === 'claude' ? 'claude' : 'opencode';

  if (provider === 'claude') {
    if (CLAUDE_MODELS.includes(wanted)) return { model: wanted, warning: null };
    return {
      model: null,
      warning: `Alice này chạy bằng Claude Code, nhưng model đang đặt là "${wanted}" — `
        + 'đó không phải model của Claude. Alice đang tạm dùng model mặc định; vào Cài đặt chọn lại model cho đúng.',
    };
  }

  if (OPENCODE_MODEL_RE.test(wanted)) return { model: wanted, warning: null };
  return {
    model: null,
    warning: `Alice này chạy bằng opencode, nhưng model đang đặt là "${wanted}" — `
      + 'đó không phải model của opencode. Alice đang xoay vòng model mặc định; vào Cài đặt chọn lại model cho đúng.',
  };
}

module.exports = { modelFor, OPENCODE_MODEL_RE };
