import os
import time
import hashlib
import hmac
import json
import math
from datetime import datetime, date
import urllib.request
from flask import Flask, jsonify, request

app = Flask(__name__)

# --- TUYA API CREDENTIALS ---
CLIENT_ID = os.environ.get("TUYA_CLIENT_ID", "hcdys9fmcvcchyrsjvqf")
CLIENT_SECRET = os.environ.get("TUYA_CLIENT_SECRET", "c58da75d76124629a490905aac55e586")
BASE_URL = os.environ.get("TUYA_BASE_URL", "https://openapi.tuyaeu.com")

# --- TUYA DEVICE IDS ---
OUTPUT_DEVICE_ID = "bf9fbc2c5e5a6dd45bvkvq"   # 16A _ Output Line (Load)
CHARGING_DEVICE_ID = "bf64784528673eddf0h0u8" # 20A _ Charging Line (Grid In)
MAIN_DEVICE_ID = "bf857f4b4a51ea82a60qmx"     # বাসার মেইন লাইন

SITE_LAT = 26.2439
SITE_LON = 88.7967
ARRAY_WATT = 800.0

token_cache = {"access_token": "", "expire_time": 0}

device_cache = {
    "t": 0,
    "out": {"online": True, "power": 120.0, "voltage": 228.0, "current": 0.6},
    "in": {"online": False, "power": 0.0, "voltage": 0.0, "current": 0.0},
    "main": {"online": False, "power": 0.0, "voltage": 0.0, "current": 0.0}
}

grid_tracker = {
    "date": str(date.today()),
    "on_seconds": 0,
    "off_seconds": 0,
    "outages": 0,
    "last_state": None,
    "last_tick": time.time()
}

energy_acc = {
    "date": str(date.today()),
    "last_t": time.time(),
    "main_kwh": 0.0,
    "in_kwh": 0.0,
    "out_kwh": 0.0,
    "pv_kwh": 0.0,
    "ch_kwh": 0.0,
    "dis_kwh": 0.0,
    "pv_peak_w": 0.0
}

user_settings = {
    "battery_soc": 85.0,
    "battery_amps": 21.0,
    "battery_voltage": 13.5,
    "battery_capacity_ah": 200,
    "tariff_rate": 15.00
}

def calc_sign(method, path, body="", access_token=""):
    t = str(int(time.time() * 1000))
    body_hash = hashlib.sha256(body.encode('utf-8')).hexdigest()
    string_to_sign = f"{method}\n{body_hash}\n\n{path}"
    to_sign = f"{CLIENT_ID}{access_token}{t}{string_to_sign}" if access_token else f"{CLIENT_ID}{t}{string_to_sign}"
    return hmac.new(CLIENT_SECRET.encode('utf-8'), to_sign.encode('utf-8'), hashlib.sha256).hexdigest().upper(), t

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
    if not token: return None
    path = f"/v1.0/devices/{device_id}"
    sign, t = calc_sign("GET", path, access_token=token)
    headers = {"client_id": CLIENT_ID, "access_token": token, "sign": sign, "t": t, "sign_method": "HMAC-SHA256"}
    req = urllib.request.Request(f"{BASE_URL}{path}", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=4) as response:
            res = json.loads(response.read().decode())
            if res.get("success"):
                result = res.get("result", {})
                is_online = result.get("online", False) or result.get("is_online", False)
                if not is_online:
                    return {"online": False, "power": 0.0, "voltage": 0.0, "current": 0.0}
                raw_power, raw_voltage, raw_current = 0.0, 0.0, 0.0
                switch_on = True
                for item in result.get("status", []):
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
    return None

def get_devices():
    now = time.time()
    if now - device_cache["t"] < 3: return device_cache
    for k, dev_id in [("out", OUTPUT_DEVICE_ID), ("in", CHARGING_DEVICE_ID), ("main", MAIN_DEVICE_ID)]:
        res = fetch_single_device(dev_id)
        if res is not None: device_cache[k] = res
    device_cache["t"] = now
    return device_cache

def get_sun_elevation():
    now = datetime.utcnow()
    day_of_year = now.timetuple().tm_yday
    dec = 23.45 * math.sin(math.radians((360 / 365) * (day_of_year - 81)))
    local_hour = (now.hour + 6) + now.minute / 60.0
    hour_angle = (local_hour - 12.0) * 15.0
    lat_rad, dec_rad, ha_rad = math.radians(SITE_LAT), math.radians(dec), math.radians(hour_angle)
    sin_el = math.sin(lat_rad) * math.sin(dec_rad) + math.cos(lat_rad) * math.cos(dec_rad) * math.cos(ha_rad)
    return round(math.degrees(math.asin(max(-1.0, min(1.0, sin_el)))), 1)

