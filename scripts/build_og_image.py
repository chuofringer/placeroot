#!/usr/bin/env python3
"""Renders site/og-image.png and site/og-image-dark.png from one template.

    uv run --group browser python scripts/build_og_image.py

The banner is the first thing anyone sees on the repo page, on placeroot.dev's
social cards, and in every link unfurl — and it was the one asset in this repo
that was hand-made, so nobody could change the wording without redrawing it.
Now it is generated from the HTML below, the same way docs/benchmarks.md's
numbers are generated rather than typed.

Two deliberate choices about shape:

- **1280x540 (2.37:1), not 1200x630 (1.9:1).** The old ratio is the classic
  Open Graph card, which is nearly a square once GitHub scales it into a
  README — it read as a picture sitting in the page rather than a banner
  across it. The wider strip fills the column. It still satisfies every
  consumer that matters: OG/Twitter want >=1.91:1 and at least 1200x630 of
  *area* is a recommendation rather than a requirement, and Twitter's
  summary_large_image crops to 2:1 anyway.
- **No internal margin.** The artwork bleeds to the edges, so the only
  padding is the page's own. The previous asset drew its own dark border,
  which on a dark repo page looked like the image had failed to load edge to
  edge.

The composition is centred rather than left-aligned for a functional reason,
not a taste one: Twitter's summary_large_image crops to 2:1, taking the crop
from the centre, so a left-aligned headline loses its right edge on exactly
the surface the card exists for.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "site"

WIDTH, HEIGHT = 1280, 540

# Same palette as site/index.html — pulled here as named constants so a theme
# change is one edit in each place rather than a hunt through hex literals.
LIGHT = {
    "bg": "#fbfaf5", "panel": "#f3f1e7", "ink": "#171c17", "muted": "#5f6a5c",
    "accent": "#5c8a63", "chip_bg": "#e9e6d8", "chip_ink": "#2c342b",
}
DARK = {
    "bg": "#141714", "panel": "#1b201b", "ink": "#f4f3ec", "muted": "#a8b3a4",
    "accent": "#9ec6a3", "chip_bg": "#232a22", "chip_ink": "#e8ece6",
}

TEMPLATE = """<!doctype html>
<meta charset="utf-8">
<style>
  @import url('https://fonts.googleapis.com/css2?family=Poppins:wght@400;600;700&display=swap');
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  html, body {{ width: {w}px; height: {h}px; overflow: hidden; }}
  body {{
    background: {bg};
    font-family: Poppins, ui-sans-serif, system-ui, -apple-system, sans-serif;
    display: flex; flex-direction: column; justify-content: center; align-items: center;
    padding: 0 84px; position: relative; text-align: center;
  }}
  /* A soft wash so the flat background isn't a dead rectangle at this size. */
  body::after {{
    content: ""; position: absolute; inset: 0;
    background: radial-gradient(1200px 560px at 50% 12%, {panel} 0%, transparent 70%);
    z-index: 0;
  }}
  .stack {{ position: relative; z-index: 1; }}
  .byline {{
    font-size: 19px; font-weight: 400; color: {muted};
    letter-spacing: 0.01em; margin-bottom: 22px;
  }}
  .byline b {{ font-weight: 600; color: {ink}; }}
  .byline i {{ font-style: normal; color: {accent}; }}
  .brand {{ display: flex; align-items: center; gap: 14px; margin-bottom: 34px;
             justify-content: center; }}
  .brand svg {{ width: 42px; height: 40px; display: block; }}
  .brand span {{ font-size: 30px; font-weight: 600; color: {ink}; letter-spacing: -0.01em; }}
  h1 {{
    font-size: 70px; font-weight: 700; line-height: 1.06;
    letter-spacing: -0.034em; color: {ink};
  }}
  h1 em {{ font-style: normal; color: {accent}; }}
  p {{
    font-size: 26px; font-weight: 400; color: {muted};
    margin-top: 26px; letter-spacing: -0.01em;
  }}
  .chip {{
    display: inline-flex; align-items: center; gap: 11px; margin-top: 34px;
    background: {chip_bg}; color: {chip_ink}; border-radius: 999px;
    padding: 15px 27px; font-size: 22px; font-weight: 600; letter-spacing: -0.01em;
  }}
  .dot {{ width: 9px; height: 9px; border-radius: 50%; background: #e8b04b; }}
</style>
<div class="stack">
  <div class="byline">Developed by <b>Vibe Mapper</b> · <i>vibemapper.dev</i></div>
  <div class="brand">
    <svg viewBox="0 0 96 92" aria-hidden="true">
      <rect x="8" y="8" width="80" height="58" rx="18" fill="{accent}"></rect>
      <path d="M28 66 L28 86 L48 66 Z" fill="{accent}"></path>
      <circle cx="48" cy="33" r="10" fill="{bg}"></circle>
      <circle cx="48" cy="33" r="4" fill="#e8b04b"></circle>
      <path d="M48 43 L48 52" stroke="{bg}" stroke-width="4" stroke-linecap="round"></path>
    </svg>
    <span>placeroot</span>
  </div>
  <h1>Ask about anywhere.<br>Get a <em>real answer</em>.</h1>
  <p>Open map data for your AI — free, no account, no API key.</p>
  <div class="chip"><span class="dot"></span>What's around downtown Palo Alto?</div>
</div>
"""


async def render(theme: dict, out: Path) -> None:
    from playwright.async_api import async_playwright

    html = TEMPLATE.format(w=WIDTH, h=HEIGHT, **theme)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(
            viewport={"width": WIDTH, "height": HEIGHT}, device_scale_factor=2
        )
        await page.set_content(html)
        # The webfont arrives over the network; screenshotting before it lands
        # bakes the fallback face into the asset.
        try:
            await page.wait_for_function("document.fonts.ready.then(() => true)", timeout=15_000)
        except Exception:  # noqa: BLE001 - a missing webfont is not worth failing on
            print("  (webfont wait timed out; rendering with the fallback face)")
        await page.screenshot(path=str(out))
        await browser.close()
    print(f"{out.relative_to(ROOT)} ({out.stat().st_size // 1024} KB, {WIDTH}x{HEIGHT} @2x)")


async def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    await render(LIGHT, OUT_DIR / "og-image.png")
    await render(DARK, OUT_DIR / "og-image-dark.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
