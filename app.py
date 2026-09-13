import os
import time
import hashlib
import hmac
import json
import math
import threading
import urllib.request
import urllib.error
from datetime import datetime, date, timezone, timedelta
from flask import Flask, jsonify, request

app = Flask(__name__)

# --- TUYA API CREDENTIALS ---
# Read from Render env vars first; fall back to the project keys so the app
# works out of the box. (If you ever rotate the secret, update it here too
# or in Render > Environment.)
CLIENT_ID = os.environ.get("TUYA_CLIENT_ID", "") or "hcdys9fmcvcchyrsjvqf"
CLIENT_SECRET = os.environ.get("TUYA_CLIENT_SECRET", "") or "c58da75d76124629a490905aac55e586"
BASE_URL = os.environ.get("TUYA_BASE_URL", "https://openapi.tuyaeu.com")
# Optional shared key required to change settings via /api/settings (public URL!)
SET_SECRET = os.environ.get("SET_SECRET", "")

OUTPUT_DEVICE_ID = "bf9fbc2c5e5a6dd45bvkvq"   # 16A Output Line (Load)
CHARGING_DEVICE_ID = "bf64784528673eddf0h0u8" # 20A Charging Line (Grid In)
MAIN_DEVICE_ID = "bf857f4b4a51ea82a60qmx"     # Main Grid Line

SITE_LAT = 26.2439
SITE_LON = 88.7967
ARRAY_WATT = 800.0          # PV array size (for the MPPT estimate)
INV_OVH_DEFAULT = 75.0      # inverter self-consumption (W) — user-settable, normally 50-150
TZ_DHK = timezone(timedelta(hours=6))  # Dhaka local time for peak timestamps
BANK_V = 12.0                # nominal bank voltage (for Wh -> Ah)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

token_cache = {"access_token": "", "expire_time": 0}
state_lock = threading.Lock()
diag = {"last_error": "never polled yet", "last_ok": 0.0}

device_cache = {
    "t": 0.0,
    "out": {"online": False, "power": 0.0, "voltage": 0.0, "current": 0.0},
    "in": {"online": False, "power": 0.0, "voltage": 0.0, "current": 0.0},
    "main": {"online": False, "power": 0.0, "voltage": 0.0, "current": 0.0},
    "connected_to_tuya": False
}

# user_settings: the ONLY manual inputs left — they are baselines/caps, not displayed fake data
user_settings = {
    "battery_soc": 85.0,             # auto-SoC baseline (%), app drifts it live from charge/discharge
    "battery_voltage": 13.5,
    "battery_capacity_ah": 200,      # 12V bank
    "inv_own_use": INV_OVH_DEFAULT,  # inverter self-consumption (W), normally 50-150
    "grid_charge_cap_a": 10.0,       # grid->battery charge cap (A) — inverter allows max 10A (7-9A avg)
    "solar_charge_cap_a": 30.0,      # solar->battery charge cap (A) — MPPT max 20-30A
    "solar_mode": "auto",            # auto = derived from meters (default)
    "manual_solar_watts": 320.0,     # only used when solar_mode == "manual"
}

grid_tracker = {
    "date": str(date.today()),
    "on_seconds": 0.0,
    "off_seconds": 0.0,
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
    "pv_peak_w": 0.0, "pv_peak_t": "--",
    "main_peak_w": 0.0, "main_peak_t": "--",
    "in_peak_w": 0.0, "in_peak_t": "--",
    "out_peak_w": 0.0, "out_peak_t": "--",
    "ch_peak_a": 0.0, "ch_peak_t": "--",
    "dis_peak_a": 0.0, "dis_peak_t": "--"
}

# auto SoC state (drifts live from the estimated charge/discharge power)
soc_state = {
    "soc": user_settings["battery_soc"],
    "last_t": time.time(),
    "last_reset_day": str(date.today())
}

