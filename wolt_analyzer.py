#!/usr/bin/env python3
"""Wolt Restaurant Pricing Analyzer - Extracts pricing structure for all restaurants at a delivery address."""

import argparse
import csv
import json
import math
import random
import time
from datetime import datetime

import requests

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
WOLT_LISTINGS_URL = "https://restaurant-api.wolt.com/v1/pages/restaurants"
WOLT_DYNAMIC_URL = "https://consumer-api.wolt.com/order-xp/web/v1/venue/slug/{slug}/dynamic"

HEADERS = {
    "User-Agent": "WoltAnalyzer/1.0 (pricing research tool)",
    "Accept": "application/json",
}

REQUEST_TIMEOUT = 15
RETRY_WAIT = 2.0


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    """Return distance in metres between two WGS-84 coordinates."""
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return int(R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)))


def _extract_location(loc) -> list:
    """Extract [lon, lat] from venue location field (handles both list and dict formats)."""
    if isinstance(loc, list) and len(loc) >= 2:
        return loc
    if isinstance(loc, dict):
        coords = loc.get("coordinates")
        if isinstance(coords, list) and len(coords) >= 2:
            return coords
    return [None, None]


def geocode(address: str) -> tuple[float, float]:
    """Return (lat, lon) for the given address using Nominatim."""
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
    return float(results[0]["lat"]), float(results[0]["lon"])


def fetch_listings(lat: float, lon: float) -> list[dict]:
    """Fetch all restaurant venues from the Wolt listings API."""
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
    for section in data.get("sections", []):
        template = section.get("template", "")
        name = section.get("name", "")
        if not ("venue" in template.lower() or "venue" in name.lower() or "restaurant" in name.lower()):
            continue
        for item in section.get("items", []):
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


def _get_json(url: str, params: dict) -> dict | None:
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException:
        return None


def fetch_dynamic_pricing(slug: str, lat: float, lon: float) -> dict | None:
    """Fetch dynamic pricing JSON for one venue. Retries once on failure."""
    url = WOLT_DYNAMIC_URL.format(slug=slug)
    params = {"lat": lat, "lon": lon}
    data = _get_json(url, params)
    if data is None:
        time.sleep(RETRY_WAIT)
        data = _get_json(url, params)
    return data


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
    """Locate the price_ranges array (DELIVERY FEE formula) in the dynamic pricing response.

    price_ranges encode the delivery fee as a piecewise-linear function of Haversine
    distance (straight-line metres):  fee_cents = a + b * haversine_distance_m
    Each band: {min, max, a, b}  —  last band has max==0 (unbounded).
    """
    candidates = [
        _deep_get(data, "venue_raw", "delivery_specs", "delivery_pricing", "price_ranges"),
        _deep_get(data, "delivery_pricing", "price_ranges"),
        _deep_get(data, "price_ranges"),
        _deep_get(data, "venue_raw", "delivery_pricing", "price_ranges"),
    ]
    for c in candidates:
        if isinstance(c, list) and c:
            return c

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


def _extract_dynamic_venue_coords(data: dict) -> list | None:
    """Try to extract venue coordinates from the dynamic API response.

    Returns [lon, lat] if found, None if the dynamic response has no location.
    The dynamic coordinates are the authoritative delivery-point coordinates used
    for the fee formula (may differ slightly from listing-API coordinates).
    """
    paths_to_try = [
        ("venue", "location"),
        ("venue_raw", "location"),
        ("venue_raw", "address", "location"),
        ("venue_raw", "address", "geo"),
        ("venue", "geo"),
        ("venue_raw", "geo"),
        ("venue_raw", "coordinates"),
        ("venue", "coordinates"),
    ]
    for path in paths_to_try:
        val = _deep_get(data, *path)
        if val is not None:
            coords = _extract_location(val)
            if coords[0] is not None and coords[1] is not None:
                return coords
    return None


