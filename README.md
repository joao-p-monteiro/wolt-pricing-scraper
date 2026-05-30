# Wolt Pricing Scraper

A Python CLI tool that scans restaurants available from a delivery address on [Wolt](https://wolt.com) and exports their pricing structure to CSV.

## Features

- **Address Geocoding** — Converts any street address to coordinates using Nominatim
- **Restaurant Discovery** — Fetches all available restaurants from the Wolt API
- **Pricing Extraction** — Extracts delivery fees, service fees, and minimum basket info for each venue
- **Distance Calculation** — Computes straight-line distance (Haversine) from delivery address to each restaurant
- **CSV Export** — Outputs structured data sorted by distance, ready for analysis

## Requirements

- Python 3.7+
- `requests` library

## Installation

```bash
pip install requests
```

## Usage

```bash
# Basic usage (auto-generated filename with timestamp)
python wolt_analyzer.py "Avenija Marina Držića 76, 10000, Zagreb, Croatia"

# Custom output filename
python wolt_analyzer.py "Avenija Marina Držića 76, 10000, Zagreb, Croatia" --output zagreb.csv
```

## CSV Output Schema

| Column | Description | Format |
|--------|-------------|--------|
| `restaurant_name` | Venue display name | String |
| `slug` | Wolt URL slug | String |
| `address` | Restaurant street address | String |
| `distance_m` | Distance from delivery address | Integer (meters) |
| `currency` | Pricing currency (extracted dynamically) | String (e.g., "EUR") |
| `online` | Currently accepting orders | "Yes" / "No" |
| `self_delivery` | Restaurant handles own delivery | "Yes" / "No" |
| `delivery_estimate` | Estimated delivery time | "XX-YY min", "XX min", or empty |
| `delivery_fee_eur` | Delivery fee | Decimal (2dp) |
| `service_fee_pct` | Service fee rate | Decimal (e.g., 6.0 = 6%) |
| `service_fee_min_eur` | Minimum service fee | Decimal (2dp) |
| `service_fee_max_eur` | Maximum service fee cap | Decimal (2dp) |
| `minimum_basket_eur` | Minimum order value | Decimal (2dp) |
| `minimum_basket_type` | How minimum is enforced | "sliding" / "blocked" / "None" |

## Example Output

| restaurant_name | slug | distance_m | delivery_fee_eur | service_fee_pct | minimum_basket_eur | minimum_basket_type |
|----------------|------|-----------|-----------------|----------------|-------------------|-------------------|
| Submarine Burger Radnička | submarine-burger-radnicka | 245 | 0.99 | 6.0 | 7.00 | sliding |
| McDonald's Držićeva | mcdonalds-drziceva | 380 | 0.00 | 6.0 | 5.00 | sliding |
| Pizza Hut Dubrava | pizza-hut-dubrava | 520 | 1.49 | 6.0 | 8.00 | None |
| KFC Zagreb Arena | kfc-zagreb-arena | 890 | 1.99 | 8.0 | 10.00 | blocked |

See `example_output/zagreb_sample.csv` for a full sample run.

## How It Works

1. **Geocoding** — Converts the provided address to lat/lon via Nominatim (OpenStreetMap)
2. **Listings** — Queries the Wolt restaurant API for all venues delivering to that location
3. **Pricing** — For each venue, fetches dynamic pricing data (with 1-1.5s delay between calls)
4. **Processing** — Extracts fees, calculates distances, handles edge cases
5. **Export** — Writes sorted CSV to disk

## Notes & Limitations

- **Rate limiting**: The script includes 1–1.5 second delays between API calls. If you encounter 429 errors, increase the delay or try again later.
- **API availability**: Wolt's consumer API is undocumented and may change without notice.
- **Distance**: Uses Haversine formula (straight-line), not actual routing distance.
- **Service fee**: Extracted from the `price_ranges` array in Wolt's dynamic pricing response. The `b` coefficient (where b > 0) represents the percentage rate as a decimal ratio.

## License

MIT
