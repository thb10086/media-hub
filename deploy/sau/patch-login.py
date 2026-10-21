"""Patch SAU's QR login so a Douyin login either finishes or says why it did not.

Run at container start (entrypoint.sh) against the SAU checkout in /app, or
locally with SAU_APP_DIR pointing at a checkout. Every patch is idempotent: it
is applied only while its anchor is still present, and skipped once its marker
is in the file, so re-running it is harmless.

1. layout      The 2026 creator-center login page: QR may sit behind a "扫码登录"
               tab, and the QR <img> lost its alt="二维码".
2. user agent  Every Playwright context gets a desktop Chrome user agent.
               ByteDance closes connections whose User-Agent says HeadlessChrome,
               so the login SDK never loads and the panel stays blank (no QR).
3. fail fast   A login thread that raised (page timeout, browser crash) left the
               SSE stream silent until the gateway gave up four minutes later;
               it now reports "500" at once.
4. waiting     Open the page on domcontentloaded (the full load waits on trackers
               that may never finish), re-send the QR whenever Douyin refreshes
               it, and when the login stalls after a scan (typically a second
               verification step) save a screenshot and the page text to logs/.
5. identity    Douyin often answers a scan from a new device with "身份验证", which
   check       nobody could answer in a headless browser. The check is reported on the
               login's event stream as soon as it shows ("verify:{json}": method sms /
               sms_send / unsupported, masked phone, Douyin's words, a screenshot); the
               owner's answer - an SMS code, or word that they sent the SMS Douyin asked
               for - arrives through POST /loginInput and is carried out on the page.
               Every click is short and may fail without stalling the login; keepalives
               ("ping") hold the stream open meanwhile.
6. data jar    After the login, open www.douyin.com before the storage state is
               saved: the creator site never sets ttwid, which DTK needs to use
               the same jar for data requests (media-hub imports it into DTK's
               identity pool). Publishing ignores the extra cookies.

SAU_USER_AGENT, when set, is the user agent every context gets. The gateway
sends the same string to DTK with the imported jar (HUB_DOUYIN_USER_AGENT), so
deploy/compose.yml sets both from one value.
"""

from __future__ import annotations

import os
from pathlib import Path

APP_DIR = Path(os.environ.get("SAU_APP_DIR", "/app"))

LAYOUT_OLD = """img_locator = page.get_by_role("img", name="二维码")
        # 获取 src 属性值
        src = await img_locator.get_attribute("src")"""

LAYOUT_NEW = """# -- patched: handle new Douyin login page layout --
        # Click QR scan tab (may not be default on new layout)
        try:
            qr_tab = page.get_by_text("扫码登录")
            if await qr_tab.count() > 0:
                await qr_tab.first.click(timeout=10000)
                await asyncio.sleep(2)
        except Exception:
            pass  # old layout without tabs

        # Find the QR code image (tries old and new selectors). The login SDK
        # arrives after the page itself: allow it about 90 seconds on a slow network,
        # looking twice a second so the code goes out as soon as it is drawn.
        src = None
        for _attempt in range(180):
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
            await asyncio.sleep(0.5)
        if not src:
            print("QR code not found after all attempts")
            try:
                logs_dir = Path(BASE_DIR / "logs")
                logs_dir.mkdir(exist_ok=True)
                shot = logs_dir / f"douyin-login-no-qr-{uuid.uuid4().hex[:8]}.png"
                await page.screenshot(path=shot)
                print(f"Douyin login page without a QR at {page.url}; screenshot {shot}")
            except Exception as exc:
                print(f"Douyin login page could not be captured: {exc!r}")
            status_queue.put("500")
            await page.close()
            await context.close()
            await browser.close()
            return None"""

GOTO_OLD = """await page.goto("https://creator.douyin.com/")
"""

GOTO_NEW = """# -- patched: goto waits for the document only; the full load waits on trackers
        await page.goto("https://creator.douyin.com/", wait_until="domcontentloaded", timeout=60000)
"""

WAIT_OLD = """        try:
            # 等待 URL 变化或超时
            await asyncio.wait_for(url_changed_event.wait(), timeout=200)  # 最多等待 200 秒
            print("监听页面跳转成功")
        except asyncio.TimeoutError:
            print("监听页面跳转超时")
            await page.close()
            await context.close()
            await browser.close()
            status_queue.put("500")
            return None
"""

