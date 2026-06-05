#!/usr/bin/env python3
"""Wolt Restaurant Pricing Analyzer - Extracts pricing structure for all restaurants at a delivery address.

FIX 2026-06-04 v2 (delivery fee source -- FINAL CORRECT):
  DELIVERY FEE COMPUTATION (replaced in this version):
    1. PRIMARY:  venue_raw.delivery_specs.original_delivery_price
                 Server-precomputed integer CENTS -> divide by 100 for EUR.
                 Validated live: Libertas->55->EUR0.55, Batak Savica->79->EUR0.79, Leggiero->29->EUR0.29.
    2. FALLBACK  (only when original_delivery_price is absent/null):
                 delivery_fee_cents = base_price + tier_a
                   - base_price: delivery_pricing_without_subscription.base_price (preferred)
                                 or delivery_pricing.base_price
                   - tier_a: the `a` field of the distance_ranges tier whose [min, max) bracket
                             contains the Haversine distance in metres.
                 Prefers delivery_pricing_without_subscription.distance_ranges (base rate).
                 Falls back to delivery_pricing.distance_ranges (subscriber / unauthenticated).
    3. DROPPED:  price_ranges is NO LONGER used for the delivery fee.

  SERVICE FEE (delivery_pricing.price_ranges) -- UNCHANGED:
    Authenticated: 10% / min EUR0.70 / max EUR2.99. Correct; leave intact.

  DISTANCE: distance_m column remains Haversine straight-line (no OSRM).
  Token rotation: refresh token persisted to .wolt_tokens.json immediately after exchange.
"""

import argparse
import csv
import json
import math
import os
import random
import time
import urllib.parse
import urllib.request as _urllib_request
from datetime import datetime

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NOMINATIM_URL     = "https://nominatim.openstreetmap.org/search"
WOLT_LISTINGS_URL = "https://restaurant-api.wolt.com/v1/pages/restaurants"
WOLT_DYNAMIC_URL  = "https://consumer-api.wolt.com/order-xp/web/v1/venue/slug/{slug}/dynamic"
WOLT_AUTH_URL     = "https://authentication.wolt.com/v1/wauth2/access_token"

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE_CANDIDATES = [
    os.path.join(_SCRIPT_DIR, ".wolt_tokens.json"),
    os.path.expanduser("~/.wolt_tokens.json"),
    "/tmp/.wolt_tokens.json",
]

HEADERS = {
    "User-Agent": "WoltAnalyzer/1.0 (pricing research tool)",
    "Accept":     "application/json",
}

REQUEST_TIMEOUT = 15
RETRY_WAIT      = 2.0


# ---------------------------------------------------------------------------
# Haversine distance
# ---------------------------------------------------------------------------

def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi    = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2)
    return int(R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)))


# ---------------------------------------------------------------------------
# Geocoding
# ---------------------------------------------------------------------------

def geocode(address):
    params = {"q": address, "format": "json", "limit": 1}
    try:
        resp = requests.get(NOMINATIM_URL, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        results = resp.json()
    except requests.RequestException as exc:
        print(f"[ERROR] Geocoding failed: {exc}")
        raise SystemExit(1)
    if not results:
        print(f"[ERROR] No geocoding results for: {address!r}")
        raise SystemExit(1)
    return float(results[0]["lat"]), float(results[0]["lon"])


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def _load_persisted_refresh_token():
    for path in TOKEN_FILE_CANDIDATES:
        try:
            with open(path) as fh:
                data = json.load(fh)
            tok = data.get("refresh_token", "").strip()
            if tok:
                print(f"  [AUTH] Loaded persisted token from {path}")
                return tok
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            continue
    return None


def _persist_tokens(refresh_token, access_token=""):
    payload = {
        "refresh_token": refresh_token,
        "access_token":  access_token,
        "persisted_at":  datetime.utcnow().isoformat() + "Z",
    }
    for path in TOKEN_FILE_CANDIDATES:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, "w") as fh:
                json.dump(payload, fh)
            print(f"  [AUTH] Rotated refresh token persisted -> {path}")
            return
        except Exception:
            continue
    print("  [AUTH][WARN] Could not persist rotated token.")


