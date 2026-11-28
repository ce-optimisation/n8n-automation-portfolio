"""Render an isolated, private campaign preview using the ordinary clip renderer."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile

from . import captions, media, render, storage
from .jobs import PermanentError
from .pipeline import all_words


PROFILES = {"vertical_720": (720, 1280), "vertical_1080": (1080, 1920)}


def eligible(rules, recording, clip):
    start, end = round(clip['start'] * 1000), round(clip['end'] * 1000)
    return (
        clip['status'] in ('ready', 'approved') and clip['safety_status'] == 'clear'
        and rules['minimum_duration_ms'] <= end - start <= min(rules['maximum_duration_ms'], 180_000)
        and any(window['source_id'] in (recording['source_url'], recording['sha256'])
                and window['start_ms'] <= start and (window['end_ms'] is None or end <= window['end_ms'])
                for window in rules['source_windows'])
    )


def create_preview(settings, db, recording, clip, target, profile, captions_on, progress=None):
    source = Path(recording['file_path'] or '')
    if not source.is_file():
        raise PermanentError('The original recording is unavailable for this preview.')
    duration = clip['end'] - clip['start']
    storage.require_space(settings, duration * storage.CLIP_BYTES_PER_S * 1.3, 'rendering a campaign preview')
    width, height = PROFILES[profile]
    dimensions = media.probe(source)
    creator = db.one('SELECT brand_key FROM creators WHERE id=?', (recording['creator_id'],)) \
        if recording['creator_id'] else None
    brand = creator['brand_key'] if creator else None
    if captions_on and captions.captions_blocked_by_policy(db, brand):
        raise PermanentError(f'This campaign asks for captions, but generated captions are off for {brand} by owner '
                             'policy. Approve a caption exception for this brand (with an accuracy review) first.')
    style = captions.style_for(db, None)
    source_settings = json.loads(recording['settings_json'] or '{}')
    style = replace(style, enabled=captions_on and not source_settings.get('source_has_burned_in_captions', False))
    preview_settings = replace(settings, out_width=width, out_height=height, keep_title_free_master=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.campaign-render-', dir=target.parent) as scratch:
        output = render.render_clip(preview_settings, source, Path(scratch), clip['start'], clip['end'],
                                    all_words(db.segments(recording['id'])), dimensions['width'], dimensions['height'],
                                    None, style=style, on_progress=progress)
        info = output['probe']
        if (info['width'], info['height'], info['video_codec'], info['audio_codec']) != (width, height, 'h264', 'aac'):
            raise PermanentError('Preview output does not match its selected profile.')
        if abs(info['duration'] - duration) > 0.20:
            raise PermanentError('Preview duration does not match its reviewed excerpt.')
        output['video'].replace(target)
