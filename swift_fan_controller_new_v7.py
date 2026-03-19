#!/usr/bin/env python3
from __future__ import annotations

"""
swift_fan_controller_new_v7.py

Smart Fan Controller – moduláris, párhuzamos implementáció.

Minden fő funkció különálló aszinkron feladatban/szálban fut:
  - ANT+ bemenő adatkezelés (HR, power)        → ANTPlusInputHandler (daemon szál + asyncio bridge)
  - BLE ventilátor kimenő vezérlés              → BLEFanOutputController (asyncio korrutin)
  - BLE bemenő adatok (HR, power)               → BLEPowerInputHandler, BLEHRInputHandler (asyncio)
  - Zwift UDP bejövő adatkezelés                → ZwiftUDPInputHandler (asyncio DatagramProtocol)
  - Power átlag számítás                        → PowerAverager + power_processor_task
  - HR átlag számítás                           → HRAverager + hr_processor_task
  - higher_wins logika                          → apply_zone_mode() (tiszta függvény)
  - Cooldown logika                             → CooldownController (állapotgép)
  - Zona számítás                               → zone_for_power(), zone_for_hr() (tiszta függvények)
  - Zona elküldése                              → zone_controller_task + send_zone()
  - Konzolos kiírás                             → ConsolePrinter (throttle-olt)

Architektúra:
  - Egyetlen asyncio event loop a fő vezérlési logikához
  - Saját daemon szál az ANT+ számára (blokkoló könyvtár)
  - asyncio.Queue a komponensek közötti adatátvitelhez
  - asyncio.Event a zóna újraszámítás jelzéséhez
  - asyncio.Lock a megosztott állapot védelméhez
  - Tiszta (mellékhatás-mentes) függvények a logikához (jól tesztelhetők)

Verziószám: 1.0.0
"""

import abc
import asyncio
import copy
import dataclasses
import enum
import json
import logging
import math
import platform as _platform
import signal
import threading
import time
import atexit
import subprocess
import sys
import os

from collections import deque
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Tuple, TYPE_CHECKING

# --- Enum-ok a magic string-ek kiváltásához ---
# str öröklés: JSON-ból jövő string értékekkel is kompatibilis (==)
class DataSource(str, enum.Enum):
    ANTPLUS = "antplus"
    BLE = "ble"
    ZWIFTUDP = "zwiftudp"

class ZoneMode(str, enum.Enum):
    POWER_ONLY = "power_only"
    HR_ONLY = "hr_only"
    HIGHER_WINS = "higher_wins"

VALID_DATA_SOURCES: tuple = tuple(DataSource)
VALID_ZONE_MODES: tuple = tuple(ZoneMode)

Node: Any = None
ANTPLUS_NETWORK_KEY: Any = None
PowerMeter: Any = None
PowerData: Any = None
HeartRate: Any = None
HeartRateData: Any = None

BleakClient: Any = None
BleakScanner: Any = None

# --- Külső könyvtárak (opcionális importok – a program importálható marad teszteléshez) ---
_ANTPLUS_AVAILABLE: bool = False
_BLEAK_AVAILABLE: bool = False
try:
    from openant.easy.node import Node
    from openant.devices import ANTPLUS_NETWORK_KEY
    from openant.devices.power_meter import PowerMeter, PowerData
    from openant.devices.heart_rate import HeartRate, HeartRateData

    _ANTPLUS_AVAILABLE = True
except ImportError:
    pass

try:
    from bleak import BleakClient, BleakScanner

    _BLEAK_AVAILABLE = True
except ImportError:
    pass

_TKINTER_AVAILABLE: bool = False

if TYPE_CHECKING:
    import tkinter as tk
    import tkinter.font as tkfont
else:
    try:
        import tkinter as tk
        import tkinter.font as tkfont

        _TKINTER_AVAILABLE = True
    except ImportError:
        tk = None  # type: ignore[assignment]
        tkfont = None  # type: ignore[assignment]

__version__ = "1.0.0"

logger = logging.getLogger("swift_fan_controller_new")


# ============================================================
# ALAPÉRTELMEZETT BEÁLLÍTÁSOK
# ============================================================

DEFAULT_SETTINGS: Dict[str, Any] = {
    "global_settings": {
        "cooldown_seconds": 120,
        "buffer_seconds": 3,
        "minimum_samples": 6,
        "buffer_rate_hz": 4,
        "dropout_timeout": 5,
    },
    "power_zones": {
        "ftp": 180,
        "min_watt": 0,
        "max_watt": 1000,
        "z1_max_percent": 60,
        "z2_max_percent": 89,
        "zero_power_immediate": False,
        "heart_rate_zones": {
            "enabled": False,
            "max_hr": 185,
            "resting_hr": 60,
            "zone_mode": ZoneMode.POWER_ONLY,
            "z1_max_percent": 70,
            "z2_max_percent": 80,
            "valid_min_hr": 30,
            "valid_max_hr": 220,
            "zero_hr_immediate": False,
        },
    },
    "ble": {
        "device_name": None,
        "scan_timeout": 10,
        "connection_timeout": 15,
        "reconnect_interval": 5,
        "max_retries": 10,
        "command_timeout": 3,
        "service_uuid": "0000ffe0-0000-1000-8000-00805f9b34fb",
        "characteristic_uuid": "0000ffe1-0000-1000-8000-00805f9b34fb",
        "pin_code": None,
    },
    "datasource": {
        "power_source": DataSource.ANTPLUS,
        "hr_source": DataSource.ANTPLUS,
        "BLE_buffer_seconds": 3,
        "BLE_minimum_samples": 6,
        "BLE_buffer_rate_hz": 4,
        "BLE_dropout_timeout": 5,
        "ANT_buffer_seconds": 3,
        "ANT_minimum_samples": 6,
        "ANT_buffer_rate_hz": 4,
        "ANT_dropout_timeout": 5,
        "zwiftUDP_buffer_seconds": 10,
        "zwiftUDP_minimum_samples": 2,
        "zwiftUDP_buffer_rate_hz": 4,
        "zwiftUDP_dropout_timeout": 15,
        "ant_power_device_id": 0,
        "ant_hr_device_id": 0,
        "ant_power_reconnect_interval": 5,
        "ant_power_max_retries": 10,
        "ant_hr_reconnect_interval": 5,
        "ant_hr_max_retries": 10,
        "ble_power_device_name": None,
        "ble_power_scan_timeout": 10,
        "ble_power_reconnect_interval": 5,
        "ble_power_max_retries": 10,
        "ble_hr_device_name": None,
        "ble_hr_scan_timeout": 10,
        "ble_hr_reconnect_interval": 5,
        "ble_hr_max_retries": 10,
        "zwift_udp_port": 7878,
        "zwift_udp_host": "127.0.0.1",
    },
}


# ============================================================
# BEÁLLÍTÁSOK BETÖLTÉSE
# ============================================================


def load_settings(settings_file: str = "settings.json") -> Dict[str, Any]:
    """Betölti és validálja a JSON beállítási fájlt.

    Alapértelmezett értékekből indul ki (DEFAULT_SETTINGS), majd felülírja
    az érvényes, fájlból betöltött értékekkel. Hibás mezőnél az alapértelmezett
    marad érvényben (figyelmeztetéssel).

    Ha a fájl nem létezik, automatikusan létrehozza az alapértelmezettekkel.

    Args:
        settings_file: A JSON beállítások fájl elérési útja.

    Returns:
        Validált beállítások dict-je.
    """
    settings = copy.deepcopy(DEFAULT_SETTINGS)

    try:
        with open(settings_file, "r", encoding="utf-8") as f:
            loaded = json.load(f)
    except FileNotFoundError:
        print(
            f"⚠ '{settings_file}' nem található, alapértelmezett beállítások használata."
        )
        _save_default_settings(settings_file, settings)
        return settings
    except (json.JSONDecodeError, OSError) as exc:
        print(f"⚠ '{settings_file}' beolvasási hiba: {exc}. Alapértelmezés használata.")
        return settings

    # --- Globális beállítások ---
    if isinstance(loaded.get("global_settings"), dict):
        gs = loaded["global_settings"]
        _load_int(gs, settings["global_settings"], "cooldown_seconds", 0, 300)
        _load_int(gs, settings["global_settings"], "buffer_seconds", 1, 10)
        _load_int(gs, settings["global_settings"], "minimum_samples", 1, 1000)
        _load_int(gs, settings["global_settings"], "buffer_rate_hz", 1, 60)
        _load_int(gs, settings["global_settings"], "dropout_timeout", 1, 120)
    # --- Teljesítmény zóna beállítások ---
    if isinstance(loaded.get("power_zones"), dict):
        pz = loaded["power_zones"]
        _load_int(pz, settings["power_zones"], "ftp", 100, 500)
        _load_int(pz, settings["power_zones"], "min_watt", 0, 9999)
        _load_int(pz, settings["power_zones"], "max_watt", 1, 100000)
        _load_int(pz, settings["power_zones"], "z1_max_percent", 1, 100)
        _load_int(pz, settings["power_zones"], "z2_max_percent", 1, 100)
        _load_bool(pz, settings["power_zones"], "zero_power_immediate")

        # --- Szívfrekvencia zóna beállítások ---
        if isinstance(pz.get("heart_rate_zones"), dict):
            hrz = pz["heart_rate_zones"]
            _load_bool(hrz, settings["power_zones"]["heart_rate_zones"], "enabled")
            _load_int(hrz, settings["power_zones"]["heart_rate_zones"], "max_hr", 100, 220)
            _load_int(hrz, settings["power_zones"]["heart_rate_zones"], "resting_hr", 30, 100)
            if hrz.get("zone_mode") in VALID_ZONE_MODES:
                settings["power_zones"]["heart_rate_zones"]["zone_mode"] = hrz["zone_mode"]
            _load_int(hrz, settings["power_zones"]["heart_rate_zones"], "valid_min_hr", 30, 100)
            _load_int(hrz, settings["power_zones"]["heart_rate_zones"], "valid_max_hr", 150, 300)
            _load_int(hrz, settings["power_zones"]["heart_rate_zones"], "z1_max_percent", 1, 100)
            _load_int(hrz, settings["power_zones"]["heart_rate_zones"], "z2_max_percent", 1, 100)
            _load_bool(hrz, settings["power_zones"]["heart_rate_zones"], "zero_hr_immediate")

    # --- BLE kimeneti beállítások ---
    if isinstance(loaded.get("ble"), dict):
        b = loaded["ble"]
        # device_name: null vagy "" → auto-discovery mód (None-ra állítva)
        if "device_name" in b:
            dn = b["device_name"]
            if dn is None or (isinstance(dn, str) and not dn.strip()):
                settings["ble"]["device_name"] = None
            elif isinstance(dn, str) and dn.strip():
                settings["ble"]["device_name"] = dn.strip()
        _load_int(b, settings["ble"], "scan_timeout", 1, 60)
        _load_int(b, settings["ble"], "connection_timeout", 1, 60)
        _load_int(b, settings["ble"], "reconnect_interval", 1, 60)
        _load_int(b, settings["ble"], "max_retries", 1, 100)
        _load_int(b, settings["ble"], "command_timeout", 1, 30)
        if isinstance(b.get("service_uuid"), str) and b["service_uuid"]:
            settings["ble"]["service_uuid"] = b["service_uuid"]
        if isinstance(b.get("characteristic_uuid"), str) and b["characteristic_uuid"]:
            settings["ble"]["characteristic_uuid"] = b["characteristic_uuid"]
        if "pin_code" in b:
            pc = b["pin_code"]
            if pc is None:
                settings["ble"]["pin_code"] = None
            elif isinstance(pc, int) and not isinstance(pc, bool) and 0 <= pc <= 999999:
                settings["ble"]["pin_code"] = str(pc)
                if len(str(pc)) < 6:
                    print(
                        f"⚠ pin_code int-ként megadva ({pc}) → \"{str(pc)}\". "
                        f"Ha vezető nullákra van szükség, string-ként add meg: "
                        f"\"pin_code\": \"{pc:06d}\""
                    )
            elif isinstance(pc, str) and pc.isdigit() and 0 < len(pc) <= 20:
                settings["ble"]["pin_code"] = pc
            else:
                print(f"⚠ Érvénytelen 'pin_code' érték: {pc}")

    # --- Adatforrás beállítások ---
    if isinstance(loaded.get("datasource"), dict):
        ds = loaded["datasource"]

        if ds.get("power_source") in VALID_DATA_SOURCES:
            settings["datasource"]["power_source"] = ds["power_source"]
        if ds.get("hr_source") in VALID_DATA_SOURCES:
            settings["datasource"]["hr_source"] = ds["hr_source"]

        # --- ANT+ eszköz beállítások ---
        for key in ("ant_power_device_id", "ant_hr_device_id"):
            _load_int(ds, settings["datasource"], key, 0, 65535)

        for key in (
            "ant_power_reconnect_interval",
            "ant_hr_reconnect_interval",
        ):
            _load_int(ds, settings["datasource"], key, 1, 60)

        for key in ("ant_power_max_retries", "ant_hr_max_retries"):
            _load_int(ds, settings["datasource"], key, 1, 100)

        # --- BLE eszköz beállítások ---
        for key in ("ble_power_device_name", "ble_hr_device_name"):
            if key in ds and (ds[key] is None or isinstance(ds[key], str)):
                settings["datasource"][key] = ds[key]

        for key in (
            "ble_power_scan_timeout",
            "ble_power_reconnect_interval",
            "ble_hr_scan_timeout",
            "ble_hr_reconnect_interval",
        ):
            _load_int(ds, settings["datasource"], key, 1, 60)

        for key in ("ble_power_max_retries", "ble_hr_max_retries"):
            _load_int(ds, settings["datasource"], key, 1, 100)

        if isinstance(ds.get("zwift_udp_host"), str) and ds["zwift_udp_host"]:
            settings["datasource"]["zwift_udp_host"] = ds["zwift_udp_host"]
        _load_int(ds, settings["datasource"], "zwift_udp_port", 1024, 65535)

        for prefix in ("BLE", "ANT", "zwiftUDP"):
            _load_int(ds, settings["datasource"], f"{prefix}_buffer_seconds", 1, 60)
            _load_int(ds, settings["datasource"], f"{prefix}_minimum_samples", 1, 100)
            _load_int(ds, settings["datasource"], f"{prefix}_buffer_rate_hz", 1, 60)
            _load_int(ds, settings["datasource"], f"{prefix}_dropout_timeout", 1, 300)

    # --- Kereszt-validációk ---

    # 1) Forrás-specifikus minimum_samples <= buffer_seconds * buffer_rate_hz
    try:
        ds_cfg = settings["datasource"]
        for prefix in ("BLE", "ANT", "zwiftUDP"):
            gs = settings["global_settings"]
            bs = int(
                ds_cfg.get(
                    f"{prefix}_buffer_seconds", gs.get("buffer_seconds", 3)
                )
            )
            ms = int(
                ds_cfg.get(
                    f"{prefix}_minimum_samples", gs.get("minimum_samples", 6)
                )
            )
            brz = int(
                ds_cfg.get(
                    f"{prefix}_buffer_rate_hz", gs.get("buffer_rate_hz", 4)
                )
            )
            if bs > 0 and brz > 0:
                max_samples = bs * brz
                if ms > max_samples:
                    print(
                        f"⚠ [{prefix}] Érvénytelen minimum_samples ({ms}) – "
                        f"nagyobb, mint buffer_seconds * buffer_rate_hz "
                        f"({bs} * {brz} = {max_samples}). "
                        f"{prefix}_minimum_samples {max_samples}-re állítva."
                    )
                    ds_cfg[f"{prefix}_minimum_samples"] = max_samples
    except Exception as exc:
        print(f"⚠ minimum_samples/buffer_seconds kereszt-validáció sikertelen: {exc}")

    # 2) Power zóna: min_watt < max_watt
    try:
        zt = settings.get("power_zones") or {}
        min_watt = zt.get("min_watt")
        max_watt = zt.get("max_watt")
        if isinstance(min_watt, int) and isinstance(max_watt, int):
            if min_watt > max_watt:
                print(
                    f"⚠ Érvénytelen watt tartomány (min_watt={min_watt}, max_watt={max_watt}). "
                    f"Feltételezett felcserélés, értékek megfordítva."
                )
                zt["min_watt"], zt["max_watt"] = max_watt, min_watt
            elif min_watt == max_watt:
                print(
                    f"⚠ min_watt és max_watt azonos értékű ({min_watt}). "
                    f"max_watt {min_watt + 1}-re állítva."
                )
                zt["max_watt"] = min_watt + 1
    except Exception as exc:
        print(f"⚠ Watt zóna kereszt-validáció sikertelen: {exc}")

    # 3) Power zóna százalékok: z1_max_percent < z2_max_percent
    try:
        zt = settings.get("power_zones") or {}
        z1p = zt.get("z1_max_percent")
        z2p = zt.get("z2_max_percent")
        if isinstance(z1p, int) and isinstance(z2p, int) and z1p >= z2p:
            low, high = min(z1p, z2p), max(z1p, z2p)
            if low == high:
                high = min(100, low + 1)
            print(
                f"⚠ Érvénytelen power zóna százalékok (z1={z1p}, z2={z2p}). "
                f"Javítva: z1={low}, z2={high}."
            )
            zt["z1_max_percent"] = low
            zt["z2_max_percent"] = high
    except Exception as exc:
        print(f"⚠ Power zóna százalék kereszt-validáció sikertelen: {exc}")

    # 4) HR zónák: z1_max_percent < z2_max_percent és resting_hr < max_hr
    try:
        hrz = settings["power_zones"].get("heart_rate_zones") or {}
        z1p = hrz.get("z1_max_percent")
        z2p = hrz.get("z2_max_percent")
        if isinstance(z1p, int) and isinstance(z2p, int):
            if z1p >= z2p:
                low = min(z1p, z2p)
                high = max(z1p, z2p)
                if low == high:
                    high = min(100, low + 1)
                print(
                    f"⚠ Érvénytelen HR zóna százalékok (z1_max_percent={z1p}, z2_max_percent={z2p}). "
                    f"Értékek rendezése és legalább 1% különbség biztosítása."
                )
                hrz["z1_max_percent"] = low
                hrz["z2_max_percent"] = high
        max_hr = hrz.get("max_hr")
        resting_hr = hrz.get("resting_hr")
        if isinstance(max_hr, int) and isinstance(resting_hr, int):
            if resting_hr >= max_hr:
                new_rest = max(30, max_hr - 1)
                print(
                    f"⚠ Érvénytelen HR értékek (resting_hr={resting_hr}, max_hr={max_hr}). "
                    f"resting_hr {new_rest}-re állítva."
                )
                hrz["resting_hr"] = new_rest
    except Exception as exc:
        print(f"⚠ HR zóna kereszt-validáció sikertelen: {exc}")

    # 5) valid_min_hr < valid_max_hr
    try:
        hrz = settings["power_zones"].get("heart_rate_zones") or {}
        valid_min = hrz.get("valid_min_hr")
        valid_max = hrz.get("valid_max_hr")
        if isinstance(valid_min, int) and isinstance(valid_max, int):
            if valid_min >= valid_max:
                print(
                    f"⚠ valid_min_hr ({valid_min}) >= valid_max_hr ({valid_max}), "
                    f"alapértelmezés visszaállítva."
                )
                hrz["valid_min_hr"] = DEFAULT_SETTINGS["power_zones"]["heart_rate_zones"][
                    "valid_min_hr"
                ]
                hrz["valid_max_hr"] = DEFAULT_SETTINGS["power_zones"]["heart_rate_zones"][
                    "valid_max_hr"
                ]
    except Exception as exc:
        print(f"⚠ valid_hr kereszt-validáció sikertelen: {exc}")

    return settings


