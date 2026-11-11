"""Validate the complete staged feed and portrait assets before publication."""
import argparse
import hashlib
import math
from pathlib import Path
import re
from urllib.parse import urlsplit, unquote
try:
    from daily_asura_feed import read, validate_document, chapter_identity
except ModuleNotFoundError:
    from refresh import read, validate_document, chapter_identity


def validate(site, public_url):
    site = Path(site)
    root = public_url.rstrip('/') + '/'
    titles = read(site/'titles.json')
    index = read(site/'index.json')['titles']
    if set(index) != {t['id'] for t in titles}:
        raise ValueError('Index and title registry differ')
    assets = 0
    for title in titles:
        if not re.fullmatch('[a-z0-9-]+',title['id']):
            raise ValueError('Invalid title ID')
        doc = validate_document(read(site/'chapters/asura'/(title['id']+'.json')), title)
        summary = index[title['id']]
        if any(summary[key] != doc[key] for key in ('seriesUrl','chapterCount','checkedAt')):
            raise ValueError('Index does not match chapter document: '+title['id'])
        manifest = site/'previews/asura'/(title['id']+'.json')
        if title['category'] == 'Novels':
            if manifest.exists() and read(manifest).get('previews'):
                raise ValueError('Novel chapter portraits must not be published')
            continue
        previews=read(manifest)
        if previews.get('titleId') != title['id'] or previews.get('revision') != 5:
            raise ValueError('Portrait manifest identity mismatch')
        chapters={c['number']:c for c in doc['chapters']}
        for number,preview in previews['previews'].items():
            chapter=chapters.get(float(number))
            if not chapter or chapter_identity(preview['chapterUrl']) != chapter_identity(chapter['url']):
                raise ValueError('Portrait belongs to another chapter')
            source=urlsplit(preview['sourceImageUrl'])
            if source.scheme!='https' or source.netloc!='cdn.asurascans.com' or not re.match(r'/asura-images/chapters(?:-merged|-restored|-stitched)?/',source.path):
                raise ValueError('Portrait has an invalid image source')
            quality=preview['quality']
            confidence=quality.get('faceConfidence',0)
            if quality.get('version')!=5 or quality.get('width')!=quality.get('height') or quality.get('width',0)<384 or quality.get('textRegions')!=0 or not math.isfinite(confidence) or not 0<=confidence<=1 or (confidence<.85 and not quality.get('editorialSelection')):
                raise ValueError('Portrait failed quality metadata checks')
            if not (quality.get('reviewed') is True or (quality.get('automated') is True and quality.get('screeningVersion')==1 and confidence>=.85)):
                raise ValueError('Portrait has no accepted screening evidence')
            uri=preview['image']['uri']
            if not uri.startswith(root): raise ValueError('Portrait outside the public feed')
            relative=Path(unquote(uri[len(root):]))
            if relative.is_absolute() or '..' in relative.parts or not str(relative).startswith('portraits/'+title['id']+'/'):
                raise ValueError('Invalid portrait asset path')
            expected=re.search(r'-([0-9a-f]{12})\.webp$',relative.name)
            if not expected or hashlib.sha256((site/relative).read_bytes()).hexdigest()[:12]!=expected.group(1):
                raise ValueError('Missing or corrupt portrait asset: '+str(relative))
            assets+=1
    return {'titles':len(titles),'portraits':assets}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site',type=Path,required=True)
    parser.add_argument('--public-url',required=True)
    args=parser.parse_args()
    print(validate(args.site,args.public_url))
