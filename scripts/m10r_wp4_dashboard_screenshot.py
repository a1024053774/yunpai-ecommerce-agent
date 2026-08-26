"""M10-R WP4 经营决策台浏览器截图（Playwright + Edge headless）。

依赖：`python -m pip install playwright`（本机使用系统 Edge，无需下载浏览器）。
用法：本地服务运行后执行：
  python scripts/m10r_wp4_dashboard_screenshot.py [--base-url http://127.0.0.1:8081]
"""

from __future__ import annotations

import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright


OUT_DIR = (
    Path(__file__).resolve().parents[1]
    / "docs" / "works" / "18-feature-m10r-wp4" / "screenshots"
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8081")
    parser.add_argument("--admin-id", default="admin-test")
    parser.add_argument("--admin-key", default="test-admin-key-123456")
    args = parser.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(f"{args.base_url}/admin", wait_until="networkidle")
        page.wait_for_timeout(600)
        if page.locator("#loginOverlay:visible").count() > 0:
            page.fill("#adminId", args.admin_id)
            page.fill("#adminKey", args.admin_key)
            page.click('#loginForm button[type="submit"]')
            page.wait_for_timeout(600)
        page.click('button[data-view="m10-decision"]')
        page.fill("#m10Store", "e2e-store")
        page.fill("#m10Period", "2026-08")
        page.click("#loadM10Decision")
        page.click("#m10SuggestionsBtn")
        page.wait_for_function(
            "!document.getElementById('m10Suggestions').innerHTML.includes('点击“生成经营建议”')",
            timeout=45000,
        )
        page.wait_for_function(
            "document.querySelectorAll('#m10Kpis .kpi').length >= 5",
            timeout=20000,
        )
        page.wait_for_timeout(600)
        page.screenshot(
            path=str(OUT_DIR / "m10-decision-formal-20260824.png"),
            full_page=True,
        )
        page.click("#m10TrendRows tr[data-sku]")
        page.wait_for_function(
            "document.getElementById('m10SkuPanel').style.display !== 'none' "
            "&& document.querySelectorAll('#m10SkuDetail h4').length >= 4",
            timeout=30000,
        )
        page.wait_for_timeout(800)
        page.screenshot(
            path=str(OUT_DIR / "m10-decision-drilldown-20260824.png"),
            full_page=True,
        )
        page.select_option("#m10Scope", "demo")
        page.click("#loadM10Decision")
        page.click("#m10SuggestionsBtn")
        page.wait_for_function(
            "!document.getElementById('m10Suggestions').innerHTML.includes('点击“生成经营建议”')",
            timeout=45000,
        )
        page.wait_for_timeout(1000)
        page.screenshot(
            path=str(OUT_DIR / "m10-decision-demo-20260824.png"),
            full_page=True,
        )
        browser.close()
    print(f"screenshots written to {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