def _compute_delivery_fee(price_ranges: list[dict], haversine_dist_m: float) -> float:
    """Compute delivery fee in cents using price_ranges and Haversine distance.

    Iterates bands in order; selects the first band where haversine_dist_m < max
    (max == 0 signals the last/unbounded band).
    Formula: fee_cents = a + b * haversine_dist_m
    """
    for pr in price_ranges:
        max_d = pr.get("max", 0)
        a = pr.get("a", 0)
        b = pr.get("b", 0.0)
        if max_d == 0 or haversine_dist_m < max_d:
            return float(a) + float(b) * haversine_dist_m
    if price_ranges:
        last = price_ranges[-1]
        return float(last.get("a", 0)) + float(last.get("b", 0.0)) * haversine_dist_m
    return 0.0


def _extract_service_fee(data: dict) -> dict | None:
    """Extract service_fee_estimate from dynamic API response.

    Returns dict with {percentage, min, max} (min/max in cents), or None if absent.
    Primary path: data["venue"]["service_fee_estimate"]
    """
    paths_to_try = [
        ("venue", "service_fee_estimate"),
        ("venue_raw", "service_fee_estimate"),
        ("service_fee_estimate",),
        ("venue", "service_fee"),
        ("venue_raw", "service_fee"),
    ]
    for path in paths_to_try:
        val = _deep_get(data, *path)
        if isinstance(val, dict) and "percentage" in val:
            return val

    def _search(obj, depth=0):
        if depth > 6:
            return None
        if isinstance(obj, dict):
            if "service_fee_estimate" in obj and isinstance(obj["service_fee_estimate"], dict):
                return obj["service_fee_estimate"]
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

    return _search(data)


def extract_pricing(data: dict) -> dict:
    """Parse dynamic pricing JSON into a flat pricing dict."""
    result = {
        "service_fee_pct":     "",
        "service_fee_min_eur": "",
        "service_fee_max_eur": "",
        "minimum_basket_eur":  "",
        "minimum_basket_type": "None",
        "self_delivery":       "No",
    }
    if not data:
        return result

    # a) Service fee — prefer service_fee_estimate
    sfe = _extract_service_fee(data)
    if sfe is not None:
        pct     = sfe.get("percentage")
        sfe_min = sfe.get("min")
        sfe_max = sfe.get("max")
        if pct is not None:
            result["service_fee_pct"] = f"{pct:.4g}"
        if sfe_min is not None:
            result["service_fee_min_eur"] = f"{sfe_min / 100:.2f}"
        if sfe_max is not None:
            result["service_fee_max_eur"] = f"{sfe_max / 100:.2f}"
    else:
        # Fallback: infer from price_ranges (works for markets where service fee
        # is encoded as b > 0 rate).  May be inaccurate for delivery-only
        # price_ranges (Croatia/HR) where b encodes delivery fee slope.
        price_ranges = _find_price_ranges(data)
        b_positive = [pr for pr in price_ranges if isinstance(pr, dict) and (pr.get("b") or 0) > 0]
        if b_positive:
            b_val = b_positive[0].get("b", 0)
            result["service_fee_pct"] = f"{round(b_val * 100, 4):.4g}"
        b_zero = [pr for pr in price_ranges if isinstance(pr, dict) and (pr.get("b") or 0) == 0]
        if b_zero:
            a_values = [pr.get("a", 0) for pr in b_zero if pr.get("a") is not None]
            if a_values:
                result["service_fee_min_eur"] = f"{min(a_values) / 100:.2f}"
                result["service_fee_max_eur"] = f"{max(a_values) / 100:.2f}"

    # b) Minimum basket (cents -> EUR)
    min_basket_cents = None
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

    # c) Minimum basket type
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
            result["minimum_basket_type"] = "None"

    # d) Self-delivery
    self_delivery = _deep_get(data, "venue_raw", "self_delivery")
    if self_delivery is None:
        self_delivery = _deep_get(data, "self_delivery")
    if self_delivery is True:
        result["self_delivery"] = "Yes"
    elif self_delivery is False:
        result["self_delivery"] = "No"

    return result


