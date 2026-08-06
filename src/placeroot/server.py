"""PlaceRoot MCP server — ground AI agents in open map data.

Run: uv run placeroot
"""

from mcp.server.mcpserver import MCPServer

from placeroot import budget, overture

mcp = MCPServer(
    "placeroot",
    instructions=(
        "Grounds spatial questions in Overture Maps open data. "
        "Answers are compact and ranked; distances in meters. Responses "
        "that don't fit the token budget carry truncated: true — narrow "
        "the query (smaller radius, a category or name filter) instead of "
        "raising limit."
    ),
)


@mcp.tool()
def find_places(
    lat: float,
    lon: float,
    radius_m: float = 1000,
    category: str | None = None,
    name: str | None = None,
    limit: int = 10,
) -> dict:
    """Find named places near a point, nearest first.

    category matches Overture's taxonomy (e.g. 'coffee_shop', 'restaurant',
    'grocery'); name is a substring match on the place name. Results include
    operating_status so agents can reason about whether a place is open.
    Returns {"results": [...]}, plus truncated/omitted_count if the answer
    didn't fit the token budget.
    """
    rows = overture.find_places(lat, lon, radius_m, category, name, limit)
    return budget.apply_budget({"results": rows}, "results")


@mcp.tool()
def summarize_area(lat: float, lon: float, radius_m: float = 1000) -> dict:
    """Summarize what's in an area: total places and top categories."""
    result = overture.summarize_area(lat, lon, radius_m)
    return budget.apply_budget(result, "top_categories")


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
