
---

# Bticino X8000 (Smarther NOT Netatmo) for Home Assistant

A high-performance, fault-tolerant Home Assistant integration for **Legrand/Bticino Smarther X8000** thermostats.

This integration is designed with a **"Stability First"** philosophy. It implements advanced algorithms to manage the strict Legrand API Rate Limits (Daily Quota), ensuring your account is never banned while maintaining high responsiveness for active devices.

---

## 🌟 Key Features

* **Full Climate Control:** Set mode (Auto, Manual, Boost, Off), temperature, and view humidity.
* **Smart Energy Saving (v2):** Algorithmically adjusts polling frequency based on thermostat state to save API calls.
* **Budget Safety Net:** Automatically throttles updates if the daily API quota is running low.
* **Granular Telemetry:** Diagnostic sensors for API usage per device, total usage, and efficiency stats (skipped polls).
* **Fail-Fast Architecture:** Detects API issues (Auth/Rate Limit) immediately to prevent "Hammering" and subsequent bans.
* **Persistent Token Storage:** Robust authentication handling that survives restarts.
* **Webhook Support:** Instant updates when settings are changed via the physical device or the Legrand App.

---

## ⚙️ Configuration Options

You can fine-tune the integration behavior via **Settings > Devices & Services > Bticino X8000 > Configure**.

| Option | Default | Description |
| --- | --- | --- |
| **Poll Interval** | `15 min` | The standard interval for fetching data. Lower values increase responsiveness but consume more API calls. |
| **Smart Energy Saving** | `ON` | **Recommended.** Activates the adaptive algorithm that reduces polling for "Passive" devices (Off/Antifrost) to save quota. |
| **Daily API Quota** | `500` | Set this to match your Legrand developer account limit. Used by the *Budget Safety Net* to calculate remaining calls. |
| **Cool Down Period** | `60 min` | If a Rate Limit (429) is hit, the integration pauses for this duration to let the ban expire. |
| **Notify Errors** | `ON` | If enabled, a persistent notification will appear on the Dashboard if the API is paused due to errors. |
| **Webhook Debounce** | `1.0 s` | Prevents flooding if the physical thermostat sends multiple events rapidly (e.g., sliding the touch bar). |

---

## 🧠 Core Logic & Algorithms

This integration distinguishes itself through three layers of protective logic designed to keep your smart home running within the strict cloud limits.

### 1. Smart Polling (State-Aware Scheduling)

When **Smart Energy Saving** is enabled, the Coordinator does not treat all devices equally. It classifies thermostats into two categories at every cycle:

* **Active Devices (Priority: High)**
* **Definition:** Thermostats in `Manual`, `Boost`, or `Automatic` mode.
* **Logic:** Even if the boiler is currently off (Idle), these devices are monitored at the **Normal Interval** (e.g., every 15 mins) to capture sudden state changes or heating demands immediately.


* **Passive Devices (Priority: Low)**
* **Definition:** Thermostats in `Off` or `Protection` (Antifrost) mode.
* **Logic:** Since these devices are unlikely to change state without user intervention, their polling interval is multiplied by **4x** (e.g., every 60 mins).
* **Benefit:** This saves approximately **75%** of API calls for unused zones (e.g., guest rooms, summer season).



### 2. Budget Safety Net (Stateless Budget Awareness)

The system continuously monitors the `api_call_count` against your `Daily API Quota`. If usage is too high for the time of day, it automatically engages **Emergency Throttling** to ensure you reach midnight without a ban.

| Remaining Calls | Mode | Behavior |
| --- | --- | --- |
| **> 100** | **Normal** | Uses standard intervals (15m Active / 60m Passive). |
| **< 100** | **Economy** | Intervals doubled (30m Active / 120m Passive). |
| **< 40** | **Survival** | Intervals quadrupled (60m Active / 240m Passive). Critical preservation mode. |

### 3. Fail-Fast & Cool Down

Legrand APIs are strict. If a request fails:

1. **Fail-Fast:** If an authentication error or a 429 (Rate Limit) occurs on *one* device, the entire update cycle stops immediately. This prevents the integration from trying to update 10 other devices, which would result in 10 more errors and a longer ban.
2. **Cool Down:** The integration enters a "Coma" state for the configured **Cool Down Period** (default 60m). During this time, no API calls are made.
3. **Auto-Recovery:** After the timer expires, it attempts a single call. If successful, it resumes normal operation automatically.

---

## 📊 Sensors & Diagnostics

The integration provides extensive telemetry to help you monitor the health of your system.

### Thermostat Entities

* **Climate:** The main control entity (Heat/Cool/Auto/Off). **Note:** If a device is unreachable (Gateway Offline/404), this entity will show as `Unavailable` rather than `Off` to avoid confusion.
* **Sensors:** Temperature, Humidity, Target Temperature.
* **Mode & Status:** Text sensors showing the raw operating mode and load state (heating/idle).

### Diagnostic Sensors

Check the **Diagnostic** section of your device to find:

* **API Usage (Per Device):** How many calls specifically targeted this thermostat.
* **API Call Count (Global):** Total calls made by the integration today. Resets at midnight.
* **Smart Polling Skips:** A counter showing how many API calls were *avoided* thanks to the optimization algorithms. A higher number means higher efficiency.

---

## ❓ Troubleshooting

### "Unavailable" vs "Off"

* **Off:** The thermostat is powered but switched off via software.
* **Unavailable:** The integration cannot reach the Legrand Cloud, or the Legrand Cloud cannot reach your physical device (Gateway Offline). Check your Wi-Fi connection.

### API Count shows 0 after restart

The API counter is saved to a persistent storage file (`.storage/bticino_x8000.api_usage`) to survive restarts. However, if you restore a backup or delete the storage folder, it may reset. The **Budget Safety Net** will adapt automatically based on the new (lower) count, gradually normalizing over 24 hours.

### Rate Limit (429) Errors

If you see this error, you have exceeded the 500 calls/day limit.

1. Check if you have other apps/integrations using the same Legrand account.
2. Enable **Smart Energy Saving**.
3. Increase the **Poll Interval** to 20 or 30 minutes.

---

**Disclaimer:** This is a custom integration and is not officially affiliated with Legrand or Bticino. Use at your own risk.