"""
🤖 BOT v7.4 - CLOUDFLARE REMOVED
(Xóa hết xử lý Cloudflare tự động - chỉ giữ watchdog hiển thị)

MAJOR CHANGES:
  ✅ XÓA: handle_cloudflare_popup() - xử lý tự động CF
  ✅ XÓA: Tất cả code liên quan đến verify/click nút Cloudflare
  ✅ XÓA: maybe_bring_to_front() - không cần đem tab lên
  ✅ GIỮA: cloudflare_watchdog() - hiển thị cửa sổ cần verify
  ✅ GIỮA: scroll_to_input_fields() - để thấy input
  ✅ Tự động BỎ QUA tin khi gặp Cloudflare
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
        # 🆕 Theo dõi cửa sổ cần xác minh CF
        self._cf_blocked_pages: dict = {}  # key → (page, timestamp, domain)


bot_state = BotState()

# ✅ SET BOT_START_TIME NGAY TẬP ĐẦU (trước khi nhận tin)
BOT_START_TIME = datetime.now(timezone.utc)

# Telegram client - optimized for non-blocking
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
    """Normalize URL to domain."""
    parsed = urlparse(url or "")
    domain = parsed.netloc or parsed.path
    return domain.lower().replace("www.", "").strip("/")


def select_random_code(codes: list) -> str:
    """Select random code from list."""
    if not codes:
        return None
    return random.choice(codes)


def _today_str() -> str:
    """Get today's date string (YYYY-MM-DD)."""
    return datetime.now().strftime("%Y-%m-%d")


# Code history logging
CODE_HISTORY_DIR = Path("logs/code_history")
CODE_HISTORY_DIR.mkdir(parents=True, exist_ok=True)


def _write_history_row(row: dict):
    """Write one row to CSV and JSONL."""
    try:
        fieldnames = [
            "time",
            "event_type",
            "channel",
            "site",
            "account",
            "code",
            "source",
            "status",
            "telegram_delay",
            "submit_elapsed",
            "message",
            "screenshot",
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
    """Background worker for history writing."""
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
    """Start background history writer task."""
    global _history_queue, _history_writer_task
    _history_queue = asyncio.Queue(maxsize=2000)
    _history_writer_task = asyncio.create_task(_history_writer_loop())
    logger.info("✅ Background history writer started")


def get_submit_semaphore() -> asyncio.Semaphore:
    """Get or create GLOBAL submit semaphore (legacy fallback)."""
    global _submit_semaphore
    if _submit_semaphore is None:
        limit = max(1, int(getattr(Config, "MAX_CONCURRENT_SUBMITS", 2)))
        _submit_semaphore = asyncio.Semaphore(limit)
    return _submit_semaphore


def get_domain_semaphore(domain: str) -> asyncio.Semaphore:
    """
    Semaphore RIÊNG cho mỗi domain → các domain khác nhau submit SONG SONG
    hoàn toàn, không phải xếp hàng chờ chung 1 semaphore global.
    """
    global _domain_semaphores
    if domain not in _domain_semaphores:
        limit = max(1, int(getattr(Config, "MAX_CONCURRENT_SUBMITS_PER_DOMAIN", 2)))
        _domain_semaphores[domain] = asyncio.Semaphore(limit)
    return _domain_semaphores[domain]


def append_code_history(
    event_type: str,
    code: str = "",
    target_url: str = "",
    account: str = "",
    channel: str = "",
    source: str = "",
    status: str = "",
    telegram_delay=None,
    submit_elapsed=None,
    message: str = "",
    screenshot: str = "",
):
    """Queue code history entry."""
    try:
        row = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "event_type": event_type,
            "channel": channel or "",
            "site": normalize_domain(target_url),
            "account": account or "",
            "code": str(code or ""),
            "source": source or "",
            "status": status or "",
            "telegram_delay": (
                "" if telegram_delay is None else f"{float(telegram_delay):.2f}"
            ),
            "submit_elapsed": (
                "" if submit_elapsed is None else f"{float(submit_elapsed):.2f}"
            ),
            "message": str(message or "").replace("\n", " ")[:300],
            "screenshot": str(screenshot or ""),
        }
        if _history_queue is not None:
            try:
                _history_queue.put_nowait(row)
            except asyncio.QueueFull:
                logger.debug("⚠️ History queue full")
        else:
            _write_history_row(row)
        return row
    except Exception as e:
        logger.debug(f"⚠️ Cannot enqueue code history: {e}")
        return None


