import os
import json
import time
import hmac
import hashlib
import requests
from flask import Flask, jsonify, request, render_template_string

app = Flask(__name__)

# ==================== TUYA CLOUD CONFIG ====================
# Render Environment Variables অথবা ডিফল্ট ভ্যালু
TUYA_ACCESS_ID = os.environ.get("TUYA_ACCESS_ID", "YOUR_TUYA_ACCESS_ID")
TUYA_ACCESS_SECRET = os.environ.get("TUYA_ACCESS_SECRET", "YOUR_TUYA_SECRET")
TUYA_ENDPOINT = os.environ.get("TUYA_ENDPOINT", "https://openapi.tuyaus.com")

DEVICE_GRID = os.environ.get("DEVICE_GRID", "YOUR_GRID_SMARTPLUG_ID")
DEVICE_LOAD = os.environ.get("DEVICE_LOAD", "YOUR_LOAD_SMARTPLUG_ID")

SETTINGS_FILE = "settings.json"

def load_settings():
    defaults = {
        "battery_soc": 85,
        "battery_amps": 21.0,
        "battery_volts": 13.5,
        "manual_soc": True,
        "manual_amps": True,
        "solar_override": 0.0
    }
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                saved = json.load(f)
                defaults.update(saved)
        except Exception:
            pass
    return defaults

def save_settings(data):
    current = load_settings()
    current.update(data)
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(current, f)
    except Exception as e:
        print("Error saving settings:", e)
    return current

# ==================== TUYA API HELPERS ====================
def tuya_get_token():
    t = str(int(time.time() * 1000))
    string_to_sign = f"{TUYA_ACCESS_ID}{t}GET\n\n\n/v1.0/token?grant_type=1"
    sign = hmac.new(
        TUYA_ACCESS_SECRET.encode('utf-8'),
        string_to_sign.encode('utf-8'),
        hashlib.sha256
    ).hexdigest().upper()

    headers = {
        'client_id': TUYA_ACCESS_ID,
        'sign': sign,
        't': t,
        'sign_method': 'HMAC-SHA256'
    }
    try:
        res = requests.get(f"{TUYA_ENDPOINT}/v1.0/token?grant_type=1", headers=headers, timeout=5)
        return res.json().get('result', {}).get('access_token')
    except Exception as e:
        print("Tuya Token Error:", e)
        return None

def tuya_get_device_status(token, device_id):
    if not token or not device_id:
        return {}
    t = str(int(time.time() * 1000))
    path = f"/v1.0/devices/{device_id}/status"
    string_to_sign = f"{TUYA_ACCESS_ID}{token}{t}GET\n\n\n{path}"
    sign = hmac.new(
        TUYA_ACCESS_SECRET.encode('utf-8'),
        string_to_sign.encode('utf-8'),
        hashlib.sha256
    ).hexdigest().upper()

    headers = {
        'client_id': TUYA_ACCESS_ID,
        'access_token': token,
        'sign': sign,
        't': t,
        'sign_method': 'HMAC-SHA256'
    }
    try:
        res = requests.get(f"{TUYA_ENDPOINT}{path}", headers=headers, timeout=5)
        result = res.json().get('result', [])
        status_map = {}
        for item in result:
            code = item.get('code')
            val = item.get('value')
            status_map[code] = val
        return status_map
    except Exception as e:
        print(f"Tuya Status Error for {device_id}:", e)
        return {}

