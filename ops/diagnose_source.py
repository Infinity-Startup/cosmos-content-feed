"""Small read-only comparison of the two normal public metadata loaders."""
import json
from pathlib import Path
from datetime import datetime, timezone
from refresh import fetch_document, document_from_html
from asura_browser import AsuraBrowser

ids = ('asura-novel-first-prince', 'asura-novel-raising-villains')
titles = {t['id']: t for t in json.loads(Path('seed/titles.json').read_text())}
now = datetime.now(timezone.utc).isoformat()
for id in ids:
    try:
        doc = fetch_document(titles[id], now)
        print('DIRECT', id, doc['chapterCount'], 'chapters', flush=True)
    except Exception as error:
        print('DIRECT', id, type(error).__name__, str(error)[:300], flush=True)
        break
with AsuraBrowser() as browser:
    for index, id in enumerate(ids):
        try:
            html = browser.fetch_html(titles[id]['sourceUrl'])
            doc = document_from_html(titles[id], now, html)
            print('BROWSER', id, doc['chapterCount'], 'chapters', flush=True)
            if index == 0:
                print('Allowing normal page scripts to finish for eight seconds', flush=True)
                browser.page.wait_for_timeout(8000)
        except Exception as error:
            print('BROWSER', id, type(error).__name__, str(error)[:300], flush=True)
            break
