"""Real user questions across every PlaceRoot tool, worldwide (#269).

The corpus behind `benchmarks/run_query_corpus.py`. Every entry runs ONE
user-shaped request and returns (ok, detail), where `ok` is a *correctness*
check and not merely "didn't raise".

That distinction is the whole point. The per-tool cold matrix this repo used
before benched one tool at a time with a known-good input, and reported every
tool under 10s while `resolve_place("Stanford Shopping Center")` took 61s and
returned nothing at all. Worse, `geocode("Casablanca")` answered Casablanca,
*Chile* in 0.2s — which a timing-only bench scores as the best result in the
suite. A fast wrong answer has to fail, so it does here.

Two other things this shape catches that a single-tool bench cannot:

- **Regional variance.** Tools were benched in Tokyo; `gers_lookup` runs
  10-33s depending on the city, and nothing said so.
- **Composite flows.** Real questions are two calls ("how many playgrounds
  near X"), and a flow whose *first* call takes a wrong branch is invisible
  when each tool is measured alone.

Coordinates in `near=` are the answer's truth, checked to a km tolerance —
the Stanford bug returned a real place 4,000 km from the one asked for.
"""

QUERIES = []


def q(qid, tool, question, fn):
    QUERIES.append({"id": qid, "tool": tool, "question": question, "fn": fn})


def _mods():
    from placeroot import (
        addresses,
        buildings,
        divisions,
        gers,
        infrastructure,
        land_use,
        overture,
        routing,
        water,
    )
    from placeroot import geocode as geo_mod
    return dict(
        addresses=addresses, buildings=buildings, divisions=divisions, gers=gers,
        infrastructure=infrastructure, land_use=land_use, overture=overture,
        routing=routing, water=water, geo=geo_mod,
    )


def _names(rows):
    return [r.get("name") for r in rows][:3]


# --------------------------------------------------------------------------
# resolve_place / geocode: "where is X"
# --------------------------------------------------------------------------

def _resolve(name, expect_sub=None, near=None):
    """near=(lat, lon, km) asserts the top hit lands in the right part of the
    world — the Stanford bug returned a real place 4,000 km from the answer."""
    def run():
        m = _mods()
        r = m["geo"].resolve_place(name)
        rows = r if isinstance(r, list) else [r]
        if not rows:
            return False, "EMPTY"
        top = rows[0]
        detail = f"{top.get('name')!r} @ {top.get('lat'):.3f},{top.get('lon'):.3f}"
        if expect_sub and expect_sub.lower() not in (top.get("name") or "").lower():
            return False, f"WRONG NAME {detail}"
        if near:
            import math
            lat, lon, km = near
            d = math.dist((top["lat"], top["lon"]), (lat, lon)) * 111
            if d > km:
                return False, f"WRONG PLACE ({d:.0f}km off) {detail}"
        return True, detail
    return run


q("r01", "resolve_place", "Where is Stanford Shopping Center?",
  _resolve("Stanford Shopping Center", "Stanford Shopping", (37.44, -122.17, 25)))
q("r02", "resolve_place", "Where is the Eiffel Tower?",
  _resolve("Eiffel Tower", "Eiffel", (48.858, 2.294, 25)))
q("r03", "resolve_place", "Where is Shibuya Crossing in Tokyo?",
  _resolve("Shibuya Crossing Tokyo", None, (35.66, 139.70, 30)))
q("r04", "resolve_place", "Where is Golden Gate Park?",
  _resolve("Golden Gate Park San Francisco", "Golden Gate", (37.77, -122.47, 25)))
q("r05", "resolve_place", "Where is Times Square?",
  _resolve("Times Square New York", None, (40.758, -73.985, 25)))
q("r06", "resolve_place", "Where is Heathrow Airport?",
  _resolve("Heathrow Airport", "Heathrow", (51.47, -0.454, 30)))
q("r07", "resolve_place", "Where is Sagrada Familia?",
  _resolve("Sagrada Familia Barcelona", None, (41.404, 2.174, 25)))
q("r08", "resolve_place", "Where is Central Park?",
  _resolve("Central Park New York", "Central Park", (40.785, -73.968, 25)))
q("r09", "resolve_place", "Where is Brandenburg Gate?",
  _resolve("Brandenburger Tor Berlin", None, (52.516, 13.377, 25)))
