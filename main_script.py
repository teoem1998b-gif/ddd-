"""
🤖 BOT v7.4 - CLOUDFLARE REMOVED (FULL FILE WITH PATCH MARKERS)

This file is a full copy of the merged main script with ALL automatic
Cloudflare handling removed and replaced by simple markers. A watchdog
is provided to bring up any pages that need manual verification.

Markers included (searchable):
- <<< PATCH START: CF_HANDLING_REMOVED / <<< PATCH END: CF_HANDLING_REMOVED
- <<< PATCH START: CF_MANUAL_REMOVED / <<< PATCH END: CF_MANUAL_REMOVED
- <<< PATCH START: CF_COMMAND_REMOVED / <<< PATCH END: CF_COMMAND_REMOVED
- <<< PATCH START: CF_DETECT / <<< PATCH END: CF_DETECT
- <<< PATCH START: CF_WATCHDOG / <<< PATCH END: CF_WATCHDOG
- <<< PATCH START: CF_CALL_REPLACED / <<< PATCH END: CF_CALL_REPLACED
- <<< PATCH START: APPEND_HISTORY_ROW / <<< PATCH END: APPEND_HISTORY_ROW

Note: This file is intended for you to edit manually if you want to
change behavior. The Cloudflare auto-handling code has been removed on
purpose so you can manually verify CF pages when watchdog brings them up.
"""

import asyncio
import csv
import json
import re
import time
import random
import traceback
import tempfile
import shutil
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from telethon import TelegramClient, events
from telethon.tl.types import MessageEntitySpoiler
from telethon.network import ConnectionTcpAbridged
from playwright.async_api import async_playwright

from config import Config
from logger_setup import logger
from code_validator import CodeValidator
from image_code_extractor import get_image_extractor
from database import init_database
from rate_limiter import init_anti_detection
from monitoring import init_monitoring
from features import print_version_info, get_shutdown_handler

# ============================================================
# DOMAIN-SPECIFIC SUBMIT BUTTON SELECTORS
# ============================================================
SUBMIT_BUTTON_SELECTORS = {
    "mm88code.com": "img.submit-btn, .submit-button-container img, .submit-btn",
    "llwincode.com": 'img[src*="btnnhancode" i], img[alt*="nhan" i]',
    "xx88code.com": 'button[aria-label="Nhận code"], button[aria-label*="Nhan code" i]',
    "o8code.com": ".modal-submit-btn",
    "new88b.today": 'button[aria-label*="Kiểm tra" i]',
    "tangquaqq88.com": 'button[aria-label*="Kiểm tra" i]',
    "uy88code.org": "#casinoSubmit",
    "mmoocode.shop": "#casinoSubmit",
}


# ============================================================
# BOT STATE
# ============================================================
class BotState:
    def __init__(self):
        self.playwright_instance = None
        self.connected_browsers = {}
        self.account_pages = {}
        self.context_locks = {}
        self.is_running = True
        self.cf_verified = {}
        self.submission_count = {}
        self._input_cache: dict = {}
        self._input_cache_ttl = 20.0
        self._site_code_seen: dict = {}
        self._page_urls: dict = {}
        self.handler_registered = False
        self._last_cleanup_time = time.time()
        self._pending_image_msgs: dict = {}
        self._PENDING_IMAGE_TTL: float = getattr(
            Config, "PENDING_IMAGE_TTL", 180.0
        )
        self._tab_fail_count: dict = {}
        self._TAB_FAIL_THRESHOLD: int = getattr(Config, "TAB_FAIL_THRESHOLD", 5)
        # Track pages that are Cloudflare-blocked so watchdog can surface them
        self._cf_blocked_pages: dict = {}


bot_state = BotState()

# ✅ SET BOT_START_TIME AT LAUNCH
BOT_START_TIME = datetime.now(timezone.utc)

