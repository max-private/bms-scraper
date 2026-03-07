"""
BookMyShow Scraper
~~~~~~~~~~~~~~~~~~
Uses Playwright to navigate BookMyShow (bypassing Cloudflare) and intercepts
the internal JSON APIs to collect structured booking data:

  city | movie | cinema | show_date | show_time | language | format |
  screen_type | seat_category | price | availability | source_url | scraped_at

Endpoints discovered via reverse-engineering (Feb 2026):
  - /api/explore/v1/discover/regions              -> city list
  - /api/explore/v1/discover/movies-{slug}        -> movie list per city
  - /api/movies-data/v4/showtimes-by-event/...    -> venues + showtimes
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page, Response
from playwright_stealth import Stealth

_stealth = Stealth()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class ScrapeConfig:
    headless: bool = False          # headed helps pass Cloudflare JS challenge
    max_cities: int | None = None
    max_movies_per_city: int | None = None
    max_dates_per_movie: int | None = None  # None = all available dates
    delay: float = 1.5              # polite delay between navigations
    timeout_ms: int = 60_000
    json_out: str = "bookmyshow_booking_data.json"
    csv_out: str = "bookmyshow_booking_data.csv"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sleep(sec: float) -> None:
    """Sleep with ±30 % jitter to appear more human-like."""
    if sec > 0:
        jitter = sec * random.uniform(-0.3, 0.3)
        time.sleep(max(0.1, sec + jitter))


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------

class BookMyShowScraper:
    """Navigate BMS in a stealth Chromium browser and extract SSR data."""

    def __init__(self, cfg: ScrapeConfig) -> None:
        self.cfg = cfg

    # ---- popup / modal handling -------------------------------------------

    @staticmethod
    def _dismiss_popups(page: Page, timeout_ms: int = 3000) -> None:
        """Dismiss any blocking popup / modal that BMS may show.

        Handles:
        - Content / age-rating warnings  ("I Accept", "OK", "Continue")
        - Cookie-consent banners
        - App-install prompts ("Not now", close button)
        - Generic modals with a dismiss / close action

        Detection strategy (BMS-specific):
        BMS content-warning overlays use dynamic styled-component classes
        (e.g. ``sc-7silkt-0``) that change frequently.  The dismiss button
        may be a **sibling** of the overlay rather than a DOM child, so we:
          1) Detect any full-viewport fixed overlay (position:fixed, z >= 50)
          2) If one exists, click the first visible dismiss-like button on the
             page regardless of its DOM parentage.
          3) Fall back to checking semantic class names on ancestors.
        """
        # Step 1 – check if a full-viewport overlay is present anywhere
        has_overlay = page.evaluate(
            """() => {
                const els = document.querySelectorAll('*');
                for (const el of els) {
                    const s = window.getComputedStyle(el);
                    const z = parseInt(s.zIndex) || 0;
                    if (s.position === 'fixed' && z >= 50) {
                        const r = el.getBoundingClientRect();
                        if (r.width > window.innerWidth * 0.8 &&
                            r.height > window.innerHeight * 0.8) {
                            return true;
                        }
                    }
                }
                return false;
            }"""
        )

        # Common dismiss-button selectors, tried in order
        dismiss_selectors = [
            # Content-warning / age-gate
            "button:has-text('I Accept')",
            "button:has-text('Accept')",
            "button:has-text('I Agree')",
            "button:has-text('Okay')",
            "button:has-text('OK')",
            "button:has-text('Continue')",
            "button:has-text('Proceed')",
            "button:has-text('Got it')",
            "button:has-text('Yes')",
            # App-install / notification prompt
            "button:has-text('Not now')",
            "button:has-text('Not Now')",
            "button:has-text('No Thanks')",
            "button:has-text('Maybe later')",
            # Generic close icons inside a visible dialog
            "[role='dialog'] button[aria-label='Close']",
            "[role='dialog'] button[aria-label='close']",
            "[role='dialog'] [class*='close' i]",
            "[role='alertdialog'] button",
            # Bottom-sheet / overlay close
            "[class*='bottom-sheet' i] button[aria-label='Close']",
            "[class*='bottomSheet' i] button[aria-label='Close']",
        ]

        for sel in dismiss_selectors:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    if has_overlay:
                        # A blocking overlay is present — click the button
                        # regardless of whether it's a DOM descendant.
                        el.click()
                        _sleep(0.5)
                        return

                    # No global overlay — check if button is inside a modal
                    in_popup = el.evaluate(
                        """e => {
                            let p = e.closest(
                                '[role="dialog"], [role="alertdialog"], '
                                + '[class*="modal" i], [class*="popup" i], '
                                + '[class*="overlay" i], [class*="sheet" i], '
                                + '[class*="banner" i], [class*="consent" i], '
                                + '[class*="warning" i], [class*="advisory" i]'
                            );
                            if (p) return true;

                            let cur = e.parentElement;
                            while (cur && cur !== document.body) {
                                const s = window.getComputedStyle(cur);
                                const pos = s.position;
                                const z = parseInt(s.zIndex) || 0;
                                if ((pos === 'fixed' || pos === 'absolute') && z >= 50) {
                                    const r = cur.getBoundingClientRect();
                                    if (r.width > 300 && r.height > 200) {
                                        return true;
                                    }
                                }
                                cur = cur.parentElement;
                            }
                            return false;
                        }"""
                    )
                    if in_popup:
                        el.click()
                        _sleep(0.5)
                        return
            except Exception:
                continue

    # ---- stealth page factory --------------------------------------------

    @staticmethod
    def _new_stealth_page(ctx: BrowserContext) -> Page:
        """Create a new page with stealth patches applied."""
        page = ctx.new_page()
        _stealth.apply_stealth_sync(page)
        return page

    # ---- browser lifecycle ------------------------------------------------

    def run(self, cities_override: list[str] | None = None) -> list[dict[str, Any]]:
        all_records: list[dict[str, Any]] = []

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=self.cfg.headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=IsolateOrigins,site-per-process",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
            )
            ctx = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/134.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1366, "height": 768},
                locale="en-IN",
                timezone_id="Asia/Kolkata",
            )
            # Remove automation-giveaway headers
            ctx.set_extra_http_headers({
                "Accept-Language": "en-IN,en;q=0.9",
            })

            try:
                # Step 1 - discover cities
                if cities_override:
                    cities = [{"code": c.upper(), "slug": c.lower()} for c in cities_override]
                else:
                    cities = self._get_cities(ctx)

                if self.cfg.max_cities is not None:
                    cities = cities[: self.cfg.max_cities]

                print(f"Cities to scrape: {len(cities)}")

                # Step 2 - for each city, get movies then showtimes
                for ci, city in enumerate(cities, 1):
                    slug = city["slug"]
                    code = city["code"]
                    print(f"\n[{ci}/{len(cities)}] City: {slug} ({code})")

                    movies = self._get_movies(ctx, slug, code)
                    if self.cfg.max_movies_per_city is not None:
                        movies = movies[: self.cfg.max_movies_per_city]
                    print(f"  Movies found: {len(movies)}")

                    for mi, movie in enumerate(movies, 1):
                        print(f"  [{mi}/{len(movies)}] {movie['title']} ({movie['event_code']})")
                        records = self._get_showtimes(ctx, slug, code, movie)
                        all_records.extend(records)
                        print(f"    -> {len(records)} booking records")
                        _sleep(self.cfg.delay)

            finally:
                ctx.close()
                browser.close()

        # Deduplicate
        seen: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for r in all_records:
            key = json.dumps(r, sort_keys=True, ensure_ascii=False)
            if key not in seen:
                seen.add(key)
                deduped.append(r)
        return deduped

    # ---- Step 1: regions --------------------------------------------------

    @staticmethod
    def _safe_parse_response(resp: Response) -> Any | None:
        """Parse JSON from response body bytes."""
        try:
            raw = resp.body().decode("utf-8", errors="replace")
            if raw and (raw[0] == "{" or raw[0] == "["):
                return json.loads(raw)
        except Exception:
            pass
        return None

    def _get_cities(self, ctx: BrowserContext) -> list[dict[str, str]]:
        """Navigate to the homepage and intercept the regions API."""
        captured_regions: list[Any] = []

        def _on_resp(resp: Response) -> None:
            if "/api/explore/v1/discover/regions" in resp.url:
                body = self._safe_parse_response(resp)
                if body is not None:
                    captured_regions.append(body)

        page = self._new_stealth_page(ctx)
        page.on("response", _on_resp)
        try:
            page.goto("https://in.bookmyshow.com", timeout=self.cfg.timeout_ms)
            _sleep(5)
            self._dismiss_popups(page)
        finally:
            page.close()

        cities: list[dict[str, str]] = []
        for payload in captured_regions:
            bms = payload.get("BookMyShow", {})
            for section in ("TopCities", "OtherCities"):
                for city in bms.get(section, []):
                    code = city.get("RegionCode", "")
                    slug = city.get("RegionSlug", "")
                    if code and slug:
                        cities.append({
                            "code": code,
                            "slug": slug,
                            "name": city.get("RegionName", slug),
                        })

        print(f"  Discovered {len(cities)} cities from regions API")
        return cities

    # ---- Step 2: movie listing --------------------------------------------

    def _get_movies(
        self, ctx: BrowserContext, city_slug: str, region_code: str
    ) -> list[dict[str, str]]:
        """Navigate to the city movies page and intercept listing API."""
        captured_listings: list[Any] = []

        def _on_resp(resp: Response) -> None:
            url = resp.url.lower()
            if "/api/explore/v1/discover/movies-" in url and resp.status == 200:
                body = self._safe_parse_response(resp)
                if body is not None:
                    captured_listings.append(body)

        page = self._new_stealth_page(ctx)
        page.on("response", _on_resp)
        dom_movies: list[dict[str, str]] = []
        try:
            page.goto(
                f"https://in.bookmyshow.com/explore/movies-{city_slug}",
                timeout=self.cfg.timeout_ms,
            )
            _sleep(5)
            self._dismiss_popups(page)
            # Scroll to trigger lazy-loading pages
            for _ in range(5):
                page.mouse.wheel(0, 3000)
                _sleep(1.5)

            # DOM fallback: extract movie links directly from the page
            # Some cities render movies server-side without the listing API
            dom_movies = self._extract_movies_from_dom(page, city_slug)
        finally:
            page.close()

        # Parse movie cards from API response
        movies: list[dict[str, str]] = []
        seen_codes: set[str] = set()

        for payload in captured_listings:
            cards = self._extract_movie_cards(payload)
            for card in cards:
                ec = card.get("event_code", "")
                if ec and ec not in seen_codes:
                    seen_codes.add(ec)
                    movies.append(card)

        # If API yielded nothing, use DOM-extracted movies
        if not movies and dom_movies:
            print(f"    (using DOM fallback: {len(dom_movies)} movies from page links)")
            movies = dom_movies

        return movies

    @staticmethod
    def _extract_movies_from_dom(page: Page, city_slug: str) -> list[dict[str, str]]:
        """Extract movie event codes and titles from <a> tags in the DOM."""
        links = page.eval_on_selector_all(
            f"a[href*='/movies/{city_slug}/']",
            r"""els => els.map(e => {
                const href = e.getAttribute('href') || '';
                // Get aria-label or the first img alt as clean title
                let title = e.getAttribute('aria-label') || '';
                if (!title) {
                    const img = e.querySelector('img');
                    if (img) title = img.getAttribute('alt') || '';
                }
                if (!title) {
                    // Use the first direct child text or heading
                    const h = e.querySelector('h2, h3, h4, [class*="Title"], [class*="title"]');
                    if (h) title = h.textContent.trim();
                }
                return {href, title};
            })""",
        )
        movies: list[dict[str, str]] = []
        seen: set[str] = set()
        for link in links:
            href = link.get("href", "")
            m = re.search(r"/movies/[^/]+/([^/]+)/(ET\d{5,})", href)
            if m:
                url_slug = m.group(1)
                ec = m.group(2)
                if ec not in seen:
                    seen.add(ec)
                    # Prefer DOM-extracted title, fall back to URL slug
                    raw_title = (link.get("title") or "").strip()
                    title = raw_title if raw_title else url_slug.replace("-", " ").title()
                    movies.append({
                        "event_code": ec,
                        "title": title,
                        "url": href,
                    })
        return movies

    @staticmethod
    def _extract_movie_cards(payload: Any) -> list[dict[str, str]]:
        """Walk the listing JSON to find movie event codes and titles."""
        results: list[dict[str, str]] = []

        def _walk(node: Any) -> None:
            if isinstance(node, dict):
                # Check for movie card patterns
                url_val = node.get("url") or node.get("link") or ""
                if isinstance(url_val, str) and "/movies/" in url_val:
                    # Extract event code from URL like /movies/mumbai/sinners/ET00413379
                    m = re.search(r"/ET\d{5,}", url_val)
                    if m:
                        ec = m.group(0).lstrip("/")
                        title = (
                            node.get("title")
                            or node.get("label")
                            or node.get("name")
                            or ""
                        )
                        results.append({
                            "event_code": ec,
                            "title": title,
                            "url": url_val,
                        })

                # Also look for explicit eventCode keys
                ec2 = node.get("eventCode") or node.get("event_code") or ""
                if isinstance(ec2, str) and ec2.startswith("ET"):
                    title = (
                        node.get("eventTitle")
                        or node.get("title")
                        or node.get("name")
                        or ""
                    )
                    results.append({
                        "event_code": ec2,
                        "title": title,
                        "url": url_val,
                    })

                for v in node.values():
                    _walk(v)
            elif isinstance(node, list):
                for item in node:
                    _walk(item)

        _walk(payload)
        return results

    # ---- Step 3: showtimes per movie --------------------------------------

    @staticmethod
    def _extract_initial_state(page: Page) -> dict[str, Any] | None:
        """Extract window.__INITIAL_STATE__ from the page (BMS SSR data)."""
        try:
            state = page.evaluate("() => window.__INITIAL_STATE__")
            return state if isinstance(state, dict) else None
        except Exception:
            return None

    def _get_showtimes(
        self,
        ctx: BrowserContext,
        city_slug: str,
        region_code: str,
        movie: dict[str, str],
    ) -> list[dict[str, Any]]:
        """Navigate to the booking page and extract showtime data.

        Primary strategy: extract from window.__INITIAL_STATE__ (SSR).
        Fallback: intercept showtimes-by-event API calls.
        """
        event_code = movie["event_code"]
        movie_title = movie["title"]

        # Build a URL slug from the movie URL
        raw_url = movie.get("url", "")
        url_slug = raw_url.strip("/").split("/")[-1] if raw_url else event_code
        # Remove the event code if it was the last segment
        if url_slug == event_code and "/" in raw_url:
            parts = [p for p in raw_url.split("/") if p]
            url_slug = parts[-2] if len(parts) >= 2 else event_code

        source_url = (
            f"https://in.bookmyshow.com/movies/{city_slug}/"
            f"{url_slug}/buytickets/{event_code}"
        )

        # Also set up API interception as fallback (in case SSR extraction fails
        # or for date-tab clicks that may trigger API calls)
        captured_static: list[Any] = []
        captured_dynamic: list[Any] = []

        def _on_resp(resp: Response) -> None:
            url = resp.url
            if resp.status != 200:
                return
            if "showtimes-by-event/primary-static" in url:
                body = self._safe_parse_response(resp)
                if body is not None:
                    captured_static.append(body)
            elif "showtimes-by-event/primary-dynamic" in url:
                body = self._safe_parse_response(resp)
                if body is not None:
                    captured_dynamic.append(body)

        page = self._new_stealth_page(ctx)
        page.on("response", _on_resp)

        records: list[dict[str, Any]] = []
        try:
            # Navigate directly to the buytickets page (skips needing to click
            # "Book tickets" on the detail page)
            page.goto(source_url, timeout=self.cfg.timeout_ms)
            _sleep(5)

            # Dismiss content-warning / age-gate / consent popup
            self._dismiss_popups(page)
            _sleep(2)
            self._dismiss_popups(page)  # second pass for chained popups

            # --- Primary strategy: extract from __INITIAL_STATE__ ---
            state = self._extract_initial_state(page)
            showtime_state = (state or {}).get("showtimesByEvent", {})

            # Extract venue names from primaryStatic across all dates
            venue_names: dict[str, str] = {}
            child_events: dict[str, dict[str, str]] = {}
            already_dates: set[str] = set()

            show_dates = showtime_state.get("showDates", {})
            for date_code, date_data in show_dates.items():
                ps_data = (
                    date_data.get("primaryStatic", {})
                    .get("data", {})
                )

                # Venue names
                for vc, vinfo in ps_data.get("venues", {}).items():
                    if isinstance(vinfo, dict):
                        venue_names[vc] = vinfo.get("venueName", vc)

                # Event title (prefer API title over URL-derived)
                api_title = (
                    ps_data.get("eventData", {})
                    .get("eventTitle", "")
                )
                if api_title:
                    movie_title = api_title

                # Child events (language/format variants)
                for ce in (
                    ps_data.get("eventData", {})
                    .get("childEvents", [])
                ):
                    child_events[ce.get("eventCode", "")] = {
                        "language": ce.get("eventLanguage", ""),
                        "dimension": ce.get("eventDimension", ""),
                    }

                # Dynamic showtime data
                dy_data = (
                    date_data.get("dynamic", {})
                    .get("data", {})
                )
                if not dy_data:
                    continue

                dc = dy_data.get("additionalData", {}).get("dateCode", date_code)
                show_date = self._format_date(dc)
                already_dates.add(dc)

                widgets = dy_data.get("showtimeWidgets", [])
                records.extend(
                    self._parse_showtime_widgets(
                        widgets,
                        city_slug=city_slug,
                        movie_title=movie_title,
                        event_code=event_code,
                        show_date=show_date,
                        venue_names=venue_names,
                        child_events=child_events,
                        source_url=source_url,
                    )
                )

            # --- Fallback: use intercepted API responses if SSR yielded nothing
            if not records and captured_dynamic:
                print("    (using API interception fallback)")
                for payload in captured_static:
                    for vc, vinfo in (
                        payload.get("data", {}).get("venues", {}).items()
                    ):
                        if isinstance(vinfo, dict):
                            venue_names[vc] = vinfo.get("venueName", vc)
                    for ce in (
                        payload.get("data", {})
                        .get("eventData", {})
                        .get("childEvents", [])
                    ):
                        child_events[ce.get("eventCode", "")] = {
                            "language": ce.get("eventLanguage", ""),
                            "dimension": ce.get("eventDimension", ""),
                        }
                    api_title = (
                        payload.get("data", {})
                        .get("eventData", {})
                        .get("eventTitle", "")
                    )
                    if api_title:
                        movie_title = api_title

                for payload in captured_dynamic:
                    data = payload.get("data", {})
                    dc = data.get("additionalData", {}).get("dateCode", "")
                    show_date = self._format_date(dc)
                    already_dates.add(dc)
                    widgets = data.get("showtimeWidgets", [])
                    records.extend(
                        self._parse_showtime_widgets(
                            widgets,
                            city_slug=city_slug,
                            movie_title=movie_title,
                            event_code=event_code,
                            show_date=show_date,
                            venue_names=venue_names,
                            child_events=child_events,
                            source_url=source_url,
                        )
                    )

            # Click additional date tabs if configured
            if self.cfg.max_dates_per_movie != 1 and records:
                extra = self._scrape_additional_dates(
                    page,
                    city_slug=city_slug,
                    movie_title=movie_title,
                    event_code=event_code,
                    venue_names=venue_names,
                    child_events=child_events,
                    source_url=source_url,
                    already_scraped_dates=already_dates,
                )
                records.extend(extra)

            # --- Multi-language / format support ---
            scraped_event_codes = {event_code}
            other_variants = [
                (ec, info)
                for ec, info in child_events.items()
                if ec and ec not in scraped_event_codes
            ]
            if other_variants:
                print(f"    Found {len(other_variants)} additional language/format variant(s)")
            for variant_ec, variant_info in other_variants:
                lang = variant_info.get('language', '')
                dim = variant_info.get('dimension', '')
                print(f"      Scraping variant: {variant_ec} ({lang} {dim})")
                variant_records = self._scrape_variant(
                    page,
                    city_slug=city_slug,
                    movie_title=movie_title,
                    parent_event_code=event_code,
                    variant_event_code=variant_ec,
                    url_slug=url_slug,
                    venue_names=venue_names,
                    child_events=child_events,
                    source_url=source_url,
                )
                records.extend(variant_records)
                scraped_event_codes.add(variant_ec)

        except Exception as exc:
            print(f"    Error: {exc}")
        finally:
            page.close()

        return records

    def _scrape_variant(
        self,
        page: Page,
        *,
        city_slug: str,
        movie_title: str,
        parent_event_code: str,
        variant_event_code: str,
        url_slug: str,
        venue_names: dict[str, str],
        child_events: dict[str, dict[str, str]],
        source_url: str,
    ) -> list[dict[str, Any]]:
        """Navigate to a language/format variant's buytickets page and capture its showtimes."""
        records: list[dict[str, Any]] = []
        variant_url = (
            f"https://in.bookmyshow.com/movies/{city_slug}/"
            f"{url_slug}/buytickets/{variant_event_code}"
        )

        try:
            page.goto(variant_url, timeout=self.cfg.timeout_ms)
            _sleep(5)
            self._dismiss_popups(page)
            _sleep(2)

            # Primary: extract from __INITIAL_STATE__
            state = self._extract_initial_state(page)
            showtime_state = (state or {}).get("showtimesByEvent", {})
            show_dates = showtime_state.get("showDates", {})

            for date_code, date_data in show_dates.items():
                ps_data = (
                    date_data.get("primaryStatic", {})
                    .get("data", {})
                )
                # Merge venue names
                for vc, vinfo in ps_data.get("venues", {}).items():
                    if isinstance(vinfo, dict) and vc not in venue_names:
                        venue_names[vc] = vinfo.get("venueName", vc)

                dy_data = (
                    date_data.get("dynamic", {})
                    .get("data", {})
                )
                if not dy_data:
                    continue

                dc = dy_data.get("additionalData", {}).get("dateCode", date_code)
                show_date = self._format_date(dc)
                widgets = dy_data.get("showtimeWidgets", [])
                records.extend(
                    self._parse_showtime_widgets(
                        widgets,
                        city_slug=city_slug,
                        movie_title=movie_title,
                        event_code=variant_event_code,
                        show_date=show_date,
                        venue_names=venue_names,
                        child_events=child_events,
                        source_url=variant_url,
                    )
                )

            print(f"        -> {len(records)} records for variant {variant_event_code}")
        except Exception as exc:
            print(f"        Variant error: {exc}")

        _sleep(self.cfg.delay)
        return records

    def _scrape_additional_dates(
        self,
        page: Page,
        *,
        city_slug: str,
        movie_title: str,
        event_code: str,
        venue_names: dict[str, str],
        child_events: dict[str, dict[str, str]],
        source_url: str,
        already_scraped_dates: set[str],
    ) -> list[dict[str, Any]]:
        """Click available date tabs on the booking page and extract data.

        After clicking a date tab, BMS updates the React/Redux state.
        We re-extract __INITIAL_STATE__ to get the newly loaded date's data.
        Falls back to API interception if state extraction yields nothing.
        """
        records: list[dict[str, Any]] = []
        dates_scraped = set(already_scraped_dates)
        max_extra = (self.cfg.max_dates_per_movie or 7) - len(dates_scraped)

        # Find date selector elements (IDs are dateCodes like 20260301)
        date_elements = page.query_selector_all("[id^='2026'], [id^='2025']")
        if not date_elements:
            return records

        for el in date_elements:
            if max_extra <= 0:
                break
            date_id = el.get_attribute("id") or ""
            if date_id in dates_scraped or not date_id:
                continue

            # Set up API interception as fallback
            captured_dynamic: list[Any] = []

            def _make_handler():
                local_list = captured_dynamic

                def _on_resp(resp: Response) -> None:
                    if (
                        resp.status == 200
                        and "showtimes-by-event/primary-dynamic" in resp.url
                    ):
                        body = self._safe_parse_response(resp)
                        if body is not None:
                            local_list.append(body)

                return _on_resp

            handler = _make_handler()
            page.on("response", handler)
            try:
                el.click()
                _sleep(3)
            except Exception:
                page.remove_listener("response", handler)
                continue

            page.remove_listener("response", handler)

            # Primary: extract updated state
            date_records: list[dict[str, Any]] = []
            state = self._extract_initial_state(page)
            if state:
                show_dates = (
                    state.get("showtimesByEvent", {})
                    .get("showDates", {})
                )
                date_data = show_dates.get(date_id, {})
                dy_data = (
                    date_data.get("dynamic", {})
                    .get("data", {})
                )
                if dy_data:
                    dc = dy_data.get("additionalData", {}).get("dateCode", date_id)
                    show_date = self._format_date(dc)
                    widgets = dy_data.get("showtimeWidgets", [])
                    date_records.extend(
                        self._parse_showtime_widgets(
                            widgets,
                            city_slug=city_slug,
                            movie_title=movie_title,
                            event_code=event_code,
                            show_date=show_date,
                            venue_names=venue_names,
                            child_events=child_events,
                            source_url=source_url,
                        )
                    )

            # Fallback: use intercepted API
            if not date_records and captured_dynamic:
                for payload in captured_dynamic:
                    data = payload.get("data", {})
                    dc = data.get("additionalData", {}).get("dateCode", "")
                    show_date = self._format_date(dc)
                    widgets = data.get("showtimeWidgets", [])
                    date_records.extend(
                        self._parse_showtime_widgets(
                            widgets,
                            city_slug=city_slug,
                            movie_title=movie_title,
                            event_code=event_code,
                            show_date=show_date,
                            venue_names=venue_names,
                            child_events=child_events,
                            source_url=source_url,
                        )
                    )

            records.extend(date_records)
            dates_scraped.add(date_id)
            max_extra -= 1

        return records

    # ---- Widget parsing ---------------------------------------------------

    def _parse_showtime_widgets(
        self,
        widgets: list[Any],
        *,
        city_slug: str,
        movie_title: str,
        event_code: str,
        show_date: str,
        venue_names: dict[str, str],
        child_events: dict[str, dict[str, str]],
        source_url: str,
    ) -> list[dict[str, Any]]:
        """Parse the showtimeWidgets array into flat records."""
        records: list[dict[str, Any]] = []

        for widget in widgets:
            if widget.get("type") != "groupList":
                continue
            for group in widget.get("data", []):
                if group.get("type") != "venueGroup":
                    continue
                self._parse_venue_group(
                    group.get("data", []),
                    records=records,
                    city_slug=city_slug,
                    movie_title=movie_title,
                    event_code=event_code,
                    show_date=show_date,
                    venue_names=venue_names,
                    child_events=child_events,
                    source_url=source_url,
                )
        return records

    def _parse_venue_group(
        self,
        items: list[Any],
        *,
        records: list[dict[str, Any]],
        city_slug: str,
        movie_title: str,
        event_code: str,
        show_date: str,
        venue_names: dict[str, str],
        child_events: dict[str, dict[str, str]],
        source_url: str,
    ) -> None:
        current_venue_code = ""
        current_venue_name = ""

        for item in items:
            itype = item.get("type", "")

            if itype == "venue-card":
                ad = item.get("additionalData", {})
                current_venue_code = ad.get("venueCode", "")
                current_venue_name = (
                    ad.get("venueName", "")
                    or venue_names.get(current_venue_code, current_venue_code)
                )

                # Showtimes nested directly inside venue-card
                for show in item.get("showtimes", []):
                    self._parse_showtime_pill(
                        show,
                        records=records,
                        city_slug=city_slug,
                        movie_title=movie_title,
                        event_code=event_code,
                        show_date=show_date,
                        venue_code=current_venue_code,
                        venue_name=current_venue_name,
                        child_events=child_events,
                        source_url=source_url,
                    )

    def _parse_showtime_pill(
        self,
        show: dict[str, Any],
        *,
        records: list[dict[str, Any]],
        city_slug: str,
        movie_title: str,
        event_code: str,
        show_date: str,
        venue_code: str,
        venue_name: str,
        child_events: dict[str, dict[str, str]],
        source_url: str,
    ) -> None:
        ad = show.get("additionalData", {})
        analytics = show.get("cta", {}).get("analytics", {})

        show_time = show.get("title", "") or ad.get("showTime", "")
        screen_attr = show.get("screenAttr", "") or ad.get("attributes", "")
        session_id = ad.get("sessionId", "")
        avail_status = ad.get("availStatus", "")

        # Map availability codes
        avail_map = {
            "1": "Almost Full",
            "2": "Filling Fast",
            "3": "Available",
            "0": "Sold Out",
        }
        availability = avail_map.get(avail_status, avail_status)

        # Language + format from analytics or child events
        language = analytics.get("language", "")
        fmt = analytics.get("format", screen_attr)

        ce = child_events.get(event_code, {})
        if not language:
            language = ce.get("language", "")
        if not fmt:
            fmt = ce.get("dimension", "")

        subtitle_lang = show.get("subtitleAcronym", "")

        # Per-category pricing
        categories = ad.get("categories", [])
        if categories:
            for cat in categories:
                cat_avail = avail_map.get(
                    cat.get("availStatus", ""), cat.get("availStatus", "")
                )
                records.append({
                    "city": city_slug,
                    "movie": movie_title,
                    "event_code": event_code,
                    "cinema": venue_name,
                    "venue_code": venue_code,
                    "show_date": show_date,
                    "show_time": show_time,
                    "session_id": session_id,
                    "language": language,
                    "subtitle_language": subtitle_lang,
                    "format": fmt,
                    "screen_type": screen_attr,
                    "seat_category": cat.get("priceDesc", ""),
                    "price": cat.get("curPrice", ""),
                    "category_availability": cat_avail,
                    "show_availability": availability,
                    "source_url": source_url,
                    "scraped_at": _now_iso(),
                })
        else:
            records.append({
                "city": city_slug,
                "movie": movie_title,
                "event_code": event_code,
                "cinema": venue_name,
                "venue_code": venue_code,
                "show_date": show_date,
                "show_time": show_time,
                "session_id": session_id,
                "language": language,
                "subtitle_language": subtitle_lang,
                "format": fmt,
                "screen_type": screen_attr,
                "seat_category": "",
                "price": "",
                "category_availability": "",
                "show_availability": availability,
                "source_url": source_url,
                "scraped_at": _now_iso(),
            })

    # ---- Utilities --------------------------------------------------------

    @staticmethod
    def _format_date(date_code: str) -> str:
        """Convert '20260301' to '2026-03-01'."""
        if len(date_code) == 8 and date_code.isdigit():
            return f"{date_code[:4]}-{date_code[4:6]}-{date_code[6:]}"
        return date_code


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