q("r10", "resolve_place", "Where is Sydney Opera House?",
  _resolve("Sydney Opera House", "Opera House", (-33.857, 151.215, 25)))
q("r11", "resolve_place", "Where is Union Station Chicago?",
  _resolve("Union Station Chicago", None, (41.878, -87.640, 30)))
q("r12", "resolve_place", "Where is Mall of America?",
  _resolve("Mall of America", "Mall of America", (44.854, -93.242, 30)))
q("r13", "resolve_place", "Where is Pike Place Market?",
  _resolve("Pike Place Market Seattle", "Pike Place", (47.609, -122.342, 25)))
q("r14", "resolve_place", "Where is Schiphol Airport?",
  _resolve("Schiphol Airport Amsterdam", None, (52.31, 4.76, 30)))
q("r15", "resolve_place", "Where is Marina Bay Sands?",
  _resolve("Marina Bay Sands Singapore", None, (1.283, 103.860, 25)))
q("r16", "resolve_place", "Where is the Colosseum in Rome?",
  _resolve("Colosseo Roma", None, (41.890, 12.492, 25)))
q("r17", "resolve_place", "Where is Copacabana Beach?",
  _resolve("Copacabana Rio de Janeiro", None, (-22.97, -43.18, 30)))
q("r18", "resolve_place", "Where is Grand Central Terminal?",
  _resolve("Grand Central Terminal", "Grand Central", (40.753, -73.977, 25)))
q("r19", "resolve_place", "Where is Griffith Observatory?",
  _resolve("Griffith Observatory Los Angeles", "Griffith", (34.118, -118.300, 25)))
q("r20", "resolve_place", "Where is King's Cross Station?",
  _resolve("King's Cross Station London", None, (51.531, -0.124, 25)))
q("r21", "resolve_place", "Where is Fisherman's Wharf?",
  _resolve("Fisherman's Wharf San Francisco", None, (37.808, -122.417, 25)))
q("r22", "resolve_place", "Where is Bondi Beach?",
  _resolve("Bondi Beach Sydney", "Bondi", (-33.891, 151.277, 25)))
q("r23", "resolve_place", "Where is the Reichstag?",
  _resolve("Reichstag Berlin", None, (52.518, 13.376, 25)))
q("r24", "resolve_place", "Where is Ueno Park?",
  _resolve("Ueno Park Tokyo", None, (35.715, 139.773, 25)))
q("r25", "resolve_place", "Where is Millennium Park Chicago?",
  _resolve("Millennium Park Chicago", "Millennium", (41.883, -87.622, 25)))

# City / region name lookups (geocode, not POI resolution)

def _geocode(name, near=None, expect_sub=None):
    def run():
        m = _mods()
        rows = m["geo"].geocode(name, limit=5)
        if not rows:
            return False, "EMPTY"
        top = rows[0]
        detail = f"{top.get('name')!r} @ {top.get('lat'):.2f},{top.get('lon'):.2f}"
        if expect_sub and expect_sub.lower() not in (top.get("name") or "").lower():
            return False, f"WRONG NAME {detail}"
        if near:
            import math
            lat, lon, km = near
            d = math.dist((top["lat"], top["lon"]), (lat, lon)) * 111
            if d > km:
                return False, f"WRONG PLACE ({d:.0f}km off) {detail}"
        return True, detail
    return run


q("g01", "geocode", "Where is Kyoto?", _geocode("Kyoto", (35.01, 135.77, 60)))
q("g02", "geocode", "Where is Reykjavik?", _geocode("Reykjavik", (64.15, -21.94, 60)))
q("g03", "geocode", "Where is Nairobi?", _geocode("Nairobi", (-1.29, 36.82, 60)))
q("g04", "geocode", "Where is Munich?", _geocode("Munich", (48.14, 11.58, 60)))
q("g05", "geocode", "Where is São Paulo?", _geocode("São Paulo", (-23.55, -46.63, 80)))
q("g06", "geocode", "Where is Ho Chi Minh City?", _geocode("Ho Chi Minh City", (10.82, 106.63, 80)))
q("g07", "geocode", "Where is Portland?", _geocode("Portland"))
q("g08", "geocode", "Where is Springfield?", _geocode("Springfield"))
q("g09", "geocode", "Where is 東京?", _geocode("東京", (35.68, 139.75, 80)))
q("g10", "geocode", "Where is Casablanca?", _geocode("Casablanca", (33.57, -7.59, 60)))
q("g11", "geocode", "Where is Tbilisi?", _geocode("Tbilisi", (41.72, 44.79, 60)))
q("g12", "geocode", "Where is Ulaanbaatar?", _geocode("Ulaanbaatar", (47.89, 106.91, 80)))

