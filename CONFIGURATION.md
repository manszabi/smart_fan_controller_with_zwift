# Konfiguráció – Smart Fan Controller

A program a `settings.json` fájlból olvassa a beállításokat. Ha a fájl nem létezik, automatikusan létrehozza az alapértelmezett értékekkel. A kommentezett referenciát lásd: `settings.example.jsonc`.

---

## Gyors kezdés

1. Másold a `settings.example.json` fájlt `settings.json` néven.
2. Állítsd be az FTP értékedet (`power_zones.ftp`).
3. Válaszd ki az adatforrást (`datasource.power_source`, `datasource.hr_source`).
4. Ha BLE ventilátort használsz, állítsd be a `ble.device_name` mezőt (vagy hagyd `null`-on az auto-discovery-hez).
5. Indítsd el: `python swift_fan_controller_new_v7.py`

---

## Globális beállítások

| Mező | Típus | Tartomány | Alapértelmezett | Leírás |
|------|-------|-----------|-----------------|--------|
| `cooldown_seconds` | int | 0–300 | 120 | Cooldown idő zóna csökkentésnél. 0 = azonnali váltás. |
| `buffer_seconds` | int | 1–10 | 3 | Gördülő átlag ablak (fallback ha forrás-specifikus nincs). |
| `minimum_samples` | int | 1–1000 | 6 | Minimum minta érvényes átlaghoz (fallback). |
| `buffer_rate_hz` | int | 1–60 | 4 | Várt mintavételi frekvencia Hz-ben (fallback). |
| `dropout_timeout` | int | 1–120 | 5 | Adatforrás kiesés timeout másodpercben (fallback). |

---

## Teljesítmény zóna határok (`power_zones`)

| Mező | Típus | Tartomány | Alapértelmezett | Leírás |
|------|-------|-----------|-----------------|--------|
| `ftp` | int | 100–500 | 200 | Funkcionális küszöbteljesítmény (watt). |
| `min_watt` | int | 0–9999 | 0 | Minimális érvényes pozitív watt. |
| `max_watt` | int | 1–100000 | 1000 | Maximális érvényes watt. |
| `z1_max_percent` | int | 1–100 | 60 | Z1 felső határ az FTP %-ában. |
| `z2_max_percent` | int | 1–100 | 89 | Z2 felső határ az FTP %-ában. |
| `zero_power_immediate` | bool | – | false | Ha true, 0W → azonnali LEVEL:0 (cooldown nélkül). |

**Zóna kiosztás:**

- **Z0:** 0W – ventilátor kikapcsolva
- **Z1:** 1W – FTP × z1_max_percent%
- **Z2:** FTP × z1_max_percent% + 1 – FTP × z2_max_percent%
- **Z3:** FTP × z2_max_percent% + 1 – max_watt

**Validáció:** a program automatikusan javítja ha `min_watt >= max_watt` vagy `z1_max_percent >= z2_max_percent`.

---

## BLE ventilátor kimenet (`ble`)

Az ESP32 BLE vezérlőhöz való csatlakozás beállításai. A program `LEVEL:N` (N=0–3) parancsokat küld a GATT karakterisztikára.

| Mező | Típus | Tartomány | Alapértelmezett | Leírás |
|------|-------|-----------|-----------------|--------|
| `device_name` | string/null | – | null | BLE eszköz neve. `null` vagy `""` → auto-discovery. |
| `scan_timeout` | int | 1–60 | 10 | BLE keresés timeout (mp). |
| `connection_timeout` | int | 1–60 | 15 | Csatlakozási timeout (mp). |
| `reconnect_interval` | int | 1–60 | 5 | Újracsatlakozás várakozási ideje (mp). |
| `max_retries` | int | 1–100 | 10 | Max újrapróbálkozás, utána 30s cooldown. |
| `command_timeout` | int | 1–30 | 3 | GATT write timeout (mp). |
| `service_uuid` | string | – | `0000ffe0-...` | BLE service UUID. |
| `characteristic_uuid` | string | – | `0000ffe1-...` | BLE characteristic UUID. |
| `pin_code` | int/string/null | – | null | Alkalmazás szintű PIN. `null` → nincs auth. |