# ---------------------------------------------------------------- Tuya API
def calc_sign(method, path, body="", access_token=""):
    t = str(int(time.time() * 1000))
    body_hash = hashlib.sha256(body.encode('utf-8')).hexdigest()
    string_to_sign = f"{method}\n{body_hash}\n\n{path}"
    to_sign = f"{CLIENT_ID}{access_token}{t}{string_to_sign}" if access_token else f"{CLIENT_ID}{t}{string_to_sign}"
    return hmac.new(CLIENT_SECRET.encode('utf-8'), to_sign.encode('utf-8'), hashlib.sha256).hexdigest().upper(), t

def _http_json(path, access_token=""):
    sign, t = calc_sign("GET", path, access_token=access_token)
    headers = {"client_id": CLIENT_ID, "sign": sign, "t": t, "sign_method": "HMAC-SHA256"}
    if access_token:
        headers["access_token"] = access_token
    req = urllib.request.Request(f"{BASE_URL}{path}", headers=headers)
    with urllib.request.urlopen(req, timeout=4.0) as response:
        return json.loads(response.read().decode())

def get_access_token():
    now = time.time()
    if token_cache["access_token"] and token_cache["expire_time"] > now + 60:
        return token_cache["access_token"]
    try:
        res = _http_json("/v1.0/token?grant_type=1")
        if res.get("success"):
            token_cache["access_token"] = res["result"]["access_token"]
            token_cache["expire_time"] = now + float(res["result"].get("expire_time", 1800))
            return token_cache["access_token"]
        diag["last_error"] = "token API: " + str(res.get("msg", "no success flag"))
        token_cache["expire_time"] = now + 30
    except urllib.error.HTTPError as e:
        diag["last_error"] = f"token API HTTP {e.code}"
        if e.code in (401, 429):
            token_cache["expire_time"] = now + 30   # back off for 30s
        token_cache["access_token"] = ""
    except Exception as e:
        diag["last_error"] = f"token API network error: {type(e).__name__}"
    return None

def _parse_device_status(status_list):
    raw_power, raw_voltage, raw_current = 0.0, 0.0, 0.0
    switch_on = True
    for item in status_list:
        code, val = item.get("code"), item.get("value")
        if code in ("cur_power", "power"):
            raw_power = float(val)
        elif code in ("cur_voltage", "voltage"):
            raw_voltage = float(val)
        elif code in ("cur_current", "current"):
            raw_current = float(val)
        elif code in ("switch", "switch_1"):
            switch_on = val is True or (isinstance(val, str) and val.lower() == "true") or val == 1
    if not switch_on:
        return {"online": True, "power": 0.0, "voltage": 0.0, "current": 0.0}
    # These plugs report raw values in fixed units: power 0.1W, voltage 0.1V, current 0.001A
    power_w = raw_power / 10.0
    volt_v = raw_voltage / 10.0
    curr_a = raw_current / 1000.0
    return {"online": True, "power": round(power_w, 1), "voltage": round(volt_v, 1), "current": round(curr_a, 2)}

def fetch_single_device(device_id):
    token = get_access_token()
    if not token:
        return None
    try:
        res = _http_json(f"/v1.0/devices/{device_id}/status", access_token=token)
        if res.get("success"):
            return _parse_device_status(res.get("result", []))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            token_cache["access_token"] = ""   # force re-token next round
    except Exception:
        pass
    return None

def poll_round():
    """Fetch all 3 devices and update the shared cache."""
    got = False
    for key, dev_id in (("out", OUTPUT_DEVICE_ID), ("in", CHARGING_DEVICE_ID), ("main", MAIN_DEVICE_ID)):
        res = fetch_single_device(dev_id)
        if res is not None:
            with state_lock:
                device_cache[key] = res
            got = True
    if got:
        diag["last_error"] = "ok"
        diag["last_ok"] = time.time()
    else:
        diag["last_error"] = "polled but no device data (token failing or devices offline)"
    with state_lock:
        device_cache["connected_to_tuya"] = got
        device_cache["t"] = time.time() if got else device_cache["t"]

