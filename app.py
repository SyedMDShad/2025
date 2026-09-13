import time
import hashlib
import hmac
import json
import urllib.request
from flask import Flask, jsonify, render_template_string

app = Flask(__name__)

# --- TUYA API CREDENTIALS ---
CLIENT_ID = "hcdys9fmcvcchyrsjvqf"
CLIENT_SECRET = "c58da75d76124629a490905aac55e586"
BASE_URL = "https://openapi.tuyaeu.com"  # Central Europe Data Center

# Device IDs
OUTPUT_DEVICE_ID = "bf9fbc2c5e5a6dd45bvkvq"   # 16A _ Output Line (Inverter to Load)
CHARGING_DEVICE_ID = "bf64784528673eddf0h0u8" # 20A _ Charging Line (Grid to Inverter)

token_cache = {"access_token": "", "expire_time": 0}

def calc_sign(method, path, body="", access_token=""):
    t = str(int(time.time() * 1000))
    body_hash = hashlib.sha256(body.encode('utf-8')).hexdigest()
    string_to_sign = f"{method}\n{body_hash}\n\n{path}"
    to_sign = f"{CLIENT_ID}{access_token}{t}{string_to_sign}" if access_token else f"{CLIENT_ID}{t}{string_to_sign}"
    sign = hmac.new(CLIENT_SECRET.encode('utf-8'), to_sign.encode('utf-8'), hashlib.sha256).hexdigest().upper()
    return sign, t

def get_access_token():
    now = time.time()
    if token_cache["access_token"] and token_cache["expire_time"] > now + 60:
        return token_cache["access_token"]
    path = "/v1.0/token?grant_type=1"
    sign, t = calc_sign("GET", path)
    headers = {"client_id": CLIENT_ID, "sign": sign, "t": t, "sign_method": "HMAC-SHA256"}
    req = urllib.request.Request(f"{BASE_URL}{path}", headers=headers)
    try:
        with urllib.request.urlopen(req) as response:
            res = json.loads(response.read().decode())
            if res.get("success"):
                token_cache["access_token"] = res["result"]["access_token"]
                token_cache["expire_time"] = now + res["result"]["expire_time"]
                return token_cache["access_token"]
    except Exception as e:
        print(f"Token error: {e}")
    return None

