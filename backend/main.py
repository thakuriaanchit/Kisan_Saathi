"""Kisan Saathi backend. All numbers come from live API calls made at request time."""
import os, json, time, datetime as dt
from pathlib import Path
from collections import defaultdict
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

load_dotenv()
MODEL = os.getenv("LLM_MODEL", "claude-sonnet-4-6")
MANDI_RESOURCE = os.getenv("MANDI_RESOURCE_ID", "9ef84268-d588-465a-a308-a864a43d0070")
MANDI_URL = f"https://api.data.gov.in/resource/{MANDI_RESOURCE}"
HTTP = httpx.Client(timeout=12)
app = FastAPI(title="Kisan Saathi")


def now_str():
    return dt.datetime.now().strftime("%d %b %Y, %I:%M %p")


def get_json(url, params=None, retries=1):
    last = None
    for _ in range(retries + 1):
        try:
            r = HTTP.get(url, params=params)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa
            last = e
            time.sleep(0.5)
    raise RuntimeError(str(last))


# ---------------- Tools (each returns real data or {"error": ...}) ----------------
def tool_geocode(place: str, **_):
    try:
        d = get_json("https://geocoding-api.open-meteo.com/v1/search",
                     {"name": place, "count": 5, "language": "en", "format": "json"})
    except Exception as e:
        return {"error": f"Location service unavailable: {e}", "missing": "location"}
    res = d.get("results") or []
    if not res:
        return {"error": "Place not found. Ask the user to try a nearby town or district name.",
                "missing": "location"}
    return {"matches": [{"name": r["name"], "district": r.get("admin2"), "state": r.get("admin1"),
                         "country": r.get("country"), "lat": r["latitude"], "lon": r["longitude"]}
                        for r in res],
            "source": "Open-Meteo Geocoding", "fetched_at": now_str()}


def tool_weather(lat: float, lon: float, **_):
    try:
        d = get_json("https://api.open-meteo.com/v1/forecast", {
            "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 7,
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,"
                     "precipitation_probability_max,wind_speed_10m_max,relative_humidity_2m_mean"})
    except Exception as e:
        return {"error": "Weather data is not available right now. Please try again in a few minutes.",
                "missing": "weather", "detail": str(e)}
    x = d["daily"]
    days = [{"date": x["time"][i], "tmax_c": x["temperature_2m_max"][i], "tmin_c": x["temperature_2m_min"][i],
             "rain_mm": x["precipitation_sum"][i], "rain_prob_pct": x["precipitation_probability_max"][i],
             "wind_kmh": x["wind_speed_10m_max"][i], "humidity_pct": x["relative_humidity_2m_mean"][i],
             "code": x["weather_code"][i]} for i in range(len(x["time"]))]
    return {"days": days, "source": "Open-Meteo forecast", "fetched_at": now_str()}


def tool_mandi(state: str, commodity: str, district: str = "", **_):
    key = os.getenv("DATA_GOV_IN_API_KEY")
    if not key:
        return {"error": "Mandi price API key is not configured.", "missing": "prices"}
    base = {"api-key": key, "format": "json", "limit": 500, "filters[commodity]": commodity}
    for scope, extra in (("district", {"filters[state]": state, "filters[district]": district}),
                         ("state", {"filters[state]": state})):
        if scope == "district" and not district:
            continue
        try:
            recs = get_json(MANDI_URL, {**base, **extra}).get("records", [])
        except Exception as e:
            return {"error": f"Mandi price service unavailable: {e}", "missing": "prices"}
        if recs:
            break
    else:
        return {"error": f"No mandi price found for {commodity} in {district or state}.", "missing": "prices"}
    by_date = defaultdict(list)
    for r in recs:
        try:
            d = dt.datetime.strptime(r["arrival_date"], "%d/%m/%Y").date()
            by_date[d].append({"market": r["market"], "district": r["district"], "state": r["state"],
                               "modal": float(r["modal_price"]), "min": float(r["min_price"]),
                               "max": float(r["max_price"])})
        except (KeyError, ValueError):
            continue
    if not by_date:
        return {"error": "Price records were unreadable.", "missing": "prices"}
    dates = sorted(by_date)
    latest = dates[-1]
    best = sorted(by_date[latest], key=lambda m: -m["modal"])[:5]
    trend = [{"date": d.isoformat(), "avg_modal_rs_per_quintal": round(sum(m["modal"] for m in by_date[d]) / len(by_date[d]), 1),
              "markets": len(by_date[d])} for d in dates[-7:]]
    prior = [t["avg_modal_rs_per_quintal"] for t in trend[:-1]]
    return {"commodity": commodity, "searched_scope": scope, "latest_date": latest.isoformat(),
            "top_markets_latest_date": best, "trend": trend,
            "latest_avg": trend[-1]["avg_modal_rs_per_quintal"],
            "earlier_days_avg": round(sum(prior) / len(prior), 1) if prior else None,
            "note": "Prices are Rs/quintal. Trend has only the dates the API returned.",
            "source": "Data.gov.in Agmarknet", "fetched_at": now_str()}


