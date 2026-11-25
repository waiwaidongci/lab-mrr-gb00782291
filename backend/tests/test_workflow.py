import os
import threading

import pytest

os.environ.setdefault('SEED_DEMO', 'false')


@pytest.fixture()
def client(tmp_path, monkeypatch):
    database = tmp_path / 'test.db'
    monkeypatch.setenv('DATABASE_URL', f'sqlite:///{database}')
    monkeypatch.setenv('ARTIFACTS_DIR', str(tmp_path / 'artifacts'))
    from sqlalchemy import create_engine
    import src.models.database as database_module
    monkeypatch.setattr(database_module, 'engine', create_engine(f'sqlite:///{database}'))
    import src.services.ledger as ledger_service
    monkeypatch.setattr(ledger_service, 'engine', database_module.engine)
    from src.models.tables import metadata
    metadata.create_all(database_module.engine)
    from src.constants.enums import Role
    from src.models.tables import users
    from src.utils.security import protect
    with database_module.engine.begin() as connection:
        for role in Role:
            connection.execute(users.insert().values(username=role.value, role=role.value,
                                                     password=protect(f'{role.value}-demo-24')))
    from fastapi.testclient import TestClient
    from src.main import app
    with TestClient(app) as test_client:
        yield test_client


def login(client, role):
    token = client.post('/login', json={'username': role, 'password': f'{role}-demo-24'}).json()['token']
    return {'Authorization': f'Bearer {token}'}


LEDGER = ('account_id,subscription_id,tier,effective_date,status,mrr_minor\n'
          'CW-01,CW-01-01,基础工位,2026-09-01,active,120000\n'
          'CW-01,CW-01-01,基础工位,2026-10-02,active,130000\n'
          'CW-01,CW-01-01,标准工位,2026-10-08,active,180000\n')
RECEIVER = ('account_id,subscription_id,tier,effective_date,status,mrr_minor\n'
            'CW-01,CW-01-01,标准工位,2026-10-09,active,190000\n')
ORDER = 'from_site,to_site,effective_date,subscription_id\n甲,乙,2026-10-05,CW-01-01\n'


def test_ingest_is_idempotent_and_late_handover_rewrites_snapshot(client):
    headers = login(client, 'analyst')
    first = client.post('/ledger/sites', headers=headers,
                        json={'site': '甲', 'name': 'a.csv', 'csv': LEDGER}).json()
    client.post('/ledger/sites', headers=headers,
                json={'site': '乙', 'name': 'b.csv', 'csv': RECEIVER})
    assert client.get('/ledger', headers=headers).json()['snapshot']['summary']['withheld'] == 1

    order = client.post('/ledger/handovers', headers=headers,
                        json={'name': 'h.csv', 'csv': ORDER}).json()
    assert order['summary']['withdrawn'] == 1

    replay = client.post('/ledger/sites', headers=headers,
                         json={'site': '甲', 'name': 'a.csv', 'csv': LEDGER}).json()
    assert replay['file_created'] is False
    assert replay['snapshot_created'] is False
    assert replay['file_id'] == first['file_id']

    ledger = client.get('/ledger', headers=headers).json()
    results = {(row['来源场地'], row['生效日']): row['采纳结果']
               for row in ledger['entries'] if row['记录类型'] == '生效记录'}
    assert results[('甲', '2026-10-08')] == '撤下越界'
    assert results[('乙', '2026-10-09')] == '已采纳'


def test_concurrent_uploads_never_expose_partial_snapshot(client):
    headers = login(client, 'analyst')
    payloads = [('甲', LEDGER), ('乙', RECEIVER)]
    errors = []

    def upload(site, csv_text):
        try:
            client.post('/ledger/sites', headers=headers,
                        json={'site': site, 'name': f'{site}.csv', 'csv': csv_text})
        except Exception as error:  # pragma: no cover - failure capture
            errors.append(error)

    threads = [threading.Thread(target=upload, args=payload) for payload in payloads]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    snapshot = client.get('/ledger', headers=headers).json()['snapshot']
    assert snapshot['summary']['files'] == 2
    assert snapshot['summary']['site_files'] == 2


def test_handover_validation_rejects_same_site(client):
    headers = login(client, 'analyst')
    bad = 'from_site,to_site,effective_date,subscription_id\n甲,甲,2026-10-05,CW-01-01\n'
    response = client.post('/ledger/handovers', headers=headers, json={'name': 'h.csv', 'csv': bad})
    assert response.status_code == 422
    assert '相同' in response.json()['detail']