# ==================== API ROUTES ====================
@app.route("/api/live")
def get_live_data():
    settings = load_settings()
    token = tuya_get_token()

    grid_data = tuya_get_device_status(token, DEVICE_GRID)
    load_data = tuya_get_device_status(token, DEVICE_LOAD)

    # Tuya Smart Plug: সাধারণত cur_power এর ইউনিট 0.1W (অথবা W)
    def parse_power(d):
        val = float(d.get('cur_power', 0.0) or 0.0)
        return val / 10.0 if val > 1000 else val

    def parse_voltage(d):
        val = float(d.get('cur_voltage', 2200) or 2200)
        return val / 10.0 if val > 1000 else (val if val > 0 else 220.0)

    grid_p = parse_power(grid_data)
    load_p = parse_power(load_data)
    ac_v = parse_voltage(grid_data)

    # সোলার ও ব্যাটারি ক্যালকুলেশন
    battery_amps = float(settings.get("battery_amps", 21.0))
    battery_volts = float(settings.get("battery_volts", 13.5))
    batt_power_flow = battery_amps * battery_volts

    # এসি লোডে সোলারের অবদান
    solar_ac_component = max(0.0, load_p - (grid_p * 0.94))

    # সর্বমোট সোলার জেনারেশন = এসি লোডের সোলার অংশ + ব্যাটারি চার্জিং ওয়াট (DC)
    calculated_solar = solar_ac_component + max(0.0, batt_power_flow)

    # ম্যানুয়াল ওভাররাইড থাকলে
    manual_solar = float(settings.get("solar_override", 0.0))
    final_solar = manual_solar if manual_solar > 0 else calculated_solar

    return jsonify({
        "solar_w": round(final_solar, 1),
        "grid_w": round(grid_p, 1),
        "load_w": round(load_p, 1),
        "ac_volts": round(ac_v, 1),
        "battery_soc": int(settings.get("battery_soc", 85)),
        "battery_amps": round(battery_amps, 1),
        "battery_volts": round(battery_volts, 1),
        "battery_power": round(abs(batt_power_flow), 1),
        "battery_charging": battery_amps > 0.5,
        "battery_discharging": battery_amps < -0.5,
        "manual_soc": settings.get("manual_soc", True)
    })

@app.route("/api/settings", methods=["GET", "POST"])
def manage_settings():
    if request.method == "POST":
        data = request.json or {}
        updated = save_settings(data)
        return jsonify({"status": "success", "settings": updated})
    return jsonify(load_settings())

