#!/usr/bin/env python3
"""
Ceiling Radar
=============
Projects live aircraft from an ADS-B JSON feed onto a ceiling as glowing
sprites with fading trails and floating telemetry, on a pure-black background
(so a projector emits no light there and only the planes/text show up).

Designed for a Raspberry Pi 5 + HDMI projector, but runs on any machine.

Feed: dump1090 / dump1090-mutability style aircraft.json
      (fields: hex, flight, lat, lon, altitude, speed, track, ...)

Controls:
  c            toggle calibration overlay
  1 2 3 4      select keystone corner (TL TR BR BL)   [calibration]
  arrows       nudge selected corner (hold Shift = x10) [calibration]
  [ ]          zoom out / in (home radius)             [calibration]
  , .          rotate view (aim the red north compass) [calibration]
  ; '          rotate FRONT/BACK yard axis             [calibration]
  x            flip horizontally                       [calibration]
  l            toggle lookout cue (near + low aircraft)
  r            reset keystone to full frame            [calibration]
  s            save config to disk
  d            toggle demo mode (simulated traffic)
  f            toggle fullscreen
  q / Esc      quit

Run:
  uv run ceiling_radar.py                  # use config.json (or defaults)
  uv run ceiling_radar.py --demo           # simulated traffic, no receiver
  uv run ceiling_radar.py --windowed
"""

import os
import sys
import json
import math
import time
import random
import re
import argparse
import threading
from collections import deque

import numpy as np

try:
    import requests
except ImportError:
    requests = None

import pygame


def log(msg):
    print("[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

DEFAULT_CONFIG = {
    "feed": {
        # dump1090-mutability live aircraft JSON
        "url": "http://receiver.local/dump1090/data/aircraft.json",
        "poll_interval": 1.0,
        "demo": True,             # safe public default; set false in config.json
        "fallback_to_demo": True,  # if the feed can't be reached, use demo
        "retry_live_interval": 15.0  # seconds between live retries after fallback
    },
    "home": {
        # Your receiver location. CHANGE THIS in config.json.
        "lat": 0.0,
        "lon": 0.0
    },
    "view": {
        "range_m": 25000,   # metres from centre to the short edge of the image
        "north_deg": 0.0,   # rotate the whole view (point map-north anywhere)
        "flip_x": True      # mirror for ceiling projection; press x to toggle
    },
    "config": {
        "write_on_start": True,  # write merged defaults into config.json on launch
        "autosave": True         # save calibration/display toggles as you change them
    },
    "lookout": {
        "enabled": True,
        "front_yard_deg": 0.0,      # true bearing from home toward the front yard
        "near_radius_m": 3200,      # cue planes within about 2 miles of home
        "max_altitude_ft": 6000,    # ADS-B/barometric altitude, not height above roof
        "color": [255, 80, 40]
    },
    "distance_color": {
        "enabled": True,            # green near home, yellow midrange, red near edge
        "near_color": [40, 255, 120],
        "mid_color": [255, 220, 40],
        "far_color": [255, 60, 40]
    },
    "display": {
        "width": 1920,
        "height": 1080,
        "fullscreen": True,
        "fps": 60
    },
    "keystone": {
        # 4 output corners in screen pixels: [TL, TR, BR, BL].
        # null = full frame (no keystone). Tune in calibration mode.
        "corners": None
    },
    "enrich": {
        # Type and route are NOT in ADS-B; these fetch them from free APIs.
        # Both need internet; if offline, the app just shows what the feed has.
        "routes": True,    # origin -> destination via adsb.lol routeset (no key)
        "types": False,    # aircraft type via adsb.fi hex lookup (no key, 1 req/plane)
        "route_api": "https://api.adsb.lol/api/0/routeset",
        "callsign_api": "https://api.adsbdb.com/v0/callsign/",
        "type_api": "https://opendata.adsb.fi/api/v2/hex/",
        "aircraft_api": "https://api.adsbdb.com/v0/aircraft/",
        "min_interval": 2.0,   # seconds between route batches (be polite)
        "cache_file": "enrich_cache.json"
    },
    "style": {
        "plane_color": [255, 190, 70],
        "text_color": [235, 235, 235],
        "trail_color": [255, 150, 40],
        "trail_seconds": 75,
        "cross_marks": True,     # drop small x tick-marks along the tail
        "plane_size": 34,        # icon wingspan in pixels
        "label_size": 30,        # base font size
        "font_path": None,       # drop in Inter/Barlow .ttf for the photo look
        "glow": True
    }
}


def deep_merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if path and os.path.exists(path):
        with open(path) as f:
            cfg = deep_merge(cfg, json.load(f))
    return cfg


def save_config(cfg, path):
    if not path:
        return
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")


# --------------------------------------------------------------------------- #
# Geometry: geo -> local metres -> ideal canvas -> projector pixels (homography)
# --------------------------------------------------------------------------- #

M_PER_DEG = 111320.0
EARTH_RADIUS_KM = 6371.0


def geo_to_enu(lat, lon, lat0, lon0):
    """Local east/north metres relative to home (good for a city-scale view)."""
    north = (lat - lat0) * M_PER_DEG
    east = (lon - lon0) * M_PER_DEG * math.cos(math.radians(lat0))
    return east, north


def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def format_near_distance(metres):
    miles = metres / 1609.344
    if miles >= 0.95:
        return "%.1f mi" % miles
    return "%d ft" % round(metres * 3.28084)


def bearing_deg_from_enu(e, n):
    return (math.degrees(math.atan2(e, n)) + 360.0) % 360.0


def yard_direction_label(bearing_deg, front_yard_deg):
    rel = ((bearing_deg - front_yard_deg + 180.0) % 360.0) - 180.0
    if -45.0 <= rel <= 45.0:
        return "FRONT"
    if rel >= 135.0 or rel <= -135.0:
        return "BACK"
    if rel > 0:
        return "RIGHT"
    return "LEFT"


def route_matches_position(lat, lon, origin, dest, alt_ft=None):
    """True when current position/altitude makes the route plausible."""
    try:
        olat, olon = float(origin["lat"]), float(origin["lon"])
        dlat, dlon = float(dest["lat"]), float(dest["lon"])
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError, KeyError):
        return False

    route_len = haversine_km(olat, olon, dlat, dlon)
    if route_len < 25:
        return False

    dist_to_origin = haversine_km(lat, lon, olat, olon)
    dist_to_dest = haversine_km(lat, lon, dlat, dlon)
    try:
        alt = float(alt_ft) if alt_ft is not None else None
    except (TypeError, ValueError):
        alt = None
    if alt is not None and alt <= 8000:
        endpoint_limit = max(65.0, min(140.0, alt * 0.018))
        if min(dist_to_origin, dist_to_dest) > endpoint_limit:
            return False

    ref_lat = math.radians((olat + dlat + lat) / 3.0)

    def xy(la, lo):
        return (EARTH_RADIUS_KM * math.radians(lo) * math.cos(ref_lat),
                EARTH_RADIUS_KM * math.radians(la))

    ox, oy = xy(olat, olon)
    dx, dy = xy(dlat, dlon)
    px, py = xy(lat, lon)
    vx, vy = dx - ox, dy - oy
    wx, wy = px - ox, py - oy
    vv = vx * vx + vy * vy
    if vv <= 0:
        return False
    t = (wx * vx + wy * vy) / vv
    t_clamped = max(0.0, min(1.0, t))
    cx, cy = ox + t_clamped * vx, oy + t_clamped * vy
    corridor = max(70.0, min(220.0, route_len * 0.08))
    off_route = math.hypot(px - cx, py - cy)
    beyond_route = max(0.0, -t, t - 1.0) * route_len
    return off_route <= corridor and beyond_route <= 160.0


