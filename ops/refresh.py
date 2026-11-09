"""Daily public chapter-feed refresh. Only a complete valid run replaces the output."""
import argparse
import concurrent.futures
from datetime import datetime, timezone
import json
import math
import re
from pathlib import Path
import shutil
import tempfile
import threading
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from update_asura_chapters import parse_chapters, ChapterParser

MAX_PAGE_BYTES = 8 * 1024 * 1024
STOP = threading.Event()


def read(path):
    return json.loads(path.read_text())


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


def series_identity(value):
    url = urlsplit(value)
    if url.scheme != 'https' or url.netloc != 'asurascans.com' or url.query or url.fragment:
        raise ValueError('Invalid Asura URL')
    return re.sub(r'-[0-9a-f]{8}$', '', url.path.rstrip('/'))


def chapter_identity(value):
    url = urlsplit(value)
    series, separator, number = url.path.rpartition('/chapter/')
    if not separator:
        raise ValueError('Not a chapter URL')
    return series_identity('https://' + url.netloc + series), number


def validate_document(doc, title):
    if not re.fullmatch(r'[a-z0-9-]+', title['id']):
        raise ValueError('Invalid title identifier')
    series = title['sourceUrl'].split('/chapter/')[0]
    if doc.get('schemaVersion') != 1 or doc.get('provider') != 'asura' or doc.get('titleId') != title['id'] or doc.get('seriesUrl') != series:
        raise ValueError('Chapter document identity mismatch')
    url = urlsplit(series)
    if url.scheme != 'https' or url.netloc != 'asurascans.com' or url.query or url.fragment or not re.fullmatch(r'/(comics|novels)/[a-z0-9-]+', url.path):
        raise ValueError('Invalid Asura series')
    if datetime.fromisoformat(doc['checkedAt'].replace('Z', '+00:00')).tzinfo is None:
        raise ValueError('Snapshot timestamp must include its timezone')
    chapters = doc['chapters']
    if not isinstance(chapters, list) or len(chapters) != doc['chapterCount'] or not 1 <= len(chapters) <= 25000:
        raise ValueError('Invalid chapter count')
    prior = -1
    for chapter in chapters:
        number = chapter['number']
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or number < 0 or number <= prior:
            raise ValueError('Invalid chapter order')
        prior = number
        expected = series.rstrip('/') + '/chapter/' + str(int(number) if float(number).is_integer() else number)
        if series_identity(chapter['url']) == '':
            raise ValueError('Invalid chapter URL')
        if chapter_identity(chapter['url']) != chapter_identity(expected) or not isinstance(chapter['title'], str) or not isinstance(chapter['premium'], bool):
            raise ValueError('Invalid chapter link or metadata')
        date = chapter.get('publishedAt')
        if date is not None:
            datetime.fromisoformat(date.replace('Z', '+00:00'))
    return doc


def fetch_document(title, checked_at):
    series = title['sourceUrl'].split('/chapter/')[0]
    request = Request(series, headers={'User-Agent': 'Mozilla/5.0 Infinity-ContentUpdater/1.0'})
    if STOP.is_set():
        raise RuntimeError('Source access stopped')
    try:
        with urlopen(request, timeout=30) as response:
            if urlsplit(response.url).netloc != 'asurascans.com':
                raise ValueError('Unexpected source redirect')
            body = response.read(MAX_PAGE_BYTES + 1)
    except HTTPError as error:
        if error.code in (403, 429):
            STOP.set()
        raise
    if len(body) > MAX_PAGE_BYTES:
        raise ValueError('Source page too large')
    html = body.decode('utf-8')
    parser = ChapterParser()
    parser.feed(html)
    canonical = parser.canonical or series
    if series_identity(canonical) != series_identity(series):
        raise ValueError('Canonical URL points to a different series')
    chapters = parse_chapters(html, canonical)
    return validate_document({'schemaVersion': 1, 'provider': 'asura', 'titleId': title['id'], 'seriesUrl': series,
                              'checkedAt': checked_at, 'chapterCount': len(chapters), 'chapters': chapters}, title)


