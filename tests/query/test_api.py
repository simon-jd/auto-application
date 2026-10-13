from fastapi.testclient import TestClient
from sqlalchemy import insert

from auto_app.core.db import tables as T
from auto_app.core.db.store import DbStore
from auto_app.settings import Paths


def test_read_api_returns_pages_details_and_errors(tmp_path):
    from auto_app.query.api import create_app

    path = tmp_path / "jobs.db"
    store = DbStore(path, Paths(home=tmp_path))
    with store.engine.begin() as conn:
        conn.execute(insert(T.jobs), [{"job_id": 1, "company_norm": "acme"},
                                    {"job_id": 2, "company_norm": "other"}])
    store.engine.dispose()
    with TestClient(create_app(path), base_url='http://127.0.0.1:8766') as client:
        response = client.get("/api/context")
        assert response.status_code == 200
        assert "counts" in response.json()
        page = client.get("/api/jobs", params={"limit": 1}).json()
        assert page["total"] == 2 and page["returned"] == 1
        second = client.get("/api/jobs", params={"limit": 1, "cursor": page["next_cursor"]}).json()
        assert [j["job_id"] for j in second["items"]] == [1]
        assert client.get("/api/jobs/1").json()["job_id"] == 1
        assert client.get("/api/jobs/999").status_code == 404
        assert client.get("/api/jobs", params={"cursor": "invalid"}).status_code == 400
        assert client.get("/api/jobs", params={"limit": 0}).status_code == 422
        assert client.post("/api/jobs/1", json={"decision": "applied"}).status_code == 405


def test_read_api_missing_db_does_not_create_it(tmp_path):
    from auto_app.query.api import create_app

    path = tmp_path / "missing" / "jobs.db"
    with TestClient(create_app(path), base_url='http://127.0.0.1:8766') as client:
        assert client.get("/api/context").status_code == 503
    assert not path.parent.exists()


def test_read_api_rejects_rebinding_host_and_cross_origin(tmp_path):
    from auto_app.query.api import create_app
    path = tmp_path / 'jobs.db'
    db = DbStore(path, Paths(home=tmp_path))
    with db.engine.begin() as conn:
        conn.execute(insert(T.jobs).values(job_id=1))
    db.engine.dispose()
    with TestClient(create_app(path), base_url='http://127.0.0.1:8766') as client:
        for route in ('/api/context', '/api/jobs?archive=all&include_excluded=true', '/api/jobs/1'):
            assert client.get(route, headers={'host': 'attacker.example:8766',
                                              'origin': 'http://attacker.example:8766'}).status_code == 403
            assert client.get(route, headers={'origin': 'https://attacker.example'}).status_code == 403
            assert client.get(route, headers={'origin': 'null'}).status_code == 403
            assert client.get(route, headers={'origin': 'http://127.0.0.1:8766'}).status_code == 200


def test_read_api_custom_port_and_explicit_private_host(tmp_path):
    from auto_app.query.api import create_app
    path = tmp_path / 'jobs.db'
    DbStore(path, Paths(home=tmp_path)).engine.dispose()
    with TestClient(create_app(path, port=9123), base_url='http://localhost:9123') as client:
        assert client.get('/api/context').status_code == 200
        assert client.get('/api/context', headers={'host': 'localhost:8766'}).status_code == 403
    with TestClient(create_app(path, allowed_hosts={'private.example:9123'}),
                    base_url='http://private.example:9123') as client:
        assert client.get('/api/context', headers={'origin': 'http://private.example:9123'}).status_code == 200
        assert client.get('/api/context', headers={'host': 'attacker.example:9123'}).status_code == 403
