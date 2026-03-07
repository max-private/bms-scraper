# BookMyShow Scraper — Arc42 Architecture Documentation

> Based on the [arc42 template](https://arc42.org/) v8.2

---

## 1. Introduction and Goals

### 1.1 Requirements Overview

The BMS Scraper is a command-line tool that extracts structured movie booking
data from [BookMyShow](https://in.bookmyshow.com) — India's largest online
entertainment ticketing platform.

**Core functional requirements:**

| ID   | Requirement                                                        |
|------|--------------------------------------------------------------------|
| FR-1 | Discover all available cities on BookMyShow                        |
| FR-2 | List currently showing movies per city                             |
| FR-3 | Extract showtimes with venue, date, time, language, and format     |
| FR-4 | Extract seat categories with prices and availability status        |
| FR-5 | Support multiple dates per movie (up to 7 days)                    |
| FR-6 | Support language/format variants (e.g., Hindi 2D vs English IMAX)  |
| FR-7 | Output structured data as JSON and CSV                             |
| FR-8 | Allow filtering by specific cities via CLI                         |

**Quality goals:**

| Priority | Goal            | Description                                                |
|----------|-----------------|------------------------------------------------------------|
| 1        | Reliability     | Bypass bot detection to consistently extract data          |
| 2        | Completeness    | Capture all showtimes across dates, languages, and formats |
| 3        | Accuracy        | Output verified, deduplicated records                      |
| 4        | Usability       | Simple CLI with sensible defaults                          |

### 1.2 Stakeholders

| Role             | Expectations                                            |
|------------------|---------------------------------------------------------|
| Data Analyst     | Clean, structured CSV/JSON data for analysis            |
| Researcher       | Comprehensive coverage of cities, movies, and showtimes |
| Developer        | Maintainable code that adapts to BMS site changes       |

---

## 2. Architecture Constraints

### 2.1 Technical Constraints

| Constraint                | Description                                                    |
|---------------------------|----------------------------------------------------------------|
| No official API           | BMS does not provide a public data API                         |
| Bot detection             | BMS uses Cloudflare and JS-based bot detection                 |
| SSR rendering             | Showtime data is embedded in `window.__INITIAL_STATE__` (SSR)  |
| Dynamic DOM classes       | BMS uses styled-components with hashed class names             |
| Rate sensitivity          | Aggressive scraping triggers blocking                          |

### 2.2 Organizational Constraints

| Constraint                | Description                                          |
|---------------------------|------------------------------------------------------|
| Single-machine execution  | Runs locally on the user's machine                   |
| Python ecosystem          | Must use Python 3.12+ with pip-installable deps      |
| Open source               | MIT licensed, no proprietary dependencies            |

### 2.3 Conventions

| Convention                | Description                                          |
|---------------------------|------------------------------------------------------|
| Output schema             | Fixed 18-field CSV/JSON schema (see Section 8)       |
| Date format               | ISO 8601 (`YYYY-MM-DD`)                              |
| Time format               | 24-hour (`HH:MM`)                                    |
| Timestamps                | UTC ISO 8601                                         |

---

## 3. System Scope and Context

### 3.1 Business Context

```mermaid
graph LR
    User["👤 User<br/>(Data Analyst)"]
    BMS["🌐 BookMyShow<br/>(Target Website)"]
    Scraper["🔧 BMS Scraper"]
    Files["📁 Output Files<br/>(JSON + CSV)"]

    User -- "CLI command<br/>--cities mumbai" --> Scraper
    Scraper -- "HTTPS<br/>Chromium browser" --> BMS
    BMS -- "HTML + SSR data" --> Scraper
    Scraper -- "Structured data" --> Files
    Files -- "Analysis input" --> User
```

### 3.2 Technical Context

```mermaid
graph TB
    subgraph UserMachine["User's Machine"]
        CLI["CLI<br/>(argparse)"]
        Engine["BookMyShowScraper<br/>(Python 3.12)"]
        PW["Playwright<br/>(Chromium)"]
        Stealth["playwright-stealth<br/>(Anti-Detection)"]
        FS["File System<br/>(JSON/CSV)"]
    end

    subgraph BMS["BookMyShow Infrastructure"]
        CF["Cloudflare<br/>(WAF / Bot Detection)"]
        Web["BMS Web App<br/>(Next.js SSR)"]
        API["BMS Internal APIs<br/>(REST JSON)"]
    end

    CLI --> Engine
    Engine --> PW
    PW --> Stealth
    Stealth -- "HTTPS" --> CF
    CF --> Web
    CF --> API
    Web -- "__INITIAL_STATE__<br/>(SSR JSON)" --> PW
    API -- "JSON responses<br/>(fallback)" --> PW
    Engine --> FS
```

**External interfaces:**

| Interface              | Protocol | Data Format | Purpose                         |
|------------------------|----------|-------------|---------------------------------|
| BMS Homepage           | HTTPS    | HTML + JS   | City discovery, movie listings  |
| BMS Buytickets Page    | HTTPS    | HTML + SSR  | Showtime data extraction        |
| BMS Regions API        | HTTPS    | JSON        | City list (API interception)    |
| BMS Movies API         | HTTPS    | JSON        | Movie list (API interception)   |
| Local File System      | File I/O | JSON, CSV   | Output data storage             |

---

## 4. Solution Strategy

| Decision                          | Rationale                                                                 |
|-----------------------------------|---------------------------------------------------------------------------|
| **Playwright over requests/Selenium** | Full browser needed to bypass Cloudflare JS challenges; Playwright is faster than Selenium with better API |
| **SSR extraction over API interception** | BMS migrated from runtime API calls to server-side rendering; `__INITIAL_STATE__` is the primary data source |
| **API interception as fallback**  | Some pages may still use runtime API calls; keeping both strategies maximizes reliability |
| **playwright-stealth**            | Patches `navigator.webdriver`, plugins, and other fingerprinting vectors to evade bot detection |
| **Headed mode by default**        | Cloudflare JS challenge sometimes requires visible browser; headless available as CLI flag |
| **Random jitter delays**          | ±30% variation on all sleeps to avoid request-pattern detection |
| **Deduplication at output**       | Date/variant scraping may produce overlapping records; JSON-key dedup ensures clean output |

---

## 5. Building Block View

### 5.1 Level 1 — Overall System

```mermaid
graph LR
    A["CLI Interface"] --> B["Scraping Engine"]
    B --> C["Stealth Browser"]
    C --> D["Data Extractor"]
    D --> E["Parsing Pipeline"]
    E --> F["Output Writer"]
```

### 5.2 Level 2 — Component Breakdown

```mermaid
C4Component
    title Building Block View - Level 2

    Container_Boundary(engine, "Scraping Engine") {
        Component(run, "run()", "Orchestrator", "Main loop: launch browser, iterate cities, movies, showtimes, deduplicate, write output")
        Component(getCities, "_get_cities()", "City Discovery", "Intercepts /api/explore/v1/discover/regions to get all BMS city slugs and codes")
        Component(getMovies, "_get_movies()", "Movie Discovery", "Intercepts /api/explore/v1/discover/movies-{slug} for movie listings per city")
        Component(domFallback, "_extract_movies_from_dom()", "DOM Fallback", "Scrapes movie links from page DOM when API intercept fails")
        Component(cardParser, "_extract_movie_cards()", "Card Parser", "Recursively walks nested API JSON to find movie card nodes")
    }

    Container_Boundary(extract, "Data Extraction") {
        Component(getShowtimes, "_get_showtimes()", "Showtime Controller", "Navigates to /buytickets/{movie}/{city}, coordinates SSR and API extraction")
        Component(ssrExtract, "_extract_initial_state()", "SSR Extractor", "Evaluates window.__INITIAL_STATE__ for server-rendered showtime data")
        Component(popups, "_dismiss_popups()", "Popup Handler", "Detects full-viewport overlays and clicks dismiss buttons")
        Component(additionalDates, "_scrape_additional_dates()", "Date Navigator", "Clicks date tabs, re-extracts SSR state for each date")
        Component(variants, "_scrape_variant()", "Variant Navigator", "Navigates to language/format variant URLs")
    }

    Container_Boundary(parse, "Parsing Pipeline") {
        Component(parseWidgets, "_parse_showtime_widgets()", "Widget Parser", "Iterates groupList from showtimeWidgets data")
        Component(parseVenue, "_parse_venue_group()", "Venue Parser", "Extracts cinema name, venue code from venue-card nodes")
        Component(parsePill, "_parse_showtime_pill()", "Pill Parser", "Extracts time, session, language, format, categories with prices")
    }

    Container_Boundary(stealth, "Anti-Detection") {
        Component(stealthPage, "_new_stealth_page()", "Stealth Page Factory", "Applies playwright-stealth patches to each new page")
        Component(sleepJitter, "_sleep()", "Jitter Delay", "Adds +/-30% randomized delay between navigations")
    }

    Rel(run, getCities, "Step 1")
    Rel(run, getMovies, "Step 2 per city")
    Rel(getMovies, domFallback, "Fallback")
    Rel(getMovies, cardParser, "Parses API response")
    Rel(run, getShowtimes, "Step 3 per movie")
    Rel(getShowtimes, ssrExtract, "Primary extraction")
    Rel(getShowtimes, popups, "Before extraction")
    Rel(getShowtimes, additionalDates, "Extra dates")
    Rel(getShowtimes, variants, "Variants")
    Rel(ssrExtract, parseWidgets, "JSON data")
    Rel(parseWidgets, parseVenue, "Per venue group")
    Rel(parseVenue, parsePill, "Per showtime")
    Rel(run, stealthPage, "Creates pages")
    Rel(run, sleepJitter, "Between navigations")

    UpdateLayoutConfig($c4ShapeInRow="3", $c4BoundaryInRow="2")
```

### 5.3 Whitebox — SSR Data Structure

The primary data source is `window.__INITIAL_STATE__` embedded in the buytickets
page. The relevant path through the JSON:

```
__INITIAL_STATE__
└── showtimesByEvent
    └── showDates
        └── {dateCode}              (e.g., "20260307")
            ├── primaryStatic
            │   └── data
            │       ├── eventData   (movie metadata)
            │       └── childEvents (language/format variants)
            └── dynamic
                └── data
                    └── showtimeWidgets
                        └── groupList[]
                            └── venueGroup
                                └── venue-card
                                    ├── venueName, venueCode
                                    └── showtimes[]
                                        ├── showTime, sessionId
                                        ├── categories[] (seat types + prices)
                                        └── availStatus (0-3)
```

---

## 6. Runtime View

### 6.1 Main Scraping Flow

```mermaid
sequenceDiagram
    participant U as User
    participant CLI as CLI
    participant E as Engine
    participant B as Browser
    participant BMS as BookMyShow

    U->>CLI: python bookmyshow_scraper.py --cities mumbai
    CLI->>E: ScrapeConfig(cities=["mumbai"])
    E->>B: Launch Chromium (stealth)

    Note over E,BMS: Phase 1 — City Resolution
    E->>E: Use provided city slugs

    Note over E,BMS: Phase 2 — Movie Discovery
    E->>B: Navigate to /explore/movies-mumbai
    B->>BMS: GET /explore/movies-mumbai
    BMS-->>B: HTML + API response
    B-->>E: Intercepted movie list JSON

    Note over E,BMS: Phase 3 — Showtime Extraction (per movie)
    loop For each movie
        E->>B: Navigate to /buytickets/{movie}/mumbai
        B->>BMS: GET /buytickets/{movie}/mumbai
        BMS-->>B: HTML with __INITIAL_STATE__
        E->>B: Dismiss popups (if any)
        E->>B: Extract __INITIAL_STATE__
        B-->>E: SSR JSON data
        E->>E: Parse showtime widgets
        E->>E: Parse venue groups
        E->>E: Parse showtime pills + categories

        opt Additional dates available
            loop For each date tab
                E->>B: Click date tab
                E->>B: Re-extract __INITIAL_STATE__
                B-->>E: Updated SSR data
                E->>E: Parse new date's showtimes
            end
        end

        opt Language/format variants
            loop For each variant
                E->>B: Navigate to variant URL
                B->>BMS: GET /buytickets/{movie}/{variant}/mumbai
                BMS-->>B: HTML with __INITIAL_STATE__
                E->>E: Parse variant showtimes
            end
        end
    end

    E->>E: Deduplicate records
    E->>U: Write JSON + CSV files
```

### 6.2 Popup Dismissal Flow

```mermaid
sequenceDiagram
    participant E as Engine
    participant P as Page
    participant DOM as Page DOM

    E->>P: _dismiss_popups()
    P->>DOM: Query all elements
    DOM-->>P: Element list

    P->>P: Check for full-viewport<br/>fixed overlay (z-index >= 50)

    alt Overlay detected
        P->>DOM: Find visible dismiss buttons<br/>("OK", "Accept", "Continue", close icons)
        DOM-->>P: Button list
        P->>DOM: Click first visible button
        P->>P: Wait for overlay to disappear
    else No overlay
        P->>DOM: Check for popup-like ancestors<br/>(modal, overlay, dialog classes)
        alt Popup ancestor found
            P->>DOM: Click dismiss button
        end
    end
```

---

## 7. Deployment View

```mermaid
graph TB
    subgraph DevMachine["Developer / Analyst Machine"]
        subgraph Python["Python 3.12 Runtime"]
            Script["bookmyshow_scraper.py"]
            PW["playwright 1.54.0"]
            PS["playwright-stealth 2.0.2"]
        end

        subgraph Chromium["Chromium Browser (managed by Playwright)"]
            Browser["Headless / Headed<br/>Chrome v134 UA"]
        end

        subgraph Output["Output Directory"]
            JSON["booking_data.json"]
            CSV["booking_data.csv"]
        end
    end

    subgraph Internet["Internet"]
        CF["Cloudflare CDN/WAF"]
        BMS["BookMyShow Servers<br/>(in.bookmyshow.com)"]
    end

    Script --> PW
    PW --> Browser
    Browser -- "HTTPS" --> CF
    CF --> BMS
    Script --> Output
```

**Installation steps:**

```bash
pip install playwright playwright-stealth
playwright install chromium
python bookmyshow_scraper.py --cities mumbai
```

---

## 8. Cross-cutting Concepts

### 8.1 Anti-Detection Strategy

The scraper employs a layered approach to evade bot detection:

```mermaid
graph LR
    subgraph Layer1["Layer 1: Browser Fingerprint"]
        A1["playwright-stealth patches"]
        A2["navigator.webdriver = false"]
        A3["Realistic plugin list"]
        A4["Language headers"]
    end

    subgraph Layer2["Layer 2: Launch Args"]
        B1["--disable-blink-features=<br/>AutomationControlled"]
        B2["--no-first-run"]
        B3["--no-default-browser-check"]
    end

    subgraph Layer3["Layer 3: Context Spoofing"]
        C1["Chrome v134 User-Agent"]
        C2["Viewport 1366x768"]
        C3["Locale en-IN"]
        C4["Timezone Asia/Kolkata"]
        C5["Accept-Language header"]
    end

    subgraph Layer4["Layer 4: Behavioral"]
        D1["Random jitter ±30%"]
        D2["Polite delay between pages"]
        D3["Headed mode (default)"]
    end

    Layer1 --> Layer2 --> Layer3 --> Layer4
```

### 8.2 Data Extraction Strategy

```mermaid
graph TD
    A["Navigate to buytickets page"] --> B{"__INITIAL_STATE__<br/>exists?"}
    B -- Yes --> C["Extract SSR JSON<br/>(Primary)"]
    B -- No --> D["Wait for API calls<br/>(Fallback)"]
    D --> E{"showtimes-by-event<br/>intercepted?"}
    E -- Yes --> F["Parse API response"]
    E -- No --> G["Log warning,<br/>skip movie"]
    C --> H["Parse showtime widgets"]
    F --> H
```

### 8.3 Error Handling

| Scenario                     | Strategy                                       |
|------------------------------|------------------------------------------------|
| Popup blocks interaction     | `_dismiss_popups()` auto-detects and dismisses  |
| SSR state missing            | Falls back to API interception                 |
| API interception timeout     | Logs warning, continues to next movie          |
| Movie page fails to load     | Caught by try/except, continues to next movie  |
| Rate limiting / blocking     | Jitter delays + headed mode reduce occurrence  |
| Duplicate records            | Post-scrape JSON-key deduplication             |

### 8.4 Output Data Schema

| #  | Field                   | Type   | Description                                   |
|----|-------------------------|--------|-----------------------------------------------|
| 1  | `city`                  | string | City slug (e.g., `mumbai`)                    |
| 2  | `movie`                 | string | Movie title                                   |
| 3  | `event_code`            | string | BMS event identifier (e.g., `ET00412345`)     |
| 4  | `cinema`                | string | Cinema / venue name                           |
| 5  | `venue_code`            | string | BMS venue identifier                          |
| 6  | `show_date`             | string | ISO date (`YYYY-MM-DD`)                       |
| 7  | `show_time`             | string | 24-hour time (`HH:MM`)                        |
| 8  | `session_id`            | string | BMS session identifier                        |
| 9  | `language`              | string | Audio language                                |
| 10 | `subtitle_language`     | string | Subtitle language (if any)                    |
| 11 | `format`                | string | Screening format (`2D`, `IMAX 2D`, `4DX`)    |
| 12 | `screen_type`           | string | Screen technology (`IMAX`, `Dolby Atmos`)     |
| 13 | `seat_category`         | string | Seat tier (`GOLD`, `CLASSIC`, `PRIME`)        |
| 14 | `price`                 | string | Ticket price in INR                           |
| 15 | `category_availability` | string | Per-category status                           |
| 16 | `show_availability`     | string | Overall show status                           |
| 17 | `source_url`            | string | Page URL scraped                              |
| 18 | `scraped_at`            | string | UTC ISO 8601 timestamp                        |

**Availability values:** `Available`, `Filling Fast`, `Almost Full`, `Sold Out`

---

## 9. Architecture Decisions

| ADR # | Decision                                  | Context                                                  | Consequences                                      |
|-------|-------------------------------------------|----------------------------------------------------------|---------------------------------------------------|
| ADR-1 | Use Playwright over requests/httpx        | BMS requires full JS execution for Cloudflare bypass     | Slower but necessary; enables SSR extraction      |
| ADR-2 | SSR extraction as primary strategy        | BMS moved from runtime APIs to `__INITIAL_STATE__` SSR   | More reliable; single page load gets all data     |
| ADR-3 | Keep API interception as fallback         | Some pages may still use runtime API calls               | Increases code complexity but improves coverage   |
| ADR-4 | playwright-stealth for anti-detection     | Standard Playwright detected and blocked by BMS          | Adds dependency but essential for functionality   |
| ADR-5 | Headed mode as default                    | Cloudflare JS challenge sometimes needs visible browser  | Less convenient but more reliable                 |
| ADR-6 | Single-threaded sequential scraping       | Parallel requests would trigger rate limiting             | Slower but avoids detection                       |
| ADR-7 | Post-scrape deduplication                 | Multi-date/variant scraping produces overlapping records | Simple; slight memory overhead for large scrapes  |

---

## 10. Quality Requirements

### 10.1 Quality Tree

```mermaid
graph TD
    Q["Quality"] --> R["Reliability"]
    Q --> C["Completeness"]
    Q --> A["Accuracy"]
    Q --> U["Usability"]
    Q --> M["Maintainability"]

    R --> R1["Bypass bot detection > 95% of runs"]
    R --> R2["Graceful degradation on failures"]

    C --> C1["All showtimes for selected cities"]
    C --> C2["All dates up to 7 days"]
    C --> C3["All language/format variants"]

    A --> A1["Zero duplicate records in output"]
    A --> A2["Correct price/availability mapping"]

    U --> U1["Single command to run"]
    U --> U2["Sensible defaults"]

    M --> M1["Adapt to BMS site changes"]
    M --> M2["Clear separation of concerns"]
```

### 10.2 Quality Scenarios

| Scenario                              | Stimulus                             | Response                                    | Metric                  |
|---------------------------------------|--------------------------------------|---------------------------------------------|-------------------------|
| Bot detection bypass                  | Scraper navigates BMS                | Pages load without Cloudflare block         | > 95% success rate      |
| BMS changes DOM structure             | Styled-component classes change      | SSR extraction still works (class-agnostic) | No code change needed   |
| Large city scrape                     | 50+ movies × 7 dates                | All records collected, no crashes           | 100% completion         |
| Content warning popup                 | Movie has age rating overlay         | Popup dismissed automatically               | < 3s detection + dismiss|
| Network timeout                       | BMS page takes > 60s to load        | Timeout caught, next movie attempted        | No crash                |

---

## 11. Risks and Technical Debt

| #  | Risk / Debt                             | Probability | Impact | Mitigation                                          |
|----|-----------------------------------------|-------------|--------|-----------------------------------------------------|
| R1 | BMS changes SSR structure               | Medium      | High   | `__INITIAL_STATE__` path is monitored; API fallback exists |
| R2 | Cloudflare upgrades block stealth       | Medium      | High   | Update playwright-stealth; switch to headed + manual CAPTCHA |
| R3 | BMS implements login-wall for showtimes | Low         | High   | Would require authentication support                |
| R4 | Rate limiting on high-volume scrapes    | Medium      | Medium | Increase delay; add proxy rotation support          |
| R5 | No retry logic on transient failures    | —           | Medium | Technical debt: add exponential backoff             |
| R6 | No proxy support                        | —           | Low    | Technical debt: add `--proxy` CLI flag              |
| R7 | Single-threaded performance             | —           | Low    | Acceptable trade-off for stealth; could add worker pool |

---

## 12. Glossary

| Term                    | Definition                                                                          |
|-------------------------|-------------------------------------------------------------------------------------|
| **BMS**                 | BookMyShow — India's largest online entertainment ticketing platform                |
| **SSR**                 | Server-Side Rendering — data pre-rendered into HTML by the server                  |
| **`__INITIAL_STATE__`** | JavaScript global containing SSR data in BMS's Next.js application                 |
| **Showtime Widget**     | BMS data structure grouping venues and their showtimes for a movie/date            |
| **Venue Group**         | A cinema's showtimes grouped under a `venue-card` node                             |
| **Showtime Pill**       | Individual showtime entry with time, session, categories, and availability          |
| **Event Code**          | BMS's unique identifier for a movie (e.g., `ET00412345`)                           |
| **Session ID**          | BMS's unique identifier for a specific showtime at a specific venue                |
| **Stealth**             | Anti-detection techniques that mask browser automation fingerprints                 |
| **Jitter**              | Random variation (±30%) added to sleep durations to avoid pattern detection         |
| **Cloudflare**          | CDN and WAF provider used by BMS for DDoS protection and bot mitigation            |
| **playwright-stealth**  | Python library that patches Playwright browsers to evade automation detection       |
| **Headed mode**         | Browser runs with a visible window (vs. headless with no UI)                       |
| **Availability status** | Categorical indicator: Available (3), Filling Fast (2), Almost Full (1), Sold Out (0) |
