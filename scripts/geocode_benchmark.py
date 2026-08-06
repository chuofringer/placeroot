"""Honest accuracy benchmark for geocode() against LIVE Overture data (#10).

Runs ~100 real-world free-text place queries — cities, neighborhoods,
"city, state" forms — against the real Overture divisions/places themes (no
fixture, no mock) and reports hit@1 / hit@5: does the expected place appear
as the top result, or anywhere in the top 5?

This is opt-in and hits the network (S3), unlike the rest of the test suite
which runs entirely offline against committed fixtures. Run with:

    uv run python scripts/geocode_benchmark.py

Expected-answer matching is intentionally loose (case-insensitive substring
on name, plus an optional admin_context substring for disambiguation) since
geocode() returns Overture's canonical names, which don't always match a
query verbatim (e.g. "NYC" -> "New York City" isn't attempted here; queries
are close to Overture's own naming, which is itself part of what's being
measured — a mismatch is a real miss, not a scoring bug).
"""

import sys
import time

from placeroot import geocode, overture

# (query, expected substring in result name, expected substring in
# admin_context or None). Cities, neighborhoods, and "city, state"/"city,
# country" forms — the shapes a free-text geocoder actually sees.
QUERIES: list[tuple[str, str, str | None]] = [
    ("Seattle", "Seattle", "Washington"),
    ("Seattle, WA", "Seattle", "Washington"),
    ("New York", "New York", None),
    ("New York City", "New York", None),
    ("Brooklyn", "Brooklyn", "New York"),
    ("Brooklyn, NY", "Brooklyn", "New York"),
    ("Manhattan", "Manhattan", None),
    ("Queens", "Queens", None),
    ("Los Angeles", "Los Angeles", "California"),
    ("Los Angeles, CA", "Los Angeles", "California"),
    ("San Francisco", "San Francisco", "California"),
    ("San Francisco, CA", "San Francisco", "California"),
    ("Chicago", "Chicago", "Illinois"),
    ("Chicago, IL", "Chicago", "Illinois"),
    ("Houston", "Houston", "Texas"),
    ("Phoenix", "Phoenix", "Arizona"),
    ("Philadelphia", "Philadelphia", "Pennsylvania"),
    ("San Antonio", "San Antonio", "Texas"),
    ("San Diego", "San Diego", "California"),
    ("Dallas", "Dallas", "Texas"),
    ("Austin", "Austin", "Texas"),
    ("Austin, TX", "Austin", "Texas"),
    ("Jacksonville", "Jacksonville", "Florida"),
    ("Fort Worth", "Fort Worth", "Texas"),
    ("Columbus", "Columbus", "Ohio"),
    ("Charlotte", "Charlotte", "North Carolina"),
    ("San Jose", "San Jose", "California"),
    ("Indianapolis", "Indianapolis", "Indiana"),
    ("Denver", "Denver", "Colorado"),
    ("Denver, CO", "Denver", "Colorado"),
    ("Boston", "Boston", "Massachusetts"),
    ("Boston, MA", "Boston", "Massachusetts"),
    ("Nashville", "Nashville", "Tennessee"),
    ("Detroit", "Detroit", "Michigan"),
    ("Portland", "Portland", None),  # OR and ME both real; loose on purpose
    ("Portland, OR", "Portland", "Oregon"),
    ("Memphis", "Memphis", "Tennessee"),
    ("Oklahoma City", "Oklahoma City", "Oklahoma"),
    ("Las Vegas", "Las Vegas", "Nevada"),
    ("Las Vegas, NV", "Las Vegas", "Nevada"),
    ("Louisville", "Louisville", "Kentucky"),
    ("Baltimore", "Baltimore", "Maryland"),
    ("Milwaukee", "Milwaukee", "Wisconsin"),
    ("Albuquerque", "Albuquerque", "New Mexico"),
    ("Tucson", "Tucson", "Arizona"),
    ("Fresno", "Fresno", "California"),
    ("Sacramento", "Sacramento", "California"),
    ("Sacramento, CA", "Sacramento", "California"),
    ("Kansas City", "Kansas City", None),  # MO and KS both real
    ("Mesa", "Mesa", "Arizona"),
    ("Atlanta", "Atlanta", "Georgia"),
    ("Atlanta, GA", "Atlanta", "Georgia"),
    ("Omaha", "Omaha", "Nebraska"),
    ("Colorado Springs", "Colorado Springs", "Colorado"),
    ("Raleigh", "Raleigh", "North Carolina"),
    ("Miami", "Miami", "Florida"),
    ("Miami, FL", "Miami", "Florida"),
    ("Long Beach", "Long Beach", "California"),
    ("Virginia Beach", "Virginia Beach", "Virginia"),
    ("Oakland", "Oakland", "California"),
    ("Minneapolis", "Minneapolis", "Minnesota"),
    ("Tulsa", "Tulsa", "Oklahoma"),
    ("Tampa", "Tampa", "Florida"),
    ("Arlington", "Arlington", None),  # TX and VA both real
    ("New Orleans", "New Orleans", "Louisiana"),
    ("New Orleans, LA", "New Orleans", "Louisiana"),
    ("Wichita", "Wichita", "Kansas"),
    ("Cleveland", "Cleveland", "Ohio"),
    ("Bakersfield", "Bakersfield", "California"),
    ("Aurora", "Aurora", None),  # CO and IL both real
    ("Anaheim", "Anaheim", "California"),
    ("Honolulu", "Honolulu", "Hawaii"),
    ("Santa Ana", "Santa Ana", "California"),
    ("Riverside", "Riverside", "California"),
    ("Riverside, CA", "Riverside", "California"),
    ("Corpus Christi", "Corpus Christi", "Texas"),
    ("Lexington", "Lexington", "Kentucky"),
    ("Pittsburgh", "Pittsburgh", "Pennsylvania"),
    ("Anchorage", "Anchorage", "Alaska"),
    ("Stockton", "Stockton", "California"),
    ("Cincinnati", "Cincinnati", "Ohio"),
    ("St. Louis", "Louis", "Missouri"),
    ("Saint Paul", "Paul", "Minnesota"),
    ("Greensboro", "Greensboro", "North Carolina"),
    ("Newark", "Newark", None),  # NJ and CA both real
    ("Plano", "Plano", "Texas"),
    ("Henderson", "Henderson", "Nevada"),
    ("Lincoln", "Lincoln", "Nebraska"),
    ("Buffalo", "Buffalo", "New York"),
    ("Jersey City", "Jersey City", "New Jersey"),
    ("Chula Vista", "Chula Vista", "California"),
    ("Fort Wayne", "Fort Wayne", "Indiana"),
    ("Orlando", "Orlando", "Florida"),
    ("St. Petersburg", "Petersburg", "Florida"),
    ("Chandler", "Chandler", "Arizona"),
    ("Laredo", "Laredo", "Texas"),
    ("Norfolk", "Norfolk", "Virginia"),
    ("Durham", "Durham", "North Carolina"),
    ("Madison", "Madison", "Wisconsin"),
    ("Lubbock", "Lubbock", "Texas"),
    ("Irvine", "Irvine", "California"),
    ("Winston-Salem", "Winston", "North Carolina"),
    ("Glendale", "Glendale", None),  # AZ and CA both real
    ("Garland", "Garland", "Texas"),
    ("Hialeah", "Hialeah", "Florida"),
    ("Reno", "Reno", "Nevada"),
    ("Chesapeake", "Chesapeake", "Virginia"),
    ("Gilbert", "Gilbert", "Arizona"),
    ("Baton Rouge", "Baton Rouge", "Louisiana"),
    ("Irving", "Irving", "Texas"),
    ("Scottsdale", "Scottsdale", "Arizona"),
    ("North Las Vegas", "North Las Vegas", "Nevada"),
    ("Fremont", "Fremont", "California"),
]


