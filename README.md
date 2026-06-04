# Wolt Pricing Scraper

A Python CLI tool that scans restaurants available from a delivery address on [Wolt](https://wolt.com) and exports their full pricing structure to CSV — including delivery fees, service fees, and minimum basket requirements.

---

## Features

- 📍 **Geocoding** — converts any free-text address to coordinates using OpenStreetMap Nominatim
- 🏪 **Restaurant discovery** — fetches all venues available for delivery at your address
- 🔐 **Authenticated pricing** — supply a Wolt refresh token to unlock the **authentic 10% service fee tier** (vs. the public 6% tier without auth)
- 💶 **Delivery fees** — computed from Wolt's `price_ranges` formula using straight-line (Haversine) distance
- 🧾 **Service fees** — extracted from `price_ranges` coefficients (the definitive source; applies whether authenticated or not)
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

### Authenticated (authentic 10% service fee tier)
```bash
python3 wolt_analyzer.py "Avenija Marina Držića 76, 10000, Zagreb, Croatia" \
  --token YOUR_WOLT_REFRESH_TOKEN
```

Or via environment variable:
```bash
export WOLT_REFRESH_TOKEN=your_refresh_token_here
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
  --output zagreb_50_auth.csv
```

The script will:
1. Resolve a refresh token (CLI flag → env var → `.wolt_tokens.json` saved from last run)
2. Exchange it for a short-lived Bearer access token and persist the rotated refresh token
3. Geocode the address
4. Fetch all available restaurants from the Wolt API
5. Sort by Haversine distance; optionally limit to the N closest
6. Retrieve dynamic pricing for each restaurant
7. Export a sorted CSV (nearest restaurants first)

---

## CLI Reference

| Argument | Required | Default | Description |
|---|---|---|---|
| `address` | ✅ | — | Delivery address as a free-text string |
| `--output FILE` | ❌ | `wolt_pricing_YYYYMMDD_HHMMSS.csv` | Output CSV filename |
| `--token REFRESH_TOKEN` | ❌ | `$WOLT_REFRESH_TOKEN` env var, then `.wolt_tokens.json` | Wolt refresh token for authenticated requests |
| `--num-restaurants N` | ❌ | all | Only process the N closest restaurants |

---

## Authentication

### Why authentication matters

Wolt uses two distinct service fee tiers depending on whether the API request carries a valid user Bearer token:

| | Unauthenticated (public) | Authenticated |
|---|---|---|
| Service fee rate | **6%** of basket value | **10%** of basket value |
| Service fee minimum (floor) | **€0.60** | **€0.70** |
| Service fee maximum (cap) | **€1.69** | **€2.99** |
| Delivery fee | Same (always `price_ranges` + Haversine) | Same |

The difference is encoded directly in the `price_ranges` coefficients returned by the dynamic pricing API — when a valid Bearer token is present, Wolt returns the authenticated coefficient set. Without a token, you get the public/web tier.

> **Why can't I just scrape the website?**
> The Wolt web app has the feature flag `dynamic_service_fee_on_venue_screen: off`, which means the service fee is never rendered in the browser UI. The only path to the authentic 10% tier data is via the authenticated REST API — web scraping is not a viable alternative.

### How to obtain your refresh token

The Wolt refresh token is stored as the `__wrtoken` cookie after you log in to [wolt.com](https://wolt.com):

1. Open [wolt.com](https://wolt.com) in your browser and log in.
2. Open **DevTools** → **Application** tab → **Storage → Cookies → https://wolt.com**.
3. Find the cookie named **`__wrtoken`** and copy its **Value**.
4. The value is a long alphanumeric string — copy it **raw, without surrounding quotes**.

### Setting up the token

**Option A — `.wolt_tokens.json` file (recommended)**

Create a file named `.wolt_tokens.json` in the directory where you run the script:

```json
{
  "refresh_token": "paste_your_raw___wrtoken_value_here"
}
```

The script reads this file automatically on startup, and **overwrites it with the rotated token** after each successful exchange.

**Option B — environment variable**

```bash
export WOLT_REFRESH_TOKEN=paste_your_raw___wrtoken_value_here
python3 wolt_analyzer.py "Your Address"
```

**Option C — CLI flag**

```bash
python3 wolt_analyzer.py "Your Address" --token paste_your_raw___wrtoken_value_here
```

### Token rotation

The Wolt refresh token **rotates on every exchange** — each time you call the token endpoint, the server invalidates the old token and returns a new one. The script handles this automatically:

- After a successful exchange, the new `refresh_token` from the response is saved to `.wolt_tokens.json`.
- On the next run, the script loads the rotated token from that file automatically.
- **Never reuse a refresh token** that has already been exchanged — it will return a 401 error.

### Token exchange technical details

The script POSTs to `https://authentication.wolt.com/v1/wauth2/access_token` using:
- `Content-Type: application/x-www-form-urlencoded` ← **required**; a JSON body returns HTTP 415.
- Body: `grant_type=refresh_token&refresh_token=<token>`

The resulting `access_token` is then sent as `Authorization: Bearer <access_token>` on all subsequent API calls. The legacy `w-authorization` header is silently ignored by Wolt's servers.

---

## How Fees Are Calculated

### `price_ranges` is dual-purpose

The `price_ranges` array (inside `venue_raw.delivery_specs.delivery_pricing` or the `delivery_pricing_without_subscription` sibling) serves two completely different functions depending on which input you feed it:

#### Delivery fee (input = Haversine distance in metres)

```
fee_cents = a + b × haversine_distance_m
```

Iterate bands in order; use the first band where `min ≤ distance < max` (`max == 0` = last unbounded band):

```python
for pr in price_ranges:
    if pr["max"] == 0 or haversine_dist < pr["max"]:
        fee = pr["a"] + pr["b"] * haversine_dist
        return max(fee, 0)
```

**Example** (restaurant at 1 832 m):
bands `[{min:0, max:1000, a:1060, b:-1.0}, {min:1000, max:2817, a:0, b:0.06}, {min:2817, max:0, a:169, b:0}]`
- Band 1: 1 832 ≥ 1 000 → skip
- Band 2: 1 832 < 2 817 → `fee = 0 + 0.06 × 1832 = 109.9 ¢ ≈ €1.10` ✓

#### Service fee (input = basket value in cents)

Bands where `b > 0` encode the service fee percentage (`b × 100`). Fixed bands (`b == 0`, `a > 0`) define the floor and cap:

| Band type | Role | Value |
|---|---|---|
| `b > 0` | Rate band | `service_fee_pct = b × 100` |
| `b == 0`, lowest `min` | Floor (minimum fee) | `floor = min field of rate band / 100` |
| `b == 0`, highest `min` | Cap (maximum fee) | `cap = a / 100` |

```
effective_fee = clamp(basket × rate, floor, cap)
```

**Authenticated example** (10% tier, basket = €15.00):
`clamp(15.00 × 10%, €0.70, €2.99) = clamp(€1.50, €0.70, €2.99) = €1.50`

**Authenticated example** (10% tier, basket = €5.00):
`clamp(5.00 × 10%, €0.70, €2.99) = clamp(€0.50, €0.70, €2.99) = €0.70` ← floor applies

> **Key insight**: `service_fee_estimate` and `do_use_backend_pricing` are **absent** from the API response even when authenticated. Do not depend on them — the `price_ranges` coefficients alone differentiate the two tiers.

---

## CSV Output

The output file contains one row per restaurant, sorted by `distance_m` ascending.

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
| `service_fee_pct` | Service fee percentage (`10` authenticated / `6` unauthenticated) |
| `service_fee_min_eur` | Minimum (floor) service fee in EUR (`0.70` auth / `0.60` unauth) |
| `service_fee_max_eur` | Maximum (cap) service fee in EUR (`2.99` auth / `1.69` unauth) |
| `minimum_basket_eur` | Minimum order value in EUR |
| `minimum_basket_type` | How the minimum is enforced: `sliding` (surcharge added), `blocked` (order rejected), or `None` |

### Example output (authenticated, Zagreb)

See [`example_output/zagreb_50_authenticated.csv`](example_output/zagreb_50_authenticated.csv) for a full validated 50-restaurant run (authenticated, 10% tier).

| restaurant_name | distance_m | delivery_fee_eur | service_fee_pct | service_fee_min_eur | service_fee_max_eur |
|---|---|---|---|---|---|
| Mamma Mia! | 221 | 0.00 | 10 | 0.70 | 2.99 |
| Leggiero - Savica | 234 | 0.00 | 10 | 0.70 | 2.99 |
| Restoran Libertas | 460 | 0.26 | 10 | 0.70 | 2.99 |
| Batak - Savica | 757 | 0.50 | 10 | 0.70 | 2.99 |

---

## Notes & Limitations

- **Rate limiting** — The Wolt API is a consumer-facing API not intended for automated access. The script includes randomised delays (1.0–1.5 s per request) to reduce the chance of triggering rate limits (HTTP 429). If you see 429 errors, increase the `RETRY_WAIT` constant or add longer sleep intervals.
- **API changes** — Wolt's internal API endpoints are undocumented and may change without notice. If the script stops working, inspect the network traffic on wolt.com and update the URL constants accordingly.
- **Geographic coverage** — Results depend on your delivery address. Restaurants available in one city may differ significantly from another.
- **Pricing accuracy** — Prices are fetched in real time and reflect Wolt's current configuration. Fees may vary by time of day, promotions, or user account status.
- **Haversine vs. road distance** — Delivery fees are computed using straight-line distance, as Wolt's own `price_ranges` formula uses straight-line metres as input, not road routing distance.
- **Token rotation** — Never reuse a refresh token across multiple runs without letting the script persist the updated `.wolt_tokens.json`. Using an already-exchanged token will return a 401 error.

---

## License

MIT License — see [LICENSE](LICENSE) for details.