def refresh(seed, output, fetch=fetch_document, now=None):
    seed, output = Path(seed).resolve(), Path(output).resolve()
    if output == seed or seed in output.parents or output in seed.parents or output.exists():
        raise ValueError('Use a new output directory outside the seed directory')
    STOP.clear()
    titles = read(seed / 'titles.json')
    if not titles or len({item['id'] for item in titles}) != len(titles):
        raise ValueError('Invalid catalogue registry')
    checked_at = now or datetime.now(timezone.utc).isoformat()
    def update(title):
        old = validate_document(read(seed / 'chapters/asura' / (title['id'] + '.json')), title)
        try:
            new = validate_document(fetch(title, checked_at), title)
        except Exception as error:
            raise ValueError(title['id'] + ': ' + str(error)) from error
        if datetime.fromisoformat(new['checkedAt'].replace('Z', '+00:00')) < datetime.fromisoformat(old['checkedAt'].replace('Z', '+00:00')):
            raise ValueError('Source check is older than the last successful snapshot')
        old_links = {item['number']: item['url'] for item in old['chapters']}
        new_links = {item['number']: item['url'] for item in new['chapters']}
        if any(number not in new_links or chapter_identity(new_links[number]) != chapter_identity(link) for number, link in old_links.items()):
            raise ValueError('Published chapters disappeared or changed URL; manual review required: ' + title['id'])
        return title, new, old_links
    # No files are published when any series fails, is incomplete or refuses requests.
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        documents = list(pool.map(update, titles))
    summary, additions, portrait_queue = {}, [], []
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='asura-feed-', dir=output.parent) as folder:
        staged = Path(folder) / 'site'
        shutil.copytree(seed, staged)
        for title, doc, old_links in documents:
            write(staged / 'chapters/asura' / (title['id'] + '.json'), doc)
            dates = [chapter['publishedAt'] for chapter in doc['chapters'] if chapter.get('publishedAt')]
            info = {'seriesUrl': doc['seriesUrl'], 'chapterCount': doc['chapterCount'], 'checkedAt': doc['checkedAt'], 'firstChapter': doc['chapters'][0]['number']}
            if dates:
                info['latestPublishedAt'] = max(dates, key=lambda value: datetime.fromisoformat(value.replace('Z', '+00:00')).replace(tzinfo=timezone.utc) if len(value) == 10 else datetime.fromisoformat(value.replace('Z', '+00:00')))
            summary[title['id']] = info
            portraits_file = staged / 'previews/asura' / (title['id'] + '.json')
            portraits = read(portraits_file)['previews'] if portraits_file.exists() else {}
            exceptions = read(staged / 'portrait-exceptions.json') if (staged / 'portrait-exceptions.json').exists() else {}
            for chapter in doc['chapters']:
                item = {'titleId': title['id'], 'title': title['title'], 'number': chapter['number'], 'url': chapter['url']}
                if chapter['number'] not in old_links:
                    additions.append(item)
                value = chapter['number']
                number = str(int(value) if float(value).is_integer() else value)
                if title['category'] in ('Comics', 'Manga') and not chapter['premium'] and number not in portraits and title['id'] + ':' + number not in exceptions:
                    portrait_queue.append(item)
        write(staged / 'index.json', {'schemaVersion': 1, 'checkedAt': checked_at, 'titles': summary})
        write(staged / 'portrait-review-queue.json', {'checkedAt': checked_at, 'chapters': portrait_queue})
        write(staged / 'update-report.json', {'checkedAt': checked_at, 'titlesChecked': len(titles), 'newChapters': additions, 'portraitsAwaitingReview': len(portrait_queue)})
        (staged / '.nojekyll').touch()
        (staged / 'index.html').write_text('<!doctype html><title>Cosmos content feed</title><p>Public Asura chapter metadata and reviewed episode portraits.</p>')
        staged.rename(output)
    return {'titlesChecked': len(titles), 'newChapters': len(additions), 'portraitsAwaitingReview': len(portrait_queue)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seed', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(refresh(args.seed, args.output)))


if __name__ == '__main__':
    main()