def get_device_data(device_id):
    token = get_access_token()
    if not token:
        return {"online": False, "power": 0.0, "voltage": 0.0, "current": 0.0}
    path = f"/v1.0/devices/{device_id}/status"
    sign, t = calc_sign("GET", path, access_token=token)
    headers = {"client_id": CLIENT_ID, "access_token": token, "sign": sign, "t": t, "sign_method": "HMAC-SHA256"}
    req = urllib.request.Request(f"{BASE_URL}{path}", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            res = json.loads(response.read().decode())
            if res.get("success"):
                status_list = res.get("result", [])
                raw_power, raw_voltage, raw_current = 0.0, 0.0, 0.0
                for item in status_list:
                    code, val = item.get("code"), item.get("value")
                    if code in ["cur_power", "power"]: raw_power = float(val)
                    elif code in ["cur_voltage", "voltage"]: raw_voltage = float(val)
                    elif code in ["cur_current", "current"]: raw_current = float(val)
                
                power_w = raw_power / 10.0 if raw_power > 3000 else raw_power
                volt_v = raw_voltage / 10.0 if raw_voltage > 1000 else raw_voltage
                curr_a = raw_current / 1000.0 if raw_current > 100 else raw_current
                return {"online": True, "power": round(power_w, 1), "voltage": round(volt_v, 1), "current": round(curr_a, 2)}
    except Exception as e:
        print(f"Device error ({device_id}): {e}")
    return {"online": False, "power": 0.0, "voltage": 0.0, "current": 0.0}

@app.route("/api/live")
def api_live():
    out_data = get_device_data(OUTPUT_DEVICE_ID)
    in_data = get_device_data(CHARGING_DEVICE_ID)
    load_power, grid_power = out_data["power"], in_data["power"]
    
    # সোলার পাওয়ার হিসাব: লোড - গ্রিড (ইনভার্টার লস সহ)
    pv_power = max(0.0, load_power - (grid_power * 0.94))
    solar_ratio = min(100, round((pv_power / load_power) * 100, 1)) if load_power > 0 else 0
    
    return jsonify({
        "timestamp": time.strftime("%H:%M:%S"),
        "load": {"power": load_power, "voltage": out_data["voltage"], "current": out_data["current"], "online": out_data["online"]},
        "grid": {"power": grid_power, "voltage": in_data["voltage"], "current": in_data["current"], "online": in_data["online"]},
        "solar": {"power": round(pv_power, 1), "percentage": solar_ratio}
    })

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="bn">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SAKO Solar Live Monitor</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: 'Inter', sans-serif; background: #0b1329; color: #f8fafc; padding: 20px; }
        .container { max-width: 950px; margin: 0 auto; }
        header { text-align: center; margin-bottom: 25px; }
        header h1 { font-size: 24px; color: #f59e0b; }
        header p { color: #94a3b8; font-size: 13px; margin-top: 4px; }
        .grid-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; margin-bottom: 20px; }
        .card { background: #172554; border-radius: 14px; padding: 20px; border: 1px solid #1e3a8a; }
        .card-solar { border-top: 4px solid #f59e0b; background: linear-gradient(180deg, rgba(245,158,11,0.1) 0%, #172554 100%); }
        .card-load { border-top: 4px solid #3b82f6; }
        .card-grid { border-top: 4px solid #10b981; }
        .card-title { font-size: 13px; font-weight: 600; color: #94a3b8; display: flex; justify-content: space-between; }
        .main-val { font-size: 38px; font-weight: 700; line-height: 1.2; margin: 10px 0; }
        .val-unit { font-size: 16px; color: #94a3b8; }
        .sub-text { font-size: 13px; color: #94a3b8; display: flex; gap: 12px; border-top: 1px solid #1e3a8a; padding-top: 10px; }
        .chart-box { background: #172554; border-radius: 14px; padding: 20px; border: 1px solid #1e3a8a; }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>☀️ SAKO Solar Live Monitor</h1>
            <p>REC Alpha Pure-R + SAKO E-SUN | Chilahati, Nilphamari</p>
        </header>

        <div class="grid-cards">
            <div class="card card-solar">
                <div class="card-title"><span>☀️ ESTIMATED SOLAR PV</span><span id="solar-ratio" style="color:#f59e0b; font-weight:700;">0%</span></div>
                <div class="main-val" style="color: #fbbf24;"><span id="solar-watt">0.0</span> <span class="val-unit">Watt</span></div>
                <div class="sub-text"><span>ফ্রি বিদ্যুৎ চলছে</span></div>
            </div>

            <div class="card card-load">
                <div class="card-title"><span>🏠 16A OUTPUT (LOAD)</span><span id="load-status">Online</span></div>
                <div class="main-val" style="color: #60a5fa;"><span id="load-watt">0.0</span> <span class="val-unit">Watt</span></div>
                <div class="sub-text">
                    <span>ভোল্টেজ: <strong id="load-v" style="color:#fff;">0V</strong></span>
                    <span>কারেন্ট: <strong id="load-a" style="color:#fff;">0A</strong></span>
                </div>
            </div>

            <div class="card card-grid">
                <div class="card-title"><span>🔌 20A CHARGING (GRID)</span><span id="grid-status">Offline</span></div>
                <div class="main-val" style="color: #34d399;"><span id="grid-watt">0.0</span> <span class="val-unit">Watt</span></div>
                <div class="sub-text">
                    <span>ভোল্টেজ: <strong id="grid-v" style="color:#fff;">0V</strong></span>
                    <span>কারেন্ট: <strong id="grid-a" style="color:#fff;">0A</strong></span>
                </div>
            </div>
        </div>

        <div class="chart-box">
            <h3 style="font-size: 15px; margin-bottom: 12px; color: #cbd5e1;">📊 Live Power Trends (প্রতি ৫ সেকেন্ডে আপডেট)</h3>
            <div style="height: 250px;"><canvas id="liveChart"></canvas></div>
        </div>
    </div>

    <script>
        const ctx = document.getElementById('liveChart').getContext('2d');
        const chart = new Chart(ctx, {
            type: 'line',
            data: {
                labels: [],
                datasets: [
                    { label: 'Solar PV (Watt)', borderColor: '#fbbf24', borderWidth: 2.5, data: [] },
                    { label: 'House Load (Watt)', borderColor: '#60a5fa', borderWidth: 2, data: [] },
                    { label: 'Grid In (Watt)', borderColor: '#10b981', borderDash: [4, 4], data: [] }
                ]
            },
            options: { responsive: true, maintainAspectRatio: false, scales: { x: { grid: { color: '#1e3a8a' } }, y: { grid: { color: '#1e3a8a' }, beginAtZero: true } } }
        });

        async function updateData() {
            try {
                const res = await fetch('/api/live');
                const data = await res.json();
                document.getElementById('solar-watt').innerText = data.solar.power;
                document.getElementById('solar-ratio').innerText = data.solar.percentage + '% Solar';
                document.getElementById('load-watt').innerText = data.load.power;
                document.getElementById('load-v').innerText = data.load.voltage + 'V';
                document.getElementById('load-a').innerText = data.load.current + 'A';
                document.getElementById('grid-watt').innerText = data.grid.power;
                document.getElementById('grid-v').innerText = data.grid.voltage + 'V';
                document.getElementById('grid-a').innerText = data.grid.current + 'A';

                if (chart.data.labels.length > 15) {
                    chart.data.labels.shift();
                    chart.data.datasets.forEach(d => d.data.shift());
                }
                chart.data.labels.push(data.timestamp);
                chart.data.datasets[0].data.push(data.solar.power);
                chart.data.datasets.data.push(data.load.power);
                chart.data.datasets.data.push(data.grid.power);
                chart.update();
            } catch(e) { console.error(e); }
        }
        setInterval(updateData, 5000);
        updateData();
    </script>
</body>
</html>
"""

@app.route("/")
def home():
    return render_template_string(HTML_TEMPLATE)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
