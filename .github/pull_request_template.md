## What & why

<!-- What does this change, and which issue does it address? -->

## Checklist

- [ ] `uv run pytest` and `uv run ruff check .` pass locally
- [ ] No tool response shape changed — or the breaking change is called out above
- [ ] If tools were added/renamed/removed: README tool table + site tool grid updated (`tests/test_site_tools_sync.py` will catch drift)
- [ ] If the root README changed: `uv run python scripts/sync_npm_readme.py` was run (`tests/test_npm_readme_sync.py` will catch drift)
- [ ] User-visible changes have a `CHANGELOG.md` entry under Unreleased
