"""Generate screened episode portraits from public chapter pages; retain failures for retry."""
import argparse
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from refresh_chapter_previews import PreviewParser, image_url
from portrait_preview_quality import selector
from portrait_policy import MIN_SOURCE_CROP
from portrait_source_cache import cached_source


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        temporary.write(data)
        name = temporary.name
    try:
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _download(url, limit, host):
    from urllib.parse import urlsplit
    if urlsplit(url).scheme != 'https' or urlsplit(url).netloc != host:
        raise ValueError('Unexpected source host')
    if host == 'asurascans.com':
        time.sleep(2)
    with urlopen(Request(url, headers={'User-Agent': 'Infinity-ChapterPreviews/1.0'}), timeout=30) as response:
        if urlsplit(response.url).netloc != host:
            raise ValueError('Unexpected source redirect')
        body = response.read(limit + 1)
    if len(body) > limit:
        raise ValueError('Oversized source')
    return body


def download(url, limit, host, html_loader=None):
    def valid(body):
        try:
            if host == 'asurascans.com':
                from urllib.parse import urlsplit
                reader = PreviewParser()
                reader.feed(body.decode('utf-8'))
                number = float(urlsplit(url).path.rsplit('/chapter/', 1)[1])
                return bool(reader.props and reader.props.get('chapterNumber') == number and any(image_url(page.get('url')) for page in reader.props.get('pages', [])))
            import io
            from PIL import Image
            with Image.open(io.BytesIO(body)) as image:
                image.verify()
            return True
        except (ValueError, OSError, KeyError, IndexError):
            return False
    def fetch(source_url, source_limit, source_host):
        if html_loader and source_host == 'asurascans.com':
            return html_loader(source_url).encode('utf-8')
        return _download(source_url, source_limit, source_host)
    return cached_source(url, limit, host, fetch, validate=valid)


