// Ghi lại mọi .ps1 dưới dạng UTF-8 CÓ BOM.
//
// Windows PowerShell 5.1 đọc file .ps1 KHÔNG có BOM theo codepage ANSI của máy. Với
// script chứa tiếng Việt, mỗi ký tự có dấu biến thành 2-3 ký tự rác — và khi rác đó
// rơi vào giữa một chuỗi hoặc một chú thích thì parser vỡ, báo lỗi ở dòng hoàn toàn
// không liên quan ("Unexpected token 'kiểm'", "The '<' operator is reserved").
//
// Cùng họ M-0004 (script Python phải reconfigure UTF-8 cho stdin/stdout trên Windows),
// chỉ khác là ở đây lỗi xảy ra lúc PARSE chứ không phải lúc chạy — nên nó giết cả
// script chứ không chỉ làm hỏng một dòng in.
const fs = require('node:fs');
const path = require('node:path');

// Quét cả `scripts/` lẫn gốc repo: `install.ps1` nằm ở gốc (để đường dẫn raw ngắn
// cho khách dán), và nó cũng vỡ y hệt nếu thiếu BOM.
const dirs = [__dirname, path.resolve(__dirname, '..')];
const BOM = '﻿';
let fixed = 0;

const files = dirs.flatMap((d) =>
  fs.readdirSync(d).filter((n) => n.endsWith('.ps1')).map((n) => path.join(d, n)));

const problems = [];

for (const p of files) {
  const name = path.relative(path.resolve(__dirname, '..'), p);
  let text = fs.readFileSync(p, 'utf8');

  if (!text.startsWith(BOM)) {
    fs.writeFileSync(p, BOM + text, 'utf8');
    console.log('BOM +', name);
    fixed += 1;
    text = BOM + text;
  }

  // `param()` phải đứng TRƯỚC mọi câu lệnh (chú thích và dòng trống không tính).
  //
  // Parser tĩnh không kêu gì khi đặt sai, nên lỗi chỉ lộ ra lúc chạy — và lộ dưới
  // dạng "The term 'param' is not recognized", một câu chẳng gợi ý gì về nguyên
  // nhân thật. Đã dính ba lần trong cùng một phiên; kiểm ở đây rẻ hơn nhiều.
  const lines = text.replace(/^﻿/, '').split(/\r?\n/);
  let firstCode = -1;
  let paramLine = -1;
  for (let i = 0; i < lines.length; i += 1) {
    const t = lines[i].trim();
    if (!t || t.startsWith('#')) continue;
    if (/^param\s*\(/i.test(t)) { paramLine = i + 1; break; }
    if (firstCode === -1) firstCode = i + 1;
  }
  if (paramLine > 0 && firstCode > 0 && firstCode < paramLine) {
    problems.push(`${name}: param() ở dòng ${paramLine} nhưng đã có lệnh ở dòng ${firstCode} — param phải đứng đầu`);
  }
}

console.log(fixed ? `Đã sửa ${fixed} file.` : 'Mọi .ps1 đã có BOM.');

if (problems.length) {
  console.error('\nLỖI:');
  for (const p of problems) console.error('  ' + p);
  process.exit(1);
}