def homography_from_corners(src, dst):
    """3x3 homography mapping the 4 src points to the 4 dst points (DLT)."""
    A = []
    for (x, y), (u, v) in zip(src, dst):
        A.append([x, y, 1, 0, 0, 0, -u * x, -u * y, -u])
        A.append([0, 0, 0, x, y, 1, -v * x, -v * y, -v])
    A = np.asarray(A, dtype=float)
    _, _, Vt = np.linalg.svd(A)
    H = Vt[-1].reshape(3, 3)
    return H / H[2, 2]


def apply_homography(H, x, y):
    d = H[2, 0] * x + H[2, 1] * y + H[2, 2]
    if abs(d) < 1e-12:
        d = 1e-12
    return ((H[0, 0] * x + H[0, 1] * y + H[0, 2]) / d,
            (H[1, 0] * x + H[1, 1] * y + H[1, 2]) / d)


class ViewState:
    """Holds the composed geo->screen transform; rebuilt when config changes."""

    def __init__(self, cfg, w, h):
        self.rebuild(cfg, w, h)

    def rebuild(self, cfg, w, h):
        self.w, self.h = w, h
        self.lat0 = cfg["home"]["lat"]
        self.lon0 = cfg["home"]["lon"]
        self.range_m = max(100.0, float(cfg["view"]["range_m"]))
        self.north_rad = math.radians(cfg["view"]["north_deg"])
        self.flip_x = bool(cfg["view"]["flip_x"])
        self.cx, self.cy = w / 2.0, h / 2.0
        self.scale = (min(w, h) / 2.0) / self.range_m
        corners = cfg["keystone"]["corners"]
        if not corners:
            corners = [[0, 0], [w, 0], [w, h], [0, h]]
        self.corners = [list(map(float, c)) for c in corners]
        self.src = [(0.0, 0.0), (w, 0.0), (w, h), (0.0, h)]
        self.H = homography_from_corners(self.src, [tuple(c) for c in self.corners])

    def enu_to_screen(self, e, n):
        """Project local east/north metres through rotation, flip, and keystone."""
        a = self.north_rad
        fx = -1.0 if self.flip_x else 1.0
        cxn = self.cx + self.scale * fx * (e * math.cos(a) - n * math.sin(a))
        cyn = self.cy - self.scale * (e * math.sin(a) + n * math.cos(a))
        return apply_homography(self.H, cxn, cyn)

    def geo_to_screen(self, lat, lon):
        e, n = geo_to_enu(lat, lon, self.lat0, self.lon0)
        return self.enu_to_screen(e, n)

    def in_range(self, lat, lon, margin=1.3):
        e, n = geo_to_enu(lat, lon, self.lat0, self.lon0)
        return math.hypot(e, n) <= self.range_m * margin


# --------------------------------------------------------------------------- #
# Feed parsing (dump1090 / dump1090-mutability)
# --------------------------------------------------------------------------- #

def parse_dump1090(payload, now):
    """Normalise a dump1090 aircraft.json into a list of fix dicts."""
    fixes = []
    for a in payload.get("aircraft", []):
        lat, lon = a.get("lat"), a.get("lon")
        if lat is None or lon is None:
            continue  # aircraft heard but not yet positioned
        alt = a.get("altitude", a.get("alt_baro"))
        if isinstance(alt, str):       # "ground"
            alt = 0
        fixes.append({
            "hex": a.get("hex", "").lower(),
            "callsign": (a.get("flight") or "").strip(),
            "lat": float(lat),
            "lon": float(lon),
            "alt": int(alt) if alt is not None else None,
            "speed": a.get("speed", a.get("gs")),
            "track": a.get("track", 0) or 0,
            "type": a.get("type"),     # absent in mutability; demo/enrich fills
            "route": a.get("route"),
            "t_fix": now,
        })
    return fixes


# --------------------------------------------------------------------------- #
# Demo source (simulated traffic so anyone can run with no receiver)
# --------------------------------------------------------------------------- #

class DemoSource:
    _airlines = [
        ("SWA", "Boeing 737-8"), ("UAL", "Boeing 777-200"),
        ("AAL", "Airbus A321"), ("DAL", "Airbus A220-300"),
        ("FFT", "Airbus A320neo"), ("FDX", "Boeing 767-300F"),
        ("JBU", "Airbus A320"), ("SKW", "Embraer E175"),
    ]
    _airports = ["DEN", "OAK", "LAX", "ORD", "ATL", "SEA", "PHX", "DFW", "JFK", "AUS"]

    def __init__(self, cfg, n=8):
        self.lat0 = cfg["home"]["lat"]
        self.lon0 = cfg["home"]["lon"]
        self.range_m = cfg["view"]["range_m"]
        self.last = time.time()
        self.planes = [self._spawn() for _ in range(n)]

    def _spawn(self):
        ang = random.uniform(0, 2 * math.pi)
        r = random.uniform(0.2, 1.1) * self.range_m
        e, n = r * math.cos(ang), r * math.sin(ang)
        lat = self.lat0 + n / M_PER_DEG
        lon = self.lon0 + e / (M_PER_DEG * math.cos(math.radians(self.lat0)))
        al, ty = random.choice(self._airlines)
        o, d = random.sample(self._airports, 2)
        return {
            "hex": "%06x" % random.randint(0, 0xFFFFFF),
            "callsign": "%s%d" % (al, random.randint(100, 4999)),
            "lat": lat, "lon": lon,
            "alt": random.choice(range(2000, 39000, 500)),
            "speed": random.randint(160, 480),
            "track": random.uniform(0, 360),
            "type": ty, "route": "%s \u2192 %s" % (o, d),
        }

    def poll(self):
        now = time.time()
        dt = now - self.last
        self.last = now
        out = []
        for p in self.planes:
            p["track"] = (p["track"] + random.uniform(-2, 2)) % 360
            dist = p["speed"] * 0.514444 * dt
            dn = dist * math.cos(math.radians(p["track"]))
            de = dist * math.sin(math.radians(p["track"]))
            p["lat"] += dn / M_PER_DEG
            p["lon"] += de / (M_PER_DEG * math.cos(math.radians(p["lat"])))
            e, n = geo_to_enu(p["lat"], p["lon"], self.lat0, self.lon0)
            if math.hypot(e, n) > self.range_m * 1.4:   # wandered off; respawn
                p.update(self._spawn())
            out.append({**p, "t_fix": now})
        return out


# --------------------------------------------------------------------------- #
# Optional enrichment (route/type via free APIs; off for type by default)
# --------------------------------------------------------------------------- #