CSV_FIELDS = [
    "city", "movie", "event_code", "cinema", "venue_code",
    "show_date", "show_time", "session_id",
    "language", "subtitle_language", "format", "screen_type",
    "seat_category", "price", "category_availability", "show_availability",
    "source_url", "scraped_at",
]


def write_json(path: str, rows: list[dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)


def write_csv(path: str, rows: list[dict[str, Any]]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Scrape BookMyShow film/cinema booking info across locations",
    )
    ap.add_argument(
        "--cities", nargs="*", default=None,
        help="City slugs, e.g. mumbai delhi-ncr bengaluru. Omit to auto-discover.",
    )
    ap.add_argument("--max-cities", type=int, default=None)
    ap.add_argument("--max-movies-per-city", type=int, default=None)
    ap.add_argument(
        "--max-dates-per-movie", type=int, default=None,
        help="How many date tabs to scrape per movie (default: up to 7)",
    )
    ap.add_argument("--delay", type=float, default=1.5)
    ap.add_argument("--timeout-ms", type=int, default=60000)
    ap.add_argument(
        "--headless", action="store_true",
        help="Run headless (may fail on Cloudflare challenge)",
    )
    ap.add_argument("--json-out", default="bookmyshow_booking_data.json")
    ap.add_argument("--csv-out", default="bookmyshow_booking_data.csv")
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    cfg = ScrapeConfig(
        headless=args.headless,
        max_cities=args.max_cities,
        max_movies_per_city=args.max_movies_per_city,
        max_dates_per_movie=args.max_dates_per_movie,
        delay=args.delay,
        timeout_ms=args.timeout_ms,
        json_out=args.json_out,
        csv_out=args.csv_out,
    )

    scraper = BookMyShowScraper(cfg)
    rows = scraper.run(cities_override=args.cities)

    print(f"\nTotal records: {len(rows)}")
    write_json(cfg.json_out, rows)
    write_csv(cfg.csv_out, rows)
    print(f"Wrote {cfg.json_out}")
    print(f"Wrote {cfg.csv_out}")


if __name__ == "__main__":
    main()
