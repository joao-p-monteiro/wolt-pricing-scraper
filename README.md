# Wolt Pricing Scraper

A Python CLI tool that scans every restaurant available from a given delivery address on [Wolt](https://wolt.com), extracts each venue's full pricing structure, and exports it to a CSV sorted by distance.

It captures the **authentic, app-level pricing** — including the real delivery fee that Wolt shows in the app/website and the 10% service fee — by authenticating with your Wolt account.

---

## What it does

1. Geocodes a street address to latitude/longitude (Nominatim / OpenStreetMap).
2. Fetches all restaurants deliverable to that location from Wolt's listings API.
3. For each restaurant, calls Wolt's dynamic pricing endpoint and extracts the delivery fee, service fee, minimum basket, and venue metadata.
4. Writes everything to a CSV sorted by straight-line distance (nearest first).

---

## Installation

```bash
git clone https://github.com/joao-p-monteiro/wolt-pricing-scraper.git
cd wolt-pricing-scraper
pip install requests
```

Requires Python 3.8+. Only third-party dependency is `requests` (everything else is standard library).

---

## Usage

```bash
# Basic — unauthenticated (public pricing)
python3 wolt_analyzer.py "Avenija Marina Držića 76, 10000, Zagreb, Croatia"

# Authenticated (recommended) — real app pricing, via a refresh token
export VAULT_WOLT_REFRESH_TOKEN="your_refresh_token_here"
python3 wolt_analyzer.py "Avenija Marina Držića 76, 10000, Zagreb, Croatia"

# Limit to the N nearest restaurants
python3 wolt_analyzer.py "Address..." --limit 50

# Custom output filename
python3 wolt_analyzer.py "Address..." --output zagreb.csv

# Pass a token inline instead of an env var
python3 wolt_analyzer.py "Address..." --token "your_refresh_token_here"

# Skip geocoding by supplying coordinates directly
python3 wolt_analyzer.py "Any label" --lat 45.7921 --lon 16.0016
```

### CLI arguments

| Argument | Type | Default | Description |
|---|---|---|---|
| `address` | positional | *(required)* | Street address to geocode and scan. If `--lat`/`--lon` are given, this is just a label. |
| `--output` | string | auto-timestamped | Output CSV filename. Defaults to `wolt_pricing_YYYYMMDD_HHMMSS.csv`. |
| `--token` | string | None | Wolt refresh token (`__wrtoken`) passed inline. Highest priority source. |
| `--limit` | int | None (all) | Process only the N nearest restaurants. Omit to process every venue. |
| `--lat` | float | None | Latitude — bypasses geocoding when provided with `--lon`. |
| `--lon` | float | None | Longitude — bypasses geocoding when provided with `--lat`. |

---

## Authentication & tokens

Wolt's public (unauthenticated) API returns **reduced public pricing** (6% service fee, €0.60/€1.69 caps). To get the **real app pricing** (10% service fee, €0.70/€2.99 caps, and the True delivery fee), the script authenticates with your Wolt account using a **refresh token**.

### Getting your refresh token

1. Log in to [wolt.com](https://wolt.com) (use your normal account).
2. Open **DevTools** (F12) → **Application** tab (Chrome) / **Storage** tab (Firefox).
3. Left sidebar → **Cookies** → `https://wolt.com`.
4. Find the cookie named **`__wrtoken`** and copy its **Value**.
5. Provide it to the script as a **raw string — no quotes, no `%22`/URL-encoding**.

### How the script finds a token (priority order)

1. `--token` CLI flag
2. `VAULT_WOLT_REFRESH_TOKEN` environment variable
3. `.wolt_tokens.json` persisted file (written automatically — see below)

If None are available, the script proceeds **unauthenticated** and reports public pricing.

### Token rotation (important)

Wolt's refresh token is **single-use and rotates on every exchange**. Each run swaps the old refresh token for a fresh access token *and* a new refresh token. The script handles this automatically:

- The token exchange POSTs to `https://authentication.wolt.com/v1/wauth2/access_token` (form-urlencoded).
- Immediately after a successful exchange, the **new** refresh token is persisted to `.wolt_tokens.json` (in the script directory, your home folder, or `/tmp` — whichever is writable).
- On the next run, the persisted token is picked up automatically, so you don't have to re-fetch it every time.

> Tip: If you store the seed token in `VAULT_WOLT_REFRESH_TOKEN`, update it occasionally with the rotated value in case `.wolt_tokens.json` is cleared.

---

## How fees are calculated

This is the part that took the most reverse-engineering, so here's exactly how each fee is derived.

### Delivery fee

**Primary source — `original_delivery_price`.** Wolt's API pre-computes the exact delivery fee for the requested coordinates and exposes it at:

```
venue_raw.delivery_specs.original_delivery_price   (integer cents)
```

The script reads this directly and divides by 100. This is the figure shown in the Wolt app/website — no client-side distance math involved. Validated live against the app: Restoran Libertas → €0.55, Batak Savica → €0.79, Leggiero → €0.29.

**Fallback — base price + distance tier.** Only used when `original_delivery_price` is absent. The fee is computed as:

```
delivery_fee_cents = base_price + tier.a
```

where `base_price` and the `distance_ranges` tiers come from `delivery_pricing_without_subscription` (the **base / non-subscriber** rate, preferred) or `delivery_pricing` (subscriber / unauthenticated rate) if the former is absent. The matching tier is the one whose `[min, max)` bracket contains the **Haversine straight-line distance** in metres (`max == 0` means the final unbounded tier).

> **Wolt+ note:** If you have a Wolt+ subscription, `delivery_pricing` reflects your *discounted* rate while `delivery_pricing_without_subscription` holds the *base* rate. The script reports the **base** fee so the output reflects standard market pricing, not your personal discount.

### Service fee

Read from the **`price_ranges`** array (this array is the *service fee* structure — it is **not** used for the delivery fee). It is indexed by basket value:

- **Rate:** the first tier with `b > 0`; `b × 100` is the percentage (e.g. `0.1` → **10%**).
- **Min / Max:** among the `b == 0` tiers, the smallest `a` is the minimum fee and the largest `a` is the maximum (cap), each divided by 100.

Authenticated, this yields **10% / min €0.70 / max €2.99**. Unauthenticated it falls back to the public **6% / €0.60 / €1.69** structure.

### Minimum basket

Taken from the venue's order-minimum field (cents → EUR). The `minimum_basket_type` column indicates whether the minimum is a `sliding` surcharge threshold, a `blocked` hard minimum, or `None`.

### Distance

`distance_m` is the **Haversine straight-line distance** (metres) between the delivery address and the venue coordinates. Note the venue location is stored as `[lon, lat]` (GeoJSON order).

---

## Output columns

The CSV contains the following columns, in order:

| Column | Description |
|---|---|
| `restaurant_name` | Venue name |
| `slug` | Wolt venue slug (URL identifier) |
| `address` | Venue street address |
| `distance_m` | Haversine straight-line distance from delivery address (metres) |
| `currency` | Currency code (extracted dynamically from the API) |
| `online` | Whether the venue is currently online (Yes/No) |
| `self_delivery` | Whether the venue self-delivers vs. uses Wolt's fleet (Yes/No) |
| `delivery_estimate` | Estimated delivery time |
| `delivery_fee_eur` | Delivery fee in EUR (primary: `original_delivery_price`; otherwise fallback) |
| `original_delivery_price_eur` | The raw server-precomputed delivery price in EUR (blank when the field is absent and the fallback was used) |
| `service_fee_pct` | Service fee rate (%) |
| `service_fee_min_eur` | Service fee minimum (EUR) |
| `service_fee_max_eur` | Service fee maximum / cap (EUR) |
| `minimum_basket_eur` | Minimum basket size (EUR) |
| `minimum_basket_type` | `sliding`, `blocked`, or `None` |

Rows are sorted by `distance_m` ascending (nearest first).

---

## Example

A validated sample run for `Avenija Marina Držića 76, 10000, Zagreb, Croatia` is included in [`example_output/zagreb_sample.csv`](example_output/zagreb_sample.csv).

---

## Notes & limitations

- **Rate limiting:** the script waits a random 1.0–1.5s between per-venue requests to be polite to Wolt's API. A full run of several hundred venues can take a while — use `--limit` for quick tests.
- **Availability varies by time of day:** outside local opening hours, far fewer venues are online.
- **Geocoding:** powered by the free Nominatim service; ambiguous addresses may resolve imprecisely. Use `--lat`/`--lon` to pin coordinates exactly.
- **Public vs. authenticated pricing:** without a token, the service fee and some pricing reflect Wolt's reduced public rates rather than the real app pricing.