WAIT_NEW = """        # -- patched: keep the QR fresh, and leave evidence when the login stalls --
        loop_time = asyncio.get_running_loop().time
        deadline = loop_time() + 200  # 最多等待 200 秒
        identity_checked = False
        while not url_changed_event.is_set() and loop_time() < deadline and not login_cancelled(id):
            try:
                await asyncio.wait_for(url_changed_event.wait(), timeout=2)
                break
            except asyncio.TimeoutError:
                pass
            if not identity_checked:
                try:
                    if await douyin_identity_check(page, id, status_queue, url_changed_event):
                        identity_checked = True
                        deadline = loop_time() + 20  # the check ran its course; give the redirect a moment
                        continue
                except Exception as exc:
                    identity_checked = True  # once is enough; the stall evidence below says the rest
                    print(f"identity check relay failed: {exc!r}")
            try:
                for img in await page.query_selector_all('img[src^="data:image/png;base64"]'):
                    box = await img.bounding_box()
                    if box and box["width"] > 100 and box["height"] > 100:
                        fresh = await img.get_attribute("src")
                        if fresh and fresh != src:
                            src = fresh
                            print("QR code refreshed by Douyin, re-sent")
                            status_queue.put(src)
                        break
            except Exception:
                pass  # the page is navigating away; the event above will say so
        if login_cancelled(id):
            print("login cancelled: a newer login replaced it")
            LOGIN_CANCELLED.pop(id, None)
            await page.close()
            await context.close()
            await browser.close()
            status_queue.put("500")
            return None
        if url_changed_event.is_set():
            print("监听页面跳转成功")
        else:
            print("监听页面跳转超时")
            try:
                logs_dir = Path(BASE_DIR / "logs")
                logs_dir.mkdir(exist_ok=True)
                shot = logs_dir / f"douyin-login-stalled-{uuid.uuid4().hex[:8]}.png"
                await page.screenshot(path=shot)
                text = " ".join((await page.inner_text("body")).split())[:300]
                print(f"Douyin login stalled at {page.url}; screenshot {shot}; page says: {text}")
            except Exception as exc:
                print(f"Douyin login stalled; the page could not be captured: {exc!r}")
            await page.close()
            await context.close()
            await browser.close()
            status_queue.put("500")
            return None
"""

RELAY_ANCHOR = """async def douyin_cookie_gen(id,status_queue):"""