# ---------------------------------------------------------------- background poller
_poll_started = False
_poll_lock = threading.Lock()

def background_tuya_poller():
    fails = 0
    while True:
        try:
            poll_round()
            fails = 0
        except Exception:
            fails += 1
        time.sleep(5 if fails < 3 else 30)

def ensure_poller():
    global _poll_started
    with _poll_lock:
        if _poll_started:
            return
        _poll_started = True
        threading.Thread(target=background_tuya_poller, daemon=True).start()
        print("Tuya background poller started", flush=True)

def post_fork(server, worker):
    ensure_poller()

# ---------------------------------------------------------------- helpers
def get_sun():
    """Return (sun_elevation_deg, dayF 0..1) for the site (Dhaka = UTC+6)."""
    now = datetime.now(timezone.utc)
    day_of_year = now.timetuple().tm_yday
    dec = 23.45 * math.sin(math.radians((360 / 365) * (day_of_year - 81)))
    local_hour = (now.hour + 6) + now.minute / 60.0
    hour_angle = (local_hour - 12.0) * 15.0
    lat = math.radians(SITE_LAT)
    d = math.radians(dec)
    h = math.radians(hour_angle)
    sin_el = math.sin(lat) * math.sin(d) + math.cos(lat) * math.cos(d) * math.cos(h)
    el = math.degrees(math.asin(max(-1.0, min(1.0, sin_el))))
    # dayF: 0 at horizon, 1 at solar noon (ratio of current to peak sin-elevation)
    sin_max = math.cos(lat - d)
    dayF = max(0.0, min(1.0, sin_el / sin_max)) if (sin_el > 0 and sin_max > 0.05) else 0.0
    return round(el, 1), dayF

def _bump_peak(wkey, tkey, val):
    if val > energy_acc[wkey]:
        energy_acc[wkey] = val
        energy_acc[tkey] = datetime.now(TZ_DHK).strftime("%H:%M")

def update_trackers(is_grid, main_w, in_w, out_w, pv_w, ch_w, dis_w, ch_a=0.0, dis_a=0.0):
    now = time.time()
    today_str = str(date.today())
    with state_lock:
        if grid_tracker["date"] != today_str:
            grid_tracker.update({"date": today_str, "on_seconds": 0.0, "off_seconds": 0.0,
                                 "outages": 0, "last_state": None, "last_tick": now})
        dt = min(max(now - grid_tracker["last_tick"], 0.0), 60.0)
        grid_tracker["last_tick"] = now
        if is_grid:
            grid_tracker["on_seconds"] += dt
        else:
            grid_tracker["off_seconds"] += dt
        if grid_tracker["last_state"] is True and is_grid is False:
            grid_tracker["outages"] += 1
        grid_tracker["last_state"] = is_grid

        if energy_acc["date"] != today_str:
            energy_acc.update({"date": today_str, "main_kwh": 0.0, "in_kwh": 0.0, "out_kwh": 0.0,
                               "pv_kwh": 0.0, "ch_kwh": 0.0, "dis_kwh": 0.0,
                               "pv_peak_w": 0.0, "pv_peak_t": "--",
                               "main_peak_w": 0.0, "main_peak_t": "--",
                               "in_peak_w": 0.0, "in_peak_t": "--",
                               "out_peak_w": 0.0, "out_peak_t": "--",
                               "ch_peak_a": 0.0, "ch_peak_t": "--",
                               "dis_peak_a": 0.0, "dis_peak_t": "--"})
        dt_h = min(max(now - energy_acc["last_t"], 0.0), 60.0) / 3600.0
        energy_acc["last_t"] = now
        energy_acc["main_kwh"] += (main_w / 1000.0) * dt_h
        energy_acc["in_kwh"] += (in_w / 1000.0) * dt_h
        energy_acc["out_kwh"] += (out_w / 1000.0) * dt_h
        energy_acc["pv_kwh"] += (pv_w / 1000.0) * dt_h
        energy_acc["ch_kwh"] += (ch_w / 1000.0) * dt_h
        energy_acc["dis_kwh"] += (dis_w / 1000.0) * dt_h
        _bump_peak("pv_peak_w", "pv_peak_t", pv_w)
        _bump_peak("main_peak_w", "main_peak_t", main_w)
        _bump_peak("in_peak_w", "in_peak_t", in_w)
        _bump_peak("out_peak_w", "out_peak_t", out_w)
        _bump_peak("ch_peak_a", "ch_peak_t", abs(ch_a))
        _bump_peak("dis_peak_a", "dis_peak_t", abs(dis_a))