# Telegram client
client = TelegramClient(
    Config.SESSION_NAME,
    Config.API_ID,
    Config.API_HASH,
    device_model="Desktop Bot",
    system_version="Windows 10",
    app_version="1.0",
    connection=ConnectionTcpAbridged,
    connection_retries=5,
    retry_delay=1,
    auto_reconnect=True,
    use_ipv6=False,
    flood_sleep_threshold=60,
    receive_updates=True,
    sequential_updates=False,
)

# Global state
_systems = None
message_queue: asyncio.Queue = None
message_workers: list = []
_history_queue: asyncio.Queue = None
_history_writer_task = None
_submit_semaphore: asyncio.Semaphore | None = None
_domain_semaphores: dict = {}
_active_submit_tasks: set[asyncio.Task] = set()

# ============================================================
# HELPERS & UTILITIES
# ============================================================

def normalize_domain(url: str) -> str:
    parsed = urlparse(url or "")
    domain = parsed.netloc or parsed.path
    return domain.lower().replace("www.", "").strip("/")


def select_random_code(codes: list) -> str:
    if not codes:
        return None
    return random.choice(codes)


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# Code history logging
CODE_HISTORY_DIR = Path("logs/code_history")
CODE_HISTORY_DIR.mkdir(parents=True, exist_ok=True)


def _write_history_row(row: dict):
    try:
        fieldnames = [
            "time", "event_type", "channel", "site", "account", "code",
            "source", "status", "telegram_delay", "submit_elapsed",
            "message", "screenshot",
        ]
        csv_path = CODE_HISTORY_DIR / f"code_history_{_today_str()}.csv"
        jsonl_path = CODE_HISTORY_DIR / f"code_history_{_today_str()}.jsonl"

        write_header = not csv_path.exists()
        with csv_path.open("a", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if write_header:
                writer.writeheader()
            writer.writerow(row)

        with jsonl_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.debug(f"⚠️ Cannot write code history: {e}")


async def _history_writer_loop():
    global _history_queue
    while True:
        try:
            row = await _history_queue.get()
            if row is None:
                break
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, _write_history_row, row)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.debug(f"⚠️ history_writer_loop error: {e}")
        finally:
            try:
                _history_queue.task_done()
            except Exception:
                pass


def start_history_writer():
    global _history_queue, _history_writer_task
    _history_queue = asyncio.Queue(maxsize=2000)
    _history_writer_task = asyncio.create_task(_history_writer_loop())
    logger.info("✅ Background history writer started")


async def safe_is_visible(element) -> bool:
    try:
        return await element.is_visible()
    except Exception:
        return False


def _invalidate_input_cache(key: str):
    bot_state._input_cache.pop(key, None)


async def find_input_fields(page, cache_key: str = None):
    now = time.time()
    if cache_key:
        cached = bot_state._input_cache.get(cache_key)
        if cached:
            username_input, code_input, cache_time = cached
            if now - cache_time < bot_state._input_cache_ttl:
                try:
                    if code_input:
                        visible = await code_input.is_visible()
                        if visible:
                            return username_input, code_input
                    _invalidate_input_cache(cache_key)
                except Exception:
                    _invalidate_input_cache(cache_key)

    username_input = None
    code_input = None

    username_selectors = [
        "#account-code", "#username-input", "#ten_tai_khoan",
        "input#username", "input[name='username']",
        "input[placeholder*='người dùng' i]", "input[placeholder*='tên' i]",
        "input[placeholder*='tài' i]", "input[placeholder*='tài khoản' i]",
        "input[placeholder*='user' i]", "input[placeholder*='đăng nhập' i]",
        "input[name='ten_tai_khoan']", "input[id='username']", "input[type='text']",
    ]
    code_selectors = [
        "#promo-code", "#giftcode-input", "input[autocomplete='one-time-code']",
        "input#code", "input[name='code']", "input[placeholder*='mã code' i]",
        "input[placeholder*='code' i]", "input[placeholder*='mã' i]",
        "input[name='giftcode']", "input[id='code']", "input[id*='code' i]",
        "input[id*='promo' i]",
    ]
    try:
        for selector in username_selectors:
            try:
                element = await page.query_selector(selector)
                if element and await safe_is_visible(element):
                    username_input = element
                    break
            except Exception:
                pass
        for selector in code_selectors:
            try:
                element = await page.query_selector(selector)
                if element and await safe_is_visible(element):
                    code_input = element
                    break
            except Exception:
                pass
        if not username_input or not code_input:
            inputs = await page.query_selector_all(
                "input:not([type='hidden']):not([type='checkbox']):not([type='radio']):not([type='submit'])"
            )
            visible_inputs = []
            for inp in inputs:
                if await safe_is_visible(inp):
                    visible_inputs.append(inp)
            if len(visible_inputs) >= 2:
                if not username_input:
                    username_input = visible_inputs[0]
                if not code_input:
                    code_input = visible_inputs[1]
            elif len(visible_inputs) == 1:
                if not code_input:
                    code_input = visible_inputs[0]
    except Exception as e:
        logger.debug(f"⚠️ Error finding input fields: {e}")
    if cache_key and code_input:
        bot_state._input_cache[cache_key] = (username_input, code_input, now)
    return username_input, code_input