# ==================== DASHBOARD UI ====================
INDEX_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>SAKO Solar Pro Command Center</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;600;800;900&family=Rajdhani:wght@500;600;700&display=swap" rel="stylesheet">
  <style>
    body { font-family: 'Rajdhani', sans-serif; background: #070b14; color: #f8fafc; }
    .mono { font-family: 'Orbitron', monospace; }
    .neon-panel {
      background: rgba(15, 23, 42, 0.75);
      backdrop-filter: blur(16px);
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: 1.25rem;
    }
    .flow-line { stroke-dasharray: 8 8; animation: flowDash 1s linear infinite; }
    @keyframes flowDash { to { stroke-dashoffset: -16; } }
  </style>
</head>
<body class="min-h-screen p-4 md:p-8 flex flex-col items-center justify-center">

  <div class="w-full max-w-4xl space-y-6">
    <header class="neon-panel p-5 flex items-center justify-between shadow-2xl border-cyan-500/20">
      <div>
        <h1 class="mono text-2xl font-bold tracking-wider bg-gradient-to-r from-amber-400 via-emerald-400 to-cyan-400 bg-clip-text text-transparent">
          SAKO SOLAR PRO
        </h1>
        <p class="text-xs text-slate-400 font-semibold">2x REC 400W • E-SUN 1.2KW • Chilahati</p>
      </div>
      <button onclick="openSettingsModal()" class="px-4 py-2 bg-slate-800/90 hover:bg-slate-700 border border-slate-600/50 rounded-xl text-xs font-semibold mono flex items-center gap-2">
        <span>⚙️ Edit Battery & System</span>
      </button>
    </header>

    <div class="grid grid-cols-2 md:grid-cols-4 gap-4">
      <div class="neon-panel p-4 border-amber-500/30">
        <div class="text-xs text-amber-400 font-bold uppercase tracking-wider mb-1">☀️ Solar PV</div>
        <div class="mono text-3xl font-extrabold text-amber-300"><span id="solar-w">0</span><span class="text-sm font-normal text-amber-500 ml-1">W</span></div>
        <div class="text-[11px] text-slate-400 mt-1">Direct DC + AC Share</div>
      </div>

      <div class="neon-panel p-4 border-cyan-500/30">
        <div class="text-xs text-cyan-400 font-bold uppercase tracking-wider mb-1">⚡ Grid (BPDB)</div>
        <div class="mono text-3xl font-extrabold text-cyan-300"><span id="grid-w">0</span><span class="text-sm font-normal text-cyan-500 ml-1">W</span></div>
        <div class="text-[11px] text-slate-400 mt-1"><span id="ac-volts">220</span>V Online</div>
      </div>

      <div onclick="openSettingsModal()" class="neon-panel p-4 border-emerald-500/30 cursor-pointer hover:border-emerald-400/60 transition group">
        <div class="flex justify-between items-center mb-1">
          <span class="text-xs text-emerald-400 font-bold uppercase tracking-wider">🔋 Battery SOC</span>
          <span class="text-[10px] text-emerald-300 bg-emerald-950/60 px-2 py-0.5 rounded-full border border-emerald-500/30">✏️ Edit</span>
        </div>
        <div class="mono text-3xl font-extrabold text-emerald-300"><span id="battery-soc">85</span><span class="text-sm font-normal text-emerald-500 ml-1">%</span></div>
        <div class="text-[11px] text-emerald-400/80 mt-1 font-semibold" id="batt-status">+21.0 A • Charging</div>
      </div>

      <div class="neon-panel p-4 border-rose-500/30">
        <div class="text-xs text-rose-400 font-bold uppercase tracking-wider mb-1">🏠 House Load</div>
        <div class="mono text-3xl font-extrabold text-rose-300"><span id="load-w">0</span><span class="text-sm font-normal text-rose-500 ml-1">W</span></div>
        <div class="text-[11px] text-slate-400 mt-1">AC Output</div>
      </div>
    </div>

    <div class="neon-panel p-6 flex flex-col items-center justify-center relative overflow-hidden">
      <div class="mono text-xs text-slate-400 mb-2 uppercase tracking-widest">Realtime Power Flow Vector</div>
      
      <svg class="w-full max-w-lg h-56" viewBox="0 0 500 240">
        <path d="M 90 70 L 250 120" stroke="#f59e0b" stroke-width="3" class="flow-line" id="flow-solar" />
        <path d="M 90 170 L 250 120" stroke="#06b6d4" stroke-width="3" class="flow-line" id="flow-grid" />
        <path d="M 250 120 L 410 70" stroke="#10b981" stroke-width="3" class="flow-line" id="flow-batt" />
        <path d="M 250 120 L 410 170" stroke="#f43f5e" stroke-width="3" class="flow-line" id="flow-load" />

        <circle cx="90" cy="70" r="32" fill="#1e293b" stroke="#f59e0b" stroke-width="3"/>
        <text x="90" y="74" text-anchor="middle" fill="#fef08a" font-size="20">☀️</text>
        <text x="90" y="115" text-anchor="middle" fill="#cbd5e1" font-size="11" class="mono font-bold">SOLAR</text>

        <circle cx="90" cy="170" r="32" fill="#1e293b" stroke="#06b6d4" stroke-width="3"/>
        <text x="90" y="174" text-anchor="middle" fill="#a5f3fc" font-size="20">⚡</text>
        <text x="90" y="215" text-anchor="middle" fill="#cbd5e1" font-size="11" class="mono font-bold">GRID</text>

        <circle cx="250" cy="120" r="40" fill="#0f172a" stroke="#8b5cf6" stroke-width="4"/>
        <text x="250" y="125" text-anchor="middle" fill="#c084fc" font-size="24">🔄</text>
        <text x="250" y="175" text-anchor="middle" fill="#cbd5e1" font-size="12" class="mono font-bold">INVERTER</text>

        <circle cx="410" cy="70" r="32" fill="#1e293b" stroke="#10b981" stroke-width="3"/>
        <text x="410" y="74" text-anchor="middle" fill="#a7f3d0" font-size="20">🔋</text>
        <text x="410" y="115" text-anchor="middle" fill="#cbd5e1" font-size="11" class="mono font-bold">BATTERY</text>

        <circle cx="410" cy="170" r="32" fill="#1e293b" stroke="#f43f5e" stroke-width="3"/>
        <text x="410" y="174" text-anchor="middle" fill="#fecdd3" font-size="20">🏠</text>
        <text x="410" y="215" text-anchor="middle" fill="#cbd5e1" font-size="11" class="mono font-bold">LOAD</text>
      </svg>
    </div>
  </div>

  <div id="settings-modal" class="fixed inset-0 bg-black/80 backdrop-blur-sm z-50 hidden items-center justify-center p-4">
    <div class="neon-panel w-full max-w-md p-6 space-y-5 border-cyan-500/40 shadow-2xl">
      <div class="flex justify-between items-center border-b border-slate-700 pb-3">
        <h2 class="mono text-lg font-bold text-cyan-300">⚙️ Override & Battery Settings</h2>
        <button onclick="closeSettingsModal()" class="text-slate-400 hover:text-white text-xl">✕</button>
      </div>

      <div class="space-y-2">
        <div class="flex justify-between text-sm">
          <span class="text-slate-300 font-semibold">Battery SOC %:</span>
          <span id="soc-display" class="mono text-emerald-400 font-bold text-lg">85%</span>
        </div>
        <input type="range" id="soc-input" min="10" max="100" value="85" class="w-full accent-emerald-500 h-2 bg-slate-700 rounded-lg cursor-pointer" oninput="document.getElementById('soc-display').innerText = this.value + '%'">
        <div class="flex gap-2 pt-1">
          <button onclick="setSOC(50)" class="px-2 py-1 bg-slate-800 hover:bg-slate-700 rounded text-xs mono">50%</button>
          <button onclick="setSOC(70)" class="px-2 py-1 bg-slate-800 hover:bg-slate-700 rounded text-xs mono">70%</button>
          <button onclick="setSOC(80)" class="px-2 py-1 bg-slate-800 hover:bg-slate-700 rounded text-xs mono">80%</button>
          <button onclick="setSOC(85)" class="px-2 py-1 bg-slate-800 hover:bg-slate-700 rounded text-xs mono">85%</button>
          <button onclick="setSOC(100)" class="px-2 py-1 bg-slate-800 hover:bg-slate-700 rounded text-xs mono">100%</button>
        </div>
      </div>

      <div class="space-y-2">
        <label class="block text-sm text-slate-300 font-semibold">Battery Current (Amps):</label>
        <input type="number" step="0.5" id="amps-input" value="21.0" class="w-full bg-slate-800 border border-slate-700 rounded-xl px-4 py-2 text-white mono font-bold focus:border-cyan-500 outline-none">
        <div class="flex gap-2 text-xs">
          <button onclick="setAmps(21.0)" class="px-2.5 py-1 bg-emerald-950 border border-emerald-500/40 text-emerald-300 rounded font-bold mono">+21A ⚡</button>
          <button onclick="setAmps(15.0)" class="px-2.5 py-1 bg-slate-800 hover:bg-slate-700 rounded mono">+15A</button>
          <button onclick="setAmps(0.0)" class="px-2.5 py-1 bg-slate-800 hover:bg-slate-700 rounded mono">0A Idle</button>
          <button onclick="setAmps(-15.0)" class="px-2.5 py-1 bg-rose-950 border border-rose-500/40 text-rose-300 rounded font-bold mono">-15A 🔻</button>
        </div>
      </div>

      <div class="space-y-2">
        <label class="block text-sm text-slate-300 font-semibold">Solar Manual Override (Watts, 0 = Auto):</label>
        <input type="number" id="solar-input" value="0" placeholder="0 for auto calculation" class="w-full bg-slate-800 border border-slate-700 rounded-xl px-4 py-2 text-amber-300 mono font-bold focus:border-amber-500 outline-none">
      </div>

      <div class="pt-2 flex gap-3">
        <button onclick="saveSettingsToServer()" class="flex-1 py-3 bg-gradient-to-r from-emerald-500 to-cyan-500 hover:from-emerald-400 hover:to-cyan-400 text-slate-950 font-bold mono rounded-xl shadow-lg transition">
          💾 SAVE & APPLY
        </button>
        <button onclick="closeSettingsModal()" class="px-5 py-3 bg-slate-800 hover:bg-slate-700 text-slate-300 font-semibold rounded-xl mono">
          Cancel
        </button>
      </div>
    </div>
  </div>

  <script>
    function setSOC(val) {
      document.getElementById('soc-input').value = val;
      document.getElementById('soc-display').innerText = val + '%';
    }
    function setAmps(val) {
      document.getElementById('amps-input').value = val;
    }

    function openSettingsModal() {
      document.getElementById('settings-modal').classList.remove('hidden');
      document.getElementById('settings-modal').classList.add('flex');
    }
    function closeSettingsModal() {
      document.getElementById('settings-modal').classList.add('hidden');
      document.getElementById('settings-modal').classList.remove('flex');
    }

    async function fetchLiveData() {
      try {
        const res = await fetch('/api/live');
        const d = await res.json();
        
        document.getElementById('solar-w').innerText = d.solar_w;
        document.getElementById('grid-w').innerText = d.grid_w;
        document.getElementById('load-w').innerText = d.load_w;
        document.getElementById('ac-volts').innerText = d.ac_volts;
        document.getElementById('battery-soc').innerText = d.battery_soc;
        
        // Battery status text
        const statusEl = document.getElementById('batt-status');
        if (d.battery_charging) {
          statusEl.innerText = `+${d.battery_amps} A • Charging (${d.battery_power}W)`;
          statusEl.className = "text-[11px] text-emerald-400 font-semibold mt-1";
        } else if (d.battery_discharging) {
          statusEl.innerText = `${d.battery_amps} A • Discharging (${d.battery_power}W)`;
          statusEl.className = "text-[11px] text-amber-400 font-semibold mt-1";
        } else {
          statusEl.innerText = `0.0 A • Idle`;
          statusEl.className = "text-[11px] text-slate-400 font-semibold mt-1";
        }

        // SVG animations control
        document.getElementById('flow-solar').style.display = d.solar_w > 10 ? 'block' : 'none';
        document.getElementById('flow-grid').style.display = d.grid_w > 10 ? 'block' : 'none';
        document.getElementById('flow-batt').style.display = Math.abs(d.battery_amps) > 0.5 ? 'block' : 'none';
        document.getElementById('flow-load').style.display = d.load_w > 10 ? 'block' : 'none';

      } catch (err) {
        console.error("Fetch error:", err);
      }
    }

    async function saveSettingsToServer() {
      const soc = parseInt(document.getElementById('soc-input').value);
      const amps = parseFloat(document.getElementById('amps-input').value);
      const solar = parseFloat(document.getElementById('solar-input').value) || 0.0;

      await fetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          battery_soc: soc,
          battery_amps: amps,
          solar_override: solar
        })
      });

      closeSettingsModal();
      fetchLiveData();
    }

    // Load settings into modal on open
    async function loadCurrentSettings() {
      try {
        const res = await fetch('/api/settings');
        const s = await res.json();
        setSOC(s.battery_soc || 85);
        setAmps(s.battery_amps || 21.0);
        document.getElementById('solar-input').value = s.solar_override || 0;
      } catch (e) {}
    }

    setInterval(fetchLiveData, 2500);
    fetchLiveData();
    loadCurrentSettings();
  </script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(INDEX_HTML)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
