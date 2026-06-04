#!/usr/bin/env python3
"""Wolt Restaurant Pricing Analyzer - Extracts pricing structure for all restaurants at a delivery address."""

import argparse
import csv
import json
import math
import os
import random
import time
import urllib.parse
import urllib.request as _urllib_request
from datetime import datetime, timezone

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NOMINATIM_URL     = "https://nominatim.openstreetmap.org/search"
WOLT_AUTH_URL     = "https://authentication.wolt.com/v1/wauth2/access_token"
WOLT_LISTINGS_URL = "https://restaurant-api.wolt.com/v1/pages/restaurants"
WOLT_DYNAMIC_URL  = "https://consumer-api.wolt.com/order-xp/web/v1/venue/slug/{slug}/dynamic"

# Persists the rotated refresh token so the session survives subsequent runs.
TOKEN_FILE = ".wolt_tokens.json"

BASE_HEADERS = {
    "User-Agent": "WoltAnalyzer/1.0 (pricing research tool)",
    "Accept":     "application/json",
}

REQUEST_TIMEOUT = 15   # seconds
RETRY_WAIT      = 2.0  # seconds between retry attempts


# ---------------------------------------------------------------------------
# Authentication & token persistence
# ---------------------------------------------------------------------------

def load_saved_tokens() -> dict:
    """Load previously persisted tokens from TOKEN_FILE.

    Returns an empty dict if the file is absent, empty, or unreadable.
    """
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_tokens(refresh_token: str, access_token: str) -> None:
    """Persist tokens to TOKEN_FILE.

    The Wolt refresh token ROTATES on every exchange -- calling this after
    each successful exchange ensures the next run can still authenticate.
    """
    payload = {
        "refresh_token": refresh_token,
        "access_token":  access_token,
        "saved_at":      datetime.now(tz=timezone.utc).isoformat(),
    }
    try:
        with open(TOKEN_FILE, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        print(f"  -> Tokens persisted to {TOKEN_FILE} (refresh token rotated).")
    except OSError as exc:
        print(f"  [WARNING] Could not save tokens to {TOKEN_FILE}: {exc}")


def exchange_refresh_token(refresh_token: str) -> str | None:
    """Exchange a Wolt refresh token for a short-lived access token.

    Endpoint: POST https://authentication.wolt.com/v1/wauth2/access_token
    Body MUST be application/x-www-form-urlencoded -- JSON body returns HTTP 415:
        grant_type=refresh_token&refresh_token=<token>

    The refresh token ROTATES on every successful exchange.  The new
    refresh_token is automatically persisted to TOKEN_FILE so the next run
    can still authenticate without re-supplying the original credential.

    Only "Authorization: Bearer <access_token>" is honoured on subsequent
    dynamic-pricing calls; the legacy "w-authorization" header is silently
    ignored by Wolt's servers.

    Returns the access_token string, or None on failure.
    """
    payload = urllib.parse.urlencode({
        "grant_type":    "refresh_token",
        "refresh_token": refresh_token,
    }).encode("utf-8")
    req = _urllib_request.Request(
        WOLT_AUTH_URL,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": "WoltAnalyzer/1.0"},
        method="POST",
    )
    try:
        with _urllib_request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
            body = json.loads(r.read())
        access_token = body.get("access_token")
        if access_token:
            print(f"  -> Auth token obtained (type: {body.get('token_type', 'unknown')})")
            # Persist the rotated refresh token for the next invocation
            new_refresh = body.get("refresh_token")
            if new_refresh:
                _save_tokens(new_refresh, access_token)
            else:
                print("  [WARNING] Server did not return a new refresh_token -- "
                      "this session may not be reusable.")
        return access_token
    except Exception as exc:
        print(f"[WARNING] Token exchange failed: {exc}")
        return None


def build_headers(access_token: str | None = None) -> dict:
    """Build request headers, optionally injecting the Bearer token."""
    headers = dict(BASE_HEADERS)
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"  # only this header is honoured
        headers["App-Language"]  = "en"
        headers["Platform"]      = "Web"
    return headers


# ---------------------------------------------------------------------------
# Haversine distance
# ---------------------------------------------------------------------------

def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    """Return distance in metres between two WGS-84 coordinates."""
    R = 6_371_000  # Earth radius in metres
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi       = math.radians(lat2 - lat1)
    dlambda    = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2)
    return int(R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)))


