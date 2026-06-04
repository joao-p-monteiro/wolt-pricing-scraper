# Wolt Pricing Scraper

A Python CLI tool that scans restaurants available from a delivery address on [Wolt](https://wolt.com) and exports their full pricing structure to CSV — including delivery fees, service fees, and minimum basket requirements.

---

## Features

- 📍 **Geocoding** — converts any free-text address to coordinates using OpenStreetMap Nominatim
- 🏪 **Restaurant discovery** — fetches all venues available for delivery at your address
- 🔐 **Optional authentication** — pass a Wolt refresh token to receive authenticated pricing (richer data where available)
- 💶 **Delivery fees** — computed from Wolt's `price_ranges` formula using straight-line (Haversine) distance
- 🧾 **Service fees** — extracted from `service_fee_estimate` (authenticated) or inferred from `price_ranges` coefficients
- 🛒 **Minimum basket** — detects the minimum order value and whether it's a hard block or sliding surcharge
- 🚚 **Self-delivery flag** — identifies restaurants that handle their own delivery vs. Wolt couriers
- ⏱️ **Delivery estimate** — includes estimated delivery time range
- 📄 **CSV export** — clean, analysis-ready output sorted by distance from your address
- 🔢 **`--num-restaurants`** — limit processing to the N closest restaurants for fast exploratory runs

---

## Requirements

- Python 3.10+
- [`requests`](https://pypi.org/project/requests/) library

---

## Installation

```bash
pip install requests
```

No other dependencies required — the tool uses only the Python standard library plus `requests`.

---

## Usage

### Basic (unauthenticated, all restaurants)
```bash
python3 wolt_analyzer.py "Avenija Marina Držića 76, 10000, Zagreb, Croatia"
```

### Custom output filename
```bash
python3 wolt_analyzer.py "Avenija Marina Držića 76, 10000, Zagreb, Croatia" --output zagreb.csv
```

### Authenticated (richer service-fee data where available)
```bash
python3 wolt_analyzer.py "Avenija Marina Držića 76, 10000, Zagreb, Croatia" \
  --token YOUR_WOLT_REFRESH_TOKEN
```

Or via environment variable:
```bash
export WOLT_REFRESH_TOKEN=your_refresh_token
python3 wolt_analyzer.py "Avenija Marina Držića 76, 10000, Zagreb, Croatia"
```

### Limit to closest N restaurants (sorted before fetching pricing)
```bash
python3 wolt_analyzer.py "Avenija Marina Držića 76, 10000, Zagreb, Croatia" \
  --num-restaurants 50
```

### Full example (authenticated, 50 restaurants, custom output)
```bash
python3 wolt_analyzer.py "Avenija Marina Držića 76, 10000, Zagreb, Croatia" \
  --token YOUR_WOLT_REFRESH_TOKEN \
  --num-restaurants 50 \
  --output zagreb_50.csv
```

The script will:
1. Optionally authenticate and obtain a Bearer access token
2. Geocode the address
3. Fetch all available restaurants from the Wolt API
4. Sort by Haversine distance; optionally limit to the N closest
5. Retrieve dynamic pricing for each restaurant
6. Export a sorted CSV (nearest restaurants first)

---

## CLI Reference

| Argument | Required | Default | Description |
|---|---|---|---|
| `address` | ✅ | — | Delivery address as a free-text string |
| `--output FILE` | ❌ | `wolt_pricing_YYYYMMDD_HHMMSS.csv` | Output CSV filename |
| `--token REFRESH_TOKEN` | ❌ | `$WOLT_REFRESH_TOKEN` env var | Wolt refresh token for authenticated requests |
| `--num-restaurants N` | ❌ | all | Only process the N closest restaurants |

---

## Authentication

When a refresh token is provided (via `--token` or the `WOLT_REFRESH_TOKEN` environment variable), the script:

1. Exchanges the refresh token for a short-lived Bearer access token via `POST https://authentication.wolt.com/v1/wauth2/access_token`
2. Passes `Authorization: Bearer <access_token>` on **all** subsequent API calls
3. Also sends `App-Language: en` and `Platform: Web` headers

If the token exchange fails, the script **falls back silently to unauthenticated mode** — no data is lost, but `service_fee_estimate` fields (where exposed by Wolt) will not be available.

### Authenticated vs. unauthenticated differences

| Field | Unauthenticated | Authenticated |
|---|---|---|
| `service_fee_pct` | Inferred from `price_ranges` b-coefficient | From `service_fee_estimate.percentage` (if present) or same fallback |
| `service_fee_min/max_eur` | Inferred from fixed-a price ranges | From `service_fee_estimate.min/max` (if present) or same fallback |
| Delivery fee | Same (always uses `price_ranges` + Haversine) | Same |
| Listings / dynamic data | Public data | Personalised/account-aware data where applicable |

---

## How Fees Are Calculated

### Delivery fee  *(corrected 2026-06-04 v2)*

The delivery fee is now read from the **server-precomputed value** returned by the Wolt dynamic-pricing API, not recomputed client-side from `price_ranges`.

#### PRIMARY path — `original_delivery_price` (used whenever present)

```
venue_raw.delivery_specs.original_delivery_price  (integer CENTS → ÷ 100 = EUR)
```

This is the canonical fee Wolt shows in the app.  
Validated live against the app:

| Venue | `original_delivery_price` (¢) | Script output | App value | Match |
|---|---|---|---|---|
| Leggiero – Savica | 29 | €0.29 | €0.29 | ✓ |
| Batak – Savica | 79 | €0.79 | €0.79 | ✓ |
| Restoran Libertas | 55 | €0.55 | €0.55 | ✓ |

#### FALLBACK path — `base_price + distance-tier a` (only when `original_delivery_price` is absent/null)

```
delivery_fee_cents = base_price + tier.a
```

where:
- **`base_price`** — taken from `delivery_pricing_without_subscription.base_price` (base/non-subscriber rate, preferred) or `delivery_pricing.base_price` (authenticated subscriber rate, fallback)  
- **`tier.a`** — the `a` field of the first `distance_ranges` tier whose `[min, max)` bracket contains the **Haversine distance** in metres  
- Tier source preference: `delivery_pricing_without_subscription.distance_ranges` > `delivery_pricing.distance_ranges`

#### ⚠ What was REMOVED

`delivery_pricing.price_ranges` is **no longer used for the delivery fee**.  
It was the previous (incorrect) source and produced wrong values when the account holds a Wolt+ subscription.

### Service fee  *(unchanged)*

Extracted from `delivery_pricing.price_ranges` — unaffected by this fix.

The standard Croatian price-ranges structure:

| Tier | Basket range | Formula | Effective fee |
|---|---|---|---|
| Sliding min | €0.00 – €7.00 | `770 + (−1.0 × basket_¢)` | €7.70 → €0.70 |
| Percentage | €7.00 – €29.90 | `10% × basket` | €0.70 → €2.99 |
| Cap | > €29.90 | fixed €2.99 | €2.99 |

The script reports:
- `service_fee_pct` = **10** (from the `b = 0.1` tiers)
- `service_fee_max_eur` = **2.99** (from the fixed-`a` cap tier)
- `service_fee_min_eur` = **2.99** (same tier — the €0.70 True minimum lives in the sliding tier and is not separately extracted; behaviour unchanged from all previous validated runs)

### Distance

`distance_m` is always the **Haversine straight-line distance** (metres) between the delivery address and the venue.  Both the delivery-fee fallback tier lookup and the `distance_m` CSV column use this value.

## CSV Output

The output file contains one row per restaurant with the following columns:

| Column | Description |
|---|---|
| `restaurant_name` | Display name of the restaurant |
| `slug` | URL-safe identifier used by Wolt |
| `address` | Restaurant street address |
| `distance_m` | Haversine straight-line distance from delivery address in metres |
| `currency` | Pricing currency (e.g. `EUR`) |
| `online` | Whether the restaurant is currently accepting orders (`Yes`/`No`) |
| `self_delivery` | Whether the restaurant uses its own delivery fleet (`Yes`/`No`) |
| `delivery_estimate` | Estimated delivery time window (e.g. `25-35 min`) |
| `delivery_fee_eur` | Delivery fee in EUR, computed from `price_ranges` + Haversine distance |
| `service_fee_pct` | Service fee as a percentage of order value (e.g. `6`) |
| `service_fee_min_eur` | Minimum service fee charged in EUR |
| `service_fee_max_eur` | Maximum service fee cap in EUR |
| `minimum_basket_eur` | Minimum order value in EUR |
| `minimum_basket_type` | How the minimum is enforced: `sliding` (surcharge added), `blocked` (order rejected), or `None` |

### Example output

| restaurant_name | distance_m | delivery_fee_eur | service_fee_pct | minimum_basket_eur | minimum_basket_type |
|---|---|---|---|---|---|
| Bistro Stara konoba | 1832 | 1.10 | 6 | 10.00 | None |
| Grill & Pizza Stara Konoba | 1841 | 1.10 | 6 | 10.00 | None |
| Catering Zlatna bula | 2187 | 0.00 | | 20.00 | None |
| Grizli Catering | 3625 | 1.69 | 6 | 25.00 | None |

---

## Notes & Limitations

- **Rate limiting** — The Wolt API is a public-facing consumer API not intended for automated access. The script includes randomised delays (1.0–1.5 s) between requests to reduce the chance of hitting rate limits (HTTP 429). If you see 429 errors, increase the `RETRY_WAIT` constant or add longer sleep intervals.
- **API changes** — Wolt's internal API endpoints are undocumented and may change at any time without notice. If the script stops working, inspect the network traffic on wolt.com and update the URL constants accordingly.
- **Geographic coverage** — Results depend on your delivery address. Restaurants available in one city may differ significantly from another.
- **Pricing accuracy** — Prices are fetched in real time and reflect Wolt's current configuration. Fees may vary by time of day, promotions, or user account status.
- **Haversine vs. road distance** — Delivery fees are computed using straight-line distance (as Wolt's own `price_ranges` formula expects), not road routing distance.
- **`service_fee_estimate` availability** — This field is only present in some markets/account configurations. The script gracefully falls back to `price_ranges` inference when it is absent.

---

## License

MIT License — see [LICENSE](LICENSE) for details.