# ============================================================
# DEDUPLICATION
# ============================================================


def _prune_site_code_seen():
    """Clean expired entries from dedup cache."""
    ttl = float(getattr(Config, "SITE_CODE_DEDUP_TTL", 10.0))
    now = time.time()
    expired = [k for k, ts in bot_state._site_code_seen.items() if now - ts > ttl]
    for k in expired:
        del bot_state._site_code_seen[k]


async def _cleanup_scheduler():
    """Periodically clean up memory caches."""
    while bot_state.is_running:
        try:
            await asyncio.sleep(
                float(getattr(Config, "INPUT_CACHE_CLEANUP_INTERVAL", 300))
            )
            _prune_site_code_seen()
            expired = await _cleanup_pending_images()
            if expired:
                logger.info(f"🧹 Cleaned {expired} expired pending image(s)")
            else:
                logger.debug("🧹 Cleanup done")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.debug(f"⚠️ Cleanup error: {e}")


def is_site_code_duplicate(domain: str, code: str) -> bool:
    """Check if code was recently submitted to this domain."""
    ttl = float(getattr(Config, "SITE_CODE_DEDUP_TTL", 10.0))
    now = time.time()
    _prune_site_code_seen()
    key = (domain, code.upper())
    seen_at = bot_state._site_code_seen.get(key)
    if seen_at is not None and now - seen_at < ttl:
        return True
    bot_state._site_code_seen[key] = now
    return False


def build_daily_summary():
    """Build end-of-day summary report."""
    try:
        csv_path = CODE_HISTORY_DIR / f"code_history_{_today_str()}.csv"
        if not csv_path.exists():
            return None

        summary = {}
        with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("event_type") != "RESULT":
                    continue
                key = (row.get("site", ""), row.get("account", ""))
                if key not in summary:
                    summary[key] = {"SUCCESS": 0, "FAILED": 0, "UNKNOWN": 0}
                status = row.get("status") or "UNKNOWN"
                summary[key].setdefault(status, 0)
                summary[key][status] += 1

        out_path = CODE_HISTORY_DIR / f"daily_summary_{_today_str()}.csv"
        with out_path.open("w", newline="", encoding="utf-8-sig") as f:
            fieldnames = [
                "date",
                "site",
                "account",
                "success",
                "failed",
                "unknown",
                "total",
            ]
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for (site, account), counts in sorted(summary.items()):
                s = counts.get("SUCCESS", 0)
                fa = counts.get("FAILED", 0)
                u = counts.get("UNKNOWN", 0)
                writer.writerow(
                    {
                        "date": _today_str(),
                        "site": site,
                        "account": account,
                        "success": s,
                        "failed": fa,
                        "unknown": u,
                        "total": s + fa + u,
                    }
                )
        logger.info(f"📒 Daily summary: {out_path}")
        return str(out_path)
    except Exception as e:
        logger.warning(f"⚠️ Cannot create daily summary: {e}")
        return None


def measure_telegram_delay_fast(msg_timestamp) -> float | None:
    """Measure Telegram message delay."""
    try:
        if msg_timestamp.tzinfo is None:
            msg_timestamp = msg_timestamp.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - msg_timestamp).total_seconds()
    except Exception:
        return None


def build_unique_account_targets():
    """Build list of unique domain targets with accounts."""
    items = []
    seen_domains = set()

    sorted_channels = sorted(
        Config.CHANNEL_CONFIG.items(),
        key=lambda item: item[1].get("priority", 999),
    )

    for chat_id, channel_config in sorted_channels:
        target_url = channel_config["url"]
        domain = normalize_domain(target_url)

        if domain in seen_domains:
            continue
        seen_domains.add(domain)

        accounts = channel_config.get("accounts", [])
        if not accounts:
            continue

        first_account = sorted(accounts, key=lambda a: a.get("priority", 999))[0]

        if domain == "mm88code.com" and len(accounts) >= 2:
            sorted_accounts = sorted(accounts, key=lambda a: a.get("priority", 999))
            for acc in sorted_accounts:
                items.append(
                    {
                        "chat_id": chat_id,
                        "channel_name": channel_config.get("name", ""),
                        "target_url": target_url,
                        "domain": domain,
                        "key": f"{domain}|{acc['username']}",
                        "port": get_user_port(acc["username"]),
                        "accounts": [acc],
                    }
                )
            continue

        port = get_user_port(first_account["username"])

        items.append(
            {
                "chat_id": chat_id,
                "channel_name": channel_config.get("name", ""),
                "target_url": target_url,
                "domain": domain,
                "key": domain,
                "port": port,
                "accounts": sorted(accounts, key=lambda a: a.get("priority", 999)),
            }
        )

    return items