**Auto-discovery:** ha `device_name` `null` vagy üres, a program automatikusan megkeresi a `service_uuid`-t hirdető eszközt. A talált eszközök a `ble_devices.log` fájlba kerülnek.

**PIN kód:** megadható int-ként (`123456`) vagy string-ként (`"012345"` ha vezető nulla szükséges). Max 20 karakter.

---

## Adatforrás beállítások (`datasource`)

### Forrás kiválasztása

| Mező | Értékek | Alapértelmezett | Leírás |
|------|---------|-----------------|--------|
| `power_source` | `"antplus"`, `"ble"`, `"zwiftudp"` | `"antplus"` | Teljesítmény adatforrás. |
| `hr_source` | `"antplus"`, `"ble"`, `"zwiftudp"` | `"antplus"` | Szívfrekvencia adatforrás. |

A power és HR különböző forrásból is jöhet (pl. `power_source: "antplus"`, `hr_source: "ble"`).

### Forrás-specifikus buffer beállítások

Minden forrásnak saját buffer paraméterei vannak. Ha nincs megadva, a globális fallback értékek érvényesek.

| Prefix | Forrás | Mezők |
|--------|--------|-------|
| `BLE_` | BLE szenzorok | `BLE_buffer_seconds`, `BLE_minimum_samples`, `BLE_buffer_rate_hz`, `BLE_dropout_timeout` |
| `ANT_` | ANT+ szenzorok | `ANT_buffer_seconds`, `ANT_minimum_samples`, `ANT_buffer_rate_hz`, `ANT_dropout_timeout` |
| `zwiftUDP_` | Zwift UDP | `zwiftUDP_buffer_seconds`, `zwiftUDP_minimum_samples`, `zwiftUDP_buffer_rate_hz`, `zwiftUDP_dropout_timeout` |

**Validáció:** `minimum_samples` nem lehet nagyobb mint `buffer_seconds × buffer_rate_hz`.

### ANT+ eszköz beállítások

| Mező | Típus | Tartomány | Alapértelmezett | Leírás |
|------|-------|-----------|-----------------|--------|
| `ant_power_device_id` | int | 0–65535 | 0 | ANT+ power meter device ID. 0 = wildcard (első elérhető). |
| `ant_hr_device_id` | int | 0–65535 | 0 | ANT+ HR monitor device ID. 0 = wildcard. |
| `ant_power_reconnect_interval` | int | 1–60 | 5 | Power reconnect várakozás (mp). |
| `ant_power_max_retries` | int | 1–100 | 10 | Power max újrapróba, utána 30s cooldown. |
| `ant_hr_reconnect_interval` | int | 1–60 | 5 | HR reconnect várakozás (mp). |
| `ant_hr_max_retries` | int | 1–100 | 10 | HR max újrapróba. |

**Device ID:** a talált eszközök az `ant_devices.log` fájlba kerülnek. Első futáskor hagyd 0-n (wildcard), majd a logból kiolvasható a specifikus device ID.

**Watchdog:** ha 30 másodpercig nem érkezik ANT+ broadcast, a program automatikusan újraindítja az ANT+ node-ot (USB dongle kihúzás/lemerülés detektálása).

### BLE szenzor bemeneti beállítások

| Mező | Típus | Tartomány | Alapértelmezett | Leírás |
|------|-------|-----------|-----------------|--------|
| `ble_power_device_name` | string/null | – | null | BLE power meter neve. `null` → auto-discovery. |
| `ble_power_scan_timeout` | int | 1–60 | 10 | Keresés timeout (mp). |
| `ble_power_reconnect_interval` | int | 1–60 | 5 | Újracsatlakozás várakozás (mp). |
| `ble_power_max_retries` | int | 1–100 | 10 | Max újrapróba. |
| `ble_hr_device_name` | string/null | – | null | BLE HR monitor neve. `null` → auto-discovery. |
| `ble_hr_scan_timeout` | int | 1–60 | 10 | Keresés timeout (mp). |
| `ble_hr_reconnect_interval` | int | 1–60 | 5 | Újracsatlakozás várakozás (mp). |
| `ble_hr_max_retries` | int | 1–100 | 10 | Max újrapróba. |

