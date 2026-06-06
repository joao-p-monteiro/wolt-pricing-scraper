# Wolt Pricing Scraper

A Python CLI that, given a street address, fetches all restaurants available for delivery on Wolt and exports their pricing (delivery fee, service fee, minimum basket, etc.) to CSV, plus a Markdown run-log.

## How to use this repository?

```bash
git clone https://github.com/joao-p-monteiro/wolt-pricing-scraper.git
cd wolt-pricing-scraper
```

Requires **Python 3**.

```bash
pip install requests certifi
```

> `certifi` is recommended — it resolves the macOS `CERTIFICATE_VERIFY_FAILED` error. The script falls back to system CAs if it is missing.

## How to authenticate in Wolt?

Authentication unlocks the real (app-level) service-fee pricing tier. Without it you get the public fallback pricing.

**Steps:**

1. Log into [wolt.com](https://wolt.com) in a browser.
2. Open **DevTools** → **Application** tab → **Cookies** → `https://wolt.com`.
3. Copy the value of the `__wrtoken` cookie.

> **CRITICAL — paste the token RAW.**
> If the value appears wrapped as `%22...%22`, strip those characters — `%22` is a URL-encoded double-quote and is **not** part of the token. Including it causes authentication to fail and the script silently falls back to unauthenticated pricing.

The refresh token rotates on every use; the script persists the rotated token automatically so subsequent runs keep working.

Provide the token via the `--token` flag or the `WOLT_REFRESH_TOKEN` environment variable.

## How to run the script?

Base version (address + token):

```bash
python3 wolt_analyzer.py "<address>" --token "<token>"
```

Extended version (address + token + limit number of restaurants):

```bash
python3 wolt_analyzer.py "<address>" --token "<token>" --limit <N>
```

Real example:

```bash
python3 wolt_analyzer.py "201 Shalva Nutsubidze St, T'bilisi 0186, Georgia" --token "JEccuArq..." --limit 10
```

**Output:** each run creates an auto-named folder of the form `YYYY-MM-DD_city_street-number/` (example: `2026-06-06_tbilisi_201-shalva-nutsubidze-st/`) containing:

- `<base>.csv` — the pricing data
- `<base>_log.md` — a Markdown run-log reporting restaurants available / requested / scanned and the success rate (scanned ÷ requested), plus run metadata (address, resolved city, coordinates, auth status).

## What fields are included in the output CSV?

| Column | Description |
|---|---|
| `restaurant_name` | The venue's display name |
| `slug` | Wolt's URL identifier for the venue |
| `address` | The venue's street address |
| `distance_m` | Straight-line (Haversine) distance from the delivery address, in metres |
| `currency` | Currency code of the pricing values |
| `online` | Whether the venue is currently online/open (`Yes`/`No`) |
| `self_delivery` | Whether the venue delivers with its own fleet vs Wolt couriers (`Yes`/`No`) |
| `delivery_estimate` | Estimated delivery time |
| `delivery_fee_eur` | Delivery fee for this address |
| `service_fee_pct` | Service fee rate (%) |
| `service_fee_min_eur` | Minimum service fee |
| `service_fee_max_eur` | Maximum (capped) service fee |
| `minimum_basket_eur` | Minimum basket / order value |
| `minimum_basket_type` | Type of minimum (surcharge threshold vs hard block) |

## Technical considerations

- **Authenticated vs unauthenticated pricing:** with a valid token the API returns the real service-fee tier (e.g. 10% / €0.70 min / €2.99 cap in some markets); unauthenticated returns the public fallback (e.g. 6% / €0.60 / €1.69). Pricing tiers vary by market/country (e.g. Georgia ≠ Croatia), so values won't always match a specific example.

- **Service fee min/max** are derived from the venue's `price_ranges`: the floor comes from the `b<0` sliding tier, the cap from the `b==0` tier.

- **Delivery fee** is read from `delivery_specs.original_delivery_price` (the server-precomputed value), with a `price_ranges`/`distance_ranges` fallback.

- **Distance** is Haversine straight-line.

- **Rate-limit handling:** exponential backoff plus a post-run retry pass for HTTP 429 responses.

- **macOS SSL fix:** the token-exchange call (the only `urllib` call) uses an SSL context built from certifi's CA bundle, with a graceful fallback to system default CAs — this resolves the macOS python.org `CERTIFICATE_VERIFY_FAILED` error without disabling verification.

- **Geocoding** uses OpenStreetMap Nominatim (sets a User-Agent and respects its rate-limit etiquette).

- **The Wolt refresh token rotates** on each exchange and is persisted automatically.