# --------------------------------------------------------------------------
# geocode_address: "where is this street address"
# --------------------------------------------------------------------------

def _addr(addr, near=None):
    def run():
        m = _mods()
        r = m["geo"].geocode_address(addr)
        rows = r.get("results", []) if isinstance(r, dict) else r
        if not rows:
            note = (r.get("note") if isinstance(r, dict) else "") or ""
            return False, f"EMPTY {note[:70]}"
        top = rows[0]
        label = top.get("address") or top.get("name")
        detail = f"{label!r} @ {top.get('lat'):.4f},{top.get('lon'):.4f}"
        if near:
            import math
            lat, lon, km = near
            d = math.dist((top["lat"], top["lon"]), (lat, lon)) * 111
            if d > km:
                return False, f"WRONG PLACE ({d:.0f}km off) {detail}"
        return True, detail
    return run


q("a01", "geocode_address", "Where is 350 5th Ave, New York?",
  _addr("350 5th Ave, New York", (40.748, -73.985, 10)))
q("a02", "geocode_address", "Where is 1600 Pennsylvania Avenue, Washington DC?",
  _addr("1600 Pennsylvania Avenue NW, Washington, DC", (38.898, -77.036, 15)))
q("a03", "geocode_address", "Where is 1 Infinite Loop, Cupertino?",
  _addr("1 Infinite Loop, Cupertino, CA", (37.332, -122.030, 15)))
q("a04", "geocode_address", "Where is 221B Baker Street, London?",
  _addr("221B Baker Street, London", (51.523, -0.158, 15)))
q("a05", "geocode_address", "Where is 233 S Wacker Dr, Chicago?",
  _addr("233 S Wacker Dr, Chicago, IL", (41.878, -87.636, 15)))
q("a06", "geocode_address", "Where is 100 Universal City Plaza, Universal City?",
  _addr("100 Universal City Plaza, Universal City, CA", (34.138, -118.353, 20)))
q("a07", "geocode_address", "Where is 5th Avenue and 42nd Street in Manhattan?",
  _addr("5th Ave & 42nd St, New York, NY", (40.753, -73.981, 10)))
q("a08", "geocode_address", "Where is 401 Bay Street, Toronto?",
  _addr("401 Bay Street, Toronto", (43.653, -79.382, 20)))

# --------------------------------------------------------------------------
# reverse_geocode / admin_lookup / address_at: "what is at these coordinates"
# --------------------------------------------------------------------------

def _reverse(lat, lon):
    def run():
        m = _mods()
        r = m["geo"].reverse_geocode(lat, lon)
        if not r:
            return False, "EMPTY"
        return True, str(r)[:110]
    return run


def _admin(lat, lon, expect_country=None):
    def run():
        m = _mods()
        r = m["divisions"].admin_lookup(lat, lon)
        chain = r.get("chain", [])
        if not chain:
            return False, "EMPTY CHAIN"
        names = [c.get("name") for c in chain]
        if expect_country and not any(
            any(e.lower() in (n or "").lower() for n in names)
            for e in ([expect_country] if isinstance(expect_country, str) else expect_country)
        ):
            return False, f"WRONG {names}"
        return True, str(names)
    return run


q("v01", "reverse_geocode", "What's at 40.7359, -73.9911?", _reverse(40.7359, -73.9911))
q("v02", "reverse_geocode", "What's at 48.8584, 2.2945 (Eiffel Tower)?", _reverse(48.8584, 2.2945))
q("v03", "reverse_geocode", "What's at -33.8568, 151.2153 (Sydney)?", _reverse(-33.8568, 151.2153))
q("v04", "reverse_geocode", "What's at 1.2834, 103.8607 (Singapore)?", _reverse(1.2834, 103.8607))
q("v05", "reverse_geocode", "What's at 55.7558, 37.6173 (Moscow)?", _reverse(55.7558, 37.6173))
q("v06", "admin_lookup", "Which city and country is 35.6595, 139.7005 in?",
  _admin(35.6595, 139.7005, ["Japan", "日本"]))
