'use strict';

/**
 * Một lượt chat, từ đầu tới cuối.
 *
 * Tách khỏi `main.js` vì đây là chỗ dễ sai nhất trong app và là chỗ phải test được
 * mà không cần mở cửa sổ Electron: thứ tự giữa "xoay session", "nạp mồi" và "gửi tin"
 * chính là thứ đã hỏng ở bản alice-social — bot quên từ lượt thứ HAI mà vẫn trả lời
 * trơn tru, nên không ai thấy bất thường (`D-0054` mục 4, `D-0055` mục 5).
 */

/**
 * Độ đầy cửa sổ THẬT = `input` + `cache.read`.
 *
 * Không phải mỗi `input`. Khi provider bật prompt-cache, phần lịch sử cũ được tính
 * sang `cache.read` và `input` của lượt sau có thể **nhỏ hơn** lượt đầu — đo thật:
 * lượt 1 `input=8048`, lượt 2 `input=6131` kèm `cache.read=1920`, dù hội thoại đã
 * dài thêm.
 *
 * Chỉ nhìn `input` thì ngưỡng nén không bao giờ chạm: app tưởng cửa sổ đang co lại
 * trong khi nó đang phình ra, và lượt vỡ sẽ tới mà chưa hề nén lần nào. Đây đúng là
 * kiểu hỏng "im lặng" mà `D-0054` cảnh báo — nhìn ngoài mọi thứ vẫn chạy trơn.
 */
function occupiedWindow(tokens) {
  if (!tokens) return null;
  const cached = tokens.cache ? (tokens.cache.read || 0) : 0;
  return (tokens.input || 0) + cached;
}

/**
 * @param {object} deps
 * @param {import('./memory/store').Store}   deps.store
 * @param {import('./memory/memory').Memory} deps.memory
 * @param {object} deps.engine   OpencodeEngine (hoặc fake trong test)
 * @param {string} deps.workDir
 * @param {object} deps.settings
 */
function createTurnRunner({ store, memory, engine, workDir, settings }) {
  /**
   * @param {string} userText
   * @param {function} [onStream]  (partialText, event) — để UI vẽ chữ chạy
   */
  /**
   * @param {string}   userText
   * @param {function} [onStream]  (partialText, event) — để UI vẽ chữ chạy
   * @param {object}   [opts]
   * @param {string?}  [opts.who]  tên người gửi khi Alice đang phục vụ nhiều người
   *   qua máy chủ public (`anonymous-x7k2q`, `nga`…). `null` = chủ máy chat trong app.
   */
  return async function runTurn(userText, onStream = null, opts = {}) {
    const who = opts.who || null;
    const { conversation, seed, reason } = await memory.ensureConversation();

    // Lưu tin NGUYÊN VĂN, không kèm mồi. Mồi là chuyện của engine, không phải thứ
    // Bệ hạ đã gõ — trộn vào là lịch sử sai và lần nén sau sẽ nén cả mồi.
    store.add({
      convId: conversation.id,
      role: 'human',
      text: userText,
      engineSession: conversation.engine_session,
      meta: who ? { who } : null,
    });

    // Nhiều người chung một Alice: model PHẢI biết ai đang nói, không thì nó trả
    // lời người này bằng ngữ cảnh của người kia mà không hề biết mình nhầm.
    const said = who ? `[${who}]: ${userText}` : userText;
    const message = seed ? `${seed}\n\n${said}` : said;

    const out = await engine.runWithFallback({
      message,
      sessionId: conversation.engine_session || null,
      model: settings.model || null,
      cwd: workDir,
      onEvent: onStream ? (ev, partial) => onStream(partial, ev) : null,
    });

    // Session của engine chỉ biết được SAU lượt đầu — lưu lại để lượt sau nối tiếp.
    if (out.sessionId && out.sessionId !== conversation.engine_session) {
      store.setEngineSession(conversation.id, out.sessionId);
      conversation.engine_session = out.sessionId;
    }

    const tokensInput = occupiedWindow(out.tokens);
    store.add({
      convId: conversation.id,
      role: 'alice',
      text: out.text,
      tokensInput,
      engineSession: out.sessionId,
      meta: { model: out.model, attempts: out.attempts || [] },
    });

    // Xoay NGAY sau lượt chạm ngưỡng, để lượt sau đã bắt đầu ở session mới. Đợi tới
    // lúc thật sự vỡ thì lượt đó mất trắng.
    const rotated = await memory.afterTurn(conversation, tokensInput);

    return {
      conversationId: conversation.id,
      engineSession: out.sessionId,
      text: out.text,
      model: out.model,
      tokens: out.tokens,
      attempts: out.attempts || [],
      seeded: Boolean(seed),
      seedReason: reason,
      rotated: rotated
        ? { reason: rotated.reason, compacted: rotated.compactedCount, kept: rotated.keptCount }
        : null,
    };
  };
}

module.exports = { createTurnRunner, occupiedWindow };
