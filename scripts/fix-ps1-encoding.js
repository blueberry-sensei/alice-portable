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

const dir = __dirname;
const BOM = '﻿';
let fixed = 0;

for (const name of fs.readdirSync(dir)) {
  if (!name.endsWith('.ps1')) continue;
  const p = path.join(dir, name);
  const text = fs.readFileSync(p, 'utf8');
  if (text.startsWith(BOM)) continue;
  fs.writeFileSync(p, BOM + text, 'utf8');
  console.log('BOM +', name);
  fixed += 1;
}
console.log(fixed ? `Đã sửa ${fixed} file.` : 'Mọi .ps1 đã có BOM.');
