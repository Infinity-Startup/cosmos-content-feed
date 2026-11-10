"""Collect one publicly available comic page image per chapter, with resumable snapshots."""
import argparse
import concurrent.futures
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
from pathlib import Path
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from update_asura_chapters import decode_prop

ROOT = Path(__file__).resolve().parents[1]
STOP = threading.Event()


def image_url(value):
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    return parsed.scheme == 'https' and parsed.netloc == 'cdn.asurascans.com' and parsed.path.startswith(('/asura-images/chapters/', '/asura-images/chapters-merged/', '/asura-images/chapters-restored/', '/asura-images/chapters-stitched/')) and not parsed.fragment


class PreviewParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.props = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'astro-island' and 'ChapterReader.' in attrs.get('component-url', ''):
            encoded = json.loads(attrs['props'])
            self.props = {key: decode_prop(encoded[key]) for key in ('chapterNumber', 'pages') if key in encoded}


def parse_preview(html, number):
    parser = PreviewParser()
    parser.feed(html)
    props = parser.props
    if not props or props.get('chapterNumber') != number:
        raise ValueError('Reader does not match requested chapter')
    pages = props.get('pages', [])
    candidates = [page for page in pages if image_url(page.get('url')) and isinstance(page.get('width'), (int, float)) and isinstance(page.get('height'), (int, float)) and page['width'] >= 300 and page['height'] >= 300]
    if not candidates:
        return None
    # Skip landscape credits/banners when a portrait comic page is supplied.
    page = next((page for page in candidates if page['height'] >= page['width'] * 1.25), candidates[0])
    return {'imageUrl': page['url'], 'width': page['width'], 'height': page['height']}


def fetch_preview(chapter):
    if STOP.is_set() or chapter['premium']:
        return None
    request = Request(chapter['url'], headers={'User-Agent': 'Infinity-ChapterPreviews/1.0', 'Accept': 'text/html'})
    try:
        with urlopen(request, timeout=20) as response:
            html = response.read(4_000_001)
        if len(html) > 4_000_000:
            raise ValueError('Oversized chapter page')
        preview = parse_preview(html.decode('utf-8'), chapter['number'])
        return {'chapterUrl': chapter['url'], **preview} if preview else None
    except HTTPError as error:
        if error.code in (403, 429):
            STOP.set()
            print('Source refused requests; stopping without bypassing it:', error.code, flush=True)
        return None
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--title', help='Refresh just this catalogue title')
    parser.add_argument('--workers', type=int, default=4, choices=range(1, 7))
    parser.add_argument('--refresh', action='store_true', help='Recheck previously collected image references')
    args = parser.parse_args()
    entries = json.loads((ROOT / 'src/data/asura-titles.json').read_text())
    total = 0
    for entry in entries:
        if entry['category'] not in ('Comics', 'Manga') or (args.title and args.title != entry['id']):
            continue
        listing = json.loads((ROOT / f"content/chapters/asura/{entry['id']}.json").read_text())
        output = ROOT / f"content/chapter-previews/asura/{entry['id']}.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        previous = json.loads(output.read_text()) if output.exists() else {}
        previews = {} if args.refresh else previous.get('previews', {})
        valid = {str(chapter['number']): chapter for chapter in listing['chapters']}
        previews = {number: preview for number, preview in previews.items() if number in valid and preview['chapterUrl'] == valid[number]['url'] and image_url(preview['imageUrl'])}
        pending = [chapter for chapter in listing['chapters'] if str(chapter['number']) not in previews and not chapter['premium']]
        checked = 0
        def save():
            data = {'schemaVersion': 1, 'provider': 'asura', 'titleId': entry['id'], 'seriesUrl': listing['seriesUrl'], 'checkedAt': datetime.now(timezone.utc).isoformat(), 'previews': dict(sorted(previews.items(), key=lambda item: float(item[0])))}
            temp = output.with_suffix('.tmp')
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
            temp.replace(output)
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            # executor.map preserves ordering; per-title progress is checkpointed.
            for chapter, preview in zip(pending, pool.map(fetch_preview, pending)):
                if preview:
                    previews[str(chapter['number'])] = preview
                checked += 1
                if checked % 50 == 0:
                    save()
                    print(entry['id'], f'{len(previews)}/{len(listing["chapters"])} previews', flush=True)
                if STOP.is_set():
                    break
        save()
        total += len(previews)
        print(entry['id'], f'complete: {len(previews)}/{len(listing["chapters"])} actual chapter images', flush=True)
        if STOP.is_set():
            break
        time.sleep(.2)
    print('Saved chapter previews:', total, flush=True)


if __name__ == '__main__':
    main()
