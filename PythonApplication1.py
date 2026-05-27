import sqlite3
import requests
import random
from datetime import datetime
from flask import Flask, jsonify, request, render_template

app = Flask(__name__)
DB = "air.db"

OPENWEATHER_KEY = "71ecabbaea996559a8f9dee0927866f6"
WAQI_TOKEN     = "aea359a83ace83e3b0eb5d5cedc8215f12f651db"

ROMANIA_CITIES = [
    ("Bucuresti",  44.43, 26.10),
    ("Cluj-Napoca",46.77, 23.59),
    ("Iasi",       47.16, 27.58),
    ("Brasov",     45.65, 25.61),
    ("Constanta",  44.18, 28.64),
    ("Timisoara",  45.75, 21.23),
    ("Sibiu",      45.79, 24.15),
    ("Oradea",     47.05, 21.93),
]
CITY_NAMES = [c[0] for c in ROMANIA_CITIES]

# ═══════════════════════ DB ═══════════════════════
def init_db():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS measurements (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            oras        TEXT,
            temperatura REAL,
            pm25        REAL,
            lat         REAL,
            lon         REAL,
            descriere   TEXT,
            recomandare TEXT,
            nivel_aer   TEXT,
            source      TEXT,
            timestamp   TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    # Migrare coloane vechi daca lipsesc
    c.execute("PRAGMA table_info(measurements)")
    cols = {row[1] for row in c.fetchall()}
    extras = [
        ("descriere",   "TEXT"),
        ("recomandare", "TEXT"),
        ("nivel_aer",   "TEXT"),
        ("source",      "TEXT"),
        ("timestamp",   "TEXT"),
    ]
    for col, typ in extras:
        if col not in cols:
            c.execute(f"ALTER TABLE measurements ADD COLUMN {col} {typ}")
    conn.commit()
    conn.close()

def db_exec(q, args=(), fetchall=False, fetchone=False):
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(q, args)
    res = None
    if fetchall:
        res = [dict(r) for r in c.fetchall()]
    elif fetchone:
        r = c.fetchone()
        res = dict(r) if r else None
    conn.commit()
    conn.close()
    return res

# ═══════════════════════ LOGICA AER ═══════════════════════
def nivel_calitate_aer(pm25):
    if pm25 is None: return "necunoscut"
    if pm25 <= 25:   return "excelent"
    if pm25 <= 50:   return "bun"
    if pm25 <= 75:   return "moderat"
    if pm25 <= 100:  return "slab"
    return "foarte slab"

def scor_gradinarit(temp, pm25, desc):
    """Returneaza un scor 0-10 pentru gradinarit."""
    score = 5
    desc_l = (desc or "").lower()
    if temp is not None:
        if 18 <= temp <= 25:   score += 3
        elif 15 <= temp <= 28: score += 2
        elif 10 <= temp <= 32: score += 1
        elif temp < 5 or temp > 35: score -= 3
    if pm25 is not None:
        if pm25 <= 25:   score += 2
        elif pm25 <= 50: score += 1
        elif pm25 > 100: score -= 2
    if any(w in desc_l for w in ["ploaie","rain","avers","furtuna","storm"]):
        score -= 2
    if any(w in desc_l for w in ["senin","clear","soare","sunny"]):
        score += 1
    return max(0, min(10, score))

def recomandare_detaliata(temp, pm25, desc):
    desc_l = (desc or "").lower()
    rain = any(w in desc_l for w in ["ploaie","rain","avers","precipit"])
    
    # Calitate aer
    if pm25 is None:      aer_msg = "ℹ Date AQI indisponibile momentan."
    elif pm25 > 100:      aer_msg = "🚨 Aer FOARTE POLUAT — evita activitatile fizice afara!"
    elif pm25 > 75:       aer_msg = "⚠️ Aer poluat — limiteaza timpul afara."
    elif pm25 > 50:       aer_msg = "🟡 Aer moderat — atentie daca ai probleme respiratorii."
    else:                 aer_msg = "✅ Aer curat — conditii excelente pentru exterior."
    
    # Temperatura si activitati
    if rain:              act_msg = "🌧️ Ploua — nu rasi, dar plantele se bucura! Verifica scurgerile."
    elif temp is None:    act_msg = "ℹ Date temperatura indisponibile."
    elif temp < 0:        act_msg = "❄️ INGHET — protejeaza plantele cu folie sau paie urgent!"
    elif temp < 8:        act_msg = "🧊 Frig intens — ideal pentru plante rezistente (varza, spanac)."
    elif temp < 15:       act_msg = "🌸 Racoare — pregateste solul, planteaza salata si ridichi."
    elif temp < 22:       act_msg = "☀️ Temperatura PERFECTA — rasadeste, pliveste, fertilizeaza!"
    elif temp < 30:       act_msg = "🌻 Cald placut — uda dimineata devreme sau dupa ora 19."
    elif temp < 36:       act_msg = "🔥 Cald mare — umbrire necesara, mulci gros, uda abundent."
    else:                 act_msg = "⛔ CANICULA — salveaza plantele, uda seara, stai la umbra!"
    
    return f"{aer_msg} | {act_msg}"

# ═══════════════════════ API EXTERNE ═══════════════════════
def get_weather(city):
    url = f"https://api.openweathermap.org/data/2.5/weather?q={city}&appid={OPENWEATHER_KEY}&units=metric&lang=ro"
    r = requests.get(url, timeout=8)
    r.raise_for_status()
    d = r.json()
    return {
        "temp":     d["main"]["temp"],
        "humidity": d["main"]["humidity"],
        "wind":     d["wind"]["speed"],
        "desc":     d["weather"][0]["description"],
        "icon":     d["weather"][0]["icon"],
        "lat":      d["coord"]["lat"],
        "lon":      d["coord"]["lon"],
    }

def get_pm25(city):
    url = f"https://api.waqi.info/feed/{city}/?token={WAQI_TOKEN}"
    r = requests.get(url, timeout=8)
    r.raise_for_status()
    d = r.json()
    try:    return d["data"]["iaqi"]["pm25"]["v"]
    except: return None

def save_measurement(oras, temp, pm25, lat, lon, desc, source):
    aer = nivel_calitate_aer(pm25)
    rec = recomandare_detaliata(temp, pm25, desc)
    db_exec("""
        INSERT INTO measurements (oras, temperatura, pm25, lat, lon, descriere, recomandare, nivel_aer, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (oras, temp, pm25, lat, lon, desc, rec, aer, source))
    return aer, rec

# ═══════════════════════ ROUTES ═══════════════════════

@app.route("/")
def home():
    return render_template("HTMLPage1.html")

# ── Live meteo pentru oras ──
@app.route("/api/meteo/live/<city>", methods=["GET"])
def api_live_city(city):
    try:
        w   = get_weather(city)
        pm  = get_pm25(city)
        aer, rec = save_measurement(city, w["temp"], pm, w["lat"], w["lon"], w["desc"], "live")
        scor = scor_gradinarit(w["temp"], pm, w["desc"])
        return jsonify({
            "oras":        city,
            "temperatura": w["temp"],
            "umiditate":   w["humidity"],
            "vant":        w["wind"],
            "pm25":        pm,
            "lat":         w["lat"],
            "lon":         w["lon"],
            "descriere":   w["desc"],
            "icon":        w["icon"],
            "recomandare": rec,
            "nivel_aer":   aer,
            "scor_gradinarit": scor,
            "source":      "live",
        })
    except Exception as e:
        return jsonify({"eroare": str(e)}), 500

# ── Toate masuratorile ──
@app.route("/api/measurements", methods=["GET"])
def get_all():
    limit = request.args.get("limit", 50, type=int)
    oras  = request.args.get("oras")
    src   = request.args.get("source")
    q = "SELECT * FROM measurements"
    conds, args = [], []
    if oras:
        conds.append("oras=?"); args.append(oras)
    if src:
        conds.append("source=?"); args.append(src)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += f" ORDER BY id DESC LIMIT {limit}"
    return jsonify(db_exec(q, tuple(args), fetchall=True))

# ── Adaugare manuala ──
@app.route("/api/measurements", methods=["POST"])
def add_manual():
    d = request.json or {}
    oras = d.get("oras")
    temp = d.get("temperatura")
    pm25 = d.get("pm25")
    if not oras or temp is None or pm25 is None:
        return jsonify({"eroare": "oras, temperatura si pm25 sunt obligatorii"}), 400
    aer, rec = save_measurement(oras, temp, pm25, d.get("lat"), d.get("lon"), d.get("descriere","manual"), "manual")
    scor = scor_gradinarit(temp, pm25, d.get("descriere",""))
    return jsonify({"mesaj": "adaugat", "nivel_aer": aer, "recomandare": rec, "scor_gradinarit": scor}), 201

# ── Stergere inregistrare ──
@app.route("/api/measurements/<int:id>", methods=["DELETE"])
def delete_one(id):
    db_exec("DELETE FROM measurements WHERE id=?", (id,))
    return jsonify({"mesaj": "sters"})

# ── Clear all ──
@app.route("/api/measurements/clear", methods=["POST"])
def clear_all():
    db_exec("DELETE FROM measurements")
    return jsonify({"mesaj": "istoric sters"})

# ── Generator simulat ──
@app.route("/generate", methods=["POST"])
def generate():
    city, lat, lon = random.choice(ROMANIA_CITIES)
    temp = round(random.uniform(-5, 38), 1)
    pm   = round(random.uniform(5, 160), 1)
    desc = random.choice(["senin", "innnorat", "ploaie usoara", "ceata", "soare puternic", "vant moderat"])
    aer, rec = save_measurement(city, temp, pm, lat, lon, desc, "simulator")
    scor = scor_gradinarit(temp, pm, desc)
    return jsonify({"city": city, "temp": temp, "pm": pm, "lat": lat, "lon": lon,
                    "nivel_aer": aer, "scor_gradinarit": scor, "descriere": desc})

# ── Generate x N ──
@app.route("/generate/<int:n>", methods=["POST"])
def generate_n(n):
    n = min(n, 20)
    results = []
    for _ in range(n):
        city, lat, lon = random.choice(ROMANIA_CITIES)
        temp = round(random.uniform(-5, 38), 1)
        pm   = round(random.uniform(5, 160), 1)
        desc = random.choice(["senin", "innnorat", "ploaie", "soare", "ceata"])
        aer, _ = save_measurement(city, temp, pm, lat, lon, desc, "simulator")
        results.append({"city": city, "temp": temp, "pm": pm, "nivel_aer": aer})
    return jsonify({"generat": len(results), "results": results})

# ── Random city live ──
@app.route("/api/random-city", methods=["GET"])
def random_city():
    city = random.choice(CITY_NAMES)
    return api_live_city(city)

# ── Statistici ──
@app.route("/api/stats", methods=["GET"])
def stats():
    total    = db_exec("SELECT COUNT(*) as n FROM measurements", fetchone=True)["n"]
    avg_t    = db_exec("SELECT ROUND(AVG(temperatura),1) as v FROM measurements", fetchone=True)["v"]
    avg_pm   = db_exec("SELECT ROUND(AVG(pm25),1) as v FROM measurements", fetchone=True)["v"]
    max_pm   = db_exec("SELECT MAX(pm25) as v FROM measurements", fetchone=True)["v"]
    min_t    = db_exec("SELECT MIN(temperatura) as v FROM measurements", fetchone=True)["v"]
    max_t    = db_exec("SELECT MAX(temperatura) as v FROM measurements", fetchone=True)["v"]
    by_city  = db_exec("""
        SELECT oras, COUNT(*) as nr, ROUND(AVG(temperatura),1) as avg_t, ROUND(AVG(pm25),1) as avg_pm
        FROM measurements GROUP BY oras ORDER BY nr DESC LIMIT 8
    """, fetchall=True)
    by_src   = db_exec("SELECT source, COUNT(*) as nr FROM measurements GROUP BY source", fetchall=True)
    return jsonify({
        "total": total, "avg_temp": avg_t, "avg_pm": avg_pm,
        "max_pm": max_pm, "min_temp": min_t, "max_temp": max_t,
        "by_city": by_city, "by_source": by_src,
    })

# ── Top orase Romania ──
@app.route("/api/top/romania", methods=["GET"])
def top_romania():
    results = []
    for city, lat, lon in ROMANIA_CITIES:
        try:
            w   = get_weather(city)
            pm  = get_pm25(city)
            aer, rec = save_measurement(city, w["temp"], pm, w["lat"], w["lon"], w["desc"], "top_romania")
            scor = scor_gradinarit(w["temp"], pm, w["desc"])
            results.append({
                "oras": city, "temperatura": w["temp"], "pm25": pm,
                "lat": w["lat"], "lon": w["lon"],
                "descriere": w["desc"], "icon": w["icon"],
                "nivel_aer": aer, "recomandare": rec,
                "scor_gradinarit": scor,
            })
        except:
            continue

    best_air    = sorted([r for r in results if r["pm25"] is not None], key=lambda x: x["pm25"])[:5]
    worst_air   = sorted([r for r in results if r["pm25"] is not None], key=lambda x: x["pm25"], reverse=True)[:5]
    best_garden = sorted(results, key=lambda x: -x["scor_gradinarit"])[:5]
    hottest     = sorted([r for r in results if r["temperatura"] is not None], key=lambda x: x["temperatura"], reverse=True)[:3]
    coldest     = sorted([r for r in results if r["temperatura"] is not None], key=lambda x: x["temperatura"])[:3]
    return jsonify({"best_air": best_air, "worst_air": worst_air,
                    "best_garden": best_garden, "hottest": hottest, "coldest": coldest})

# ── Export CSV simplu ──
@app.route("/api/export/csv", methods=["GET"])
def export_csv():
    from flask import Response
    rows = db_exec("SELECT * FROM measurements ORDER BY id DESC", fetchall=True)
    if not rows:
        return Response("id,oras,temperatura,pm25,lat,lon,descriere,nivel_aer,source,timestamp\n",
                        mimetype="text/csv",
                        headers={"Content-Disposition": "attachment;filename=meteo_export.csv"})
    keys = list(rows[0].keys())
    lines = [",".join(keys)]
    for r in rows:
        lines.append(",".join(str(r.get(k,"")) for k in keys))
    return Response("\n".join(lines), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment;filename=meteo_export.csv"})

# ═══════════════════════ RUN ═══════════════════════
if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5000)