def update_trackers(is_grid, main_w, in_w, out_w, pv_w, ch_w, dis_w):
    now = time.time()
    today_str = str(date.today())
    if grid_tracker["date"] != today_str:
        grid_tracker["date"] = today_str
        grid_tracker["on_seconds"] = 0
        grid_tracker["off_seconds"] = 0
        grid_tracker["outages"] = 0
        grid_tracker["last_state"] = None
        grid_tracker["last_tick"] = now

    dt = min(max(now - grid_tracker["last_tick"], 0), 30)
    grid_tracker["last_tick"] = now

    if is_grid: grid_tracker["on_seconds"] += dt
    else: grid_tracker["off_seconds"] += dt

    if grid_tracker["last_state"] is True and is_grid is False:
        grid_tracker["outages"] += 1
    grid_tracker["last_state"] = is_grid

    if energy_acc["date"] != today_str:
        energy_acc["date"] = today_str
        energy_acc["main_kwh"] = 0.0
        energy_acc["in_kwh"] = 0.0
        energy_acc["out_kwh"] = 0.0
        energy_acc["pv_kwh"] = 0.0
        energy_acc["ch_kwh"] = 0.0
        energy_acc["dis_kwh"] = 0.0
        energy_acc["pv_peak_w"] = 0.0

    dt_h = min(max(now - energy_acc["last_t"], 0), 30) / 3600.0
    energy_acc["last_t"] = now

    energy_acc["main_kwh"] += (main_w / 1000.0) * dt_h
    energy_acc["in_kwh"] += (in_w / 1000.0) * dt_h
    energy_acc["out_kwh"] += (out_w / 1000.0) * dt_h
    energy_acc["pv_kwh"] += (pv_w / 1000.0) * dt_h
    energy_acc["ch_kwh"] += (ch_w / 1000.0) * dt_h
    energy_acc["dis_kwh"] += (dis_w / 1000.0) * dt_h
    if pv_w > energy_acc["pv_peak_w"]: energy_acc["pv_peak_w"] = pv_w

