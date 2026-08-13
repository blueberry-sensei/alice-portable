'use strict';

/**
 * Luật "@alice mới trả lời" — CHỈ áp cho chat public nhiều người.
 *
 * Bệ hạ chốt (2026-08-13): trong app là chat một-một nên gõ gì Alice cũng trả lời;
 * còn trang web công khai là chỗ nhiều người nói với NHAU, Alice chen vào mọi câu
 * thì vừa ồn vừa đốt API key. Nhưng Alice vẫn phải ĐỌC hết — im lặng khác với mù.
 *
 * Tách ra file riêng vì đây là một luật, và luật thì phải test được mà không cần
 * dựng máy chủ.
 */

/** Bỏ dấu tiếng Việt để "@alice-phượng" và "@alice-phuong" là một. */
function fold(s) {
  return String(s)
    .normalize('NFD').replace(/[̀-ͯ]/g, '')
    .replace(/đ/g, 'd').replace(/Đ/g, 'D')
    .toLowerCase();
}

/**
 * Các tên gọi được chấp nhận sau dấu `@`: luôn có `alice`, cộng thêm tên riêng của
 * Alice này (cả bản đầy đủ lẫn bản bỏ chữ "alice" ở đầu).
 * "Alice K-OS" → nhận `@alice`, `@alice-k-os`, `@k-os`, `@kos`.
 */
function handlesFor(aliceName) {
  const out = new Set(['alice']);
  const folded = fold(aliceName || '').trim();
  if (!folded) return out;
  const dashed = folded.replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
  if (dashed) {
    out.add(dashed);
    out.add(dashed.replace(/-/g, ''));
    const short = dashed.replace(/^alice-?/, '');
    if (short) {
      out.add(short);
      out.add(short.replace(/-/g, ''));
    }
  }
  return out;
}

/**
 * Tin này có gọi Alice không?
 *
 * Cố ý KHÔNG dùng `text.includes('@alice')`: "email@alice.com" sẽ khớp. Dấu `@`
 * phải đứng đầu tin hoặc sau một khoảng trắng/dấu câu, và tên phải kết thúc ở ranh
 * giới từ.
 */
function isMention(text, aliceName = '') {
  const s = fold(text || '');
  if (!s.includes('@')) return false;
  const handles = handlesFor(aliceName);
  const re = /(^|[\s(\[{"'>,;:!?])@([a-z0-9][a-z0-9-]*)/g;
  let m;
  while ((m = re.exec(s)) !== null) {
    // Cắt dần đuôi: "@alice-k-os," đã bị regex loại dấu phẩy, nhưng "@alice-oi"
    // thì `alice` vẫn phải khớp nếu người ta gõ "@alice-ơi".
    let handle = m[2];
    while (handle) {
      if (handles.has(handle)) return true;
      const cut = handle.lastIndexOf('-');
      if (cut < 0) break;
      handle = handle.slice(0, cut);
    }
  }
  return false;
}

module.exports = { isMention, handlesFor, fold };