class Enricher:
    """Fills aircraft type and route (origin -> destination) from free APIs.

    Route lookup first tries adsb.lol's batched routeset endpoint, then falls
    back to adsbdb's per-callsign API. Type lookup tries adsb.fi first, then
    adsbdb's per-aircraft API. All providers degrade silently offline.
    Runs entirely inside the poller thread, so no locking is needed.
    """

    def __init__(self, cfg):
        e = cfg["enrich"]
        self.do_routes = bool(e.get("routes")) and requests is not None
        self.do_types = bool(e.get("types")) and requests is not None
        self.route_api = e.get("route_api", "https://api.adsb.lol/api/0/routeset")
        self.callsign_api = e.get("callsign_api", "https://api.adsbdb.com/v0/callsign/")
        self.type_api = e.get("type_api", "https://opendata.adsb.fi/api/v2/hex/")
        self.aircraft_api = e.get("aircraft_api", "https://api.adsbdb.com/v0/aircraft/")
        self.min_interval = float(e.get("min_interval", 2.0))
        self.cache_file = e.get("cache_file", "enrich_cache.json")
        self.routes = {}   # callsign -> "DEN -> OAK" or None
        self.types = {}    # hex -> "Boeing 737-8" or None
        if os.path.exists(self.cache_file):
            try:
                c = json.load(open(self.cache_file))
                if c.get("route_cache_version") == 3:
                    self.routes = c.get("routes", {})
                self.types = c.get("types", {})
            except Exception:
                pass
        self._route_q = {}        # callsign -> (lat, lon, alt_ft) awaiting lookup
        self._route_fallback_q = deque()
        self._route_pos = {}
        self._route_reject_until = {}
        self._type_q = deque()    # hex awaiting lookup
        self._last_route = 0.0
        self._last_route_fallback = 0.0
        self._dirty = False
        self.status = self._status()

    def annotate(self, fix):
        """Fill type/route from cache; queue anything unknown. Mutates fix."""
        cs = fix.get("callsign")
        if self.do_routes and cs and not fix.get("route"):
            if cs in self.routes:
                route = self._route_from_cache(cs, fix["lat"], fix["lon"], fix.get("alt"))
                if route:
                    fix["route"] = route
                elif self.routes[cs] is not None:
                    self.routes.pop(cs, None)
                    self._dirty = True
                    self._queue_route_fallback(cs, fix["lat"], fix["lon"], fix.get("alt"))
            elif self._route_reject_until.get(cs, 0) > time.time():
                pass
            elif cs not in self._route_fallback_q:
                self._route_q[cs] = (fix["lat"], fix["lon"], fix.get("alt"))
        hx = fix.get("hex")
        if self.do_types and hx and not fix.get("type"):
            if hx in self.types:
                if self.types[hx]:
                    fix["type"] = self.types[hx]
            elif hx not in self._type_q:
                self._type_q.append(hx)
        return fix

    @staticmethod
    def fmt_route(item):
        codes = (item.get("_airport_codes_iata") or "").strip()
        if not codes or "unknown" in codes.lower():
            return None
        parts = [p for p in codes.split("-") if p]
        if len(parts) >= 2:
            return "%s \u2192 %s" % (parts[0], parts[-1])
        return None

    @staticmethod
    def fmt_adsbdb_route(payload):
        route = ((payload.get("response") or {}).get("flightroute") or {})
        origin = route.get("origin") or {}
        dest = route.get("destination") or {}
        o = origin.get("iata_code") or origin.get("icao_code")
        d = dest.get("iata_code") or dest.get("icao_code")
        if o and d:
            return "%s \u2192 %s" % (o, d)
        return None

    @staticmethod
    def route_record_from_adsbdb(payload, lat, lon, alt_ft=None):
        route = ((payload.get("response") or {}).get("flightroute") or {})
        origin = route.get("origin") or {}
        dest = route.get("destination") or {}
        name = Enricher.fmt_adsbdb_route(payload)
        if not name:
            return None
        rec = {
            "route": name,
            "origin": {
                "code": origin.get("iata_code") or origin.get("icao_code"),
                "lat": origin.get("latitude"),
                "lon": origin.get("longitude"),
            },
            "destination": {
                "code": dest.get("iata_code") or dest.get("icao_code"),
                "lat": dest.get("latitude"),
                "lon": dest.get("longitude"),
            },
            "validated_at": time.time(),
        }
        if route_matches_position(lat, lon, rec["origin"], rec["destination"], alt_ft):
            return rec
        return None

    @staticmethod
    def fmt_type(payload):
        acs = payload.get("ac")
        if isinstance(acs, list) and acs:
            ac = acs[0] or {}
            return ac.get("desc") or ac.get("t")
        ac = ((payload.get("response") or {}).get("aircraft") or {})
        if ac:
            maker = ac.get("manufacturer")
            model = ac.get("type") or ac.get("icao_type")
            if maker and model and maker.lower() not in model.lower():
                return "%s %s" % (maker, model)
            return model
        return None

    def _queue_route_fallback(self, callsign, lat=None, lon=None, alt_ft=None):
        if lat is not None and lon is not None:
            self._route_pos[callsign] = (lat, lon, alt_ft)
        if callsign not in self._route_fallback_q:
            self._route_fallback_q.append(callsign)

    def _route_from_cache(self, callsign, lat, lon, alt_ft=None):
        entry = self.routes.get(callsign)
        if not entry:
            return None
        if not isinstance(entry, dict):
            return None
        if route_matches_position(lat, lon, entry.get("origin"), entry.get("destination"), alt_ft):
            return entry.get("route")
        return None

    def _status(self, note=None):
        parts = []
        if self.do_routes:
            filled = sum(1 for v in self.routes.values() if v)
            parts.append("routes %d/%d" % (filled, len(self.routes)))
        else:
            parts.append("routes off")
        if self.do_types:
            filled = sum(1 for v in self.types.values() if v)
            parts.append("types %d/%d" % (filled, len(self.types)))
        else:
            parts.append("types off")
        if note:
            parts.append(note)
        return "enrich: " + ", ".join(parts)

    def work(self):
        """Do a little network work per call (from the poller thread)."""
        now = time.time()
        if (self.do_routes and self._route_q
                and now - self._last_route >= self.min_interval):
            self._last_route = now
            batch = list(self._route_q.items())[:100]
            planes = [{"callsign": cs, "lat": ll[0], "lng": ll[1]}
                      for cs, ll in batch]
            matched = set()
            try:
                r = requests.post(self.route_api, json={"planes": planes},
                                  headers={"accept": "application/json"}, timeout=5)
                r.raise_for_status()
                text = r.text.strip()
                rows = r.json() if text else []
                for item in rows:
                    cs = (item.get("callsign") or "").strip()
                    if cs:
                        route = self.fmt_route(item)
                        if route:
                            matched.add(cs)
                        else:
                            self._queue_route_fallback(cs, *self._route_q.get(cs, (None, None, None)))
                for cs, _ in batch:
                    self._queue_route_fallback(cs, *self._route_q.get(cs, (None, None, None)))
                    self._route_q.pop(cs, None)
                self._dirty = True
                self.status = self._status("routeset %d/%d" % (len(matched), len(batch)))
            except Exception as e:
                self.status = self._status("routeset error: %s" % type(e).__name__)
        if (self.do_routes and self._route_fallback_q
                and now - self._last_route_fallback >= self.min_interval):
            self._last_route_fallback = now
            cs = self._route_fallback_q.popleft()
            lat, lon, alt_ft = self._route_pos.get(cs, (None, None, None))
            try:
                r = requests.get(self.callsign_api + cs, timeout=5)
                if r.status_code == 404:
                    self.routes.pop(cs, None)
                else:
                    r.raise_for_status()
                    self.routes[cs] = self.route_record_from_adsbdb(r.json(), lat, lon, alt_ft)
                    if not self.routes[cs]:
                        self.routes.pop(cs, None)
                if cs not in self.routes:
                    self._route_reject_until[cs] = time.time() + 900.0
                self._dirty = True
                self.status = self._status("route %s" % ("verified" if cs in self.routes else "rejected"))
            except Exception as e:
                self._queue_route_fallback(cs, lat, lon, alt_ft)
                self.status = self._status("callsign error: %s" % type(e).__name__)
        if self.do_types and self._type_q:
            hx = self._type_q.popleft()
            try:
                r = requests.get(self.type_api + hx, timeout=4)
                r.raise_for_status()
                self.types[hx] = self.fmt_type(r.json())
            except Exception as first_error:
                try:
                    r = requests.get(self.aircraft_api + hx, timeout=5)
                    if r.status_code == 404:
                        self.types[hx] = None
                    else:
                        r.raise_for_status()
                        self.types[hx] = self.fmt_type(r.json())
                except Exception:
                    self.types[hx] = None
                    self.status = self._status("type error: %s" % type(first_error).__name__)
                else:
                    self.status = self._status("type %s" % ("ok" if self.types[hx] else "none"))
            else:
                self.status = self._status("type %s" % ("ok" if self.types[hx] else "none"))
            self._dirty = True

    def flush(self):
        if self._dirty:
            try:
                json.dump({
                    "route_cache_version": 3,
                    "routes": self.routes,
                    "types": self.types,
                },
                          open(self.cache_file, "w"))
            except Exception:
                pass
            self._dirty = False