@app.route("/api/states")
def ha_states():
    devs = get_devices()
    now_iso = datetime.utcnow().isoformat() + "Z"
    
    out_p = devs["out"].get("power", 0.0)
    in_p = devs["in"].get("power", 0.0)
    main_p = devs["main"].get("power", 0.0)
    
    out_v = devs["out"].get("voltage", 228.0)
    in_v = devs["in"].get("voltage", 228.0)
    main_v = devs["main"].get("voltage", 228.0)

    out_a = devs["out"].get("current", 0.0)
    in_a = devs["in"].get("current", 0.0)
    main_a = devs["main"].get("current", 0.0)

    is_grid = (in_v > 120 or main_v > 120)
    sun_el = get_sun_elevation()

    # সোলার হিসাব: ব্যাটারি চার্জিং কারেন্ট (~২১A @ ১৩.৫V = ২৮৩W) + লোড
    charge_amp = user_settings.get("battery_amps", 21.0)
    batt_soc = user_settings.get("battery_soc", 85.0)

    if sun_el > 0:
        bat_chg_w = round(charge_amp * 13.5, 1)
        dis_w = 0.0
        if not is_grid:
            solar_pv_w = round(out_p + bat_chg_w, 1)
        else:
            load_covered = max(0.0, out_p - in_p)
            solar_pv_w = round(load_covered + bat_chg_w, 1)
        ch_w = bat_chg_w
        current_amp = charge_amp
    else:
        solar_pv_w = 0.0
        if is_grid and in_p > 20:
            ch_w = round(in_p * 0.85, 1)
            dis_w = 0.0
            current_amp = round(ch_w / 13.5, 1)
        elif not is_grid:
            ch_w = 0.0
            dis_w = out_p + 35.0
            current_amp = round(-(out_p / 12.0), 1)
        else:
            ch_w, dis_w, current_amp = 0.0, 0.0, 0.0

    update_trackers(is_grid, main_p, in_p, out_p, solar_pv_w, ch_w, dis_w)

    on_h = round(grid_tracker["on_seconds"] / 3600.0, 1)
    off_h = round(grid_tracker["off_seconds"] / 3600.0, 1)

    states = [
        {"entity_id": "sensor.baasaar_mein_laain_power", "state": str(main_p), "last_updated": now_iso},
        {"entity_id": "sensor.baasaar_mein_laain_current", "state": str(main_a), "last_updated": now_iso},
        {"entity_id": "sensor.baasaar_mein_laain_voltage", "state": str(main_v), "last_updated": now_iso},
        
        {"entity_id": "sensor.20a_charging_line_power", "state": str(in_p), "last_updated": now_iso},
        {"entity_id": "sensor.20a_charging_line_current", "state": str(in_a), "last_updated": now_iso},
        {"entity_id": "sensor.20a_charging_line_voltage", "state": str(in_v), "last_updated": now_iso},

        {"entity_id": "sensor.16a_output_line_power", "state": str(out_p), "last_updated": now_iso},
        {"entity_id": "sensor.16a_output_line_current", "state": str(out_a), "last_updated": now_iso},
        {"entity_id": "sensor.16a_output_line_voltage", "state": str(out_v), "last_updated": now_iso},

        {"entity_id": "binary_sensor.energy_mate_v4_grid_present", "state": "on" if is_grid else "off", "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_pv_estimated_power", "state": str(solar_pv_w), "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_battery_charge_power", "state": str(ch_w), "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_battery_discharge_power", "state": str(dis_w), "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_battery_estimated_soc", "state": str(batt_soc), "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_battery_current", "state": str(current_amp), "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_battery_net_current", "state": str(current_amp), "last_updated": now_iso},
        
        {"entity_id": "sensor.energy_mate_v4_main_daily", "state": str(round(energy_acc["main_kwh"], 3)), "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_input_daily", "state": str(round(energy_acc["in_kwh"], 3)), "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_output_daily", "state": str(round(energy_acc["out_kwh"], 3)), "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_pv_daily", "state": str(round(energy_acc["pv_kwh"], 3)), "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_battery_charge_daily", "state": str(round(energy_acc["ch_kwh"], 3)), "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_battery_discharge_daily", "state": str(round(energy_acc["dis_kwh"], 3)), "last_updated": now_iso},
        
        {"entity_id": "sensor.energy_mate_v4_grid_on_today", "state": str(on_h), "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_grid_off_today", "state": str(off_h), "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_grid_outages_today", "state": str(grid_tracker["outages"]), "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_pv_peak_today", "state": str(round(energy_acc["pv_peak_w"], 0)), "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_inverter_overhead_power", "state": "45", "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_pack_version", "state": "V10", "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_poll_heartbeat", "state": str(int(time.time())), "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_effective_pv_array_power", "state": str(ARRAY_WATT), "last_updated": now_iso},

        {"entity_id": "sun.sun", "state": "above_horizon" if sun_el > 0 else "below_horizon", "attributes": {"elevation": sun_el}},
        {"entity_id": "input_number.energy_final_site_lat", "state": str(SITE_LAT)},
        {"entity_id": "input_number.energy_final_site_lon", "state": str(SITE_LON)},
        {"entity_id": "input_number.energy_final_panel_azimuth", "state": "180"},
        {"entity_id": "input_number.energy_final_panel_tilt", "state": "10"},
        {"entity_id": "input_number.energy_final_manual_soc", "state": str(batt_soc)},
        {"entity_id": "input_boolean.energy_final_battery_manual_soc", "state": "off"}
    ]
    return jsonify(states)

@app.route("/api/settings", methods=["GET", "POST"])
def handle_settings():
    if request.method == "POST":
        data = request.get_json(force=True, silent=True) or {}
        user_settings.update(data)
        return jsonify({"success": True, "settings": user_settings})
    return jsonify(user_settings)

@app.route("/api/services/<path:subpath>", methods=["GET", "POST"])
def ha_services(subpath):
    data = request.get_json(force=True, silent=True) or {}
    if "input_number/set_value" in subpath:
        val = data.get("value")
        if val is not None: user_settings["battery_soc"] = float(val)
    return jsonify({"success": True})

@app.route("/api/manifest")
def ha_manifest():
    return jsonify({"version_string": "Sako-Lite-V10-Cloud"})

@app.route("/")
def home():
    for name in ["Energy.html", "energy.html", "index.html"]:
        if os.path.exists(name):
            with open(name, "r", encoding="utf-8") as f: return f.read()
    return "<h3>Sako Lite V10: Please upload energy.html to repository root!</h3>"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
