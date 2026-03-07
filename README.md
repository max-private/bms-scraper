# BookMyShow Booking Scraper

Collects structured booking data for every film × cinema × showtime across BookMyShow locations.

Uses **Playwright** to drive a real Chromium browser (bypassing Cloudflare) and intercepts the internal JSON APIs to extract:

| Field | Example |
|---|---|
| city | mumbai |
| movie | The Kerala Story 2: Goes Beyond |
| event_code | ET00484171 |
| cinema | MovieMax: Wonder Mall, Thane |
| venue_code | MMWM |
| show_date | 2026-02-28 |
| show_time | 11:55 PM |
| session_id | 35460 |
| language | Hindi |
| subtitle_language | |
| format | DOLBY 7.1 |
| screen_type | Renovated |
| seat_category | RECLINER |
| price | 400.00 |
| category_availability | Available |
| show_availability | Filling Fast |
| source_url | https://in.bookmyshow.com/movies/mumbai/... |
| scraped_at | 2026-02-28T18:37:31Z |

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
```

Requires **Python 3.10+**.

## Usage

### Auto-discover all cities

```bash
python bookmyshow_scraper.py
```

### Specific cities

```bash
python bookmyshow_scraper.py --cities mumbai delhi-ncr bengaluru
```

### Limit scope for testing

```bash
python bookmyshow_scraper.py --cities mumbai --max-movies-per-city 2 --max-dates-per-movie 1
```

### All options

```
--cities              City slugs (omit to auto-discover from regions API)
--max-cities          Limit number of cities
--max-movies-per-city Limit movies per city
--max-dates-per-movie Date tabs to scrape per movie (default: up to 7)
--delay               Seconds between navigations (default: 1.5)
--timeout-ms          Page load timeout in ms (default: 60000)
--headless            Run headless (may fail on Cloudflare challenge)
--json-out            JSON output path (default: bookmyshow_booking_data.json)
--csv-out             CSV output path (default: bookmyshow_booking_data.csv)
```

## How it works

1. **City discovery** — navigates the homepage and intercepts `/api/explore/v1/discover/regions`
2. **Movie listing** — navigates `/explore/movies-{city}` and intercepts the listing API, scrolling to load all pages
3. **Showtime scraping** — navigates each movie's detail page, clicks "Book tickets", and intercepts the `showtimes-by-event/primary-static` (venue metadata) and `primary-dynamic` (show times, prices, availability) APIs
4. **Date tabs** — clicks additional date selectors on the booking page to capture future dates

Outputs are deduplicated and written to both JSON and CSV.

## Notes

- Runs in **headed mode** by default — Cloudflare's JS challenge requires a visible browser window.
- BookMyShow's internal APIs may change without notice.
- Respect their terms of service and applicable laws before any production use.
