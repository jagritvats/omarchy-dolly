#!/usr/bin/env python3
"""Headless browser smoke test of the editor UI.

    python3 tests/editor_smoke.py EDITOR_URL OUT_DIR [--export] [--share]

Loads the editor, checks the lanes render, drags a zoom, switches to 9:16,
auto-detects speed-ups, draws a blur box, optionally runs a plain export and
the share sheet (GitHub preset -> copy -> upload when a target is configured).
Saves screenshots for review. Needs playwright + Chromium (CHROME env var).
"""
import asyncio
import os
import shutil
import sys
from pathlib import Path

from playwright.async_api import async_playwright


def chrome():
    for c in (os.environ.get("CHROME"), shutil.which("chromium"), shutil.which("google-chrome"), "/usr/bin/google-chrome"):
        if c and Path(c).exists():
            return c
    return None


async def wait_job(pg, sel, done=("Done", "Failed"), secs=900):
    for _ in range(secs):
        await pg.wait_for_timeout(1000)
        txt = await pg.inner_text(sel)
        if txt.startswith(done):
            return txt
    return txt


async def main(url, out: Path, do_export: bool, do_share: bool):
    out.mkdir(parents=True, exist_ok=True)
    errors = []
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path=chrome(), args=["--no-sandbox", "--disable-background-networking"])
        pg = await b.new_page(viewport={"width": 1680, "height": 1000})
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: m.type == "error" and errors.append(m.text))
        pg.on("dialog", lambda d: asyncio.ensure_future(d.accept("Demo style")))
        await pg.goto(url)
        await pg.wait_for_selector("#zlane .it", timeout=30000)
        await pg.wait_for_timeout(1500)
        print("zoom segments:", await pg.locator("#zlane .it").count())
        # drag the second zoom a bit
        seg = pg.locator("#zlane .it").nth(1)
        sb = await seg.bounding_box()
        await pg.mouse.move(sb["x"] + sb["width"] / 2, sb["y"] + 15)
        await pg.mouse.down()
        await pg.mouse.move(sb["x"] + sb["width"] / 2 + 20, sb["y"] + 15, steps=5)
        await pg.mouse.up()
        # speed-ups
        await pg.click("#speedDetect")
        await pg.wait_for_timeout(5000)
        print("speed segments:", await pg.locator("#slane .it").count())
        # seek into the terminal part and draw a blur box over the prompt line
        await pg.evaluate("seek(24.0)")
        await pg.wait_for_timeout(800)
        await pg.click("#addBlur")
        cb = await pg.locator("#view").bounding_box()
        await pg.mouse.move(cb["x"] + cb["width"] * 0.06, cb["y"] + cb["height"] * 0.62)
        await pg.mouse.down()
        await pg.mouse.move(cb["x"] + cb["width"] * 0.38, cb["y"] + cb["height"] * 0.68, steps=6)
        await pg.mouse.up()
        await pg.wait_for_timeout(600)
        print("privacy boxes:", await pg.locator("#plane .it").count())
        await pg.screenshot(path=str(out / "editor-1-lanes.png"))
        # vertical preview
        await pg.click("#aspect div[data-v='9:16']")
        await pg.wait_for_timeout(4000)
        await pg.evaluate("seek(10.6)")
        await pg.wait_for_timeout(800)
        await pg.screenshot(path=str(out / "editor-2-vertical.png"))
        await pg.click("#aspect div[data-v='16:9']")
        await pg.wait_for_timeout(2500)
        # save a style preset (prompt auto-accepted)
        await pg.click("#stylesave")
        await pg.wait_for_timeout(500)
        if do_export:
            await pg.evaluate("""() => { for (const [g,v] of [['res','1280'],['efps','30'],['qual','fast']])
                document.querySelector(`#${g} .opt[data-v="${v}"]`).click() }""")
            await pg.click("#exportBtn")
            print("export:", await wait_job(pg, "#progtxt"))
        if do_share:
            await pg.click("#shareTop")
            await pg.wait_for_selector(".pc", timeout=60000)
            await pg.wait_for_timeout(500)
            await pg.screenshot(path=str(out / "editor-3-share.png"))
            await pg.click(".pc[data-id='github']")
            await pg.click("#shExport")
            print("share export:", await wait_job(pg, "#shtxt"))
            await pg.click("#shCopy")
            await pg.wait_for_timeout(800)
            if await pg.locator("#shTarget option").count():
                await pg.click("#shUpload")
                await pg.wait_for_timeout(3000)
                print("link:", await pg.inner_text("#shUrl"))
            await pg.wait_for_timeout(800)
            await pg.screenshot(path=str(out / "editor-4-shared.png"))
        await b.close()
    errors = [e for e in errors if "favicon" not in e]
    print("js errors:", errors)
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1], Path(sys.argv[2]), "--export" in sys.argv, "--share" in sys.argv)))
