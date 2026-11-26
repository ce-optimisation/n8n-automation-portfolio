"""Campaign brief intake and version history inside the editorial workspace."""
import hashlib
import json
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse

from commercial_intelligence.assess import assess
from commercial_intelligence.brief_engine import analyse
from .. import campaign_render
from ..jobs import PermanentError
from ..publishing.base import RIGHTS_OK


def register(app, core, page):
    db, queue, settings = core.db, core.queue, core.settings

    def preview_target(job_id):
        return settings.data_dir / 'campaign_previews' / str(job_id) / 'preview.mp4'

    def preview_input(brief_id, clip_id):
        brief = db.one('SELECT interpretation FROM campaign_briefs WHERE id=?', (brief_id,))
        clip = db.clip(clip_id)
        recording = db.recording(clip['recording_id']) if clip else None
        rules = json.loads(brief['interpretation']).get('selection_rules') if brief else None
        if (not rules or not recording or clip['status'] != 'approved' or clip['rights_status'] not in RIGHTS_OK
                or datetime.now(timezone.utc) >= datetime.fromisoformat(rules['submission_deadline'])
                or not campaign_render.eligible(rules, recording, clip)):
            raise PermanentError('The brief, clip, recorded rights or source window does not permit this preview.')
        return recording, clip

    def render_preview(context):
        params = context.payload
        recording, clip = preview_input(params['brief_id'], context.job['clip_id'])
        if clip['render_rev'] != params['render_rev'] or params['profile'] not in campaign_render.PROFILES:
            raise PermanentError('The requested clip revision or profile changed.')
        target = preview_target(context.id)
        if not target.is_file():
            campaign_render.create_preview(settings, db, recording, clip, target, params['profile'],
                                           params['captions_on'], context.progress)

    queue.register('campaign_preview', render_preview)

    @app.post('/campaigns/{brief_id}/preview')
    def queue_preview(brief_id: int, clip_id: int = Form(...), profile: str = Form(...), captions_on: bool = Form(False)):
        if profile not in campaign_render.PROFILES:
            raise HTTPException(422, 'Choose a supported preview profile.')
        try:
            recording, clip = preview_input(brief_id, clip_id)
        except PermanentError as error:
            raise HTTPException(422, str(error)) from error
        revision = clip['render_rev']
        key = f'campaign-preview:{brief_id}:{clip_id}:{revision}:{profile}:{int(captions_on)}'
        previous = db.one("SELECT id FROM jobs WHERE dedupe_key=? AND status='done' ORDER BY id DESC LIMIT 1", (key,))
        if not previous or not preview_target(previous['id']).is_file():
            queue.enqueue('campaign_preview', clip_id=clip_id, recording_id=recording['id'], dedupe_key=key,
                          payload={'brief_id': brief_id, 'render_rev': revision, 'profile': profile,
                                   'captions_on': captions_on}, max_attempts=1)
        return RedirectResponse(f'/campaigns/{brief_id}', status_code=303)

    @app.get('/campaigns/preview/{job_id}/video')
    def preview_video(job_id: int):
        job = db.one("SELECT clip_id,payload_json FROM jobs WHERE id=? AND kind='campaign_preview' AND status='done'", (job_id,))
        if not job or not preview_target(job_id).is_file():
            raise HTTPException(404)
        try:
            _, clip = preview_input(json.loads(job['payload_json'])['brief_id'], job['clip_id'])
        except PermanentError as error:
            raise HTTPException(404) from error
        if clip['render_rev'] != json.loads(job['payload_json'])['render_rev']:
            raise HTTPException(404)
        return FileResponse(preview_target(job_id), media_type='video/mp4', headers={'Cache-Control': 'no-store'})
    @app.get('/campaigns')
    def campaigns(request: Request):
        rows = db.query('SELECT id, source_ref, source_sha256, created_at, interpretation '
                        'FROM campaign_briefs ORDER BY id DESC LIMIT 100')
        totals = db.query('SELECT currency, SUM(gross_minor-fee_minor) AS net_minor '
                          'FROM campaign_receipts GROUP BY currency ORDER BY currency')
        outcomes = db.query('SELECT status, COUNT(*) AS total FROM campaign_submissions GROUP BY status ORDER BY status')
        attributed = db.query('SELECT r.currency, s.clip_id, COALESCE(cr.name, \'Unattributed\') AS creator, '
                              'COALESCE(b.name, \'Unattributed\') AS brand, '
                              'SUM(r.gross_minor-r.fee_minor) AS net_minor '
                              'FROM campaign_receipts r JOIN campaign_submissions s ON s.id=r.submission_id '
                              'LEFT JOIN clips c ON c.id=s.clip_id LEFT JOIN recordings rec ON rec.id=c.recording_id '
                              'LEFT JOIN creators cr ON cr.id=rec.creator_id LEFT JOIN brands b ON b.key=cr.brand_key '
                              'GROUP BY r.currency,s.clip_id,cr.name,b.name ORDER BY r.currency,net_minor DESC LIMIT 50')
        return page(request, 'campaigns.html', rows=[(r, json.loads(r['interpretation'])) for r in rows],
                    totals=totals, outcomes=outcomes, attributed=attributed)

    @app.get('/campaigns/{brief_id}')
    def campaign(request: Request, brief_id: int):
        row = db.one('SELECT * FROM campaign_briefs WHERE id=?', (brief_id,))
        if row is None:
            raise HTTPException(404)
        versions = db.query('SELECT id, source_sha256 FROM campaign_briefs WHERE source_ref=? ORDER BY id DESC',
                            (row['source_ref'],))
        result = json.loads(row['interpretation'])
        matches = {}
        rules = result.get('selection_rules') or {}
        expired = bool(rules) and datetime.now(timezone.utc) >= datetime.fromisoformat(rules['submission_deadline'])
        candidates = {}
        for window in rules.get('source_windows', []):
            source = window['source_id']
            if source not in matches:
                matches[source] = db.query('SELECT id, title FROM recordings WHERE source_url=? OR sha256=? '
                                           'ORDER BY id DESC LIMIT 20', (source, source))
            for recording in (() if expired else matches[source]):
                rows = db.query("SELECT id, start, end, score, status, rights_status FROM clips WHERE recording_id=? "
                                "AND status IN ('ready','approved') AND safety_status='clear' AND video_path IS NOT NULL "
                                'AND ROUND(start*1000)>=? AND (? IS NULL OR ROUND(end*1000)<=?) '
                                'AND ROUND(end*1000)-ROUND(start*1000) BETWEEN ? AND ? ORDER BY score DESC LIMIT 20',
                                (recording['id'], window['start_ms'], window['end_ms'], window['end_ms'],
                                 rules['minimum_duration_ms'], rules['maximum_duration_ms']))
                candidates.update((clip['id'], clip) for clip in rows)
        ranked = sorted(candidates.values(), key=lambda clip: (-(clip['score'] or 0), clip['id']))[:20]
        preview_candidates = [clip for clip in ranked if clip['status'] == 'approved' and clip['rights_status'] in RIGHTS_OK]
        submissions = db.query('SELECT * FROM campaign_submissions WHERE brief_id=? ORDER BY id DESC', (brief_id,))
        receipts = db.query('SELECT r.* FROM campaign_receipts r JOIN campaign_submissions s ON s.id=r.submission_id '
                            'WHERE s.brief_id=? ORDER BY r.id DESC', (brief_id,))
        latest = db.one('SELECT evaluation FROM campaign_assessments WHERE brief_id=? ORDER BY id DESC LIMIT 1',
                        (brief_id,))
        previews = db.query("SELECT id,clip_id,status,message FROM jobs WHERE kind='campaign_preview' "
                            "AND json_extract(payload_json,'$.brief_id')=? ORDER BY id DESC LIMIT 20", (brief_id,))
        return page(request, 'campaigns.html', brief=row, result=result, versions=versions, matches=matches,
                    expired=expired, candidates=[] if expired else ranked, submissions=submissions, receipts=receipts,
                    assessment=json.loads(latest['evaluation']) if latest else None, previews=previews,
                    preview_candidates=[] if expired else preview_candidates)

    @app.post('/campaigns')
    def import_campaign(source_ref: str = Form(...), brief: str = Form(...)):
        source_ref = source_ref.strip()
        raw = brief.encode('utf-8')
        if not source_ref or len(source_ref) > 500 or not brief.strip() or len(raw) > 65_536:
            raise HTTPException(422, 'Provide a source reference of up to 500 characters and a brief of up to 64 KiB.')
        result = analyse(raw, source_ref=source_ref, reference_year=datetime.now(timezone.utc).year,
                         observed_at=datetime.now(timezone.utc).isoformat())
        windows = (result.get('selection_rules') or result.get('proposed_selection_rules') or {}).get('source_windows', [])
        if len(result['evidence']['Source window']) > 30 or len(windows) > 30:
            raise HTTPException(422, 'A brief may contain up to 30 source windows.')
        digest = result['source_sha256']
        db.execute('INSERT INTO campaign_briefs (source_ref,source_sha256,original_text,interpretation,created_at) '
                   'VALUES (?,?,?,?,?) ON CONFLICT(source_ref,source_sha256) DO NOTHING',
                   (source_ref, digest, brief, json.dumps(result), time.time()))
        row = db.one('SELECT id FROM campaign_briefs WHERE source_ref=? AND source_sha256=?', (source_ref, digest))
        return RedirectResponse(f"/campaigns/{row['id']}", status_code=303)

    @app.post('/campaigns/{brief_id}/confirm-rules')
    def confirm_rules(brief_id: int, confirm: bool = Form(False)):
        """Owner confirms rules proposed from a free-form brief. Never authorises publication."""
        row = db.one('SELECT interpretation FROM campaign_briefs WHERE id=?', (brief_id,))
        if row is None:
            raise HTTPException(404)
        result = json.loads(row['interpretation'])
        proposed = result.get('proposed_selection_rules')
        if not confirm:
            raise HTTPException(422, 'Tick the confirmation after checking the rules against the original brief.')
        if result.get('selection_rules'):
            raise HTTPException(409, 'This brief version already has confirmed rules.')
        if not proposed or not proposed.get('submission_deadline') or not proposed.get('source_windows'):
            raise HTTPException(422, 'Rules need a stated deadline and at least one source before confirmation.')
        if result.get('injection_flags'):
            raise HTTPException(422, 'This brief contains flagged text. Import a cleaned copy from the original source.')
        if any(h.get('field') in ('duration', 'submission_deadline') for h in result.get('holds', [])):
            raise HTTPException(422, 'Resolve duration and deadline holds before confirming.')
        rules = {k: v for k, v in proposed.items() if k != 'requires_owner_confirmation'}
        rules['evidence'] = f"{result['source_ref']}#sha256={result['source_sha256']}"
        rules['confirmed_by_owner_at'] = datetime.now(timezone.utc).isoformat()
        result['selection_rules'] = rules
        result['publication_authorized'] = False
        db.execute('UPDATE campaign_briefs SET interpretation=? WHERE id=?', (json.dumps(result), brief_id))
        return RedirectResponse(f'/campaigns/{brief_id}', status_code=303)

    @app.post('/campaigns/{brief_id}/assessment')
    def record_assessment(brief_id: int, details: str = Form(...)):
        row = db.one('SELECT interpretation FROM campaign_briefs WHERE id=?', (brief_id,))
        if row is None:
            raise HTTPException(404)
        try:
            if len(details.encode('utf-8')) > 16_384:
                raise ValueError('Assessment must be up to 16 KiB.')
            payload = json.loads(details)
            if not isinstance(payload, dict):
                raise ValueError('Enter a structured assessment object.')
            campaign = json.loads(row['interpretation']).get('campaign')
            if not campaign or payload.get('campaign') != campaign:
                raise ValueError('Complete the brief interpretation and use its exact campaign name.')
            evaluation = assess(payload)
            canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'))
        except (ValueError, RecursionError) as error:
            raise HTTPException(422, str(error)) from error
        db.execute('INSERT INTO campaign_assessments (brief_id,input_sha256,input_json,evaluation,created_at) '
                   'VALUES (?,?,?,?,?) ON CONFLICT(brief_id,input_sha256) DO NOTHING',
                   (brief_id, hashlib.sha256(canonical.encode()).hexdigest(), canonical, json.dumps(evaluation), time.time()))
        return RedirectResponse(f'/campaigns/{brief_id}', status_code=303)

    @app.post('/campaigns/{brief_id}/submission')
    def record_submission(brief_id: int, clip_id: int = Form(...), url: str = Form(...),
                          status: str = Form(...), evidence: str = Form(...)):
        if not db.one('SELECT id FROM campaign_briefs WHERE id=?', (brief_id,)) or not db.clip(clip_id):
            raise HTTPException(404)
        url, evidence = url.strip(), evidence.strip()
        try:
            parsed = urlsplit(url)
        except ValueError as error:
            raise HTTPException(422, 'Enter an HTTPS publication URL.') from error
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None or len(url) > 1500
                or status not in {'submitted', 'approved', 'rejected'} or not 0 < len(evidence) <= 2000):
            raise HTTPException(422, 'Enter an HTTPS publication URL, a valid status and evidence.')
        db.execute('INSERT INTO campaign_submissions (brief_id,clip_id,url,status,evidence,updated_at) VALUES (?,?,?,?,?,?) '
                   'ON CONFLICT(brief_id,url) DO UPDATE SET status=excluded.status,evidence=excluded.evidence,'
                   'updated_at=excluded.updated_at WHERE campaign_submissions.clip_id=excluded.clip_id',
                   (brief_id, clip_id, url, status, evidence, time.time()))
        stored = db.one('SELECT clip_id FROM campaign_submissions WHERE brief_id=? AND url=?', (brief_id, url))
        if stored['clip_id'] != clip_id:
            raise HTTPException(409, 'This URL is already linked to a different clip.')
        return RedirectResponse(f'/campaigns/{brief_id}', status_code=303)

    @app.post('/campaigns/{brief_id}/receipt')
    def record_receipt(brief_id: int, submission_id: int = Form(...), reference: str = Form(...),
                       currency: str = Form(...), gross_minor: int = Form(...), fee_minor: int = Form(...)):
        if not db.one('SELECT id FROM campaign_submissions WHERE id=? AND brief_id=?', (submission_id, brief_id)):
            raise HTTPException(404)
        reference, currency = reference.strip(), currency.strip().upper()
        if (not 0 < len(reference) <= 200 or len(currency) != 3 or not currency.isascii() or not currency.isalpha()
                or not 0 <= fee_minor <= gross_minor <= 10**12 or gross_minor == 0):
            raise HTTPException(422, 'Enter a payment reference, currency and valid amounts in minor units.')
        db.execute('INSERT INTO campaign_receipts (submission_id,reference,currency,gross_minor,fee_minor,recorded_at) '
                   'VALUES (?,?,?,?,?,?) ON CONFLICT(currency,reference) DO NOTHING',
                   (submission_id, reference, currency, gross_minor, fee_minor, time.time()))
        stored = db.one('SELECT submission_id,gross_minor,fee_minor FROM campaign_receipts WHERE currency=? AND reference=?',
                        (currency, reference))
        if tuple(stored) != (submission_id, gross_minor, fee_minor):
            raise HTTPException(409, 'This payment reference is already recorded with different details.')
        return RedirectResponse(f'/campaigns/{brief_id}', status_code=303)
