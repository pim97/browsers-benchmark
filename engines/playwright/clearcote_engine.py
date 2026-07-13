import asyncio
import glob
import os
import shutil
import tempfile
from typing import Dict, Optional, Literal

import psutil
from clearcote.async_api import launch as clearcote_launch

from config.settings import settings
from engines.playwright_base import PlaywrightBase
from utils.js_script import load_js_script
from utils.process import find_new_child_processes

_FRESH_PRO_EXE = None


def _ensure_fresh_pro_binary() -> Optional[str]:
    """Windows: the clearcote async launch of the PRO build hits a poisoned SxS
    activation context ("spawn UNKNOWN") that same-path retries never clear — the
    sync path recovers by relaunching from a pristine copy on a fresh temp path
    (clearcote._win_av_retry). The async path has no such recovery, so we do it here:
    copy the cached PRO browser dir to a fresh path once and launch from there.

    Returns the fresh chrome.exe path, or None if no PRO build is cached (falls back
    to version-based launch, e.g. on non-Windows)."""
    global _FRESH_PRO_EXE
    if _FRESH_PRO_EXE and os.path.exists(_FRESH_PRO_EXE):
        return _FRESH_PRO_EXE
    if os.name != "nt":
        return None
    cache = os.path.join(os.environ.get("LOCALAPPDATA", ""), "clearcote", "Cache")
    cands = sorted(glob.glob(os.path.join(cache, "pro-*", "browser", "chrome.exe")))
    if not cands:
        return None
    src_dir = os.path.dirname(cands[-1])  # newest PRO build
    dst_dir = os.path.join(tempfile.gettempdir(), "clearcote_bench_fresh", os.path.basename(os.path.dirname(src_dir)), "browser")
    exe = os.path.join(dst_dir, "chrome.exe")
    if not os.path.exists(exe):
        os.makedirs(os.path.dirname(dst_dir), exist_ok=True)
        shutil.copytree(src_dir, dst_dir)
    _FRESH_PRO_EXE = exe
    return exe


class ClearcoteEngine(PlaywrightBase):
    def __init__(
            self,
            name: str = "clearcote",

            user_agent: Optional[str] = None,
            headless: bool = True,

            proxy: Optional[Dict[str, str]] = None,
            **kwargs
    ):
        """
        Initialize the ClearcoteEngine.

        Clearcote is a Playwright drop-in stealth Chromium (a cloakbrowser-style fork).
        Uses version="150" (matches current stable Chrome TLS/UA/JA4), the real GPU
        (disable_gpu_fingerprint), and --disable-fingerprint-noise so the canvas/WebGL
        readback matches genuine hardware.

        :param name: Name of the engine instance
        :param user_agent: Custom user agent string
        :param headless: Whether to run the browser in headless mode
        :param proxy: Proxy settings, if any
        """
        browser_type: Literal['chromium', 'firefox', 'webkit'] = 'chromium'
        super().__init__(name, browser_type, user_agent, headless, proxy)

    async def start(self) -> None:
        """Initialize and start the browser"""

        self._start_time = asyncio.get_event_loop().time()

        # get processes before browser is started
        parent_process = psutil.Process(os.getpid())
        process_children_before = parent_process.children(recursive=True)

        proxy = None
        if self.proxy:
            proxy = {
                "server": f"{self.proxy['protocol']}://{self.proxy['host']}:{self.proxy['port']}",
            }
            proxy["bypass"] = "google.com,localhost"

            if "username" in self.proxy and "password" in self.proxy:
                proxy["username"] = self.proxy["username"]
                proxy["password"] = self.proxy["password"]

        # configure browser options (best-fingerprint config)
        browser_options = {
            "headless": self.headless,
            "version": os.environ.get("CLEARCOTE_VERSION", "150"),
            "brand": "Chrome",
            "platform": "windows",
            "disable_gpu_fingerprint": True,
            "geoip": bool(proxy),
            "args": ["--disable-fingerprint-noise", "--window-size=1366,768"],
        }

        license_key = os.environ.get("CLEARCOTE_LICENSE_KEY")
        if license_key:
            browser_options["license_key"] = license_key

        # allow pinning a local binary (skips catalog download / version selection)
        exe = os.environ.get("CLEARCOTE_BINARY")
        if not exe:
            # Windows PRO async-launch SxS workaround: launch from a fresh-path copy.
            exe = await asyncio.to_thread(_ensure_fresh_pro_binary)
        if exe:
            browser_options["executable_path"] = exe
            browser_options.pop("version", None)

        if proxy:
            browser_options["proxy"] = proxy

        self.browser = await clearcote_launch(**browser_options)

        # configure context options
        context_options = {"ignore_https_errors": True, "no_viewport": True}

        if self.user_agent:
            context_options["user_agent"] = self.user_agent

        # create context and page
        self.context = await self.browser.new_context(**context_options)
        self.page = await self.context.new_page()

        self.page.set_default_timeout(settings.browser.action_timeout_s * 1000)
        self.page.set_default_navigation_timeout(settings.browser.page_load_timeout_s * 1000)

        # monkey-patch attachShadow to force open mode for closed shadow DOM
        await self.context.add_init_script(await load_js_script('unlockShadowDom.js'))

        # track process for resource usage
        process_children_after = parent_process.children(recursive=True)
        process_children_filtered = find_new_child_processes(process_children_before, process_children_after)
        self.process_list = process_children_filtered
