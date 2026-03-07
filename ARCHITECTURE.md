# BookMyShow Scraper — Architecture (C4 Model)

This document describes the architecture of the BMS Scraper using the
[C4 model](https://c4model.com/) by Simon Brown.

---

## Level 1 — System Context

Shows how the scraper fits into the broader landscape: the user, BookMyShow,
and the local file system.

```mermaid
C4Context
    title BookMyShow Scraper - System Context (C4 Level 1)

    Person(user, "User", "Data analyst / researcher who needs BMS booking data")

    System(scraper, "BMS Scraper", "Playwright-based web scraper that extracts movie showtime, pricing, and availability data from BookMyShow")

    System_Ext(bms, "BookMyShow", "India's largest online ticketing platform for movies, events, and entertainment")

    System_Ext(fs, "Local File System", "Stores output as JSON and CSV files")

    Rel(user, scraper, "Configures & runs via CLI", "python bookmyshow_scraper.py --cities mumbai")
    Rel(scraper, bms, "Navigates pages & extracts SSR data", "HTTPS / Chromium")
    Rel(scraper, fs, "Writes structured data", "JSON, CSV")
    Rel(user, fs, "Consumes output data")

    UpdateLayoutConfig($c4ShapeInRow="3", $c4BoundaryInRow="1")
```

---

## Level 2 — Container Diagram

Breaks the scraper into its major internal containers and shows the data flow
between them.

```mermaid
C4Container
    title BookMyShow Scraper - Container Diagram (C4 Level 2)

    Person(user, "User", "Runs scraper via CLI")

    System_Boundary(scraper, "BMS Scraper System") {
        Container(cli, "CLI Interface", "Python argparse", "Parses command-line args into ScrapeConfig: cities, limits, delay, output paths")
        Container(engine, "Scraping Engine", "BookMyShowScraper class", "Orchestrates the multi-phase scrape: cities -> movies -> showtimes across dates/variants")
        Container(browser, "Stealth Browser", "Playwright + playwright-stealth", "Launches Chromium with anti-detection: patched webdriver, Chrome UA, locale spoofing, jitter delays")
        Container(extractor, "Data Extractor", "SSR + API Interceptor", "Extracts window.__INITIAL_STATE__ (primary) or intercepts XHR JSON APIs (fallback)")
        Container(parser, "Data Parser", "Python dataclass pipeline", "Parses showtime widgets -> venue groups -> showtime pills -> seat categories with prices")
        Container(writer, "Output Writer", "JSON + CSV serializer", "Deduplicates records and writes to .json and .csv files")
    }

    System_Ext(bms, "BookMyShow", "Target website serving movie/showtime data via SSR and APIs")

    Rel(user, cli, "Configures", "CLI flags")
    Rel(cli, engine, "Initializes with", "ScrapeConfig")
    Rel(engine, browser, "Creates pages via", "BrowserContext")
    Rel(browser, bms, "Navigates & loads", "HTTPS")
    Rel(browser, extractor, "Provides page to", "Page object")
    Rel(extractor, parser, "Feeds raw JSON", "dict")
    Rel(parser, writer, "Produces records", "list[dict]")
    Rel(writer, user, "Outputs data files", "JSON, CSV")

    UpdateLayoutConfig($c4ShapeInRow="3", $c4BoundaryInRow="1")
```

---

## Level 3 — Component Diagram

Drills into every method and component, grouped by responsibility.

```mermaid
C4Component
    title BookMyShow Scraper - Component Diagram (C4 Level 3)

    Container_Boundary(engine, "Scraping Engine") {
        Component(run, "run()", "Orchestrator", "Main loop: launch browser, iterate cities, movies, showtimes, deduplicate, write output")
        Component(getCities, "_get_cities()", "City Discovery", "Intercepts /api/explore/v1/discover/regions to get all BMS city slugs and codes")
        Component(getMovies, "_get_movies()", "Movie Discovery", "Intercepts /api/explore/v1/discover/movies-{slug} for movie listings per city")
        Component(domFallback, "_extract_movies_from_dom()", "DOM Fallback", "Scrapes movie links from page DOM when API intercept fails")
        Component(cardParser, "_extract_movie_cards()", "Card Parser", "Recursively walks nested API JSON to find movie card nodes with titles and event codes")
    }

    Container_Boundary(extract, "Data Extraction") {
        Component(getShowtimes, "_get_showtimes()", "Showtime Controller", "Navigates to /buytickets/{movie}/{city}, coordinates SSR extraction and API fallback")
        Component(ssrExtract, "_extract_initial_state()", "SSR Extractor", "Evaluates window.__INITIAL_STATE__ to get server-rendered showtime data")
        Component(popups, "_dismiss_popups()", "Popup Handler", "Detects full-viewport overlays and clicks dismiss buttons regardless of DOM ancestry")
        Component(additionalDates, "_scrape_additional_dates()", "Date Navigator", "Clicks date tabs in the showtime page, re-extracts SSR state for each date")
        Component(variants, "_scrape_variant()", "Variant Navigator", "Navigates to language/format variant URLs and extracts their showtime data")
    }

    Container_Boundary(parse, "Parsing Pipeline") {
        Component(parseWidgets, "_parse_showtime_widgets()", "Widget Parser", "Entry point: iterates groupList from showtimeWidgets data")
        Component(parseVenue, "_parse_venue_group()", "Venue Parser", "Extracts cinema name, venue code from venue-card nodes")
        Component(parsePill, "_parse_showtime_pill()", "Pill Parser", "Extracts time, session ID, language, format, screen type, and seat categories with prices and availability")
    }

    Container_Boundary(stealth, "Anti-Detection") {
        Component(stealthPage, "_new_stealth_page()", "Stealth Page Factory", "Applies playwright-stealth patches to each new page: webdriver=false, plugins, languages")
        Component(sleepJitter, "_sleep()", "Jitter Delay", "Adds +/-30% randomized delay between navigations to mimic human behavior")
    }

    Rel(run, getCities, "Step 1")
    Rel(run, getMovies, "Step 2 per city")
    Rel(getMovies, domFallback, "Fallback")
    Rel(getMovies, cardParser, "Parses response")
    Rel(run, getShowtimes, "Step 3 per movie")
    Rel(getShowtimes, ssrExtract, "Primary")
    Rel(getShowtimes, popups, "Before extract")
    Rel(getShowtimes, additionalDates, "Extra dates")
    Rel(getShowtimes, variants, "Language/format variants")
    Rel(ssrExtract, parseWidgets, "JSON data")
    Rel(parseWidgets, parseVenue, "Per venue group")
    Rel(parseVenue, parsePill, "Per showtime")
    Rel(run, stealthPage, "Creates pages")
    Rel(run, sleepJitter, "Between navigations")

    UpdateLayoutConfig($c4ShapeInRow="3", $c4BoundaryInRow="2")
```

---

## Data Schema

Each scraped record contains:

| Field                   | Description                                      | Example                        |
|-------------------------|--------------------------------------------------|--------------------------------|
| `city`                  | City slug                                        | `bengaluru`                    |
| `movie`                 | Movie title                                      | `Scream 7`                     |
| `event_code`            | BMS event identifier                             | `ET00412345`                   |
| `cinema`                | Cinema / venue name                              | `PVR Orion Mall`               |
| `venue_code`            | BMS venue identifier                             | `PVBN`                         |
| `show_date`             | Show date                                        | `2026-03-07`                   |
| `show_time`             | Show time (24h)                                  | `14:30`                        |
| `session_id`            | BMS session identifier                           | `1234567890123456`             |
| `language`              | Audio language                                   | `English`                      |
| `subtitle_language`     | Subtitle language (if any)                       | `Hindi`                        |
| `format`                | Screening format                                 | `2D`, `IMAX 2D`, `4DX`        |
| `screen_type`           | Screen technology                                | `IMAX`, `Dolby Atmos`         |
| `seat_category`         | Seat tier name                                   | `GOLD`, `CLASSIC`, `PRIME`     |
| `price`                 | Ticket price (INR)                               | `350.00`                       |
| `category_availability` | Seat-tier availability                           | `Available`, `Filling Fast`    |
| `show_availability`     | Overall show availability                        | `Available`, `Almost Full`     |
| `source_url`            | Page URL the data was scraped from               | `https://in.bookmyshow.com/…` |
| `scraped_at`            | UTC timestamp of extraction                      | `2026-03-07T10:30:00Z`        |

---

## Technology Stack

| Layer            | Technology                                             |
|------------------|--------------------------------------------------------|
| Language         | Python 3.12                                            |
| Browser Engine   | Playwright (Chromium)                                  |
| Anti-Detection   | playwright-stealth, custom UA, locale/timezone spoofing|
| Data Extraction  | SSR (`window.__INITIAL_STATE__`) + API interception    |
| Output Formats   | JSON, CSV                                              |
| CLI Framework    | argparse                                               |