def get_user_port(user: str) -> int:
    """Get CDP port for user."""
    for port, users_list in getattr(Config, "CDP_CONNECTIONS", {}).items():
        if user in users_list:
            return int(port)
    return 9222


def get_default_account_for_domain(domain_key: str) -> str | None:
    """Get default account for domain (watchdog use)."""
    if "|" in domain_key:
        domain, user = domain_key.split("|", 1)
        return user
    domain = domain_key
    for chat_id, cfg in Config.CHANNEL_CONFIG.items():
        if normalize_domain(cfg["url"]) == domain:
            accounts = cfg.get("accounts", [])
            if accounts:
                return sorted(accounts, key=lambda a: a.get("priority", 999))[0][
                    "username"
                ]
    return None


# ============================================================
# BROWSER INITIALIZATION
# ============================================================


async def verify_telegram_session():
    """Verify Telegram session is valid."""
    logger.info("\n" + "=" * 70)
    logger.info("🔐 VERIFYING TELEGRAM SESSION...")
    try:
        me = await client.get_me()
        dc_id = client.session.dc_id
        dc_names = {
            1: "DC1 Miami 🇺🇸",
            2: "DC2 Amsterdam 🇳🇱",
            3: "DC3 Miami 🇺🇸",
            4: "DC4 Amsterdam 🇳🇱",
            5: "DC5 Singapore 🇸🇬",
        }
        dc_label = dc_names.get(dc_id, f"DC{dc_id} Unknown")
        logger.info(f"✅ SESSION VALID! @{me.username} (ID: {me.id})")
        logger.info(
            f"📡 Telegram DC: {dc_label} — {'✅ Tốt cho VN' if dc_id == 5 else '⚠️ Xa VN, có thể delay'}"
        )
        return True
    except Exception as e:
        logger.error(f"❌ SESSION ERROR: {e}")
        return False


async def verify_channels_and_get_ids():
    """Verify all configured channels are accessible."""
    logger.info("\n" + "=" * 70)
    logger.info("📡 VERIFYING CHANNELS...")
    valid_channels = {}
    my_dialogs = {dialog.id: dialog async for dialog in client.iter_dialogs()}

    for chat_id, channel_config in Config.CHANNEL_CONFIG.items():
        if chat_id in my_dialogs:
            logger.info(f"✅ VALID: {channel_config['name']}")
            valid_channels[chat_id] = channel_config
        else:
            logger.warning(f"❌ NOT JOINED: {channel_config['name']}")

    return valid_channels


async def init_systems():
    """Initialize all systems."""
    print_version_info()
    db = init_database(Config.DATABASE_PATH)
    anti_det = init_anti_detection()
    _, _, perf_mon = init_monitoring()

    bot_state.playwright_instance = await async_playwright().start()
    get_shutdown_handler().setup(bot_state)

    start_history_writer()

    return {
        "db": db,
        "anti_detection": anti_det,
        "performance_monitor": perf_mon,
    }


async def safe_is_visible(element) -> bool:
    """Safely check if element is visible."""
    try:
        return await element.is_visible()
    except Exception:
        return False


def _invalidate_input_cache(key: str):
    """Invalidate input field cache."""
    bot_state._input_cache.pop(key, None)


