"""Campaign brief intake and version history inside the editorial workspace."""
import json
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from commercial_intelligence.interpret_brief import interpret


def register(app, db, page):
    @app.get('/campaigns')
    def campaigns(request: Request):
        rows = db.query('SELECT id, source_ref, source_sha256, created_at, interpretation '
                        'FROM campaign_briefs ORDER BY id DESC LIMIT 100')
        totals = db.query('SELECT currency, SUM(gross_minor-fee_minor) AS net_minor '
                          'FROM campaign_receipts GROUP BY currency ORDER BY currency')
        return page(request, 'campaigns.html', rows=[(r, json.loads(r['interpretation'])) for r in rows], totals=totals)

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
                rows = db.query("SELECT id, start, end, score FROM clips WHERE recording_id=? "
                                "AND status IN ('ready','approved') AND safety_status='clear' AND video_path IS NOT NULL "
                                'AND ROUND(start*1000)>=? AND (? IS NULL OR ROUND(end*1000)<=?) '
                                'AND ROUND(end*1000)-ROUND(start*1000) BETWEEN ? AND ? ORDER BY score DESC LIMIT 20',
                                (recording['id'], window['start_ms'], window['end_ms'], window['end_ms'],
                                 rules['minimum_duration_ms'], rules['maximum_duration_ms']))
                candidates.update((clip['id'], clip) for clip in rows)
        ranked = sorted(candidates.values(), key=lambda clip: (-(clip['score'] or 0), clip['id']))[:20]
        submissions = db.query('SELECT * FROM campaign_submissions WHERE brief_id=? ORDER BY id DESC', (brief_id,))
        receipts = db.query('SELECT r.* FROM campaign_receipts r JOIN campaign_submissions s ON s.id=r.submission_id '
                            'WHERE s.brief_id=? ORDER BY r.id DESC', (brief_id,))
        return page(request, 'campaigns.html', brief=row, result=result, versions=versions, matches=matches,
                    expired=expired, candidates=[] if expired else ranked, submissions=submissions, receipts=receipts)

    @app.post('/campaigns')
    def import_campaign(source_ref: str = Form(...), brief: str = Form(...)):
        source_ref = source_ref.strip()
        raw = brief.encode('utf-8')
        if not source_ref or len(source_ref) > 500 or not brief.strip() or len(raw) > 65_536:
            raise HTTPException(422, 'Provide a source reference of up to 500 characters and a brief of up to 64 KiB.')
        result = interpret(raw, source_ref=source_ref)
        if len(result['evidence']['Source window']) > 30:
            raise HTTPException(422, 'A brief may contain up to 30 source windows.')
        digest = result['source_sha256']
        db.execute('INSERT INTO campaign_briefs (source_ref,source_sha256,original_text,interpretation,created_at) '
                   'VALUES (?,?,?,?,?) ON CONFLICT(source_ref,source_sha256) DO NOTHING',
                   (source_ref, digest, brief, json.dumps(result), time.time()))
        row = db.one('SELECT id FROM campaign_briefs WHERE source_ref=? AND source_sha256=?', (source_ref, digest))
        return RedirectResponse(f"/campaigns/{row['id']}", status_code=303)

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