async def scroll_to_input_fields(page):
    try:
        found = await page.evaluate("""
            () => {
                const inputs = document.querySelectorAll('input[type="text"], input:not([type="hidden"])');
                if (inputs.length > 0) {
                    const firstInput = inputs[0];
                    firstInput.scrollIntoView({behavior: 'smooth', block: 'center'});
                    firstInput.focus();
                    return true;
                }
                return false;
            }
        """)
        await asyncio.sleep(0.1)
        if found:
            logger.debug("✅ Scrolled to input fields")
        else:
            logger.warning("⚠️ scroll_to_input_fields: không tìm thấy input nào trên trang")
        return found
    except Exception as e:
        logger.debug(f"⚠️ Scroll error: {e}")
        return False


async def get_input_value(input_element) -> str:
    try:
        return (await input_element.input_value(timeout=1000)).strip()
    except Exception:
        return ""


# ============================================================
# SUBMIT BUTTON CLICKING
# ============================================================


async def click_submit_fast(page, domain: str = "") -> bool:
    domain_sel = SUBMIT_BUTTON_SELECTORS.get(domain)
    if domain_sel:
        try:
            clicked = await page.evaluate(f"""
                async () => {{
                    const deadline = Date.now() + 600;
                    while (Date.now() < deadline) {{
                        const btn = document.querySelector('{domain_sel}');
                        if (btn && !btn.disabled) {{
                            const rect = btn.getBoundingClientRect();
                            if (rect.width > 0 and rect.height > 0) {{
                                btn.click();
                                return True;
                            }}
                        }}
                        await new Promise(r => setTimeout(r, 100));
                    }}
                    const btn = document.querySelector('{domain_sel}');
                    if (btn) {{ btn.click(); return true; }}
                    return false;
                }}
            """)
            if clicked:
                logger.debug(f"✅ Clicked domain-specific button: {domain}")
                return True
        except Exception:
            pass
    try:
        clicked = await page.evaluate("""
            () => {
                const keywords = [
                    'kiểm tra ngay', 'kiem tra ngay', 'kiểm tra', 'kiem tra', 'nhận code', 'nhan code',
                    'nhận ngay', 'nhan ngay', 'áp dụng', 'ap dung', 'đổi code', 'doi code', 'nạp code', 'nap code',
                    'gửi', 'gui', 'submit', 'apply'
                ];
                const EXCLUDE = /menu|nav|home|close|cancel|toggle|hamburger|back|trở về|huỷ|hủy|đóng|xác thực|xac thuc|verify|check/i;
                const els = [...document.querySelectorAll('button, a[role="button"], div[role="button"], span[role="button"], input[type="button"], input[type="submit"]')];
                for (const kw of keywords) {
                    for (const el of els) {
                        if (el.disabled) continue;
                        const aria = (el.getAttribute('aria-label') || '').toLowerCase();
                        const img = el.querySelector('img[alt]');
                        const imgAlt = img ? (img.getAttribute('alt') || '').toLowerCase() : '';
                        const txt = (el.innerText || el.textContent || el.value || '').toLowerCase().trim();
                        if (EXCLUDE.test(aria + txt)) continue;
                        if ([txt, aria, imgAlt].some(s => s and s.includes(kw))) {
                            const rect = el.getBoundingClientRect();
                            if (rect.width > 0 and rect.height > 0) {
                                el.click();
                                return True;
                            }
                        }
                    }
                }
                return false;
            }
        """)
        if clicked:
            return True
    except Exception:
        pass
    generic_selectors = [
        "button[type='submit']", "input[type='submit']", ".btn-submit", ".apply-btn",
        ".submit-btn", "[class*='submit' i]", "[class*='apply' i]",
    ]
    for sel in generic_selectors:
        try:
            el = await page.query_selector(sel)
            if el and await safe_is_visible(el):
                await page.evaluate("el => el.click()", el)
                return True
        except Exception:
            pass
    try:
        await page.keyboard.press("Enter")
        return True
    except Exception:
        return False


