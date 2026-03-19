# Smart Fan Controller

Kerékpáros edzés ventilátor vezérlő – ANT+, BLE és Zwift UDP szenzor adatok alapján automatikusan szabályozza a BLE ventilátort (ESP32).

## Működés

A program valós időben fogadja a teljesítmény (watt) és szívfrekvencia (bpm) adatokat, gördülő átlagot számít, meghatározza a ventilátor zónát (0–3), és BLE-n keresztül elküldi a `LEVEL:N` parancsot az ESP32 vezérlőnek.

```
┌─────────────┐     ┌──────────────────────┐     ┌─────────────┐
│  ANT+ Power ├────►│                      ├────►│             │
│  ANT+ HR    │     │   Swift Fan          │     │  ESP32 BLE  │
├─────────────┤     │   Controller         │     │  Ventilátor │
│  BLE Power  ├────►│                      ├────►│  Vezérlő    │
│  BLE HR     │     │  ┌────────────────┐  │     │             │
├─────────────┤     │  │ Gördülő átlag  │  │     │  LEVEL:0–3  │
│  Zwift UDP  ├────►│  │ Zóna számítás  │  │     └─────────────┘
│  (API/UDP)  │     │  │ Cooldown       │  │
└─────────────┘     │  │ Higher Wins    │  │
                    │  └────────────────┘  │
                    └──────────────────────┘
```

## Fő jellemzők

- **Három adatforrás:** ANT+ (USB dongle), BLE (Bluetooth Low Energy), Zwift UDP – szabadon kombinálhatók
- **Három zóna mód:** power_only, hr_only, higher_wins (a magasabb zóna nyer)
- **Adaptív cooldown:** zóna csökkentésnél várakozás (felezés nagy esésnél, duplázás visszaemelkedésnél)
- **Auto-discovery:** BLE és ANT+ eszközök automatikus felderítése és logolása
- **Watchdog:** ANT+ USB dongle kihúzás/lemerülés automatikus detektálása és reconnect
- **HUD:** Star Trek LCARS stílusú lebegő ablak (tkinter) – valós idejű zóna, watt, HR kijelzés
- **Headless mód:** tkinter nélkül is fut (pl. Raspberry Pi terminálban)

## Telepítés

### Követelmények

- Python 3.10+
- ANT+ USB dongle (pl. Garmin ANT+ Stick) – ha ANT+ forrást használsz
- Bluetooth adapter – ha BLE forrást/kimenetet használsz

### Függőségek

```bash
pip install -r requirements.txt
```

| Csomag | Szükséges? | Funkció |
|--------|-----------|---------|
| `bleak` | Opcionális | BLE kommunikáció (ventilátor + BLE szenzorok) |
| `openant` | Opcionális | ANT+ kommunikáció (power meter, HR monitor) |
| `requests` | Opcionális | Zwift API polling (`zwift_api_polling.py`, ha `zwiftudp_sources: "zwift_api_polling"`) |

A program a rendelkezésre álló könyvtárak alapján automatikusan engedélyezi/letiltja az adatforrásokat. Nem kötelező mindet telepíteni.

## Indítás

```bash
# Alapértelmezett settings.json-nal
python swift_fan_controller_new_v7.py

# Vagy a példa beállítások másolása után
cp settings.example.json settings.json
# ... settings.json szerkesztése ...
python swift_fan_controller_new_v7.py
```

## Konfiguráció

A részletes beállítási leírást lásd: [CONFIGURATION.md](CONFIGURATION.md)

Röviden a `settings.json` fő szekciói:

| Szekció | Tartalom |
|---------|----------|
| `global_settings` | cooldown, buffer, dropout timeout |
| `power_zones` | FTP, watt tartomány, zóna százalékok, 0W azonnali leállás |
| `heart_rate_zones` | HR zónák, zone_mode (power_only/hr_only/higher_wins) |
| `ble` | ESP32 ventilátor vezérlő (kimenet) |
| `datasource` | Adatforrás kiválasztás, ANT+/BLE/Zwift specifikus beállítások |

Kommentezett referencia: `settings.example.jsonc`

## Típuspéldák

### ANT+ power meter + ANT+ HR → BLE ventilátor

```json
{
  "power_zones": { "ftp": 200 },
  "ble": { "device_name": "FanController" },
  "datasource": {
    "power_source": "antplus",
    "hr_source": "antplus"
  },
  "heart_rate_zones": {
    "enabled": true,
    "zone_mode": "higher_wins"
  }
}
```

### BLE power meter + BLE HR → BLE ventilátor (auto-discovery)

```json
{
  "power_zones": { "ftp": 250 },
  "ble": { "device_name": null },
  "datasource": {
    "power_source": "ble",
    "hr_source": "ble"
  },
  "heart_rate_zones": {
    "enabled": true,
    "zone_mode": "higher_wins"
  }
}
```

### Zwift power + BLE HR → BLE ventilátor (API polling, alapértelmezett)

