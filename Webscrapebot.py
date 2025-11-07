#!/usr/bin/env python3
"""
aa_flight_scraper.py
Enhanced AA.com flight scraper with anti-blocking measures.
Features:
- Oxylabs residential proxy with IP rotation
- Randomized headers, user agents, and delays
- Human-like behavior simulation
- Block detection with retries
- Optimized for low-traffic hours (e.g., 12:37 AM WAT)
- Compatible with GitHub Codespaces/Termux
"""

import json
import time
import random
import zipfile
import re
import os
import sys
import shutil
import tempfile
import logging
from pathlib import Path
from typing import List, Dict, Optional
from dataclasses import dataclass
from datetime import datetime, timedelta
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains
from selenium.common.exceptions import TimeoutException, WebDriverException
from bs4 import BeautifulSoup
import undetected_chromedriver as uc

try:
    from selenium_stealth import stealth
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False

# ---------- CONFIGURATION ----------
@dataclass
class ScraperConfig:
    """Scraper configuration"""
    choose_flights_url: str = os.getenv("CHOOSE_FLIGHTS_URL", "https://www.aa.com/booking/choose-flights/1?sid=cd999728-5192-4458-851b-44677700fabf")
    origin: str = os.getenv("ORIGIN", "LAX")
    destination: str = os.getenv("DEST", "JFK")
    date: str = os.getenv("DATE", (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d"))
    passengers: int = int(os.getenv("PASSENGERS", "1"))
    default_taxes: float = 5.60
    out_json: Path = Path("aa_results_oxylabs.json")
    debug_html: Path = Path("aa_debug_oxylabs.html")
    debug_screenshot: Path = Path("aa_debug_oxylabs.png")
    headless: bool = os.getenv("HEADLESS", "True").lower() == "true"
    chrome_binary: str = "/usr/bin/google-chrome-stable"
    request_timeout: int = 120  # Increased for potential blocks
    min_sleep: float = 1.5
    max_sleep: float = 7.0  # Longer delays for low-traffic stealth
    typing_min: float = 0.05
    typing_max: float = 0.2
    max_retries: int = 5  # More retries for block recovery
    requests_per_hour: int = 8  # Lower rate for low-traffic hours

@dataclass
class ProxyConfig:
    """Proxy configuration with forced residential mode"""
    user: str = os.getenv("PROXY_USER", "Ifeoluwa_GsEur")
    password: str = os.getenv("PROXY_PASS", "Ifeoluwa4291+")
    host: str = "unblock.oxylabs.io"
    port: int = 60000
    residential: bool = True  # Forced residential for IP rotation

@dataclass
class FlightData:
    """Flight data structure"""
    flight_number: str
    departure_time: str
    arrival_time: str
    points_required: int
    cash_price_usd: float
    taxes_fees_usd: float
    cpp: float
    raw_text: str

# ---------- SELECTORS ----------
class AASelectors:
    """CSS selectors for AA.com"""
    FLIGHT_BUTTONS = "button.swiper-slide"
    FLIGHT_DATE = "span.weekly-date"
    FLIGHT_POINTS = "span.price.weekly-price.award"
    FLIGHT_CASH = "span.price.weekly-price:not(.award)"
    DEPARTURE_TIME = ".departure-time, .dep-time"
    ARRIVAL_TIME = ".arrival-time, .arr-time"
    FLIGHT_NUMBER = ".flight-number, .flightNum"

# ---------- LOGGING ----------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler('aa_scraper.log', mode='a')]
)
logger = logging.getLogger(__name__)

# ---------- HELPERS ----------
def rand_sleep(a: float = None, b: float = None, config: ScraperConfig = None) -> None:
    """Random sleep with configurable bounds"""
    a = a or (config.min_sleep if config else 1.5)
    b = b or (config.max_sleep if config else 7.0)
    time.sleep(random.uniform(a, b))

def human_typing(element, text: str, config: ScraperConfig):
    """Simulate human typing with random delays"""
    for char in text:
        element.send_keys(char)
        time.sleep(random.uniform(config.typing_min, config.typing_max))

def parse_money(text: Optional[str]) -> float:
    """Parse money text to float"""
    if not text:
        return 0.0
    match = re.search(r"\$?\s*([\d,]+(?:\.\d{1,2})?)", text)
    return float(match.group(1).replace(",", "")) if match else 0.0

def parse_points(text: Optional[str]) -> int:
    """Parse points text to integer"""
    if not text:
        return 0
    text = text.strip().lower()
    if "k" in text:
        return int(float(text.replace("k", "").replace(",", "")) * 1000)
    match = re.search(r"(\d{1,3}(?:,\d{3})*)", text)
    return int(match.group(1).replace(",", "")) if match else 0

def calculate_cpp(cash: float, points: int, taxes: float) -> float:
    """Calculate cents per point"""
    return round(((cash - taxes) / points) * 100, 2) if points > 0 else 0.0

def create_proxy_auth_extension(proxy: ProxyConfig, temp_dir: str) -> Optional[Path]:
    """Create Chrome proxy authentication extension"""
    manifest = {
        "version": "1.0.0",
        "manifest_version": 2,
        "name": "proxy_auth_extension",
        "permissions": ["proxy", "tabs", "unlimitedStorage", "storage", "<all_urls>", "webRequest", "webRequestBlocking"],
        "background": {"scripts": ["background.js"]}
    }
    if proxy.residential:
        bg_script = f"""
        var config = {{mode:"pac_script", pacScript: {{data: "function FindProxyForURL(url, host) {{ return 'HTTPS {proxy.host}:{proxy.port}'; }}"}}}};
        chrome.proxy.settings.set({{value: config, scope: "regular"}}, function(){{}});
        function callbackFn(details) {{ return {{authCredentials: {{username: "{proxy.user}", password: "{proxy.password}"}}}}; }};
        chrome.webRequest.onAuthRequired.addListener(callbackFn, {{urls: ["<all_urls>"]}}, ['blocking']);
        """
    else:
        bg_script = f"""
        var config = {{mode:"fixed_servers", rules: {{singleProxy: {{scheme: "https", host: "{proxy.host}", port: {proxy.port}}}, bypassList: ["localhost"]}}}};
        chrome.proxy.settings.set({{value: config, scope: "regular"}}, function(){{}});
        function callbackFn(details) {{ return {{authCredentials: {{username: "{proxy.user}", password: "{proxy.password}"}}}}; }};
        chrome.webRequest.onAuthRequired.addListener(callbackFn, {{urls: ["<all_urls>"]}}, ['blocking']);
        """
    zip_path = Path(temp_dir) / "proxy_auth_ext.zip"
    try:
        with zipfile.ZipFile(zip_path, "w") as z:
            z.writestr("manifest.json", json.dumps(manifest))
            z.writestr("background.js", bg_script)
        logger.info(f"Proxy extension created → {zip_path} (Residential: {proxy.residential})")
        return zip_path
    except Exception as e:
        logger.error(f"Failed to create proxy extension: {e}")
        return None

# ---------- BROWSER MANAGEMENT ----------
class BrowserManager:
    """Manage browser lifecycle with anti-blocking measures"""
    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
        "Mozilla/5.0 (Android 13; Mobile; rv:109.0) Gecko/20100101 Firefox/118.0"
    ]
    ACCEPT_LANGUAGES = ["en-US,en;q=0.9", "en-GB,en;q=0.9", "es-ES,es;q=0.9", "fr-FR,fr;q=0.9"]

    def __init__(self, config: ScraperConfig, proxy: ProxyConfig):
        self.config = config
        self.proxy = proxy
        self.ext_path = None
        self.driver = None
        self.temp_dir = None
        self.request_count = 0
        self.last_request_time = time.time()
        self.use_proxy = True  # Toggle for proxy fallback

    def create_driver(self, retry_count: int = 0) -> Optional[uc.Chrome]:
        """Initialize undetected Chrome with proxy and retry on failure"""
        if retry_count >= 2:
            logger.warning("Max proxy retries reached, attempting without proxy")
            self.use_proxy = False

        options = uc.ChromeOptions()
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--ignore-certificate-errors")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--disable-blink-features=AutomationControlled")

        user_agent = random.choice(self.USER_AGENTS)
        accept_lang = random.choice(self.ACCEPT_LANGUAGES)
        options.add_argument(f"--user-agent={user_agent}")
        options.add_argument(f"--accept-language={accept_lang}")
        logger.info(f"Using user agent: {user_agent[:50]}... and language: {accept_lang}")

        if self.config.headless:
            options.add_argument("--headless=new")
            logger.info("Running in headless mode")

        if self.use_proxy:
            self.temp_dir = tempfile.mkdtemp(prefix="oxylabs_ext_")
            self.ext_path = create_proxy_auth_extension(self.proxy, self.temp_dir)
            if self.ext_path and self.ext_path.exists():
                options.add_extension(str(self.ext_path))
                logger.info("Proxy extension loaded")
            else:
                logger.error("Proxy extension creation failed, retrying without proxy")
                self.use_proxy = False

        if Path(self.config.chrome_binary).exists():
            options.binary_location = self.config.chrome_binary
        else:
            logger.warning(f"Chrome binary not found at {self.config.chrome_binary}, using default")

        logger.info("Initializing undetected ChromeDriver...")
        try:
            self.driver = uc.Chrome(options=options, use_subprocess=True)
            self.driver.set_page_load_timeout(self.config.request_timeout)
            if HAS_STEALTH:
                try:
                    stealth(self.driver, languages=[accept_lang.split(",")[0]], vendor="Google Inc.", platform="Win32",
                            webgl_vendor="Intel Inc.", renderer="Intel Iris OpenGL Engine", fix_hairline=True,
                            navigator_platform="Win32" if random.random() > 0.5 else "MacIntel")
                    logger.info("Selenium-stealth applied with randomized fingerprint")
                except Exception as e:
                    logger.warning(f"Failed to apply stealth: {e}")
            logger.info(f"Chrome version: {self.driver.capabilities['browserVersion']}")
            return self.driver
        except Exception as e:
            logger.error(f"Failed to initialize driver: {e}")
            if self.use_proxy and retry_count < 2:
                self.quit()
                return self.create_driver(retry_count + 1)
            return None

    def simulate_human_behavior(self):
        """Simulate human-like actions with frequent interactions"""
        if not self.driver:
            return
        try:
            scroll_y = random.randint(100, 700)
            self.driver.execute_script(f"window.scrollBy(0, {scroll_y});")
            rand_sleep(0.7, 2.0, self.config)
            self.driver.execute_script(f"window.scrollBy(0, -{scroll_y // 2});")
            rand_sleep(0.5, 1.5, self.config)
            elements = self.driver.find_elements(By.CSS_SELECTOR, "a, button, span, input")
            if elements:
                target = random.choice(elements[:15])
                action = ActionChains(self.driver)
                action.move_to_element(target).perform()
                if random.random() > 0.6:
                    action.click().perform()
                rand_sleep(0.3, 1.2, self.config)
        except Exception as e:
            logger.debug(f"Behavior simulation error: {e}")

    def rate_limit(self, config: ScraperConfig):
        """Enforce request rate limiting"""
        self.request_count += 1
        current_time = time.time()
        if self.request_count > config.requests_per_hour:
            elapsed = current_time - self.last_request_time
            if elapsed < 3600:  # 1 hour in seconds
                sleep_time = (3600 - elapsed) / (config.requests_per_hour - self.request_count + 1)
                logger.info(f"Rate limit hit, sleeping for {sleep_time:.1f} seconds...")
                time.sleep(sleep_time)
            self.request_count = 1
            self.last_request_time = current_time
        else:
            self.last_request_time = current_time

    def quit(self):
        """Close browser and clean up"""
        if self.driver:
            try:
                self.driver.quit()
                logger.info("Browser closed")
            except Exception as e:
                logger.error(f"Error closing browser: {e}")
        if self.temp_dir and os.path.exists(self.temp_dir):
            try:
                shutil.rmtree(self.temp_dir)
                logger.info("Temporary directory cleaned up")
            except Exception as e:
                logger.error(f"Error cleaning up temp dir: {e}")