# ============================================================
# CLOUDFLARE HANDLING - AUTOMATIC HANDLING REMOVED
# ============================================================

# <<< PATCH START: CF_HANDLING_REMOVED
# (ĐÃ REMOVE) handle_cloudflare_popup: Xóa xử lý Cloudflare tự động.
# <<< PATCH END: CF_HANDLING_REMOVED

# <<< PATCH START: CF_MANUAL_REMOVED
# (ĐÃ REMOVE) wait_for_manual_cf_verification: Bot không chờ lệnh Telegram nữa.
# Bạn sẽ xác minh Cloudflare thủ công trên trình duyệt khi watchdog báo.
# <<< PATCH END: CF_MANUAL_REMOVED

# <<< PATCH START: CF_COMMAND_REMOVED
# (ĐÃ REMOVE) Telegram /cf_verified handler - không dùng nữa.
# <<< PATCH END: CF_COMMAND_REMOVED


# <<< PATCH START: CF_DETECT
async def detect_cloudflare_on_page(page) -> bool:
    """Trả về True nếu phát hiện Cloudflare (iframe/modal/URL). CHỈ PHÁT HIỆN."""
    try:
        try:
            url = (page.url or "").lower()
            if "challenges.cloudflare.com" in url or "/cdn-cgi/challenge-platform" in url:
                return True
        except Exception:
            pass
        for sel in ["text=Mã xác thực", "text=MÃ XÁC THỰC", "h3:has-text('xác thực')"]:
            try:
                el = await page.query_selector(sel)
                if el and await safe_is_visible(el):
                    return True
            except Exception:
                pass
        cf_selectors = [
            "iframe[src*='turnstile']",
            "iframe[src*='challenges.cloudflare.com']",
            ".cf-turnstile",
            "[data-sitekey]",
        ]
        for sel in cf_selectors:
            try:
                el = await page.query_selector(sel)
                if el and await safe_is_visible(el):
                    return True
            except Exception:
                pass
        return False
    except Exception:
        return False
# <<< PATCH END: CF_DETECT


# ============================================================
# RESULT DETECTION
# ============================================================


async def _fetch_element_text(page, selector: str) -> str:
    try:
        elements = await page.query_selector_all(selector)
        texts = []
        for el in elements:
            try:
                text = await el.inner_text(timeout=300)
                if text and text.strip():
                    texts.append(text.strip())
            except Exception:
                pass
        return " ".join(texts)
    except Exception:
        return ""