def drift_soc(ch_w, dis_w):
    """Auto SoC: integrate net battery flow. Resets baseline at midnight."""
    now = time.time()
    today_str = str(date.today())
    with state_lock:
        if soc_state["last_reset_day"] != today_str:
            soc_state["soc"] = user_settings["battery_soc"]
            soc_state["last_reset_day"] = today_str
            soc_state["last_t"] = now
        dt_h = min(max(now - soc_state["last_t"], 0.0), 60.0) / 3600.0
        soc_state["last_t"] = now
        cap_wh = max(user_settings["battery_capacity_ah"] * 12.0, 1.0)
        soc_state["soc"] = min(100.0, max(0.0, soc_state["soc"] + ((ch_w * 0.85) - dis_w) * dt_h / cap_wh * 100.0))
        return soc_state["soc"]

# ---------------------------------------------------------------- routes
@app.route("/api/states")
def ha_states():
    ensure_poller()
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    with state_lock:
        t = device_cache["t"]
        conn = device_cache["connected_to_tuya"]
        out_p = device_cache["out"].get("power", 0.0)
        in_p = device_cache["in"].get("power", 0.0)
        main_p = device_cache["main"].get("power", 0.0)
        out_v = device_cache["out"].get("voltage", 0.0)
        in_v = device_cache["in"].get("voltage", 0.0)
        main_v = device_cache["main"].get("voltage", 0.0)
        out_a = device_cache["out"].get("current", 0.0)
        in_a = device_cache["in"].get("current", 0.0)
        main_a = device_cache["main"].get("current", 0.0)

    live = conn and (time.time() - t) < 90   # data is only "live" if poller is fresh
    if not live:
        out_p = in_p = main_p = 0.0
        out_v = in_v = main_v = 0.0
        out_a = in_a = main_a = 0.0

    is_grid = (in_v > 120 or main_v > 120 or in_p > 10 or main_p > 10)
    sun_el, dayF = get_sun()

    solar_mode = user_settings.get("solar_mode", "auto")
    manual_w = user_settings.get("manual_solar_watts", 320.0)
    ovh = user_settings.get("inv_own_use", INV_OVH_DEFAULT)          # inverter own use (W)
    batt_v = user_settings.get("battery_voltage", 13.5)
    grid_cap_w = max(user_settings.get("grid_charge_cap_a", 10.0), 0.0) * batt_v   # grid->battery cap (W)
    solar_cap_w = max(user_settings.get("solar_charge_cap_a", 30.0), 0.0) * batt_v  # solar->battery cap (W)
    pv_dc = ARRAY_WATT * (dayF ** 1.3) * 0.85 if sun_el > 2 else 0.0   # clear-sky MPPT estimate
    full = soc_state["soc"] >= 99.5
    flood_w = 1.0 * batt_v   # battery at 100% still draws 1A flood/trickle charge

    # ---------------- AUTO power model (from live meters) ----------------
    # Inverter balance:  grid-in + battery-dis = load-out + battery-ch + ovh
    # in - out - ovh goes to the battery (capped by grid/solar charge cap);
    # at 100% the battery only takes 1A flood charge, the rest = inverter own use.
    if solar_mode == "manual":
        # explicit override mode (user's choice)
        solar_pv_w = round(manual_w, 1)
        ch_w = round(flood_w, 1) if full else round(min(grid_cap_w, solar_cap_w or grid_cap_w), 1)
        dis_w = 0.0
    elif not live:
        solar_pv_w = ch_w = dis_w = 0.0
    elif is_grid:
        surplus = in_p - out_p - ovh
        if full:
            ch_w, dis_w = flood_w, 0.0             # 100%: only 1A flood charge, rest = own use
        elif surplus > 15:
            ch_w, dis_w = min(surplus, grid_cap_w), 0.0   # grid surplus -> battery (cap 10A)
        elif surplus < -15:
            ch_w, dis_w = 0.0, -surplus            # grid can't cover load -> battery
        else:
            ch_w = dis_w = 0.0                     # within inverter overhead band
        solar_pv_w = round(min(pv_dc, solar_cap_w), 1) if (sun_el > 2 and not full) else 0.0
    else:
        # grid off: daytime = solar feeds the load + charges battery; night = battery feeds the load
        if sun_el > 2:
            dis_w = 0.0
            ch_w = flood_w if full else min(solar_cap_w, pv_dc)   # solar cap 30A
            solar_pv_w = round(out_p + ovh + ch_w, 1)
        else:
            dis_w = out_p + ovh
            ch_w = 0.0
            solar_pv_w = 0.0

    if ch_w > dis_w and ch_w > 3:
        current_amp = round(ch_w / batt_v, 1)
    elif dis_w > ch_w and dis_w > 3:
        current_amp = round(-dis_w / 13.8, 1)
    else:
        current_amp = 0.0

    batt_soc = drift_soc(ch_w, dis_w)

    update_trackers(is_grid, main_p, in_p, out_p, solar_pv_w, ch_w, dis_w,
                    ch_a=current_amp, dis_a=abs(current_amp) if current_amp < 0 else 0.0)

    with state_lock:
        on_h = round(grid_tracker["on_seconds"] / 3600.0, 1)
        off_h = round(grid_tracker["off_seconds"] / 3600.0, 1)
        outages = grid_tracker["outages"]
        main_k = round(energy_acc["main_kwh"], 3)
        in_k = round(energy_acc["in_kwh"], 3)
        out_k = round(energy_acc["out_kwh"], 3)
        pv_k = round(energy_acc["pv_kwh"], 3)
        ch_k = round(energy_acc["ch_kwh"], 3)
        dis_k = round(energy_acc["dis_kwh"], 3)
        pv_peak = round(energy_acc["pv_peak_w"], 0)
        pv_peak_t = energy_acc["pv_peak_t"]
        pk_main = round(energy_acc["main_peak_w"], 0); pk_main_t = energy_acc["main_peak_t"]
        pk_in = round(energy_acc["in_peak_w"], 0);     pk_in_t = energy_acc["in_peak_t"]
        pk_out = round(energy_acc["out_peak_w"], 0);   pk_out_t = energy_acc["out_peak_t"]
        pk_cha = round(energy_acc["ch_peak_a"], 1);    pk_cha_t = energy_acc["ch_peak_t"]
        pk_disa = round(energy_acc["dis_peak_a"], 1);  pk_disa_t = energy_acc["dis_peak_t"]

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
        {"entity_id": "sensor.energy_mate_v4_battery_charge_power", "state": str(round(ch_w, 1)), "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_battery_discharge_power", "state": str(round(dis_w, 1)), "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_battery_estimated_soc", "state": str(round(batt_soc, 1)), "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_battery_current", "state": str(current_amp), "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_battery_net_current", "state": str(current_amp), "last_updated": now_iso},

        {"entity_id": "sensor.energy_mate_v4_main_daily", "state": str(main_k), "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_input_daily", "state": str(in_k), "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_output_daily", "state": str(out_k), "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_pv_daily", "state": str(pv_k), "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_battery_charge_daily", "state": str(ch_k), "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_battery_discharge_daily", "state": str(dis_k), "last_updated": now_iso},

        {"entity_id": "sensor.energy_mate_v4_grid_on_today", "state": str(on_h), "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_grid_off_today", "state": str(off_h), "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_grid_outages_today", "state": str(outages), "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_pv_peak_today", "state": str(pv_peak), "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_peak_pv_time", "state": pv_peak_t, "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_peak_main_w", "state": str(pk_main), "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_peak_main_time", "state": pk_main_t, "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_peak_in_w", "state": str(pk_in), "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_peak_in_time", "state": pk_in_t, "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_peak_out_w", "state": str(pk_out), "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_peak_out_time", "state": pk_out_t, "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_peak_ch_a", "state": str(pk_cha), "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_peak_ch_time", "state": pk_cha_t, "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_peak_dis_a", "state": str(pk_disa), "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_peak_dis_time", "state": pk_disa_t, "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_inverter_own_use", "state": str(round(ovh, 1)), "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_inverter_overhead_power", "state": str(round(ovh, 1)), "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_pack_version", "state": "V11.0", "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_poll_heartbeat", "state": str(int(time.time())), "last_updated": now_iso},
        {"entity_id": "sensor.energy_mate_v4_effective_pv_array_power", "state": str(ARRAY_WATT), "last_updated": now_iso},

        {"entity_id": "sun.sun", "state": "above_horizon" if sun_el > 0 else "below_horizon", "attributes": {"elevation": sun_el}},
        {"entity_id": "input_number.energy_final_site_lat", "state": str(SITE_LAT), "last_updated": now_iso},
        {"entity_id": "input_number.energy_final_site_lon", "state": str(SITE_LON), "last_updated": now_iso},
        {"entity_id": "input_number.energy_final_panel_azimuth", "state": "180", "last_updated": now_iso},
        {"entity_id": "input_number.energy_final_panel_tilt", "state": "10", "last_updated": now_iso},
        {"entity_id": "input_number.energy_final_manual_soc", "state": str(user_settings["battery_soc"]), "last_updated": now_iso},
        {"entity_id": "input_boolean.energy_final_battery_manual_soc", "state": "off", "last_updated": now_iso}
    ]
    resp = jsonify(states)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp

