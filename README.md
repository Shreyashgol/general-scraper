# Scrape Platform

Paste a product or listing URL; get a CSV of every product. Ships a CLI, a FastAPI
backend, and a React SPA over one scraping engine.

Extracted fields: **Product Name**, **Category**, **Subcategory**, **Image URL**,
**MRP**, **ASP** (selling price), **Product Link**.

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
| Speed | ~50 products/sec, or a few with categories ([why](#the-cost-stated-plainly)) | ~1 product/sec (renders each page) |
| Confidence | Exact | Best effort — reported in the run's warnings |

Adding a site = one entry in `services/registry.py`. Nothing else changes.

### When URL shape can't tell products from categories

Shape-based discovery has a blind spot: some storefronts serve a product and a
category at the *same* URL shape. beyoung.in gives both a bare single-segment
slug — `/black-jacquard-striped-t-shirt` (product) and `/t-shirts-for-men`
(category) — so they collapse into one cluster, and a sample seeded from the
homepage (which links mostly to categories) rejects the whole thing. The run ends
with *"Found links, but no group of them parsed as product pages."*

Such sites publish the answer themselves. `SitemapProductSource`
(`services/sitemap_source.py`) walks the site's `robots.txt` → `sitemap.xml`
tree, descends only the **product branch** — a nested sitemap whose loc names it
products (`products.xml`, Shopify's `sitemap_products_1.xml`, Yoast's
`product-sitemap.xml`), gzip shards included — and hands those exact URLs to the
same extractor. It still samples a few and asks `is_product_page`, so a
mislabelled or stale sitemap falls back to the shape crawler rather than exporting
non-products. A site with no product-specific sitemap yields nothing and falls
back too — it never treats an undifferentiated URL list as products.

**Extraction takes the cheapest route that works.** Most storefronts render
product data server-side, so the fast path is a plain **concurrent HTTP GET** — no
browser, `_HTTP_CONCURRENCY` pages in flight at once — which turns the generic
crawler's ~1 product/sec into a whole catalogue in seconds. Only when a sampled
page fails to yield fields over raw HTTP (its data needs JavaScript) does it fall
back to rendering each page in a browser. On beyoung.in the fast path holds: 40
products in ~1s versus minutes to render them one by one.

`beyoung.in` is registered to it; force it anywhere with `--source sitemap`.

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

## Category and subcategory

Both columns are *read*, never inferred. A product named "Slit Bodycon Dress" is
not filed under Dresses on the strength of its name — a title is a marketing
string, not a taxonomy, and "Backless Freedom Bra" would sort itself into
Freedom. When a site publishes no taxonomy the columns stay empty, which is a
fact; a guess would be a fact-shaped mistake.

**On an unknown site** the trail is the taxonomy: `BreadcrumbList` JSON-LD, then a
`schema.org/Product.category` path, then a breadcrumb `<nav>`. Reduced by one rule
— drop the root, drop the trailing product name, take the next two:

```
Home  >  Women  >  Bags  >  Backpacks  >  Kitty School Backpack
~~~~     ~~~~~     ~~~~     ~~~~~~~~~     ~~~~~~~~~~~~~~~~~~~~~
drop     category  subcat    ignored      dropped (it is the product)
```

Taking the *two most specific* crumbs instead would answer `("Women", "Bags")` on
a three-crumb site and `("Bags", "Backpacks")` on a four-crumb one — the same
column meaning a different depth per row.

**On savana.com** neither exists. The product page's SSR payload carries a real
four-level taxonomy, but as bare integers:

```
level1  2    the storefront itself — constant, carries nothing
level2  11   → category      Bags
level3  57   → subcategory   Backpacks
level4  341  finer than any shopper's mental model
```

Nothing on the site resolves those ids. `flowType: "CATEGORY"` is a legal
goods-flow type yet answers `QUERY Category info error` for every id, and no other
endpoint returns a name. So `services/taxonomy.py` holds the map, and its names
were read off savana's own labels rather than invented: products were sampled from
each homepage category tile, and each id named by the tile it appeared under
(`level2 750` only ever under "Denim") or by the noun its products share (`level3
57` is "Kitty School Backpack", "Solid School Backpack", …).

An unmapped id exports as `cat:<id>` and the run reports it. A new savana category
therefore shows up as an obvious gap rather than a plausible wrong label. Extend
the map without touching code via `SAVANA_TAXONOMY_PATH`:

```json
{"level2": {"999": "Footwear"}, "level3": {"1000": "Sneakers"}}
```

### The cost, stated plainly

The category ids live *only* on the product page. The listing API returns 40
products per request and no taxonomy at all — a goods record's one
category-shaped field, `itemTrack`, holds the *activity* id ("Everyday bags"), a
merchandising flow rather than a catalogue category. So these two columns cost one
page load per product, and the `api` source drops from ~50 products/sec to a few.

That buys a distinction the cheap route cannot express: the single listing
"Everyday bags" contains both `57` Backpacks and `54` Tote Bags. Labelling every
product with its listing's title would flatten them into one wrong answer.

Keep the speed and lose the columns with `SAVANA_API_FETCH_CATEGORIES=false`. A
product page that fails to load leaves its categories empty and is counted in the
run's warnings — one dead page never discards a product the listing API already
described in full.

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
  Shared: Extractor + page_signals + breadcrumbs, taxonomy, BrowserManager,
          robots, retry, config, logging
```

Extraction priority: **1. structured data** (JSON-LD / `schema.org/Product`) →
**1b. breadcrumbs** (category / subcategory) → **2. DOM selectors** →
**3. heuristics + Open-Graph fallback**. Each strategy fills only the gaps left by
the previous one. The generic crawler deliberately *excludes* the Savana-tuned DOM
selectors from its chain, but keeps the breadcrumb reader — breadcrumbs are a
convention, not a site quirk.

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

For a split production deploy, point the static frontend at the API host and let
the API accept that frontend origin:

```bash
# Vercel frontend
VITE_API_BASE_URL=https://general-scraper.onrender.com

# FastAPI backend
SAVANA_CORS_ORIGINS=https://general-scraper-beta.vercel.app
```

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
# and drains each one.
python -m savana_scraper scrape

# Cap the run, choose an output dir
python -m savana_scraper scrape -n 1000 -o ./outputs

# Skip the per-product category lookup: ~1200 products in ~25s, with the
# category/subcategory columns left empty.
SAVANA_API_FETCH_CATEGORIES=false python -m savana_scraper scrape

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
| `api` | Savana's `goods-flow/pageList` JSON, plus one product page per item for its category ids | a few products/sec; ~50/sec with `SAVANA_API_FETCH_CATEGORIES=false` |
| `browser` | Renders the Savana listing with Playwright, then every product page | ~1 product/sec |
| `generic` | Any site: cluster links, verify by sampling, extract each page | ~1 product/sec |

`browser` gets category and subcategory for free — it already loads every product
page, which is the only place savana's category ids appear.

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
| `SAVANA_API_FETCH_CATEGORIES` | Fetch each product page for its category ids | `true` |
| `SAVANA_API_DETAIL_CONCURRENCY` | Product pages fetched in parallel when enriching | `4` |
| `SAVANA_TAXONOMY_PATH` | JSON file extending the category id → name map | unset |
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