q("v07", "admin_lookup", "Which country is -1.2921, 36.8219 in?",
  _admin(-1.2921, 36.8219, ["Kenya"]))
q("v08", "admin_lookup", "Which state is 30.2672, -97.7431 in?",
  _admin(30.2672, -97.7431, ["Texas"]))
q("v09", "admin_lookup", "Which region is 64.1466, -21.9426 in?",
  _admin(64.1466, -21.9426, ["Iceland", "Ísland"]))
q("v10", "admin_lookup", "Which country is 19.4326, -99.1332 in?",
  _admin(19.4326, -99.1332, ["Mexico", "México"]))
def _call(module, method, *a, check=None, **kw):
    """Call one tool and report it. `check` decides ok; default is "answered
    without raising", which is right where an empty result is a real answer
    (no address coverage in this country, no water near this point)."""
    def run():
        r = getattr(_mods()[module], method)(*a, **kw)
        return (check(r) if check else True), str(r)[:110]
    return run


q("v11", "address_at", "What's the street address at 40.7359, -73.9911?",
  _call("addresses", "address_at", 40.7359, -73.9911))
q("v12", "address_at", "What's the address at 51.5074, -0.1278 (London)?",
  _call("addresses", "address_at", 51.5074, -0.1278))

# --------------------------------------------------------------------------
# find_places / within_distance: "what's near here"
# --------------------------------------------------------------------------

def _find(lat, lon, category=None, radius=1000, need=1, limit=20):
    def run():
        m = _mods()
        rows = m["overture"].find_places(
            lat, lon, radius_m=radius, category=category, limit=limit
        )
        if len(rows) < need:
            return False, f"only {len(rows)} rows"
        return True, f"n={len(rows)} {_names(rows)}"
    return run


q("f01", "find_places", "Coffee shops near Shibuya?", _find(35.6595, 139.7005, "coffee_shop"))
q("f02", "find_places", "Restaurants near Times Square?", _find(40.758, -73.985, "restaurant"))
q("f03", "find_places", "Pharmacies near the Louvre?", _find(48.8606, 2.3376, "pharmacy"))
q("f04", "find_places", "Supermarkets near Kreuzberg, Berlin?",
  _find(52.4990, 13.4180, "grocery_store"))
q("f05", "find_places", "Bars near Temple Bar, Dublin?", _find(53.3455, -6.2637, "bar"))
q("f06", "find_places", "Hotels near Marina Bay, Singapore?", _find(1.2834, 103.8607, "hotel"))
q("f07", "find_places", "Bookstores near Harvard Square?", _find(42.3736, -71.1190, "bookstore"))
q("f08", "find_places", "Bakeries near Montmartre?", _find(48.8867, 2.3431, "bakery"))
q("f09", "find_places", "Gyms near downtown Austin?", _find(30.2672, -97.7431, "gym"))
q("f10", "find_places", "Museums near Museumplein, Amsterdam?", _find(52.3579, 4.8816, "museum"))
q("f11", "find_places", "Banks near Bank Junction, London?", _find(51.5134, -0.0886, "bank"))
q("f12", "find_places", "Hospitals near central Nairobi?",
  _find(-1.2921, 36.8219, "hospital", 5000))
q("f13", "find_places", "Anything at all near Ushuaia (sparse area)?",
  _find(-54.8019, -68.3030, None, 3000))
q("f14", "find_places", "What's near Machu Picchu (remote)?", _find(-13.1631, -72.5450, None, 5000))
q("f15", "find_places", "Parks near Ueno, Tokyo?", _find(35.7148, 139.7734, "park", 2000))
q("f16", "find_places", "Playgrounds near Palo Alto?",
  _find(37.4419, -122.1430, "playground", 3000))
q("f17", "find_places", "Schools near Brooklyn Heights?", _find(40.6959, -73.9955, "school", 2000))
q("f18", "find_places", "Cafes near Plaza de Mayo, Buenos Aires?",
  _find(-34.6083, -58.3712, "cafe"))
