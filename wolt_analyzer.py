#!/usr/bin/env python3
"""Wolt Restaurant Pricing Analyzer - Extracts pricing structure for all restaurants at a delivery address."""

import argparse
import csv
import math
import random
import time
from datetime import datetime

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
WOLT_LISTINGS_URL = "https://restaurant-api.wolt.com/v1/pages/restaurants"
WOLT_DYNAMIC_URL = "https://consumer-api.wolt.com/order-xp/web/v1/venue/slug/{slug}/dynamic"

HEADERS = {
    "User-Agent": "WoltAnalyzer/1.0 (pricing research tool)",
    "Accept": "application/json",
}

REQUEST_TIMEOUT = 15  # seconds
RETRY_WAIT = 2.0      # seconds between retry attempts


# ---------------------------------------------------------------------------
# Haversine distance
# ---------------------------------------------------------------------------

def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    """Return distance in metres between two WGS-84 coordinates."""
    R = 6371000  # Earth radius in metres
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return int(R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)))


# ---------------------------------------------------------------------------
# Geocoding
# ---------------------------------------------------------------------------

def geocode(address: str) -> tuple[float, float]:
    """Return (lat, lon) for the given address using Nominatim.

    Exits the program with an error message if geocoding fails.
    """
    params = {"q": address, "format": "json", "limit": 1}
    try:
        resp = requests.get(NOMINATIM_URL, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        results = resp.json()
    except requests.RequestException as exc:
        print(f"[ERROR] Geocoding request failed: {exc}")
        raise SystemExit(1)

    if not results:
        print(f"[ERROR] No geocoding results found for: {address!r}")
        raise SystemExit(1)

    lat = float(results[0]["lat"])
    lon = float(results[0]["lon"])
    return lat, lon



# ---------------------------------------------------------------------------
# Location helpers
# ---------------------------------------------------------------------------

def _extract_location(loc) -> list:
    """Extract [lon, lat] from venue location field (handles both list and dict formats)."""
    if isinstance(loc, list) and len(loc) >= 2:
        return loc  # Already [lon, lat]
    if isinstance(loc, dict):
        coords = loc.get("coordinates")
        if isinstance(coords, list) and len(coords) >= 2:
            return coords
    return [None, None]

# ---------------------------------------------------------------------------
# Restaurant listings
# ---------------------------------------------------------------------------

def fetch_listings(lat: float, lon: float) -> list[dict]:
    """Fetch all restaurant venues from the Wolt listings API.

    Returns a deduplicated list of venue dicts (keyed by slug).
    Exits on API failure.
    """
    params = {"lat": lat, "lon": lon}
    try:
        resp = requests.get(WOLT_LISTINGS_URL, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        print(f"[ERROR] Restaurant listings request failed: {exc}")
        raise SystemExit(1)

    seen_slugs: set[str] = set()
    venues: list[dict] = []

    # Walk every section that contains venue data (handles multiple template names)
    sections = data.get("sections", [])
    for section in sections:
        # Accept any section that contains venue data (handles template variations)
        template = section.get("template", "")
        name = section.get("name", "")
        if not ("venue" in template.lower() or "venue" in name.lower() or "restaurant" in name.lower()):
            continue
        items = section.get("items", [])
        for item in items:
            venue = item.get("venue")
            if not venue:
                continue
            slug = venue.get("slug", "")
            if not slug or slug in seen_slugs:
                continue
            seen_slugs.add(slug)
            venues.append({
                "name":               venue.get("name", ""),
                "slug":               slug,
                "address":            venue.get("address", ""),
                "location":           _extract_location(venue.get("location")),
                "online":             venue.get("online", False),
                "delivery_price_int": venue.get("delivery_price_int", 0) or 0,
                "estimate":           venue.get("estimate"),
                "estimate_range":     venue.get("estimate_range"),
                "currency":           venue.get("currency", ""),
            })

    return venues


# ---------------------------------------------------------------------------
# Dynamic pricing fetch (single venue)
# ---------------------------------------------------------------------------

def _get_json(url: str, params: dict) -> dict | None:
    """GET request returning parsed JSON, or None on any failure."""
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException:
        return None


def fetch_dynamic_pricing(slug: str, lat: float, lon: float) -> dict | None:
    """Fetch dynamic pricing JSON for one venue.  Retries once on failure."""
    url = WOLT_DYNAMIC_URL.format(slug=slug)
    params = {"lat": lat, "lon": lon}

    data = _get_json(url, params)
    if data is None:
        time.sleep(RETRY_WAIT)
        data = _get_json(url, params)

    return data  # may still be None after retry


# ---------------------------------------------------------------------------
# Pricing extraction helpers
# ---------------------------------------------------------------------------

def _deep_get(obj, *keys, default=None):
    """Safely traverse nested dicts/lists by successive keys."""
    for key in keys:
        if obj is None:
            return default
        if isinstance(obj, dict):
            obj = obj.get(key)
        elif isinstance(obj, list):
            try:
                obj = obj[key]
            except (IndexError, TypeError):
                return default
        else:
            return default
    return obj if obj is not None else default


def _find_price_ranges(data: dict) -> list[dict]:
    """Locate the price_ranges array anywhere in the dynamic pricing response."""
    # Common paths
    candidates = [
        _deep_get(data, "delivery_pricing", "price_ranges"),
        _deep_get(data, "price_ranges"),
        _deep_get(data, "venue_raw", "delivery_specs", "delivery_pricing", "price_ranges"),
        _deep_get(data, "venue_raw", "delivery_pricing", "price_ranges"),
    ]
    for c in candidates:
        if isinstance(c, list) and c:
            return c

    # Recursive search as fallback
    def _search(obj, depth=0):
        if depth > 8:
            return None
        if isinstance(obj, dict):
            if "price_ranges" in obj and isinstance(obj["price_ranges"], list):
                return obj["price_ranges"]
            for v in obj.values():
                result = _search(v, depth + 1)
                if result is not None:
                    return result
        elif isinstance(obj, list):
            for item in obj:
                result = _search(item, depth + 1)
                if result is not None:
                    return result
        return None

    return _search(data) or []


def extract_pricing(data: dict) -> dict:
    """Parse dynamic pricing JSON into a flat pricing dict."""
    result = {
        "service_fee_pct":     "",
        "service_fee_min_eur": "",
        "service_fee_max_eur": "",
        "minimum_basket_eur":  "",
        "minimum_basket_type": "none",
        "self_delivery":       "No",
    }

    if not data:
        return result

    # ------------------------------------------------------------------
    # a) Service fee rate (percentage)  — first price_range where b > 0
    # ------------------------------------------------------------------
    price_ranges = _find_price_ranges(data)
    b_positive = [pr for pr in price_ranges if isinstance(pr, dict) and (pr.get("b") or 0) > 0]
    if b_positive:
        b_val = b_positive[0].get("b", 0)
        result["service_fee_pct"] = f"{round(b_val * 100, 4):.4g}"

    # ------------------------------------------------------------------
    # b) Service fee min / max  — price_ranges where b == 0
    # ------------------------------------------------------------------
    b_zero = [pr for pr in price_ranges if isinstance(pr, dict) and (pr.get("b") or 0) == 0]
    if b_zero:
        a_values = [pr.get("a", 0) for pr in b_zero if pr.get("a") is not None]
        if a_values:
            result["service_fee_min_eur"] = f"{min(a_values) / 100:.2f}"
            result["service_fee_max_eur"] = f"{max(a_values) / 100:.2f}"

    # ------------------------------------------------------------------
    # d) Minimum basket (cents → EUR)
    # ------------------------------------------------------------------
    min_basket_cents = None

    # Try multiple known paths, ordered by preference
    paths = [
        ("delivery_specs", "order_minimum_no_surcharge"),
        ("venue_raw", "delivery_specs", "order_minimum_no_surcharge"),
        ("order_minimum_no_surcharge",),
        ("venue_raw", "order_minimum_no_surcharge"),
        ("order_minimum",),
        ("venue_raw", "order_minimum"),
        ("order_minimum_possible",),
        ("venue_raw", "order_minimum_possible"),
    ]
    for path in paths:
        val = _deep_get(data, *path)
        if val is not None:
            try:
                min_basket_cents = int(val)
                break
            except (TypeError, ValueError):
                continue

    if min_basket_cents is not None:
        result["minimum_basket_eur"] = f"{min_basket_cents / 100:.2f}"

    # ------------------------------------------------------------------
    # e) Minimum basket type
    # ------------------------------------------------------------------
    surcharge_type_val = None
    type_paths = [
        ("venue_raw", "delivery_specs", "small_order_surcharge_type"),
        ("venue_raw", "delivery_specs", "surcharge_type"),
        ("venue_raw", "delivery_specs", "type"),
        ("delivery_specs", "small_order_surcharge_type"),
        ("delivery_specs", "surcharge_type"),
        ("delivery_specs", "type"),
        ("small_order_surcharge_type",),
        ("surcharge_type",),
    ]
    for path in type_paths:
        val = _deep_get(data, *path)
        if val is not None:
            surcharge_type_val = str(val).upper()
            break

    if surcharge_type_val:
        if "GRADUAL" in surcharge_type_val or "SLIDING" in surcharge_type_val:
            result["minimum_basket_type"] = "sliding"
        elif "BLOCK" in surcharge_type_val or "BLOCKED" in surcharge_type_val:
            result["minimum_basket_type"] = "blocked"
        else:
            result["minimum_basket_type"] = "none"

    # ------------------------------------------------------------------
    # f) Self-delivery
    # ------------------------------------------------------------------
    self_delivery = _deep_get(data, "venue_raw", "self_delivery")
    if self_delivery is None:
        self_delivery = _deep_get(data, "self_delivery")
    if self_delivery is True:
        result["self_delivery"] = "Yes"
    elif self_delivery is False:
        result["self_delivery"] = "No"

    return result


# ---------------------------------------------------------------------------
# Delivery estimate formatter
# ---------------------------------------------------------------------------

def format_estimate(venue: dict) -> str:
    """Return a human-readable delivery estimate string."""
    est_range = venue.get("estimate_range")
    if est_range:
        return f"{est_range} min"
    estimate = venue.get("estimate")
    if estimate is not None:
        return f"{estimate} min"
    return ""


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

CSV_COLUMNS = [
    "restaurant_name",
    "slug",
    "address",
    "distance_m",
    "currency",
    "online",
    "self_delivery",
    "delivery_estimate",
    "delivery_fee_eur",
    "service_fee_pct",
    "service_fee_min_eur",
    "service_fee_max_eur",
    "minimum_basket_eur",
    "minimum_basket_type",
]


def build_row(venue: dict, pricing: dict, user_lat: float, user_lon: float) -> dict:
    """Assemble one CSV row from venue data + pricing data."""
    loc = venue.get("location", [None, None])
    # Wolt returns [lon, lat]
    if isinstance(loc, (list, tuple)) and len(loc) == 2:
        venue_lon, venue_lat = loc
    else:
        venue_lon, venue_lat = None, None

    if venue_lat is not None and venue_lon is not None:
        distance_m = haversine(user_lat, user_lon, float(venue_lat), float(venue_lon))
    else:
        distance_m = 0

    delivery_fee_eur = f"{venue.get('delivery_price_int', 0) / 100:.2f}"
    online_str = "Yes" if venue.get("online") else "No"

    return {
        "restaurant_name":    venue.get("name", ""),
        "slug":               venue.get("slug", ""),
        "address":            venue.get("address", ""),
        "distance_m":         distance_m,
        "currency":           venue.get("currency", ""),
        "online":             online_str,
        "self_delivery":      pricing.get("self_delivery", "No"),
        "delivery_estimate":  format_estimate(venue),
        "delivery_fee_eur":   delivery_fee_eur,
        "service_fee_pct":    pricing.get("service_fee_pct", ""),
        "service_fee_min_eur": pricing.get("service_fee_min_eur", ""),
        "service_fee_max_eur": pricing.get("service_fee_max_eur", ""),
        "minimum_basket_eur": pricing.get("minimum_basket_eur", ""),
        "minimum_basket_type": pricing.get("minimum_basket_type", "none"),
    }


def export_csv(rows: list[dict], output_path: str) -> None:
    """Write rows to CSV, sorted by distance_m ascending."""
    rows_sorted = sorted(rows, key=lambda r: r.get("distance_m", 0))
    with open(output_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows_sorted)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Wolt Restaurant Pricing Analyzer – fetches pricing for all restaurants at a delivery address.",
    )
    parser.add_argument(
        "address",
        type=str,
        help='Delivery address, e.g. "Avenija Marina Držića 76, 10000, Zagreb, Croatia"',
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output CSV filename (default: wolt_pricing_YYYYMMDD_HHMMSS.csv)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # Default output filename
    if args.output:
        output_path = args.output
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = f"wolt_pricing_{timestamp}.csv"

    # ------------------------------------------------------------------
    # 1. Geocode the delivery address
    # ------------------------------------------------------------------
    print(f"\nGeocoding address: {args.address!r} …")
    lat, lon = geocode(args.address)
    print(f"  → Coordinates: lat={lat:.6f}, lon={lon:.6f}\n")

    # ------------------------------------------------------------------
    # 2. Fetch restaurant listings
    # ------------------------------------------------------------------
    print("Fetching restaurant listings from Wolt …")
    venues = fetch_listings(lat, lon)
    total = len(venues)
    print(f"  → {total} unique restaurants found.\n")

    if total == 0:
        print("[WARNING] No restaurants returned by the listings API.  Exiting.")
        raise SystemExit(0)

    # ------------------------------------------------------------------
    # 3. Per-restaurant dynamic pricing
    # ------------------------------------------------------------------
    rows: list[dict] = []

    for idx, venue in enumerate(venues, start=1):
        name = venue.get("name", venue.get("slug", "?"))
        print(f"  Fetching pricing [{idx}/{total}]: {name} …")

        time.sleep(random.uniform(1.0, 1.5))

        data = fetch_dynamic_pricing(venue["slug"], lat, lon)

        if data is None:
            print(f"  [WARNING] Could not fetch pricing for {name!r} after retry – using empty values.")
            pricing: dict = {
                "service_fee_pct":     "",
                "service_fee_min_eur": "",
                "service_fee_max_eur": "",
                "minimum_basket_eur":  "",
                "minimum_basket_type": "none",
                "self_delivery":       "No",
            }
        else:
            pricing = extract_pricing(data)

        row = build_row(venue, pricing, lat, lon)
        rows.append(row)

    # ------------------------------------------------------------------
    # 4. Export CSV
    # ------------------------------------------------------------------
    export_csv(rows, output_path)
    print(f"\nDone! {len(rows)} restaurants exported to {output_path}")


if __name__ == "__main__":
    main()