# ---------------------------------------------------------------------------
# Geocoding
# ---------------------------------------------------------------------------

def geocode(address: str) -> tuple[float, float]:
    """Return (lat, lon) for the given address using Nominatim."""
    params = {"q": address, "format": "json", "limit": 1}
    try:
        resp = requests.get(
            NOMINATIM_URL, params=params, headers=BASE_HEADERS, timeout=REQUEST_TIMEOUT
        )
        resp.raise_for_status()
        results = resp.json()
    except requests.RequestException as exc:
        print(f"[ERROR] Geocoding request failed: {exc}")
        raise SystemExit(1)

    if not results:
        print(f"[ERROR] No geocoding results found for: {address!r}")
        raise SystemExit(1)

    return float(results[0]["lat"]), float(results[0]["lon"])


# ---------------------------------------------------------------------------
# Location helpers
# ---------------------------------------------------------------------------

def _extract_location(loc) -> list:
    """Extract [lon, lat] from venue location field (handles both list and dict formats)."""
    if isinstance(loc, list) and len(loc) >= 2:
        return loc
    if isinstance(loc, dict):
        coords = loc.get("coordinates")
        if isinstance(coords, list) and len(coords) >= 2:
            return coords
    return [None, None]


# ---------------------------------------------------------------------------
# Restaurant listings
# ---------------------------------------------------------------------------

