"""Patch SAU login.py for the new Douyin creator center layout (2026).

Douyin changed their login page:
- Two tabs: "扫码登录" (QR) and "验证码登录" (SMS). QR may not be default.
- QR <img> no longer has alt="二维码"; it has an empty alt attribute.
- The QR code is a data:image/png;base64 <img> near the "扫一扫" text.

This is idempotent: it only patches if the old selector is still present.
"""

from pathlib import Path

LOGIN_PY = Path("/app/myUtils/login.py")

OLD = '''img_locator = page.get_by_role("img", name="二维码")
        # 获取 src 属性值
        src = await img_locator.get_attribute("src")'''

NEW = '''# -- patched: handle new Douyin login page layout --
        # Click QR scan tab (may not be default on new layout)
        try:
            qr_tab = page.get_by_text("扫码登录")
            if await qr_tab.count() > 0:
                await qr_tab.first.click(timeout=10000)
                await asyncio.sleep(2)
        except Exception:
            pass  # old layout without tabs

        # Find the QR code image (tries old and new selectors)
        src = None
        for _attempt in range(8):
            old_img = page.get_by_role("img", name="二维码")
            if await old_img.count() > 0:
                src = await old_img.first.get_attribute("src")
                if src:
                    break
            imgs = await page.query_selector_all('img[src^="data:image/png;base64"]')
            for img in imgs:
                box = await img.bounding_box()
                if box and box["width"] > 100 and box["height"] > 100:
                    candidate = await img.get_attribute("src")
                    cls = await img.get_attribute("class") or ""
                    if "brand" in cls or "icon" in cls or "bg" in cls:
                        continue
                    src = candidate
                    break
            if src:
                break
            await asyncio.sleep(2)
        if not src:
            print("QR code not found after all attempts")
            status_queue.put("500")
            await page.close()
            await context.close()
            await browser.close()
            return None'''


def main() -> None:
    if not LOGIN_PY.exists():
        return
    source = LOGIN_PY.read_text(encoding="utf-8")
    if OLD not in source:
        # Already patched or pattern changed
        return
    LOGIN_PY.write_text(source.replace(OLD, NEW), encoding="utf-8")
    print("  login.py patched for new Douyin layout")


if __name__ == "__main__":
    main()
