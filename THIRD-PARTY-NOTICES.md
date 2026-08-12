# Third-party notices

Repo này phát hành kèm một số thành phần của bên thứ ba. Giấy phép MIT ở
[`LICENSE`](LICENSE) áp cho **mã nguồn của repo này**, không áp cho những thứ dưới đây.

---

## Fonts — SIL Open Font License 1.1

Các file `.woff2` trong `src/renderer/assets/fonts/`:

| Font | Tác giả |
|---|---|
| **Nunito** | Vernon Adams, Cyreal, Jacques Le Bailly |
| **Baloo 2** | Ek Type |
| **JetBrains Mono** | JetBrains |

Cả ba đều phát hành theo **SIL Open Font License, Version 1.1**. Bản subset ở đây
lấy từ Google Fonts. Toàn văn giấy phép: <https://openfontlicense.org>

Tóm tắt nghĩa vụ khi bạn phát hành lại: giữ nguyên thông báo bản quyền và giấy phép
này, **không bán riêng font**, và nếu sửa font thì không được dùng Reserved Font Name.

```
Copyright (c) The Nunito Project Authors
Copyright (c) The Baloo 2 Project Authors (https://github.com/EkType/Baloo2)
Copyright (c) The JetBrains Mono Project Authors (https://github.com/JetBrains/JetBrainsMono)

This Font Software is licensed under the SIL Open Font License, Version 1.1.
This license is copied below, and is also available with a FAQ at:
https://openfontlicense.org

PERMISSION & CONDITIONS
Permission is hereby granted, free of charge, to any person obtaining a copy of the
Font Software, to use, study, copy, merge, embed, modify, redistribute, and sell
modified and unmodified copies of the Font Software, subject to the following
conditions:

1) Neither the Font Software nor any of its individual components, in Original or
Modified Versions, may be sold by itself.

2) Original or Modified Versions of the Font Software may be bundled, redistributed
and/or sold with any software, provided that each copy contains the above copyright
notice and this license. These can be included either as stand-alone text files,
human-readable headers or in the appropriate machine-readable metadata fields within
text or binary files as long as those fields can be easily viewed by the user.

3) No Modified Version of the Font Software may use the Reserved Font Name(s) unless
explicit written permission is granted by the corresponding Copyright Holder. This
restriction only applies to the primary font name as presented to the users.

4) The name(s) of the Copyright Holder(s) or the Author(s) of the Font Software shall
not be used to promote, endorse or advertise any Modified Version, except to
acknowledge the contribution(s) of the Copyright Holder(s) and the Author(s) or with
their explicit written permission.

5) The Font Software, modified or unmodified, in part or in whole, must be distributed
entirely under this license, and must not be distributed under any other license. The
requirement for fonts to remain under this license does not apply to any document
created using the Font Software.

TERMINATION
This license becomes null and void if any of the above conditions are not met.

DISCLAIMER
THE FONT SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO ANY WARRANTIES OF MERCHANTABILITY, FITNESS FOR A
PARTICULAR PURPOSE AND NONINFRINGEMENT OF COPYRIGHT, PATENT, TRADEMARK, OR OTHER
RIGHT. IN NO EVENT SHALL THE COPYRIGHT HOLDER BE LIABLE FOR ANY CLAIM, DAMAGES OR
OTHER LIABILITY, INCLUDING ANY GENERAL, SPECIAL, INDIRECT, INCIDENTAL, OR
CONSEQUENTIAL DAMAGES, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF THE USE OR INABILITY TO USE THE FONT SOFTWARE OR FROM OTHER DEALINGS IN
THE FONT SOFTWARE.
```

---

## Ảnh mặc định của Alice

`src/renderer/assets/img/alice-default.png` là ảnh minh hoạ nhân vật do chủ repo cung
cấp. Nếu bạn fork và phát hành lại, **hãy thay bằng ảnh của bạn** — hoặc dùng chức
năng *Cài đặt → Đổi ảnh…* trong app, ảnh riêng lưu ngoài repo.

---

## Alice Brain — nguồn trong `brain-source/`

Từ bản 0.1.3, nguồn thuần Python của Alice Brain được **vendor vào repo** tại
[`brain-source/`](brain-source/README.md) (`sag_api`, `sag_agent`, `alicecore`),
copy từ hai repo `blueberry-sensei/alice-brain` và `blueberry-sensei/alice-core`.
Cả hai phát hành theo **MIT** với hai dòng bản quyền:

```
Copyright (c) 2026 Blueberry Sensei
Copyright (c) 2026 [tác giả kế thừa — xem LICENSE của từng repo]
```

Phần **chạy** của brain (`runtime/brain/`, gồm Python nhúng và các gói PyPI) vẫn
được đóng gói vào lúc build và KHÔNG nằm trong repo — xem mục dưới.

---

## Phần mềm KHÔNG nằm trong repo này

Hai thứ dưới đây app cần lúc chạy nhưng **không** được commit vào repo; chúng do
script `bundle:*` lấy từ máy bạn:

| Thành phần | Giấy phép |
|---|---|
| **OpenCode** (`runtime/opencode/opencode.exe`) | của [opencode.ai](https://opencode.ai), theo điều khoản của họ |
| **Alice Brain** (`runtime/brain/`, gồm `sag_api`, `alicecore` và các gói PyPI) | của tác giả tương ứng, theo giấy phép riêng |
| **Electron** | MIT |

Repo này chỉ đóng gói, không sở hữu và không cấp lại quyền cho chúng.
