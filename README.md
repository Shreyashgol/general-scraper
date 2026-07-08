# Scrape Platform

Paste a product or listing URL; get a CSV of every product. Ships a CLI, a FastAPI
backend, and a React SPA over one scraping engine.

Extracted fields: **Product Name**, **Image URL**, **MRP**, **ASP** (selling price),
**Product Link**.

## How general is it, really

Two things generalise very differently, and the design says so out loud.

**Field extraction generalises.** Enterprise storefronts almost always ship
`schema.org/Product` JSON-LD, microdata, or Open Graph. `Extractor` reads all three.

**Link discovery does not.** Nothing on a page marks which anchors are products.
So the platform keeps a **registry of site adapters** and falls back to a
**generic crawler** for everything else:

| | Registered domain (e.g. `savana.com`) | Any other domain |
| --- | --- | --- |
| Discovery | The site's own JSON API | Cluster links by URL shape, then *verify* by sampling |
| Speed | ~50 products/sec | ~1 product/sec (renders each page) |
| Confidence | Exact | Best effort — reported in the run's warnings |

Adding a site = one entry in `services/registry.py`. Nothing else changes.

### How the generic crawler avoids lying to you

Picking the biggest group of same-shaped URLs is not enough: on savana.com's
homepage the largest group is 70 `/activity/<id>` **category** links, beating the
40 `/details/<slug>-<id>` product links. So candidate clusters are **sampled and
verified** — we fetch a few pages from each and ask whether they are really
product pages.

Once a product pattern is verified, the crawler drains the listing by every
mechanism a storefront might use:

| Mechanism | How it's found |
| --- | --- |
| Infinite scroll | Scrolls the element that *actually* owns the scrollbar (`window.scrollTo` is a no-op on many sites) |
| Load more button | `[data-load-more]`, `.load-more`, or a control whose label *starts with* "load / show / view / see more". Skipped when disabled, `aria-disabled`, or hidden |
| Next link | `rel=next` → aria/class hooks → exact link text (`next`, `›`, `»`) |
| Numbered pagination | The anchor labelled *current + 1*. Current page comes from `aria-current` / `.current`, else from the URL (`page-2`, `?page=5`) |

Ordered by confidence. Numbered pagination is the last resort, and the anchor must
sit inside a `<nav>` or a `pag*`-classed container — otherwise a product named "2"
becomes your next page. "Next day delivery" is not a next link, and "Download more
brochures" is not a load-more button; both are pinned by tests.

Later pages are filtered through the same template, so there is no re-sampling.
Off-host and already-visited links are refused: a pagination widget that links back
to page 1 is an infinite crawl.

Pagination is interleaved with extraction, not crawled up front — a run capped by
`--max-products` stops the moment the cap is hit rather than walking every page
first. `SAVANA_MAX_LISTING_PAGES` (default 50, `0` = unlimited) bounds it.

Telling a product page from a listing page is the crux, and one signal does it:

> On a listing, every price sits inside a small box that links elsewhere.
> On a product page, the main price stands free.

A product page with a "related items" carousel has both — carded prices for the
neighbours, one free-standing price of its own. That single distinction classifies
the page *and* picks the right price out of it (see `services/page_signals.py`).
Without it, a category page passes every field check and the crawler exports
category names as products with the grid's max/min as MRP/ASP: believable, and
wrong. When nothing verifies, the run reports *no products found* plus the patterns
it tried — it fails loudly rather than returning plausible garbage.

## robots.txt

The CLI scrapes what its operator chose. The web platform scrapes whatever a user
pastes in — a different trust model. So jobs check `robots.txt` first and return a
`blocked` status when disallowed. The SPA offers an explicit
*"Ignore robots.txt — only if you are authorized"* override, so a human makes that
call, not the code.

Note `savana.com/robots.txt` allows Googlebot and friends but ends with
`User-agent: * / Disallow: /`. Scraping it requires the override.

## Architecture

The pipeline depends only on the `ProductSource` abstraction, so swapping the data
source (JSON API / Playwright / generic) or the output format never touches it.

```
React SPA (frontend/)  ──HTTP──►  FastAPI (web/app.py)
CLI (cli.py)                        └─ JobStore (web/jobs.py, async jobs)
  └────────────┬───────────────────────────┘
               ▼
       ScrapePipeline (services/pipeline.py)
         ├─ ProductSource (services/adapter.py)          ◄── registry.py picks one
         │    ├─ ApiProductSource      savana goods-flow JSON  (fast path)
         │    ├─ BrowserProductSource  savana via Playwright
         │    └─ GenericProductSource  any site: cluster → verify → extract
         ├─ Validator                  services/validator.py
         ├─ CsvExporter (atomic)       services/exporter.py
         └─ RunState (resume)          storage/state.py
  Shared: Extractor + page_signals, BrowserManager, robots, retry, config, logging
```

