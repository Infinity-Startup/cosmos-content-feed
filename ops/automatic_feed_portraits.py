"""Generate screened episode portraits from public chapter pages; retain failures for retry."""
import argparse
import hashlib
import json
import time
from pathlib import Path
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from refresh_chapter_previews import PreviewParser, image_url
from portrait_preview_quality import selector
from portrait_policy import MIN_SOURCE_CROP
from portrait_source_cache import cached_source


def _download(url, limit, host):
    from urllib.parse import urlsplit
    if urlsplit(url).scheme != 'https' or urlsplit(url).netloc != host:
        raise ValueError('Unexpected source host')
    with urlopen(Request(url, headers={'User-Agent': 'Infinity-ChapterPreviews/1.0'}), timeout=30) as response:
        if urlsplit(response.url).netloc != host:
            raise ValueError('Unexpected source redirect')
        body = response.read(limit + 1)
    if len(body) > limit:
        raise ValueError('Oversized source')
    return body


def download(url, limit, host):
    return cached_source(url, limit, host, _download)


def process(site, model, public_url, engine=None, fetch=download, max_episodes=0, title_id=None):
    site = Path(site)
    queue_file = site / 'portrait-review-queue.json'
    queue = json.loads(queue_file.read_text())
    engine = engine or selector(model)
    exclusions_file = site / 'portrait-exclusions.json'
    exclusions = json.loads(exclusions_file.read_text()) if exclusions_file.exists() else {}
    pending, generated = [], 0
    stopped = False
    deadline = time.monotonic() + 45 * 60
    attempts = 0
    for item in queue['chapters']:
        if (title_id and item['titleId'] != title_id) or stopped or time.monotonic() >= deadline or (max_episodes and attempts >= max_episodes):
            pending.append(item)
            continue
        attempts += 1
        attempted_at = datetime.now(timezone.utc).isoformat()
        episode_deadline = time.monotonic() + 300
        try:
            parser = PreviewParser()
            parser.feed(fetch(item['url'], 8_000_000, 'asurascans.com').decode('utf-8'))
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
            asset.write_bytes(body)
            manifest_file = site / 'previews/asura' / (item['titleId'] + '.json')
            manifest = json.loads(manifest_file.read_text()) if manifest_file.exists() else {'titleId': item['titleId'], 'revision': 5, 'previews': {}}
            manifest['previews'][number] = {'chapterUrl': item['url'], 'sourceImageUrl': url, 'image': {'uri': public_url.rstrip('/') + '/' + relative}, 'quality': {**quality, 'reviewed': False, 'automated': True, 'screeningVersion': 1}}
            manifest_file.parent.mkdir(parents=True, exist_ok=True)
            manifest_file.write_text(json.dumps(manifest, indent=2) + '\n')
            generated += 1
        except Exception as error:
            if isinstance(error, HTTPError) and error.code in (403, 429):
                stopped = True
            pending.append({**item, 'lastAttemptAt': attempted_at, 'reason': str(error)[:240]})
        print(f"Portraits: {generated} generated; {len(pending)} pending", flush=True)
    queue['chapters'] = pending
    queue_file.write_text(json.dumps(queue, indent=2) + '\n')
    report_file = site / 'update-report.json'
    report = json.loads(report_file.read_text())
    report.update(portraitsGenerated=generated, portraitsAwaitingReview=len(pending))
    report_file.write_text(json.dumps(report, indent=2) + '\n')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site', type=Path, required=True)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--public-url', required=True)
    parser.add_argument('--max-episodes', type=int, default=0, help='Optional manual pilot limit; zero processes the full queue')
    parser.add_argument('--title', help='Optional manual title pilot; all titles remain eligible in daily runs')
    args = parser.parse_args()
    if args.max_episodes < 0:
        parser.error('--max-episodes must be nonnegative')
    process(args.site, args.model, args.public_url, max_episodes=args.max_episodes, title_id=args.title)