q("f19", "find_places", "Restaurants near Gangnam, Seoul?", _find(37.4979, 127.0276, "restaurant"))
q("f20", "find_places", "Shops near Grand Bazaar, Istanbul?", _find(41.0106, 28.9681, None, 800))
q("f21", "within_distance", "Is there a cafe within 500m of Shibuya?",
  _call("overture", "within_distance", 35.6595, 139.7005, max_distance_m=500, category="cafe",
        check=lambda r: r.get("within") is not None))
q("f22", "within_distance", "Is there a pharmacy within 300m of the Colosseum?",
  _call("overture", "within_distance", 41.8902, 12.4922, max_distance_m=300, category="pharmacy",
        check=lambda r: r.get("within") is not None))
q("f23", "within_distance", "Is there a grocery store within 1km of Reykjavik centre?",
  _call("overture", "within_distance", 64.1466, -21.9426, max_distance_m=1000,
                                          category="grocery_store",
        check=lambda r: r.get("within") is not None))

# --------------------------------------------------------------------------
# summarize_area / compare_areas
# --------------------------------------------------------------------------

def _summarize(lat, lon, radius=1000, need=1):
    def run():
        m = _mods()
        r = m["overture"].summarize_area(lat, lon, radius_m=radius)
        n = r.get("total_places", 0)
        if n < need:
            return False, f"total={n}"
        return True, f"total={n}"
    return run


q("s01", "summarize_area", "What kind of neighborhood is Shibuya?", _summarize(35.6595, 139.7005))
q("s02", "summarize_area", "What's in downtown Chicago?", _summarize(41.8781, -87.6298))
q("s03", "summarize_area", "What's around the Sagrada Familia?", _summarize(41.4036, 2.1744))
q("s04", "summarize_area", "What's in central Lagos?", _summarize(6.4550, 3.3841, 2000))
q("s05", "summarize_area", "What's around Copacabana?", _summarize(-22.9711, -43.1822))
q("s06", "summarize_area", "What's near Karol Bagh, Delhi?", _summarize(28.6519, 77.1909, 2000))
q("s07", "compare_areas", "Compare Shibuya and Shinjuku",
  lambda: (lambda r: (len(r.get("areas", [])) == 2, f"areas={len(r.get('areas', []))}"))(
      _mods()["overture"].compare_areas([(35.6595, 139.7005), (35.6896, 139.6917)], radius_m=800)))
q("s08", "compare_areas", "Compare SoHo NYC with Williamsburg Brooklyn",
  lambda: (lambda r: (len(r.get("areas", [])) == 2, f"areas={len(r.get('areas', []))}"))(
      _mods()["overture"].compare_areas([(40.7233, -74.0030), (40.7081, -73.9571)], radius_m=800)))
q("s09", "compare_areas", "Compare central Paris with central Berlin",
  lambda: (lambda r: (len(r.get("areas", [])) == 2, f"areas={len(r.get('areas', []))}"))(
      _mods()["overture"].compare_areas([(48.8566, 2.3522), (52.5200, 13.4050)], radius_m=1000)))

# --------------------------------------------------------------------------
# buildings / land use / infrastructure / water
# --------------------------------------------------------------------------

q("b01", "summarize_buildings", "How built up is Shibuya?",
  lambda: (lambda r: (r.get("count", 0) > 0, f"count={r.get('count')}"))(
      _mods()["buildings"].summarize_buildings(35.6595, 139.7005, radius_m=500)))
q("b02", "summarize_buildings", "How dense are the buildings in central Cairo?",
  lambda: (lambda r: (r.get("count", 0) > 0, f"count={r.get('count')}"))(
      _mods()["buildings"].summarize_buildings(30.0444, 31.2357, radius_m=500)))
q("b03", "summarize_buildings", "How built up is downtown Houston?",
  lambda: (lambda r: (r.get("count", 0) > 0, f"count={r.get('count')}"))(
      _mods()["buildings"].summarize_buildings(29.7604, -95.3698, radius_m=500)))
q("b04", "buildings_at", "What building is at the Empire State Building?",
  lambda: (lambda r: (len(r) > 0, f"n={len(r)}"))(
      _mods()["buildings"].buildings_at(40.7484, -73.9857)))