@app.route("/healthz")
def healthz():
    ensure_poller()
    with state_lock:
        t = device_cache["t"]
        conn = device_cache["connected_to_tuya"]
    age = round(time.time() - t, 1) if t else None
    return jsonify({"ok": True, "tuya_connected": conn, "last_device_update_age_s": age,
                    "creds_configured": bool(CLIENT_ID and CLIENT_SECRET),
                    "last_error": diag["last_error"], "ts": time.time()})

@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "POST":
        if SET_SECRET:
            key = request.args.get("key", "") or request.headers.get("X-Set-Key", "")
            if key != SET_SECRET:
                return jsonify({"success": False, "error": "bad key"}), 403
        data = request.get_json(force=True, silent=True) or {}
        for field in ("battery_soc", "battery_capacity_ah", "manual_solar_watts",
                      "inv_own_use", "grid_charge_cap_a", "solar_charge_cap_a", "battery_voltage"):
            if field in data:
                try:
                    user_settings[field] = float(data[field])
                except (TypeError, ValueError):
                    pass
        if "solar_mode" in data:
            user_settings["solar_mode"] = str(data["solar_mode"])
        # user changed the SoC baseline -> reseed the auto drift now
        with state_lock:
            if "battery_soc" in data:
                soc_state["soc"] = user_settings["battery_soc"]
        return jsonify({"success": True, "settings": user_settings})
    return jsonify(user_settings)

@app.route("/api/sync", methods=["GET", "POST"])
def api_force_sync():
    def do_sync():
        try:
            poll_round()
        except Exception:
            pass
    threading.Thread(target=do_sync, daemon=True).start()
    return jsonify({"status": "sync_triggered", "timestamp": time.time()})

@app.route("/")
def home():
    ensure_poller()
    for name in ("Energy.html", "energy.html", "index.html"):
        path = os.path.join(BASE_DIR, name)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
    return "<h3>Energy.html not found! Please check repository files.</h3>"

if __name__ == "__main__":
    ensure_poller()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
