#!/usr/bin/env bash
# Quarterly listings health check (#254's "Maintenance rule").
#
# Probes the listing surfaces #254 actually landed plus the install command
# they all point at. Exit 0 with nothing on stdout when everything holds;
# exit 1 with a markdown report on stdout otherwise — listings-check.yml
# turns that report into a GitHub issue.
#
# Network-dependent by design; not run from pytest.
#
# Usage:
#   bash scripts/check_listings.sh

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_VERSION=$(grep -m1 '^version = ' "$REPO_ROOT/pyproject.toml" | sed -E 's/version = "(.*)"/\1/')

# Same Cloudflare Pages project that serves placeroot.dev (custom domain).
# Used only when the custom domain returns a Bot Fight Mode challenge so we
# can tell "edge blocked curl" from "site is actually down".
# Overridable for local/self-tests (see tests/test_check_listings.py).
PAGES_ORIGIN_HOST="${CHECK_LISTINGS_PAGES_ORIGIN_HOST:-placeroot.pages.dev}"
# Scheme for the Pages-origin fallback (https in prod; http for local fixtures).
PAGES_ORIGIN_SCHEME="${CHECK_LISTINGS_PAGES_ORIGIN_SCHEME:-https}"
GLAMA_URL="${CHECK_LISTINGS_GLAMA_URL:-https://glama.ai/mcp/servers/chuofringer/placeroot}"
SITE_URL="${CHECK_LISTINGS_SITE_URL:-https://placeroot.dev}"
WHY_URL="${CHECK_LISTINGS_WHY_URL:-https://placeroot.dev/why-placeroot}"
# Host whose custom-domain 403s may fall back to PAGES_ORIGIN_HOST.
CUSTOM_DOMAIN_HOST="${CHECK_LISTINGS_CUSTOM_DOMAIN_HOST:-placeroot.dev}"

FAILURES=()

is_cloudflare_bot_challenge() {
    # is_cloudflare_bot_challenge <http_code> <headers_file> <body_file>
    # True only for Cloudflare's JS interstitial (Bot Fight / managed
    # challenge), not for a plain origin 403.
    local code="$1" headers_file="$2" body_file="$3"
    [ "$code" = "403" ] || return 1
    if grep -qiE '^cf-mitigated:[[:space:]]*challenge' "$headers_file"; then
        return 0
    fi
    # Header can be stripped by some proxies; the interstitial body is
    # distinctive enough on its own.
    if grep -q 'Just a moment...' "$body_file" \
        && grep -q 'cdn-cgi/challenge-platform' "$body_file"; then
        return 0
    fi
    return 1
}

pages_origin_url() {
    # pages_origin_url <url>
    # Rewrite https://$CUSTOM_DOMAIN_HOST/<path> -> https://$PAGES_ORIGIN_HOST/<path>.
    # Empty stdout for any other host — we never fall back for Glama/etc.
    local url="$1" rest=""
    if [[ "$url" == "https://$CUSTOM_DOMAIN_HOST" \
       || "$url" == "https://$CUSTOM_DOMAIN_HOST/"* \
       || "$url" == "http://$CUSTOM_DOMAIN_HOST" \
       || "$url" == "http://$CUSTOM_DOMAIN_HOST/"* ]]; then
        rest="${url#*://$CUSTOM_DOMAIN_HOST}"
        printf '%s://%s%s\n' "$PAGES_ORIGIN_SCHEME" "$PAGES_ORIGIN_HOST" "$rest"
    else
        printf '\n'
    fi
}