def _matches(result: dict, expected_name: str, expected_admin: str | None) -> bool:
    if expected_name.lower() not in result["name"].lower():
        return False
    if expected_admin is None:
        return True
    return any(expected_admin.lower() in a.lower() for a in result.get("admin_context", []))


def run() -> None:
    overture.conn()  # warm the shared connection (progress bar already disabled there)
    hit_1 = hit_5 = 0
    misses = []
    t0 = time.time()
    for query, expected_name, expected_admin in QUERIES:
        results = geocode.geocode(query, limit=5)
        top1 = bool(results) and _matches(results[0], expected_name, expected_admin)
        top5 = any(_matches(r, expected_name, expected_admin) for r in results)
        hit_1 += top1
        hit_5 += top5
        if not top5:
            got = [r["name"] for r in results] or ["<no results>"]
            misses.append((query, expected_name, expected_admin, got))
    elapsed = time.time() - t0
    n = len(QUERIES)

    print(f"queries: {n}, elapsed: {elapsed:.1f}s")
    print(f"hit@1: {hit_1}/{n} ({100 * hit_1 / n:.1f}%)")
    print(f"hit@5: {hit_5}/{n} ({100 * hit_5 / n:.1f}%)")
    if misses:
        print("\nmisses (query -> expected [admin] vs got):")
        for query, expected_name, expected_admin, got in misses:
            admin_note = f" [{expected_admin}]" if expected_admin else ""
            print(f"  {query!r} -> {expected_name!r}{admin_note} vs {got}")


if __name__ == "__main__":
    if "-m" in sys.argv or "live" in sys.argv or "--live" in sys.argv:
        pass  # flags accepted but not required; this script always hits live S3
    run()
