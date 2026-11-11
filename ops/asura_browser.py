"""Normal isolated Chrome access to public Asura pages; never solve challenges."""
import re
import time
from urllib.parse import urlsplit
from urllib.error import HTTPError


def validate_page_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.netloc != 'asurascans.com' or parsed.query or parsed.fragment or not re.fullmatch(r'/(comics|novels)/[a-z0-9-]+(?:/chapter/[0-9]+(?:\.[0-9]+)?)?/?', parsed.path):
        raise ValueError('Unexpected Asura page URL: ' + parsed.scheme + '://' + parsed.netloc + parsed.path)


def check_response(url, status, headers, html, requested_url=None):
    if headers.get('cf-mitigated') == 'challenge' or '<title>Just a moment...' in html:
        raise HTTPError(url, 403, 'Source browser verification required', headers, None)
    if status >= 400:
        raise HTTPError(url, status, 'Source refused browser request', headers, None)
    validate_page_url(url)
    if requested_url is not None:
        validate_page_url(requested_url)
        identity = lambda value: re.sub(r'-[0-9a-f]{8}(?=/chapter/|$)', '', urlsplit(value).path.rstrip('/'))
        if identity(url) != identity(requested_url):
            raise ValueError('Source redirected to a different story or chapter')
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
        navigations = []
        def observe(response):
            if response.request.is_navigation_request() and response.request.frame == self.page.main_frame:
                navigations.append(response)
        self.page.on('response', observe)
        try:
            response = self.page.goto(url, wait_until='domcontentloaded', timeout=45000)
            if response is None:
                raise ValueError('Source returned no response')
            # A managed verification page may finish and navigate normally after
            # DOMContentLoaded. Allow ordinary browser loading to settle; no clicks,
            # challenge solving, profile reuse or fingerprint changes are performed.
            if response.headers.get('cf-mitigated') == 'challenge':
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    self.page.wait_for_timeout(500)
                    latest = navigations[-1] if navigations else response
                    if latest is not response and latest.headers.get('cf-mitigated') != 'challenge':
                        response = latest
                        break
            # Metadata is server-rendered. Validate the response's own URL/body,
            # even when later client navigation changes the address bar.
            return check_response(response.url, response.status, response.headers,
                                  response.text(), requested_url=url)
        finally:
            self.next_request = time.monotonic() + 2
            self.page.remove_listener('response', observe)

    def __exit__(self, *error):
        try:
            self.browser.close()
        finally:
            self.runtime.stop()