RELAY_NEW = """# -- patched: relay Douyin's post-scan identity check to the person binding the account --
# Douyin often answers a scan from a new device with "身份验证". The browser here is
# headless, so nobody could answer and the login stalled. Now the check is reported on the
# login's event stream ("verify:{json}") as soon as it shows, and what the owner does in the
# product arrives through POST /loginInput: an SMS code to type in, or word that they sent
# the SMS Douyin asked for. Every click is short and may fail; the report always goes out,
# with a screenshot of what Douyin shows. Accounts without a check never get here.
import base64 as _base64
import json as _json
import queue as _queue
import re as _re

LOGIN_INPUTS = {}  # login id -> what the owner sent through /loginInput while the check is open
LOGIN_CANCELLED = {}  # login id -> when POST /loginCancel asked to stop it (a newer login replaced it)
SENT_SMS = "__sent__"  # the owner says they sent the SMS Douyin asked for
RESEND = "__resend__"  # the owner asks for the code again
_VERIFY_TITLE = "身份验证"
_RECEIVE_CODE = "接收短信验证码"
_SEND_SMS = "发送短信验证"
_GET_CODE = "获取验证码"
_RESEND = ("重新发送", "重新获取", "获取验证码")
_SUBMIT = ("验证", "确定", "确认", "提交", "完成", "下一步")
_SENT = ("我已发送", "已发送", "我已发送短信", "发送完成", "确定", "确认", "验证", "下一步")
_FAILURE_WORDS = ("错误", "有误", "失效", "过期", "频繁", "失败", "未收到")
_PHONE = _re.compile(r"(?:[+]?86[ ]*)?1[0-9]{2}[*]{2,}[0-9]{2,4}")
_VERIFY_WAIT_SECONDS = 300
_KEEPALIVE_SECONDS = 20
_CLICK_MS = 4000


async def _reachable(locator):
    # Playwright's trial click runs every actionability check - visible, stable, enabled and
    # not covered by another element - without clicking. The login form behind Douyin's
    # dialog fails it, so nothing is ever typed or clicked underneath the dialog (the first
    # real check on 2026-10-02 typed the code into that form's phone field).
    try:
        await locator.click(trial=True, timeout=1500)
        return True
    except Exception:
        return False


async def _visible(page, text, *, role=None, reachable=False):
    # The first visible element with this text in any frame; the dialog may sit in an iframe.
    # reachable: also on top, i.e. clickable right now (see _reachable).
    for frame in page.frames:
        try:
            if role:
                # Exact: a submit button named "验证" must not match "获取验证码", which resends the SMS.
                matches = frame.get_by_role(role, name=text, exact=True)
            else:
                matches = frame.get_by_text(text, exact=False)
            for index in range(await matches.count()):
                candidate = matches.nth(index)
                if await candidate.is_visible() and (not reachable or await _reachable(candidate)):
                    return frame, candidate
        except Exception:
            continue
    return None, None


async def _click(locator):
    # Short and forgiving: a control that will not take the click must not stall the login.
    for force in (False, True):
        try:
            await locator.click(timeout=_CLICK_MS, force=force)
            return True
        except Exception:
            continue
    return False


async def _page_text(page):
    parts = []
    for frame in page.frames:
        try:
            parts.append(await frame.inner_text("body", timeout=3000))
        except Exception:
            continue
    return " ".join(" ".join(parts).split())


def _around(text, word, size=160):
    # What Douyin wrote around a word, e.g. the instructions next to "发送".
    at = text.find(word)
    if at < 0:
        return None
    return text[max(0, at - 40) : at + size].strip()


async def _snapshot(page):
    try:
        shot = await page.screenshot(type="jpeg", quality=55, timeout=5000)
    except Exception:
        return None
    return "data:image/jpeg;base64," + _base64.b64encode(shot).decode()


async def _report(page, status_queue, **fields):
    fields["screenshot"] = await _snapshot(page)
    status_queue.put("verify:" + _json.dumps({k: v for k, v in fields.items() if v}, ensure_ascii=False))


async def _code_field(page):
    # The code box on top: Douyin's own SMS-login form under the dialog has one with the same
    # placeholder, and a phone field before it, so being visible is not enough.
    selectors = (
        'input[placeholder*="验证码"]',
        'input[inputmode="numeric"]',
        'input[type="tel"]',
        'input[type="number"]',
        "input",
    )
    for frame in page.frames:
        for selector in selectors:
            try:
                fields = frame.locator(selector)
                for index in range(await fields.count()):
                    field = fields.nth(index)
                    if await field.is_visible() and await field.is_editable() and await _reachable(field):
                        return field
            except Exception:
                continue
    return None


async def _exact_text(page, text):
    # An element whose whole text is the label and that is on top: "验证", never "获取验证码" or
    # "无法验证通过？选择其他验证方式".
    for frame in page.frames:
        try:
            matches = frame.get_by_text(text, exact=True)
            for index in range(await matches.count()):
                candidate = matches.nth(index)
                if await candidate.is_visible() and await _reachable(candidate):
                    return frame, candidate
        except Exception:
            continue
    return None, None


async def _submit_button(page, labels):
    # Real buttons first. Douyin's "验证" in the SMS dialog is a styled <div> with no button
    # role: on 2026-10-02 the code sat typed in the box and nothing ever clicked it, so the
    # login hung on "still checking". Then any element whose whole text is the label.
    for label in labels:
        _, button = await _visible(page, label, role="button", reachable=True)
        if button is not None:
            return button
    for label in labels:
        _, element = await _exact_text(page, label)
        if element is not None:
            return element
    return None


async def _choose(page):
    # The check the owner can answer from the product: receiving a code first, then sending
    # an SMS; anything else is shown as it is.
    _, receive = await _visible(page, _RECEIVE_CODE, reachable=True)
    if receive is not None and await _click(receive):
        await asyncio.sleep(2)
        # Some layouts send the code on their own (the button turns into a countdown).
        _, get_code = await _visible(page, _GET_CODE, reachable=True)
        if get_code is not None:
            await _click(get_code)
            await asyncio.sleep(1)
        found = _PHONE.search(await _page_text(page))
        return "sms", found.group(0) if found else None, None
    _, send = await _visible(page, _SEND_SMS, reachable=True)
    if send is not None and await _click(send):
        await asyncio.sleep(2)
        text = await _page_text(page)
        found = _PHONE.search(text)
        return "sms_send", found.group(0) if found else None, _around(text, "发送") or text[:200]
    text = await _page_text(page)
    return "unsupported", None, _around(text, _VERIFY_TITLE) or text[:200]


async def _enter_code(page, code):
    # (problem, the page's text right before submitting) - see _verdict for why the text.
    field = await _code_field(page)
    if field is None:
        return "页面上没有填验证码的地方", ""
    try:
        await field.fill("", timeout=_CLICK_MS)
        await field.type(code, delay=60, timeout=_CLICK_MS * 3)
    except Exception:
        return "验证码没能填进页面", ""
    await asyncio.sleep(1)
    before = await _page_text(page)
    button = await _submit_button(page, _SUBMIT)
    if button is not None:
        clicked = await _click(button)
        print(f"identity check: code typed, submit {'clicked' if clicked else 'did not take the click'}")
    else:
        # No control to press: many code forms submit on Enter.
        print("identity check: code typed, no submit control found; pressing Enter")
        try:
            await field.press("Enter", timeout=_CLICK_MS)
        except Exception:
            pass
    return None, before


async def _confirm_sent(page):
    button = await _submit_button(page, _SENT)
    if button is None:
        for label in _SENT:  # some confirmations are plain text, not buttons
            _, button = await _visible(page, label, reachable=True)
            if button is not None:
                break
    before = await _page_text(page)
    if button is not None and await _click(button):
        return None, before
    return "页面上没有确认已发送的按钮", before


async def _resend(page):
    for label in _RESEND:
        _, button = await _visible(page, label, reachable=True)
        if button is not None and await _click(button):
            await asyncio.sleep(1)
            return "已重新发送验证码，请留意短信"
    return "现在还不能重新发送，请稍等一会儿再试"


def _new_failure(before, after):
    # A failure word Douyin showed for this answer: one the page did not already show right
    # before submitting (an earlier attempt's message may still be on screen).
    for word in _FAILURE_WORDS:
        if word in after and _around(after, word) != _around(before, word):
            return word
    return None


async def _verdict(page, done_event, sent, before):
    # After an answer: None when the login went through; otherwise (error, notice). An error
    # only when Douyin says so on the page - silence is "still checking", not a wrong code.
    for _ in range(2):
        try:
            await asyncio.wait_for(done_event.wait(), timeout=10)
            return None
        except asyncio.TimeoutError:
            pass
        reason = _new_failure(before, await _page_text(page))
        if reason:
            return f"抖音提示验证{reason}", None
        # The submit button may not have taken the first click (it enables only once the code is in).
        button = await _submit_button(page, _SENT if sent else _SUBMIT)
        if button is None:
            break
        await _click(button)
    if done_event.is_set():
        return None
    if sent:
        return None, "抖音还没确认收到短信，确认短信已发出后再点一次"
    return None, "已提交，抖音还在确认。如果手机抖音弹出了登录确认，请点“确认”；一直没有结果的话，重新获取验证码再试"


def login_cancelled(login_id):
    return login_id in LOGIN_CANCELLED


def cancel_login(login_id):
    # Forget requests older than 15 minutes, so ids of logins that already ended do not pile up.
    import time as _time

    now = _time.monotonic()
    for stale in [key for key, at in LOGIN_CANCELLED.items() if now - at > 900]:
        LOGIN_CANCELLED.pop(stale, None)
    LOGIN_CANCELLED[login_id] = now


async def douyin_identity_check(page, login_id, status_queue, done_event):
    # False when no check is on screen. Otherwise it runs until the login goes through
    # (done_event), five minutes pass, or the check cannot be answered from here.
    _, title = await _visible(page, _VERIFY_TITLE)
    if title is None:
        return False
    print("Douyin asks for an identity check after the scan")
    inputs = LOGIN_INPUTS.setdefault(login_id, _queue.Queue())
    method, phone, message = "unsupported", None, None
    try:
        try:
            method, phone, message = await _choose(page)
        except Exception as exc:
            print(f"identity check: could not pick a method: {exc!r}")
        print(f"identity check relayed as {method}")
        await _report(page, status_queue, method=method, phone=phone, message=message)
        loop_time = asyncio.get_running_loop().time
        deadline = loop_time() + _VERIFY_WAIT_SECONDS
        last_signal = loop_time()
        while not done_event.is_set() and loop_time() < deadline and not login_cancelled(login_id):
            try:
                received = inputs.get_nowait()
            except _queue.Empty:
                if loop_time() - last_signal > _KEEPALIVE_SECONDS:
                    status_queue.put("ping")  # keeps the gateway's read timeout from firing
                    last_signal = loop_time()
                try:
                    await asyncio.wait_for(done_event.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass
                continue
            if received == RESEND:
                notice = await _resend(page)
                await _report(page, status_queue, method=method, phone=phone, message=message, notice=notice)
                last_signal = loop_time()
                continue
            sent = received == SENT_SMS
            problem, before = await (_confirm_sent(page) if sent else _enter_code(page, received))
            notice = None
            if problem is None:
                verdict = await _verdict(page, done_event, sent, before)
                if verdict is None:
                    break
                problem, notice = verdict
            await _report(
                page, status_queue, method=method, phone=phone, message=message, error=problem, notice=notice
            )
            last_signal = loop_time()
    except Exception as exc:
        print(f"identity check relay failed: {exc!r}")
        await _report(page, status_queue, method="unsupported", message="身份验证中断了，请重新扫码")
    finally:
        LOGIN_INPUTS.pop(login_id, None)
    return True


async def douyin_cookie_gen(id,status_queue):"""

