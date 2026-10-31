"""Live checks (real network calls, no mocks). Run: pytest -v"""
import os, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "backend"))
from fastapi.testclient import TestClient
import main

c = TestClient(main.app)

def test_health():
    assert c.get("/api/health").json()["ok"]

def test_unknown_place_is_friendly_404():
    r = c.get("/api/weather", params={"place": "zzqxqxqxjjkk"})
    assert r.status_code == 404 and "not found" in r.json()["detail"].lower()

def test_live_weather_changes_with_location():
    a, b = c.get("/api/weather", params={"place": "Kanpur"}), c.get("/api/weather", params={"place": "Shimla"})
    assert a.status_code == 200 and b.status_code == 200
    assert len(a.json()["days"]) == 7 and a.json()["days"] != b.json()["days"]
    assert a.json()["fetched_at"]

def test_weather_failure_shows_error_not_numbers(monkeypatch):
    monkeypatch.setattr(main, "get_json", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("blocked")))
    out = main.tool_weather(26.4, 80.3)
    assert "error" in out and "days" not in out

def test_prices_live():
    if not os.getenv("DATA_GOV_IN_API_KEY"):
        import pytest; pytest.skip("DATA_GOV_IN_API_KEY not set")
    r = c.get("/api/prices", params={"state": "Uttar Pradesh", "commodity": "Potato", "district": "Kanpur Nagar"})
    assert r.status_code in (200, 503)  # 503 = clear "no data" message
    if r.status_code == 200:
        assert r.json()["top_markets_latest_date"] and r.json()["source"]

def test_rejects_bad_language():
    r = c.post("/api/chat", json={"messages": [{"role": "user", "content": "hi"}], "language": "fr", "location": "Kanpur", "crop": "Wheat"})
    assert r.status_code == 422