def fetch_listings(lat: float, lon: float, headers: dict) -> list[dict]:
    """Fetch all restaurant venues from the Wolt listings API.

    Returns a deduplicated list of venue dicts (keyed by slug).
    """
    params = {"lat": lat, "lon": lon}
    try:
        resp = requests.get(
            WOLT_LISTINGS_URL, params=params, headers=headers, timeout=REQUEST_TIMEOUT
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        print(f"[ERROR] Restaurant listings request failed: {exc}")
        raise SystemExit(1)

    seen_slugs: set[str] = set()
    venues: list[dict]   = []

    for section in data.get("sections", []):
        template = section.get("template", "")
        name     = section.get("name", "")
        if not ("venue" in template.lower()
                or "venue" in name.lower()
                or "restaurant" in name.lower()):
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


# ---------------------------------------------------------------------------
# Dynamic pricing fetch (single venue)
# ---------------------------------------------------------------------------

def _get_json(url: str, params: dict, headers: dict) -> dict | None:
    """GET request returning parsed JSON, or None on any failure."""
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException:
        return None


def fetch_dynamic_pricing(
    slug: str, lat: float, lon: float, headers: dict
) -> dict | None:
    """Fetch dynamic pricing JSON for one venue.  Retries once on failure."""
    url    = WOLT_DYNAMIC_URL.format(slug=slug)
    params = {"lat": lat, "lon": lon}

    data = _get_json(url, params, headers)
    if data is None:
        time.sleep(RETRY_WAIT)
        data = _get_json(url, params, headers)

    return data


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
    """Locate the price_ranges array anywhere in the dynamic pricing response.

    price_ranges lives inside delivery_specs.delivery_pricing (or the
    delivery_pricing_without_subscription sibling when the subscription path
    is absent).  A recursive search is used as a final fallback.
    """
    candidates = [
        _deep_get(data, "delivery_pricing", "price_ranges"),
        _deep_get(data, "price_ranges"),
        _deep_get(data, "venue_raw", "delivery_specs", "delivery_pricing", "price_ranges"),
        _deep_get(data, "venue_raw", "delivery_specs",
                  "delivery_pricing_without_subscription", "price_ranges"),
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
                r = _search(v, depth + 1)
                if r is not None:
                    return r
        elif isinstance(obj, list):
            for item in obj:
                r = _search(item, depth + 1)
                if r is not None:
                    return r
        return None

    return _search(data) or []


def _compute_delivery_fee(price_ranges: list[dict], haversine_distance_m: float) -> float:
    """Compute delivery fee in cents from price_ranges using Haversine distance.

    Finds the first range where min <= distance < max
    (max == 0 signals the last / unbounded range).
    Formula: fee_cents = a + b * distance_m
    """
    for pr in price_ranges:
        range_min = pr.get("min", 0)
        range_max = pr.get("max", 0)
        a = pr.get("a", 0)
        b = pr.get("b", 0.0)
        # max == 0 means unbounded (last range)
        if range_max == 0 or haversine_distance_m < range_max:
            fee = float(a) + float(b) * haversine_distance_m
            return max(fee, 0.0)
    # Fallback: use last range
    if price_ranges:
        last = price_ranges[-1]
        fee  = float(last.get("a", 0)) + float(last.get("b", 0.0)) * haversine_distance_m
        return max(fee, 0.0)
    return 0.0


def extract_pricing(data: dict) -> dict:
    """Parse dynamic pricing JSON into a flat pricing dict.

    Service fee is always derived from price_ranges -- this is True whether
    the request was authenticated or not.  The price_ranges coefficients
    ARE the differentiator:

        Authenticated   : b = 0.10  (10%)  |  floor = EUR 0.70  |  cap = EUR 2.99
        Unauthenticated : b = 0.06  ( 6%)  |  floor = EUR 0.60  |  cap = EUR 1.69

    Important: do_use_backend_pricing stays "no" and service_fee_estimate is
    absent even when authenticated -- do NOT depend on those fields.

    price_ranges is DUAL-PURPOSE:
      - DELIVERY FEE  : input = Haversine distance (m);  fee_cents = a + b*dist
      - SERVICE FEE   : input = basket (cents);
                        rate%  = b*100 for bands where b > 0;
                        floor  = min field of first rate band / 100;
                        cap    = a of final b==0 band (highest min) / 100.
    """
    result: dict = {
        "service_fee_pct":     "",
        "service_fee_min_eur": "",
        "service_fee_max_eur": "",
        "minimum_basket_eur":  "",
        "minimum_basket_type": "None",
        "self_delivery":       "No",
    }

    if not data:
        return result

    # ------------------------------------------------------------------
    # a) Service fee -- always from price_ranges coefficients
    #    (service_fee_estimate is absent even when authenticated)
    # ------------------------------------------------------------------
    price_ranges = _find_price_ranges(data)

    # Rate bands: b > 0  ->  service_fee% = b * 100
    b_positive = [
        pr for pr in price_ranges
        if isinstance(pr, dict) and (pr.get("b") or 0) > 0
    ]
    # Cap/floor bands: b == 0 with a non-zero a value
    b_zero = [
        pr for pr in price_ranges
        if isinstance(pr, dict) and (pr.get("b") or 0) == 0 and (pr.get("a") or 0) > 0
    ]

    if b_positive:
        b_val = b_positive[0].get("b", 0)
        result["service_fee_pct"] = f"{round(b_val * 100, 4):.4g}"

        # Floor: min field of the first rate band is the basket threshold that
        # equals the minimum fee charged (e.g. 70 cents -> EUR 0.70 authenticated).
        floor_cents = b_positive[0].get("min", 0)
        if floor_cents:
            result["service_fee_min_eur"] = f"{floor_cents / 100:.2f}"

    if b_zero:
        # Cap: a of the final capped band (the one with the highest min value).
        cap_band  = max(b_zero, key=lambda pr: pr.get("min", 0))
        cap_cents = cap_band.get("a", 0)
        if cap_cents:
            result["service_fee_max_eur"] = f"{cap_cents / 100:.2f}"

        # Fallback for floor if not derivable from the rate band's min field.
        if not result["service_fee_min_eur"]:
            other_bands = [
                pr for pr in b_zero
                if pr.get("min", 0) < cap_band.get("min", 0)
            ]
            if other_bands:
                floor_a = min(pr.get("a", 0) for pr in other_bands)
                if floor_a:
                    result["service_fee_min_eur"] = f"{floor_a / 100:.2f}"

    # ------------------------------------------------------------------
    # b) Minimum basket (cents -> EUR)
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # c) Minimum basket type
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
            result["minimum_basket_type"] = "None"

    # ------------------------------------------------------------------
    # d) Self-delivery
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
    if isinstance(loc, (list, tuple)) and len(loc) == 2:
        venue_lon, venue_lat = loc
    else:
        venue_lon, venue_lat = None, None

    distance_m = (
        haversine(user_lat, user_lon, float(venue_lat), float(venue_lon))
        if venue_lat is not None and venue_lon is not None
        else 0
    )

    delivery_fee_eur = pricing.get(
        "delivery_fee_eur",
        f"{venue.get('delivery_price_int', 0) / 100:.2f}",
    )
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
        description=(
            "Wolt Restaurant Pricing Analyzer - fetches pricing for all restaurants "
            "at a delivery address."
        ),
    )
    parser.add_argument(
        "address",
        type=str,
        help='Delivery address, e.g. "Avenija Marina Drzica 76, 10000, Zagreb, Croatia"',
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output CSV filename (default: wolt_pricing_YYYYMMDD_HHMMSS.csv)",
    )
    parser.add_argument(
        "--token",
        type=str,
        default=None,
        metavar="REFRESH_TOKEN",
        help=(
            "Wolt refresh token for authenticated requests (optional). "
            "Falls back to the WOLT_REFRESH_TOKEN environment variable, "
            f"then to the saved token in {TOKEN_FILE}."
        ),
    )
    parser.add_argument(
        "--num-restaurants",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Only process the N closest restaurants (sorted by Haversine distance "
            "before fetching dynamic pricing). Default: all restaurants."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # ------------------------------------------------------------------
    # 0. Resolve output path
    # ------------------------------------------------------------------
    if args.output:
        output_path = args.output
    else:
        timestamp   = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = f"wolt_pricing_{timestamp}.csv"

    # ------------------------------------------------------------------
    # 1. Optional authentication
    #    Priority: --token flag  >  WOLT_REFRESH_TOKEN env var  >  TOKEN_FILE
    # ------------------------------------------------------------------
    refresh_token = args.token or os.environ.get("WOLT_REFRESH_TOKEN")

    # Normalise: env vars can be URL-encoded and/or quote-wrapped
    if refresh_token:
        refresh_token = urllib.parse.unquote(refresh_token).strip('"').strip("'")

    # Last resort: load from persisted token file (contains rotated token from last run)
    if not refresh_token:
        saved = load_saved_tokens()
        if saved.get("refresh_token"):
            refresh_token = saved["refresh_token"]
            print(f"\nUsing saved refresh token from {TOKEN_FILE}.")

    access_token: str | None = None

    if refresh_token:
        print("\nAuthenticating with Wolt ...")
        access_token = exchange_refresh_token(refresh_token)
        if access_token:
            print("  -> Authenticated mode: 10% service fee tier (min EUR 0.70 / max EUR 2.99).")
        else:
            print("  [WARNING] Authentication failed - proceeding unauthenticated "
                  "(public 6% service fee tier: min EUR 0.60 / max EUR 1.69).")
    else:
        print("\nNo refresh token provided - proceeding unauthenticated "
              "(public pricing: 6% service fee, min EUR 0.60, max EUR 1.69).")

    headers = build_headers(access_token)

    # ------------------------------------------------------------------
    # 2. Geocode the delivery address
    # ------------------------------------------------------------------
    print(f"\nGeocoding address: {args.address!r} ...")
    lat, lon = geocode(args.address)
    print(f"  -> Coordinates: lat={lat:.6f}, lon={lon:.6f}\n")

    # ------------------------------------------------------------------
    # 3. Fetch restaurant listings
    # ------------------------------------------------------------------
    print("Fetching restaurant listings from Wolt ...")
    venues = fetch_listings(lat, lon, headers)
    print(f"  -> {len(venues)} unique restaurants found.\n")

    if not venues:
        print("[WARNING] No restaurants returned by the listings API.  Exiting.")
        raise SystemExit(0)

    # ------------------------------------------------------------------
    # 4. Sort by Haversine distance; optionally limit to N restaurants
    # ------------------------------------------------------------------
    def _haversine_venue(v: dict) -> int:
        loc = v.get("location", [None, None])
        if isinstance(loc, (list, tuple)) and len(loc) == 2 and None not in loc:
            return haversine(lat, lon, float(loc[1]), float(loc[0]))
        return 999_999

    venues.sort(key=_haversine_venue)

    if args.num_restaurants is not None and args.num_restaurants > 0:
        venues = venues[: args.num_restaurants]
        print(
            f"  -> Limiting to the {len(venues)} closest restaurants "
            f"(--num-restaurants {args.num_restaurants}).\n"
        )

    total = len(venues)

    # ------------------------------------------------------------------
    # 5. Per-restaurant dynamic pricing
    # ------------------------------------------------------------------
    rows: list[dict] = []

    for idx, venue in enumerate(venues, start=1):
        name = venue.get("name", venue.get("slug", "?"))
        print(f"  [{idx}/{total}] {name} ...", end=" ", flush=True)

        time.sleep(random.uniform(1.0, 1.5))

        data = fetch_dynamic_pricing(venue["slug"], lat, lon, headers)

        if data is None:
            print("[WARN] Could not fetch pricing after retry - using empty values.")
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

            # ----------------------------------------------------------
            # Delivery fee from price_ranges + Haversine distance
            # ----------------------------------------------------------
            loc = venue.get("location", [None, None])
            if isinstance(loc, (list, tuple)) and len(loc) == 2 and None not in loc:
                venue_lon_val, venue_lat_val = float(loc[0]), float(loc[1])
                h_dist = haversine(lat, lon, venue_lat_val, venue_lon_val)
            else:
                venue_lat_val = venue_lon_val = None
                h_dist = 0

            price_ranges = _find_price_ranges(data)

            if price_ranges and venue_lat_val is not None:
                fee_cents = _compute_delivery_fee(price_ranges, h_dist)
                pricing["delivery_fee_eur"] = f"{fee_cents / 100:.2f}"
                print(
                    f"dist={h_dist}m  fee=EUR {fee_cents/100:.2f}  "
                    f"svc={pricing.get('service_fee_pct', '?')}%"
                )
            else:
                # Fallback: use delivery_price_int from listings API
                fallback_cents = venue.get("delivery_price_int", 0) or 0
                pricing["delivery_fee_eur"] = f"{fallback_cents / 100:.2f}"
                print(
                    f"[WARN] No price_ranges - fallback fee=EUR {fallback_cents/100:.2f}  "
                    f"svc={pricing.get('service_fee_pct', '?')}%"
                )

        row = build_row(venue, pricing, lat, lon)
        rows.append(row)

    # ------------------------------------------------------------------
    # 6. Export CSV
    # ------------------------------------------------------------------
    export_csv(rows, output_path)
    print(f"\nDone! {len(rows)} restaurants exported to {output_path}")


if __name__ == "__main__":
    main()