def _filter_nextjs_noise(text: str) -> str:
    if not text:
        return ""
    noise_markers = ["__next_f","__NEXT","self.__next","push([1,\"","\"stylesheet\"","\"link\"","webpack","hydrat","\"rel\":","\"href\":","::[[[\"$\""]]
    t = text.strip()
    for marker in noise_markers:
        if marker in t:
            return ""
    if t.startswith(('{"','[["','[[["','self.')):
        return ""
    return t


async def detect_result_text(page) -> str:
    PRIORITY_SELECTORS = [
        ".swal2-html-container", ".swal2-title", ".swal2-popup",
        "div[class*='popup'] p", "div[class*='modal'] p", "div[class*='dialog'] p",
        "div[class*='alert'] p", "div[class*='notice'] p", "div[class*='message'] p",
        ".text-red-600", ".text-green-600", ".text-yellow-600",
    ]
    for sel in PRIORITY_SELECTORS:
        try:
            txt = await _fetch_element_text(page, sel)
            if txt and len(txt.strip()) >= 3:
                clean = _filter_nextjs_noise(txt.strip())
                if clean:
                    return clean
        except Exception:
            pass
    result_selectors = [
        ".text-red-600", ".text-green-600", "p.mt-1.text.sm",
    ]
    tasks = [_fetch_element_text(page, sel) for sel in result_selectors]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    combined = ""
    for r in results:
        if isinstance(r, str) and r.strip():
            filtered = _filter_nextjs_noise(r.strip())
            if filtered:
                combined += filtered + " "
    if len(combined.strip()) >= 3:
        return combined.strip()
    try:
        page_text = await page.evaluate("""
            () => {
                const keywords = [
                    'thành công', 'thanh cong', 'thất bại', 'that bai',
                    'sai', 'lỗi', 'loi', 'đã sử dụng', 'da su dung',
                    'success', 'failed', 'error', 'invalid', 'used',
                    'không hợp lệ', 'khong hop le', 'hết hạn', 'het han',
                    'không đúng', 'không tồn tại',
                ];
                const noisePatterns = ['__next_f', '__NEXT', 'self.__next', 'push([', 'webpack'];
                const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
                let node;
                while (node = walker.nextNode()) {
                    const parent = node.parentElement;
                    if (!parent) continue;
                    const tag = parent.tagName || '';
                    if (['SCRIPT','STYLE','NOSCRIPT'].includes(tag)) continue;
                    const txt = (node.textContent || '').trim();
                    if (txt.length < 3) continue;
                    if (noisePatterns.some(p => txt.includes(p))) continue;
                    const lower = txt.toLowerCase();
                    if (keywords.some(k => lower.includes(k))) return txt;
                }
                return '';
            }
        """)
        if page_text:
            clean = _filter_nextjs_noise(page_text)
            if clean:
                return clean
    except Exception:
        pass
    return ""


async def take_result_screenshot(page, user: str, code: str, target_url: str, status: str) -> str:
    if not bool(getattr(Config, "SCREENSHOT_ON_UNKNOWN", False)):
        return ""
    try:
        shot_dir = Path("logs/screenshots")
        shot_dir.mkdir(parents=True, exist_ok=True)
        safe_domain = normalize_domain(target_url).replace(".", "_").replace("/", "_")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = shot_dir / f"{safe_domain}_{user}_{code}_{status}_{ts}.png"
        await page.screenshot(path=str(path), full_page=False)
        return str(path)
    except Exception as e:
        logger.debug(f"⚠️ Cannot take screenshot: {e}")
        return ""


async def connect_to_cdp_port(port: int):
    if port in bot_state.connected_browsers:
        return bot_state.connected_browsers[port]
    logger.info(f"🖥️ Connecting to CDP port {port}...")
    browser = await bot_state.playwright_instance.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
    bot_state.connected_browsers[port] = browser
    logger.info(f"✅ Connected to CDP port {port}")
    return browser