DATA_JAR_OLD = """        uuid_v1 = uuid.uuid1()
        print(f"UUID v1: {uuid_v1}")
"""

DATA_JAR_NEW = """        # -- patched: open www.douyin.com before saving, so the jar also carries the
        # cookies the data service signs with (ttwid, odin_tt, ...); publishing ignores them
        try:
            await page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=30000)
            for _ in range(15):
                if any(c["name"] == "ttwid" for c in await context.cookies("https://www.douyin.com/")):
                    break
                await asyncio.sleep(1)
            else:
                print("www.douyin.com set no ttwid; the jar will not work for the data service")
        except Exception as exc:
            print(f"www.douyin.com could not be opened; the jar is saved without its cookies: {exc!r}")
        uuid_v1 = uuid.uuid1()
        print(f"UUID v1: {uuid_v1}")
"""

ROUTE_ANCHOR = """# 包装函数：在线程中运行异步函数"""

ROUTE_NEW = """# -- patched: stop a login a newer one replaced, so it frees its browser at once --
@app.route('/loginCancel', methods=['POST'])
def login_cancel():
    from myUtils.login import cancel_login
    data = request.get_json(silent=True) or {}
    login_id = str(data.get('id') or '')
    if not login_id:
        return jsonify({"code": 500, "msg": "id is required", "data": None}), 400
    cancel_login(login_id)
    return jsonify({"code": 200, "msg": None, "data": None})


# -- patched: the owner's answer to a running login's identity check (see myUtils/login.py) --
@app.route('/loginInput', methods=['POST'])
def login_input():
    from myUtils.login import LOGIN_INPUTS, RESEND, SENT_SMS
    data = request.get_json(silent=True) or {}
    inputs = LOGIN_INPUTS.get(str(data.get('id') or ''))
    if inputs is None:
        return jsonify({"code": 500, "msg": "no login is waiting for the owner", "data": None}), 404
    code = str(data.get('code') or '').strip()
    if data.get('action') == 'sent':
        inputs.put(SENT_SMS)
    elif data.get('action') == 'resend':
        inputs.put(RESEND)
    elif code.isdigit():
        inputs.put(code)
    else:
        answer = "send a numeric code, action=sent or action=resend"
        return jsonify({"code": 500, "msg": answer, "data": None}), 400
    return jsonify({"code": 200, "msg": None, "data": None})


# 包装函数：在线程中运行异步函数"""