Extraction priority: **1. structured data** (JSON-LD / `schema.org/Product`) →
**2. DOM selectors** → **3. heuristics + Open-Graph fallback**. Each strategy fills
only the gaps left by the previous one. The generic crawler deliberately *excludes*
the Savana-tuned DOM selectors from its chain.

## Setup

```bash
cd savana-scraper
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"        # or: pip install -r requirements.txt
python -m playwright install chromium
```

## Web platform

```bash
# Build the SPA once; FastAPI then serves it from /
cd frontend && npm install && npm run build && cd ..

uvicorn savana_scraper.web.app:app --reload      # → http://localhost:8000
```

For frontend development with hot reload, run the API on `:8000` and, in another
terminal, `cd frontend && npm run dev` (Vite proxies `/api` to it).

| Endpoint | |
| --- | --- |
| `POST /api/jobs` | `{url, max_products, ignore_robots}` → job, immediately |
| `GET /api/jobs/{id}` | status, live counts, warnings, preview rows |
| `GET /api/jobs/{id}/csv` | download the result |

Jobs run as in-process asyncio tasks and **are lost on server restart** — the right
trade for a single-user tool. Moving to Redis/Celery means reimplementing
`web/jobs.py` and nothing else.

## Usage

```bash
# Crawl the whole site (default): harvests every listing linked from the seed
# and drains each one. ~1200 products in ~25s.
python -m savana_scraper scrape

# Cap the run, choose an output dir
python -m savana_scraper scrape -n 1000 -o ./outputs

# One listing only
python -m savana_scraper scrape "https://www.savana.com/activity/13070" --no-crawl

# Ignore a previous checkpoint and start fresh
python -m savana_scraper scrape --no-resume

# Verbose logging (also un-mutes httpx request logs)
python -m savana_scraper scrape --log-level DEBUG
```

Output: `outputs/savana_products_<timestamp>.csv`.

### Sources

| `--source` | How it works | Speed |
| --- | --- | --- |
| `auto` (default) | Registry lookup on the URL's domain, else `generic` | — |
| `api` | Savana's `goods-flow/pageList` JSON, which already carries every CSV field | ~50 products/sec |
| `browser` | Renders the Savana listing with Playwright, then every product page | ~1 product/sec |
| `generic` | Any site: cluster links, verify by sampling, extract each page | ~1 product/sec |

`api` and `browser` produce identical `name`/`mrp`/`asp` values — the API mapping
(`salePrice` → MRP, `promotePrice` → ASP) is the same one the product page's SSR
payload uses, verified against 8 random product pages. Use `--source browser
--headed` to watch it work, or if the API ever changes shape.

> **Note:** each run writes a *new* timestamped CSV. On a resumed run that file
> holds only the products scraped *this* time — previously-processed ones are
> skipped (see `Skipped (resume)` in the report). Use `--no-resume` for a CSV
> containing everything from scratch.

## Configuration

Everything is config-driven via `Settings` (env-overridable, prefix `SAVANA_`) —
see `savana_scraper/core/config.py`. Common knobs:

| Env var | Meaning | Default |
|---|---|---|
| `SAVANA_HEADLESS` | Run browser headless | `true` |
| `SAVANA_MAX_PRODUCTS` | Cap products (0 = all) | `0` |
| `SAVANA_MAX_SCROLLS` | Lazy-load scroll iterations | `30` |
| `SAVANA_REQUEST_DELAY_S` | Politeness delay between pages | `1.0` |
| `SAVANA_MAX_RETRIES` | Retry attempts per product | `3` |
| `SAVANA_SEL_PRODUCT_LINK` | Discovery link selector | see config |
| `SAVANA_SEL_NAME` / `_IMAGE` / `_MRP` / `_ASP` | DOM extraction selectors | see config |

Because the site's DOM class names are dynamic, the **selectors are intentionally
externalised** so they can be tuned without code changes. Prefer JSON-LD when the
site exposes it; fall back to selectors otherwise.

## Reliability

- **Retry** — transient browser/extraction errors retried with exponential backoff.
- **Resume** — progress is checkpointed to `storage/state/run_<hash>.json`; an
  interrupted run skips already-processed products on restart.
- **Logging** — structured Rich logging (`--log-level`).
- **Dedup** — products de-duplicated by normalized URL (query/fragment ignored).

## Testing

```bash
pytest                 # unit + integration (browser test auto-skips if no Chromium)
ruff check . && black --check . && mypy savana_scraper
```

## Roadmap

V1 `SavanaAdapter` implements the generic `EcommerceAdapter` interface so future
versions (universal adapter → AI-generated scrapers → autonomous collection) extend
rather than replace it.