async def find_input_fields(page, cache_key: str = None):
    """Find username and code input fields with caching."""
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
        "#account-code",
        "#username-input",
        "#ten_tai_khoan",
        "input#username",
        "input[name='username']",
        "input[placeholder*='người dùng' i]",
        "input[placeholder*='tên' i]",
        "input[placeholder*='tài' i]",
        "input[placeholder*='tài khoản' i]",
        "input[placeholder*='user' i]",
        "input[placeholder*='đăng nhập' i]",
        "input[name='ten_tai_khoan']",
        "input[id='username']",
        "input[type='text']",
    ]

    code_selectors = [
        "#promo-code",
        "#giftcode-input",
        "input[autocomplete='one-time-code']",
        "input#code",
        "input[name='code']",
        "input[placeholder*='mã code' i]",
        "input[placeholder*='code' i]",
        "input[placeholder*='mã' i]",
        "input[name='giftcode']",
        "input[id='code']",
        "input[id*='code' i]",
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
                "input:not([type='hidden']):not([type='checkbox'])"
                ":not([type='radio']):not([type='submit'])"
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


# ============================================================
# SCROLL TO INPUT FIELDS
# ============================================================


async def scroll_to_input_fields(page):
    """Cuộn tới input field để hiển thị."""
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
            logger.warning(
                "⚠️ scroll_to_input_fields: không tìm thấy input nào trên trang"
            )
        return found
    except Exception as e:
        logger.debug(f"⚠️ Scroll error: {e}")
        return False


async def get_input_value(input_element) -> str:
    """Get current value from input element."""
    try:
        return (await input_element.input_value(timeout=1000)).strip()
    except Exception:
        return ""


# ============================================================
# SUBMIT BUTTON CLICKING
# ============================================================


async def click_submit_fast(page, domain: str = "") -> bool:
    """Click submit button with domain-specific selectors first."""
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
                            if (rect.width > 0 && rect.height > 0) {{
                                btn.click();
                                return true;
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
                    'kiểm tra ngay', 'kiem tra ngay',
                    'kiểm tra', 'kiem tra',
                    'nhận code', 'nhan code',
                    'nhận ngay', 'nhan ngay',
                    'áp dụng', 'ap dung',
                    'đổi code', 'doi code',
                    'nạp code', 'nap code',
                    'gửi', 'gui',
                    'submit', 'apply'
                ];
                const EXCLUDE = /menu|nav|home|close|cancel|toggle|hamburger|back|trở về|huỷ|hủy|đóng|xác thực|xac thuc|verify|check/i;
                const els = [...document.querySelectorAll(
                    'button, a[role="button"], div[role="button"], span[role="button"], input[type="button"], input[type="submit"]'
                )];
                for (const kw of keywords) {
                    for (const el of els) {
                        if (el.disabled) continue;
                        const aria = (el.getAttribute('aria-label') || '').toLowerCase();
                        const img = el.querySelector('img[alt]');
                        const imgAlt = img ? (img.getAttribute('alt') || '').toLowerCase() : '';
                        const txt = (el.innerText || el.textContent || el.value || '').toLowerCase().trim();
                        if (EXCLUDE.test(aria + txt)) continue;
                        if ([txt, aria, imgAlt].some(s => s && s.includes(kw))) {
                            const rect = el.getBoundingClientRect();
                            if (rect.width > 0 && rect.height > 0) {
                                el.click();
                                return true;
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
        "button[type='submit']",
        "input[type='submit']",
        ".btn-submit",
        ".apply-btn",
        ".submit-btn",
        "[class*='submit' i]",
        "[class*='apply' i]",
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
# ❌ XÓA: CLOUDFLARE HANDLING
# ============================================================
# ✅ Tất cả xử lý Cloudflare tự động đã bị XÓA
# ✅ Chỉ giữa watchdog để hiển thị cửa sổ cần verify


async def detect_cloudflare_on_page(page) -> bool:
    """
    🆕 ĐƠNGIẢN: Chỉ phát hiện Cloudflare, không xử lý
    
    Return: True nếu phát hiện CF, False nếu không
    """
    try:
        # Kiểm tra URL
        try:
            url = page.url.lower()
            if "challenges.cloudflare.com" in url or "/cdn-cgi/challenge-platform" in url:
                return True
        except Exception:
            pass

        # Kiểm tra modal "Mã xác thực"
        for sel in ["text=Mã xác thực", "text=MÃ XÁC THỰC", "h3:has-text('xác thực')"]:
            try:
                el = await page.query_selector(sel)
                if el and await safe_is_visible(el):
                    return True
            except Exception:
                pass

        # Kiểm tra Turnstile iframe
        cf_selectors = [
            "iframe[src*='challenges.cloudflare.com']",
            "iframe[src*='turnstile']",
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


# ============================================================
# RESULT DETECTION
# ============================================================


async def _fetch_element_text(page, selector: str) -> str:
    """Fetch text from element(s)."""
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
    """Filter Next.js hydration noise."""
    if not text:
        return ""
    noise_markers = [
        "__next_f",
        "__NEXT",
        "self.__next",
        'push([1,"',
        '"stylesheet"',
        '"link"',
        "webpack",
        "hydrat",
        '"rel":',
        '"href":',
        ':[[["$"',
    ]
    t = text.strip()
    for marker in noise_markers:
        if marker in t:
            return ""
    if t.startswith(('{"', '[["', '[[["', "self.")):
        return ""
    return t


async def detect_result_text(page) -> str:
    """Detect result text on page."""
    PRIORITY_SELECTORS = [
        ".swal2-html-container",
        ".swal2-title",
        ".swal2-popup",
        "div[class*='popup'] p",
        "div[class*='modal'] p",
        "div[class*='dialog'] p",
        "div[class*='alert'] p",
        "div[class*='notice'] p",
        "div[class*='message'] p",
        ".text-red-600",
        ".text-green-600",
        ".text-yellow-600",
        ".text-red-500",
        ".text-green-500",
        "p.mt-1.text-sm",
        "div[class*='rounded-2xl'] p",
        "div[class*='rounded-xl'] p",
        "div[class*='rounded-lg'] p",
        "[role='alert']",
        "[role='status']",
        "[role='dialog']",
        "div[style*='position: fixed'] p",
        "div[style*='position:fixed'] p",
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
        ".text-red-600",
        ".text-green-600",
        "p.mt-1.text-sm",
        "div[class*='rounded-2xl'] p",
        "div[class*='rounded-xl'] p",
        "div[class*='rounded-lg'] p",
        "[role='dialog']",
        "[role='alert']",
        "[role='status']",
        ".modal-body",
        ".modal-content",
        ".popup-content",
        ".alert",
        "[class*='success']",
        "[class*='error']",
        "[class*='toast']",
        "[class*='result']",
        "[class*='notify']",
        "[class*='modal']",
        "[class*='popup']",
        "[class*='notification']",
        "div[style*='position: fixed']",
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
                const walker = document.createTreeWalker(
                    document.body, NodeFilter.SHOW_TEXT, null, false
                );
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


async def take_result_screenshot(
    page, user: str, code: str, target_url: str, status: str
) -> str:
    """Take screenshot of result."""
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
    """Connect to CDP port."""
    if port in bot_state.connected_browsers:
        return bot_state.connected_browsers[port]

    logger.info(f"🖥️ Connecting to CDP port {port}...")
    browser = await bot_state.playwright_instance.chromium.connect_over_cdp(
        f"http://127.0.0.1:{port}"
    )
    bot_state.connected_browsers[port] = browser

    logger.info(f"✅ Connected to CDP port {port}")
    return browser


async def _setup_page_performance(page, label: str = ""):
    """Optimize page performance."""
    _BLOCK_DOMAINS = (
        "google-analytics",
        "googletagmanager",
        "doubleclick",
        "facebook.net",
        "fbcdn.net",
        "hotjar",
    )
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
    """Đóng modal/popup/notification không mong muốn."""
    try:
        closed = await page.evaluate("""
            () => {
                const CLOSE_KEYWORDS = ['đóng', 'close', 'x', 'cancel', 'hủy', 'dismiss', 'got it', 'ok', 'thoát'];
                const SKIP_TEXT = ['xác thực', 'xac thuc', 'submit', 'kiểm tra', 'áp dụng', 'nhận'];
                const OVERLAY_SEL = [
                    '.modal', '[class*="modal" i]', '[class*="popup" i]',
                    '[class*="overlay" i]', '[class*="dialog" i]',
                    '[class*="notification" i]', '[class*="toast" i]',
                    '[class*="alert" i]:not(.alert-success):not(.alert-info)',
                    '[class*="banner" i]', '[class*="announcement" i]',
                ];
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
                            if (CLOSE_KEYWORDS.some(k => txt === k || txt.startsWith(k))) {
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
    """Wake up tab before submitting."""
    try:
        # ✅ Không gọi bring_to_front() nữa
        await page.evaluate("""
            Object.defineProperty(document, 'visibilityState', {
                get: () => 'visible', configurable: true
            });
        """)
        await _close_unwanted_popups(page)
    except Exception:
        pass


async def auto_fill_username_on_startup(page, domain: str, username: str):
    """Fill username on page load."""
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


async def _setup_one_domain_tab(
    item: dict, assigned_pages: set, assign_lock: asyncio.Lock
):
    """Setup one domain tab with timeout."""
    label = item.get("key", item["domain"])
    try:
        return await asyncio.wait_for(
            _setup_one_domain_tab_inner(item, assigned_pages, assign_lock),
            timeout=20.0,
        )
    except asyncio.TimeoutError:
        logger.warning(f"⏰ [{label}] Setup timeout 20s")
        return False
    except Exception as e:
        logger.error(f"❌ [{label}] Setup error: {e}")
        return False


async def _setup_one_domain_tab_inner(
    item: dict, assigned_pages: set, assign_lock: asyncio.Lock
):
    """Inner setup logic."""
    target_url = item["target_url"]
    domain = item["domain"]
    port = item["port"]
    accounts = item["accounts"]
    key = item.get("key", domain)

    browser = await connect_to_cdp_port(port)
    if not browser.contexts:
        logger.error(f"❌ [{domain}] Port {port} has no context")
        return False

    context = browser.contexts[0]
    page = None
    reason = ""

    async with assign_lock:
        for p in context.pages:
            try:
                if domain in p.url.lower() and p not in assigned_pages:
                    page = p
                    reason = "tab_existing"
                    assigned_pages.add(page)
                    break
            except Exception:
                pass

        if not page:
            if bool(getattr(Config, "AUTO_OPEN_MISSING_TABS", True)):
                page = await context.new_page()
                assigned_pages.add(page)
                reason = "tab_new"
            else:
                logger.error(f"❌ [{domain}] No tab available")
                return False

    if reason == "tab_new":
        await _setup_page_performance(page, label=domain)
        try:
            await page.goto(target_url, wait_until="domcontentloaded", timeout=10000)
            await scroll_to_input_fields(page)
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.warning(f"⚠️ [{domain}] Page load error (continuing): {e}")
    else:
        await _setup_page_performance(page, label=domain)
        await scroll_to_input_fields(page)
        await asyncio.sleep(0.5)

    bot_state.account_pages[key] = page
    bot_state.context_locks[key] = asyncio.Lock()
    bot_state.cf_verified[key] = True
    bot_state.submission_count[key] = 0

    first_account = accounts[0]["username"] if accounts else ""
    if first_account:
        await auto_fill_username_on_startup(page, key, first_account)

    _, code_input = await find_input_fields(page)

    if code_input:
        logger.info(f"✅ [{key}] Ready | acc: {[a['username'] for a in accounts]}")
    else:
        logger.warning(f"⚠️ [{key}] Code input not found")

    return True


async def preload_browsers_and_accounts():
    """Preload all browser tabs in parallel."""
    bot_state._site_code_seen.clear()
    logger.info("🧹 Cleared runtime code cache (_site_code_seen)")

    account_targets = build_unique_account_targets()
    if not account_targets:
        logger.error("❌ No channels configured")
        return

    total_tabs = len(account_targets)
    logger.info(f"🔄 Opening {total_tabs} tabs...")

    assigned_pages = set()
    assign_lock = asyncio.Lock()
    done_count = 0
    done_lock = asyncio.Lock()

    async def _setup_with_progress(item):
        nonlocal done_count
        result = await _setup_one_domain_tab(item, assigned_pages, assign_lock)
        async with done_lock:
            done_count += 1
            status = "✅" if result else "❌"
            logger.info(
                f"  {status} [{done_count}/{total_tabs}] {item.get('key', item['domain'])}"
            )
        return result

    results = await asyncio.gather(
        *[_setup_with_progress(item) for item in account_targets],
        return_exceptions=True,
    )

    ok = sum(1 for r in results if r is True)
    logger.info(f"✅ Complete: {ok}/{total_tabs} tabs ready")
    if ok < total_tabs:
        logger.warning(f"⚠️ {total_tabs - ok} tabs failed")
    logger.info("🤖 BOT RUNNING — listening to Telegram...")


# ... [TẠM THỜI CẮT - CÁC PHẦN KHÁC GIỮ NGUYÊN] ...
# 
# Các phần cần import từ main_script.py gốc:
# - CODE EXTRACTION & VALIDATION (dòng ~1400-1670)
# - SUBMIT CODE (dòng ~1676-2100)
# - OCR PROCESSING (dòng ~2296-2415)  
# - MESSAGE PROCESSING (dòng ~2459-2770)
# - MESSAGE WORKERS (dòng ~2772-2828)
# - TELEGRAM HANDLER (dòng ~2831-3104)
# - WATCHDOGS (dòng ~3107-3430)
# - MAIN (dòng ~3430+)
#
# ⚠️ CẮT ĐI:
#   - detect_cloudflare_on_page sử dụng trong submit_code_safe (thay đổi logic)
#   - maybe_bring_to_front() (xóa hoàn toàn)
#   - handle_cloudflare_popup() (xóa hoàn toàn)