THREAD_OLD = """def run_async_function(type,id,status_queue):
    match type:"""

THREAD_NEW = """def run_async_function(type,id,status_queue):
    # -- patched: a login that raises must still end the SSE stream --
    try:
        _run_login(type, id, status_queue)
    except Exception as exc:
        print(f"login thread failed: {exc!r}")
        status_queue.put("500")


def _run_login(type,id,status_queue):
    match type:"""

USER_AGENT_ANCHOR = """async def set_init_script(context):"""

USER_AGENT_NEW = """# -- patched: a desktop Chrome user agent for every Playwright context --
# ByteDance closes connections whose User-Agent says HeadlessChrome, which leaves
# Douyin's login panel blank. SAU_USER_AGENT wins when set (media-hub sends the
# same string to its data service); otherwise the platform part matches the host
# and the major version matches the browser actually launched.
import os as _os
import sys as _sys

try:
    from playwright.async_api import Browser as _PlaywrightBrowser
except ImportError:  # pragma: no cover - playwright is a hard dependency
    _PlaywrightBrowser = None


def desktop_user_agent(browser_version: str) -> str:
    configured = _os.environ.get("SAU_USER_AGENT", "").strip()
    if configured:
        return configured
    major = (browser_version or "136").split(".")[0]
    system = {
        "darwin": "Macintosh; Intel Mac OS X 10_15_7",
        "win32": "Windows NT 10.0; Win64; x64",
    }.get(_sys.platform, "X11; Linux x86_64")
    return (
        f"Mozilla/5.0 ({system}) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{major}.0.0.0 Safari/537.36"
    )


if _PlaywrightBrowser is not None and not getattr(_PlaywrightBrowser.new_context, "_desktop_ua", False):
    _original_new_context = _PlaywrightBrowser.new_context

    async def _new_context_with_desktop_ua(self, *args, **kwargs):
        kwargs.setdefault("user_agent", desktop_user_agent(self.version))
        kwargs.setdefault("locale", "zh-CN")
        return await _original_new_context(self, *args, **kwargs)

    _new_context_with_desktop_ua._desktop_ua = True
    _PlaywrightBrowser.new_context = _new_context_with_desktop_ua


async def set_init_script(context):"""