url_check() {
    # url_check <label> <url>
    local label="$1" url="$2"
    local body headers code origin origin_code origin_body

    body=$(mktemp)
    headers=$(mktemp)
    # curl writes the body even on non-2xx; -f would abort before we can
    # inspect a challenge response, so leave it off.
    # On connect failure curl prints 000 via -w and exits nonzero; keep that
    # single 000 rather than appending a second one.
    code=$(curl -sS -D "$headers" -o "$body" -w '%{http_code}' -L --max-time 30 "$url" || true)
    code=${code:-000}

    if [ "$code" = "200" ]; then
        echo "- OK: $label ($url) -> $code"
        rm -f "$body" "$headers"
        return
    fi

    if is_cloudflare_bot_challenge "$code" "$headers" "$body"; then
        origin=$(pages_origin_url "$url")
        if [ -n "$origin" ]; then
            origin_body=$(mktemp)
            origin_code=$(curl -sS -o "$origin_body" -w '%{http_code}' -L --max-time 30 "$origin" || true)
            origin_code=${origin_code:-000}
            # Require a PlaceRoot page from the Pages origin — a challenge on
            # both hosts, or a 200 for some other site, still fails.
            if [ "$origin_code" = "200" ] \
                && grep -qiE '<title>[^<]*PlaceRoot' "$origin_body" \
                && ! grep -q 'Just a moment...' "$origin_body"; then
                echo "- OK: $label ($url) -> Cloudflare bot challenge (HTTP $code); content verified via $origin -> $origin_code"
                rm -f "$body" "$headers" "$origin_body"
                return
            fi
            echo "- FAIL: $label ($url) -> Cloudflare bot challenge (HTTP $code); origin check ($origin) -> $origin_code (content not verified)"
            FAILURES+=("$label ($url) returned Cloudflare bot challenge (HTTP $code) and origin check ($origin) did not verify live content (HTTP $origin_code)")
            rm -f "$body" "$headers" "$origin_body"
            return
        fi
        echo "- FAIL: $label ($url) -> Cloudflare bot challenge (HTTP $code); no Pages origin fallback for this host"
        FAILURES+=("$label ($url) returned Cloudflare bot challenge (HTTP $code) with no origin fallback")
        rm -f "$body" "$headers"
        return
    fi

    echo "- FAIL: $label ($url) -> $code"
    FAILURES+=("$label ($url) returned HTTP $code")
    rm -f "$body" "$headers"
}

echo "## Quarterly listings check"
echo
echo "Repo version (pyproject.toml): $REPO_VERSION"
echo

echo "### Listing pages"
url_check "Glama listing" "$GLAMA_URL"
url_check "Motivation page" "$WHY_URL"
url_check "Site" "$SITE_URL"
echo

if [ "${CHECK_LISTINGS_SKIP_REGISTRY:-}" != "1" ]; then
    echo "### Official MCP registry entry"
    REGISTRY_JSON=$(curl -s -L --max-time 30 "https://registry.modelcontextprotocol.io/v0/servers?search=io.github.chuofringer/placeroot")
    if [ -z "$REGISTRY_JSON" ]; then
        echo "- FAIL: registry API (https://registry.modelcontextprotocol.io/v0/servers?search=io.github.chuofringer/placeroot) returned no response"
        FAILURES+=("Registry API returned no response")
    else
        LATEST_VERSION=$(echo "$REGISTRY_JSON" | jq -r '
            [.servers[] | select(.server.name == "io.github.chuofringer/placeroot")
                         | select(._meta."io.modelcontextprotocol.registry/official".isLatest == true)]
            | .[0].server.version // empty
        ')
        if [ -z "$LATEST_VERSION" ]; then
            echo "- FAIL: registry entry for io.github.chuofringer/placeroot not found (or no version marked latest)"
            FAILURES+=("Registry entry for io.github.chuofringer/placeroot not found, or no version marked latest")
        else
            echo "- registry's latest published version: $LATEST_VERSION"
            # Newest-first sort; if the repo version sorts ahead of the registry's,
            # the registry is behind.
            NEWEST=$(printf '%s\n%s\n' "$REPO_VERSION" "$LATEST_VERSION" | sort -V | tail -n1)
            if [ "$NEWEST" = "$REPO_VERSION" ] && [ "$LATEST_VERSION" != "$REPO_VERSION" ]; then
                echo "- FAIL: registry's latest version ($LATEST_VERSION) is older than the repo's ($REPO_VERSION)"
                FAILURES+=("Registry's latest listed version ($LATEST_VERSION) is older than the repo's ($REPO_VERSION) — publish-mcp-registry.yml may not have run, or the release didn't trigger it")
            else
                echo "- OK: registry version is current (>= repo version)"
            fi
        fi
    fi
    echo
fi

if [ "${CHECK_LISTINGS_SKIP_UVX:-}" != "1" ]; then
    echo "### Install instructions"
    if uvx --from placeroot placeroot --help >/dev/null 2>&1; then
        echo "- OK: 'uvx placeroot' installs and runs"
    else
        echo "- FAIL: 'uvx placeroot --help' did not succeed"
        FAILURES+=("The published install command ('uvx placeroot') did not run successfully")
    fi
    echo
fi

if [ "${#FAILURES[@]}" -eq 0 ]; then
    exit 0
fi

echo "### Summary"
echo
echo "${#FAILURES[@]} check(s) failed:"
for f in "${FAILURES[@]}"; do
    echo "- $f"
done
exit 1