def exchange_refresh_token(refresh_token):
    clean = urllib.parse.unquote(refresh_token).strip('"').strip("'").strip()
    payload = urllib.parse.urlencode({
        "grant_type":    "refresh_token",
        "refresh_token": clean,
    }).encode("utf-8")
    req = _urllib_request.Request(
        WOLT_AUTH_URL,
        data=payload,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent":   "WoltAnalyzer/1.0",
        },
        method="POST",
    )
    try:
        with _urllib_request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
            body = json.loads(r.read())
        at  = body.get("access_token", "").strip()
        nrt = body.get("refresh_token", "").strip()
        if at:
            print(f"  [AUTH] Access token obtained (type={body.get('token_type','bearer')})")
            print(f"  [AUTH] New refresh token: {nrt[:12]}...{nrt[-6:]}")
            return at, nrt or clean
    except Exception as exc:
        print(f"  [AUTH][WARN] Token exchange failed: {exc}")
    return None, None


def resolve_auth(cli_token=None):
    candidates = []
    if cli_token:
        candidates.append(("CLI --token", cli_token))
    vault = os.environ.get("VAULT_WOLT_REFRESH_TOKEN", "").strip()
    if vault:
        candidates.append(("vault VAULT_WOLT_REFRESH_TOKEN", vault))
    persisted = _load_persisted_refresh_token()
    if persisted:
        candidates.append(("persisted .wolt_tokens.json", persisted))

    if not candidates:
        print("  [AUTH] No refresh token available -- proceeding unauthenticated.")
        return None, HEADERS.copy()

    for label, token in candidates:
        print(f"  [AUTH] Trying: {label} ({token[:8]}...)")
        at, nrt = exchange_refresh_token(token)
        if at:
            _persist_tokens(nrt or token, at)
            return at, {**HEADERS, "Authorization": f"Bearer {at}"}

    print("  [AUTH][WARN] All token sources failed -- proceeding unauthenticated.")
    return None, HEADERS.copy()


# ---------------------------------------------------------------------------
# Location helpers
# ---------------------------------------------------------------------------

def _extract_location(loc):
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