# --------------------------------------------------------------------------- #
# Background feed poller
# --------------------------------------------------------------------------- #

class Poller(threading.Thread):
    def __init__(self, cfg, enricher):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.enricher = enricher
        self.lock = threading.Lock()
        self.fixes = []
        self.demo = cfg["feed"]["demo"]
        self.demo_source = DemoSource(cfg) if self.demo else None
        self.running = True
        self.status = "starting"
        self.fallback_reason = None
        self.next_live_retry = 0.0
        self.live_retry_interval = float(cfg["feed"].get("retry_live_interval", 15.0))
        log("feed starting in %s mode; url=%s; fallback_to_demo=%s" % (
            "demo" if self.demo else "live",
            cfg["feed"]["url"],
            cfg["feed"]["fallback_to_demo"],
        ))

    def set_demo(self, on, reason=None):
        self.demo = on
        self.demo_source = DemoSource(self.cfg) if on else None
        self.fallback_reason = reason if on else None
        self.next_live_retry = (
            time.time() + self.live_retry_interval
            if on and reason else 0.0
        )
        if on:
            if reason:
                log("switching to demo mode after feed error: %s" % reason)
            else:
                log("demo mode enabled")
        else:
            log("live mode enabled")

    def _fetch_real(self):
        if requests is None:
            raise RuntimeError("requests not installed")
        r = requests.get(self.cfg["feed"]["url"], timeout=4)
        r.raise_for_status()
        return parse_dump1090(r.json(), time.time())

    def _poll_demo_fallback(self, now):
        if self.fallback_reason and now >= self.next_live_retry:
            try:
                fixes = self._fetch_real()
                self.demo = False
                self.demo_source = None
                self.fallback_reason = None
                self.next_live_retry = 0.0
                self.status = "live"
                log("live feed recovered")
                return fixes
            except Exception as e:
                err = "%s: %s" % (type(e).__name__, e)
                self.fallback_reason = err
                self.next_live_retry = now + self.live_retry_interval
                log("live retry failed: %s" % err)
        fixes = self.demo_source.poll() if self.demo_source else []
        if self.fallback_reason:
            self.status = "demo fallback: %s" % self.fallback_reason
        else:
            self.status = "demo"
        return fixes

    def run(self):
        while self.running:
            t0 = time.time()
            try:
                now = time.time()
                if self.demo:
                    fixes = self._poll_demo_fallback(now)
                else:
                    fixes = self._fetch_real()
                    self.status = "live"
            except Exception as e:
                err = "%s: %s" % (type(e).__name__, e)
                log("feed error from %s: %s" % (self.cfg["feed"]["url"], err))
                self.status = "feed error: %s" % e
                if self.cfg["feed"]["fallback_to_demo"] and not self.demo:
                    self.set_demo(True, err)
                fixes = self.demo_source.poll() if self.demo_source else []
            for fx in fixes:
                self.enricher.annotate(fx)
            with self.lock:
                self.fixes = fixes
            self.enricher.work()
            self.enricher.flush()
            if self.status in ("live", "demo"):
                self.status = "%s | %s" % (self.status, self.enricher.status)
            time.sleep(max(0.05, self.cfg["feed"]["poll_interval"] - (time.time() - t0)))

    def snapshot(self):
        with self.lock:
            return list(self.fixes), self.status


# --------------------------------------------------------------------------- #
# Aircraft model with dead-reckoning smoothing + trail
# --------------------------------------------------------------------------- #

class Aircraft:
    def __init__(self, fix):
        self.hex = fix["hex"]
        self.disp_lat = fix["lat"]
        self.disp_lon = fix["lon"]
        self.trail = deque(maxlen=400)
        self._last_trail_t = 0.0
        self.update(fix)

    def update(self, fix):
        self.callsign = fix["callsign"] or self.hex.upper()
        self.lat = fix["lat"]
        self.lon = fix["lon"]
        self.alt = fix["alt"]
        self.speed = fix["speed"] or 0
        self.track = fix["track"] or 0
        self.type = fix.get("type")
        self.route = fix.get("route")
        self.t_fix = fix["t_fix"]

    def target(self, now):
        """Extrapolate from the last fix along its track (dead reckoning)."""
        dt = max(0.0, now - self.t_fix)
        dist = (self.speed or 0) * 0.514444 * dt
        dn = dist * math.cos(math.radians(self.track))
        de = dist * math.sin(math.radians(self.track))
        lat = self.lat + dn / M_PER_DEG
        lon = self.lon + de / (M_PER_DEG * math.cos(math.radians(self.lat)))
        return lat, lon

    def step(self, now, k=0.2):
        tlat, tlon = self.target(now)
        self.disp_lat += (tlat - self.disp_lat) * k
        self.disp_lon += (tlon - self.disp_lon) * k
        if now - self._last_trail_t > 0.25:
            self.trail.append((self.disp_lat, self.disp_lon, now))
            self._last_trail_t = now


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def scale_color(c, f):
    f = max(0.0, min(1.0, f))
    return (int(c[0] * f), int(c[1] * f), int(c[2] * f))


def mix_color(a, b, t):
    t = max(0.0, min(1.0, t))
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def distance_gradient_color(frac, near, mid, far):
    frac = max(0.0, min(1.0, frac))
    if frac <= 0.5:
        return mix_color(near, mid, frac * 2.0)
    return mix_color(mid, far, (frac - 0.5) * 2.0)


def make_glow(radius, color):
    surf = pygame.Surface((radius * 2, radius * 2))
    surf.fill((0, 0, 0))
    for r in range(radius, 0, -1):
        inten = (1 - r / radius) ** 2
        pygame.draw.circle(surf, scale_color(color, inten), (radius, radius), r)
    return surf


