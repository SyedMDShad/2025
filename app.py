import os
import time
import hashlib
import hmac
import json
from datetime import datetime, date
import urllib.request
from flask import Flask, jsonify, request

app = Flask(__name__)

CLIENT_ID = os.environ.get("TUYA_CLIENT_ID", "hcdys9fmcvcchyrsjvqf")
CLIENT_SECRET = os.environ.get("TUYA_CLIENT_SECRET", "c58da75d76124629a490905aac55e586")
BASE_URL = os.environ.get("TUYA_BASE_URL", "https://openapi.tuyaeu.com")

OUTPUT_DEVICE_ID = "bf9fbc2c5e5a6dd45bvkvq"   # 16A Output Line (Load)
CHARGING_DEVICE_ID = "bf64784528673eddf0h0u8" # 20A Charging Line (Grid In)
MAIN_DEVICE_ID = "bf857f4b4a51ea82a60qmx"     # বাসার মেইন লাইন

token_cache = {"access_token": "", "expire_time": 0}
device_cache = {
    "t": 0,
    "out": {"online": True, "power": 120.0, "voltage": 228.0, "current": 0.6},
    "in": {"online": False, "power": 0.0, "voltage": 0.0, "current": 0.0},
    "main": {"online": False, "power": 0.0, "voltage": 0.0, "current": 0.0}
}

user_settings = {
    "battery_soc": 85.0,
    "battery_amps": 21.0,
    "battery_voltage": 13.5,
    "battery_capacity_ah": 200,
    "solar_mode": "auto",
    "manual_solar_watts": 320.0,
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
                if not (result.get("online", False) or result.get("is_online", False)):
                    return {"online": False, "power": 0.0, "voltage": 0.0, "current": 0.0}
                raw_power, raw_voltage, raw_current = 0.0, 0.0, 0.0
                for item in result.get("status", []):
                    code, val = item.get("code"), item.get("value")
                    if code in ["cur_power", "power"]: raw_power = float(val)
                    elif code in ["cur_voltage", "voltage"]: raw_voltage = float(val)
                    elif code in ["cur_current", "current"]: raw_current = float(val)
                power_w = raw_power / 10.0
                volt_v = raw_voltage / 10.0 if raw_voltage > 1000 else raw_voltage
                curr_a = raw_current / 1000.0 if raw_current > 100 else raw_current
                return {"online": True, "power": round(power_w, 1), "voltage": round(volt_v, 1), "current": round(curr_a, 2)}
    except Exception as e:
        print(f"Device error: {e}")
    return None

def get_devices():
    now = time.time()
    if now - device_cache["t"] < 3: return device_cache
    for k, dev_id in [("out", OUTPUT_DEVICE_ID), ("in", CHARGING_DEVICE_ID), ("main", MAIN_DEVICE_ID)]:
        res = fetch_single_device(dev_id)
        if res is not None: device_cache[k] = res
    device_cache["t"] = now
    return device_cache

@app.route("/api/states")
def ha_states():
    devs = get_devices()
    now_iso = datetime.utcnow().isoformat() + "Z"
    load_w = devs["out"].get("power", 0.0)
    grid_w = devs["in"].get("power", 0.0)
    main_w = devs["main"].get("power", 0.0)
    is_grid = (devs["in"].get("voltage", 0) > 120 or devs["main"].get("voltage", 0) > 120)

    # সোলার হিসাব: ব্যাটারি চার্জিং কারেন্ট (~২১A @ ১৩.৫V = ২৮৩W) + লোড
    chg_amps = user_settings.get("battery_amps", 21.0)
    bat_chg_w = round(chg_amps * 13.5, 1)
    solar_pv_w = round(load_w + bat_chg_w, 1)

    states = [
        {"entity_id": "sensor.16a_output_line_power", "state": str(load_w), "last_updated": now_iso},
        {"entity_id": "sensor.20a_charging_line_power", "state": str(grid_w), "last_updated": now_iso},
        {"entity_id": "sensor.baasaar_mein_laain_power", "state": str(main_w), "last_updated": now_iso},
        {"entity_id": "sensor.16a_output_line_voltage", "state": str(devs["out"].get("voltage", 228.0)), "last_updated": now_iso},
        {"entity_id": "sensor.20a_charging_line_voltage", "state": str(devs["in"].get("voltage", 228.0)), "last_updated": now_iso},
        {"entity_id": "sensor.baasaar_mein_laain_voltage", "state": str(devs["main"].get("voltage", 228.0)), "last_updated": now_iso},
        {"entity_id": "binary_sensor.energy_mate_grid_available", "state": "on" if is_grid else "off", "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_inverter_output_power", "state": str(load_w), "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_inverter_input_power", "state": str(grid_w), "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_main_grid_power", "state": str(main_w), "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_pv_power", "state": str(solar_pv_w), "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_battery_soc", "state": str(user_settings["battery_soc"]), "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_battery_net_current", "state": str(chg_amps), "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_inverter_own_consumption", "state": "45", "last_updated": now_iso}
    ]
    return jsonify(states)

@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "POST":
        data = request.get_json(force=True, silent=True) or {}
        user_settings.update(data)
        return jsonify({"success": True, "settings": user_settings})
    return jsonify(user_settings)

@app.route("/")
def home():
    for name in ["Energy.html", "energy.html", "index.html"]:
        if os.path.exists(name):
            with open(name, "r", encoding="utf-8") as f:
                return f.read()
    return "<h3>Energy.html not found in repository!</h3>"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
