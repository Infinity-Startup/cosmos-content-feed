"""Normal isolated Chrome access to public Asura pages; never solve challenges."""
import re
import time
from urllib.parse import urlsplit
from urllib.error import HTTPError


def validate_page_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.netloc != 'asurascans.com' or parsed.query or parsed.fragment or not re.fullmatch(r'/(comics|novels)/[a-z0-9-]+(?:/chapter/[0-9]+(?:\.[0-9]+)?)?/?', parsed.path):
        raise ValueError('Unexpected Asura page URL')


def check_response(url, status, headers, html):
    validate_page_url(url)
    if headers.get('cf-mitigated') == 'challenge' or '<title>Just a moment...' in html:
        raise HTTPError(url, 403, 'Source browser verification required', headers, None)
    if status >= 400:
        raise HTTPError(url, status, 'Source refused browser request', headers, None)
    if len(html.encode('utf-8')) > 8_000_000:
        raise ValueError('Oversized source page')
    return html


class AsuraBrowser:
    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self.runtime = sync_playwright().start()
        try:
            self.browser = self.runtime.chromium.launch(channel='chrome', headless=False)
            self.context = self.browser.new_context(accept_downloads=False)
            self.page = self.context.new_page()
            self.next_request = 0
            return self
        except BaseException:
            self.runtime.stop()
            raise

    def fetch_html(self, url):
        validate_page_url(url)
        time.sleep(max(0, self.next_request - time.monotonic()))
        response = self.page.goto(url, wait_until='domcontentloaded', timeout=45000)
        self.next_request = time.monotonic() + 1
        if response is None:
            raise ValueError('Source returned no response')
        html = self.page.content()
        return check_response(self.page.url, response.status, response.headers, html)

    def __exit__(self, *error):
        try:
            self.browser.close()
        finally:
            self.runtime.stop()
