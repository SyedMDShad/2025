import time
import hashlib
import hmac
import json
import os
import math
from datetime import datetime, date
import urllib.request
from flask import Flask, jsonify

app = Flask(__name__)

# --- TUYA API CREDENTIALS ---
CLIENT_ID = "hcdys9fmcvcchyrsjvqf"
CLIENT_SECRET = "c58da75d76124629a490905aac55e586"
BASE_URL = "https://openapi.tuyaeu.com"

# --- TUYA DEVICE IDS ---
OUTPUT_DEVICE_ID = "bf9fbc2c5e5a6dd45bvkvq"   # 16A _ Output Line (Load)
CHARGING_DEVICE_ID = "bf64784528673eddf0h0u8" # 20A _ Charging Line (Grid In)
MAIN_DEVICE_ID = "bf857f4b4a51ea82a60qmx"     # বাসার মেইন লাইন।

# --- EXACT PINPOINT SITE: KHANKA SHORIF JAME MASJID, CHILAHATI ---
SITE_LAT = 26.3110
SITE_LON = 88.7840
ARRAY_WATT = 800.0 # 2x REC 400W

token_cache = {"access_token": "", "expire_time": 0}
weather_cache = {"t": 0, "factor": 1.0, "code": 0, "rain": 0.0, "temp": 28.0, "cloud": 0}
device_cache = {"t": 0, "data": {}}

# লাইভ লোডশেডিং ও মিটারিং ট্র্যাকার
grid_tracker = {
    "date": str(date.today()),
    "on_seconds": 0,
    "off_seconds": 0,
    "outages": 0,
    "last_state": None,
    "last_tick": time.time()
}

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
        with urllib.request.urlopen(req, timeout=5) as response:
            res = json.loads(response.read().decode())
            if res.get("success"):
                token_cache["access_token"] = res["result"]["access_token"]
                token_cache["expire_time"] = now + res["result"]["expire_time"]
                return token_cache["access_token"]
    except Exception as e:
        print(f"Token error: {e}")
    return None

