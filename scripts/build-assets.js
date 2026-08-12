// Sinh asset của renderer từ bản bóc DREAM.
//
// Không chép tay: file trong bundle đặt tên bằng UUID không đuôi, và cùng một font
// có tới 4-5 subset (latin / latin-ext / vietnamese / cyrillic / devanagari). Map
// bằng mắt thì sai một cái là mất dấu tiếng Việt ở đúng chỗ khó thấy nhất — chữ vẫn
// hiện, chỉ là hiện bằng font dự phòng.
//
// Nguồn sự thật là các khối @font-face trong chính HTML đã bóc: chúng đã nói sẵn
// uuid nào là family nào, weight bao nhiêu, unicode-range nào.
const fs = require('node:fs');
const path = require('node:path');

const SRC = path.resolve(__dirname, '..', 'dream-src');
const OUT = path.resolve(__dirname, '..', 'src', 'renderer', 'assets');

const html = fs.readFileSync(path.join(SRC, 'index.source.html'), 'utf8');
const index = JSON.parse(fs.readFileSync(path.join(SRC, 'assets-index.json'), 'utf8'));
const mimeOf = new Map(index.map((a) => [a.path, a.mime]));

// ── font ────────────────────────────────────────────────────────────────────
const FACE_RE = /@font-face\s*\{([^}]*)\}/g;
const field = (block, name) => {
  const m = block.match(new RegExp(`${name}\\s*:\\s*([^;]+);`));
  return m ? m[1].trim().replace(/^['"]|['"]$/g, '') : null;
};

// Nhận diện subset bằng dải mã đặc trưng, không bằng thứ tự xuất hiện.
function subsetOf(range) {
  if (!range) return 'latin';
  if (range.includes('U+1EA0-1EF9')) return 'vietnamese';
  if (range.includes('U+0900-097F')) return 'devanagari';
  if (range.includes('U+0370-0377')) return 'greek';
  if (range.includes('U+0460-052F')) return 'cyrillic-ext';
  if (range.includes('U+0400-045F')) return 'cyrillic';
  if (range.includes('U+0100-02BA')) return 'latin-ext';
  return 'latin';
}

// App chỉ hiển thị tiếng Việt + Anh. Bỏ cyrillic/greek/devanagari để bản portable
// khỏi vác theo hơn 200KB font không bao giờ dùng tới.
const KEEP = new Set(['latin', 'latin-ext', 'vietnamese']);

const faces = [];
const wanted = new Set();
let m;
while ((m = FACE_RE.exec(html))) {
  const block = m[1];
  const family = field(block, 'font-family');
  const weight = field(block, 'font-weight') || '400';
  const range = field(block, 'unicode-range');
  const srcField = field(block, 'src') || '';
  const uuid = (srcField.match(/url\(["']?([^"')]+)["']?\)/) || [])[1];
  if (!family || !uuid) continue;

  const subset = subsetOf(range);
  if (!KEEP.has(subset)) continue;

  const slug = family.toLowerCase().replace(/[^a-z0-9]+/g, '-');
  const file = `${slug}-${weight}-${subset}.woff2`;
  faces.push({ family, weight, range, file });
  wanted.add(`${uuid}::${file}`);
}

fs.mkdirSync(path.join(OUT, 'fonts'), { recursive: true });
fs.mkdirSync(path.join(OUT, 'img'), { recursive: true });

for (const pair of wanted) {
  const [uuid, file] = pair.split('::');
  fs.copyFileSync(path.join(SRC, 'assets', uuid), path.join(OUT, 'fonts', file));
}

const css = [
  '/* Sinh bởi scripts/build-assets.js từ DREAM Design System.html — đừng sửa tay. */',
  ...faces.map((f) => [
    '@font-face {',
    `  font-family: '${f.family}';`,
    '  font-style: normal;',
    `  font-weight: ${f.weight};`,
    '  font-display: swap;',
    `  src: url('fonts/${f.file}') format('woff2');`,
    f.range ? `  unicode-range: ${f.range};` : null,
    '}',
  ].filter(Boolean).join('\n')),
].join('\n\n');
fs.writeFileSync(path.join(OUT, 'fonts.css'), css + '\n', 'utf8');

// ── ảnh ─────────────────────────────────────────────────────────────────────
// CỐ Ý không lấy ảnh từ file design system: đó là art của bộ DREAM, không phải
// chân dung Alice. Ảnh mặc định của Alice là `assets/img/alice-default.png`, và
// người dùng đổi được ngay trong app (Cài đặt → Đổi ảnh Alice) — ảnh riêng lưu ở
// `alice-data/avatar.png`, không đụng vào file trong bộ cài.
const defaultAvatar = path.join(OUT, 'img', 'alice-default.png');
if (!fs.existsSync(defaultAvatar)) {
  console.warn('⚠ thiếu assets/img/alice-default.png — app sẽ hiện avatar chữ ★ thay ảnh');
}

console.log(`fonts: ${wanted.size} file (${new Set(faces.map((f) => f.family)).size} họ)`);
console.log(`avatar mặc định: ${fs.existsSync(defaultAvatar) ? 'có' : 'THIẾU'}`);