async def _setup_page_performance(page, label: str = ""):
    _BLOCK_DOMAINS = ("google-analytics","googletagmanager","doubleclick","facebook.net","fbcdn.net","hotjar",)
    _BLOCK_TYPES = ("media", "ping")
    async def _handle_route(route):
        req = route.request
        url = req.url.lower()
        rtype = req.resource_type
        if "cloudflare" in url:
            await route.continue_()
            return
        if any(d in url for d in _BLOCK_DOMAINS):
            await route.abort()
            return
        if rtype in _BLOCK_TYPES:
            await route.abort()
            return
        await route.continue_()
    try:
        await page.route("**/*", _handle_route)
    except Exception as e:
        logger.debug(f"⚠️ [{label}] Cannot setup route: {e}")


async def _close_unwanted_popups(page):
    try:
        closed = await page.evaluate("""
            () => {
                const CLOSE_KEYWORDS = ['đóng','close','x','cancel','hủy','dismiss','got it','ok','thoát'];
                const SKIP_TEXT = ['xác thực','xac thuc','submit','kiểm tra','áp dụng','nhận'];
                const OVERLAY_SEL = ['.modal','[class*="modal" i]','[class*="popup" i]','[class*="overlay" i]','[class*="dialog" i]','[class*="notification" i]','[class*="toast" i]','[class*="alert" i]:not(.alert-success):not(.alert-info)','[class*="banner" i]','[class*="announcement" i]'];
                let count = 0;
                for (const sel of OVERLAY_SEL) {
                    const els = [...document.querySelectorAll(sel)];
                    for (const el of els) {
                        const style = window.getComputedStyle(el);
                        if (style.display === 'none' || style.visibility === 'hidden') continue;
                        const rect = el.getBoundingClientRect();
                        if (rect.width === 0 || rect.height === 0) continue;
                        const btns = [...el.querySelectorAll('button, [role="button"], a, span')];
                        for (const btn of btns) {
                            const txt = (btn.innerText || btn.textContent || btn.getAttribute('aria-label') || '').trim().toLowerCase();
                            if (SKIP_TEXT.some(s => txt.includes(s))) continue;
                            if (['đóng','close','x','cancel','hủy','dismiss'].some(k => txt === k or txt.startswith(k))) {
                                btn.click();
                                count++;
                                break;
                            }
                        }
                    }
                }
                return count;
            }
        """)
        if closed and closed > 0:
            logger.debug(f"🧹 Đóng {closed} popup không mong muốn")
            await asyncio.sleep(0.3)
    except Exception:
        pass


async def _wake_tab_for_submit(page):
    try:
        await page.evaluate("""
            Object.defineProperty(document, 'visibilityState', {get: () => 'visible', configurable: true});
        """)
        await _close_unwanted_popups(page)
    except Exception:
        pass


async def auto_fill_username_on_startup(page, domain: str, username: str):
    try:
        await scroll_to_input_fields(page)
        await asyncio.sleep(0.3)
        username_input, _ = await find_input_fields(page, cache_key=None)
        if not username_input:
            return False
        current_value = await get_input_value(username_input)
        if current_value.lower() == username.lower():
            return True
        if current_value == "":
            await username_input.fill(username)
            logger.info(f"✅ [{domain}] Filled username: {username}")
            return True
        return False
    except Exception as e:
        logger.warning(f"⚠️ [{domain}] Cannot fill username: {e}")
        return False


async def _setup_one_domain_tab(item: dict, assigned_pages: set, assign_lock: asyncio.Lock):
    label = item.get("key", item["domain"])
    try:
        return await asyncio.wait_for(_setup_one_domain_tab_inner(item, assigned_pages, assign_lock), timeout=20.0)
    except asyncio.TimeoutError:
        logger.warning(f"⏰ [{label}] Setup timeout 20s")
        return False
    except Exception as e:
        logger.error(f"❌ [{label}] Setup error: {e}")
        return False


async def _setup_one_domain_tab_inner(item: dict, assigned_pages: set, assign_lock: asyncio.Lock):
    target_url = item[[... truncated for brevity ...]]