# ---------- SCRAPING ----------
def scrape_choose_flights(browser: BrowserManager, config: ScraperConfig) -> List[FlightData]:
    """Scrape flight data with block detection and retry"""
    browser.rate_limit(config)
    logger.info(f"Navigating to: {config.choose_flights_url} at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    driver = browser.driver
    driver.get(config.choose_flights_url)

    try:
        WebDriverWait(driver, config.request_timeout).until(
            EC.presence_of_all_elements_located((By.CSS_SELECTOR, AASelectors.FLIGHT_BUTTONS))
        )
    except TimeoutException:
        logger.error(f"Timeout waiting for flights (>{config.request_timeout}s)")
        try:
            driver.save_screenshot(str(config.debug_screenshot))
            logger.info(f"Screenshot saved to {config.debug_screenshot}")
        except Exception:
            pass
        return []

    rand_sleep(3, 8, config)  # Longer initial delay for stealth
    browser.simulate_human_behavior()

    page_text = driver.page_source.lower()
    if "access denied" in page_text or "blocked" in page_text:
        logger.warning("Potential IP block detected, consider switching IP or proxy")
        driver.save_screenshot(str(config.debug_screenshot))
        return []

    html = driver.page_source
    config.debug_html.write_text(html, encoding="utf-8")
    logger.info(f"Debug HTML saved to {config.debug_html}")
    driver.save_screenshot(str(config.debug_screenshot))
    logger.info(f"Debug screenshot saved to {config.debug_screenshot}")

    soup = BeautifulSoup(html, "html.parser")
    flight_buttons = soup.select(AASelectors.FLIGHT_BUTTONS)
    logger.info(f"Found {len(flight_buttons)} flight buttons")

    flights = []
    for i, button in enumerate(flight_buttons, 1):
        try:
            date_elem = button.select_one(AASelectors.FLIGHT_DATE)
            points_elem = button.select_one(AASelectors.FLIGHT_POINTS)
            cash_elem = button.select_one(AASelectors.FLIGHT_CASH)
            dep_elem = button.select_one(AASelectors.DEPARTURE_TIME)
            arr_elem = button.select_one(AASelectors.ARRIVAL_TIME)
            fn_elem = button.select_one(AASelectors.FLIGHT_NUMBER)

            if not date_elem:
                continue

            departure_time = dep_elem.get_text(strip=True) if dep_elem else ""
            arrival_time = arr_elem.get_text(strip=True) if arr_elem else ""
            flight_number = fn_elem.get_text(strip=True) if fn_elem else f"AA_{i}"
            points = parse_points(points_elem.get_text(strip=True) if points_elem else "")
            cash = parse_money(cash_elem.get_text(strip=True) if cash_elem else "")
            cpp = calculate_cpp(cash, points, config.default_taxes)

            if cash or points:
                flights.append(FlightData(
                    flight_number=flight_number,
                    departure_time=departure_time,
                    arrival_time=arrival_time,
                    points_required=points,
                    cash_price_usd=cash,
                    taxes_fees_usd=config.default_taxes,
                    cpp=cpp,
                    raw_text=button.get_text(strip=True)[:200]
                ))
                logger.debug(f"Flight {i}: {flight_number} - ${cash} / {points} pts ({cpp} cpp)")
                rand_sleep(0.7, 2.0, config)  # Delay between flights
        except Exception as e:
            logger.warning(f"Error parsing flight {i}: {e}")
            continue

    logger.info(f"Successfully parsed {len(flights)} flights")
    return flights

# ---------- MAIN ----------
def main():
    print("=" * 70)
    print(f"AA Flight Scraper with Anti-Blocking - Started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    config = ScraperConfig()
    proxy = ProxyConfig(residential=True)
    browser = BrowserManager(config, proxy)

    try:
        if not browser.create_driver():
            logger.error("Failed to initialize browser - exiting")
            return

        flights = []
        for attempt in range(config.max_retries):
            try:
                flights = scrape_choose_flights(browser, config)
                if flights:
                    break
                if attempt < config.max_retries - 1:
                    retry_delay = 2 ** attempt * 5  # Longer backoff for blocks
                    logger.info(f"No flights or block detected, retrying in {retry_delay} seconds... (Attempt {attempt + 1}/{config.max_retries})")
                    browser.quit()
                    browser = BrowserManager(config, proxy)  # Recreate browser with new proxy session
                    if not browser.create_driver(attempt):
                        logger.error(f"Retry {attempt + 1} failed to initialize driver")
                        continue
                    time.sleep(retry_delay)
            except (TimeoutException, WebDriverException) as e:
                logger.error(f"Network/Driver error on attempt {attempt + 1}: {e}")
                if attempt < config.max_retries - 1:
                    retry_delay = 2 ** attempt * 5
                    time.sleep(retry_delay)
                continue
        else:
            if not flights:
                logger.error("All retry attempts exhausted, IP may be permanently blocked")
                return

        if flights:
            # Deduplicate based on key fields
            seen = set()
            unique_flights = []
            for flight in flights:
                key = (flight.points_required, flight.cash_price_usd, flight.departure_time)
                if key not in seen:
                    seen.add(key)
                    unique_flights.append(flight)

            result = {
                "search_metadata": {
                    "origin": config.origin,
                    "destination": config.destination,
                    "date": config.date,
                    "passengers": config.passengers,
                    "cabin_class": "economy",
                    "scrape_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                },
                "flights": [vars(f) for f in unique_flights],
                "total_results": len(unique_flights),
                "debug": {
                    "html": str(config.debug_html.resolve()),
                    "screenshot": str(config.debug_screenshot.resolve())
                }
            }
            config.out_json.write_text(json.dumps(result, indent=2))
            logger.info(f"✓ Saved {len(unique_flights)} flights to {config.out_json}")

            # Display summary
            print(f"\n{'=' * 70}")
            print("RESULTS SUMMARY")
            print(f"{'=' * 70}")
            print(f"Route: {config.origin} → {config.destination}")
            print(f"Date: {config.date}")
            print(f"Total flights found: {len(unique_flights)}")
            print(f"\n{'Flight':<15} {'Departure':<10} {'Arrival':<10} {'Points':<10} {'Cash':<10} {'CPP':<8}")
            print("-" * 70)
            for flight in unique_flights[:10]:
                print(f"{flight.flight_number:<15} {flight.departure_time:<10} {flight.arrival_time:<10} "
                      f"{flight.points_required:<10,} ${flight.cash_price_usd:<9.2f} {flight.cpp:<8.2f}")
            if len(unique_flights) > 10:
                print(f"\n... and {len(unique_flights) - 10} more flights")
            
            # Best value
            valid_flights = [f for f in unique_flights if f.cpp > 0]
            if valid_flights:
                best = max(valid_flights, key=lambda x: x.cpp)
                print(f"\n🏆 Best value: {best.flight_number} ({best.cpp} cpp)")
        else:
            logger.warning("No flight data collected")

    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
    finally:
        browser.quit()

if __name__ == "__main__":
    main()