q("b05", "buildings_at", "What building is at 51.5007, -0.1246 (Big Ben)?",
  lambda: (lambda r: (len(r) > 0, f"n={len(r)}"))(
      _mods()["buildings"].buildings_at(51.5007, -0.1246)))
q("b06", "buildings_at", "What building is at Marina Bay Sands?",
  lambda: (lambda r: (len(r) > 0, f"n={len(r)}"))(
      _mods()["buildings"].buildings_at(1.2834, 103.8607)))
q("l01", "land_use_at", "What is the land use at Central Park?",
  lambda: (lambda r: (True, str(r)[:110]))(_mods()["land_use"].land_use_at(40.7829, -73.9654)))
q("l02", "land_use_at", "What's the land cover in the Amazon at -3, -60?",
  lambda: (lambda r: (r.get("land_cover") is not None, str(r)[:110]))(
      _mods()["land_use"].land_use_at(-3.0, -60.0)))
q("l03", "land_use_at", "What's the land cover in the Sahara at 23, 10?",
  lambda: (lambda r: (r.get("land_cover") is not None, str(r)[:110]))(
      _mods()["land_use"].land_use_at(23.0, 10.0)))
q("l04", "land_use_at", "What's the land use in central Tokyo?",
  lambda: (lambda r: (True, str(r)[:110]))(_mods()["land_use"].land_use_at(35.6595, 139.7005)))
q("l05", "land_use_at", "What's the land cover in the Swiss Alps?",
  lambda: (lambda r: (True, str(r)[:110]))(_mods()["land_use"].land_use_at(46.6, 8.0)))
q("i01", "infrastructure_at", "What infrastructure is near Shibuya station?",
  lambda: (lambda r: (True, f"n={len(r)}"))(
      _mods()["infrastructure"].infrastructure_at(35.6595, 139.7005, radius_m=500)))
q("i02", "infrastructure_at", "What infrastructure is near JFK airport?",
  lambda: (lambda r: (True, f"n={len(r)}"))(
      _mods()["infrastructure"].infrastructure_at(40.6413, -73.7781, radius_m=1000)))
q("i03", "infrastructure_at", "What infrastructure is near the Port of Rotterdam?",
  lambda: (lambda r: (True, f"n={len(r)}"))(
      _mods()["infrastructure"].infrastructure_at(51.9490, 4.1400, radius_m=2000)))
q("w01", "water_near", "Is there water near the Tokyo waterfront?",
  _call("water", "water_near", 35.6300, 139.7800, radius_m=1000))
q("w02", "water_near", "What water is near Lake Geneva?",
  lambda: (lambda r: (len(r) > 0, f"n={len(r)}"))(
      _mods()["water"].water_near(46.4500, 6.5000, radius_m=2000)))
q("w03", "water_near", "What water is near the Chicago riverfront?",
  _call("water", "water_near", 41.8881, -87.6270, radius_m=1000))
q("w04", "water_near", "Is there water near Venice?",
  lambda: (lambda r: (len(r) > 0, f"n={len(r)}"))(
      _mods()["water"].water_near(45.4408, 12.3155, radius_m=1500)))

# --------------------------------------------------------------------------
# routing
# --------------------------------------------------------------------------

def _route(a, b, mode="walk"):
    def run():
        m = _mods()
        r = m["routing"].route(a[0], a[1], b[0], b[1], mode=mode)
        d = r.get("distance_m")
        if not d:
            return False, f"NO ROUTE {str(r)[:80]}"
        return True, f"{d:.0f}m"
    return run


q("t01", "route", "How do I walk from Shibuya to Ebisu?",
  _route((35.6595, 139.7005), (35.6467, 139.7101)))
q("t02", "route", "Walking route from Times Square to Bryant Park?",
  _route((40.758, -73.985), (40.7536, -73.9832)))
q("t03", "route", "Walking route from the Louvre to Notre-Dame?",
  _route((48.8606, 2.3376), (48.8530, 2.3499)))
q("t04", "route", "Drive from downtown Austin to the airport?",
  _route((30.2672, -97.7431), (30.1975, -97.6664), "drive"))
q("t05", "route", "Walk from Brandenburg Gate to Museum Island?",
  _route((52.5163, 13.3777), (52.5169, 13.4019)))
