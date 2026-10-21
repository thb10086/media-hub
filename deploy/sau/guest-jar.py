"""Print a guest www.douyin.com storage state from SAU's browser, as JSON on stdout.

``deploy/deploy.sh guest-identity`` pipes it into ``media-hub import-jar -`` so
DTK's identity pool has an identity before anyone has bound an account. A guest
identity is enough for the hot board, video details and downloads; search needs
a logged-in one, which every QR-bound Douyin account adds on its own.

Uses SAU's own browser setup, so the User-Agent is SAU_USER_AGENT - the string
the gateway hands to DTK with the jar. Diagnostics go to stderr.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

APP_DIR = os.environ.get("SAU_APP_DIR", "/app")
WWW = "https://www.douyin.com/"


async def guest_jar() -> dict:
    sys.path.insert(0, APP_DIR)
    from myUtils.login import get_browser_options
    from playwright.async_api import async_playwright
    from utils.base_social_media import set_init_script

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(**get_browser_options())
        try:
            context = await set_init_script(await browser.new_context())
            page = await context.new_page()
            await page.goto(WWW, wait_until="domcontentloaded", timeout=60000)
            for _ in range(20):
                if any(c["name"] == "ttwid" for c in await context.cookies(WWW)):
                    break
                await asyncio.sleep(1)
            else:
                raise SystemExit("www.douyin.com set no ttwid; try again later")
            await asyncio.sleep(3)  # the page's own scripts add a few more cookies
            print(f"guest jar from {await page.evaluate('navigator.userAgent')}", file=sys.stderr)
            return await context.storage_state()
        finally:
            await browser.close()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(guest_jar())))