def tool_soil(lat: float, lon: float, **_):
    try:
        d = get_json("https://rest.isric.org/soilgrids/v2.0/properties/query",
                     {"lat": lat, "lon": lon, "property": ["phh2o", "clay", "sand"],
                      "depth": "0-5cm", "value": "mean"}, retries=0)
        out = {}
        for l in d["properties"]["layers"]:
            v = l["depths"][0]["values"]["mean"]
            out[l["name"]] = None if v is None else round(v / l["unit_measure"]["d_factor"], 1)
        return {"soil_0_5cm": out, "units": "pH; clay/sand in %", "source": "ISRIC SoilGrids", "fetched_at": now_str()}
    except Exception as e:
        return {"error": "Soil data is not available right now.", "missing": "soil", "detail": str(e)}


TOOLS = {"geocode": tool_geocode, "get_weather": tool_weather, "get_mandi_prices": tool_mandi, "get_soil": tool_soil}
TOOL_SCHEMAS = [
    {"name": "geocode", "description": "Convert a place name to lat/lon/district/state.",
     "input_schema": {"type": "object", "properties": {"place": {"type": "string"}}, "required": ["place"]}},
    {"name": "get_weather", "description": "Live 7-day forecast for coordinates.",
     "input_schema": {"type": "object", "properties": {"lat": {"type": "number"}, "lon": {"type": "number"}}, "required": ["lat", "lon"]}},
    {"name": "get_mandi_prices", "description": "Live mandi prices (Rs/quintal). Use English state/district/commodity names, e.g. Uttar Pradesh, Kanpur Nagar, Potato.",
     "input_schema": {"type": "object", "properties": {"state": {"type": "string"}, "district": {"type": "string"}, "commodity": {"type": "string"}}, "required": ["state", "commodity"]}},
    {"name": "get_soil", "description": "Live soil pH/clay/sand for coordinates.",
     "input_schema": {"type": "object", "properties": {"lat": {"type": "number"}, "lon": {"type": "number"}}, "required": ["lat", "lon"]}},
]

SYSTEM = """You are Kisan Saathi, an assistant for Indian farmers. Reply ONLY in {lang}, in very short, simple sentences.
Farmer context: location="{loc}", crop="{crop}", sowing/growth stage="{stage}".
RULES:
- Every weather, price or soil number must come from tool results in this conversation. Never invent or recall numbers. Call geocode first, then the tools needed (weather for farm actions; get_mandi_prices for selling; soil only if relevant).
- If a tool returns an error, say plainly which data is missing. Do not guess.
- Weather guidance: rain within 24h -> no spraying/fertiliser, delay irrigation; wind >30 km/h -> no spraying; >40C or <4C -> heat/frost protection tips; rain in harvest week -> harvest early or cover produce.
- Price guidance: prefer same district/state. If latest_avg > earlier_days_avg suggest sell now; if lower, suggest waiting only if the crop stores safely. Name the best market and its modal price per quintal with the date.
- Never give pesticide/medicine doses; for pests/diseases suggest the local Krishi Vigyan Kendra or Kisan Call Centre 1800-180-1551.
- If the question is unrelated to farming, politely say you help with weather, crop actions and mandi prices.
- Format: first line "Summary: ...". Then "Do this week:" with lines "- Day: action", then "Don't:" lines. Then, only if price data was fetched, "Sell or wait:" lines. End with: "This is a suggestion, not a guarantee."
- Mention the data source name next to numbers."""


class Msg(BaseModel):
    role: str
    content: str


class ChatReq(BaseModel):
    messages: list[Msg] = Field(min_length=1, max_length=20)
    language: str = Field(pattern="^(hi|en)$")
    location: str = Field(min_length=2, max_length=80)
    crop: str = Field(min_length=2, max_length=40)
    stage: str = Field(default="", max_length=60)


