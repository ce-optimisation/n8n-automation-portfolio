from fastapi.testclient import TestClient
from clipengine.web.app import create_app


def test_campaign_versions_and_source_matching(settings):
    app = create_app(settings, start_worker=False)
    source = 'https://example.test/episode'
    db = app.state.core.db
    recording = db.insert('recordings', title='Matching episode', source_url=source, source_type='url', status='ready')
    accepted = db.insert('clips', recording_id=recording, idx=1, start=10, end=25, status='ready', video_path='fixture.mp4')
    rejected = db.insert('clips', recording_id=recording, idx=2, start=10, end=25, status='rejected', video_path='fixture.mp4')
    brief = ('Campaign: Sample\nDuration: 10–20 seconds\n'
             f'Source window: {source} | 00:00:10 | 00:03:00\n'
             'Destination: instagram:fixture\nSubmission deadline: 2099-01-01T00:00:00Z')
    with TestClient(app, follow_redirects=False) as client:
        first = client.post('/campaigns', data={'source_ref':'Fixture', 'brief':brief})
        again = client.post('/campaigns', data={'source_ref':'Fixture', 'brief':brief})
        assert first.status_code == again.status_code == 303
        assert first.headers['location'] == again.headers['location']
        latest = client.post('/campaigns', data={'source_ref':'Fixture', 'brief':brief + '\n<script>alert(1)</script>'})
        valid = client.get(first.headers['location'])
        assert 'Matching episode' in valid.text and 'Rules extracted for review' in valid.text
        assert f'/clips/{accepted}' in valid.text and f'/clips/{rejected}' not in valid.text
        held = client.get(latest.headers['location'])
        assert 'Needs review' in held.text and '<script>alert(1)</script>' not in held.text
        assert first.headers['location'] in held.text
        assert len(app.state.core.db.query('SELECT id FROM campaign_briefs')) == 2
        assert client.get('/campaigns/999').status_code == 404
        endpoint = first.headers['location']
        submission = {'clip_id': accepted, 'url': 'https://example.test/finished-post', 'status': 'approved', 'evidence': 'Fixture approval'}
        assert client.post(endpoint+'/submission', data=submission).status_code == 303
        assert client.post(endpoint+'/submission', data={**submission, 'clip_id': rejected}).status_code == 409
        receipt = {'submission_id': 1, 'reference': 'fixture-receipt', 'currency': 'GBP', 'gross_minor': 1000, 'fee_minor': 120}
        assert client.post(endpoint+'/receipt', data=receipt).status_code == 303
        assert client.post(endpoint+'/receipt', data=receipt).status_code == 303
        assert client.post(endpoint+'/receipt', data={**receipt,'gross_minor':1100}).status_code == 409
        assert 'GBP 880 minor units net' in client.get(endpoint).text
        assert len(db.query('SELECT id FROM campaign_receipts')) == 1



def test_commercial_migration_preserves_existing_recording_and_backup(settings):
    from contextlib import closing
    import sqlite3
    from clipengine.db import Database

    with closing(sqlite3.connect(settings.db_path)) as old, old:
        old.executescript("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT); INSERT INTO meta VALUES ('schema_version','8'); CREATE TABLE legacy_fixture(note TEXT); INSERT INTO legacy_fixture VALUES ('retained');")
    db = Database(settings.db_path)
    assert db.get_kv('schema_version') == '9'
    assert db.one('SELECT note FROM legacy_fixture')['note'] == 'retained'
    assert not db.query('SELECT id FROM campaign_briefs')
    backups = list((settings.data_dir / 'backups').glob('*.sqlite*'))
    assert backups
    with closing(sqlite3.connect(backups[0])) as restored:
        assert restored.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        assert restored.execute('SELECT note FROM legacy_fixture').fetchone() == ('retained',)


def test_agency_and_campaign_routes_use_dashboard_auth(settings):
    settings.dashboard_password = 'fixture-password'
    app = create_app(settings, start_worker=False)
    with TestClient(app, follow_redirects=False) as client:
        for url in ('/campaigns', '/agency', '/agency/export'):
            assert client.get(url).headers['location'] == '/login'
        assert client.post('/login', data={'password': 'fixture-password'}).status_code == 303
        for url in ('/campaigns', '/agency', '/agency/export'):
            assert client.get(url).status_code == 200
        assert client.post('/agency/import', data={'research':'[]'}, headers={'Origin':'https://foreign.test'}).status_code == 403


def test_customer_workspace_starts_empty_and_supports_own_brand(settings):
    settings.workspace_id = 'customer-demo'
    app = create_app(settings, start_worker=False)
    core = app.state.core
    assert not core.db.query('SELECT key FROM brands')
    assert not core.db.query('SELECT id FROM creators')
    assert not core.db.query('SELECT id FROM social_accounts')
    assert core.db.get_kv('recording.enabled') == '0'
    assert core.db.get_kv('delivery.enabled') == '0'
    niche = core.db.one('SELECT key FROM niches ORDER BY key')['key']
    with TestClient(app, follow_redirects=False) as client:
        assert client.post('/creators/brand', data={'key':'studio-demo','name':'Demo Studio','niche':niche}).status_code == 303
        creator = client.post('/creators', data={'name':'Demo Creator'})
        assert creator.status_code == 303
        assert 'Demo Studio' in client.get(creator.headers['location']).text
        assert client.post(creator.headers['location'].split('?')[0], data={'brand_key':'studio-demo'}).status_code == 303
        assert core.db.one('SELECT brand_key FROM creators')['brand_key'] == 'studio-demo'
        assert 'Demo Studio' in client.get('/accounts').text