def _douyin_segment(source: str) -> tuple[int, int]:
    """Bounds of douyin_cookie_gen: the other platforms share its wait block."""
    start = source.find("async def douyin_cookie_gen(")
    if start < 0:
        return -1, -1
    end = source.find("\nasync def ", start + 1)
    return start, len(source) if end < 0 else end


def _patch(path: Path, name: str, old: str, new: str, *, marker: str, douyin_only: bool = False) -> None:
    if not path.exists():
        print(f"  skip {name}: {path} not found")
        return
    source = path.read_text(encoding="utf-8")
    start, end = _douyin_segment(source) if douyin_only else (0, len(source))
    if start < 0:
        print(f"  skip {name}: douyin_cookie_gen not found")
        return
    segment = source[start:end]
    if marker in segment:
        return
    if segment.count(old) != 1:
        print(f"  skip {name}: anchor not found exactly once (upstream changed?)")
        return
    path.write_text(source[:start] + segment.replace(old, new) + source[end:], encoding="utf-8")
    print(f"  patched {name}")


def main() -> None:
    login_py = APP_DIR / "myUtils" / "login.py"
    _patch(
        login_py,
        "douyin login layout",
        LAYOUT_OLD,
        LAYOUT_NEW,
        marker="# -- patched: handle new Douyin login page layout --",
        douyin_only=True,
    )
    _patch(
        login_py, "douyin login page load", GOTO_OLD, GOTO_NEW, marker="# -- patched: goto", douyin_only=True
    )
    _patch(
        login_py,
        "douyin login wait",
        WAIT_OLD,
        WAIT_NEW,
        marker="# -- patched: keep the QR fresh",
        douyin_only=True,
    )
    _patch(
        login_py,
        "douyin identity check relay",
        RELAY_ANCHOR,
        RELAY_NEW,
        marker="# -- patched: relay Douyin's post-scan identity check",
    )
    _patch(
        login_py,
        "douyin data jar",
        DATA_JAR_OLD,
        DATA_JAR_NEW,
        marker="# -- patched: open www.douyin.com before saving",
        douyin_only=True,
    )
    _patch(
        APP_DIR / "sau_backend.py",
        "login input route",
        ROUTE_ANCHOR,
        ROUTE_NEW,
        marker="# -- patched: the owner's answer to a running login's identity check",
    )
    _patch(
        APP_DIR / "sau_backend.py",
        "login thread errors",
        THREAD_OLD,
        THREAD_NEW,
        marker="# -- patched: a login that raises",
    )
    _patch(
        APP_DIR / "utils" / "base_social_media.py",
        "desktop user agent",
        USER_AGENT_ANCHOR,
        USER_AGENT_NEW,
        marker="# -- patched: a desktop Chrome user agent",
    )


if __name__ == "__main__":
    main()
