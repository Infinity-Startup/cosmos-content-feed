"""Bounded, integrity-checked cache of successfully fetched public source bytes."""
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

MAX_CACHE_BYTES = 2 * 1024 * 1024 * 1024


def cached_source(url, limit, host, fetch, directory=None, validate=None):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.netloc != host or parsed.username or parsed.password:
        raise ValueError('Unexpected source host')
    root = Path(directory or os.environ.get('PORTRAIT_SOURCE_CACHE', '/tmp/asura-portrait-source-cache'))
    key = hashlib.sha256(url.encode()).hexdigest()
    binary, metadata = root / (key + '.bin'), root / (key + '.json')
    try:
        info, body = json.loads(metadata.read_text()), binary.read_bytes()
        if info['url'] == url and len(body) <= limit and hashlib.sha256(body).hexdigest() == info['sha256'] and (validate is None or validate(body)):
            binary.touch()
            return body
    except (OSError, ValueError, KeyError):
        pass
    body = fetch(url, limit, host)
    if len(body) > limit:
        raise ValueError('Oversized source')
    if validate is not None and not validate(body):
        raise ValueError('Source response is not valid episode artwork or reader metadata')
    root.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(body)
    metadata.write_text(json.dumps({'url': url, 'sha256': hashlib.sha256(body).hexdigest()}))
    files = sorted(root.glob('*.bin'), key=lambda p: p.stat().st_mtime)
    size = sum(p.stat().st_size for p in files)
    for file in files:
        if size <= MAX_CACHE_BYTES:
            break
        size -= file.stat().st_size
        file.unlink()
        file.with_suffix('.json').unlink(missing_ok=True)
    return body