# top-down airliner silhouette in a ~40x44 box, nose up
PLANE_POLY = [
    (20, 0), (24, 14), (40, 24), (40, 28), (24, 22), (23, 34),
    (30, 40), (30, 43), (20, 38), (10, 43), (10, 40), (17, 34),
    (16, 22), (0, 28), (0, 24), (16, 14),
]


def make_plane_sprite(size, color):
    base = pygame.Surface((44, 44), pygame.SRCALPHA)
    pygame.draw.polygon(base, (*color, 255), PLANE_POLY)
    s = size / 40.0
    return pygame.transform.smoothscale(base, (int(44 * s), int(44 * s)))


class Renderer:
    def __init__(self, screen, cfg):
        self.screen = screen
        self.apply_style(cfg)

    def apply_style(self, cfg):
        st = cfg["style"]
        dc = cfg.get("distance_color", {})
        self.glow_on = st["glow"]
        self.plane_color = tuple(st["plane_color"])
        self.text_color = tuple(st["text_color"])
        self.trail_color = tuple(st["trail_color"])
        self.distance_color_on = bool(dc.get("enabled", False))
        self.distance_near_color = tuple(dc.get("near_color", [40, 255, 120]))
        self.distance_mid_color = tuple(dc.get("mid_color", [255, 220, 40]))
        self.distance_far_color = tuple(dc.get("far_color", [255, 60, 40]))
        self.apply_lookout(cfg)
        self.trail_seconds = st["trail_seconds"]
        self.cross_marks = st.get("cross_marks", True)
        self.plane_size = st["plane_size"]
        fp = st["font_path"]
        ls = st["label_size"]
        self.font_big = pygame.font.Font(fp, int(ls * 1.25))
        self.font_big.set_bold(True)
        self.font_small = pygame.font.Font(fp, int(ls * 0.78))
        self.font_lookout = pygame.font.Font(fp, int(ls * 0.95))
        self.font_lookout.set_bold(True)
        self.font_demo = pygame.font.Font(fp, max(44, int(ls * 1.8)))
        self.font_demo.set_bold(True)
        self.font_demo_small = pygame.font.Font(fp, max(20, int(ls * 0.8)))
        self.plane_sprite = make_plane_sprite(self.plane_size, self.plane_color)
        self.glow = make_glow(int(self.plane_size * 1.25), self.plane_color)
        self._sprite_cache = {}
        self._glow_cache = {}

    def apply_lookout(self, cfg):
        lo = cfg.get("lookout", {})
        self.lookout_on = bool(lo.get("enabled", True))
        self.lookout_front_deg = float(lo.get("front_yard_deg", 0.0))
        self.lookout_radius_m = float(lo.get("near_radius_m", 3200.0))
        self.lookout_alt_ft = int(lo.get("max_altitude_ft", 6000))
        self.lookout_color = tuple(lo.get("color", [255, 80, 40]))

    def _cross(self, pt, size, color):
        x, y = int(pt[0]), int(pt[1])
        pygame.draw.line(self.screen, color, (x - size, y - size), (x + size, y + size), 1)
        pygame.draw.line(self.screen, color, (x - size, y + size), (x + size, y - size), 1)

    def _distance_fraction(self, view, lat, lon):
        e, n = geo_to_enu(lat, lon, view.lat0, view.lon0)
        return math.hypot(e, n) / max(1.0, view.range_m)

    def _distance_color(self, view, lat, lon):
        if not self.distance_color_on:
            return self.plane_color
        frac = self._distance_fraction(view, lat, lon)
        frac = round(max(0.0, min(1.0, frac)) * 32.0) / 32.0
        return distance_gradient_color(
            frac,
            self.distance_near_color,
            self.distance_mid_color,
            self.distance_far_color
        )

    def _colored_sprite(self, color):
        key = tuple(color)
        if key not in self._sprite_cache:
            self._sprite_cache[key] = make_plane_sprite(self.plane_size, key)
        return self._sprite_cache[key]

    def _colored_glow(self, color):
        key = tuple(color)
        if key not in self._glow_cache:
            self._glow_cache[key] = make_glow(int(self.plane_size * 1.25), key)
        return self._glow_cache[key]

    def draw_trail(self, view, ac, now):
        trail = [(la, lo, t) for (la, lo, t) in ac.trail
                 if now - t <= self.trail_seconds]
        pts = [view.geo_to_screen(la, lo) for (la, lo, _) in trail]
        if len(pts) < 2:
            return
        n = len(pts)
        for i in range(1, n):                  # wide dim glow pass
            f = i / n
            trail_color = self._distance_color(view, trail[i][0], trail[i][1])
            pygame.draw.line(self.screen, scale_color(trail_color, 0.18 * f),
                             pts[i - 1], pts[i], 4)
        for i in range(1, n):                  # thin bright comet line
            f = i / n
            trail_color = self._distance_color(view, trail[i][0], trail[i][1])
            pygame.draw.line(self.screen, scale_color(trail_color, f),
                             pts[i - 1], pts[i], 2)
        if self.cross_marks:                   # x tick-marks down the tail
            step = max(3, n // 8)
            for i in range(0, n - 1, step):
                f = (i + 1) / n
                trail_color = self._distance_color(view, trail[i][0], trail[i][1])
                self._cross(pts[i], 5, scale_color(trail_color, 0.4 * f + 0.16))

    def draw_plane(self, view, ac):
        x, y = view.geo_to_screen(ac.disp_lat, ac.disp_lon)
        if not (-80 <= x <= view.w + 80 and -80 <= y <= view.h + 80):
            return
        # heading from on-screen motion (handles north rotation, flip, keystone)
        ahead = view.range_m * 0.02
        dn = ahead * math.cos(math.radians(ac.track))
        de = ahead * math.sin(math.radians(ac.track))
        la2 = ac.disp_lat + dn / M_PER_DEG
        lo2 = ac.disp_lon + de / (M_PER_DEG * math.cos(math.radians(ac.disp_lat)))
        x2, y2 = view.geo_to_screen(la2, lo2)
        ang = -math.degrees(math.atan2(x2 - x, -(y2 - y)))  # nose-up sprite

        plane_color = self._distance_color(view, ac.disp_lat, ac.disp_lon)
        if self.glow_on:
            g = self._colored_glow(plane_color)
            self.screen.blit(g, (x - g.get_width() / 2, y - g.get_height() / 2),
                             special_flags=pygame.BLEND_RGB_ADD)
        sprite = pygame.transform.rotozoom(self._colored_sprite(plane_color), ang, 1.0)
        self.screen.blit(sprite, (x - sprite.get_width() / 2,
                                  y - sprite.get_height() / 2))
        self.draw_label(view, ac, x, y)

    def _lookout_text(self, view, ac):
        if not self.lookout_on or ac.alt is None or ac.alt > self.lookout_alt_ft:
            return None
        e, n = geo_to_enu(ac.disp_lat, ac.disp_lon, view.lat0, view.lon0)
        dist = math.hypot(e, n)
        if dist > self.lookout_radius_m:
            return None
        direction = yard_direction_label(bearing_deg_from_enu(e, n),
                                         self.lookout_front_deg)
        return "LOOK %s  *  %s  %s ft" % (
            direction, format_near_distance(dist), format(ac.alt, ","))

    def _draw_backed_text(self, text, x, y, font, color):
        surf = font.render(text, True, color)
        rect = surf.get_rect(topleft=(int(x), int(y)))
        pad = 5
        bg = pygame.Rect(rect.x - pad, rect.y - pad,
                         rect.w + pad * 2, rect.h + pad * 2)
        pygame.draw.rect(self.screen, (0, 0, 0), bg)
        pygame.draw.rect(self.screen, color, bg, 2)
        self.screen.blit(surf, rect)
        return rect.h + pad * 2

    def draw_label(self, view, ac, x, y):
        ox, oy = self.plane_size * 0.9, -self.plane_size * 1.1
        cy = y + oy
        lookout = self._lookout_text(view, ac)
        if lookout:
            pygame.draw.circle(self.screen, self.lookout_color,
                               (int(x), int(y)), int(self.plane_size * 0.9), 3)
            cy -= self.font_lookout.get_height() + 12
            cy += self._draw_backed_text(lookout, x + ox, cy,
                                         self.font_lookout, self.lookout_color) + 3
        s = self.font_big.render(ac.callsign, True, self.text_color)
        self.screen.blit(s, (x + ox, cy))
        cy += s.get_height() + 1
        sub = []
        if ac.type:
            sub.append(ac.type)
        if ac.alt is not None:
            sub.append("%s ft" % format(ac.alt, ","))
        if ac.speed:
            sub.append("%d kt" % ac.speed)
        if sub:
            s = self.font_small.render("   ".join(sub), True, self.text_color)
            self.screen.blit(s, (x + ox, cy))
            cy += s.get_height() + 1
        if ac.route:
            self._draw_route(ac.route, x + ox, cy)

    def _draw_route(self, route, x, y):
        parts = re.split(r"\s*(?:->|\u2192|>)\s*", route, maxsplit=1)
        f, col = self.font_small, self.text_color
        o = f.render(parts[0], True, col)
        self.screen.blit(o, (x, y))
        if len(parts) < 2:
            return
        ax = x + o.get_width() + 8
        my = y + o.get_height() // 2
        aw = 16
        pygame.draw.line(self.screen, col, (ax, my), (ax + aw, my), 2)
        pygame.draw.polygon(self.screen, col,
                            [(ax + aw, my - 4), (ax + aw + 6, my), (ax + aw, my + 4)])
        d = f.render(parts[1], True, col)
        self.screen.blit(d, (ax + aw + 11, y))

    def draw_demo_banner(self, status):
        if not status.startswith("demo"):
            return
        headline = "DEMO MODE"
        detail = "simulated aircraft"
        if status.startswith("demo fallback:"):
            detail = "live feed unavailable"
        color = (255, 70, 45)
        text = self.font_demo.render(headline, True, color)
        detail_text = self.font_demo_small.render(detail, True, color)
        width = max(text.get_width(), detail_text.get_width())
        pad_x = 22
        pad_y = 12
        gap = 4
        rect = pygame.Rect(
            0, 0,
            width + pad_x * 2,
            text.get_height() + detail_text.get_height() + gap + pad_y * 2
        )
        rect.midtop = (self.screen.get_width() // 2, 22)
        pygame.draw.rect(self.screen, (0, 0, 0), rect)
        pygame.draw.rect(self.screen, color, rect, 3)
        self.screen.blit(text, text.get_rect(centerx=rect.centerx, top=rect.top + pad_y))
        self.screen.blit(
            detail_text,
            detail_text.get_rect(centerx=rect.centerx,
                                 top=rect.top + pad_y + text.get_height() + gap)
        )


# --------------------------------------------------------------------------- #
# Calibration overlay
# --------------------------------------------------------------------------- #

class Calibrator:
    LABELS = ["TL", "TR", "BR", "BL"]
    NORTH_RED = (255, 20, 20)
    NORTH_GLOW = (120, 0, 0)
    COMPASS_CYAN = (0, 240, 255)
    COMPASS_YELLOW = (255, 235, 0)
    COMPASS_MAGENTA = (255, 45, 210)
    COMPASS_WHITE = (255, 255, 255)
    GRID_GREEN = (60, 180, 100)
    HOME_ORANGE = (255, 140, 0)

    def __init__(self):
        self.active = False
        self.sel = 0
        self.font = pygame.font.Font(None, 26)
        self.font_range = pygame.font.Font(None, 34)
        self.font_range.set_bold(True)
        self.font_cardinal = pygame.font.Font(None, 64)
        self.font_cardinal.set_bold(True)
        self.font_north = pygame.font.Font(None, 90)
        self.font_north.set_bold(True)

    def handle(self, event, cfg, view):
        """Returns True if the view must be rebuilt."""
        if event.type != pygame.KEYDOWN:
            return False
        k = event.key
        big = pygame.key.get_mods() & pygame.KMOD_SHIFT
        step = 10 if big else 1
        if k in (pygame.K_1, pygame.K_2, pygame.K_3, pygame.K_4):
            self.sel = k - pygame.K_1
            return False
        corners = view.corners
        if k == pygame.K_LEFT:
            corners[self.sel][0] -= step
        elif k == pygame.K_RIGHT:
            corners[self.sel][0] += step
        elif k == pygame.K_UP:
            corners[self.sel][1] -= step
        elif k == pygame.K_DOWN:
            corners[self.sel][1] += step
        elif k == pygame.K_LEFTBRACKET:
            cfg["view"]["range_m"] *= 1.05
        elif k == pygame.K_RIGHTBRACKET:
            cfg["view"]["range_m"] /= 1.05
        elif k == pygame.K_COMMA:
            cfg["view"]["north_deg"] -= 1
        elif k == pygame.K_PERIOD:
            cfg["view"]["north_deg"] += 1
        elif k == pygame.K_SEMICOLON:
            lo = cfg.setdefault("lookout", {})
            lo["front_yard_deg"] = (float(lo.get("front_yard_deg", 0.0)) - 1) % 360
        elif k == pygame.K_QUOTE:
            lo = cfg.setdefault("lookout", {})
            lo["front_yard_deg"] = (float(lo.get("front_yard_deg", 0.0)) + 1) % 360
        elif k == pygame.K_x:
            cfg["view"]["flip_x"] = not cfg["view"]["flip_x"]
        elif k == pygame.K_r:
            corners[:] = [[0, 0], [view.w, 0], [view.w, view.h], [0, view.h]]
        else:
            return False
        cfg["keystone"]["corners"] = corners
        return True

    def _draw_polyline_enu(self, screen, view, points, color, width=1, closed=False):
        pts = [view.enu_to_screen(e, n) for e, n in points]
        if len(pts) >= 2:
            pygame.draw.lines(screen, color, closed, pts, width)
        return pts

    def _draw_label_centered(self, screen, text, pos, font, color):
        surf = font.render(text, True, color)
        rect = surf.get_rect(center=(int(pos[0]), int(pos[1])))
        screen.blit(surf, rect)

    def _draw_label_backed(self, screen, text, pos, font, color):
        surf = font.render(text, True, color)
        rect = surf.get_rect(topleft=(int(pos[0]), int(pos[1])))
        pad = 4
        bg = pygame.Rect(rect.x - pad, rect.y - pad,
                         rect.w + pad * 2, rect.h + pad * 2)
        pygame.draw.rect(screen, (0, 0, 0), bg)
        pygame.draw.rect(screen, color, bg, 1)
        screen.blit(surf, rect)

    def _format_distance(self, metres, compact=False):
        miles = metres / 1609.344
        if compact:
            if miles >= 10:
                return "%.0f mi" % miles
            if miles >= 1:
                return "%.1f mi" % miles
            return "%d ft" % round(metres * 3.28084)
        km = metres / 1000.0
        if miles >= 10:
            return "%.0f mi / %.0f km" % (miles, km)
        if miles >= 1:
            return "%.1f mi / %.1f km" % (miles, km)
        return "%d ft / %d m" % (round(metres * 3.28084), round(metres))

    def _draw_circle_enu(self, screen, view, radius, color, width=1):
        pts = []
        for deg in range(0, 361, 5):
            a = math.radians(deg)
            pts.append((radius * math.sin(a), radius * math.cos(a)))
        self._draw_polyline_enu(screen, view, pts, color, width, closed=True)

    def _draw_range_map(self, screen, cfg, view):
        center = view.enu_to_screen(0, 0)
        for fraction, color, width in [
                (0.25, self.GRID_GREEN, 1),
                (0.50, self.COMPASS_CYAN, 2),
                (0.75, self.GRID_GREEN, 1),
                (1.00, self.COMPASS_YELLOW, 3)]:
            r = view.range_m * fraction
            self._draw_circle_enu(screen, view, r, color, width)
            label_pos = view.enu_to_screen(r * 0.72, -r * 0.72)
            self._draw_label_backed(
                screen, self._format_distance(r, compact=True),
                (label_pos[0] + 8, label_pos[1] - 16),
                self.font, color
            )

        for e, n in [(-view.range_m, 0), (view.range_m, 0),
                     (0, -view.range_m), (0, view.range_m)]:
            self._draw_polyline_enu(screen, view, [(0, 0), (e, n)], self.GRID_GREEN, 1)

        cx, cy = center
        pygame.draw.circle(screen, self.HOME_ORANGE, (int(cx), int(cy)), 24, 4)
        pygame.draw.circle(screen, self.COMPASS_WHITE, (int(cx), int(cy)), 9, 0)
        pygame.draw.line(screen, self.HOME_ORANGE, (cx - 34, cy), (cx + 34, cy), 3)
        pygame.draw.line(screen, self.HOME_ORANGE, (cx, cy - 34), (cx, cy + 34), 3)
        self._draw_label_centered(screen, "HOME", (cx, cy + 58),
                                  self.font_range, self.HOME_ORANGE)
        lookout = cfg.get("lookout", {})
        front_deg = math.radians(float(lookout.get("front_yard_deg", 0.0)))
        axis_len = view.range_m * 0.55
        front = (axis_len * math.sin(front_deg), axis_len * math.cos(front_deg))
        back = (-front[0], -front[1])
        self._draw_polyline_enu(screen, view, [back, front], self.HOME_ORANGE, 4)
        self._draw_label_centered(screen, "FRONT", view.enu_to_screen(*front),
                                  self.font_range, self.HOME_ORANGE)
        self._draw_label_centered(screen, "BACK", view.enu_to_screen(*back),
                                  self.font_range, self.HOME_ORANGE)

    def _draw_north_arrow(self, screen, view, radius):
        center = view.enu_to_screen(0, 0)
        tip = view.enu_to_screen(0, radius * 0.9)
        dx, dy = tip[0] - center[0], tip[1] - center[1]
        length = max(1.0, math.hypot(dx, dy))
        ux, uy = dx / length, dy / length
        px, py = -uy, ux
        base = (tip[0] - ux * 120, tip[1] - uy * 120)
        wide = max(52, min(view.w, view.h) * 0.055)
        tri = [
            (tip[0], tip[1]),
            (base[0] + px * wide, base[1] + py * wide),
            (base[0] - px * wide, base[1] - py * wide),
        ]

        pygame.draw.line(screen, self.NORTH_GLOW, center, tip, 22)
        pygame.draw.line(screen, self.NORTH_RED, center, tip, 10)
        pygame.draw.polygon(screen, self.NORTH_GLOW, tri, 0)
        pygame.draw.polygon(screen, self.NORTH_RED, tri, 0)
        pygame.draw.polygon(screen, self.COMPASS_WHITE, tri, 4)

        label = (tip[0] + ux * 58, tip[1] + uy * 58)
        self._draw_label_centered(screen, "N", label, self.font_north, self.NORTH_RED)

    def _draw_compass(self, screen, view):
        radius = view.range_m * 0.62
        ring = []
        for deg in range(0, 361, 5):
            a = math.radians(deg)
            ring.append((radius * math.sin(a), radius * math.cos(a)))
        self._draw_polyline_enu(screen, view, ring, self.COMPASS_WHITE, 3, closed=True)
        self._draw_polyline_enu(screen, view, ring, self.GRID_GREEN, 1, closed=True)

        for deg in range(0, 360, 15):
            a = math.radians(deg)
            outer = radius
            inner = radius * (0.88 if deg % 45 == 0 else 0.94)
            color = self.COMPASS_WHITE if deg % 45 == 0 else self.GRID_GREEN
            self._draw_polyline_enu(
                screen, view,
                [(inner * math.sin(a), inner * math.cos(a)),
                 (outer * math.sin(a), outer * math.cos(a))],
                color, 4 if deg % 45 == 0 else 2
            )

        axes = [
            ((0, -radius), (0, radius), self.NORTH_RED, 5),
            ((-radius, 0), (radius, 0), self.COMPASS_CYAN, 3),
        ]
        for p1, p2, color, width in axes:
            self._draw_polyline_enu(screen, view, [p1, p2], color, width)

        cardinals = [
            ("E", (radius * 1.1, 0), self.COMPASS_CYAN),
            ("S", (0, -radius * 1.1), self.COMPASS_YELLOW),
            ("W", (-radius * 1.1, 0), self.COMPASS_MAGENTA),
        ]
        for text, enu, color in cardinals:
            self._draw_label_centered(screen, text, view.enu_to_screen(*enu),
                                      self.font_cardinal, color)

        self._draw_north_arrow(screen, view, radius)

    def draw(self, screen, cfg, view, status):
        c = [tuple(map(int, p)) for p in view.corners]
        self._draw_range_map(screen, cfg, view)
        self._draw_compass(screen, view)
        pygame.draw.lines(screen, (60, 120, 90), True, c, 1)
        # range rings + crosshair
        cx, cy = apply_homography(view.H, view.cx, view.cy)
        pygame.draw.line(screen, (40, 80, 60), (cx - 18, cy), (cx + 18, cy), 1)
        pygame.draw.line(screen, (40, 80, 60), (cx, cy - 18), (cx, cy + 18), 1)
        for i, p in enumerate(c):
            col = (255, 220, 80) if i == self.sel else (90, 140, 110)
            pygame.draw.circle(screen, col, p, 9, 2)
            lbl = self.font.render(self.LABELS[i], True, col)
            screen.blit(lbl, (p[0] + 10, p[1] + 10))
        info = [
            "CALIBRATION  (c to exit)",
            "corner: %s   1/2/3/4 select, arrows nudge (Shift x10)" % self.LABELS[self.sel],
            "[ ] radius %s    diameter %s" % (
                self._format_distance(view.range_m),
                self._format_distance(view.range_m * 2.0)),
            "area %.0f sq mi    , . north %d deg    x flip %s" % (
                math.pi * (view.range_m / 1609.344) ** 2,
                cfg["view"]["north_deg"], cfg["view"]["flip_x"]),
            "; ' front yard %.0f deg" % float(cfg.get("lookout", {}).get("front_yard_deg", 0.0)),
            "l lookout %s    r reset corners    s save" % cfg.get("lookout", {}).get("enabled", True),
            "feed: %s" % status,
        ]
        y = 16
        for line in info:
            surf = self.font.render(line, True, (120, 200, 150))
            screen.blit(surf, (16, y))
            y += 24


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def selftest():
    # homography identity
    src = [(0, 0), (100, 0), (100, 80), (0, 80)]
    H = homography_from_corners(src, src)
    assert abs(apply_homography(H, 37, 21)[0] - 37) < 1e-6
    # keystone maps corners correctly
    dst = [(10, 5), (90, 0), (95, 78), (5, 80)]
    H2 = homography_from_corners(src, dst)
    for s, d in zip(src, dst):
        got = apply_homography(H2, *s)
        assert abs(got[0] - d[0]) < 1e-6 and abs(got[1] - d[1]) < 1e-6
    # geo round-trip
    e, n = geo_to_enu(1.01, 1.01, 1.0, 1.0)
    assert n > 0 and e > 0  # NE of home
    # route formatting from a routeset-style response item
    assert Enricher.fmt_route({"_airport_codes_iata": "DEN-OAK"}) == "DEN \u2192 OAK"
    assert Enricher.fmt_route({"_airport_codes_iata": "unknown"}) is None
    assert Enricher.fmt_route({"_airport_codes_iata": ""}) is None
    assert Enricher.fmt_adsbdb_route({
        "response": {"flightroute": {
            "origin": {"iata_code": "CLT"},
            "destination": {"iata_code": "BOS"},
        }}
    }) == "CLT \u2192 BOS"
    assert Enricher.fmt_type({
        "response": {"aircraft": {"manufacturer": "Boeing", "type": "737-8"}}
    }) == "Boeing 737-8"
    chs = {"lat": 32.8986, "lon": -80.0405}
    bos = {"lat": 42.3656, "lon": -71.0096}
    assert route_matches_position(38.5, -75.5, chs, bos)
    assert not route_matches_position(30.0, -97.0, chs, bos)
    dal = {"lat": 32.8471, "lon": -96.8518}
    lax = {"lat": 33.9425, "lon": -118.4081}
    aus_lat, aus_lon = 30.1945, -97.6699
    assert not route_matches_position(aus_lat, aus_lon, dal, lax, 4000)
    assert route_matches_position(32.82, -96.86, dal, lax, 4000)
    assert yard_direction_label(0, 0) == "FRONT"
    assert yard_direction_label(180, 0) == "BACK"
    assert yard_direction_label(90, 0) == "RIGHT"
    assert yard_direction_label(270, 0) == "LEFT"
    assert distance_gradient_color(0.0, (0, 100, 0), (100, 100, 0), (100, 0, 0)) == (0, 100, 0)
    assert distance_gradient_color(0.5, (0, 100, 0), (100, 100, 0), (100, 0, 0)) == (100, 100, 0)
    assert distance_gradient_color(1.0, (0, 100, 0), (100, 100, 0), (100, 0, 0)) == (100, 0, 0)
    print("selftest OK")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--windowed", action="store_true")
    ap.add_argument("--width", type=int)
    ap.add_argument("--height", type=int)
    ap.add_argument("--frames", type=int, default=0, help="headless: run N frames then exit")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        selftest()
        return

    cfg = load_config(args.config)
    if cfg.get("config", {}).get("write_on_start", True):
        save_config(cfg, args.config)
    if args.demo:
        cfg["feed"]["demo"] = True
    if args.windowed:
        cfg["display"]["fullscreen"] = False
    if args.width:
        cfg["display"]["width"] = args.width
    if args.height:
        cfg["display"]["height"] = args.height

    pygame.init()
    pygame.font.init()

    w, h = cfg["display"]["width"], cfg["display"]["height"]
    flags = pygame.FULLSCREEN | pygame.SCALED if cfg["display"]["fullscreen"] else 0
    try:
        screen = pygame.display.set_mode((w, h), flags)
    except pygame.error:
        screen = pygame.display.set_mode((w, h))
    w, h = screen.get_size()
    pygame.display.set_caption("Ceiling Radar")
    pygame.mouse.set_visible(False)
    clock = pygame.time.Clock()

    view = ViewState(cfg, w, h)
    renderer = Renderer(screen, cfg)
    calib = Calibrator()
    enricher = Enricher(cfg)
    poller = Poller(cfg, enricher)
    poller.start()

    def autosave_config():
        if cfg.get("config", {}).get("autosave", True):
            save_config(cfg, args.config)

    fleet = {}
    frame = 0
    running = True
    while running:
        now = time.time()
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                if event.key in (pygame.K_q, pygame.K_ESCAPE):
                    running = False
                elif event.key == pygame.K_c:
                    calib.active = not calib.active
                elif event.key == pygame.K_s:
                    save_config(cfg, args.config)
                elif event.key == pygame.K_d:
                    poller.set_demo(not poller.demo)
                    cfg["feed"]["demo"] = poller.demo
                    autosave_config()
                elif event.key == pygame.K_l:
                    lo = cfg.setdefault("lookout", {})
                    lo["enabled"] = not bool(lo.get("enabled", True))
                    renderer.apply_style(cfg)
                    autosave_config()
                elif event.key == pygame.K_f:
                    cfg["display"]["fullscreen"] = not cfg["display"]["fullscreen"]
                    fl = pygame.FULLSCREEN | pygame.SCALED if cfg["display"]["fullscreen"] else 0
                    screen = pygame.display.set_mode((w, h), fl)
                    renderer.screen = screen
                    autosave_config()
                elif calib.active:
                    if calib.handle(event, cfg, view):
                        view.rebuild(cfg, w, h)
                        renderer.apply_lookout(cfg)
                        autosave_config()

        # ingest fixes
        fixes, status = poller.snapshot()
        seen = set()
        for fx in fixes:
            seen.add(fx["hex"])
            if fx["hex"] in fleet:
                fleet[fx["hex"]].update(fx)
            else:
                fleet[fx["hex"]] = Aircraft(fx)
        # drop stale
        for hx in list(fleet):
            if now - fleet[hx].t_fix > 60:
                del fleet[hx]

        screen.fill((0, 0, 0))
        for ac in fleet.values():
            ac.step(now)
            if not view.in_range(ac.disp_lat, ac.disp_lon):
                continue
            renderer.draw_trail(view, ac, now)
        for ac in fleet.values():
            if view.in_range(ac.disp_lat, ac.disp_lon):
                renderer.draw_plane(view, ac)

        renderer.draw_demo_banner(status)

        if calib.active:
            calib.draw(screen, cfg, view, status)

        pygame.display.flip()
        clock.tick(cfg["display"]["fps"])

        frame += 1
        if args.frames and frame >= args.frames:
            running = False

    poller.running = False
    enricher.flush()
    pygame.quit()


if __name__ == "__main__":
    main()