def process(site, model, public_url, engine=None, fetch=download, max_episodes=0, title_id=None):
    site = Path(site)
    queue_file = site / 'portrait-review-queue.json'
    queue = json.loads(queue_file.read_text())
    engine = engine or selector(model)
    exclusions_file = site / 'portrait-exclusions.json'
    exclusions = json.loads(exclusions_file.read_text()) if exclusions_file.exists() else {}
    pending, generated = [], 0
    source_failures, consecutive_refusals, deferred = 0, 0, 0
    stopped = False
    deadline = time.monotonic() + 45 * 60
    attempts = 0
    for item in queue['chapters']:
        if (title_id and item['titleId'] != title_id) or stopped or time.monotonic() >= deadline or (max_episodes and attempts >= max_episodes):
            pending.append(item)
            if not title_id or item['titleId'] == title_id:
                deferred += 1
            continue
        # Recovery after an interrupted run must retain an already published image.
        manifest_file = site / 'previews/asura' / (item['titleId'] + '.json')
        number = str(int(item['number']) if float(item['number']).is_integer() else item['number'])
        if manifest_file.exists() and number in json.loads(manifest_file.read_text()).get('previews', {}):
            continue
        attempts += 1
        attempted_at = datetime.now(timezone.utc).isoformat()
        episode_deadline = time.monotonic() + 300
        try:
            parser = PreviewParser()
            parser.feed(fetch(item['url'], 8_000_000, 'asurascans.com').decode('utf-8'))
            consecutive_refusals = 0
            if not parser.props or parser.props.get('chapterNumber') != item['number']:
                raise ValueError('Wrong chapter reader')
            selected = None
            # Bounded work, skip banners, try multiple actual episode pages.
            pages = [p['url'] for p in parser.props.get('pages', []) if image_url(p.get('url')) and p.get('width', 0) >= MIN_SOURCE_CROP and p.get('height', 0) >= MIN_SOURCE_CROP]
            pages = list(dict.fromkeys(pages))
            print(f"Searching {item['titleId']} chapter {item['number']}: {len(pages)} episode images", flush=True)
            # Cover the whole episode, including its later panels.
            indexes = sorted(set(round(i * (len(pages) - 1) / 31) for i in range(32))) if len(pages) > 32 else range(len(pages))
            scanned = 0
            for page_index in indexes:
                url = pages[page_index]
                if time.monotonic() >= episode_deadline:
                    break
                try:
                    body = fetch(url, 24_000_000, 'cdn.asurascans.com')
                    scanned += 1
                    for tight in (False, True):
                        selected = engine.select(body, min_crop=MIN_SOURCE_CROP, full_scan=True, tight=tight, excluded_hashes=[int(h, 16) for h in exclusions.get(item['titleId'], [])])
                        if selected:
                            break
                except HTTPError as error:
                    if error.code in (403, 429):
                        raise
                    if error.code not in (404, 410):
                        raise
                    continue
                except (OSError, ValueError):
                    # One corrupt/oversized image must not hide valid later panels.
                    continue
                if selected:
                    break
            if not selected:
                raise ValueError(f'No clean high-resolution portrait after {scanned} of {len(pages)} episode images')
            body, quality = selected
            # No editorial bypass and no claim that a human reviewed this image.
            if quality.get('textRegions') != 0 or quality.get('faceConfidence', 0) < .85 or quality.get('width') != 384 or quality.get('height') != 384:
                raise ValueError('Portrait failed automated quality gate')
            digest = hashlib.sha256(body).hexdigest()
            value = item['number']
            number = str(int(value) if float(value).is_integer() else value)
            relative = f"portraits/{item['titleId']}/{number}-auto-v5-{digest[:12]}.webp"
            asset = site / relative
            asset.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(asset, body)
            manifest_file = site / 'previews/asura' / (item['titleId'] + '.json')
            manifest = json.loads(manifest_file.read_text()) if manifest_file.exists() else {'titleId': item['titleId'], 'revision': 5, 'previews': {}}
            manifest['previews'][number] = {'chapterUrl': item['url'], 'sourceImageUrl': url, 'image': {'uri': public_url.rstrip('/') + '/' + relative}, 'quality': {**quality, 'reviewed': False, 'automated': True, 'screeningVersion': 1}}
            manifest_file.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(manifest_file, (json.dumps(manifest, indent=2) + '\n').encode())
            generated += 1
        except Exception as error:
            quality_failure = isinstance(error, ValueError) and str(error).startswith('No clean high-resolution portrait')
            if not quality_failure:
                source_failures += 1
            if isinstance(error, HTTPError):
                consecutive_refusals = consecutive_refusals + 1 if error.code == 403 else 0
                stopped = error.code == 429 or consecutive_refusals >= 3
            pending.append({**item, 'lastAttemptAt': attempted_at, 'reason': str(error)[:240], 'failureKind': 'quality' if quality_failure else 'source'})
        print(f"Portraits: {generated} generated; {len(pending)} pending", flush=True)
    queue['chapters'] = pending
    atomic_write(queue_file, (json.dumps(queue, indent=2) + '\n').encode())
    report_file = site / 'update-report.json'
    report = json.loads(report_file.read_text())
    report.update(portraitsGenerated=generated, portraitsAwaitingReview=len(pending), portraitSourceFailures=source_failures, portraitDeferred=deferred, portraitCheckedAt=datetime.now(timezone.utc).isoformat())
    atomic_write(report_file, (json.dumps(report, indent=2) + '\n').encode())
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site', type=Path, required=True)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--public-url', required=True)
    parser.add_argument('--max-episodes', type=int, default=0, help='Optional manual pilot limit; zero processes the full queue')
    parser.add_argument('--title', help='Optional manual title pilot; all titles remain eligible in daily runs')
    parser.add_argument('--browser', action='store_true', help='Use normal Chrome for uncached public reader metadata')
    args = parser.parse_args()
    if args.max_episodes < 0:
        parser.error('--max-episodes must be nonnegative')
    if args.browser:
        from asura_browser import AsuraBrowser
        with AsuraBrowser() as browser:
            process(args.site, args.model, args.public_url,
                    fetch=lambda url, limit, host: download(url, limit, host, html_loader=browser.fetch_html),
                    max_episodes=args.max_episodes, title_id=args.title)
    else:
        process(args.site, args.model, args.public_url, max_episodes=args.max_episodes, title_id=args.title)