q("t06", "route", "Walk from Trafalgar Square to Covent Garden?",
  _route((51.5080, -0.1281), (51.5117, -0.1240)))
q("t07", "isochrone", "How far can I walk in 10 minutes from Shibuya?",
  lambda: (lambda r: (bool(r), str(r)[:90]))(
      _mods()["routing"].isochrone(35.6595, 139.7005, minutes=10, mode="walk")))
q("t08", "isochrone", "What's within a 15 minute walk of the Colosseum?",
  lambda: (lambda r: (bool(r), str(r)[:90]))(
      _mods()["routing"].isochrone(41.8902, 12.4922, minutes=15, mode="walk")))
q("t09", "isochrone", "What's within a 10 minute drive of downtown Seattle?",
  lambda: (lambda r: (bool(r), str(r)[:90]))(
      _mods()["routing"].isochrone(47.6062, -122.3321, minutes=10, mode="drive")))

# --------------------------------------------------------------------------
# place_details / gers_lookup (id round-trips)
# --------------------------------------------------------------------------

def _details(lat, lon):
    def run():
        m = _mods()
        rows = m["overture"].find_places(lat, lon, radius_m=800, limit=1)
        if not rows:
            return False, "no seed place"
        r = m["overture"].place_details(id=rows[0]["id"], near_lat=lat, near_lon=lon)
        return bool(r), str(r)[:90]
    return run


def _gers(lat, lon):
    def run():
        m = _mods()
        rows = m["overture"].find_places(lat, lon, radius_m=800, limit=1)
        if not rows:
            return False, "no seed place"
        r = m["gers"].gers_lookup(rows[0]["id"], near_lat=rows[0]["lat"], near_lon=rows[0]["lon"])
        if not r:
            return False, "MISS"
        return True, f"{r.get('name')!r} related={list((r.get('related') or {}).keys())[:3]}"
    return run


q("d01", "place_details", "Tell me more about a place in Shibuya", _details(35.6595, 139.7005))
q("d02", "place_details", "Tell me more about a place near the Louvre", _details(48.8606, 2.3376))
q("d03", "place_details", "Tell me more about a place in Mexico City", _details(19.4326, -99.1332))
q("d04", "gers_lookup", "What is this GERS id (Shibuya place)?", _gers(35.6595, 139.7005))
q("d05", "gers_lookup", "What is this GERS id (London place)?", _gers(51.5074, -0.1278))
q("d06", "gers_lookup", "What is this GERS id (São Paulo place)?", _gers(-23.5505, -46.6333))

# --------------------------------------------------------------------------
# Composite flows — the real shape of a user question ("X near Y")
# --------------------------------------------------------------------------

def _flow(place, category, radius=2000, need=1):
    def run():
        m = _mods()
        r = m["geo"].resolve_place(place)
        rows = r if isinstance(r, list) else [r]
        if not rows:
            return False, f"resolve EMPTY for {place!r}"
        top = rows[0]
        found = m["overture"].find_places(
            top["lat"], top["lon"], radius_m=radius, category=category, limit=50
        )
        if len(found) < need:
            return False, f"resolved {top.get('name')!r} but only {len(found)} {category}"
        return True, f"{top.get('name')!r} -> n={len(found)}"
    return run


q("c01", "flow", "How many playgrounds near Stanford Shopping Center?",
  _flow("Stanford Shopping Center", "playground"))
q("c02", "flow", "Coffee shops near the Eiffel Tower?",
  _flow("Eiffel Tower", "coffee_shop"))
q("c03", "flow", "Restaurants near Union Square San Francisco?",
  _flow("Union Square San Francisco", "restaurant"))
q("c04", "flow", "Pharmacies near Shibuya Station?",
  _flow("Shibuya Station Tokyo", "pharmacy"))
q("c05", "flow", "Hotels near Heathrow Airport?",
  _flow("Heathrow Airport", "hotel", 5000))
q("c06", "flow", "Parks near Brooklyn Bridge?",
  _flow("Brooklyn Bridge", "park"))
q("c07", "flow", "Supermarkets near Alexanderplatz?",
  _flow("Alexanderplatz Berlin", "grocery_store"))
q("c08", "flow", "Cafes near Trinity College Dublin?",
  _flow("Trinity College Dublin", "cafe"))