def fetch_single_device(device_id):
    token = get_access_token()
    if not token:
        return {"online": False, "power": 0.0, "voltage": 0.0, "current": 0.0}
    path = f"/v1.0/devices/{device_id}"
    sign, t = calc_sign("GET", path, access_token=token)
    headers = {"client_id": CLIENT_ID, "access_token": token, "sign": sign, "t": t, "sign_method": "HMAC-SHA256"}
    req = urllib.request.Request(f"{BASE_URL}{path}", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            res = json.loads(response.read().decode())
            if res.get("success"):
                result = res.get("result", {})
                is_online = result.get("online", False) or result.get("is_online", False)
                if not is_online:
                    return {"online": False, "power": 0.0, "voltage": 0.0, "current": 0.0}
                
                status_list = result.get("status", [])
                raw_power, raw_voltage, raw_current = 0.0, 0.0, 0.0
                switch_on = True
                for item in status_list:
                    code, val = item.get("code"), item.get("value")
                    if code in ["cur_power", "power"]: raw_power = float(val)
                    elif code in ["cur_voltage", "voltage"]: raw_voltage = float(val)
                    elif code in ["cur_current", "current"]: raw_current = float(val)
                    elif code in ["switch", "switch_1"]: switch_on = bool(val)
                
                if not switch_on:
                    return {"online": True, "power": 0.0, "voltage": 0.0, "current": 0.0}
                
                power_w = raw_power / 10.0
                volt_v = raw_voltage / 10.0 if raw_voltage > 1000 else raw_voltage
                curr_a = raw_current / 1000.0 if raw_current > 100 else raw_current
                return {"online": True, "power": round(power_w, 1), "voltage": round(volt_v, 1), "current": round(curr_a, 2)}
    except Exception as e:
        print(f"Device error ({device_id}): {e}")
    return {"online": False, "power": 0.0, "voltage": 0.0, "current": 0.0}

def get_all_devices_cached():
    now = time.time()
    if now - device_cache["t"] < 3 and device_cache["data"]:
        return device_cache["data"]
    
    out_d = fetch_single_device(OUTPUT_DEVICE_ID)
    in_d = fetch_single_device(CHARGING_DEVICE_ID)
    main_d = fetch_single_device(MAIN_DEVICE_ID)
    
    data = {"out": out_d, "in": in_d, "main": main_d}
    device_cache["t"] = now
    device_cache["data"] = data
    return data

def get_sun_elevation(lat=SITE_LAT, lon=SITE_LON):
    now = datetime.utcnow()
    day_of_year = now.timetuple().tm_yday
    dec = 23.45 * math.sin(math.radians((360 / 365) * (day_of_year - 81)))
    local_hour = (now.hour + 6) + now.minute / 60.0
    hour_angle = (local_hour - 12.0) * 15.0
    lat_rad, dec_rad, ha_rad = math.radians(lat), math.radians(dec), math.radians(hour_angle)
    sin_el = math.sin(lat_rad) * math.sin(dec_rad) + math.cos(lat_rad) * math.cos(dec_rad) * math.cos(ha_rad)
    return round(math.degrees(math.asin(max(-1.0, min(1.0, sin_el)))), 1)

def get_live_weather():
    now = time.time()
    if now - weather_cache["t"] < 600:
        return weather_cache
    try:
        url = f"https://api.open-meteo.com/v1/forecast?latitude={SITE_LAT}&longitude={SITE_LON}&current=temperature_2m,relative_humidity_2m,weather_code,cloud_cover,precipitation&timezone=Asia%2FDhaka"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            curr = data.get("current", {})
            code = curr.get("weather_code", 0)
            cloud = curr.get("cloud_cover", 0)
            rain = curr.get("precipitation", 0.0)
            
            if code in [95, 96, 99]: f = 0.12
            elif code in [55, 63, 65, 81, 82]: f = 0.18
            elif code in [51, 53, 61, 80]: f = 0.25
            elif code == 3 or cloud > 85: f = 0.40
            elif code == 2 or cloud > 50: f = 0.70
            elif code == 1: f = 0.90
            else: f = 1.0

            if rain > 0.2:
                f = min(f, 0.20)

            weather_cache["t"] = now
            weather_cache["factor"] = f
            weather_cache["code"] = code
            weather_cache["rain"] = rain
            weather_cache["cloud"] = cloud
            weather_cache["temp"] = curr.get("temperature_2m", 28.0)
    except Exception as e:
        print(f"Weather error: {e}")
    return weather_cache

def update_grid_tracker(is_grid):
    now = time.time()
    today_str = str(date.today())
    if grid_tracker["date"] != today_str:
        grid_tracker["date"] = today_str
        grid_tracker["on_seconds"] = 0
        grid_tracker["off_seconds"] = 0
        grid_tracker["outages"] = 0
        grid_tracker["last_state"] = None
        grid_tracker["last_tick"] = now

    dt = min(max(now - grid_tracker["last_tick"], 0), 60)
    grid_tracker["last_tick"] = now

    if is_grid:
        grid_tracker["on_seconds"] += dt
    else:
        grid_tracker["off_seconds"] += dt

    if grid_tracker["last_state"] is True and is_grid is False:
        grid_tracker["outages"] += 1
    grid_tracker["last_state"] = is_grid

# --- API ENDPOINTS ---

@app.route("/api/states")
def ha_states():
    devs = get_all_devices_cached()
    out_data = devs["out"]
    in_data = devs["in"]
    main_data = devs["main"]

    now_iso = datetime.utcnow().isoformat() + "Z"
    is_grid = (in_data.get("voltage", 0) > 120 or main_data.get("voltage", 0) > 120 or in_data.get("online", False))
    update_grid_tracker(is_grid)

    load_w = out_data.get("power", 0.0)
    grid_w = in_data.get("power", 0.0)
    main_w = main_data.get("power", 0.0)

    sun_el = get_sun_elevation()
    wx = get_live_weather()

    if sun_el <= 2:
        weather_potential = 0.0
    else:
        sin_el = math.sin(math.radians(sun_el))
        clear_sky_pot = ARRAY_WATT * sin_el * 0.82
        weather_potential = clear_sky_pot * wx["factor"]

    if not is_grid:
        pv_w = min(load_w, max(0.0, weather_potential))
        dis_w = max(0.0, (load_w / 0.90 + 35.0) - pv_w)
        ch_w = 0.0
    else:
        if grid_w <= 5:
            pv_w = min(load_w, max(0.0, weather_potential))
        else:
            calc_pv = max(0.0, load_w - grid_w * 0.94)
            pv_w = min(calc_pv, max(0.0, weather_potential))
        dis_w = 0.0
        ch_w = max(0.0, pv_w - load_w / 0.90)

    on_hours = round(grid_tracker["on_seconds"] / 3600.0, 1)
    off_hours = round(grid_tracker["off_seconds"] / 3600.0, 1)

    states = [
        {"entity_id": "sensor.baasaar_mein_laain_power", "state": str(main_w), "last_updated": now_iso},
        {"entity_id": "sensor.baasaar_mein_laain_voltage", "state": str(main_data.get("voltage", 0.0)), "last_updated": now_iso},
        {"entity_id": "sensor.baasaar_mein_laain_current", "state": str(main_data.get("current", 0.0)), "last_updated": now_iso},
        {"entity_id": "sensor.20a_charging_line_power", "state": str(grid_w), "last_updated": now_iso},
        {"entity_id": "sensor.20a_charging_line_voltage", "state": str(in_data.get("voltage", 0.0)), "last_updated": now_iso},
        {"entity_id": "sensor.20a_charging_line_current", "state": str(in_data.get("current", 0.0)), "last_updated": now_iso},
        {"entity_id": "sensor.16a_output_line_power", "state": str(load_w), "last_updated": now_iso},
        {"entity_id": "sensor.16a_output_line_voltage", "state": str(out_data.get("voltage", 0.0)), "last_updated": now_iso},
        {"entity_id": "sensor.16a_output_line_current", "state": str(out_data.get("current", 0.0)), "last_updated": now_iso},
        {"entity_id": "binary_sensor.energy_mate_v4_grid_present", "state": "on" if is_grid else "off", "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_grid_on_today", "state": str(on_hours), "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_grid_off_today", "state": str(off_hours), "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_grid_outages_today", "state": str(grid_tracker["outages"]), "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_pv_estimated_power", "state": str(round(pv_w, 1)), "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_battery_charge_power", "state": str(round(ch_w, 1)), "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_battery_discharge_power", "state": str(round(dis_w, 1)), "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_pack_version", "state": "V10-Cloud", "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_poll_heartbeat", "state": str(int(time.time())), "last_updated": now_iso},
        {"entity_id": "sun.sun", "state": "above_horizon" if sun_el > 0 else "below_horizon", "attributes": {"elevation": sun_el}},
        {"entity_id": "input_number.energy_final_site_lat", "state": str(SITE_LAT)},
        {"entity_id": "input_number.energy_final_site_lon", "state": str(SITE_LON)},
        {"entity_id": "input_number.energy_final_panel_azimuth", "state": "180"},
        {"entity_id": "input_number.energy_final_panel_tilt", "state": "10"},
        {"entity_id": "sensor.energy_mate_v4_effective_pv_array_power", "state": str(ARRAY_WATT)},
    ]
    return jsonify(states)

@app.route("/api/services/<path:subpath>", methods=["GET", "POST"])
def ha_services(subpath):
    return jsonify({"success": True})

@app.route("/api/manifest")
def ha_manifest():
    return jsonify({"version_string": "Tuya-Cloud-Live-V10"})

@app.route("/")
def index():
    if os.path.exists("energy.html"):
        with open("energy.html", "r", encoding="utf-8") as f:
            html = f.read()
        html = html.replace('const BUNDLED_URL_L="http://192.168.68.71";', 'const BUNDLED_URL_L=window.location.origin;')
        html = html.replace('const BUNDLED_URL_R="https://j6wj3jkik6fnfjkznprustr5dkvxezwi.ui.nabu.casa";', 'const BUNDLED_URL_R=window.location.origin;')
        html = html.replace('const BUNDLED_TOKEN="', 'const BUNDLED_TOKEN="cloud_')
        return html
    return "<h3>Please upload energy.html to your GitHub repository root!</h3>"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