def format_estimate(venue: dict) -> str:
    est_range = venue.get("estimate_range")
    if est_range:
        return f"{est_range} min"
    estimate = venue.get("estimate")
    if estimate is not None:
        return f"{estimate} min"
    return ""


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
    """Assemble one CSV row from venue data + pricing data.

    delivery_fee_eur is taken from pricing["delivery_fee_eur"] if present
    (computed from price_ranges + Haversine in the main loop),
    otherwise falls back to venue["delivery_price_int"] / 100.
    distance_m always uses straight-line Haversine from LISTINGS venue coords (per spec).
    """
    loc = venue.get("location", [None, None])
    if isinstance(loc, (list, tuple)) and len(loc) == 2:
        venue_lon, venue_lat = loc
    else:
        venue_lon, venue_lat = None, None

    if venue_lat is not None and venue_lon is not None:
        distance_m = haversine(user_lat, user_lon, float(venue_lat), float(venue_lon))
    else:
        distance_m = 0

    if "delivery_fee_eur" in pricing:
        delivery_fee_eur = pricing["delivery_fee_eur"]
    else:
        delivery_fee_eur = f"{venue.get('delivery_price_int', 0) / 100:.2f}"

    online_str = "Yes" if venue.get("online") else "No"

    return {
        "restaurant_name":     venue.get("name", ""),
        "slug":                venue.get("slug", ""),
        "address":             venue.get("address", ""),
        "distance_m":          distance_m,
        "currency":            venue.get("currency", ""),
        "online":              online_str,
        "self_delivery":       pricing.get("self_delivery", "No"),
        "delivery_estimate":   format_estimate(venue),
        "delivery_fee_eur":    delivery_fee_eur,
        "service_fee_pct":     pricing.get("service_fee_pct", ""),
        "service_fee_min_eur": pricing.get("service_fee_min_eur", ""),
        "service_fee_max_eur": pricing.get("service_fee_max_eur", ""),
        "minimum_basket_eur":  pricing.get("minimum_basket_eur", ""),
        "minimum_basket_type": pricing.get("minimum_basket_type", "None"),
    }


def export_csv(rows: list[dict], output_path: str) -> None:
    rows_sorted = sorted(rows, key=lambda r: r.get("distance_m", 0))
    with open(output_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows_sorted)


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
    if args.output:
        output_path = args.output
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = f"wolt_pricing_{timestamp}.csv"

    print(f"\nGeocoding address: {args.address!r} …")
    lat, lon = geocode(args.address)
    print(f"  → Coordinates: lat={lat:.6f}, lon={lon:.6f}\n")

    print("Fetching restaurant listings from Wolt …")
    venues = fetch_listings(lat, lon)
    total = len(venues)
    print(f"  → {total} unique restaurants found.\n")

    if total == 0:
        print("[WARNING] No restaurants returned by the listings API.  Exiting.")
        raise SystemExit(0)

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
                "minimum_basket_type": "None",
                "self_delivery":       "No",
            }
        else:
            pricing = extract_pricing(data)

            price_ranges = _find_price_ranges(data)

            if price_ranges:
                # Prefer dynamic API venue coords (authoritative delivery point);
                # fall back to listings coords if the dynamic response has none.
                dyn_coords = _extract_dynamic_venue_coords(data)
                if dyn_coords is not None:
                    dyn_lon, dyn_lat = dyn_coords
                    fee_dist_m = haversine(lat, lon, float(dyn_lat), float(dyn_lon))
                    coord_src = "dynamic"
                else:
                    loc = venue.get("location", [None, None])
                    if isinstance(loc, (list, tuple)) and len(loc) == 2 and loc[0] is not None:
                        fee_dist_m = haversine(lat, lon, float(loc[1]), float(loc[0]))
                    else:
                        fee_dist_m = 0
                    coord_src = "listings"

                fee_cents = _compute_delivery_fee(price_ranges, float(fee_dist_m))
                pricing["delivery_fee_eur"] = f"{fee_cents / 100:.2f}"
                print(f"    fee_dist={fee_dist_m}m ({coord_src})  delivery_fee=€{fee_cents/100:.2f}")
            else:
                fallback_cents = venue.get("delivery_price_int", 0) or 0
                pricing["delivery_fee_eur"] = f"{fallback_cents / 100:.2f}"
                print(f"    [WARN] No price_ranges found — falling back to listings API value")

        row = build_row(venue, pricing, lat, lon)
        rows.append(row)

    export_csv(rows, output_path)
    print(f"\nDone! {len(rows)} restaurants exported to {output_path}")


if __name__ == "__main__":
    main()