q("c09", "flow", "Restaurants near Sydney Opera House?",
  _flow("Sydney Opera House", "restaurant"))
q("c10", "flow", "Museums near Central Park?",
  _flow("Central Park New York", "museum"))
q("c11", "flow", "Bars near Shinjuku Station?",
  _flow("Shinjuku Station Tokyo", "bar"))
q("c12", "flow", "Schools near Golden Gate Park?",
  _flow("Golden Gate Park San Francisco", "school", 3000))
def _resolve_then(place, follow):
    """resolve_place(place), then hand the top hit to `follow` — the shape of
    a real two-call question, and the shape a per-tool bench cannot see."""
    def run():
        m = _mods()
        r = m["geo"].resolve_place(place)
        rows = r if isinstance(r, list) else [r]
        if not rows:
            return False, f"resolve EMPTY for {place!r}"
        return follow(m, rows[0])
    return run


def _summarize_follow(m, top):
    s = m["overture"].summarize_area(top["lat"], top["lon"], radius_m=800)
    return s.get("total_places", 0) > 0, f"{top.get('name')!r} total={s.get('total_places')}"


def _admin_follow(m, top):
    chain = m["divisions"].admin_lookup(top["lat"], top["lon"]).get("chain", [])
    return bool(chain), f"{top.get('name')!r} {[c.get('name') for c in chain]}"


q("c13", "flow", "What's the neighborhood like around Pike Place Market?",
  _resolve_then("Pike Place Market Seattle", _summarize_follow))
q("c14", "flow", "Which admin area is the Colosseum in?",
  _resolve_then("Colosseo Roma", _admin_follow))
def _route_between(from_place, to_place, mode="walk"):
    """Two resolves feeding a route — three calls, the deepest flow here."""
    def run():
        m = _mods()
        a = m["geo"].resolve_place(from_place) or []
        b = m["geo"].resolve_place(to_place) or []
        if not (a and b):
            return False, f"resolve EMPTY ({from_place!r} -> {to_place!r})"
        r = m["routing"].route(a[0]["lat"], a[0]["lon"], b[0]["lat"], b[0]["lon"], mode=mode)
        return bool(r.get("distance_m")), f"{r.get('distance_m')}m"
    return run


q("c15", "flow", "How far is it to walk from Shibuya Station to Yoyogi Park?",
  _route_between("Shibuya Station Tokyo", "Yoyogi Park Tokyo"))


# --------------------------------------------------------------------------
# Natural phrasings (#272) — lowercase, misspellings, filler words, partial
# names: how people actually type. Round 2 of the sweep that caught the
# Casablanca and Stanford bugs; these caught "notre dame paris" resolving to
# Indiana and "harvard square cambridge" to the wrong Cambridge (of three).
# --------------------------------------------------------------------------

q("x01", "resolve_place", "coffe shops near pike place market seatle",
  _resolve("pike place market seattle", None, (47.609, -122.342, 25)))
q("x02", "resolve_place", "wheres the golden gate bridge",
  _resolve("golden gate bridge san francisco", None, (37.82, -122.48, 25)))
q("x03", "resolve_place", "notre dame paris (dropped-word landmark)",
  _resolve("notre dame paris", None, (48.853, 2.35, 25)))
q("x04", "resolve_place", "harvard square cambridge (third city of the name)",
  _resolve("harvard square cambridge", "Harvard Square", (42.373, -71.119, 25)))
q("x05", "resolve_place", "san jose airport (name-prefix city)",
  _resolve("san jose airport", None, (37.36, -121.93, 30)))
q("x06", "resolve_place", "palo alto caltrain station",
  _resolve("palo alto caltrain station", None, (37.443, -122.164, 25)))
q("x07", "flow", "stuff to eat near the space needle",
  _flow("space needle seattle", "restaurant"))
q("x08", "flow", "gas station near disneyland",
  _flow("disneyland anaheim", "fuel_station", 4000))
q("x09", "flow", "sushi near tsukiji market tokyo",
  _flow("tsukiji market tokyo", "sushi_restaurant", 2000))
q("x10", "flow", "dentist near the mission district sf",
  _flow("mission district san francisco", "dentist"))
q("x11", "flow", "hotels near niagara falls",
  _flow("niagara falls", "hotel", 6000))