def _load_int(src: dict, dst: dict, key: str, lo: int, hi: int) -> None:
    """Helper: int mezőt tölt be érvényes tartomány esetén.

    Törtszámokat és bool értékeket visszautasítja.

    Args:
        src: Forrás dict (betöltött JSON).
        dst: Cél dict (settings).
        key: A mező neve.
        lo:  Minimális elfogadható érték (inclusive).
        hi:  Maximális elfogadható érték (inclusive).
    """
    if key not in src:
        return
    v = src[key]
    if isinstance(v, bool):
        print(f"⚠ Érvénytelen '{key}' érték: {v!r} (true/false helyett {lo}–{hi} közötti egész kell)")
        return
    if isinstance(v, float) and not v.is_integer():
        print(f"⚠ Érvénytelen '{key}' érték: {v} (törtrész nem elfogadott, egész kell)")
        return
    if isinstance(v, (int, float)) and lo <= v <= hi:
        dst[key] = int(v)
    else:
        print(f"⚠ Érvénytelen '{key}' érték: {v} ({lo}–{hi} közötti egész kell)")


def _load_bool(src: dict, dst: dict, key: str) -> None:
    """Helper: bool mezőt tölt be."""
    if key in src:
        if isinstance(src[key], bool):
            dst[key] = src[key]
        else:
            print(f"⚠ Érvénytelen '{key}' érték: {src[key]} (true/false kell)")


def _save_default_settings(path: str, settings: Dict[str, Any]) -> None:
    """Létrehozza az alapértelmezett settings.json fájlt."""
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2, ensure_ascii=False)
        print(f"✓ Alapértelmezett '{path}' létrehozva.")
    except OSError as exc:
        print(f"✗ Nem sikerült létrehozni a '{path}' fájlt: {exc}")


# ============================================================
# TISZTA FÜGGVÉNYEK – ZÓNA SZÁMÍTÁS
# ============================================================


def _resolve_buffer_settings(settings: Dict[str, Any], role: str) -> Dict[str, Any]:
    """
    Visszaadja a megfelelő buffer/dropout paramétereket a megadott szerephez.

    A role ("power" vagy "hr") alapján meghatározza az aktív adatforrást
    (datasource.power_source ill. datasource.hr_source mezőkből), majd
    visszaadja a forrás-specifikus buffer beállításokat.
    Fallback: globális buffer_seconds / minimum_samples / buffer_rate_hz / dropout_timeout.

    Args:
        settings: Betöltött beállítások dict-je.
        role:     "power" – a power_source alapján,
                  "hr"    – a hr_source alapján.
    Returns:
        Dict: buffer_seconds, minimum_samples, buffer_rate_hz, dropout_timeout
    """
    ds = settings["datasource"]
    source_key = "power_source" if role == "power" else "hr_source"
    source = ds.get(source_key, DataSource.ANTPLUS)

    if source == DataSource.BLE:
        prefix = "BLE"
    elif source == DataSource.ANTPLUS:
        prefix = "ANT"
    else:  # zwiftudp
        prefix = "zwiftUDP"

    gs = settings["global_settings"]
    return {
        "buffer_seconds": ds.get(
            f"{prefix}_buffer_seconds", gs.get("buffer_seconds", 3)
        ),
        "minimum_samples": ds.get(
            f"{prefix}_minimum_samples", gs.get("minimum_samples", 6)
        ),
        "buffer_rate_hz": ds.get(
            f"{prefix}_buffer_rate_hz", gs.get("buffer_rate_hz", 4)
        ),
        "dropout_timeout": ds.get(
            f"{prefix}_dropout_timeout", gs.get("dropout_timeout", 5)
        ),
    }


def calculate_power_zones(
    ftp: int,
    min_watt: int,
    max_watt: int,
    z1_pct: int,
    z2_pct: int,
) -> Dict[int, Tuple[int, int]]:
    """Kiszámítja a teljesítmény zóna határokat.

    Args:
        ftp: Funkcionális küszöbteljesítmény (W).
        min_watt: Minimális érvényes pozitív teljesítmény (W).
        max_watt: Maximális érvényes teljesítmény (W).
        z1_pct: Z1 felső határ az FTP %-ában.
        z2_pct: Z2 felső határ az FTP %-ában.

    Returns:
        Dict formátum: {0: (0,0), 1: (1, z1_max), 2: (z1_max+1, z2_max), 3: (z2_max+1, max_watt)}
    """
    # max(1, ...) védi az érvénytelen z1_max=0 esetet (pl. nagyon alacsony FTP/százalék)
    z1_max = max(1, int(ftp * z1_pct / 100))
    z2_max = max(2, min(int(ftp * z2_pct / 100), max_watt))
    z1_max = min(z1_max, z2_max - 1)
    return {
        0: (0, 0),
        1: (1, z1_max),
        2: (z1_max + 1, z2_max),
        3: (z2_max + 1, max_watt),
    }


def calculate_hr_zones(
    max_hr: int,
    resting_hr: int,
    z1_pct: int,
    z2_pct: int,
) -> Dict[str, int]:
    """Kiszámítja a HR zóna határokat bpm-ben.

    Args:
        max_hr: Maximális szívfrekvencia (bpm).
        resting_hr: Pihenő szívfrekvencia (bpm); ez alatt Z0.
        z1_pct: Z1 felső határ a max_hr %-ában.
        z2_pct: Z2 felső határ a max_hr %-ában.

    Returns:
        Dict: {'resting': int, 'z1_max': int, 'z2_max': int}
    """
    return {
        "resting": resting_hr,
        "z1_max": int(max_hr * z1_pct / 100),
        "z2_max": int(max_hr * z2_pct / 100),
    }


def zone_for_power(power: float, zones: Dict[int, Tuple[int, int]]) -> int:
    """Meghatározza a teljesítmény zónát (0–3) az adott watt értékhez.

    Args:
        power: Teljesítmény wattban.
        zones: Zóna határok dict-je (calculate_power_zones kimenetele).

    Returns:
        Zóna szám (0–3).
    """
    if power <= 0:
        return 0
    # Védekezés üres vagy hibás zones dict ellen (ValueError elkerülése)
    positive_lows = [lo for lo, hi in zones.values() if lo > 0]
    if not positive_lows:
        return 0
    min_lo = min(positive_lows)
    if power < min_lo:
        return 0
    for zone_num in sorted(zones):
        lo, hi = zones[zone_num]
        if lo <= power <= hi:
            return zone_num
    return 3  # csak max_watt felett érthető el


def zone_for_hr(hr: int, hr_zones: Dict[str, int]) -> int:
    """Meghatározza a HR zónát (0–3) az adott bpm értékhez.

    Args:
        hr: Szívfrekvencia bpm-ben.
        hr_zones: HR zóna határok dict-je (calculate_hr_zones kimenetele).

    Returns:
        Zóna szám (0–3).
    """
    if hr <= 0 or hr < hr_zones["resting"]:
        return 0
    if hr <= hr_zones["z1_max"]:
        return 1
    if hr <= hr_zones["z2_max"]:
        return 2
    return 3


def is_valid_power(power: Any, min_watt: int, max_watt: int) -> bool:
    """Ellenőrzi, hogy az érték érvényes teljesítmény adat-e.

    Args:
        power: Az ellenőrizendő érték.
        min_watt: Minimális érvényes pozitív watt (0 és min_watt között elutasítva).
        max_watt: Maximális érvényes watt.

    Returns:
        True, ha érvényes teljesítmény adat.
    """
    if isinstance(power, bool):
        return False
    if not isinstance(power, (int, float)):
        return False
    if math.isnan(power) or math.isinf(power):
        return False
    if power < 0 or power > max_watt:
        return False
    if 0 < power < min_watt:
        return False
    return True


def is_valid_hr(hr: Any, valid_min_hr: int, valid_max_hr: int) -> bool:
    """Ellenőrzi, hogy az érték érvényes szívfrekvencia adat-e.

    Args:
        hr: Az ellenőrizendő érték.
        valid_min_hr: Minimális érvényes HR érték (bpm).
        valid_max_hr: Maximális érvényes HR érték (bpm).

    Returns:
        True, ha érvényes HR adat.
    """
    if isinstance(hr, bool):
        return False
    if not isinstance(hr, (int, float)):
        return False
    if math.isnan(hr) or math.isinf(hr):
        return False
    if hr < valid_min_hr or hr > valid_max_hr:
        return False
    return True


# ============================================================
# TISZTA FÜGGVÉNYEK – ÁTLAGSZÁMÍTÁS
# ============================================================


def compute_average(samples: deque) -> Optional[float]:
    """Kiszámítja a minták számtani átlagát.

    Args:
        samples: Mintákat tartalmazó deque.

    Returns:
        Az átlag float értéke, vagy None, ha nincs minta.
    """
    if not samples:
        return None
    return sum(samples) / len(samples)


# ============================================================
# TISZTA FÜGGVÉNYEK – ZÓNA LOGIKA (higher_wins, zone_mode)
# ============================================================


def higher_wins(zone_a: int, zone_b: int) -> int:
    """A két zóna közül a nagyobbat adja vissza.

    Args:
        zone_a: Első zóna (0–3).
        zone_b: Második zóna (0–3).

    Returns:
        A nagyobb zóna szám.
    """
    return max(zone_a, zone_b)


def apply_zone_mode(
    power_zone: Optional[int],
    hr_zone: Optional[int],
    zone_mode: ZoneMode,
) -> Optional[int]:
    """A zone_mode alapján kombinálja a power és HR zónákat.

    Zóna módok:
        "power_only"  – csak a teljesítmény zóna dönt (HR figyelmen kívül)
        "hr_only"     – csak a HR zóna dönt (power figyelmen kívül)
        "higher_wins" – a kettő közül a nagyobb dönt

    Args:
        power_zone: Teljesítmény zóna (0–3), vagy None ha nem elérhető.
        hr_zone: HR zóna (0–3), vagy None ha nem elérhető.
        zone_mode: A kombinálási mód ("power_only", "hr_only", "higher_wins").

    Returns:
        A végső zóna szám (0–3), vagy None ha nincs elég adat.
    """
    if zone_mode == ZoneMode.POWER_ONLY:
        return power_zone
    if zone_mode == ZoneMode.HR_ONLY:
        return hr_zone
    # higher_wins: mindkét forrásból a nagyobb
    if power_zone is not None and hr_zone is not None:
        return higher_wins(power_zone, hr_zone)
    if power_zone is not None:
        return power_zone
    return hr_zone


# ============================================================
# COOLDOWN LOGIKA
# ============================================================


class CooldownController:
    """Cooldown logika kezelője zóna csökkentés esetén.

    Zóna csökkentésekor nem vált azonnal, hanem cooldown_seconds
    másodpercig vár. Zóna növelésekor azonnal vált, cooldown nélkül.

    Adaptív cooldown módosítások:
        - Nagy zónaesés (>= 2 szint) vagy 0W → cooldown felezés (gyorsabb leállás)
        - Pending zóna emelkedik → cooldown duplázás (lassabb emelkedés)

    Attribútumok:
        cooldown_seconds: A cooldown időtartama másodpercben.
        active: True, ha a cooldown timer fut.
        start_time: A cooldown indítási ideje (time.monotonic()).
        pending_zone: A cooldown lejárta után alkalmazandó zóna.
        can_halve: True, ha a cooldown felezés még elvégezhető.
        can_double: True, ha a cooldown duplázás még elvégezhető.
    """

    PRINT_INTERVAL = 10.0

    def __init__(self, cooldown_seconds: int) -> None:
        self._lock = threading.Lock()
        self.cooldown_seconds = cooldown_seconds
        self.active = False
        self.start_time = 0.0
        self.pending_zone: Optional[int] = None
        self.can_halve = True
        self.can_double = False
        self._last_print = 0.0

    def process(
        self,
        current_zone: Optional[int],
        new_zone: int,
        zero_immediate: bool,
    ) -> Optional[int]:
        """Feldolgozza az új zóna javaslatot és alkalmazza a cooldown logikát.

        Args:
            current_zone: Az aktuális zóna (None = még nincs döntés).
            new_zone: Az új javasolt zóna (0–3).
            zero_immediate: True, ha 0W esetén azonnali leállás szükséges.

        Returns:
            A küldendő zóna szintje, ha változás szükséges; None egyébként.
        """
        with self._lock:
            return self._process_locked(current_zone, new_zone, zero_immediate)

    def _process_locked(
        self,
        current_zone: Optional[int],
        new_zone: int,
        zero_immediate: bool,
    ) -> Optional[int]:
        """Belső process logika – lock alatt hívandó."""
        now = time.monotonic()

        # Első döntés – nincs előző zóna
        if current_zone is None:
            self._reset_locked()
            return new_zone

        # 0W azonnali leállás (zero_power_immediate=True)
        if new_zone == 0 and zero_immediate:
            if current_zone != 0:
                self._reset_locked()
                print("✓ 0W detektálva: azonnali leállás (cooldown nélkül)")
                return 0
            return None

        # Aktív cooldown kezelése
        if self.active:
            return self._handle_active(current_zone, new_zone, now)

        # Nincs cooldown – normál zónaváltás logika
        if new_zone == current_zone:
            return None
        if new_zone > current_zone:
            return new_zone
        # cooldown_seconds == 0 → azonnali váltás, nincs cooldown
        if self.cooldown_seconds == 0:
            return new_zone
        # Zóna csökkentés → cooldown indul
        return self._start(current_zone, new_zone, now)

    def _start(self, current_zone: int, new_zone: int, now: float) -> Optional[int]:
        """Cooldown indítása zóna csökkentésnél."""
        self.active = True
        self.start_time = now
        self.pending_zone = new_zone
        self.can_halve = True
        self.can_double = False
        # _last_print beállítása megakadályozza az azonnali dupla kiírást
        # az első _handle_active hívásnál
        self._last_print = now
        print(
            f"🕐 Cooldown indítva: {self.cooldown_seconds}s várakozás (cél: {new_zone})"
        )
        # Nagy zónaesés esetén azonnali felezés
        if new_zone == 0 or (current_zone - new_zone >= 2):
            self._halve(now)
        return None

    def _handle_active(
        self, current_zone: int, new_zone: int, now: float
    ) -> Optional[int]:
        """Aktív cooldown feldolgozása – lock alatt hívandó."""
        # Zóna emelkedés → cooldown törlése
        if new_zone >= current_zone:
            self._reset_locked()
            if new_zone > current_zone:
                print(f"✓ Teljesítmény emelkedés: cooldown törölve → zóna: {new_zone}")
                return new_zone
            return None

        elapsed = now - self.start_time

        # Cooldown lejárt
        if elapsed >= self.cooldown_seconds:
            target = new_zone
            self._reset_locked()
            if target != current_zone:
                print(f"✓ Cooldown lejárt! Zóna váltás: {current_zone} → {target}")
                return target
            print("✓ Cooldown lejárt, nincs zónaváltás (már a célzónában)")
            return None

        remaining = self.cooldown_seconds - elapsed

        # Pending zóna frissítése + adaptív cooldown módosítás
        if new_zone != self.pending_zone:
            old_pending = self.pending_zone
            self.pending_zone = new_zone
            if old_pending is not None and new_zone > old_pending and self.can_double:
                self._double(now)
                remaining = self.cooldown_seconds - (now - self.start_time)
                print(
                    f"🕐 Cooldown aktív: még {remaining:.0f}s (várakozó zóna: {new_zone})"
                )
            elif (new_zone == 0 or (current_zone - new_zone >= 2)) and self.can_halve:
                self._halve(now)
                remaining = self.cooldown_seconds - (now - self.start_time)
                print(
                    f"🕐 Cooldown aktív: még {remaining:.0f}s (várakozó zóna: {new_zone})"
                )
            else:
                print(
                    f"🕐 Cooldown aktív: még {remaining:.0f}s (várakozó zóna: {new_zone})"
                )
            self._last_print = now
        elif now - self._last_print >= self.PRINT_INTERVAL:
            print(
                f"🕐 Cooldown aktív: még {remaining:.0f}s (várakozó: {self.pending_zone})"
            )
            self._last_print = now

        return None

    def _halve(self, now: float) -> None:
        """Felezi a maradék cooldown időt."""
        remaining = max(0.0, self.cooldown_seconds - (now - self.start_time))
        new_remaining = remaining / 2
        self.start_time = now - (self.cooldown_seconds - new_remaining)
        self.can_halve = False
        self.can_double = True
        print(f"🕐 Cooldown felezve: {remaining:.0f}s → {new_remaining:.0f}s")

    def _double(self, now: float) -> None:
        """Duplázza a maradék cooldown időt."""
        remaining = max(0.0, self.cooldown_seconds - (now - self.start_time))
        new_remaining = min(remaining * 2, float(self.cooldown_seconds))
        self.start_time = now - (self.cooldown_seconds - new_remaining)
        self.can_double = False
        self.can_halve = True
        print(f"🕐 Cooldown duplázva: {remaining:.0f}s → {new_remaining:.0f}s")

    def reset(self) -> None:
        """Törli a cooldown állapotát (publikus API, szálbiztos)."""
        with self._lock:
            self._reset_locked()

    def _reset_locked(self) -> None:
        """Törli a cooldown állapotát – lock alatt hívandó."""
        self.active = False
        self.pending_zone = None
        self.can_halve = True
        self.can_double = False

    def snapshot(self) -> Tuple[bool, float]:
        """Szálbiztos pillanatfelvétel a HUD számára.

        Returns:
            (active, remaining_seconds) tuple.
        """
        with self._lock:
            if not self.active:
                return False, 0.0
            remaining = max(0.0, self.cooldown_seconds - (time.monotonic() - self.start_time))
            return True, remaining

    def __repr__(self) -> str:
        active, remaining = self.snapshot()
        return (
            f"CooldownController(active={active}, remaining={remaining:.1f}s, "
            f"pending_zone={self.pending_zone}, cooldown={self.cooldown_seconds}s)"
        )


# ============================================================
# GÖRDÜLŐ ÁTLAGOLÁS – KÖZÖS BASE CLASS
# ============================================================


