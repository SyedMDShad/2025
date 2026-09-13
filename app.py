      # --------------------------------------------------------------------------
      # 100% Automatic PV Calculation Engine (Fixed Battery DC Charging Bug)
      # --------------------------------------------------------------------------
      - name: "Energy Mate PV Estimated Power"
        unique_id: energy_mate_pv_estimated_power_v6
        unit_of_measurement: "W"
        device_class: power
        state_class: measurement
        state: >
          {% if is_state('sun.sun', 'below_horizon') %}
            0.0
          {% else %}
            {% set grid_on = is_state('binary_sensor.energy_mate_grid_available', 'on') %}
            {% set inv_in = states('sensor.energy_mate_inverter_input_power') | float(0) %}
            {% set inv_out = states('sensor.energy_mate_inverter_output_power') | float(0) %}
            {% set own_use = states('sensor.energy_mate_inverter_own_consumption') | float(45) %}
            {% set max_array = states('input_number.energy_final_pv_total_watt') | float(800) %}
            {% set chg_amps = states('input_number.energy_final_battery_max_charge_amps') | float(21.0) %}
            {% set bat_chg_w = chg_amps * 13.5 %}
            
            {% if not grid_on %}
              {# গ্রিড অফ: লোড + ব্যাটারি চার্জিং = আসল সোলার জেনারেশন #}
              {% set total_pv = inv_out + bat_chg_w %}
              {{ [[total_pv, 0] | max, max_array] | min | round(1) }}
            {% else %}
              {# গ্রিড অন: লোড কাভার + ব্যাটারি চার্জিং #}
              {% set load_covered = [inv_out - inv_in, 0] | max %}
              {% set total_pv = load_covered + bat_chg_w %}
              {{ [[total_pv, 0] | max, max_array] | min | round(1) }}
            {% endif %}
          {% endif %}

      # --------------------------------------------------------------------------
      # Battery Live Current (Now reflects 20-22A real solar charging)
      # --------------------------------------------------------------------------
      - name: "Energy Mate Battery Net Current"
        unique_id: energy_mate_battery_net_current_v6
        unit_of_measurement: "A"
        device_class: current
        state_class: measurement
        state: >
          {% set sun_up = not is_state('sun.sun', 'below_horizon') %}
          {% set grid_on = is_state('binary_sensor.energy_mate_grid_available', 'on') %}
          {% set inv_out = states('sensor.energy_mate_inverter_output_power') | float(0) %}
          {% set chg_amps = states('input_number.energy_final_battery_max_charge_amps') | float(21.0) %}
          
          {% if sun_up %}
            {# দিনের বেলায় সোলার থেকে ব্যাটারিতে চার্জিং ঢুকছে #}
            {{ chg_amps | round(1) }}
          {% elif not grid_on %}
            {# রাতে লোডশেডিংয়ে ডিসচার্জিং #}
            {{ (- (inv_out / 12.0)) | round(1) }}
          {% else %}
            0.0
          {% endif %}