**Auto-discovery:** a BLE Power a Cycling Power Service (UUID: `0x1818`), a BLE HR a Heart Rate Service (UUID: `0x180D`) alapján keres automatikusan.

### Zwift UDP beállítások

| Mező | Típus | Tartomány | Alapértelmezett | Leírás |
|------|-------|-----------|-----------------|--------|
| `zwift_udp_port` | int | 1024–65535 | 7878 | UDP port a zwift_api_polling.py-hoz. |
| `zwift_udp_host` | string | – | `"127.0.0.1"` | UDP host (localhost). |

A program automatikusan elindítja a `zwift_api_polling.py` scriptet ha Zwift UDP forrás van beállítva.

---

## Szívfrekvencia zónák (`heart_rate_zones`)

| Mező | Típus | Tartomány | Alapértelmezett | Leírás |
|------|-------|-----------|-----------------|--------|
| `enabled` | bool | – | false | Ha false, HR adatok csak megjelennek de nem befolyásolják a ventilátort. |
| `max_hr` | int | 100–220 | 185 | Maximális szívfrekvencia (bpm). |
| `resting_hr` | int | 30–100 | 60 | Pihenő szívfrekvencia (bpm). |
| `zone_mode` | string | lásd lent | `"power_only"` | Zóna kombináció módja. |
| `z1_max_percent` | int | 1–100 | 70 | Z1 felső határ a max_hr %-ában. |
| `z2_max_percent` | int | 1–100 | 80 | Z2 felső határ a max_hr %-ában. |
| `valid_min_hr` | int | 30–100 | 30 | Érvényes HR alsó határ szűréshez. |
| `valid_max_hr` | int | 150–300 | 220 | Érvényes HR felső határ szűréshez. |

**Zóna módok:**

- `"power_only"` – csak a teljesítmény zóna dönt (HR figyelmen kívül)
- `"hr_only"` – csak a HR zóna dönt (power figyelmen kívül)
- `"higher_wins"` – a kettő közül a magasabb zóna érvényesül

**HR zóna kiosztás:**

- **Z0:** resting_hr alatt
- **Z1:** resting_hr – max_hr × z1_max_percent%
- **Z2:** max_hr × z1_max_percent% + 1 – max_hr × z2_max_percent%
- **Z3:** max_hr × z2_max_percent% felett

---

## Cooldown logika

A cooldown csak zóna **csökkentésnél** aktív. Zóna **emelkedésnél** azonnali váltás történik.

**Adaptív módosítások:**

- Nagy zónaesés (≥2 szint) vagy 0W cél → cooldown idő **felezése** (gyorsabb leállás)
- Pending zóna visszaemelkedik → cooldown idő **duplázása** (lassabb emelkedés)
- Felezés és duplázás egyszer-egyszer alkalmazható ciklusonként

---

## Automatikus újracsatlakozás

Mind az ANT+, mind a BLE oldalon automatikus reconnect logika működik:

1. Kapcsolat megszakadás detektálása (BLE: bleak disconnect event, ANT+: watchdog timeout)
2. `reconnect_interval` másodperc várakozás
3. Újrapróbálkozás (max `max_retries` alkalommal)
4. Ha elérte a max-ot: 30 másodperc cooldown, majd újrakezdés

---

## Log fájlok

| Fájl | Tartalom |
|------|----------|
| `ble_devices.log` | Talált BLE eszközök (deduplikált, csak új eszközök kerülnek bele) |
| `ant_devices.log` | Talált ANT+ eszközök (deduplikált, device_type + device_id alapján) |

Ezek a fájlok hasznosak a device_name / device_id beállításához: első futás wildcard módban, majd a logból kiolvasható a specifikus eszköz azonosító.