class _RollingAverager:
    """Gördülő átlagot számít bejövő numerikus mintákból.

    buffer_rate_hz mintát vár másodpercenként, és buffer_seconds
    másodpercnyi ablakot tart. Az effective_minimum automatikusan
    alkalmazkodik a valódi buffer méretéhez, így akkor is
    számol átlagot, ha kevesebb adat érkezik, mint minimum_samples.

    Attribútumok:
        buffer: Mintákat tároló deque (maxlen = buffer_seconds × buffer_rate_hz).
        minimum_samples: Kívánt minimum mintaszám érvényes átlaghoz.
        effective_minimum: Ténylegesen alkalmazott minimum (max: buffersize // 2).
        buffersize: A buffer maximális mérete.
    """

    def __init__(
        self,
        buffer_seconds: int,
        minimum_samples: int,
        buffer_rate_hz: int = 4,
        label: str = "adat",
    ) -> None:
        rate = max(1, int(buffer_rate_hz))
        self.buffersize = max(1, int(buffer_seconds) * rate)
        self.buffer: deque = deque(maxlen=self.buffersize)
        self.minimum_samples = minimum_samples
        # Védelem: effective_minimum soha nem nagyobb, mint a buffer fele
        self.effective_minimum = min(self.minimum_samples, max(1, self.buffersize // 2))
        self._label = label

    def add_sample(self, value: float) -> Optional[float]:
        """Új minta hozzáadása és az átlag visszaadása, ha elég minta van."""
        self.buffer.append(value)
        if len(self.buffer) < self.effective_minimum:
            logging.debug(
                "%s adatok gyűjtése: %d/%d (effective min)",
                self._label,
                len(self.buffer),
                self.effective_minimum,
            )
            return None
        return compute_average(self.buffer)

    def clear(self) -> None:
        """Törli az összes pufferelt mintát."""
        self.buffer.clear()


class PowerAverager(_RollingAverager):
    """Gördülő átlagszámítás teljesítmény (watt) mintákhoz."""

    def __init__(
        self, buffer_seconds: int, minimum_samples: int, buffer_rate_hz: int = 4
    ) -> None:
        super().__init__(buffer_seconds, minimum_samples, buffer_rate_hz, label="Power")


class HRAverager(_RollingAverager):
    """Gördülő átlagszámítás szívfrekvencia (bpm) mintákhoz."""

    def __init__(
        self, buffer_seconds: int, minimum_samples: int, buffer_rate_hz: int = 4
    ) -> None:
        super().__init__(buffer_seconds, minimum_samples, buffer_rate_hz, label="HR")


# ============================================================
# KONZOLOS KIÍRÁS (throttle-olt)
# ============================================================


class ConsolePrinter:
    """Throttle-olt konzol kiírás – ugyanaz az üzenet nem jelenhet meg túl sűrűn.

    Minden üzenettípushoz (key) külön időzítőt tart. Az üzenet csak
    akkor kerül kiírásra, ha az utolsó kiírás óta legalább interval
    másodperc telt el.

    Megjegyzés: a metódus neve 'emit', hogy ne fedje el a beépített print()-et.

    Attribútumok:
        _last_times: Utolsó kiírás ideje üzenetkulcsonként.
    """

    def __init__(self) -> None:
        self._last_times: Dict[str, float] = {}

    def emit(self, key: str, message: str, interval: float = 1.0) -> bool:
        """Kiírja az üzenetet, ha az interval eltelt.

        Args:
            key: Egyedi kulcs az üzenet azonosításához (pl. "power_raw").
            message: A kiírandó szöveg.
            interval: Minimális másodpercek száma két azonos kulcsú kiírás között.

        Returns:
            True, ha az üzenet kiírásra kerül; False, ha throttle-olt.
        """
        now = time.monotonic()
        if now - self._last_times.get(key, 0.0) >= interval:
            print(message)
            self._last_times[key] = now
            return True
        return False

    def print(self, key: str, message: str, interval: float = 1.0) -> bool:
        """Deprecated: használd az emit()-et. Elfedi a beépített print()-et."""
        import warnings
        warnings.warn(
            "ConsolePrinter.print() deprecated, használd az emit()-et",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.emit(key, message, interval)


# ============================================================
# UI SNAPSHOT – szálbiztos adatcsere asyncio ↔ tkinter között
# ============================================================


@dataclasses.dataclass
class UISnapshot:
    """Szálbiztos snapshot az asyncio loop és a tkinter UI között.

    Az asyncio oldalon update() hívással frissítendő,
    a tkinter oldalon read() hívással olvasható.
    A threading.Lock garantálja a race condition-mentességet.
    """

    zone: Optional[int] = None
    avg_power: Optional[float] = None
    avg_hr: Optional[float] = None
    _lock: threading.Lock = dataclasses.field(default_factory=threading.Lock, repr=False)

    def update(
        self,
        zone: Optional[int],
        avg_power: Optional[float],
        avg_hr: Optional[float],
    ) -> None:
        """Frissíti a snapshot értékeit (asyncio szálból hívandó)."""
        with self._lock:
            self.zone = zone
            self.avg_power = avg_power
            self.avg_hr = avg_hr

    def read(self) -> Tuple[Optional[int], Optional[float], Optional[float]]:
        """Visszaadja a snapshot értékeit (tkinter szálból hívandó)."""
        with self._lock:
            return self.zone, self.avg_power, self.avg_hr


# ============================================================
# MEGOSZTOTT ÁLLAPOT
# ============================================================


class ControllerState:
    """A vezérlő megosztott állapota, asyncio.Lock-kal védve.

    Minden olyan mezőt tartalmaz, amelyet több asyncio korrutin is olvas
    vagy módosít. A lock biztosítja, hogy az olvasás-módosítás-írás
    műveletek atomikusak legyenek.

    Az ui_snapshot külön threading.Lock-kal védett, és kizárólag
    a tkinter UI frissítéséhez használatos (szálbiztos olvasás).

    Attribútumok:
        current_zone: Az aktuálisan aktív ventilátor zóna (None = nincs döntés még).
        current_power_zone: A legutóbb kiszámított power zóna.
        current_hr_zone: A legutóbb kiszámított HR zóna.
        current_avg_power: A legutóbbi átlagolt teljesítmény (W).
        current_avg_hr: A legutóbbi átlagolt HR (bpm).
        last_power_time: Utolsó power adat érkezési ideje (monotonic).
        last_hr_time: Utolsó HR adat érkezési ideje (monotonic), vagy None.
        lock: asyncio.Lock a párhuzamos módosítások ellen.
        ui_snapshot: UISnapshot a tkinter UI szálbiztos frissítéséhez.
    """

    def __init__(self) -> None:
        self.current_zone: Optional[int] = None
        self.current_power_zone: Optional[int] = None
        self.current_hr_zone: Optional[int] = None
        self.current_avg_power: Optional[float] = None
        self.current_avg_hr: Optional[float] = None
        self.last_power_time: Optional[float] = None
        self.last_hr_time: Optional[float] = None
        self.lock = asyncio.Lock()
        self.ui_snapshot = UISnapshot()

    def __repr__(self) -> str:
        return (
            f"ControllerState(zone={self.current_zone}, "
            f"power_zone={self.current_power_zone}, hr_zone={self.current_hr_zone}, "
            f"avg_power={self.current_avg_power}, avg_hr={self.current_avg_hr})"
        )


# ============================================================
# ZÓNA ELKÜLDÉSE (helper)
# ============================================================


async def send_zone(zone: int, zone_queue: asyncio.Queue[int]) -> None:
    """Zóna parancsot küld a BLE fan kimenet queue-ba.

    Ha a queue teli (maxsize=1), a régi parancsot elveti és az újat
    teszi be, hogy mindig a legfrissebb zóna kerüljön küldésre.
    A get_nowait() után a queue garantáltan üres, ezért put_nowait()
    nem dobhat QueueFull-t.

    Args:
        zone: Ventilátor zóna szintje (0–3).
        zone_queue: A BLE fan output asyncio.Queue-ja.
    """
    try:
        zone_queue.get_nowait()
    except asyncio.QueueEmpty:
        pass
    # get_nowait() után garantáltan szabad hely van
    zone_queue.put_nowait(zone)


# ============================================================
# BLE ESZKÖZ KERESÉS ÉS LOGOLÁS (közös segédfüggvények)
# ============================================================

_BLE_LOG_FILE = "ble_devices.log"


def _log_ble_devices_to_file(
    devices_info: List[Tuple[Optional[str], str, List[str]]],
    scan_context: str,
) -> None:
    """Talált BLE eszközöket ír a ble_devices.log fájlba (append módban).

    Csak olyan eszközöket ír a fájlba, amelyek address-e még nem szerepel benne.
    Ha a fájl nem létezik, létrehozza. Minden bejegyzés időbélyeggel ellátott.

    Args:
        devices_info: Lista (name, address, service_uuids) tuple-ökből.
        scan_context: A keresés kontextusa (pl. "BLE Fan", "BLE Power").
    """
    if not devices_info:
        return

    # Meglévő address-ek beolvasása a fájlból
    existing_addresses: set[str] = set()
    try:
        with open(_BLE_LOG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                # Sorok formátuma: "  név | ADDRESS | UUIDs: ..."
                parts = line.split("|")
                if len(parts) >= 2:
                    existing_addresses.add(parts[1].strip())
    except FileNotFoundError:
        pass  # Még nem létezik a fájl, minden eszköz új
    except OSError as exc:
        logger.warning(f"Nem sikerült olvasni a {_BLE_LOG_FILE} fájlt: {exc}")

    # Csak az új eszközök szűrése
    new_devices = [
        (name, addr, uuids)
        for name, addr, uuids in devices_info
        if addr not in existing_addresses
    ]

    if not new_devices:
        return

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(_BLE_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"\n--- BLE Scan ({scan_context}) @ {timestamp} ---\n")
            for name, addr, uuids in new_devices:
                uuid_str = ", ".join(uuids[:5]) if uuids else "–"
                f.write(f"  {name or '(névtelen)':30s} | {addr} | UUIDs: {uuid_str}\n")
    except OSError as exc:
        logger.warning(f"Nem sikerült írni a {_BLE_LOG_FILE} fájlba: {exc}")


def _print_ble_devices(
    devices_info: List[Tuple[Optional[str], str, List[str]]],
    scan_context: str,
    matched_addr: Optional[str] = None,
) -> None:
    """Talált BLE eszközöket ír a konzolra.

    Args:
        devices_info: Lista (name, address, service_uuids) tuple-ökből.
        scan_context: A keresés kontextusa.
        matched_addr: Az automatikusan kiválasztott eszköz címe (◄ jelöléshez).
    """
    print(f"\n📡 BLE Scan ({scan_context}): {len(devices_info)} eszköz található")
    for name, addr, uuids in devices_info:
        marker = " ◄ AUTO" if matched_addr and addr == matched_addr else ""
        icon = "📱" if name else "❓"
        uuid_str = ", ".join(uuids[:3]) if uuids else "–"
        print(f"  {icon} {name or '(névtelen)':30s} | {addr} | {uuid_str}{marker}")
    if not devices_info:
        print("  (nincs eszköz a közelben)")


async def _scan_ble_with_autodiscovery(
    scan_timeout: int,
    target_service_uuid: Optional[str],
    scan_context: str,
) -> Tuple[Optional[Any], List[Tuple[Optional[str], str, List[str]]]]:
    """BLE eszközöket keres, logolja, és opcionálisan keres egy megadott service UUID-val.

    Ha target_service_uuid megadva, az első olyan eszközt választja ki,
    amelyik hirdeti ezt az UUID-t.

    Args:
        scan_timeout: Keresési timeout másodpercben.
        target_service_uuid: Keresett service UUID (vagy None).
        scan_context: A keresés kontextusa (logoláshoz).

    Returns:
        (matched_device, devices_info) – matched_device az első egyezés (BLEDevice)
        vagy None, devices_info a teljes lista.
    """
    if not _BLEAK_AVAILABLE:
        return None, []

    devices_info: List[Tuple[Optional[str], str, List[str]]] = []
    matched: Optional[Any] = None

    try:
        # return_adv=True: dict[str, tuple[BLEDevice, AdvertisementData]]
        discovered = await BleakScanner.discover(
            timeout=scan_timeout, return_adv=True
        )

        items = discovered.values() if isinstance(discovered, dict) else discovered

        for item in items:
            device: Any = None
            uuids: List[str] = []
            if isinstance(item, tuple) and len(item) == 2:
                device = item[0]
                adv_data: Any = item[1]
                uuids = (
                    list(adv_data.service_uuids)
                    if hasattr(adv_data, "service_uuids") and adv_data.service_uuids
                    else []
                )
            else:
                device = item

            dev_name: Optional[str] = getattr(device, "name", None)
            dev_addr: str = getattr(device, "address", str(device))
            devices_info.append((dev_name, dev_addr, uuids))

            if target_service_uuid and matched is None:
                if any(u.lower() == target_service_uuid.lower() for u in uuids):
                    matched = device

    except TypeError:
        # Fallback régebbi Bleak verziókhoz (return_adv nem támogatott)
        devices: Any = await BleakScanner.discover(timeout=scan_timeout)
        devices_info = [
            (getattr(d, "name", None), getattr(d, "address", ""), [])
            for d in devices
        ]
        matched = None

    except Exception as exc:
        logger.error(f"BLE scan hiba ({scan_context}): {exc}")
        return None, []

    matched_addr: Optional[str] = getattr(matched, "address", None) if matched else None
    _print_ble_devices(devices_info, scan_context, matched_addr)
    _log_ble_devices_to_file(devices_info, scan_context)

    return matched, devices_info


# ============================================================
# BLE VENTILÁTOR KIMENET VEZÉRLŐ
# ============================================================


class BLEFanOutputController:
    """BLE alapú ventilátor kimenet vezérlő (LEVEL:N parancsok küldése).

    Asyncio korrutin alapú implementáció. A parancsokat egy
    asyncio.Queue-n keresztül fogadja, és a BLE GATT karakterisztikára
    írja ki az ESP32 vezérlőnek. PIN autentikáció is támogatott.

    Attribútumok:
        device_name: A keresett BLE eszköz neve.
        is_connected: True, ha a BLE kapcsolat aktív.
        last_sent: Az utoljára sikeresen elküldött zóna szint.
    """

    RETRY_RESET_SECONDS = 30
    DISCONNECT_TIMEOUT = 5.0

    def __init__(self, settings: Dict[str, Any]) -> None:
        ble = settings["ble"]
        self.device_name: Optional[str] = ble["device_name"]
        self.scan_timeout: int = ble["scan_timeout"]
        self.connection_timeout: int = ble["connection_timeout"]
        self.reconnect_interval: int = ble["reconnect_interval"]
        self.max_retries: int = ble["max_retries"]
        self.command_timeout: int = ble["command_timeout"]
        self.service_uuid: str = ble["service_uuid"]
        self.characteristic_uuid: str = ble["characteristic_uuid"]
        self.pin_code: Optional[str] = ble.get("pin_code")

        self.is_connected: bool = False
        self.last_sent: Optional[int] = None
        self._client: Optional[Any] = None
        self._device_address: Optional[str] = None
        self._retry_count: int = 0
        self._retry_reset_time: Optional[float] = None
        self._auth_failed: bool = False
        self.last_sent_time: float = 0.0
        # Utolsó reconnect kísérlet ideje – non-blocking reconnect logikához
        self._last_reconnect_attempt: float = 0.0

    def __repr__(self) -> str:
        return (
            f"BLEFanOutputController(device={self.device_name!r}, "
            f"connected={self.is_connected}, last_sent={self.last_sent}, "
            f"retries={self._retry_count}/{self.max_retries})"
        )

    async def run(self, zone_queue: asyncio.Queue[int]) -> None:
        """A BLE fan kimenet fő korrutinja – olvassa a zone_queue-t és küldi a parancsokat.

        Indításkor megpróbál csatlakozni a BLE eszközhöz, majd folyamatosan
        olvassa a zone_queue-t és elküldi a zóna parancsokat.

        Args:
            zone_queue: asyncio.Queue, amelyből a zóna parancsokat olvassa.
        """
        if not _BLEAK_AVAILABLE:
            msg = "BLE Fan: bleak könyvtár nem elérhető – BLE kimenet letiltva!"
            logger.error(msg)
            return

        logger.info("BLE Fan Output korrutin elindítva")
        await self._initial_connect()

        while True:
            zone = await zone_queue.get()
            await self._send_zone(zone)

    async def _initial_connect(self) -> None:
        """Kezdeti BLE csatlakozás indításkor (hiba esetén folytatja)."""
        ok = await self._scan_and_connect()
        if not ok:
            logger.warning(
                "BLE Fan: kezdeti csatlakozás sikertelen, automatikus újrapróbálkozás parancs küldéskor."
            )

    async def _scan_and_connect(self) -> bool:
        """BLE eszköz keresése és csatlakozás.

        Ha device_name üres vagy None, automatikus felderítés indul:
        a service_uuid alapján keres megfelelő eszközt, az összes talált
        eszközt konzolra és ble_devices.log-ba írja.

        Returns:
            True, ha a csatlakozás sikeres.
        """
        if not _BLEAK_AVAILABLE:
            return False

        # --- Automatikus felderítés (nincs device_name beállítva) ---
        if not self.device_name:
            try:
                matched, _ = await _scan_ble_with_autodiscovery(
                    self.scan_timeout, self.service_uuid, "BLE Fan (auto)"
                )
                if matched is not None:
                    self._device_address = matched.address
                    print(
                        f"✓ BLE Fan auto-csatlakozás: "
                        f"{matched.name or '(névtelen)'} ({matched.address})"
                    )
                    logger.info(
                        f"BLE Fan auto-felderítés: {matched.name} ({matched.address})"
                    )
                    return await self._connect()
                print(
                    f"⚠ BLE Fan: nem található eszköz a(z) {self.service_uuid} "
                    f"service UUID-val – újrapróbálkozás..."
                )
                return False
            except Exception as exc:
                logger.error(f"BLE Fan auto-felderítés hiba: {exc}")
                return False

        # --- Név alapú keresés (device_name beállítva) ---
        try:
            devices = await BleakScanner.discover(timeout=self.scan_timeout)
            for d in devices:
                if d.name == self.device_name:
                    self._device_address = d.address
                    logger.info(f"BLE Fan eszköz megtalálva: {d.name} ({d.address})")
                    return await self._connect()
                if d.name is None:
                    logger.debug(f"BLE eszköz név nélkül: {d.address}")

            logger.error(f"BLE Fan eszköz nem található: {self.device_name}")
            return False

        except Exception as exc:
            logger.error(f"BLE Fan keresési hiba: {exc}")
            return False

    async def _connect(self) -> bool:
        """Csatlakozás a korábban megtalált BLE eszközhöz.

        Returns:
            True, ha a csatlakozás sikeres.
        """
        if not _BLEAK_AVAILABLE:
            return False
        if not self._device_address:
            return False

        try:
            client = self._client
            if client and client.is_connected:
                return True

            client = BleakClient(
                self._device_address,
                timeout=self.connection_timeout,
                disconnected_callback=self._on_disconnect,
            )
            self._client = client

            await client.connect()

            if self.pin_code is not None:
                ok = await self._authenticate()
                if not ok:
                    return False

            self.is_connected = True
            self._retry_count = 0
            self._retry_reset_time = None
            self.last_sent = None
            logger.info(f"BLE Fan csatlakozva: {self._device_address}")
            return True

        except Exception as exc:
            logger.error(f"BLE Fan csatlakozási hiba: {exc}")
            self.is_connected = False
            self._client = None
            return False

    async def _authenticate(self) -> bool:
        """Alkalmazás szintű BLE PIN autentikáció.

        Returns:
            True, ha az autentikáció sikeres (vagy timeout esetén is folytatja).
        """
        client = self._client
        if client is None:
            logger.error("BLE AUTH hiba: nincs aktív BLE kliens")
            return False

        try:
            auth_event = asyncio.Event()
            auth_result: list = [""]

            def _notify_cb(sender: Any, data: bytes) -> None:
                auth_result[0] = data.decode("utf-8", errors="replace").strip()
                auth_event.set()

            await client.start_notify(self.characteristic_uuid, _notify_cb)
            try:
                try:
                    await asyncio.wait_for(
                        client.write_gatt_char(
                            self.characteristic_uuid,
                            f"AUTH:{self.pin_code}".encode("utf-8"),
                        ),
                        timeout=self.command_timeout,
                    )
                except asyncio.TimeoutError:
                    logger.error("BLE AUTH write timeout")
                    return False

                try:
                    await asyncio.wait_for(
                        auth_event.wait(),
                        timeout=self.command_timeout,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "BLE AUTH válasz timeout - folytatás autentikáció nélkül"
                    )
                    return True

                resp: str = auth_result[0]
                if not resp:
                    logger.error("BLE AUTH: üres válasz")
                    return False
                if resp == "AUTH_OK":
                    logger.info("BLE AUTH sikeres")
                    return True
                if resp in ("AUTH_FAIL", "AUTH_LOCKED"):
                    logger.error(
                        f"BLE AUTH sikertelen: {resp} - ellenorizd a pin_code erteket!"
                    )
                    print(f"✗ BLE PIN hiba ({resp}): helytelen pin_code! Javítsd a settings.json-ban.")
                    self._auth_failed = True
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                    return False

                logger.warning(f"BLE AUTH ismeretlen válasz: {resp} - folytatás")
                return True

            finally:
                try:
                    await client.stop_notify(self.characteristic_uuid)
                except Exception:
                    pass

        except Exception as exc:
            logger.error(f"BLE AUTH hiba: {exc}")
            return False

    def _on_disconnect(self, client: Any) -> None:
        """Callback: BLE kapcsolat váratlan megszakadásakor.

        Bleak nem garantálja, hogy az asyncio event loop szálán hívja ezt,
        ezért loop.call_soon_threadsafe()-fel delegáljuk az állapotmódosítást.
        """
        try:
            loop = asyncio.get_event_loop()
            loop.call_soon_threadsafe(self._handle_disconnect)
        except RuntimeError:
            # Ha nincs elérhető loop, közvetlen hívás (fallback)
            self._handle_disconnect()

    def _handle_disconnect(self) -> None:
        """Disconnect állapotmódosítás – az asyncio event loop-on hívandó."""
        logger.warning("BLE Fan kapcsolat megszakadt")
        self.is_connected = False
        self.last_sent = None
        # NEM nullázzuk self._client-et itt – az asyncio oldalon kezeljük

    async def _send_zone(self, zone: int) -> None:
        """Zóna parancs küldése BLE-n, szükség esetén újracsatlakozással.

        A reconnect non-blocking: ha az utolsó kísérlet óta még nem telt el
        reconnect_interval másodperc, a parancsot kihagyja (nem blokkolja
        a zone_queue olvasását).

        Args:
            zone: Ventilátor zóna szintje (0–3).
        """
        if self._auth_failed:
            logger.error(
                "BLE Fan: AUTH hiba, parancs elutasítva! Javítsd a pin_code-ot."
            )
            return

        if self.last_sent == zone and self.is_connected:
            return

        if not self.is_connected:
            now = time.monotonic()
            # Csak akkor próbálunk újra, ha elég idő telt el az utolsó kísérlet óta
            if now - self._last_reconnect_attempt < self.reconnect_interval:
                return
            self._last_reconnect_attempt = now
            ok = await self._reconnect_once()
            if not ok:
                return

        await self._write_level(zone)

    async def _reconnect_once(self) -> bool:
        """Egyetlen újracsatlakozási kísérlet, sleep nélkül.

        A sleep-mentes implementáció biztosítja, hogy a zone_queue olvasása
        ne blokkolódjon hosszú reconnect várakozás miatt.

        Returns:
            True, ha az újracsatlakozás sikeres.
        """
        now = time.monotonic()

        if self._retry_reset_time is not None:
            elapsed = now - self._retry_reset_time
            if elapsed >= self.RETRY_RESET_SECONDS:
                self._retry_count = 0
                self._retry_reset_time = None
            else:
                return False

        if self._retry_count >= self.max_retries:
            if self._retry_reset_time is None:
                self._retry_reset_time = now
                logger.warning(
                    f"BLE Fan: max újracsatlakozás elérve ({self.max_retries})! "
                    f"{self.RETRY_RESET_SECONDS}s múlva újrapróbálkozik..."
                )
            return False

        self._retry_count += 1
        logger.info(
            f"BLE Fan újracsatlakozás... ({self._retry_count}/{self.max_retries})"
        )

        if self._device_address:
            return await self._connect()
        return await self._scan_and_connect()

    async def _write_level(self, zone: int) -> None:
        """LEVEL:N parancs írása a BLE GATT karakterisztikára.

        Args:
            zone: Ventilátor zóna szintje (0–3).
        """
        client = self._client
        if client is None or not client.is_connected:
            self.is_connected = False
            self._client = None
            return

        try:
            msg = f"LEVEL:{zone}"
            await asyncio.wait_for(
                client.write_gatt_char(
                    self.characteristic_uuid,
                    msg.encode("utf-8"),
                ),
                timeout=self.command_timeout,
            )
            self.last_sent = zone
            self.last_sent_time = time.monotonic()
            logger.info(f"BLE Fan parancs elküldve: {msg}")

        except asyncio.TimeoutError:
            logger.error(f"BLE Fan parancs küldés timeout ({self.command_timeout}s)")
            self.is_connected = False
            self._client = None

        except Exception as exc:
            logger.error(f"BLE Fan küldési hiba: {exc}")
            self.is_connected = False
            self._client = None

    async def disconnect(self) -> None:
        """Bontja a BLE kapcsolatot és felszabadítja a klienst."""
        client = self._client
        if client is not None:
            try:
                await asyncio.wait_for(
                    client.disconnect(),
                    timeout=self.DISCONNECT_TIMEOUT,
                )
            except Exception:
                pass
            finally:
                self.is_connected = False
                self._client = None


# ============================================================
# ANT+ BEMENŐ ADATKEZELÉS
# ============================================================


_ANT_LOG_FILE = "ant_devices.log"


def _log_ant_device_to_file(
    device_type: str,
    device_id: int,
    device_info: str,
) -> None:
    """Talált ANT+ eszközt ír az ant_devices.log fájlba (append módban).

    Csak akkor ír, ha az eszköz (device_type + device_id) még nem szerepel
    a fájlban. Ha a fájl nem létezik, létrehozza.

    Args:
        device_type: Az eszköz típusa (pl. "PowerMeter", "HeartRate").
        device_id: Az ANT+ device number.
        device_info: Egyéb információ az eszközről.
    """
    # Egyedi kulcs: "típus | device_id"
    entry_key = f"{device_type} | {device_id}"

    # Meglévő bejegyzések ellenőrzése
    existing_entries: set[str] = set()
    try:
        with open(_ANT_LOG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                # Sorok formátuma: "  TÍPUS | DEVICE_ID | info"
                parts = line.split("|")
                if len(parts) >= 2:
                    existing_entries.add(f"{parts[0].strip()} | {parts[1].strip()}")
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning(f"Nem sikerült olvasni a {_ANT_LOG_FILE} fájlt: {exc}")

    if entry_key in existing_entries:
        return

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(_ANT_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(
                f"  {device_type:20s} | {device_id} | {device_info} "
                f"| @ {timestamp}\n"
            )
    except OSError as exc:
        logger.warning(f"Nem sikerült írni a {_ANT_LOG_FILE} fájlba: {exc}")


class ANTPlusInputHandler:
    """ANT+ power és HR adatforrás kezelője saját daemon szálban.

    Az openant könyvtár blokkoló API-t használ, ezért saját daemon szálban fut.
    Az érkező adatokat az asyncio event loop-ba hídalkotja
    (asyncio.run_coroutine_threadsafe) és az asyncio queue-kba teszi.

    Ha a settings-ben ant_power_device_id / ant_hr_device_id meg van adva
    (és nem 0), specifikus eszközhöz csatlakozik. Ha 0, az első elérhető
    (wildcard) eszközt használja.

    Attribútumok:
        power_queue: asyncio.Queue a power adatokhoz.
        hr_queue: asyncio.Queue a HR adatokhoz.
        loop: A fő asyncio event loop referenciája.
    """

    MAX_RETRY_COOLDOWN = 30
    WATCHDOG_TIMEOUT = 30  # Ha ennyi mp-ig nincs adat, a node-ot leállítjuk

    def __init__(
        self,
        settings: Dict[str, Any],
        power_queue: asyncio.Queue[float],
        hr_queue: asyncio.Queue[float],
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.settings = settings
        self.ds = settings["datasource"]
        self.hr_enabled = settings["power_zones"].get("heart_rate_zones", {}).get("enabled", False)
        self.power_queue = power_queue
        self.hr_queue = hr_queue
        self.loop = loop

        # ANT+ device ID-k (0 = wildcard / első elérhető)
        self._power_device_id: int = self.ds.get("ant_power_device_id", 0)
        self._hr_device_id: int = self.ds.get("ant_hr_device_id", 0)

        # Reconnect beállítások a settings-ből (a kettő közül a nagyobbat használja,
        # mert a power és HR egyetlen közös ANT+ szálban fut)
        self._reconnect_delay: int = max(
            self.ds.get("ant_power_reconnect_interval", 5),
            self.ds.get("ant_hr_reconnect_interval", 5),
        )
        self._max_retries: int = max(
            self.ds.get("ant_power_max_retries", 10),
            self.ds.get("ant_hr_max_retries", 10),
        )

        self._running = threading.Event()
        self._node: Optional[Any] = None
        self._devices: list = []
        self._lastdata: float = 0.0  # utolsó bármilyen adat ideje (thread loop használja)
        self._node_started: float = 0.0  # node.start() indulási ideje (watchdog-hoz)
        self.power_lastdata: float = 0.0
        self.hr_lastdata: float = 0.0
        self.power_connected: bool = False
        self.hr_connected: bool = False

    def start(self) -> threading.Thread:
        """Elindítja az ANT+ daemon szálat.

        Returns:
            A létrehozott daemon threading.Thread objektum.
        """
        self._running.set()
        t = threading.Thread(
            target=self._thread_loop, daemon=True, name="ANTPlus-Thread"
        )
        t.start()

        # Indulási log: milyen device ID-kkal indul
        power_src = self.ds.get("power_source", DataSource.ANTPLUS)
        hr_src = self.ds.get("hr_source", DataSource.ANTPLUS)
        if power_src == DataSource.ANTPLUS:
            pid = self._power_device_id
            mode = f"device_id={pid}" if pid else "wildcard (első elérhető)"
            logger.info(f"ANT+ Power szál elindítva – {mode}")
        if hr_src == DataSource.ANTPLUS and self.hr_enabled:
            hid = self._hr_device_id
            mode = f"device_id={hid}" if hid else "wildcard (első elérhető)"
            logger.info(f"ANT+ HR szál elindítva – {mode}")

        return t

    def stop(self) -> None:
        """Leállítja az ANT+ szálat és az ANT+ node-ot."""
        self._running.clear()
        self._stop_node()

    def _put_power(self, power: float) -> None:
        """Power értéket tesz az asyncio queue-ba (thread-safe)."""
        try:
            asyncio.run_coroutine_threadsafe(self.power_queue.put(power), self.loop)
        except RuntimeError:
            pass  # Loop már leállt – shutdown közben normális

    def _put_hr(self, hr: int) -> None:
        """HR értéket tesz az asyncio queue-ba (thread-safe)."""
        try:
            asyncio.run_coroutine_threadsafe(self.hr_queue.put(hr), self.loop)
        except RuntimeError:
            pass  # Loop már leállt – shutdown közben normális

    def _on_any_broadcast(self, data: Any) -> None:
        """Watchdog heartbeat: minden beérkező ANT+ broadcast frissíti az időbélyeget.

        Az openant on_update callbackje minden adatcsomagnál hívódik,
        függetlenül attól, hogy az event count változott-e (tehát akkor is,
        ha a power meter 0W-ot küld mert a felhasználó nem teker).
        Ez biztosítja, hogy a watchdog ne detektáljon false positive-ot.
        """
        self._lastdata = time.monotonic()

    def _on_data(self, page: Any, page_name: str, data: Any) -> None:
        """ANT+ adatcsomag callback – power és HR adatokat irányít a queue-kba.

        Csak akkor hívódik, ha ÚJ mérési adat érkezett (event count változott).
        A watchdog heartbeat-et az _on_any_broadcast kezeli külön.
        """
        if not _ANTPLUS_AVAILABLE:
            return
        now = time.monotonic()
        if isinstance(data, PowerData):
            self.power_lastdata = now
            self._put_power(data.instantaneous_power)
        elif isinstance(data, HeartRateData):
            self.hr_lastdata = now
            self._put_hr(data.heart_rate)

    def _make_on_found(
        self, sensor_label: str, device_type_str: str, device_ref: Any
    ) -> Any:
        """Létrehoz egy on_found callbacket az adott szenzorhoz.

        Az openant on_found() paraméter nélkül hívódik (staticmethod).
        A device_ref az openant device objektum referenciája, amelyen
        a device_id attribútum elérhető (wildcard esetén az openant
        automatikusan beállítja az első talált eszköz ID-jára).

        Args:
            sensor_label: Log prefix (pl. "ANT+ Power").
            device_type_str: Logfájl eszköz típus (pl. "PowerMeter").
            device_ref: Az openant device objektum referenciája.

        Returns:
            Paraméter nélküli callback függvény.
        """
        def _on_found() -> None:
            if "Power" in sensor_label:
                self.power_connected = True
            else:
                self.hr_connected = True
            dev_id = getattr(device_ref, "device_id", 0)
            dev_name = getattr(device_ref, "name", "")
            info = dev_name or sensor_label
            logger.info(f"{sensor_label} eszköz megtalálva: id={dev_id} ({info})")
            print(f"\u2713 {sensor_label} csatlakozva: id={dev_id} ({info})")
            _log_ant_device_to_file(device_type_str, dev_id, info)
        return _on_found

    def _init_node(self) -> None:
        """Inicializálja az ANT+ node-ot és regisztrálja az eszközöket.

        Ha ant_power_device_id / ant_hr_device_id meg van adva (nem 0),
        specifikus eszközhöz csatlakozik. Ha 0, wildcard mód (első elérhető).
        """
        if not _ANTPLUS_AVAILABLE:
            raise RuntimeError("openant könyvtár nem elérhető")
        node = Node()
        assert node is not None  # Pylance: Node() mindig valid objektumot ad
        node.set_network_key(0x00, ANTPLUS_NETWORK_KEY)
        self._node = node
        self._devices = []

        if self.ds.get("power_source", DataSource.ANTPLUS) == DataSource.ANTPLUS:
            pid = self._power_device_id
            meter = PowerMeter(self._node, device_id=pid)
            meter.on_found = self._make_on_found("ANT+ Power", "PowerMeter", meter)
            meter.on_device_data = self._on_data
            meter.on_update = self._on_any_broadcast
            self._devices.append(meter)

        if self.ds.get("hr_source", DataSource.ANTPLUS) == DataSource.ANTPLUS and self.hr_enabled:
            hid = self._hr_device_id
            hr_monitor = HeartRate(self._node, device_id=hid)
            hr_monitor.on_found = self._make_on_found("ANT+ HR", "HeartRate", hr_monitor)
            hr_monitor.on_device_data = self._on_data
            hr_monitor.on_update = self._on_any_broadcast
            self._devices.append(hr_monitor)

    def _stop_node(self) -> None:
        """Leállítja és felszabadítja az ANT+ node-ot.

        A connected flag-eket visszaállítja False-ra, mert az openant
        on_lost callbackje nem létezik.
        """
        self.power_connected = False
        self.hr_connected = False
        try:
            for d in self._devices:
                try:
                    d.close_channel()
                except Exception:
                    pass
            if self._node:
                self._node.stop()
                self._node = None
            self._devices = []
        except Exception:
            pass

    def _watchdog(self) -> None:
        """Watchdog szál: ha az ANT+ node fut, de sokáig nem jön adat, leállítja.

        Az openant Node.start() blokkoló hívás, és USB megszakadás esetén
        NEM tér vissza (a belső _main loop üres queue-ból olvas örökké).
        Ez a watchdog detektálja a helyzetet és kívülről hívja a node.stop()-ot,
        ami lehetővé teszi a _thread_loop retry logikájának lefutását.
        """
        while self._running.is_set():
            # 5 mp-enként ellenőrzi
            self._running.wait(timeout=5)
            if not self._running.is_set():
                break
            node = self._node
            if node is None:
                continue

            now = time.monotonic()
            started = self._node_started
            last = self._lastdata

            # Ha a node fut és volt már sikeres adat, de azóta WATCHDOG_TIMEOUT
            # ideje nem jött semmi → valószínűleg USB megszakadás
            if last > 0 and (now - last) > self.WATCHDOG_TIMEOUT:
                logger.warning(
                    f"ANT+ watchdog: {self.WATCHDOG_TIMEOUT}s óta nincs adat, "
                    f"node leállítása..."
                )
                try:
                    node.stop()
                except Exception:
                    pass
            # Ha a node elindul, de WATCHDOG_TIMEOUT * 2 ideje nem jött semmi adat
            # (pl. rossz device_id, vagy az eszköz soha nem volt hatótávolságban)
            elif last == 0.0 and started > 0 and (now - started) > self.WATCHDOG_TIMEOUT * 2:
                logger.warning(
                    f"ANT+ watchdog: {self.WATCHDOG_TIMEOUT * 2}s óta nem érkezett "
                    f"adat az indítás óta, node leállítása..."
                )
                try:
                    node.stop()
                except Exception:
                    pass

    def _thread_loop(self) -> None:
        """Az ANT+ szál fő ciklusa – újracsatlakozási logikával.

        Egy watchdog szálat is indít, ami figyeli, hogy jön-e adat. Ha az
        USB ANT+ stick megszakad, az openant Node.start() nem tér vissza
        magától – a watchdog kívülről hívja a node.stop()-ot, ami feloldja
        a blokkolást.
        """
        # Watchdog szál indítása
        watchdog = threading.Thread(
            target=self._watchdog, daemon=True, name="ANTPlus-Watchdog"
        )
        watchdog.start()

        retry_count = 0
        while self._running.is_set():
            try:
                self._init_node()
                self._lastdata = 0.0
                self._node_started = time.monotonic()
                if self._node is None:
                    raise RuntimeError("Node inicializálás sikertelen")
                self._node.start()  # Blokkoló hívás – itt vár, amíg az ANT+ node fut

                if not self._running.is_set():
                    break

                # Ha volt sikeres adat, reseteljük a számolót
                if self._lastdata > 0:
                    retry_count = 0
                    logger.info("ANT+ node normálisan leállt, újraindítás...")
                else:
                    retry_count += 1
                    logger.warning(
                        f"ANT+ node leállt adat nélkül, újraindítás... "
                        f"({retry_count}/{self._max_retries})"
                    )

            except Exception as exc:
                if not self._running.is_set():
                    break
                retry_count += 1
                logger.warning(f"ANT+ hiba ({retry_count}/{self._max_retries}): {exc}")

            if not self._running.is_set():
                break

            if retry_count >= self._max_retries:
                logger.warning(
                    f"ANT+ max próbálkozások ({self._max_retries}), "
                    f"{self.MAX_RETRY_COOLDOWN}s várakozás..."
                )
                time.sleep(self.MAX_RETRY_COOLDOWN)
                if not self._running.is_set():
                    break
                retry_count = 0

            self._stop_node()
            self._node_started = 0.0
            time.sleep(self._reconnect_delay)

        self._stop_node()
        logger.info("ANT+ szál leállítva")


# ============================================================
# BLE SZENZOR KÖZÖS ŐSOSZTÁLY (DRY)
# ============================================================


class _BLESensorInputHandler(abc.ABC):
    """Közös ősosztály BLE szenzor handlerekhez (Power, HR).

    Asyncio korrutin alapú implementáció. A scan, csatlakozás, notification
    subscribe és retry/reconnect logika itt van, az alosztályok csak a
    szenzor-specifikus konstansokat és az adat-parse-olást definiálják.

    Alosztályoknak felül kell írniuk:
        SERVICE_UUID: A BLE service UUID string.
        MEASUREMENT_UUID: A BLE measurement characteristic UUID string.
        _sensor_label: Rövid név logokhoz (pl. "BLE Power").
        _settings_prefix: Settings kulcs prefix (pl. "ble_power").
        _parse_notification(data): Nyers bájt → szám konverzió.

    Attribútumok:
        device_name: A keresett BLE eszköz neve (None = auto-discovery).
        is_connected: True, ha a BLE kapcsolat aktív.
        lastdata: Utolsó sikeres adat időbélyege (time.monotonic).
    """

    SERVICE_UUID: str
    MEASUREMENT_UUID: str
    _sensor_label: str
    _settings_prefix: str
    RETRY_RESET_SECONDS = 30

    def __init__(
        self, settings: Dict[str, Any], queue: asyncio.Queue[float]
    ) -> None:
        ds = settings["datasource"]
        pfx = self._settings_prefix
        self.device_name: Optional[str] = ds.get(f"{pfx}_device_name")
        self.scan_timeout: int = ds.get(f"{pfx}_scan_timeout", 10)
        self.reconnect_interval: int = ds.get(f"{pfx}_reconnect_interval", 5)
        self.max_retries: int = ds.get(f"{pfx}_max_retries", 10)
        self._queue = queue
        self.is_connected = False
        self._retry_count = 0
        self.lastdata = 0.0

    @abc.abstractmethod
    def _parse_notification(self, data: bytes) -> Optional[float]:
        """Nyers BLE notification bájtokból kinyeri a mért értéket.

        Returns:
            A kinyert érték (float), vagy None ha az adat érvénytelen/túl rövid.
        """
        ...

    async def run(self) -> None:
        """A BLE szenzor fogadó fő korrutinja – újracsatlakozási logikával.

        Ha nincs device_name, automatikusan keres a SERVICE_UUID alapján
        hirdető eszközt, és folyamatosan próbálkozik, amíg talál egyet.
        """
        label = self._sensor_label
        if not _BLEAK_AVAILABLE:
            logger.error(f"{label}: bleak könyvtár nem elérhető!")
            return

        if self.device_name:
            logger.info(f"{label} fogadó elindítva: {self.device_name}")
        else:
            print(f"\U0001f4e1 {label}: nincs eszköznév megadva, automatikus felderítés...")
            logger.info(f"{label} fogadó elindítva (auto-discovery mód)")

        while True:
            try:
                await self._scan_and_subscribe()
                # _scan_and_subscribe normálisan tért vissza (pl. a BLE eszköz
                # lekapcsolódott de a connect/subscribe sikeres volt).
                # Rövid várakozás az újracsatlakozás előtt, hogy ne legyen
                # gyors végtelen loop ha az eszköz ismételten megszakad.
                self._retry_count = 0
                self.is_connected = False
                logger.info(
                    f"{label} kapcsolat megszakadt, újracsatlakozás "
                    f"{self.reconnect_interval}s múlva..."
                )
                await asyncio.sleep(self.reconnect_interval)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._retry_count += 1
                self.is_connected = False
                logger.warning(
                    f"{label} kapcsolat hiba "
                    f"({self._retry_count}/{self.max_retries}): {exc}"
                )
                if self._retry_count >= self.max_retries:
                    logger.warning(
                        f"{label}: max újracsatlakozás elérve, "
                        f"{self.RETRY_RESET_SECONDS}s várakozás..."
                    )
                    await asyncio.sleep(self.RETRY_RESET_SECONDS)
                    self._retry_count = 0
                else:
                    await asyncio.sleep(self.reconnect_interval)

    async def _scan_and_subscribe(self) -> None:
        """BLE eszköz keresése, csatlakozás, notification feliratkozás.

        Ha device_name megadva: név alapján keres.
        Ha device_name üres: auto-discovery a SERVICE_UUID alapján.
        """
        if not _BLEAK_AVAILABLE:
            return

        label = self._sensor_label
        addr = None

        if self.device_name:
            # --- Név alapú keresés ---
            logger.info(f"{label} keresés: {self.device_name}...")
            devices = await BleakScanner.discover(timeout=self.scan_timeout)
            for d in devices:
                if d.name == self.device_name:
                    addr = d.address
                    logger.info(f"{label} eszköz megtalálva: {d.name} ({d.address})")
                    break
                if d.name is None:
                    logger.debug(f"BLE eszköz név nélkül: {d.address}")
            if not addr:
                raise Exception(f"{label} eszköz nem található: {self.device_name}")
        else:
            # --- Automatikus felderítés service UUID alapján ---
            matched, _ = await _scan_ble_with_autodiscovery(
                self.scan_timeout,
                self.SERVICE_UUID,
                f"{label} (auto)",
            )
            if matched is None:
                raise Exception(
                    f"{label}: nem található szolgáltatás eszköz – "
                    "újrapróbálkozás..."
                )
            addr = matched.address
            print(
                f"\u2713 {label} auto-csatlakozás: "
                f"{matched.name or '(névtelen)'} ({matched.address})"
            )

        async with BleakClient(addr) as client:
            self.is_connected = True
            self._retry_count = 0
            logger.info(f"{label} csatlakozva: {addr}")

            def _handler(sender: Any, data: bytes) -> None:
                try:
                    value = self._parse_notification(data)
                    if value is None:
                        return
                    self.lastdata = time.monotonic()
                    try:
                        self._queue.put_nowait(value)
                    except asyncio.QueueFull:
                        logger.debug(f"{label} queue teli, adat elvetve")
                except Exception as exc:
                    logger.warning(f"{label} notification hiba: {exc}")

            await client.start_notify(self.MEASUREMENT_UUID, _handler)
            while client.is_connected:
                await asyncio.sleep(1)
            # stop_notify felesleges bontott kapcsolaton – a context manager kezeli

        self.is_connected = False


# ============================================================
# BLE POWER BEMENŐ ADATKEZELÉS
# ============================================================


class BLEPowerInputHandler(_BLESensorInputHandler):
    """BLE Cycling Power Service (UUID: 0x1818) fogadó.

    Parse: flags (2 bájt LE) → instantaneous power (2 bájt LE, signed int16).
    """

    SERVICE_UUID = "00001818-0000-1000-8000-00805f9b34fb"
    MEASUREMENT_UUID = "00002a63-0000-1000-8000-00805f9b34fb"
    _sensor_label = "BLE Power"
    _settings_prefix = "ble_power"

    @property
    def power_lastdata(self) -> float:
        """Visszafelé kompatibilis alias a lastdata attribútumhoz."""
        return self.lastdata

    @power_lastdata.setter
    def power_lastdata(self, value: float) -> None:
        self.lastdata = value

    def _parse_notification(self, data: bytes) -> Optional[float]:
        if len(data) < 4:
            return None
        return float(int.from_bytes(data[2:4], byteorder="little", signed=True))


# ============================================================
# BLE HR BEMENŐ ADATKEZELÉS
# ============================================================


class BLEHRInputHandler(_BLESensorInputHandler):
    """BLE Heart Rate Service (UUID: 0x180D) fogadó.

    Parse: flags byte bit 0 → 0 = 8-bites HR, 1 = 16-bites HR.
    """

    SERVICE_UUID = "0000180d-0000-1000-8000-00805f9b34fb"
    MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"
    _sensor_label = "BLE HR"
    _settings_prefix = "ble_hr"

    @property
    def hr_lastdata(self) -> float:
        """Visszafelé kompatibilis alias a lastdata attribútumhoz."""
        return self.lastdata

    @hr_lastdata.setter
    def hr_lastdata(self, value: float) -> None:
        self.lastdata = value

    def _parse_notification(self, data: bytes) -> Optional[float]:
        if len(data) < 2:
            return None
        flags = data[0]
        # bit 0: 0 = 8-bites HR, 1 = 16-bites HR
        if flags & 0x01:
            if len(data) < 3:
                return None
            return float(int.from_bytes(data[1:3], byteorder="little"))
        return float(data[1])


# ============================================================
# ZWIFT UDP BEMENŐ ADATKEZELÉS
# ============================================================


class ZwiftUDPInputHandler:
    """Zwift UDP adatforrás fogadó – asyncio DatagramProtocol alapú.

    A zwift-udp-monitor programból érkező JSON csomagokat fogadja UDP-n.
    Asyncio DatagramProtocol alapú implementáció, teljesen non-blocking.
    Érvényes power és HR értékeket az asyncio queue-kba teszi.

    JSON formátum:
        {"power": int, "heartrate": int}

    Attribútumok:
        process_power: True, ha a power adatokat kell feldolgozni.
        process_hr: True, ha a HR adatokat kell feldolgozni.
        last_packet_time: utolsó érvényes ZwiftUDP csomag ideje (monotonic).
    """

    def __init__(
        self,
        settings: Dict[str, Any],
        power_queue: asyncio.Queue[float],
        hr_queue: asyncio.Queue[float],
    ) -> None:
        ds = settings["datasource"]
        self.settings = settings
        self.host: str = ds.get("zwift_udp_host", "127.0.0.1")
        self.port: int = ds.get("zwift_udp_port", 7878)
        self.power_queue = power_queue
        self.hr_queue = hr_queue

        self.process_power: bool = ds.get("power_source") == DataSource.ZWIFTUDP
        hr_enabled = settings["power_zones"].get("heart_rate_zones", {}).get("enabled", False)
        self.process_hr: bool = ds.get("hr_source") == DataSource.ZWIFTUDP and hr_enabled

        self._transport: Any = None

        # HUD számára: utolsó érvényes csomag ideje
        self.last_packet_time: float = 0.0

    async def run(self) -> None:
        """A Zwift UDP fogadó fő korrutinja – asyncio DatagramProtocol-t indít."""
        loop = asyncio.get_running_loop()
        logger.info(f"Zwift UDP fogadó elindítva: {self.host}:{self.port}")

        handler = self

        class _Protocol(asyncio.DatagramProtocol):
            def connection_made(self, transport: Any) -> None:
                logger.info(f"Zwift UDP socket kötve: {handler.host}:{handler.port}")
                handler._transport = transport

            def datagram_received(self, data: bytes, addr: Any) -> None:
                handler._process_packet(data)

            def error_received(self, exc: Exception) -> None:
                logger.warning(f"Zwift UDP hiba: {exc}")

            def connection_lost(self, exc: Optional[Exception]) -> None:
                logger.info("Zwift UDP kapcsolat lezárva")

        try:
            transport, _ = await loop.create_datagram_endpoint(
                _Protocol,
                local_addr=(self.host, self.port),
            )
            try:
                while True:
                    await asyncio.sleep(3600)
            finally:
                transport.close()
        except asyncio.CancelledError:
            raise
        except OSError as exc:
            logger.error(f"Zwift UDP bind hiba: {exc}")

    def _process_packet(self, raw: bytes) -> None:
        """JSON csomag feldolgozása – validáció és queue-ba helyezés.

        A power validációhoz a settings-ből olvassa a max_watt értéket,
        így konzisztens marad a power_processor_task szűrőjével.
        """
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        if not isinstance(data, dict):
            return

        valid_any = False

        if self.process_power and "power" in data:
            p = data["power"]
            min_watt = self.settings["power_zones"]["min_watt"]
            max_watt = self.settings["power_zones"]["max_watt"]
            if is_valid_power(p, min_watt, max_watt):
                try:
                    self.power_queue.put_nowait(round(p))
                    valid_any = True
                except asyncio.QueueFull:
                    logger.debug("Zwift UDP: power queue teli, adat elvetve")
            else:
                logger.debug(f"Zwift UDP: érvénytelen power: {p}")

        if self.process_hr and "heartrate" in data:
            hrz = self.settings["power_zones"].get("heart_rate_zones", {})
            valid_min_hr: int = hrz.get("valid_min_hr", 30)
            valid_max_hr: int = hrz.get("valid_max_hr", 220)

            h = data["heartrate"]
            if is_valid_hr(h, valid_min_hr, valid_max_hr):
                try:
                    self.hr_queue.put_nowait(round(h))
                    valid_any = True
                except asyncio.QueueFull:
                    logger.debug("Zwift UDP: hr queue teli, adat elvetve")
            else:
                logger.debug(f"Zwift UDP: érvénytelen heartrate: {h}")

        # Ha bármilyen érvényes adatot elfogadtunk, frissítjük az időbélyeget
        if valid_any:
            self.last_packet_time = time.monotonic()


# ============================================================
# POWER FELDOLGOZÓ KORRUTIN
# ============================================================


async def power_processor_task(
    raw_power_queue: asyncio.Queue[float],
    state: ControllerState,
    zone_event: asyncio.Event,
    power_averager: PowerAverager,
    printer: ConsolePrinter,
    settings: Dict[str, Any],
    power_zones: Dict[int, Tuple[int, int]],
) -> None:
    """Teljesítmény adatok feldolgozása – validálás, átlagolás, állapot frissítés.

    Olvassa a raw_power_queue-t, validálja a beérkező watt értékeket,
    gördülő átlagot számít, meghatározza a zónát, frissíti a megosztott
    állapotot, majd jelzi a zone_event-tel, hogy zóna újraszámítás szükséges.

    Args:
        raw_power_queue: Nyers power adatok asyncio.Queue-ja.
        state: A megosztott vezérlő állapot.
        zone_event: asyncio.Event – beállítja, ha új átlag áll rendelkezésre.
        power_averager: PowerAverager példány.
        printer: ConsolePrinter a konzol kiíráshoz.
        settings: Betöltött beállítások dict-je.
        power_zones: Kiszámított power zóna határok.
    """
    min_watt = settings["power_zones"]["min_watt"]
    max_watt = settings["power_zones"]["max_watt"]
    hr_enabled = settings["power_zones"].get("heart_rate_zones", {}).get("enabled", False)
    zone_mode = (
        settings["power_zones"]["heart_rate_zones"].get("zone_mode", ZoneMode.POWER_ONLY)
        if hr_enabled
        else ZoneMode.POWER_ONLY
    )

    logger.info("Power processor korrutin elindítva")

    while True:
        power = await raw_power_queue.get()

        if not is_valid_power(power, min_watt, max_watt):
            printer.emit("invalid_power", "⚠ FIGYELMEZTETÉS: Érvénytelen power adat!")
            continue

        power = int(power)
        now = time.monotonic()

        if zone_mode != "higher_wins":
            printer.emit("power_raw", f"⚡ Teljesítmény: {power} watt")

        avg_power = power_averager.add_sample(power)
        if avg_power is None:
            # Fix #39: Buffer feltöltés alatt is frissítjük a timestampet,
            # hogy a dropout checker ne jelezzen hamis kiesést
            async with state.lock:
                state.last_power_time = now
            continue

        avg_power = round(avg_power)
        new_power_zone = zone_for_power(avg_power, power_zones)

        if zone_mode == ZoneMode.HIGHER_WINS:
            printer.emit(
                "power_avg_hw",
                f"⚡ Átlag teljesítmény: {avg_power} watt | Power zóna: {new_power_zone} | Higher Wins!",
            )
        else:
            printer.emit(
                "power_avg",
                f"⚡ Átlag teljesítmény: {avg_power} watt | Power zóna: {new_power_zone}",
            )

        async with state.lock:
            state.last_power_time = now
            state.current_power_zone = new_power_zone
            state.current_avg_power = avg_power
            # Fix #1: UI snapshot frissítése lock alatt – konzisztens pillanatfelvétel
            state.ui_snapshot.update(
                state.current_zone,
                float(avg_power),
                state.current_avg_hr,
            )

        zone_event.set()  # Zone controller újraszámítást igényel


# ============================================================
# HR FELDOLGOZÓ KORRUTIN
# ============================================================


async def hr_processor_task(
    raw_hr_queue: asyncio.Queue[float],
    state: ControllerState,
    zone_event: asyncio.Event,
    hr_averager: HRAverager,
    printer: ConsolePrinter,
    settings: Dict[str, Any],
    hr_zones: Dict[str, int],
) -> None:
    """HR adatok feldolgozása – validálás, átlagolás, állapot frissítés.

    Olvassa a raw_hr_queue-t, validálja a bpm értékeket, gördülő átlagot
    számít, meghatározza a HR zónát, frissíti a megosztott állapotot, majd
    jelzi a zone_event-tel, hogy zóna újraszámítás szükséges.

    Frissíti a state.last_hr_time mezőt, amelyet a dropout checker
    hr_only és higher_wins módban figyelembe vesz.

    Args:
        raw_hr_queue: Nyers HR adatok asyncio.Queue-ja.
        state: A megosztott vezérlő állapot.
        zone_event: asyncio.Event – beállítja, ha új átlag áll rendelkezésre.
        hr_averager: HRAverager példány.
        printer: ConsolePrinter a konzol kiíráshoz.
        settings: Betöltött beállítások dict-je.
        hr_zones: Kiszámított HR zóna határok.
    """
    hrz = settings["power_zones"].get("heart_rate_zones", {})
    hr_enabled = settings["power_zones"].get("heart_rate_zones", {}).get("enabled", False)
    zone_mode = (
        settings["power_zones"]["heart_rate_zones"].get("zone_mode", ZoneMode.POWER_ONLY)
        if hr_enabled
        else ZoneMode.POWER_ONLY
    )
    valid_min_hr: int = hrz.get("valid_min_hr", 30)
    valid_max_hr: int = hrz.get("valid_max_hr", 220)

    logger.info("HR processor korrutin elindítva")

    while True:
        hr = await raw_hr_queue.get()

        try:
            hr = int(hr)
        except (TypeError, ValueError):
            continue
        if not is_valid_hr(hr, valid_min_hr, valid_max_hr):
            continue

        # Egyetlen now a ciklus elejéhez – konzisztens timestamp az egész iterációban
        now = time.monotonic()

        if not hr_enabled:
            printer.emit("hr_disabled", f"❤ Szívfrekvencia: {hr} bpm")
            async with state.lock:
                state.last_hr_time = now
            continue

        if zone_mode in (ZoneMode.HR_ONLY, ZoneMode.POWER_ONLY):
            printer.emit("hr_raw", f"❤ HR: {hr} bpm")

        avg_hr = hr_averager.add_sample(hr)

        if avg_hr is None:
            # Buffer feltöltés alatt is frissítjük a timestampet (dropout checker számára)
            async with state.lock:
                state.last_hr_time = now
            continue

        avg_hr = round(avg_hr)
        new_hr_zone = zone_for_hr(avg_hr, hr_zones)

        if zone_mode == ZoneMode.HR_ONLY:
            printer.emit(
                "hr_avg",
                f"❤ Átlag HR: {avg_hr} bpm | HR zóna: {new_hr_zone}",
            )
        elif zone_mode == ZoneMode.HIGHER_WINS:
            printer.emit(
                "hr_avg_hw",
                f"❤ Átlag HR: {avg_hr} bpm | HR zóna: {new_hr_zone} | Higher Wins!",
            )

        async with state.lock:
            state.last_hr_time = now
            state.current_hr_zone = new_hr_zone
            state.current_avg_hr = float(avg_hr)
            # Fix #1: UI snapshot frissítése lock alatt – konzisztens pillanatfelvétel
            state.ui_snapshot.update(
                state.current_zone,
                state.current_avg_power,
                float(avg_hr),
            )

        zone_event.set()  # Zone controller újraszámítást igényel


# ============================================================
# ZÓNA VEZÉRLŐ KORRUTIN
# ============================================================


async def zone_controller_task(
    state: ControllerState,
    zone_queue: asyncio.Queue[int],
    cooldown_ctrl: CooldownController,
    settings: Dict[str, Any],
    zone_event: asyncio.Event,
) -> None:
    """Zóna vezérlő – kombinálja a power és HR zónákat, alkalmazza a cooldownt.

    Megvárja a zone_event jelzést (amelyet a power és HR processorok állítanak be),
    majd a legfrissebb állapot alapján:
    1. Meghatározza a final zónát (apply_zone_mode / higher_wins)
    2. Alkalmazza a cooldown logikát (CooldownController)
    3. Ha szükséges, elküldi a zóna parancsot a BLE fan queue-ba

    Megjegyzés higher_wins módban: ha hr_zone None (az átlagoló még nem gyűjtött
    elég mintát), de hr_fresh True, az apply_zone_mode csak a power_zone-t
    használja – ez szándékos viselkedés.

    Args:
        state: A megosztott vezérlő állapot.
        zone_queue: BLE fan output asyncio.Queue-ja.
        cooldown_ctrl: CooldownController példány.
        settings: Betöltött beállítások dict-je.
        zone_event: asyncio.Event – jelzi, hogy új adat érkezett.
    """
    hr_enabled = settings["power_zones"].get("heart_rate_zones", {}).get("enabled", False)
    zone_mode = (
        settings["power_zones"]["heart_rate_zones"].get("zone_mode", ZoneMode.POWER_ONLY)
        if hr_enabled
        else ZoneMode.POWER_ONLY
    )
    zero_power_immediate = settings["power_zones"].get("zero_power_immediate", False)
    zero_hr_immediate = settings["power_zones"]["heart_rate_zones"].get("zero_hr_immediate", False)
    power_buf = _resolve_buffer_settings(settings, "power")
    hr_buf = _resolve_buffer_settings(settings, "hr")
    power_dropout_timeout = power_buf["dropout_timeout"]
    hr_dropout_timeout = hr_buf["dropout_timeout"]

    logger.info("Zóna vezérlő korrutin elindítva")

    while True:
        await zone_event.wait()
        zone_event.clear()

        # Állapot pillanatfelvétel (lock alatt)
        async with state.lock:
            power_zone = state.current_power_zone
            hr_zone = state.current_hr_zone
            current_zone = state.current_zone
            now = time.monotonic()
            last_power = state.last_power_time
            last_hr = state.last_hr_time

        # Frissesség ellenőrzése (dropout figyelembe vételéhez)
        # Fix #3: last_power_time most Optional – None = még nem érkezett adat
        power_fresh = (
            last_power is not None
            and (now - last_power) < power_dropout_timeout
        )
        hr_fresh = last_hr is not None and (now - last_hr) < hr_dropout_timeout

        # Zóna kombinálás a zone_mode alapján
        if zone_mode == ZoneMode.POWER_ONLY:
            final_zone = power_zone if power_fresh else None
        elif zone_mode == ZoneMode.HR_ONLY:
            final_zone = hr_zone if hr_fresh else None
        else:  # higher_wins
            p = power_zone if power_fresh else None
            h = hr_zone if hr_fresh else None
            final_zone = apply_zone_mode(p, h, zone_mode)

        if final_zone is None:
            continue  # Nincs elég friss adat a döntéshez

        # Azonnali leállás flag (zero_power_immediate / zero_hr_immediate)
        use_zero_immediate = (
            (zero_power_immediate and power_zone is not None and power_zone == 0 and power_fresh)
            or (zero_hr_immediate and hr_zone is not None and hr_zone == 0 and hr_fresh)
        )

        # Cooldown logika alkalmazása
        zone_to_send = cooldown_ctrl.process(current_zone, final_zone, use_zero_immediate)

        if zone_to_send is not None:
            async with state.lock:
                state.current_zone = zone_to_send
                # Fix #1: UI snapshot frissítése lock alatt – konzisztens pillanatfelvétel
                state.ui_snapshot.update(
                    zone_to_send,
                    state.current_avg_power,
                    state.current_avg_hr,
                )
            await send_zone(zone_to_send, zone_queue)
            print(f"→ Zóna elküldve: LEVEL:{zone_to_send}")


# ============================================================
# DROPOUT ELLENŐRZŐ KORRUTIN
# ============================================================


async def dropout_checker_task(
    state: ControllerState,
    zonequeue: asyncio.Queue[int],
    settings: Dict[str, Any],
    poweraverager: PowerAverager,
    hraverager: HRAverager,
    power_dropout_timeout: float,
    hr_dropout_timeout: float,
    zone_mode: ZoneMode,
    cooldown_ctrl: CooldownController,
) -> None:
    """Adatforrás kiesés detektálása, Z0 küldése és pufferek ürítése.

    Args:
        state: A megosztott vezérlő állapot.
        zonequeue: BLE fan output asyncio.Queue-ja.
        settings: Betöltött beállítások dict-je.
        poweraverager: PowerAverager példány (ürítéshez).
        hraverager: HRAverager példány (ürítéshez).
        power_dropout_timeout: Power forrás timeout másodpercben.
        hr_dropout_timeout: HR forrás timeout másodpercben.
        zone_mode: Aktív zóna mód (paraméterként kapja, nem számolja újra).
        cooldown_ctrl: CooldownController példány (dropout-kor reseteléshez).
    """
    logger.info("Dropout checker korrutin elindítva")

    while True:
        await asyncio.sleep(1)
        now = time.monotonic()
        send_dropout = False

        # Fix #2: Egyetlen lock blokk az egész ellenőrzéshez
        async with state.lock:
            if state.current_zone is None or state.current_zone == 0:
                continue

            # Fix #3: last_power_time Optional – None = még nem érkezett adat
            power_fresh = (
                state.last_power_time is not None
                and (now - state.last_power_time) < power_dropout_timeout
            )
            hr_fresh = (
                state.last_hr_time is not None
                and (now - state.last_hr_time) < hr_dropout_timeout
            )

            if zone_mode == ZoneMode.POWER_ONLY:
                stale = not power_fresh
                elapsed = (
                    now - state.last_power_time
                    if state.last_power_time is not None
                    else float("inf")
                )
                label = "power"
            elif zone_mode == ZoneMode.HR_ONLY:
                elapsed = (
                    now - state.last_hr_time
                    if state.last_hr_time is not None
                    else float("inf")
                )
                # Fix #4: hr_only dropout akkor is triggerel, ha soha nem érkezett HR
                stale = not hr_fresh
                label = "HR"
            else:  # higher_wins
                stale = not power_fresh and not hr_fresh

                if stale:
                    elapsed = max(
                        (
                            now - state.last_power_time
                            if state.last_power_time is not None
                            else float("inf")
                        ),
                        (
                            now - state.last_hr_time
                            if state.last_hr_time is not None
                            else float("inf")
                        ),
                    )
                elif not power_fresh:
                    elapsed = (
                        now - state.last_power_time
                        if state.last_power_time is not None
                        else float("inf")
                    )
                elif not hr_fresh:
                    elapsed = (
                        now - state.last_hr_time
                        if state.last_hr_time is not None
                        else float("inf")
                    )
                else:
                    elapsed = 0.0
                label = "power+HR"

            if stale:
                print(f"Adatforrás kiesett ({label}), {elapsed:.1f}s → LEVEL:0")
                if not power_fresh:
                    poweraverager.clear()
                    state.current_avg_power = None
                    state.current_power_zone = None
                if not hr_fresh:
                    hraverager.clear()
                    state.current_avg_hr = None
                    state.current_hr_zone = None
                state.current_zone = 0
                # Fix #28: Cooldown állapot resetelése dropout-kor
                cooldown_ctrl.reset()
                # Fix #40: UI snapshot frissítése – a HUD is lássa a dropout-ot
                state.ui_snapshot.update(0, state.current_avg_power, state.current_avg_hr)
                send_dropout = True

        if send_dropout:
            await send_zone(0, zonequeue)


class BLECombinedSensor:
    def __init__(self, power_handler=None, hr_handler=None):
        self.power_handler = power_handler
        self.hr_handler = hr_handler

    @property
    def power_lastdata(self):
        if self.power_handler:
            return getattr(self.power_handler, "power_lastdata", 0)
        return 0

    @property
    def hr_lastdata(self):
        if self.hr_handler:
            return getattr(self.hr_handler, "hr_lastdata", 0)
        return 0


# ============================================================
# TASK WRAPPER – kivétel logoláshoz
# ============================================================


async def _guarded_task(
    coro: Any,
    name: str,
    *,
    max_retries: int = 0,
    retry_delay: float = 5.0,
    coro_factory: Any = None,
) -> None:
    """Task wrapper: elkapja és logolja a váratlan kivételeket.

    CancelledError-t tovább engedi (normál leálláshoz szükséges).
    Minden más kivételt kritikus szinten logolja, hogy ne tűnjön el csendben.

    Ha max_retries > 0 és coro_factory adott, a task automatikusan újraindul
    exponenciális backoff-fal (retry_delay * 2^attempt, max 60s).

    Args:
        coro: Az indítandó korrutin (első futáshoz).
        name: A task neve (logoláshoz).
        max_retries: Max újrapróbálkozások száma (0 = nincs retry).
        retry_delay: Kezdő várakozás másodpercben újraindítás előtt.
        coro_factory: Paraméter nélküli callable, ami új korrutint ad vissza.
            Retry-hoz kötelező, mert egy korrutin csak egyszer await-elhető.
    """
    attempt = 0
    current_coro = coro
    while True:
        try:
            await current_coro
            return  # Normál befejezés
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            attempt += 1
            if coro_factory is not None and attempt <= max_retries:
                delay = min(retry_delay * (2 ** (attempt - 1)), 60.0)
                logger.warning(
                    f"Task '{name}' hiba ({attempt}/{max_retries}): {exc} "
                    f"→ újraindítás {delay:.0f}s múlva",
                    exc_info=True,
                )
                await asyncio.sleep(delay)
                current_coro = coro_factory()
            else:
                logger.error(
                    f"Task '{name}' váratlanul leállt: {exc}", exc_info=True
                )
                return


# ============================================================
# FAN CONTROLLER – FŐ ÖSSZEHANGOLÁS
# ============================================================


class FanController:
    """A Smart Fan Controller fő orchestrátora.

    Összefogja az összes komponenst, elindítja az asyncio task-okat
    és a szálakat, és gondoskodik a tiszta leállításról.

    Indítási sorrend:
        1. Beállítások betöltése
        2. Zóna határok kiszámítása
        3. Átlagolók, cooldown, printer létrehozása
        4. BLE fan output asyncio task indítása
        5. BLE power/HR input asyncio task-ok indítása (ha szükséges)
        6. Zwift UDP input asyncio task indítása (ha szükséges)
        7. ANT+ szál indítása (ha szükséges)
        8. Power/HR processor asyncio task-ok indítása
        9. Zone controller asyncio task indítása
        10. Dropout checker asyncio task indítása
        11. Főciklus: Ctrl+C / SIGTERM megvárása
        12. Leállítás: minden task és szál leállítása
    """

    def __init__(self, settings_file: str = "settings.json") -> None:
        self.settings = load_settings(settings_file)
        self._antplus_handler: Optional[ANTPlusInputHandler] = None
        self._antplus_thread: Optional[threading.Thread] = None
        self._tasks: list = []
        self._running = True
        self._zwift_proc: Optional[subprocess.Popen] = None
        # Handler ref-ek (HUD és leállítás számára)
        self._ble_fan: Optional[BLEFanOutputController] = None
        self._ble_power: Optional[BLEPowerInputHandler] = None
        self._ble_hr: Optional[BLEHRInputHandler] = None
        self._zwift_udp: Optional[ZwiftUDPInputHandler] = None
        self._state: Optional[ControllerState] = None
        self._cooldown_ctrl: Optional[CooldownController] = None
        self._ble_sensor_handler: Optional[BLECombinedSensor] = None

    def __repr__(self) -> str:
        ds = self.settings.get("datasource", {})
        return (
            f"FanController(running={self._running}, "
            f"power_src={ds.get('power_source')}, "
            f"hr_src={ds.get('hr_source')}, "
            f"tasks={len(self._tasks)})"
        )

    def print_startup_info(self) -> None:
        """Kiírja az indítási konfigurációs összefoglalót."""
        s = self.settings
        ds = s["datasource"]
        hrz = s["power_zones"].get("heart_rate_zones", {})

        power_buf = _resolve_buffer_settings(s, "power")
        hr_buf = _resolve_buffer_settings(s, "hr")

        hr_enabled = hrz.get("enabled", False)
        zone_mode = hrz.get("zone_mode", ZoneMode.POWER_ONLY) if hr_enabled else ZoneMode.POWER_ONLY

        print("-" * 60)
        print(f"  Smart Fan Controller v{__version__}  |  Power+HR → BLE Fan")
        print("-" * 60)
        zt = s["power_zones"]
        print(f"FTP: {zt['ftp']}W | Érvényes tartomány: 0–{zt['max_watt']}W")

        power_zones = calculate_power_zones(
            zt["ftp"],
            zt["min_watt"],
            zt["max_watt"],
            zt["z1_max_percent"],
            zt["z2_max_percent"],
        )
        print(f"Zóna határok: {power_zones}")

        print(
            f"💪 Power buffer ({ds['power_source'].upper()}): "
            f"{power_buf['buffer_seconds']}s | "
            f"minta: {power_buf['minimum_samples']} | "
            f"rate: {power_buf['buffer_rate_hz']}Hz | "
            f"dropout: {power_buf['dropout_timeout']}s"
        )
        print(
            f"❤️  HR buffer    ({ds['hr_source'].upper()}): "
            f"{hr_buf['buffer_seconds']}s | "
            f"minta: {hr_buf['minimum_samples']} | "
            f"rate: {hr_buf['buffer_rate_hz']}Hz | "
            f"dropout: {hr_buf['dropout_timeout']}s"
        )

        print(
            f"Cooldown: {s['global_settings']['cooldown_seconds']}s  |  "
            f"0W azonnali: {'Igen' if s['power_zones'].get('zero_power_immediate', False) else 'Nem'}  |  "
            f"0HR azonnali: {'Igen' if s['power_zones']['heart_rate_zones'].get('zero_hr_immediate', False) else 'Nem'}"
        )
        ble_fan_name = s["ble"]["device_name"]
        if ble_fan_name:
            print(f"BLE Fan: {ble_fan_name}")
        else:
            print("BLE Fan: (auto-discovery – service UUID alapján)")
        if s["ble"].get("pin_code"):
            print(f"BLE PIN: {'*' * len(str(s['ble']['pin_code']))}")

        # BLE szenzor auto-discovery jelzés
        if ds.get("power_source") == DataSource.BLE and not ds.get("ble_power_device_name"):
            print("BLE Power: (auto-discovery – Cycling Power Service)")
        if ds.get("hr_source") == DataSource.BLE and not ds.get("ble_hr_device_name"):
            print("BLE HR: (auto-discovery – Heart Rate Service)")

        print(f"Zónamód: {zone_mode}")
        print("-" * 60)

    async def run(self) -> None:
        """A vezérlő fő asyncio korrutinja – elindít mindent és vár."""
        self._tasks = []
        s = self.settings
        ds = s["datasource"]
        hr_enabled = s["power_zones"].get("heart_rate_zones", {}).get("enabled", False)
        if hr_enabled:
            zone_mode = s["power_zones"]["heart_rate_zones"].get("zone_mode", ZoneMode.POWER_ONLY)
        else:
            zone_mode = ZoneMode.POWER_ONLY

        # --- Zóna határok kiszámítása ---
        power_zones = calculate_power_zones(
            s["power_zones"]["ftp"],
            s["power_zones"]["min_watt"],
            s["power_zones"]["max_watt"],
            s["power_zones"]["z1_max_percent"],
            s["power_zones"]["z2_max_percent"],
        )
        hr_zones = (
            calculate_hr_zones(
                s["power_zones"]["heart_rate_zones"]["max_hr"],
                s["power_zones"]["heart_rate_zones"]["resting_hr"],
                s["power_zones"]["heart_rate_zones"]["z1_max_percent"],
                s["power_zones"]["heart_rate_zones"]["z2_max_percent"],
            )
            if hr_enabled
            else {"resting": 60, "z1_max": 130, "z2_max": 148}
        )

        # --- Komponensek létrehozása ---
        raw_power_queue: asyncio.Queue[float] = asyncio.Queue(maxsize=100)
        raw_hr_queue: asyncio.Queue[float] = asyncio.Queue(maxsize=100)
        zone_cmd_queue: asyncio.Queue[int] = asyncio.Queue(maxsize=1)
        zone_event = asyncio.Event()

        state = ControllerState()
        self._state = state
        power_buf = _resolve_buffer_settings(s, "power")
        hr_buf = _resolve_buffer_settings(s, "hr")

        power_averager = PowerAverager(
            power_buf["buffer_seconds"],
            power_buf["minimum_samples"],
            power_buf["buffer_rate_hz"],
        )
        hr_averager = HRAverager(
            hr_buf["buffer_seconds"],
            hr_buf["minimum_samples"],
            hr_buf["buffer_rate_hz"],
        )
        cooldown_ctrl = CooldownController(s["global_settings"]["cooldown_seconds"])
        self._cooldown_ctrl = cooldown_ctrl
        printer = ConsolePrinter()

        # --- BLE Fan Output ---
        ble_fan = BLEFanOutputController(s)
        self._ble_fan = ble_fan
        self._tasks.append(
            asyncio.create_task(
                _guarded_task(
                    ble_fan.run(zone_cmd_queue),
                    "BLEFanOutput",
                    max_retries=3,
                    retry_delay=5.0,
                    coro_factory=lambda: ble_fan.run(zone_cmd_queue),
                ),
                name="BLEFanOutput",
            )
        )

        # --- Bemeneti adatforrások ---
        power_source = ds.get("power_source", DataSource.ANTPLUS)
        hr_source = ds.get("hr_source", DataSource.ANTPLUS)

        if power_source == DataSource.BLE:
            ble_power = BLEPowerInputHandler(s, raw_power_queue)
            self._ble_power = ble_power
            self._tasks.append(
                asyncio.create_task(
                    _guarded_task(
                        ble_power.run(),
                        "BLEPowerInput",
                        max_retries=3,
                        retry_delay=5.0,
                        coro_factory=lambda: ble_power.run(),
                    ),
                    name="BLEPowerInput",
                )
            )

        if hr_source == DataSource.BLE and hr_enabled:
            ble_hr = BLEHRInputHandler(s, raw_hr_queue)
            self._ble_hr = ble_hr
            self._tasks.append(
                asyncio.create_task(
                    _guarded_task(
                        ble_hr.run(),
                        "BLEHRInput",
                        max_retries=3,
                        retry_delay=5.0,
                        coro_factory=lambda: ble_hr.run(),
                    ),
                    name="BLEHRInput",
                )
            )

        self._ble_sensor_handler = BLECombinedSensor(
            power_handler=self._ble_power, hr_handler=self._ble_hr
        )

        needs_zwift = (power_source == DataSource.ZWIFTUDP) or (
            hr_source == DataSource.ZWIFTUDP and hr_enabled
        )
        if needs_zwift:
            # Subprocess indítása – platform-specifikus kezelés
            try:
                if getattr(sys, 'frozen', False):
                    # PyInstaller frozen exe: zwift_api_polling.exe az exe mellett
                    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
                    cmd = [os.path.join(exe_dir, "zwift_api_polling.exe")]
                else:
                    monitor_script = os.path.join(
                        os.path.dirname(os.path.abspath(__file__)), "zwift_api_polling.py"
                    )
                    cmd = [sys.executable, monitor_script]

                if _platform.system() == "Windows":
                    startupinfo = subprocess.STARTUPINFO()
                    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    startupinfo.wShowWindow = 4  # SW_SHOWNOACTIVATE
                    creation_flags = subprocess.CREATE_NEW_CONSOLE
                else:
                    startupinfo = None
                    creation_flags = 0

                popen_kwargs: Dict[str, Any] = dict(
                    stdin=subprocess.DEVNULL,
                    creationflags=creation_flags,
                )
                if startupinfo is not None:
                    popen_kwargs["startupinfo"] = startupinfo
                else:
                    popen_kwargs["close_fds"] = True

                self._zwift_proc = subprocess.Popen(cmd, **popen_kwargs)

                msg = f"zwift_api_polling.py elindítva (PID: {self._zwift_proc.pid})"
                logger.info(msg)

            except FileNotFoundError as exc:
                logger.error(f"zwift_api_polling.py nem található: {exc}")
            except OSError as exc:
                logger.error(f"zwift_api_polling.py indítása sikertelen: {exc}")
            except Exception as exc:
                logger.error(f"Váratlan hiba zwift_api_polling.py indításakor: {exc}")

            # UDP handler mindig létrejön, függetlenül a subprocess sikerétől
            zwiftudp = ZwiftUDPInputHandler(s, raw_power_queue, raw_hr_queue)
            self._zwift_udp = zwiftudp
            self._tasks.append(
                asyncio.create_task(
                    _guarded_task(
                        zwiftudp.run(),
                        "ZwiftUDPInput",
                        max_retries=3,
                        retry_delay=5.0,
                        coro_factory=lambda: zwiftudp.run(),
                    ),
                    name="ZwiftUDPInput",
                )
            )

        needs_antplus = (power_source == DataSource.ANTPLUS) or (
            hr_source == DataSource.ANTPLUS and hr_enabled
        )
        if needs_antplus:
            if _ANTPLUS_AVAILABLE:
                self._antplus_handler = ANTPlusInputHandler(
                    s, raw_power_queue, raw_hr_queue, asyncio.get_running_loop()
                )
                self._antplus_thread = self._antplus_handler.start()
            else:
                logger.warning(
                    "ANT+ forrás kérve, de az openant könyvtár nem elérhető!"
                )

        # --- Feldolgozó és vezérlő korrutinok ---
        self._tasks.append(
            asyncio.create_task(
                _guarded_task(
                    power_processor_task(
                        raw_power_queue,
                        state,
                        zone_event,
                        power_averager,
                        printer,
                        s,
                        power_zones,
                    ),
                    "PowerProcessor",
                ),
                name="PowerProcessor",
            )
        )
        self._tasks.append(
            asyncio.create_task(
                _guarded_task(
                    hr_processor_task(
                        raw_hr_queue,
                        state,
                        zone_event,
                        hr_averager,
                        printer,
                        s,
                        hr_zones,
                    ),
                    "HRProcessor",
                ),
                name="HRProcessor",
            )
        )
        self._tasks.append(
            asyncio.create_task(
                _guarded_task(
                    zone_controller_task(
                        state,
                        zone_cmd_queue,
                        cooldown_ctrl,
                        s,
                        zone_event,
                    ),
                    "ZoneController",
                ),
                name="ZoneController",
            )
        )
        self._tasks.append(
            asyncio.create_task(
                _guarded_task(
                    dropout_checker_task(
                        state,
                        zone_cmd_queue,
                        s,
                        power_averager,
                        hr_averager,
                        power_buf["dropout_timeout"],
                        hr_buf["dropout_timeout"],
                        zone_mode,
                        cooldown_ctrl,
                    ),
                    "DropoutChecker",
                ),
                name="DropoutChecker",
            )
        )

        print()
        print("🚴 Figyelés elindítva... (Ctrl+C a leállításhoz)")
        print()

        try:
            if self._tasks:
                await asyncio.gather(*self._tasks)
            else:
                while self._running:
                    await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            # Fix #12: guard against None if setup crashed before ble_fan init
            if self._ble_fan is not None:
                await self._ble_fan.disconnect()
                self._ble_fan = None
            # Fix #13: ANT+ leállítás a stop()-ban történik, nem duplikáljuk itt

    def stop(self) -> None:
        """Leállítja az összes task-ot és szálat.

        Megjegyzés: task.cancel() csak kérést küld az event loop-nak;
        a tényleges megszakítás az asyncio loop következő iterációján
        történik. A main() asyncio_thread.join(timeout=3.0) hívása
        elegendő időt biztosít a tiszta leálláshoz.
        """
        self._running = False
        for task in self._tasks:
            if not task.done():
                try:
                    task.cancel()
                except Exception:
                    pass
        if self._antplus_handler:
            self._antplus_handler.stop()
        if self._antplus_thread and self._antplus_thread.is_alive():
            self._antplus_thread.join(timeout=5.0)
            if self._antplus_thread.is_alive():
                logger.warning("ANT+ szál nem állt le 5s alatt!")

        # Fix #17: Zwift UDP transport bezárása
        if self._zwift_udp is not None:
            t = getattr(self._zwift_udp, "_transport", None)
            if t is not None:
                try:
                    t.close()
                except Exception:
                    pass

        # Zwift subprocess leállítása
        if self._zwift_proc is not None:
            if self._zwift_proc.poll() is None:  # csak ha még fut
                logger.info(
                    f"zwift_api_polling.py leállítása (PID: {self._zwift_proc.pid})..."
                )
                try:
                    self._zwift_proc.terminate()
                    self._zwift_proc.wait(timeout=5.0)
                    logger.info("zwift_api_polling.py leállítva")
                except subprocess.TimeoutExpired:
                    logger.warning("zwift_api_polling.py nem állt le 5s alatt, kill...")
                    self._zwift_proc.kill()
                except OSError as exc:
                    logger.error(f"zwift_api_polling.py leállítása sikertelen: {exc}")
                finally:
                    self._zwift_proc = None


# ============================================================
# HUD ABLAK (tkinter) – Star Trek LCARS stílus
# ============================================================


class HUDWindow:
    """Lebegő, átlátszó HUD ablak – Star Trek LCARS stílusú megjelenítés."""

    UPDATE_INTERVAL_MS = 500

    # ─── LCARS SZÍN PALETTA ───
    BG = "#000a14"
    PANEL_BG = "#001020"
    LCARS_ORANGE = "#FF9900"
    LCARS_GOLD = "#FFCC66"
    LCARS_BLUE = "#5599FF"
    LCARS_CYAN = "#00CCFF"
    LCARS_CYAN_DIM = "#006688"
    LCARS_RED = "#FF3333"
    LCARS_MAGENTA = "#CC6699"
    LCARS_TAN = "#FFAA66"
    LCARS_PURPLE = "#9977CC"
    TEXT_BRIGHT = "#DDEEFF"
    TEXT_DIM = "#556688"
    BORDER_GLOW = "#003355"
    ZONE_COLORS = {
        0: "#556688",
        1: "#00CCFF",
        2: "#FF9900",
        3: "#FF3333",
    }

    def __init__(self, controller: "FanController") -> None:
        self._base_width = 340
        self._base_height = 460
        self._scale = 1.0
        self._ctrl = controller

        # ───────── ROOT ─────────
        self._root = tk.Tk()
        self._root.title("LCARS Fan HUD")
        self._root.attributes("-topmost", True)
        self._root.attributes("-alpha", 0.92)
        self._root.overrideredirect(True)
        self._root.geometry(f"{self._base_width}x{self._base_height}+20+20")
        self._root.configure(bg=self.BG)

        # ───────── LCARS FONT BETÖLTÉS ─────────
        self._try_load_lcars_font()
        self._font_family = self._detect_best_font()

        # Referencia listák a skálázható label-ekhez
        self._row_key_labels: list = []
        self._status_key_labels: list = []

        # ───────── LCARS FEJLÉC ─────────
        self._header_height = 50
        header = tk.Canvas(
            self._root, bg=self.BG, highlightthickness=0, height=self._header_height
        )
        header.pack(side=tk.TOP, fill=tk.X, padx=0, pady=0)
        self._header_canvas = header
        self._draw_header(header, self._base_width)

        # ───────── LCARS LÁBLÉC (ELŐRE PACK-ELJÜK, HOGY NE TŰNJÖN EL) ─────────
        self._footer_height = 50
        footer = tk.Canvas(
            self._root, bg=self.BG, highlightthickness=0, height=self._footer_height
        )
        footer.pack(side=tk.BOTTOM, fill=tk.X, padx=0, pady=0)
        self._footer_canvas = footer
        self._draw_footer(footer, self._base_width)

        # ───────── FŐ TARTALOM KERET ─────────
        body = tk.Frame(self._root, bg=self.BG)
        body.pack(fill=tk.BOTH, expand=True, padx=0, pady=0)

        # LCARS bal oldalsáv
        sidebar = tk.Canvas(body, bg=self.BG, width=16, highlightthickness=0)
        sidebar.pack(side=tk.LEFT, fill=tk.Y, padx=0)
        self._sidebar = sidebar

        # Jobb oldali tartalom panel
        content = tk.Frame(body, bg=self.PANEL_BG)
        content.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 6), pady=(0, 0))

        # ───────── ZÓNA KIJELZŐ (nagy) ─────────
        zone_frame = tk.Frame(content, bg="#001828", bd=0)
        zone_frame.pack(fill=tk.X, padx=6, pady=(8, 4))

        self._lbl_zone_label = tk.Label(
            zone_frame, text="FAN ZONE", fg="#000a14",
            bg=self.LCARS_CYAN, font=(self._font_family, 9, "bold"),
            anchor="w", padx=4, pady=2,
        )
        self._lbl_zone_label.pack(fill=tk.X, padx=0, pady=(0, 0))

        self._lbl_zone = tk.Label(
            zone_frame, text="– – –", fg=self.LCARS_CYAN,
            bg="#001828", font=(self._font_family, 30, "bold"), anchor="center"
        )
        self._lbl_zone.pack(fill=tk.X, padx=8, pady=(0, 6))

        # ───────── ÁLLAPOT CSÍK (state tiles) ─────────
        tile_frame = tk.Frame(content, bg=self.PANEL_BG)
        tile_frame.pack(fill=tk.X, padx=6, pady=(0, 4))
        tile_frame.bind("<ButtonPress-1>", self._on_drag_start)
        tile_frame.bind("<B1-Motion>", self._on_drag_move)

        self._tile_zero_imm     = self._make_tile(tile_frame, "ZRO IMM")
        self._tile_zero_hr_imm  = self._make_tile(tile_frame, "ZHR IMM")
        self._tile_higher_wins  = self._make_tile(tile_frame, "HI WINS")
        self._tile_ant          = self._make_tile(tile_frame, "ANT+")
        self._tile_ble          = self._make_tile(tile_frame, "BLE")
        self._tile_cooldown     = self._make_tile(tile_frame, "COOL")
        self._tile_frame        = tile_frame

        # ───────── TELEMETRIA SOROK (színes LCARS háttérrel) ─────────
        self._lbl_power = self._make_row(
            content, "POWER", "– – –", self.LCARS_GOLD, label_bg=self.LCARS_TAN)
        self._lbl_hr = self._make_row(
            content, "HEART RATE", "– – –", self.LCARS_RED, label_bg=self.LCARS_ORANGE)

        # ───────── SZEPARÁTOR ─────────
        sep = tk.Canvas(content, bg=self.PANEL_BG, height=2, highlightthickness=0)
        sep.pack(fill=tk.X, padx=10, pady=(6, 6))
        sep.create_line(0, 1, 400, 1, fill=self.BORDER_GLOW, width=1)

        # ───────── RENDSZER STÁTUSZ (színes LCARS háttérrel) ─────────
        self._lbl_ble = self._make_status_row(
            content, "BLE FAN", "OFFLINE", label_bg=self.LCARS_BLUE)
        self._lbl_ble_sens = self._make_status_row(
            content, "BLE SENS", "– – –", label_bg=self.LCARS_BLUE)
        self._lbl_ant = self._make_status_row(
            content, "ANT+", "– – –", label_bg=self.LCARS_PURPLE)
        self._lbl_zwift_udp = self._make_status_row(
            content, "ZWIFT", "– – –", label_bg=self.LCARS_PURPLE)

        # ───────── SZEPARÁTOR 2 ─────────
        sep2 = tk.Canvas(content, bg=self.PANEL_BG, height=2, highlightthickness=0)
        sep2.pack(fill=tk.X, padx=10, pady=(6, 4))
        sep2.create_line(0, 1, 400, 1, fill=self.BORDER_GLOW, width=1)

        # ───────── RENDSZER INFO (színes LCARS háttérrel) ─────────
        self._lbl_last_sent = self._make_status_row(
            content, "LAST TX", "– – –", label_bg=self.LCARS_TAN)
        self._lbl_cool = self._make_status_row(
            content, "COOLDOWN", "– – –", label_bg=self.LCARS_TAN)

        # ───────── ALPHA SLIDER ─────────
        slider_frame = tk.Frame(content, bg=self.PANEL_BG)
        slider_frame.pack(fill=tk.X, padx=6, pady=(6, 4))
        self._slider_frame = slider_frame

        self._opacity_label = tk.Label(
            slider_frame, text="OPACITY", fg="#000a14",
            bg=self.LCARS_GOLD, font=(self._font_family, 9, "bold"),
            anchor="w", padx=4, pady=2
        )
        self._opacity_label.pack(side=tk.LEFT)

        self._alpha_value = tk.Label(
            slider_frame, text="92%", fg=self.LCARS_CYAN,
            bg=self.PANEL_BG, font=(self._font_family, 11, "bold"), width=4
        )
        self._alpha_value.pack(side=tk.RIGHT, padx=(4, 0))

        self._alpha_slider = tk.Scale(
            slider_frame,
            from_=20, to=100, orient=tk.HORIZONTAL,
            fg=self.LCARS_CYAN, bg=self.PANEL_BG,
            troughcolor="#002244",
            activebackground=self.LCARS_BLUE,
            highlightbackground=self.BORDER_GLOW,
            highlightthickness=1,
            borderwidth=0, relief="flat",
            command=self._on_alpha_change,
            showvalue=False, length=160, width=14,
        )
        self._alpha_slider.set(92)
        self._alpha_slider.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 0))

        # ───────── DRAG & RESIZE ─────────
        self._drag_x = 0
        self._drag_y = 0
        self._resize_start_w = 0
        self._resize_start_h = 0
        self._resize_start_x = 0
        self._resize_start_y = 0

        drag_widgets = [
            self._lbl_zone, self._lbl_zone_label,
            self._lbl_power, self._lbl_hr,
            self._lbl_ble, self._lbl_ble_sens, self._lbl_ant, self._lbl_zwift_udp,
            self._lbl_last_sent, self._lbl_cool,
            header, sidebar, footer, zone_frame, content,
        ]
        for w in drag_widgets:
            w.bind("<ButtonPress-1>", self._on_drag_start)
            w.bind("<B1-Motion>", self._on_drag_move)

        self._root.bind("<ButtonPress-1>", self._on_drag_start)
        self._root.bind("<B1-Motion>", self._on_drag_move)
        self._root.bind("<Escape>", lambda e: self.close())
        self._root.bind("<ButtonPress-3>", self._show_menu)

        # ───────── KONTEXTUS MENÜ ─────────
        self._menu = tk.Menu(
            self._root, tearoff=0, bg="#001828", fg=self.LCARS_CYAN,
            activebackground=self.LCARS_BLUE, activeforeground="white",
            font=(self._font_family, 10),
        )
        self._menu.add_command(label="Bezárás", command=self.close)
        self._menu.add_separator()
        self._menu.add_command(
            label="Opacity: 50%", command=lambda: self._set_alpha_from_menu(50)
        )
        self._menu.add_command(
            label="Opacity: 85%", command=lambda: self._set_alpha_from_menu(85)
        )
        self._menu.add_command(
            label="Opacity: 100%", command=lambda: self._set_alpha_from_menu(100)
        )

        # ───────── RESIZE GRIP ─────────
        self._resize_grip = tk.Canvas(
            self._root, width=18, height=18,
            bg=self.BG, highlightthickness=0, cursor="size_nw_se"
        )
        self._resize_grip.place(relx=1.0, rely=1.0, anchor="se")
        self._resize_grip.create_polygon(
            0, 18, 18, 18, 18, 0,
            fill=self.LCARS_ORANGE, outline=self.LCARS_GOLD
        )
        self._resize_grip.bind("<ButtonPress-1>", self._on_resize_start)
        self._resize_grip.bind("<B1-Motion>", self._on_resize_drag)

        self._content = content
        self._update()

    # ───────── LCARS FONT BETÖLTÉS ─────────

    def _try_load_lcars_font(self) -> None:
        """Antonio font letöltése és betöltése (Windows)."""
        if _platform.system() != "Windows":
            return
        try:
            import urllib.request
            import ctypes

            font_dir = os.path.join(os.path.expanduser("~"), ".swift_fan_fonts")
            os.makedirs(font_dir, exist_ok=True)

            for style in ("Bold", "Regular"):
                fpath = os.path.join(font_dir, f"Antonio-{style}.ttf")
                if not os.path.exists(fpath):
                    url = (
                        "https://raw.githubusercontent.com/google/fonts/main/"
                        f"ofl/antonio/static/Antonio-{style}.ttf"
                    )
                    urllib.request.urlretrieve(url, fpath)
                FR_PRIVATE = 0x10
                ctypes.windll.gdi32.AddFontResourceExW(fpath, FR_PRIVATE, 0)
        except Exception:
            pass  # sikertelen letöltés: rendszer fontokra váltunk

    def _detect_best_font(self) -> str:
        """Legjobb elérhető LCARS-stílusú font kiválasztása."""
        try:
            available = set(tkfont.families(root=self._root))
        except Exception:
            return "Consolas"

        # Prioritás: autentikus LCARS kinézet → modern futurisztikus → fallback
        preferred = [
            "Antonio",            # Google Font – legközelebbi az LCARS-hoz
            "Michroma",           # Google Font – tiszta, futurisztikus
            "Century Gothic",     # Windows – kerekített, LCARS-szerű
            "Eras Bold ITC",      # MS Office – nagyon LCARS-szerű
            "Eras Medium ITC",    # MS Office
            "Bahnschrift",        # Windows 10+ – modern, tiszta
            "Trebuchet MS",       # Windows – kerekített sans-serif
            "Segoe UI",           # Windows – tiszta, olvasható
            "Consolas",           # Monospace fallback
        ]
        for f in preferred:
            if f in available:
                return f
        return "Consolas"

    # ───────── LCARS DEKORÁCIÓS RAJZOK ─────────

    @staticmethod
    def _arc_points(cx: float, cy: float, r: float,
                    start_deg: float, end_deg: float, steps: int = 20) -> list:
        """Ív pontjait adja vissza [x1,y1, x2,y2, ...] formátumban.

        A szögek matematikai konvencióban (0°=jobb, CCW pozitív),
        Canvas koordinátákra konvertálva (y lefelé nő).
        """
        pts: list = []
        for i in range(steps + 1):
            angle = math.radians(start_deg + (end_deg - start_deg) * i / steps)
            pts.append(cx + r * math.cos(angle))
            pts.append(cy - r * math.sin(angle))  # canvas y: lefelé nő
        return pts

    def _draw_header(self, canvas: tk.Canvas, w: int) -> None:
        """LCARS fejléc rajzolása – felső sáv + lekerekített belső sarok.

        Minden méret a scale-hez igazodik.
        """
        canvas.delete("all")
        s = self._scale
        ch = self._header_height  # aktuális canvas magasság
        bar_h = max(8, int(14 * s))
        sw = max(10, int(16 * s))
        R = max(14, int(26 * s))

        pts = [
            0, 0,
            w - 6, 0,
            w - 6, bar_h,
        ]
        pts.extend(self._arc_points(sw + R, bar_h + R, R, 90, 180, 20))
        pts.extend([sw, ch, 0, ch])
        canvas.create_polygon(pts, fill=self.LCARS_ORANGE, outline="", smooth=False)

        title_size = max(8, int(12 * s))
        canvas.create_text(
            (sw + R + w - 6) // 2, bar_h + max(10, int(18 * s)),
            text="SWIFT FAN CTRL",
            fill=self.LCARS_CYAN, font=(self._font_family, title_size, "bold"),
        )
        badge_w = max(40, int(62 * s))
        canvas.create_rectangle(
            w - badge_w - 8, 1, w - 8, bar_h - 2,
            fill=self.LCARS_MAGENTA, outline="",
        )
        ver_size = max(6, int(7 * s))
        canvas.create_text(
            w - badge_w // 2 - 8, bar_h // 2 - 1, text="v1.0.0",
            fill="#FFFFFF", font=(self._font_family, ver_size),
        )

        # Külső lekerekített sarok (csak felső-bal)
        r = max(12, int(18 * s))
        canvas.create_rectangle(0, 0, r, r, fill=self.BG, outline="")
        canvas.create_arc(0, 0, 2 * r, 2 * r, start=90, extent=90,
                          fill=self.LCARS_ORANGE, outline="")

    def _draw_footer(self, canvas: tk.Canvas, w: int) -> None:
        """LCARS lábléc rajzolása – alsó sáv + lekerekített belső sarok.

        Szimmetrikus a fejléccel: minden méret a scale-hez igazodik.
        """
        canvas.delete("all")
        s = self._scale
        fh = self._footer_height  # aktuális canvas magasság
        bar_h = max(8, int(14 * s))
        sw = max(10, int(16 * s))
        R = max(14, int(26 * s))
        bar_top = fh - bar_h

        pts = [
            0, 0,
            sw, 0,
        ]
        pts.extend(self._arc_points(sw + R, bar_top - R, R, 180, 270, 20))
        pts.extend([w - 6, bar_top, w - 6, fh, 0, fh])
        canvas.create_polygon(pts, fill=self.LCARS_BLUE, outline="", smooth=False)

        seg_x = sw + R + 8
        seg_w = max(1, (w - 6 - seg_x) // 3)
        canvas.create_rectangle(
            seg_x + seg_w + 4, bar_top, seg_x + 2 * seg_w, fh,
            fill=self.LCARS_PURPLE, outline="",
        )
        canvas.create_rectangle(
            seg_x + 2 * seg_w + 4, bar_top, w - 6, fh,
            fill=self.LCARS_TAN, outline="",
        )

        footer_text_size = max(7, int(9 * s))
        canvas.create_text(
            (sw + R + w - 6) // 2, max(6, bar_top // 2),
            text="STARFLEET CYCLING DIV",
            fill=self.LCARS_CYAN_DIM, font=(self._font_family, footer_text_size),
        )

        # Külső lekerekített sarok (csak alsó-bal)
        r = max(12, int(18 * s))
        canvas.create_rectangle(0, fh - r, r, fh, fill=self.BG, outline="")
        canvas.create_arc(0, fh - 2 * r, 2 * r, fh, start=180, extent=90,
                          fill=self.LCARS_BLUE, outline="")

    def _draw_sidebar(self, h: int) -> None:
        """LCARS bal oldalsáv rajzolása – színes szegmensek.

        A szegmensek szélessége a skálához igazodik,
        és az utolsó szegmens mindig kitölti a maradék helyet.
        """
        self._sidebar.delete("all")
        sw = max(10, int(16 * self._scale))
        self._sidebar.config(width=sw)
        if h < 10:
            return
        colors = [self.LCARS_ORANGE, self.LCARS_GOLD, self.LCARS_BLUE,
                  self.LCARS_MAGENTA, self.LCARS_PURPLE, self.LCARS_TAN]
        n = len(colors)
        seg_h = max(10, h // n)
        gap = max(1, int(2 * self._scale))
        for i, c in enumerate(colors):
            y = i * seg_h
            # Az utolsó szegmens mindig a sidebar aljáig ér
            bottom = h if i == n - 1 else y + seg_h
            self._sidebar.create_rectangle(
                0, y + gap, sw, bottom - gap, fill=c, outline="",
            )

    # ───────── UI SEGÉDEK ─────────

    # ── LCARS SZÍN HÁTTERES PANELES SOR ──────────────────────

    _VAL_BG = "#001828"  # érték-mező sötét háttere

    def _make_row(self, parent: tk.Frame, label: str, value: str,
                  color: str, label_bg: str | None = None) -> tk.Label:
        """Telemetria sor LCARS színes label háttérrel."""
        if label_bg is None:
            label_bg = self.LCARS_TAN
        row = tk.Frame(parent, bg=self.PANEL_BG)
        row.pack(fill=tk.X, padx=6, pady=2)

        key_lbl = tk.Label(
            row, text=label, fg="#000a14",
            bg=label_bg, font=(self._font_family, 9, "bold"),
            width=12, anchor="w", padx=4, pady=3,
        )
        key_lbl.pack(side=tk.LEFT, fill=tk.Y)
        self._row_key_labels.append(key_lbl)

        val_lbl = tk.Label(
            row, text=value, fg=color,
            bg=self._VAL_BG, font=(self._font_family, 14, "bold"),
            anchor="e", padx=6, pady=3,
        )
        val_lbl.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(2, 0))

        for w in (key_lbl, val_lbl, row):
            w.bind("<ButtonPress-1>", self._on_drag_start)
            w.bind("<B1-Motion>", self._on_drag_move)
        return val_lbl

    def _make_tile(self, parent: tk.Frame, text: str) -> tk.Label:
        """Állapot csík – egy színes téglaszerű kis Label."""
        lbl = tk.Label(
            parent,
            text=text,
            fg="#000a14",
            bg=self.TEXT_DIM,
            font=(self._font_family, 9, "bold"),
            padx=5, pady=2,
            anchor="center",
        )
        lbl.pack(side=tk.LEFT, padx=(0, 2), pady=0, expand=True, fill=tk.X)
        lbl.bind("<ButtonPress-1>", self._on_drag_start)
        lbl.bind("<B1-Motion>", self._on_drag_move)
        return lbl

    def _make_status_row(self, parent: tk.Frame, label: str, value: str,
                         label_bg: str | None = None) -> tk.Label:
        """Státusz sor LCARS színes label háttérrel."""
        if label_bg is None:
            label_bg = self.LCARS_BLUE
        row = tk.Frame(parent, bg=self.PANEL_BG)
        row.pack(fill=tk.X, padx=6, pady=2)

        key_lbl = tk.Label(
            row, text=label, fg="#000a14",
            bg=label_bg, font=(self._font_family, 9, "bold"),
            width=12, anchor="w", padx=4, pady=2,
        )
        key_lbl.pack(side=tk.LEFT, fill=tk.Y)
        self._status_key_labels.append(key_lbl)

        val_lbl = tk.Label(
            row, text=value, fg=self.TEXT_DIM,
            bg=self._VAL_BG, font=(self._font_family, 11),
            anchor="e", padx=6, pady=2,
        )
        val_lbl.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(2, 0))

        for w in (key_lbl, val_lbl, row):
            w.bind("<ButtonPress-1>", self._on_drag_start)
            w.bind("<B1-Motion>", self._on_drag_move)
        return val_lbl

    # ───────── SEGÉDFÜGGVÉNYEK ─────────

    def _on_resize_start(self, event: tk.Event) -> None:
        self._resize_start_w = self._root.winfo_width()
        self._resize_start_h = self._root.winfo_height()
        self._resize_start_x = event.x_root
        self._resize_start_y = event.y_root

    def _on_resize_drag(self, event: tk.Event) -> None:
        dx = event.x_root - self._resize_start_x
        dy = event.y_root - self._resize_start_y
        new_w = max(220, self._resize_start_w + dx)
        new_h = max(280, self._resize_start_h + dy)
        self._root.geometry(f"{new_w}x{new_h}")
        self._scale = new_w / self._base_width
        self._apply_scale()
        self._draw_header(self._header_canvas, new_w)
        self._draw_footer(self._footer_canvas, new_w)
        sidebar_h = max(10, new_h - self._header_height - self._footer_height)
        self._draw_sidebar(sidebar_h)
        self._resize_grip.place(relx=1.0, rely=1.0, anchor="se")

    def _set_alpha_from_menu(self, percent: int) -> None:
        self._root.attributes("-alpha", percent / 100.0)
        self._alpha_slider.set(percent)
        self._alpha_value.config(text=f"{percent}%")

    def _on_alpha_change(self, value: str) -> None:
        try:
            v = int(float(value))
        except ValueError:
            return
        self._root.attributes("-alpha", v / 100.0)
        self._alpha_value.config(text=f"{v}%")

    def _on_drag_start(self, event: tk.Event) -> None:
        if event.widget in (
            self._alpha_slider, self._alpha_value,
            self._slider_frame, self._resize_grip,
        ):
            return
        self._drag_x = event.x
        self._drag_y = event.y

    def _on_drag_move(self, event: tk.Event) -> None:
        if event.widget in (
            self._alpha_slider, self._alpha_value,
            self._slider_frame, self._resize_grip,
        ):
            return
        x = self._root.winfo_x() + event.x - self._drag_x
        y = self._root.winfo_y() + event.y - self._drag_y
        self._root.geometry(f"+{x}+{y}")

    def _show_menu(self, event: tk.Event) -> None:
        try:
            self._menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._menu.grab_release()

    # ───────── FRISSÍTÉS ─────────

    def _update(self) -> None:
        try:
            state = self._ctrl._state
            ble_fan = self._ctrl._ble_fan
            cool = self._ctrl._cooldown_ctrl

            if state is not None:
                zone, power, hr = state.ui_snapshot.read()

                zone_color = (
                    self.ZONE_COLORS.get(zone, self.LCARS_CYAN)
                    if zone is not None else self.TEXT_DIM
                )
                zone_names = {0: "STANDBY", 1: "ZONE 1", 2: "ZONE 2", 3: "ZONE 3"}
                zone_txt = zone_names.get(zone, "– – –") if zone is not None else "– – –"

                self._lbl_zone.config(text=zone_txt, fg=zone_color)

                self._lbl_power.config(
                    text="– – –" if power is None else f"{power:.0f} W",
                    fg=self.LCARS_GOLD if power is not None else self.TEXT_DIM,
                )
                self._lbl_hr.config(
                    text="– – –" if hr is None else f"{hr:.0f} BPM",
                    fg=self.LCARS_RED if hr is not None else self.TEXT_DIM,
                )

            # BLE fan
            if ble_fan is not None:
                if ble_fan._auth_failed:
                    self._lbl_ble.config(text="PIN FAIL", fg=self.LCARS_GOLD)
                elif ble_fan.is_connected:
                    self._lbl_ble.config(text="ONLINE", fg=self.LCARS_CYAN)
                else:
                    self._lbl_ble.config(text="OFFLINE", fg=self.LCARS_RED)
            else:
                self._lbl_ble.config(text="DISABLED", fg=self.TEXT_DIM)

            # BLE szenzorok
            ds = self._ctrl.settings["datasource"]
            power_ble = ds.get("power_source") == DataSource.BLE
            hr_ble = ds.get("hr_source") == DataSource.BLE

            if not power_ble and not hr_ble:
                self._lbl_ble_sens.config(text="– – –", fg=self.TEXT_DIM)
            else:
                ble = getattr(self._ctrl, "_ble_sensor_handler", None)
                if ble is not None:
                    now = time.monotonic()
                    power_ok = power_ble and (ble.power_lastdata > 0) and (now - ble.power_lastdata < 10)
                    hr_ok = hr_ble and (ble.hr_lastdata > 0) and (now - ble.hr_lastdata < 10)
                    p_s = "OK" if power_ok else ("--" if not power_ble else "FAIL")
                    h_s = "OK" if hr_ok else ("--" if not hr_ble else "FAIL")

                    ble_states = []
                    if power_ble: ble_states.append(power_ok)
                    if hr_ble: ble_states.append(hr_ok)

                    if any(s is False for s in ble_states):
                        row_color = self.LCARS_RED
                    elif all(s is True for s in ble_states):
                        row_color = self.LCARS_CYAN
                    else:
                        row_color = self.LCARS_GOLD

                    self._lbl_ble_sens.config(text=f"P:{p_s}  HR:{h_s}", fg=row_color)
                else:
                    self._lbl_ble_sens.config(text="STANDBY", fg=self.LCARS_GOLD)

            # ANT+
            power_ant = ds.get("power_source") == DataSource.ANTPLUS
            hr_ant = ds.get("hr_source") == DataSource.ANTPLUS
            ant = getattr(self._ctrl, "_antplus_handler", None)

            if not power_ant and not hr_ant:
                self._lbl_ant.config(text="– – –", fg=self.TEXT_DIM)
            elif ant is not None:
                now = time.monotonic()
                power_ok = power_ant and (ant.power_lastdata > 0) and (now - ant.power_lastdata < 10)
                hr_ok = hr_ant and (ant.hr_lastdata > 0) and (now - ant.hr_lastdata < 10)
                p_s = "OK" if power_ok else ("--" if not power_ant else "FAIL")
                h_s = "OK" if hr_ok else ("--" if not hr_ant else "FAIL")

                ant_states = []
                if power_ant: ant_states.append(power_ok)
                if hr_ant: ant_states.append(hr_ok)

                if any(s is False for s in ant_states):
                    row_color = self.LCARS_RED
                elif all(s is True for s in ant_states):
                    row_color = self.LCARS_CYAN
                else:
                    row_color = self.LCARS_GOLD

                self._lbl_ant.config(text=f"P:{p_s}  HR:{h_s}", fg=row_color)
            else:
                self._lbl_ant.config(text="– – –", fg=self.TEXT_DIM)

            # Zwift
            zwift = getattr(self._ctrl, "_zwift_udp", None)
            power_zwift = ds.get("power_source") == DataSource.ZWIFTUDP
            hr_zwift = ds.get("hr_source") == DataSource.ZWIFTUDP

            if zwift is not None and (power_zwift or hr_zwift):
                now = time.monotonic()
                ok = zwift.last_packet_time > 0 and (now - zwift.last_packet_time) < 5.0
                self._lbl_zwift_udp.config(
                    text="RECEIVING" if ok else "NO SIGNAL",
                    fg=self.LCARS_CYAN if ok else self.LCARS_RED,
                )
            else:
                self._lbl_zwift_udp.config(text="– – –", fg=self.TEXT_DIM)

            # Last TX
            if ble_fan is not None and getattr(ble_fan, "last_sent_time", 0) > 0:
                ago = time.monotonic() - ble_fan.last_sent_time
                self._lbl_last_sent.config(
                    text=f"{ago:.0f}s AGO", fg=self.LCARS_TAN
                )
            else:
                self._lbl_last_sent.config(text="– – –", fg=self.TEXT_DIM)

            # Cooldown
            if cool is not None:
                active, remaining = cool.snapshot()
                if active:
                    self._lbl_cool.config(
                        text=f"{remaining:.0f}s", fg=self.LCARS_GOLD
                    )
                else:
                    self._lbl_cool.config(text="INACTIVE", fg=self.TEXT_DIM)
            else:
                self._lbl_cool.config(text="– – –", fg=self.TEXT_DIM)

            # ── Állapot csík frissítése ──
            # zero_power_immediate
            zpi = self._ctrl.settings["power_zones"].get("zero_power_immediate", False)
            self._tile_zero_imm.config(bg=self.LCARS_CYAN if zpi else self.TEXT_DIM)

            # zero_hr_immediate
            zhi = self._ctrl.settings["power_zones"]["heart_rate_zones"].get("zero_hr_immediate", False)
            self._tile_zero_hr_imm.config(bg=self.LCARS_CYAN if zhi else self.TEXT_DIM)

            # higher_wins
            zone_mode_val = self._ctrl.settings["power_zones"]["heart_rate_zones"].get(
                "zone_mode", ZoneMode.POWER_ONLY
            )
            hw = (zone_mode_val == ZoneMode.HIGHER_WINS)
            self._tile_higher_wins.config(bg=self.LCARS_ORANGE if hw else self.TEXT_DIM)

            # ANT HR/PWR
            self._tile_ant.config(
                bg=self.LCARS_PURPLE if (power_ant or hr_ant) else self.TEXT_DIM
            )

            # BLE HR/PWR
            self._tile_ble.config(
                bg=self.LCARS_BLUE if (power_ble or hr_ble) else self.TEXT_DIM
            )

            # Cooldown tile
            if cool is not None:
                cd_active, _ = cool.snapshot()
                self._tile_cooldown.config(bg=self.LCARS_GOLD if cd_active else self.TEXT_DIM)
            else:
                self._tile_cooldown.config(bg=self.TEXT_DIM)

        except Exception as exc:
            logger.warning(f"HUD _update hiba: {exc}")
        finally:
            self._root.after(self.UPDATE_INTERVAL_MS, self._update)

    def _apply_scale(self) -> None:
        s = self._scale
        ff = self._font_family

        # ── Zóna kijelző ──
        self._lbl_zone.config(font=(ff, max(14, int(30 * s)), "bold"))
        self._lbl_zone_label.config(font=(ff, max(7, int(9 * s))))

        # ── Telemetria adat sorok (POWER, HR) – értékek ──
        val_size = max(10, int(14 * s))
        for lbl in [self._lbl_power, self._lbl_hr]:
            lbl.config(font=(ff, val_size, "bold"))

        # ── Telemetria adat sorok – kulcs label-ek (színes hátteres, bold) ──
        row_key_size = max(7, int(9 * s))
        for lbl in self._row_key_labels:
            lbl.config(font=(ff, row_key_size, "bold"))

        # ── Státusz sorok – értékek ──
        status_size = max(8, int(11 * s))
        for lbl in [
            self._lbl_ble, self._lbl_ble_sens, self._lbl_ant,
            self._lbl_zwift_udp, self._lbl_last_sent, self._lbl_cool,
        ]:
            lbl.config(font=(ff, status_size))

        # ── Státusz sorok – kulcs label-ek (színes hátteres, bold) ──
        status_key_size = max(7, int(9 * s))
        for lbl in self._status_key_labels:
            lbl.config(font=(ff, status_key_size, "bold"))

        # ── Állapot csík (tile-ok) ──
        tile_size = max(6, int(7 * s))
        for tile in (
            self._tile_zero_imm, self._tile_zero_hr_imm, self._tile_higher_wins,
            self._tile_ant, self._tile_ble, self._tile_cooldown,
        ):
            tile.config(font=(ff, tile_size, "bold"))

        # ── Opacity slider + label ──
        self._alpha_slider.config(length=max(80, int(160 * s)), width=max(10, int(14 * s)))
        self._alpha_value.config(font=(ff, max(8, int(11 * s)), "bold"))
        self._opacity_label.config(font=(ff, max(7, int(9 * s)), "bold"))

        # ── Header / Footer canvas magasság skálázása ──
        new_header_h = max(30, int(50 * s))
        new_footer_h = max(30, int(50 * s))
        self._header_canvas.config(height=new_header_h)
        self._footer_canvas.config(height=new_footer_h)
        self._header_height = new_header_h
        self._footer_height = new_footer_h

    # ───────── FUTTATÁS / BEZÁRÁS ─────────

    def run(self) -> None:
        self._draw_sidebar(self._base_height - self._header_height - self._footer_height)
        self._root.mainloop()

    def close(self) -> None:
        try:
            self._root.destroy()
        except Exception:
            pass


# ============================================================
# MAIN
# ============================================================


def main() -> None:
    # Windows: SelectorEventLoop megbízhatóbb threaded asyncio-hoz
    # Python 3.16-tól ezek az API-k eltávolításra kerülnek
    if _platform.system() == "Windows" and sys.version_info < (3, 14):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    # Fix #27: logging basicConfig – handler nélkül a logger.info/warning láthatatlan
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("swift_fan_controller_new").setLevel(logging.ERROR)
    logging.getLogger("bleak").setLevel(logging.CRITICAL)
    logging.getLogger("openant").setLevel(logging.CRITICAL)

    # PyInstaller frozen exe: settings.json az exe mellett keresendő
    if getattr(sys, 'frozen', False):
        _exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        _settings_path = os.path.join(_exe_dir, "settings.json")
    else:
        _settings_path = "settings.json"
    controller = FanController(_settings_path)
    controller.print_startup_info()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    cleaned_up = False
    # Shutdown event: az asyncio loop-ot megbízhatóan leállítja
    shutdown_event = asyncio.Event()
    # Mutable konténer: a signal handler-nek kell a HUD referencia,
    # ami később jön létre. Lista azért, hogy nonlocal nélkül módosítható legyen.
    hud_ref: list = [None]

    def cleanup() -> None:
        nonlocal cleaned_up
        if cleaned_up:
            return
        cleaned_up = True
        controller.stop()

    def signal_handler(signum: int, frame: Any) -> None:
        print(f"\nSignal {signum} fogadva, leállítás...")
        cleanup()
        # Tkinter mainloop leállítása – ez a kulcs, különben a fő szál nem áll le
        hud = hud_ref[0]
        if hud is not None:
            try:
                # after_idle: a tkinter szálból hívja a destroy-t (thread-safe)
                hud._root.after_idle(hud.close)
            except Exception:
                pass
        try:
            loop.call_soon_threadsafe(shutdown_event.set)
        except Exception:
            pass

    # SIGTERM: Unix-on megbízható, Windows-on a Popen.terminate() nem garantálja
    # SIGINT: Ctrl+C mindkét platformon működik
    signal.signal(signal.SIGTERM, signal_handler)
    try:
        signal.signal(signal.SIGINT, signal_handler)
    except (OSError, ValueError):
        pass  # Egyes környezetekben (pl. nem főszál) SIGINT nem regisztrálható

    atexit.register(cleanup)

    loop_ready = threading.Event()

    async def _run_until_shutdown() -> None:
        """Controller futtatása shutdown_event-ig."""
        controller_task = asyncio.create_task(controller.run())
        shutdown_task = asyncio.create_task(shutdown_event.wait())
        done, pending = await asyncio.wait(
            [controller_task, shutdown_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass

    def run_asyncio() -> None:
        asyncio.set_event_loop(loop)
        # Jelezzük, hogy az event loop fut és kész feladatokat fogadni
        loop.call_soon(loop_ready.set)
        try:
            loop.run_until_complete(_run_until_shutdown())
        except Exception as exc:
            # Fix #22: teljes traceback logolása
            logger.error(f"AsyncioThread hiba: {exc}", exc_info=True)
        finally:
            loop_ready.set()  # Ha hiba miatt kilép, ne blokkoljon örökre

    asyncio_thread = threading.Thread(
        target=run_asyncio, daemon=True, name="AsyncioThread"
    )
    asyncio_thread.start()

    # Megvárjuk, amíg az asyncio event loop ténylegesen elindul (max 5s)
    loop_ready.wait(timeout=5.0)

    # Fix #20: tkinter opcionális – headless módban HUD nélkül fut
    if _TKINTER_AVAILABLE:
        hud = HUDWindow(controller)
        hud_ref[0] = hud
        try:
            hud.run()
        except KeyboardInterrupt:
            print("\nLeállítás (Ctrl+C)...")
        finally:
            hud_ref[0] = None
            hud.close()
            cleanup()

            # Shutdown event jelzése → asyncio loop megbízhatóan leáll
            try:
                loop.call_soon_threadsafe(shutdown_event.set)
            except Exception:
                pass

            asyncio_thread.join(timeout=3.0)
            loop.close()
            print("\nProgram leállítva.")
    else:
        logger.warning("tkinter nem elérhető, HUD nélkül fut")
        print("⚠ tkinter nem elérhető – HUD nélkül fut. Ctrl+C a leállításhoz.")
        try:
            asyncio_thread.join()
        except KeyboardInterrupt:
            print("\nLeállítás (Ctrl+C)...")
        finally:
            cleanup()
            try:
                loop.call_soon_threadsafe(shutdown_event.set)
            except Exception:
                pass
            asyncio_thread.join(timeout=3.0)
            loop.close()
            print("\nProgram leállítva.")


if __name__ == "__main__":
    main()