def derive_warnings(days, lang):
    hi = lang == "hi"
    w = []
    for d in days[:3]:
        if (d["rain_mm"] or 0) >= 30:
            w.append(f"{d['date']}: " + ("भारी बारिश की संभावना" if hi else "Heavy rain expected"))
        if (d["tmax_c"] or 0) >= 40:
            w.append(f"{d['date']}: " + ("लू / भीषण गर्मी" if hi else "Heatwave"))
        if (d["tmin_c"] if d["tmin_c"] is not None else 99) <= 4:
            w.append(f"{d['date']}: " + ("पाला / ठंड" if hi else "Frost risk"))
        if (d["wind_kmh"] or 0) >= 30:
            w.append(f"{d['date']}: " + ("तेज हवा" if hi else "Strong wind"))
    return w


@app.get("/api/health")
def health():
    return {"ok": True, "llm_key": bool(os.getenv("ANTHROPIC_API_KEY")), "mandi_key": bool(os.getenv("DATA_GOV_IN_API_KEY"))}


@app.get("/api/weather")
def weather(place: str):
    g = tool_geocode(place)
    if "error" in g:
        raise HTTPException(404, g["error"])
    m = g["matches"][0]
    w = tool_weather(m["lat"], m["lon"])
    if "error" in w:
        raise HTTPException(503, w["error"])
    return {"place": m, **w}


@app.get("/api/prices")
def prices(state: str, commodity: str, district: str = ""):
    p = tool_mandi(state, commodity, district)
    if "error" in p:
        raise HTTPException(503, p["error"])
    return p


@app.post("/api/chat")
def chat(req: ChatReq):
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise HTTPException(500, "ANTHROPIC_API_KEY is not set on the server.")
    import anthropic
    client = anthropic.Anthropic()
    lang = "Hindi (Devanagari script)" if req.language == "hi" else "simple English"
    system = SYSTEM.format(lang=lang, loc=req.location, crop=req.crop, stage=req.stage or "not given")
    msgs = [{"role": m.role, "content": m.content} for m in req.messages]
    sources, weather_data, price_data, errors, called = [], None, None, [], False
    nudged = False
    for _ in range(8):
        try:
            resp = client.messages.create(model=MODEL, max_tokens=1200, system=system, tools=TOOL_SCHEMAS, messages=msgs)
        except Exception as e:
            raise HTTPException(502, f"AI service error: {e}")
        uses = [b for b in resp.content if b.type == "tool_use"]
        if not uses:
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            if not called and not nudged:  # guardrail: answer must be based on tool results
                nudged = True
                msgs += [{"role": "assistant", "content": resp.content},
                         {"role": "user", "content": "Use the tools first; the answer must be based on live tool results, or politely decline if unrelated to farming."}]
                continue
            if not called and nudged and not text:
                text = ""
            break
        called = True
        msgs.append({"role": "assistant", "content": resp.content})
        results = []
        for u in uses:
            out = TOOLS[u.name](**u.input) if u.name in TOOLS else {"error": "unknown tool"}
            if "error" in out:
                errors.append(out["missing"] if "missing" in out else u.name)
            else:
                sources.append({"name": out["source"], "time": out["fetched_at"]})
                if u.name == "get_weather":
                    weather_data = out
                if u.name == "get_mandi_prices":
                    price_data = out
            results.append({"type": "tool_result", "tool_use_id": u.id, "content": json.dumps(out, ensure_ascii=False)})
        msgs.append({"role": "user", "content": results})
    else:
        text = ""
    if not called:
        text = ("मैं मौसम, फसल के काम और मंडी भाव में मदद कर सकता हूँ। कृपया खेती से जुड़ा सवाल पूछें।"
                if req.language == "hi" else
                "I can help with weather, crop actions and mandi prices. Please ask a farming question.")
    return {"answer": text, "sources": sources, "missing_data": sorted(set(errors)),
            "warnings": derive_warnings(weather_data["days"], req.language) if weather_data else [],
            "weather": weather_data["days"] if weather_data else None,
            "price_trend": price_data["trend"] if price_data else None,
            "best_markets": price_data["top_markets_latest_date"] if price_data else None}


app.mount("/", StaticFiles(directory=Path(__file__).parent.parent / "frontend", html=True), name="frontend")