def fetch_listings(lat, lon, headers):
    params = {"lat": lat, "lon": lon}
    try:
        resp = requests.get(WOLT_LISTINGS_URL, params=params, headers=headers,
                            timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        print(f"[ERROR] Listings request failed: {exc}")
        raise SystemExit(1)

    seen = set()
    venues = []
    for section in data.get("sections", []):
        tmpl  = section.get("template", "")
        sname = section.get("name", "")
        if not ("venue" in tmpl.lower() or "venue" in sname.lower()
                or "restaurant" in sname.lower()):
            continue
        for item in section.get("items", []):
            v = item.get("venue")
            if not v:
                continue
            slug = v.get("slug", "")
            if not slug or slug in seen:
                continue
            seen.add(slug)
            venues.append({
                "name":               v.get("name", ""),
                "slug":               slug,
                "address":            v.get("address", ""),
                "location":           _extract_location(v.get("location")),
                "online":             v.get("online", False),
                "delivery_price_int": v.get("delivery_price_int", 0) or 0,
                "estimate":           v.get("estimate"),
                "estimate_range":     v.get("estimate_range"),
                "currency":           v.get("currency", ""),
            })
    return venues


# ---------------------------------------------------------------------------
# Dynamic pricing fetch
# ---------------------------------------------------------------------------

def _get_json(url, params, headers):
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException:
        return None


def fetch_dynamic_pricing(slug, lat, lon, headers):
    url       = WOLT_DYNAMIC_URL.format(slug=slug)
    params    = {"lat": lat, "lon": lon}
    base_wait = 2.0
    for attempt in range(3):
        data = _get_json(url, params, headers)
        if data is not None:
            return data
        wait = base_wait * (2 ** attempt)
        print(f"    [WARN] Dynamic fetch failed (attempt {attempt + 1}/3); "
              f"retrying in {wait:.1f}s ...")
        time.sleep(wait)
    return None


# ---------------------------------------------------------------------------
# Generic deep-get
# ---------------------------------------------------------------------------

def _deep_get(obj, *keys, default=None):
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


# ---------------------------------------------------------------------------
# DELIVERY FEE -- PRIMARY path
# ---------------------------------------------------------------------------

def _get_original_delivery_price(data):
    """Return server-precomputed delivery price in CENTS, or None if absent.

    PRIMARY delivery fee source (FIX 2026-06-04 v2).
    Path: venue_raw.delivery_specs.original_delivery_price (integer cents).
    Validated: Libertas->55 (EUR0.55), Batak Savica->79 (EUR0.79), Leggiero->29 (EUR0.29).
    """
    for path in [
        ("venue_raw", "delivery_specs", "original_delivery_price"),
        ("venue_raw", "original_delivery_price"),
        ("original_delivery_price",),
    ]:
        val = _deep_get(data, *path)
        if val is not None:
            try:
                return int(val)
            except (TypeError, ValueError):
                continue
    return None


# ---------------------------------------------------------------------------
# DELIVERY FEE -- FALLBACK path
# ---------------------------------------------------------------------------

def _get_delivery_pricing_for_fallback(data):
    """Return (base_price_cents, distance_ranges, source_label) for fallback delivery-fee calc.

    Prefers delivery_pricing_without_subscription (base/non-subscriber rate).
    Falls back to delivery_pricing (subscriber / unauthenticated rate).
    """
    ds = _deep_get(data, "venue_raw", "delivery_specs") or {}

    dpws = ds.get("delivery_pricing_without_subscription") or {}
    dr_ws = dpws.get("distance_ranges")
    if isinstance(dr_ws, list) and dr_ws:
        bp = int(dpws.get("base_price", 0) or 0)
        return bp, dr_ws, "without_subscription"

    dp = ds.get("delivery_pricing") or {}
    dr_dp = dp.get("distance_ranges")
    if isinstance(dr_dp, list) and dr_dp:
        bp = int(dp.get("base_price", 0) or 0)
        return bp, dr_dp, "delivery_pricing"

    for path, label in [
        (("venue_raw", "delivery_pricing_without_subscription"), "without_sub_top"),
        (("delivery_pricing_without_subscription",),             "without_sub_root"),
        (("venue_raw", "delivery_pricing"),                      "delivery_pricing_top"),
        (("delivery_pricing",),                                  "delivery_pricing_root"),
    ]:
        obj = _deep_get(data, *path) or {}
        dr  = obj.get("distance_ranges")
        if isinstance(dr, list) and dr:
            bp = int(obj.get("base_price", 0) or 0)
            return bp, dr, label

    return 0, [], "None"


def _compute_fallback_delivery_fee(base_price_cents, distance_ranges, haversine_dist_m):
    """FALLBACK delivery fee in CENTS: base_price + tier.a for matching Haversine tier.

    Tier match: [min, max); max==0 means unbounded (last tier).
    """
    for tier in distance_ranges:
        min_d = int(tier.get("min", 0) or 0)
        max_d = int(tier.get("max", 0) or 0)
        a     = int(tier.get("a",   0) or 0)
        if max_d == 0 or (haversine_dist_m >= min_d and haversine_dist_m < max_d):
            return base_price_cents + a
    if distance_ranges:
        last = distance_ranges[-1]
        return base_price_cents + int(last.get("a", 0) or 0)
    return base_price_cents


# ---------------------------------------------------------------------------
# SERVICE FEE -- price_ranges (UNCHANGED)
# ---------------------------------------------------------------------------

def _find_price_ranges(data):
    """Locate price_ranges for SERVICE FEE (basket-value tiers). UNCHANGED.

    Uses delivery_pricing.price_ranges.
    Authenticated result: 10% / min EUR0.70 / max EUR2.99.
    price_ranges must NOT be used for delivery fee.
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


# ---------------------------------------------------------------------------
# Full pricing extraction (service fee + basket + self-delivery)
# ---------------------------------------------------------------------------

def extract_pricing(data):
    result = {
        "service_fee_pct":             "",
        "service_fee_min_eur":         "",
        "service_fee_max_eur":         "",
        "minimum_basket_eur":          "",
        "minimum_basket_type":         "None",
        "self_delivery":               "No",
    }
    if not data:
        return result

    # Service fee (UNCHANGED) -- uses price_ranges
    price_ranges = _find_price_ranges(data)
    b_positive = [pr for pr in price_ranges
                  if isinstance(pr, dict) and (pr.get("b") or 0) > 0]
    if b_positive:
        result["service_fee_pct"] = f"{round(b_positive[0].get('b', 0) * 100, 4):.4g}"

    b_neg  = [pr for pr in price_ranges if isinstance(pr, dict) and (pr.get("b") or 0) < 0]
    b_zero = [pr for pr in price_ranges if isinstance(pr, dict) and (pr.get("b") or 0) == 0]

    # FLOOR / MINIMUM: primary = b<0 sliding tier evaluated at its upper basket bound
    if b_neg:
        bn = b_neg[0]
        floor_cents = bn.get("a", 0) + bn.get("b", 0) * (bn.get("max") or 0)
        result["service_fee_min_eur"] = f"{max(0, floor_cents) / 100:.2f}"
    elif b_zero:
        a_vals = [pr.get("a", 0) for pr in b_zero if pr.get("a") is not None]
        if a_vals:
            result["service_fee_min_eur"] = f"{min(a_vals) / 100:.2f}"

    # CAP / MAXIMUM: highest a among b==0 tiers (unchanged)
    if b_zero:
        a_vals = [pr.get("a", 0) for pr in b_zero if pr.get("a") is not None]
        if a_vals:
            result["service_fee_max_eur"] = f"{max(a_vals) / 100:.2f}"

    # Minimum basket
    min_basket_cents = None
    for path in [
        ("venue_raw", "delivery_specs", "order_minimum_no_surcharge"),
        ("order_minimum_no_surcharge",),
        ("venue_raw", "order_minimum_no_surcharge"),
        ("order_minimum",),
        ("venue_raw", "order_minimum"),
        ("order_minimum_possible",),
        ("venue_raw", "order_minimum_possible"),
    ]:
        val = _deep_get(data, *path)
        if val is not None:
            try:
                min_basket_cents = int(val)
                break
            except (TypeError, ValueError):
                continue
    if min_basket_cents is not None:
        result["minimum_basket_eur"] = f"{min_basket_cents / 100:.2f}"

    # Minimum basket type
    for path in [
        ("venue_raw", "delivery_specs", "small_order_surcharge_type"),
        ("venue_raw", "delivery_specs", "surcharge_type"),
        ("venue_raw", "delivery_specs", "type"),
        ("delivery_specs", "small_order_surcharge_type"),
        ("small_order_surcharge_type",),
        ("surcharge_type",),
    ]:
        val = _deep_get(data, *path)
        if val is not None:
            st = str(val).upper()
            if "GRADUAL" in st or "SLIDING" in st:
                result["minimum_basket_type"] = "sliding"
            elif "BLOCK" in st or "BLOCKED" in st:
                result["minimum_basket_type"] = "blocked"
            break

    # Self-delivery
    sd = (_deep_get(data, "venue_raw", "self_delivery")
          or _deep_get(data, "self_delivery"))
    if sd is True:
        result["self_delivery"] = "Yes"
    elif sd is False:
        result["self_delivery"] = "No"

    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def format_estimate(venue):
    est_range = venue.get("estimate_range")
    if est_range:
        return f"{est_range} min"
    estimate = venue.get("estimate")
    if estimate is not None:
        return f"{estimate} min"
    return ""


CSV_COLUMNS = [
    "restaurant_name", "slug", "address", "distance_m", "currency",
    "online", "self_delivery", "delivery_estimate", "delivery_fee_eur",
    "delivery_fee_source",
    "service_fee_pct", "service_fee_min_eur", "service_fee_max_eur",
    "minimum_basket_eur", "minimum_basket_type",
]


def build_row(venue, pricing, user_lat, user_lon):
    loc = venue.get("location", [None, None])
    if isinstance(loc, (list, tuple)) and len(loc) == 2:
        venue_lon, venue_lat = loc
    else:
        venue_lon, venue_lat = None, None

    distance_m = (haversine(user_lat, user_lon, float(venue_lat), float(venue_lon))
                  if venue_lat is not None and venue_lon is not None else 0)

    delivery_fee_eur = (pricing.get("delivery_fee_eur")
                        or f"{venue.get('delivery_price_int', 0) / 100:.2f}")

    return {
        "restaurant_name":             venue.get("name", ""),
        "slug":                        venue.get("slug", ""),
        "address":                     venue.get("address", ""),
        "distance_m":                  distance_m,
        "currency":                    venue.get("currency", ""),
        "online":                      "Yes" if venue.get("online") else "No",
        "self_delivery":               pricing.get("self_delivery", "No"),
        "delivery_estimate":           format_estimate(venue),
        "delivery_fee_eur":            delivery_fee_eur,
        "delivery_fee_source":         pricing.get("delivery_fee_source", ""),
        "service_fee_pct":             pricing.get("service_fee_pct", ""),
        "service_fee_min_eur":         pricing.get("service_fee_min_eur", ""),
        "service_fee_max_eur":         pricing.get("service_fee_max_eur", ""),
        "minimum_basket_eur":          pricing.get("minimum_basket_eur", ""),
        "minimum_basket_type":         pricing.get("minimum_basket_type", "None"),
    }


def export_csv(rows, output_path):
    rows_sorted = sorted(rows, key=lambda r: r.get("distance_m", 0))
    with open(output_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows_sorted)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Wolt Restaurant Pricing Analyzer")
    parser.add_argument("address", type=str)
    parser.add_argument("--output", type=str, default=None)
    parser.add_argument("--token",  type=str, default=None)
    parser.add_argument("--limit",  type=int, default=None)
    parser.add_argument("--lat",    type=float, default=None)
    parser.add_argument("--lon",    type=float, default=None)
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = parse_args()
    output_path = args.output or f"wolt_pricing_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    print("\nAuthenticating with Wolt ...")
    access_token, auth_headers = resolve_auth(cli_token=args.token)
    if access_token:
        print("  -> Authenticated: PRIMARY original_delivery_price; "
              "FALLBACK base_price + distance-tier-a (without_subscription preferred).\n")
    else:
        print("  -> Unauthenticated: delivery_pricing fallback used.\n")

    if args.lat is not None and args.lon is not None:
        lat, lon = args.lat, args.lon
        print(f"Using override coordinates: lat={lat}, lon={lon}\n")
    else:
        print(f"Geocoding: {args.address!r} ...")
        lat, lon = geocode(args.address)
        print(f"  -> lat={lat:.6f}, lon={lon:.6f}\n")

    print("Fetching restaurant listings ...")
    venues = fetch_listings(lat, lon, auth_headers)
    print(f"  -> {len(venues)} unique restaurants found.\n")
    if not venues:
        raise SystemExit(0)
    if args.limit:
        venues = venues[:args.limit]

    rows = []
    for idx, venue in enumerate(venues, start=1):
        name = venue.get("name", venue.get("slug", "?"))
        print(f"  [{idx}/{len(venues)}] {name} ...")
        time.sleep(random.uniform(1.5, 2.5))
        if idx % 10 == 0:
            print(f"    [throttle] Extra 3s pause after venue {idx} ...")
            time.sleep(3.0)

        loc = venue.get("location", [None, None])
        venue_lon_val = loc[0] if isinstance(loc, (list, tuple)) and len(loc) == 2 else None
        venue_lat_val = loc[1] if isinstance(loc, (list, tuple)) and len(loc) == 2 else None
        hav_dist = (haversine(lat, lon, float(venue_lat_val), float(venue_lon_val))
                    if venue_lat_val is not None else 0)

        data = fetch_dynamic_pricing(venue["slug"], lat, lon, auth_headers)

        if data is None:
            pricing = {
                "service_fee_pct": "", "service_fee_min_eur": "",
                "service_fee_max_eur": "", "minimum_basket_eur": "",
                "minimum_basket_type": "None", "self_delivery": "No",
                "delivery_fee_source": "",
                "delivery_fee_eur": f"{venue.get('delivery_price_int', 0) / 100:.2f}",
            }
        else:
            pricing = extract_pricing(data)

            # ------------------------------------------------------------------
            # DELIVERY FEE: PRIMARY original_delivery_price -> FALLBACK tier-a
            # ------------------------------------------------------------------
            odp_cents = _get_original_delivery_price(data)

            if odp_cents is not None:
                pricing["delivery_fee_eur"]    = f"{odp_cents / 100:.2f}"
                pricing["delivery_fee_source"] = "primary:original_delivery_price"
            else:
                base_price, dist_ranges, src = _get_delivery_pricing_for_fallback(data)
                if dist_ranges:
                    fee_cents = _compute_fallback_delivery_fee(base_price, dist_ranges, hav_dist)
                    pricing["delivery_fee_eur"]    = f"{fee_cents / 100:.2f}"
                    pricing["delivery_fee_source"] = f"fallback:{src}"
                else:
                    fb = venue.get("delivery_price_int", 0) or 0
                    pricing["delivery_fee_eur"]    = f"{fb / 100:.2f}"
                    pricing["delivery_fee_source"] = "fallback:listing_price_int"

            print(
                f"    hav={hav_dist}m  fee=EUR{pricing['delivery_fee_eur']}"
                f"  [{pricing['delivery_fee_source']}]"
                f"  svc={pricing.get('service_fee_pct', '?')}%"
                f"  svc_min=EUR{pricing.get('service_fee_min_eur', '?')}"
                f"  svc_max=EUR{pricing.get('service_fee_max_eur', '?')}"
            )

        rows.append(build_row(venue, pricing, lat, lon))

    # ------------------------------------------------------------------
    # Post-run retry pass: re-fetch venues whose pricing is blank (429s)
    # ------------------------------------------------------------------
    failed_indices = [i for i, r in enumerate(rows) if not r.get("service_fee_pct")]
    if failed_indices:
        print(f"\n[RETRY] {len(failed_indices)} venue(s) missing pricing data; "
              f"waiting 30s before retry ...")
        time.sleep(30.0)
        for i in failed_indices:
            row  = rows[i]
            slug = row["slug"]
            name = row["restaurant_name"]
            print(f"  [RETRY] {name} ({slug}) ...")
            time.sleep(random.uniform(1.5, 2.5))

            venue_match = next((v for v in venues if v.get("slug") == slug), None)
            if venue_match is None:
                continue

            loc_r  = venue_match.get("location", [None, None])
            v_lon_r = loc_r[0] if isinstance(loc_r, (list, tuple)) and len(loc_r) == 2 else None
            v_lat_r = loc_r[1] if isinstance(loc_r, (list, tuple)) and len(loc_r) == 2 else None
            hav_dist_r = (haversine(lat, lon, float(v_lat_r), float(v_lon_r))
                          if v_lat_r is not None else 0)

            data2 = fetch_dynamic_pricing(slug, lat, lon, auth_headers)
            if data2 is None:
                print(f"    [RETRY] Still failed for {name}; keeping blank row.")
                continue

            pricing2 = extract_pricing(data2)
            odp2 = _get_original_delivery_price(data2)
            if odp2 is not None:
                pricing2["delivery_fee_eur"]    = f"{odp2 / 100:.2f}"
                pricing2["delivery_fee_source"] = "primary:original_delivery_price"
            else:
                bp2, dr2, src2 = _get_delivery_pricing_for_fallback(data2)
                if dr2:
                    fc2 = _compute_fallback_delivery_fee(bp2, dr2, hav_dist_r)
                    pricing2["delivery_fee_eur"]    = f"{fc2 / 100:.2f}"
                    pricing2["delivery_fee_source"] = f"fallback:{src2}"
                else:
                    fb2 = venue_match.get("delivery_price_int", 0) or 0
                    pricing2["delivery_fee_eur"]    = f"{fb2 / 100:.2f}"
                    pricing2["delivery_fee_source"] = "fallback:listing_price_int"

            rows[i] = build_row(venue_match, pricing2, lat, lon)
            print(
                f"    [RETRY] OK: fee=EUR{pricing2['delivery_fee_eur']}"
                f"  [{pricing2['delivery_fee_source']}]"
                f"  svc={pricing2.get('service_fee_pct', '?')}%"
                f"  svc_min=EUR{pricing2.get('service_fee_min_eur', '?')}"
                f"  svc_max=EUR{pricing2.get('service_fee_max_eur', '?')}"
            )

    export_csv(rows, output_path)
    print(f"\nDone! {len(rows)} restaurants -> {output_path}")


if __name__ == "__main__":
    main()
