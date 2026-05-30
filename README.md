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

### Delivery fee
Delivery fee is computed entirely from the `price_ranges` array returned by the dynamic pricing API, using the **straight-line Haversine distance** (in metres) between user and venue:

```
fee_cents = a + b × haversine_distance_m
```

The script iterates ranges in order and selects the first range where `distance < max` (or `max == 0`, which signals the last/unbounded range):

```python
for pr in price_ranges:
    if pr["max"] == 0 or haversine_dist < pr["max"]:
        fee = pr["a"] + pr["b"] * haversine_dist
        return max(fee, 0)
```

**Example** (restaurant at 1832 m with ranges `[{min:0, max:1000, a:1060, b:-1.0}, {min:1000, max:2817, a:0, b:0.06}, {min:2817, max:0, a:169, b:0}]`):
- Range 1: 1832 ≥ 1000 → skip
- Range 2: 1832 < 2817 → `fee = 0 + 0.06 × 1832 = 109.9 ¢ ≈ €1.10` ✓

### Service fee
1. **Priority 1 (authenticated):** `venue.service_fee_estimate` or `venue_raw.service_fee_estimate` — contains `{percentage, min, max}` directly.
2. **Fallback:** Search `price_ranges` for entries where `b > 0`; interpret `b × 100` as the service fee percentage, and fixed-`a` entries (b == 0) as min/max bounds.

---

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