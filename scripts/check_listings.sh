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

FAILURES=()

url_check() {
    # url_check <label> <url>
    local label="$1" url="$2" code
    code=$(curl -s -o /dev/null -w '%{http_code}' -L --max-time 30 "$url")
    if [ "$code" = "200" ]; then
        echo "- OK: $label ($url) -> $code"
    else
        echo "- FAIL: $label ($url) -> $code"
        FAILURES+=("$label ($url) returned HTTP $code")
    fi
}

echo "## Quarterly listings check"
echo
echo "Repo version (pyproject.toml): $REPO_VERSION"
echo

echo "### Listing pages"
url_check "Glama listing" "https://glama.ai/mcp/servers/chuofringer/placeroot"
url_check "Motivation page" "https://placeroot.dev/why-placeroot"
url_check "Site" "https://placeroot.dev"
echo

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

echo "### Install instructions"
if uvx --from placeroot placeroot --help >/dev/null 2>&1; then
    echo "- OK: 'uvx placeroot' installs and runs"
else
    echo "- FAIL: 'uvx placeroot --help' did not succeed"
    FAILURES+=("The published install command ('uvx placeroot') did not run successfully")
fi
echo

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