```json
{
  "power_zones": { "ftp": 180 },
  "ble": { "device_name": "FanController", "pin_code": 123456 },
  "datasource": {
    "power_source": "zwiftudp",
    "hr_source": "ble",
    "zwiftudp_sources": "zwift_api_polling"
  },
  "heart_rate_zones": {
    "enabled": true,
    "zone_mode": "higher_wins"
  }
}
```

### Zwift power + BLE HR → BLE ventilátor (UDP monitor, bejelentkezés nélkül)

```json
{
  "power_zones": { "ftp": 180 },
  "ble": { "device_name": "FanController", "pin_code": 123456 },
  "datasource": {
    "power_source": "zwiftudp",
    "hr_source": "ble",
    "zwiftudp_sources": "zwift_udp_monitor"
  },
  "heart_rate_zones": {
    "enabled": true,
    "zone_mode": "higher_wins"
  }
}
```

### Csak power (ANT+), HR nélkül

```json
{
  "power_zones": { "ftp": 200 },
  "datasource": {
    "power_source": "antplus"
  },
  "heart_rate_zones": {
    "enabled": false
  }
}
```

## Zóna logika

| Zóna | Ventilátor | Power (FTP=200, z1=60%, z2=89%) | HR (max=185, z1=70%, z2=80%) |
|------|------------|------|-------|
| Z0 | Ki | 0W | < 60 bpm |
| Z1 | Alacsony | 1–120W | 60–129 bpm |
| Z2 | Közepes | 121–178W | 130–148 bpm |
| Z3 | Maximum | 179W+ | 149+ bpm |

## Architektúra

```
main()
├── AsyncioThread (daemon)
│   ├── BLEFanOutput         – zone_queue → LEVEL:N BLE GATT write
│   ├── BLEPowerInput*       – BLE notification → raw_power_queue
│   ├── BLEHRInput*          – BLE notification → raw_hr_queue
│   ├── ZwiftUDPInput*       – UDP JSON → raw_power_queue / raw_hr_queue
│   ├── PowerProcessor       – raw_power_queue → átlag → zóna → zone_event
│   ├── HRProcessor          – raw_hr_queue → átlag → zóna → zone_event
│   ├── ZoneController       – zone_event → cooldown → zone_queue
│   └── DropoutChecker       – timeout → Z0
├── ANTPlus-Thread* (daemon)
│   ├── openant Node.start() – blokkoló ANT+ loop
│   └── ANTPlus-Watchdog     – USB disconnect detektálás
└── tkinter HUD (fő szál)
    └── 500ms polling → UISnapshot

* = opcionális, beállítástól függően
```

## Fájlok

| Fájl | Leírás |
|------|--------|
| `swift_fan_controller_new_v7.py` | Fő program |
| `zwift_api_polling.py` | Zwift HTTPS API polling script (automatikusan indul, ha `zwiftudp_sources: "zwift_api_polling"`) |
| `zwift_udp_monitor.py` | Zwift Companion App UDP figyelő script (automatikusan indul, ha `zwiftudp_sources: "zwift_udp_monitor"`) |
| `esp32_fan_controller.ino` | ESP32 firmware (Arduino – Xiao ESP32-C3) |
| `settings.json` | Aktív beállítások |
| `settings.example.json` | Példa beállítások (alapértelmezett értékek) |
| `settings.example.jsonc` | Kommentezett beállítás referencia |
| `ble_devices.log` | Talált BLE eszközök (automatikusan generált) |
| `ant_devices.log` | Talált ANT+ eszközök (automatikusan generált) |
| `CONFIGURATION.md` | Részletes konfigurációs dokumentáció |

## ESP32 firmware

Az `esp32_fan_controller.ino` a BLE ventilátor vezérlő firmware-je, **Seeed Studio Xiao ESP32-C3** mikrovezérlőre. A Python program ezzel kommunikál BLE-n keresztül.

**Firmware v5.2.0** – főbb jellemzők:

| Paraméter | Érték |
|-----------|-------|
| BLE device neve | `FanController` |
| Service UUID | `0000ffe0-0000-1000-8000-00805f9b34fb` |
| Characteristic UUID | `0000ffe1-0000-1000-8000-00805f9b34fb` |
| Alapértelmezett PIN | `123456` |
| Parancsformátum | `LEVEL:0` – `LEVEL:3` |
| Deep sleep timeout | 30 perc inaktivitás után |
| BLE zóna timeout | 10 perc BLE kapcsolat nélkül → LEVEL:0 |

**Zóna–relé megfeleltetés:**

| Zóna | Ventilátor | Relék aktív |
|------|------------|-------------|
| 0 | Ki | – |
| 1 | 33% | FAN1 |
| 2 | 66% | FAN1 + FAN2 |
| 3 | 100% | FAN1 + FAN2 + FAN3 |

**Szükséges Arduino könyvtárak:** OneButton, AsyncTCP, ESPAsyncWebServer, WebSerial, ArduinoJson, ElegantOTA.

## Leállítás

`Ctrl+C` vagy ablak bezárás. A program gondoskodik a tiszta leállításról: BLE disconnect, ANT+ node stop, subprocess terminate.
