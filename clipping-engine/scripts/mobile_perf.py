"""Measure private authenticated mobile HTML, poster and bounded video range latency."""
import argparse
from collections import Counter
from pathlib import Path
import re
import shutil
import statistics
from tempfile import TemporaryDirectory
import time
from urllib.parse import urlsplit

import httpx

from clipengine.config import get_settings


def profile(snapshot):
    """Profile a scratch copy of an existing SQLite backup. Never write to production."""
    from dataclasses import replace
    from unittest.mock import patch

    from fastapi.testclient import TestClient
    from jinja2 import Template
    from clipengine.db import Database
    from clipengine.web.app import create_app

    calls, seconds = Counter(), Counter()

    def instrument(method, label):
        def wrapped(*args, **kwargs):
            start = time.perf_counter()
            try:
                return method(*args, **kwargs)
            finally:
                calls[label] += 1
                seconds[label] += time.perf_counter() - start
        return wrapped

    with TemporaryDirectory(prefix='clipengine-mobile-profile-') as directory:
        root = Path(directory)
        shutil.copyfile(snapshot, root / 'clipengine.sqlite3')
        settings = replace(get_settings(), data_dir=root, dashboard_password='', workspace_id=None)
        app = create_app(settings, start_worker=False)
        with (TestClient(app) as client, patch.object(Database, 'query', instrument(Database.query, 'SQL reads')),
              patch.object(Template, 'render', instrument(Template.render, 'template renders')),
              patch.object(httpx.Client, 'send', side_effect=AssertionError('Unexpected outbound HTTP'))):
            for path in ('/m', '/m/results'):
                calls.clear()
                seconds.clear()
                start = time.perf_counter()
                reply = client.get(path)
                print(path, reply.status_code, f'{(time.perf_counter()-start)*1000:.0f}ms',
                      dict(calls), {key: round(value*1000, 1) for key, value in seconds.items()}, flush=True)


def measure(base, password, repeats):
    print(f'Endpoint: {urlsplit(base).hostname}', flush=True)
    with httpx.Client(base_url=base, trust_env=False, timeout=15) as client:
        if password:
            reply = client.post('/login', data={'password': password})
            assert reply.status_code == 303 and 'ce_auth' in client.cookies, 'Dashboard authentication failed'

        def timed(path, headers=None):
            elapsed = []
            for _ in range(repeats):
                start = time.perf_counter()
                with client.stream('GET', path, headers=headers) as response:
                    head = time.perf_counter()
                    content = response.read() if not headers or response.status_code == 206 else b''
                    end = time.perf_counter()
                elapsed.append((head-start, end-start))
                print(path, response.status_code, len(content), round((head-start)*1000),
                      round((end-start)*1000), response.headers.get('content-range', '-'), flush=True)
            print('Median TTFB/total ms:', *(round(statistics.median([pair[i] for pair in elapsed])*1000)
                                               for i in (0, 1)), flush=True)
            return content

        html = timed('/m')
        timed('/m/results')
        match = re.search(rb'(/clips/[0-9]+/video)', html)
        if match:
            video = match[1].decode()
            timed(video.replace('/video', '/thumb'))
            timed(video, {'Range': 'bytes=0-65535'})
            timed(video, {'Range': 'bytes=-65536'})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('bases', nargs='*')
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--profile-snapshot', type=Path)
    args = parser.parse_args()
    if args.profile_snapshot:
        profile(args.profile_snapshot)
    allowed = {'http://127.0.0.1:8765', get_settings().dashboard_url.rstrip('/')}
    for base in args.bases:
        if base.rstrip('/') not in allowed:
            parser.error('Use the configured private dashboard URL or the local engine URL.')
        try:
            measure(base, get_settings().dashboard_password, args.repeats)
        except httpx.HTTPError as error:
            print(type(error).__name__, flush=True)
