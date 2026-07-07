"""Server boots, endpoints respond, static UI is served (no network needed —
ingestor loops fail gracefully in the sandbox and log to latency_log)."""
from fastapi.testclient import TestClient


def test_boot_and_endpoints():
    from polyweather.api.app import app
    with TestClient(app) as client:
        r = client.get("/api/cities")
        assert r.status_code == 200
        keys = {c["key"] for c in r.json()}
        assert keys == {"london", "amsterdam", "tokyo"}
        for c in r.json():
            assert c["phase"] in ("NIGHT", "MORNING", "PEAK", "LOCKIN", "SETTLED")

        r = client.get("/api/state/tokyo")
        assert r.status_code == 200
        body = r.json()
        assert body["city"] == "tokyo"
        assert "pricing" in body and "market" in body

        assert client.get("/api/state/nope").status_code == 404
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/eval").status_code == 200

        r = client.get("/")
        assert r.status_code == 200 and "polyweather" in r.text
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/static/vendor/echarts.min.js").status_code == 200
