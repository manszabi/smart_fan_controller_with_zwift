import os
import sys
import logging
import json
import math
import time
import asyncio
import threading
import queue
import copy
import signal
import atexit
import socket
from collections import deque

__version__ = "1.3.0"
from openant.easy.node import Node
from openant.devices import ANTPLUS_NETWORK_KEY
from openant.devices.power_meter import PowerMeter, PowerData
from openant.devices.heart_rate import HeartRate, HeartRateData
from bleak import BleakClient, BleakScanner

logger = logging.getLogger('smart_fan_controller')

# ============================================================
# Alapértelmezett beállítások
# ============================================================
# FONTOS: NE módosítsd közvetlenül! Mindig copy.deepcopy()-val használd.
DEFAULT_SETTINGS = {
    "ftp": 180,                    # Funkcionális küszöbteljesítmény wattban (100–500)
    "min_watt": 0,                 # Minimális érvényes teljesítmény (0 vagy több)
    "max_watt": 1000,              # Maximális érvényes teljesítmény (min_watt-nál több)
    "cooldown_seconds": 120,       # Zóna csökkentés előtti várakozási idő másodpercben (0–300)
    "buffer_seconds": 3,           # Átlagolási ablak mérete másodpercben (1–10)
    "minimum_samples": 8,          # Zónadöntéshez szükséges minimális minták száma
    "dropout_timeout": 5,          # Adat nélküli idő (s), ami után 0-s zónára vált
    "zero_power_immediate": False, # True: 0W esetén azonnali leállás cooldown nélkül
    "zone_thresholds": {
        # Zóna határok az FTP százalékában:
        # Z0: 0W (leállás), Z1: 1W–z1_max, Z2: z1_max+1–z2_max, Z3: z2_max+1–max_watt
        "z1_max_percent": 60,      # Z1 felső határ: FTP×60% (pl. 180W → 108W)
        "z2_max_percent": 89       # Z2 felső határ: FTP×89% (pl. 180W → 160W)
    },
    "ble": {
        "device_name": "FanController",  # BLE eszköz neve (pontosan egyezzen az ESP32-vel)
        "scan_timeout": 10,        # BLE keresési időkorlát másodpercben (1–60)
        "connection_timeout": 15,  # BLE csatlakozási időkorlát másodpercben (1–60)
        "reconnect_interval": 5,   # Újracsatlakozási próbák közötti várakozás (s, 1–60)
        "max_retries": 10,         # Maximális újracsatlakozási kísérletek száma (1–100)
        "command_timeout": 3,      # BLE parancs küldési időkorlát másodpercben (1–30)
        "service_uuid": "0000ffe0-0000-1000-8000-00805f9b34fb",         # GATT szerviz UUID
        "characteristic_uuid": "0000ffe1-0000-1000-8000-00805f9b34fb", # GATT karakterisztika UUID
        "pin_code": None           # BLE PIN kód alkalmazás szintű autentikációhoz (null = nincs auth, int 0–999999 vagy string pl. "007")
    },
    "data_source": {
        "power_source": "antplus",           # Power adatforrás: "antplus", "ble" vagy "zwift_udp"
        "hr_source": "antplus",              # HR adatforrás: "antplus", "ble" vagy "zwift_udp"
        "ble_power_device_name": None,       # BLE power meter neve (szükséges ha power_source: "ble")
        "ble_power_scan_timeout": 10,        # BLE power keresési időkorlát (s, 1–60)
        "ble_power_reconnect_interval": 5,   # BLE power újracsatlakozási várakozás (s, 1–60)
        "ble_power_max_retries": 10,         # BLE power maximális újracsatlakozási kísérletek (1–100)
        "ble_hr_device_name": None,          # BLE HR eszköz neve (szükséges ha hr_source: "ble")
        "ble_hr_scan_timeout": 10,           # BLE HR keresési időkorlát (s, 1–60)
        "ble_hr_reconnect_interval": 5,      # BLE HR újracsatlakozási várakozás (s, 1–60)
        "ble_hr_max_retries": 10,            # BLE HR maximális újracsatlakozási kísérletek (1–100)
        "zwift_udp_port": 7878,              # UDP port amire a Zwift UDP listener figyel (1024–65535)
        "zwift_udp_host": "127.0.0.1",       # Listen cím (localhost)
        "zwift_udp_buffer_seconds": 10,      # Átlagolási ablak Zwift UDP forrás esetén (1–60)
        "zwift_udp_minimum_samples": 2,      # Zónadöntéshez szükséges minimum minták Zwift UDP esetén (1–20)
        "zwift_udp_dropout_timeout": 15,     # Dropout timeout Zwift UDP forrás esetén (1–120)
    },
    "heart_rate_zones": {
        "enabled": False,          # True: HR zóna rendszer aktív (befolyásolja a ventilátort)
        "max_hr": 185,             # Maximális szívfrekvencia bpm-ben (100–220)
        "resting_hr": 60,          # Pihenő szívfrekvencia bpm-ben (30–100); ez alatt → Z0
        # zone_mode: a HR és teljesítmény zóna összevonási módja:
        #   "power_only"  – csak a teljesítmény zóna dönt (HR figyelmen kívül)
        #   "hr_only"     – csak a HR zóna dönt (teljesítmény figyelmen kívül)
        #   "higher_wins" – a kettő közül a magasabb értékű zóna dönt
        "zone_mode": "power_only",
        "z1_max_percent": 70,      # HR Z1 felső határ: max_hr×70% (pl. 185 → 129 bpm)
        "z2_max_percent": 80       # HR Z2 felső határ: max_hr×80% (pl. 185 → 148 bpm)
    }
}


# ============================================================
# BLEController
# ============================================================
class BLEController:
    """BLE (Bluetooth Low Energy) kapcsolat kezelője az ESP32 ventilátor vezérlőhöz.

    Egy dedikált háttérszálban futó asyncio event loop segítségével kezeli
    a BLE kapcsolatot, parancsok sorba állítását és küldését.
    Ha pin_code be van állítva, alkalmazás szintű PIN autentikációt végez
    a kapcsolat felépítése után (AUTH:<pin_code> üzenet a GATT karakterisztikára).

    Attribútumok:
        device_name (str): A keresett BLE eszköz neve.
        command_queue (queue.Queue): A BLE parancsok várakozási sora (max 1 elem).
        running (bool): True, ha a háttérszál fut.
        is_connected (bool): True, ha a BLE kapcsolat aktív.
    """

    QUEUE_POLL_TIMEOUT = 0.5
    DISCONNECT_TIMEOUT = 5.0
    RETRY_RESET_SECONDS = 30
    STOP_JOIN_TIMEOUT = 5

    def __init__(self, settings):
        """Inicializálja a BLEController-t a megadott beállításokkal.

        Paraméterek:
            settings (dict): A teljes beállítások dict, amelyből a 'ble' kulcs
                             alatt lévő értékeket olvassa ki.
        """
        self.device_name = settings['ble']['device_name']
        self.scan_timeout = settings['ble']['scan_timeout']
        self.connection_timeout = settings['ble']['connection_timeout']
        self.reconnect_interval = settings['ble']['reconnect_interval']
        self.max_retries = settings['ble']['max_retries']
        self.command_timeout = settings['ble']['command_timeout']
        self.service_uuid = settings['ble']['service_uuid']
        self.characteristic_uuid = settings['ble']['characteristic_uuid']
        self.pin_code = settings['ble'].get('pin_code', None)

        self.client = None
        self.device_address = None
        self.is_connected = False
        self.auth_failed = False
        self.retry_count = 0
        self.retry_reset_time = None
        self.last_sent_command = None
        self._state_lock = threading.Lock()

        self.command_queue = queue.Queue(maxsize=1)
        self._queue_lock = threading.Lock()
        self.running = threading.Event()
        self.thread = None
        self.loop = None
        self.ready_event = threading.Event()

    def start(self):
        """Elindítja a BLE háttérszálat.

        Létrehoz egy daemon szálat, amely a _ble_loop metódust futtatja.
        Ha a szál már fut, figyelmeztetést ír ki és visszatér.
        """
        if self.running.is_set():
            logger.warning("BLE thread már fut!")
            return

        self.running.set()
        self.thread = threading.Thread(target=self._ble_loop, daemon=True, name="BLE-Thread")
        self.thread.start()
        logger.info("BLE thread elindítva")

    def _ble_loop(self):
        """A BLE háttérszál fő ciklusa.

        Egy új asyncio event loop-ot hoz létre, elvégzi az inicializálást,
        majd várakozik a command_queue-ból érkező parancsokra, és elküldi
        azokat a BLE eszköznek.
        A szál leállításakor bontja a kapcsolatot és lezárja az event loop-ot.
        """
        try:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)

            logger.info("BLE inicializálás...")
            self.loop.run_until_complete(self._initial_connect())

            self.ready_event.set()

            while self.running.is_set():
                try:
                    try:
                        level = self.command_queue.get(timeout=self.QUEUE_POLL_TIMEOUT)

                        self.loop.run_until_complete(self._send_command_async(level))
                    except queue.Empty:
                        continue
                except Exception as e:
                    logger.error(f"BLE loop hiba: {e}")
                    time.sleep(1)

            logger.info("BLE kapcsolat lezárása...")
            self.loop.run_until_complete(self._disconnect_async())

        except Exception as e:
            logger.error(f"BLE thread kritikus hiba: {e}")
        finally:
            self.ready_event.set()
            if self.loop:
                self.loop.close()
            logger.info("BLE thread leállt")

    async def _initial_connect(self):
        """Kezdeti BLE kapcsolat felépítése indításkor.

        Megpróbál csatlakozni a BLE eszközhöz. Ha nem sikerül,
        figyelmeztető üzenetet ír ki, de a program folytatódik
        (a parancs küldéskor automatikusan újrapróbálkozik).
        """
        success = await self._scan_and_connect_async()
        if not success:
            logger.warning("Nem sikerült csatlakozni a BLE eszközhöz, de folytatjuk. A program automatikusan újrapróbálkozik parancs küldéskor.")

    async def _scan_and_connect_async(self):
        """BLE eszköz keresése és csatlakozás.

        A scan_timeout másodpercig keres BLE eszközöket, majd megkeresi
        a device_name nevűt és megpróbál csatlakozni.

        Visszaad:
            bool: True, ha a csatlakozás sikeres; False egyébként.
        """
        try:
            devices = await BleakScanner.discover(timeout=self.scan_timeout)
            for device in devices:
                if device.name == self.device_name:
                    logger.info(f"Eszköz megtalálva: {device.name} ({device.address})")
                    self.device_address = device.address
                    return await self._connect_async()
            logger.error(f"Nem található: {self.device_name}")
            return False
        except Exception as e:
            logger.error(f"Keresési hiba: {e}")
            return False

    async def _connect_async(self):
        """Csatlakozás a korábban megtalált BLE eszközhöz.

        Ha már van aktív kapcsolat, nem próbál újra csatlakozni.
        Ha pin_code be van állítva, alkalmazás szintű autentikációt végez:
        az AUTH:<pin_code> üzenetet elküldi a GATT karakterisztikára.

        Visszaad:
            bool: True, ha a csatlakozás sikeres; False egyébként.
        """
        if not self.device_address:
            return False
        try:
            if self.client and await self._is_connected_async():
                return True
            self.client = BleakClient(
                self.device_address,
                timeout=self.connection_timeout,
                disconnected_callback=self._on_disconnect
            )
            client = self.client
            await client.connect()
            if self.pin_code is not None:
                logger.info(f"BLE PIN autentikáció folyamatban: {self.device_address}")
                try:
                    auth_event = asyncio.Event()
                    auth_response = [None]

                    def _auth_notify_callback(sender, data):
                        response = data.decode('utf-8', errors='replace').strip()
                        auth_response[0] = response
                        auth_event.set()

                    await client.start_notify(self.characteristic_uuid, _auth_notify_callback)
                    try:
                        auth_message = f"AUTH:{str(self.pin_code)}"
                        await asyncio.wait_for(
                            client.write_gatt_char(
                                self.characteristic_uuid,
                                auth_message.encode('utf-8')
                            ),
                            timeout=self.command_timeout
                        )
                        logger.info(f"BLE PIN elküldve: {self.device_address}")
                        try:
                            await asyncio.wait_for(auth_event.wait(), timeout=self.command_timeout)
                        except asyncio.TimeoutError:
                            logger.warning("BLE AUTH válasz timeout - folytatás autentikáció nélkül")
                        else:
                            response = auth_response[0]
                            if response == "AUTH_OK":
                                logger.info("BLE AUTH sikeres")
                            elif response == "AUTH_FAIL":
                                logger.error("BLE AUTH SIKERTELEN - rossz PIN! Ellenőrizd a settings.json 'pin_code' értékét!")
                                try:
                                    await client.disconnect()
                                except Exception:
                                    pass
                                with self._state_lock:
                                    self.is_connected = False
                                    self.auth_failed = True
                                    self.client = None
                                return False
                            elif response == "AUTH_LOCKED":
                                logger.error("BLE AUTH LOCKOUT - az ESP32 ideiglenesen blokkolva! Ellenőrizd a PIN-t a settings.json-ban!")
                                try:
                                    await client.disconnect()
                                except Exception:
                                    pass
                                with self._state_lock:
                                    self.is_connected = False
                                    self.auth_failed = True
                                    self.client = None
                                return False
                    finally:
                        try:
                            await client.stop_notify(self.characteristic_uuid)
                        except Exception:
                            pass
                except Exception as auth_err:
                    logger.error(f"BLE PIN küldési hiba: {auth_err} → kapcsolat bontása")
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                    with self._state_lock:
                        self.is_connected = False
                        self.client = None
                    return False
            with self._state_lock:
                self.is_connected = True
                self.retry_count = 0
                self.retry_reset_time = None
                self.last_sent_command = None
            logger.info(f"Csatlakozva: {self.device_address}")
            return True
        except Exception as e:
            logger.error(f"Csatlakozási hiba: {e}")
            with self._state_lock:
                self.is_connected = False
                self.client = None
            return False

    async def _is_connected_async(self):
        """Ellenőrzi, hogy a BLE kapcsolat aktív-e.

        Visszaad:
            bool: True, ha a kliens csatlakoztatva van; False egyébként.
        """
        try:
            with self._state_lock:
                client = self.client
            if client:
                return client.is_connected
        except Exception:
            pass
        return False

    def _on_disconnect(self, client):
        """Callback: BLE kapcsolat váratlan megszakadásakor hívódik meg."""
        logger.warning("BLE kapcsolat váratlanul megszakadt")
        with self._state_lock:
            self.is_connected = False
            self.last_sent_command = None
            self.client = None
            self.auth_failed = False

    async def _disconnect_async(self):
        """Bontja a BLE kapcsolatot és felszabadítja a klienst."""
        with self._state_lock:
            client = self.client
        if client:
            try:
                await asyncio.wait_for(client.disconnect(), timeout=self.DISCONNECT_TIMEOUT)
                logger.info("BLE kapcsolat lezárva")
            except asyncio.TimeoutError:
                logger.warning("BLE disconnect timeout")
            except Exception:
                pass
            finally:
                with self._state_lock:
                    self.is_connected = False
                    self.client = None

    async def _send_command_async(self, level):
        """Parancs aszinkron elküldése BLE-n, szükség esetén újracsatlakozással.

        Ha nincs kapcsolat, megpróbál újracsatlakozni (max max_retries kísérlet).
        Ha elérte a max kísérletszámot, 30 másodpercet vár, majd újraindul.
        Azonos level esetén (és van aktív kapcsolat) nem küld ismét.

        Paraméterek:
            level (int): A ventilátor zóna szintje (0–3).

        Visszaad:
            bool: True, ha a parancs elküldése sikeres; False egyébként.
        """
        with self._state_lock:
            last_cmd = self.last_sent_command
            auth_failed = self.auth_failed
        if auth_failed:
            logger.error("BLE parancs elutasítva - AUTH hiba! Javítsd a PIN-t a settings.json-ban és indítsd újra!")
            return False
        if last_cmd == level and await self._is_connected_async():
            return True

        if not await self._is_connected_async():
            with self._state_lock:
                retry_reset_time = self.retry_reset_time
                retry_count = self.retry_count

            if retry_reset_time is not None:
                elapsed = time.time() - retry_reset_time
                if elapsed >= self.RETRY_RESET_SECONDS:
                    logger.info(f"Retry count reset ({elapsed:.0f}s telt el), újrapróbálkozás...")
                    with self._state_lock:
                        self.retry_count = 0
                        self.retry_reset_time = None
                    retry_count = 0
                else:
                    remaining = self.RETRY_RESET_SECONDS - elapsed
                    logger.info(f"Újrapróbálkozás {remaining:.0f}s múlva...")
                    await asyncio.sleep(min(remaining, self.reconnect_interval))
                    return False

            if retry_count < self.max_retries:
                with self._state_lock:
                    self.retry_count += 1
                    retry_count = self.retry_count
                logger.info(f"Újracsatlakozás... ({retry_count}/{self.max_retries})")
                if self.device_address:
                    if await self._connect_async():
                        return await self._send_immediate(level)
                else:
                    if await self._scan_and_connect_async():
                        return await self._send_immediate(level)
                await asyncio.sleep(self.reconnect_interval)
                return False
            else:
                with self._state_lock:
                    if self.retry_reset_time is None:
                        self.retry_reset_time = time.time()
                        logger.warning(f"Max újracsatlakozási kísérletek elérve ({self.max_retries})! {self.RETRY_RESET_SECONDS}s múlva újrapróbálkozik...")
                return False

        return await self._send_immediate(level)

    async def _send_immediate(self, level):
        """Azonnal elküldi a parancsot a BLE GATT karakterisztikára.

        A parancs formátuma: "LEVEL:<n>" (pl. "LEVEL:2").
        Timeout esetén leállítja a kapcsolatot.

        Paraméterek:
            level (int): A ventilátor zóna szintje (0–3).

        Visszaad:
            bool: True, ha a küldés sikeres; False egyébként.
        """
        with self._state_lock:
            client = self.client
        if not client or not await self._is_connected_async():
            with self._state_lock:
                self.is_connected = False
            return False
        try:
            message = f"LEVEL:{level}"
            await asyncio.wait_for(
                client.write_gatt_char(
                    self.characteristic_uuid,
                    message.encode('utf-8')
                ),
                timeout=self.command_timeout
            )
            with self._state_lock:
                self.last_sent_command = level
            logger.info(f"Parancs elküldve: {message}")
            return True
        except asyncio.TimeoutError:
            logger.error(f"Parancs küldés timeout ({self.command_timeout}s)")
            with self._state_lock:
                self.is_connected = False
            return False
        except Exception as e:
            logger.error(f"Küldési hiba: {e}")
            with self._state_lock:
                self.is_connected = False
            return False

    def send_command_sync(self, level):
        """Ventilátor szint parancs szinkron küldése a BLE szálnak.

        A parancsot a command_queue-ba teszi, amelyből a BLE háttérszál
        veszi ki és küldi el. A sor mérete 1; ha teli van, a régi parancsot
        elveti és az újat teszi be.

        Paraméterek:
            level (int): A ventilátor zóna szintje (0–3). Más érték esetén
                         figyelmeztetést ír ki és visszatér.
        """
        if isinstance(level, bool) or not isinstance(level, int) or level < 0 or level > 3:
            logger.warning(f"Érvénytelen parancs szint: {level} (egész számnak kell lennie, 0-3 között)")
            return
        if not self.running.is_set():
            logger.warning("BLE thread nem fut, parancs elvetve")
            return
        with self._queue_lock:
            try:
                self.command_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self.command_queue.put_nowait(level)
            except queue.Full:
                logger.warning(f"Queue hiba, parancs elvetve: LEVEL:{level}")

    def stop(self):
        """Leállítja a BLE háttérszálat.

        Jelzi a szálnak a leállást (running=False), majd megvárja
        legfeljebb 5 másodpercig a szál befejezését.
        """
        if not self.running.is_set():
            return
        logger.info("BLE thread leállítása...")
        self.running.clear()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=self.STOP_JOIN_TIMEOUT)
            if self.thread.is_alive():
                logger.warning("BLE thread nem állt le időben")
            else:
                logger.info("BLE thread leállítva")


# ============================================================
# PowerZoneController
# ============================================================
class PowerZoneController:
    """A fő vezérlő logika: teljesítmény zónák kiszámítása, cooldown és dropout kezelés.

    A beállítások alapján (settings.json) kiszámítja a teljesítmény zóna határokat
    (Z0–Z3), és az érkező power/HR adatok alapján dönt a ventilátor szintjéről.

    Zóna rendszer:
        Z0: 0W (leállás vagy dropout)
        Z1: alacsony teljesítmény  (1W – FTP×z1_max_percent%)
        Z2: közepes teljesítmény   (Z1_max+1W – FTP×z2_max_percent%)
        Z3: magas teljesítmény     (Z2_max+1W – max_watt)

    Cooldown mechanizmus:
        Zóna csökkentésekor a rendszer nem vált azonnal, hanem cooldown_seconds
        másodpercig vár. Ez megakadályozza a rövid teljesítmény-visszaesések
        miatti felesleges zóna-váltásokat (pl. hegyi szakasz utáni pihenő).
        Zóna növelésekor nincs cooldown – azonnal reagál.

    Buffer/átlagolás:
        Az adatokat egy deque pufferbe gyűjti (buffer_seconds × 4 mintahely).
        A zónadöntés az átlagos teljesítmény alapján történik, nem az azonnali
        értékek alapján. Legalább minimum_samples minta kell a döntéshez.

    Dropout detektálás:
        Ha dropout_timeout másodpercig nem érkezik adat, a ventilátor azonnal
        Z0-ra (ki) kapcsol, megelőzve, hogy az utolsó zónán maradjon.

    Attribútumok:
        ftp (int): Funkcionális küszöbteljesítmény wattban.
        zones (dict): A kiszámított zóna határok {0: (min, max), ...} formátumban.
        current_zone (int|None): Aktuálisan aktív zóna (None = még nincs döntés).
        cooldown_active (bool): True, ha a cooldown timer fut.
        ble (BLEController): A BLE kommunikációs réteg.
    """

    BUFFER_RATE_HZ = 4
    COOLDOWN_PRINT_INTERVAL = 10
    PRINT_THROTTLE_SECONDS = 1.0

    def __init__(self, settings_file="settings.json"):
        """Inicializálja a PowerZoneController-t.

        Betölti és validálja a beállításokat, kiszámítja a zóna határokat,
        inicializálja a puffereket, és létrehozza a BLEController példányt.

        Paraméterek:
            settings_file (str): A JSON beállítások fájl elérési útja.
                                 Alapértelmezett: "settings.json"
        """
        self.settings = self.load_and_validate_settings(settings_file)

        self.ftp = self.settings['ftp']
        self.min_watt = self.settings['min_watt']
        self.max_watt = self.settings['max_watt']
        self.cooldown_seconds = self.settings['cooldown_seconds']
        self.buffer_seconds = self.settings['buffer_seconds']
        self.minimum_samples = self.settings['minimum_samples']
        self.dropout_timeout = self.settings['dropout_timeout']
        self.zero_power_immediate = self.settings['zero_power_immediate']
        self.zone_thresholds = self.settings['zone_thresholds']
        self.hr_zone_settings = self.settings.get('heart_rate_zones', copy.deepcopy(DEFAULT_SETTINGS['heart_rate_zones']))

        ds = self.settings.get('data_source', {})
        uses_zwift_udp = (ds.get('power_source') == 'zwift_udp' or
                          ds.get('hr_source') == 'zwift_udp')
        if uses_zwift_udp:
            self.buffer_seconds = ds.get('zwift_udp_buffer_seconds', 10)
            self.minimum_samples = ds.get('zwift_udp_minimum_samples', 2)
            self.dropout_timeout = ds.get('zwift_udp_dropout_timeout', 15)

        self.zones = self.calculate_zones()

        self.current_zone = None
        self.last_zone_change = time.time()
        self.cooldown_active = False
        self.cooldown_start_time = 0
        self.pending_zone = None
        self.can_halve = True
        self.can_double = False

        self.last_data_time = time.time()

        buffer_size = int(self.buffer_seconds * self.BUFFER_RATE_HZ)
        self.power_buffer = deque(maxlen=buffer_size)

        self.state_lock = threading.Lock()
        self.last_cooldown_print = 0

        self.current_heart_rate = None
        self.current_hr_zone = None
        self.current_power_zone = None
        self.current_avg_power = None
        hr_buffer_size = int(self.buffer_seconds * self.BUFFER_RATE_HZ)
        self.hr_buffer = deque(maxlen=hr_buffer_size)
        self.last_hr_print_time = 0
        self.last_power_print_time = 0
        self.last_hr_zone_print_time = 0
        self.last_hr_data_time = None

        self.ble = BLEController(self.settings)

        self.running = threading.Event()
        self.dropout_thread = None

        print(f"FTP: {self.ftp}W")
        print(f"Érvényes watt tartomány: 0W - {self.max_watt}W")
        print(f"Zóna határok: {self.zones}")
        print(f"Buffer méret: {buffer_size} adat ({self.buffer_seconds}s)")
        print(f"Minimum minták: {self.minimum_samples}")
        print(f"Dropout timeout: {self.dropout_timeout}s")
        print(f"Cooldown: {self.cooldown_seconds}s")
        print(f"0W azonnali: {'Igen' if self.zero_power_immediate else 'Nem'}")
        print(f"BLE eszköz: {self.settings['ble']['device_name']}")
        pin_code = self.settings['ble'].get('pin_code', None)
        if pin_code is not None:
            print(f"BLE PIN: {'*' * len(str(pin_code))}")
        print(f"Power forrás: {self.settings['data_source']['power_source']}")
        print(f"HR forrás: {self.settings['data_source']['hr_source']}")
        if uses_zwift_udp:
            print(f"🔄 Zwift UDP mód: buffer={self.buffer_seconds}s, min_samples={self.minimum_samples}, dropout={self.dropout_timeout}s")
        if self.hr_zone_settings.get('enabled', False):
            hr_z = self.hr_zones
            print(f"HR zóna mód: {self.hr_zone_settings.get('zone_mode', 'power_only')}")
            print(f"HR zóna határok: Z0 < {self.hr_zone_settings['resting_hr']} bpm, Z1 < {hr_z['z1_max']} bpm, Z2 < {hr_z['z2_max']} bpm")

    def start_dropout_checker(self):
        """Elindítja a dropout ellenőrző háttérszálat.

        Másodpercenként meghívja a check_dropout metódust, hogy detektálja
        az adatforrás kiesését és szükség esetén Z0-ra kapcsoljon.
        """
        self.running.set()
        self.dropout_thread = threading.Thread(
            target=self._dropout_check_loop,
            daemon=True,
            name="Dropout-Thread"
        )
        self.dropout_thread.start()
        logger.info("Dropout ellenőrző thread elindítva")

    def _dropout_check_loop(self):
        """A dropout ellenőrző szál ciklusa – másodpercenként fut."""
        while self.running.is_set():
            self.check_dropout()
            time.sleep(1)

    def stop_dropout_checker(self):
        """Leállítja a dropout ellenőrző háttérszálat."""
        self.running.clear()
        if self.dropout_thread and self.dropout_thread.is_alive():
            self.dropout_thread.join(timeout=3)
            logger.info("Dropout ellenőrző thread leállítva")

    def load_and_validate_settings(self, settings_file):
        """Betölti és validálja a JSON beállítási fájlt.

        Az alapértelmezett értékekből (DEFAULT_SETTINGS) indul ki, majd
        felülírja az érvényes, fájlból betöltött értékekkel. Minden mezőre
        ellenőrzi a típust és az érvényes tartományt. Hibás érték esetén
        figyelmeztetést ír ki és az alapértelmezett értéket tartja meg.

        Ha a fájl nem létezik, automatikusan létrehozza az alapértelmezettekkel.

        Paraméterek:
            settings_file (str): A JSON beállítások fájl elérési útja.

        Visszaad:
            dict: A validált beállítások dict-je.
        """
        settings = copy.deepcopy(DEFAULT_SETTINGS)

        try:
            with open(settings_file, 'r', encoding='utf-8') as f:
                loaded_settings = json.load(f)
        except FileNotFoundError:
            print(f"⚠ FIGYELMEZTETÉS: '{settings_file}' nem található! Alapértelmezett beállítások használata.")
            self.save_default_settings(settings_file)
            return settings
        except json.JSONDecodeError as e:
            print(f"⚠ FIGYELMEZTETÉS: '{settings_file}' hibás JSON formátum! ({e})")
            return settings
        except Exception as e:
            print(f"⚠ FIGYELMEZTETÉS: Hiba a beállítások betöltésekor! ({e})")
            return settings

        validation_failed = False

        if 'ftp' in loaded_settings:
            if isinstance(loaded_settings['ftp'], (int, float)) and not isinstance(loaded_settings['ftp'], bool) and 100 <= loaded_settings['ftp'] <= 500:
                settings['ftp'] = int(loaded_settings['ftp'])
            else:
                print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'ftp' érték: {loaded_settings['ftp']} (100-500 között kell lennie)")
                validation_failed = True

        if 'min_watt' in loaded_settings:
            if isinstance(loaded_settings['min_watt'], (int, float)) and not isinstance(loaded_settings['min_watt'], bool) and loaded_settings['min_watt'] >= 0:
                settings['min_watt'] = int(loaded_settings['min_watt'])
            else:
                print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'min_watt' érték: {loaded_settings['min_watt']} (0 vagy nagyobb kell legyen)")
                validation_failed = True

        if 'max_watt' in loaded_settings:
            if isinstance(loaded_settings['max_watt'], (int, float)) and not isinstance(loaded_settings['max_watt'], bool) and loaded_settings['max_watt'] > 0:
                settings['max_watt'] = int(loaded_settings['max_watt'])
            else:
                print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'max_watt' érték: {loaded_settings['max_watt']} (0-nál nagyobb kell legyen)")
                validation_failed = True

        if 'cooldown_seconds' in loaded_settings:
            if isinstance(loaded_settings['cooldown_seconds'], (int, float)) and not isinstance(loaded_settings['cooldown_seconds'], bool) and 0 <= loaded_settings['cooldown_seconds'] <= 300:
                settings['cooldown_seconds'] = int(loaded_settings['cooldown_seconds'])
            else:
                print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'cooldown_seconds' érték: {loaded_settings['cooldown_seconds']} (0-300 között kell lennie)")
                validation_failed = True

        if 'buffer_seconds' in loaded_settings:
            if isinstance(loaded_settings['buffer_seconds'], (int, float)) and not isinstance(loaded_settings['buffer_seconds'], bool) and 1 <= loaded_settings['buffer_seconds'] <= 10:
                settings['buffer_seconds'] = int(loaded_settings['buffer_seconds'])
            else:
                print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'buffer_seconds' érték: {loaded_settings['buffer_seconds']} (1-10 között kell lennie)")
                validation_failed = True

        if 'minimum_samples' in loaded_settings:
            if isinstance(loaded_settings['minimum_samples'], (int, float)) and not isinstance(loaded_settings['minimum_samples'], bool) and loaded_settings['minimum_samples'] > 0:
                settings['minimum_samples'] = int(loaded_settings['minimum_samples'])
            else:
                print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'minimum_samples' érték: {loaded_settings['minimum_samples']} (0-nál nagyobb kell legyen)")
                validation_failed = True

        if 'dropout_timeout' in loaded_settings:
            if isinstance(loaded_settings['dropout_timeout'], (int, float)) and not isinstance(loaded_settings['dropout_timeout'], bool) and loaded_settings['dropout_timeout'] > 0:
                settings['dropout_timeout'] = int(loaded_settings['dropout_timeout'])
            else:
                print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'dropout_timeout' érték: {loaded_settings['dropout_timeout']} (0-nál nagyobb kell legyen)")
                validation_failed = True

        if 'zero_power_immediate' in loaded_settings:
            if isinstance(loaded_settings['zero_power_immediate'], bool):
                settings['zero_power_immediate'] = loaded_settings['zero_power_immediate']
            else:
                print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'zero_power_immediate' érték: {loaded_settings['zero_power_immediate']} (true vagy false kell legyen)")
                validation_failed = True

        if 'zone_thresholds' in loaded_settings:
            if isinstance(loaded_settings['zone_thresholds'], dict):
                z_thresholds = loaded_settings['zone_thresholds']
                if 'z1_max_percent' in z_thresholds:
                    if isinstance(z_thresholds['z1_max_percent'], (int, float)) and 1 <= z_thresholds['z1_max_percent'] <= 100:
                        settings['zone_thresholds']['z1_max_percent'] = int(z_thresholds['z1_max_percent'])
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'z1_max_percent' érték: {z_thresholds['z1_max_percent']} (1-100 között kell lennie)")
                        validation_failed = True
                if 'z2_max_percent' in z_thresholds:
                    if isinstance(z_thresholds['z2_max_percent'], (int, float)) and 1 <= z_thresholds['z2_max_percent'] <= 100:
                        settings['zone_thresholds']['z2_max_percent'] = int(z_thresholds['z2_max_percent'])
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'z2_max_percent' érték: {z_thresholds['z2_max_percent']} (1-100 között kell lennie)")
                        validation_failed = True
                if settings['zone_thresholds']['z1_max_percent'] >= settings['zone_thresholds']['z2_max_percent']:
                    print(f"⚠ FIGYELMEZTETÉS: z1_max_percent >= z2_max_percent! Alapértelmezett zóna határok használata.")
                    settings['zone_thresholds'] = copy.deepcopy(DEFAULT_SETTINGS['zone_thresholds'])
                    validation_failed = True
            else:
                print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'zone_thresholds' formátum")
                validation_failed = True

        if 'ble' in loaded_settings:
            if isinstance(loaded_settings['ble'], dict):
                ble_settings = loaded_settings['ble']
                
                if 'device_name' in ble_settings:
                    if isinstance(ble_settings['device_name'], str) and len(ble_settings['device_name']) > 0:
                        settings['ble']['device_name'] = ble_settings['device_name']
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'device_name' érték")
                        validation_failed = True
                if 'scan_timeout' in ble_settings:
                    if isinstance(ble_settings['scan_timeout'], (int, float)) and 1 <= ble_settings['scan_timeout'] <= 60:
                        settings['ble']['scan_timeout'] = int(ble_settings['scan_timeout'])
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'scan_timeout' érték: {ble_settings['scan_timeout']} (1-60 között kell lennie)")
                        validation_failed = True
                if 'connection_timeout' in ble_settings:
                    if isinstance(ble_settings['connection_timeout'], (int, float)) and 1 <= ble_settings['connection_timeout'] <= 60:
                        settings['ble']['connection_timeout'] = int(ble_settings['connection_timeout'])
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'connection_timeout' érték: {ble_settings['connection_timeout']} (1-60 között kell lennie)")
                        validation_failed = True
                if 'reconnect_interval' in ble_settings:
                    if isinstance(ble_settings['reconnect_interval'], (int, float)) and 1 <= ble_settings['reconnect_interval'] <= 60:
                        settings['ble']['reconnect_interval'] = int(ble_settings['reconnect_interval'])
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'reconnect_interval' érték: {ble_settings['reconnect_interval']} (1-60 között kell lennie)")
                        validation_failed = True
                if 'max_retries' in ble_settings:
                    if isinstance(ble_settings['max_retries'], (int, float)) and 1 <= ble_settings['max_retries'] <= 100:
                        settings['ble']['max_retries'] = int(ble_settings['max_retries'])
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'max_retries' érték: {ble_settings['max_retries']} (1-100 között kell lennie)")
                        validation_failed = True
                if 'command_timeout' in ble_settings:
                    if isinstance(ble_settings['command_timeout'], (int, float)) and 1 <= ble_settings['command_timeout'] <= 30:
                        settings['ble']['command_timeout'] = int(ble_settings['command_timeout'])
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'command_timeout' érték: {ble_settings['command_timeout']} (1-30 között kell lennie)")
                        validation_failed = True
                if 'service_uuid' in ble_settings:
                    if isinstance(ble_settings['service_uuid'], str) and len(ble_settings['service_uuid']) > 0:
                        settings['ble']['service_uuid'] = ble_settings['service_uuid']
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'service_uuid' érték")
                        validation_failed = True
                if 'characteristic_uuid' in ble_settings:
                    if isinstance(ble_settings['characteristic_uuid'], str) and len(ble_settings['characteristic_uuid']) > 0:
                        settings['ble']['characteristic_uuid'] = ble_settings['characteristic_uuid']
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'characteristic_uuid' érték")
                        validation_failed = True
                if 'pin_code' in ble_settings:
                    if ble_settings['pin_code'] is None:
                        settings['ble']['pin_code'] = None
                    elif isinstance(ble_settings['pin_code'], int) and not isinstance(ble_settings['pin_code'], bool) and 0 <= ble_settings['pin_code'] <= 999999:
                        settings['ble']['pin_code'] = str(ble_settings['pin_code'])
                    elif isinstance(ble_settings['pin_code'], str) and len(ble_settings['pin_code']) > 0 and len(ble_settings['pin_code']) <= 20 and ble_settings['pin_code'].isdigit():
                        settings['ble']['pin_code'] = ble_settings['pin_code']
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'pin_code' érték: {ble_settings['pin_code']} (0-999999 közötti egész szám, számjegyekből álló szöveg, vagy null kell legyen)")
                        validation_failed = True
            else:
                print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'ble' formátum")
                validation_failed = True

        if 'data_source' in loaded_settings:
            if isinstance(loaded_settings['data_source'], dict):
                ds = loaded_settings['data_source']

                if 'power_source' in ds:
                    if ds['power_source'] in ('antplus', 'ble', 'zwift_udp'):
                        settings['data_source']['power_source'] = ds['power_source']
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'power_source' érték: {ds['power_source']} ('antplus', 'ble' vagy 'zwift_udp' kell legyen)")
                        validation_failed = True

                if 'hr_source' in ds:
                    if ds['hr_source'] in ('antplus', 'ble', 'zwift_udp'):
                        settings['data_source']['hr_source'] = ds['hr_source']
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'hr_source' érték: {ds['hr_source']} ('antplus', 'ble' vagy 'zwift_udp' kell legyen)")
                        validation_failed = True

                if 'ble_power_device_name' in ds:
                    if ds['ble_power_device_name'] is None or (isinstance(ds['ble_power_device_name'], str) and len(ds['ble_power_device_name']) > 0):
                        settings['data_source']['ble_power_device_name'] = ds['ble_power_device_name']
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'ble_power_device_name' érték")
                        validation_failed = True

                if 'ble_hr_device_name' in ds:
                    if ds['ble_hr_device_name'] is None or (isinstance(ds['ble_hr_device_name'], str) and len(ds['ble_hr_device_name']) > 0):
                        settings['data_source']['ble_hr_device_name'] = ds['ble_hr_device_name']
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'ble_hr_device_name' érték")
                        validation_failed = True

                for field in [
                    'ble_power_scan_timeout',
                    'ble_power_reconnect_interval',
                    'ble_power_max_retries',
                    'ble_hr_scan_timeout',
                    'ble_hr_reconnect_interval',
                    'ble_hr_max_retries',
                ]:
                    if field in ds:
                        max_val = 100 if 'max_retries' in field else 60
                        if isinstance(ds[field], int) and not isinstance(ds[field], bool) and 1 <= ds[field] <= max_val:
                            settings['data_source'][field] = ds[field]
                        else:
                            print(f"⚠ FIGYELMEZTETÉS: Érvénytelen '{field}' érték: {ds[field]} (1-{max_val} között kell lennie)")
                            validation_failed = True

                if 'zwift_udp_buffer_seconds' in ds:
                    if isinstance(ds['zwift_udp_buffer_seconds'], int) and not isinstance(ds['zwift_udp_buffer_seconds'], bool) and 1 <= ds['zwift_udp_buffer_seconds'] <= 60:
                        settings['data_source']['zwift_udp_buffer_seconds'] = ds['zwift_udp_buffer_seconds']
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'zwift_udp_buffer_seconds' érték: {ds['zwift_udp_buffer_seconds']} (1-60 közötti egész szám kell legyen)")
                        validation_failed = True

                if 'zwift_udp_minimum_samples' in ds:
                    if isinstance(ds['zwift_udp_minimum_samples'], int) and not isinstance(ds['zwift_udp_minimum_samples'], bool) and 1 <= ds['zwift_udp_minimum_samples'] <= 20:
                        settings['data_source']['zwift_udp_minimum_samples'] = ds['zwift_udp_minimum_samples']
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'zwift_udp_minimum_samples' érték: {ds['zwift_udp_minimum_samples']} (1-20 közötti egész szám kell legyen)")
                        validation_failed = True

                if 'zwift_udp_dropout_timeout' in ds:
                    if isinstance(ds['zwift_udp_dropout_timeout'], int) and not isinstance(ds['zwift_udp_dropout_timeout'], bool) and 1 <= ds['zwift_udp_dropout_timeout'] <= 120:
                        settings['data_source']['zwift_udp_dropout_timeout'] = ds['zwift_udp_dropout_timeout']
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'zwift_udp_dropout_timeout' érték: {ds['zwift_udp_dropout_timeout']} (1-120 közötti egész szám kell legyen)")
                        validation_failed = True

                zwift_buf_size = settings['data_source']['zwift_udp_buffer_seconds'] * self.BUFFER_RATE_HZ
                if settings['data_source']['zwift_udp_minimum_samples'] > zwift_buf_size:
                    print(f"⚠ FIGYELMEZTETÉS: 'zwift_udp_minimum_samples' ({settings['data_source']['zwift_udp_minimum_samples']}) nagyobb mint Zwift UDP buffer méret ({zwift_buf_size})!")
                    settings['data_source']['zwift_udp_minimum_samples'] = zwift_buf_size
                    validation_failed = True

                ds_known_keys = {
                    'power_source', 'hr_source',
                    'ble_power_device_name', 'ble_power_scan_timeout',
                    'ble_power_reconnect_interval', 'ble_power_max_retries',
                    'ble_hr_device_name', 'ble_hr_scan_timeout',
                    'ble_hr_reconnect_interval', 'ble_hr_max_retries',
                    'zwift_udp_port', 'zwift_udp_host',
                    'zwift_udp_buffer_seconds', 'zwift_udp_minimum_samples', 'zwift_udp_dropout_timeout',
                }
                ds_unknown = set(ds.keys()) - ds_known_keys
                if ds_unknown:
                    print(f"⚠ FIGYELMEZTETÉS: Ismeretlen data_source mező(k): {', '.join(ds_unknown)}")

                # Figyelmeztetés ha BLE forrás van beállítva de nincs eszköznév
                if settings['data_source']['power_source'] == 'ble' and not settings['data_source'].get('ble_power_device_name'):
                    print(f"⚠ FIGYELMEZTETÉS: power_source='ble' de 'ble_power_device_name' nincs megadva!")
                if settings['data_source']['hr_source'] == 'ble' and not settings['data_source'].get('ble_hr_device_name'):
                    print(f"⚠ FIGYELMEZTETÉS: hr_source='ble' de 'ble_hr_device_name' nincs megadva!")

                # Zwift UDP port validáció
                if 'zwift_udp_port' in ds:
                    if isinstance(ds['zwift_udp_port'], int) and not isinstance(ds['zwift_udp_port'], bool) and 1024 <= ds['zwift_udp_port'] <= 65535:
                        settings['data_source']['zwift_udp_port'] = ds['zwift_udp_port']
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'zwift_udp_port' érték: {ds['zwift_udp_port']} (1024-65535 közötti egész szám kell legyen)")
                        validation_failed = True

                # Zwift UDP host validáció
                if 'zwift_udp_host' in ds:
                    if isinstance(ds['zwift_udp_host'], str) and len(ds['zwift_udp_host']) > 0:
                        settings['data_source']['zwift_udp_host'] = ds['zwift_udp_host']
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'zwift_udp_host' érték: {ds['zwift_udp_host']} (nem üres szöveg kell legyen)")
                        validation_failed = True
            else:
                print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'data_source' formátum")
                validation_failed = True

        if 'heart_rate_zones' in loaded_settings:
            if isinstance(loaded_settings['heart_rate_zones'], dict):
                hrz = loaded_settings['heart_rate_zones']
                if 'enabled' in hrz:
                    if isinstance(hrz['enabled'], bool):
                        settings['heart_rate_zones']['enabled'] = hrz['enabled']
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'heart_rate_zones.enabled' érték (true vagy false kell legyen)")
                        validation_failed = True
                if 'max_hr' in hrz:
                    if isinstance(hrz['max_hr'], int) and not isinstance(hrz['max_hr'], bool) and 100 <= hrz['max_hr'] <= 220:
                        settings['heart_rate_zones']['max_hr'] = hrz['max_hr']
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'max_hr' érték: {hrz['max_hr']} (100-220 közötti egész szám kell legyen)")
                        validation_failed = True
                if 'resting_hr' in hrz:
                    if isinstance(hrz['resting_hr'], int) and not isinstance(hrz['resting_hr'], bool) and 30 <= hrz['resting_hr'] <= 100:
                        settings['heart_rate_zones']['resting_hr'] = hrz['resting_hr']
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'resting_hr' érték: {hrz['resting_hr']} (30-100 közötti egész szám kell legyen)")
                        validation_failed = True
                if 'zone_mode' in hrz:
                    if hrz['zone_mode'] in ('hr_only', 'higher_wins', 'power_only'):
                        settings['heart_rate_zones']['zone_mode'] = hrz['zone_mode']
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'zone_mode' érték: {hrz['zone_mode']} ('hr_only', 'higher_wins' vagy 'power_only' kell legyen)")
                        validation_failed = True
                if 'z1_max_percent' in hrz:
                    if isinstance(hrz['z1_max_percent'], int) and not isinstance(hrz['z1_max_percent'], bool) and 1 <= hrz['z1_max_percent'] <= 100:
                        settings['heart_rate_zones']['z1_max_percent'] = hrz['z1_max_percent']
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'heart_rate_zones.z1_max_percent' érték: {hrz['z1_max_percent']} (1-100 között kell lennie)")
                        validation_failed = True
                if 'z2_max_percent' in hrz:
                    if isinstance(hrz['z2_max_percent'], int) and not isinstance(hrz['z2_max_percent'], bool) and 1 <= hrz['z2_max_percent'] <= 100:
                        settings['heart_rate_zones']['z2_max_percent'] = hrz['z2_max_percent']
                    else:
                        print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'heart_rate_zones.z2_max_percent' érték: {hrz['z2_max_percent']} (1-100 között kell lennie)")
                        validation_failed = True
                if settings['heart_rate_zones']['z1_max_percent'] >= settings['heart_rate_zones']['z2_max_percent']:
                    print(f"⚠ FIGYELMEZTETÉS: HR z1_max_percent >= z2_max_percent! Alapértelmezett HR zóna határok használata.")
                    settings['heart_rate_zones']['z1_max_percent'] = DEFAULT_SETTINGS['heart_rate_zones']['z1_max_percent']
                    settings['heart_rate_zones']['z2_max_percent'] = DEFAULT_SETTINGS['heart_rate_zones']['z2_max_percent']
                    validation_failed = True
                max_hr = settings['heart_rate_zones']['max_hr']
                resting_hr = settings['heart_rate_zones']['resting_hr']
                z1_max = max_hr * settings['heart_rate_zones']['z1_max_percent'] / 100
                if resting_hr >= z1_max:
                    print(f"⚠ FIGYELMEZTETÉS: 'resting_hr' ({resting_hr}) >= z1_max ({z1_max:.0f})! Alapértelmezett HR zóna határok használata.")
                    settings['heart_rate_zones']['resting_hr'] = DEFAULT_SETTINGS['heart_rate_zones']['resting_hr']
                    settings['heart_rate_zones']['z1_max_percent'] = DEFAULT_SETTINGS['heart_rate_zones']['z1_max_percent']
                    settings['heart_rate_zones']['z2_max_percent'] = DEFAULT_SETTINGS['heart_rate_zones']['z2_max_percent']
                    validation_failed = True
            else:
                print(f"⚠ FIGYELMEZTETÉS: Érvénytelen 'heart_rate_zones' formátum")
                validation_failed = True

        if settings['min_watt'] >= settings['max_watt']:
            print(f"⚠ FIGYELMEZTETÉS: 'min_watt' >= 'max_watt'! Alapértelmezett értékek használata.")
            settings['min_watt'] = DEFAULT_SETTINGS['min_watt']
            settings['max_watt'] = DEFAULT_SETTINGS['max_watt']
            validation_failed = True

        buffer_size = settings['buffer_seconds'] * self.BUFFER_RATE_HZ
        if settings['minimum_samples'] > buffer_size:
            print(f"⚠ FIGYELMEZTETÉS: 'minimum_samples' ({settings['minimum_samples']}) nagyobb mint buffer méret ({buffer_size})!")
            settings['minimum_samples'] = buffer_size
            validation_failed = True

        known_keys = {'ftp', 'min_watt', 'max_watt', 'cooldown_seconds', 'buffer_seconds',
                      'minimum_samples', 'dropout_timeout', 'zero_power_immediate',
                      'zone_thresholds', 'ble', 'data_source',
                      'heart_rate_zones'}
        unknown_keys = set(loaded_settings.keys()) - known_keys
        if unknown_keys:
            print(f"⚠ FIGYELMEZTETÉS: Ismeretlen mező(k): {', '.join(unknown_keys)}")

        if validation_failed:
            print("\n⚠ HIBÁS BEÁLLÍTÁSOK! Érvényes értékek használata.")

        return settings

    def save_default_settings(self, settings_file):
        """Létrehozza a settings.json fájlt az alapértelmezett beállításokkal.

        Paraméterek:
            settings_file (str): A létrehozandó fájl elérési útja.
        """
        try:
            with open(settings_file, 'w', encoding='utf-8') as f:
                json.dump(copy.deepcopy(DEFAULT_SETTINGS), f, indent=2, ensure_ascii=False)
            print(f"✓ Alapértelmezett '{settings_file}' létrehozva.")
            print(f"  Szerkeszd a fájlt a beállítások módosításához: {os.path.abspath(settings_file)}")
        except PermissionError:
            print(f"✗ Nincs írási jogosultság a '{settings_file}' fájlhoz!")
            print(f"  Hozd létre manuálisan: {os.path.abspath(settings_file)}")
        except Exception as e:
            print(f"✗ Nem sikerült létrehozni a '{settings_file}' fájlt: {e}")

    def calculate_zones(self):
        """Kiszámítja a teljesítmény zóna határokat az FTP és a százalékos küszöbök alapján.

        A határokat az FTP százalékában számítja:
            Z1 max = FTP × z1_max_percent / 100
            Z2 max = FTP × z2_max_percent / 100  (max_watt-nál nem lehet több)

        Ha a kiszámított értékek meghaladják a max_watt-ot vagy egymást átfedik,
        figyelmeztetést ír ki és levágja az értékeket.

        Visszaad:
            dict: {0: (0, 0), 1: (1, z1_max), 2: (z1_max+1, z2_max), 3: (z2_max+1, max_watt)}
        """
        z1_max = int(self.ftp * self.zone_thresholds['z1_max_percent'] / 100)
        z2_max = int(self.ftp * self.zone_thresholds['z2_max_percent'] / 100)

        z2_max = min(z2_max, self.max_watt)
        z1_max = min(z1_max, z2_max - 1)

        z1_max_orig = int(self.ftp * self.zone_thresholds['z1_max_percent'] / 100)
        z2_max_orig = int(self.ftp * self.zone_thresholds['z2_max_percent'] / 100)
        if z2_max_orig > self.max_watt:
            print(f"⚠ FIGYELMEZTETÉS: z2_max ({z2_max_orig}W) > max_watt ({self.max_watt}W), határolva!")
        if z1_max_orig > z2_max - 1:
            print(f"⚠ FIGYELMEZTETÉS: z1_max ({z1_max_orig}W) határolva z2_max-hoz ({z2_max}W)!")

        return {
            0: (0, 0),
            1: (1, z1_max),
            2: (z1_max + 1, z2_max),
            3: (z2_max + 1, self.max_watt)
        }

    @property
    def hr_zones(self):
        """Kiszámítja a HR zóna határokat bpm-ben.

        Visszaad:
            dict: {'resting_hr': int, 'z1_max': int, 'z2_max': int}
        """
        max_hr = self.hr_zone_settings['max_hr']
        z1_max = int(max_hr * self.hr_zone_settings['z1_max_percent'] / 100)
        z2_max = int(max_hr * self.hr_zone_settings['z2_max_percent'] / 100)
        return {
            'resting_hr': self.hr_zone_settings['resting_hr'],
            'z1_max': z1_max,
            'z2_max': z2_max,
        }

    def get_hr_zone(self, hr):
        """Meghatározza a HR zónát (0–3) a megadott szívfrekvencia alapján.

        Zóna 0: 0 bpm vagy pihenő HR alatt
        Zóna 1: pihenő HR – Z1 határ
        Zóna 2: Z1 határ – Z2 határ
        Zóna 3: Z2 határ felett

        Paraméterek:
            hr (int): A szívfrekvencia bpm-ben.

        Visszaad:
            int: A zóna szintje (0–3).
        """
        if hr == 0 or hr < self.hr_zone_settings['resting_hr']:
            return 0
        max_hr = self.hr_zone_settings['max_hr']
        z1_boundary = max_hr * self.hr_zone_settings['z1_max_percent'] / 100
        z2_boundary = max_hr * self.hr_zone_settings['z2_max_percent'] / 100
        if hr < z1_boundary:
            return 1
        if hr < z2_boundary:
            return 2
        return 3

    def is_valid_power(self, power):
        """Ellenőrzi, hogy az érték érvényes teljesítmény adat-e.

        Paraméterek:
            power: Az ellenőrizendő érték.

        Visszaad:
            bool: True, ha szám, nem bool, nem NaN/Inf, nem negatív, és nem haladja meg a max_watt-ot.
        """
        try:
            if not isinstance(power, (int, float)):
                return False
            if isinstance(power, bool):
                return False
            if math.isnan(power) or math.isinf(power):
                return False
            if power < 0:
                return False
            if 0 < power < self.min_watt:
                return False
            if power > self.max_watt:
                return False
            return True
        except Exception:
            return False

    def get_zone_for_power(self, power):
        """Meghatározza a teljesítmény zónát (0–3) a megadott wattérték alapján.

        Paraméterek:
            power (int|float): A teljesítmény wattban.

        Visszaad:
            int: A zóna szintje (0–3). Ha egyik határon sem belül, Z3-at ad vissza.
        """
        if power == 0:
            return 0
        for zone, (min_p, max_p) in self.zones.items():
            if min_p <= power <= max_p:
                return zone
        return 3

    def check_dropout(self):
        """Adatforrás kiesés detektálása és Z0-ra kapcsolás.

        Ha a legutóbbi adat óta eltelt idő eléri a dropout_timeout-ot,
        és az aktuális zóna nem 0, akkor Z0-ra vált és elküldi a BLE parancsot.
        Ez megakadályozza, hogy az utolsó zónán maradjon végtelen ideig.
        Másodpercenként hívja a _dropout_check_loop.
        """
        current_time = time.time()
        send_needed = False
        with self.state_lock:
            time_since_last_data = current_time - self.last_data_time
            if time_since_last_data >= self.dropout_timeout:
                if self.current_zone is not None and self.current_zone != 0:
                    print(f"⚠ Adatforrás kiesett ({time_since_last_data:.1f}s) → LEVEL:0")
                    self.current_zone = 0
                    self.cooldown_active = False
                    self.pending_zone = None
                    self.power_buffer.clear()
                    self.hr_buffer.clear()
                    send_needed = True

        if send_needed:
            self.ble.send_command_sync(0)

    def check_cooldown_and_apply(self, new_zone):
        """Ellenőrzi, hogy a cooldown lejárt-e, és szükség esetén alkalmazza az új zónát.

        Ha a cooldown_seconds idő eltelt, végrehajtja a zónaváltást.
        Ha még nem járt le, frissíti a várakozó zónát, és 10 másodpercenként
        kiírja a hátralévő időt. Ha a zóna a jelenlegi fölé emelkedik, a cooldown
        azonnal törlésre kerül.

        Paraméterek:
            new_zone (int): Az alkalmazni kívánt célzóna (0–3).

        Visszaad:
            int|None: A küldendő zóna szintje, ha zónaváltás történt; None egyébként.
        """
        current_time = time.time()
        send_zone = None

        # Zone increase during cooldown: cancel immediately
        if new_zone >= self.current_zone:
            self.cooldown_active = False
            self.pending_zone = None
            if new_zone > self.current_zone:
                print(f"✓ Teljesítmény emelkedés: cooldown törölve (új zóna: {new_zone} >= jelenlegi: {self.current_zone})")
                self.current_zone = new_zone
                self.last_zone_change = current_time
                return new_zone
            return None

        time_elapsed = current_time - self.cooldown_start_time

        if time_elapsed >= self.cooldown_seconds:
            self.cooldown_active = False
            target_zone = new_zone

            if target_zone != self.current_zone:
                print(f"✓ Cooldown lejárt! Zóna váltás: {self.current_zone} → {target_zone}")
                self.current_zone = target_zone
                self.last_zone_change = current_time
                send_zone = target_zone
            else:
                print(f"✓ Cooldown lejárt, de nincs zóna váltás (már a célzónában vagyunk)")

            self.pending_zone = None
        else:
            remaining = self.cooldown_seconds - time_elapsed
            should_print = (current_time - self.last_cooldown_print) >= self.COOLDOWN_PRINT_INTERVAL

            if new_zone != self.pending_zone and new_zone < self.current_zone:
                old_pending = self.pending_zone
                self.pending_zone = new_zone

                if old_pending is not None and new_zone > old_pending:
                    # Pending zone increased → doubling
                    if self.can_double:
                        new_remaining = min(remaining * 2, self.cooldown_seconds)
                        self.cooldown_start_time = current_time - (self.cooldown_seconds - new_remaining)
                        self.can_double = False
                        self.can_halve = True
                        print(f"🕐 Cooldown duplázva: {remaining:.0f}s → {new_remaining:.0f}s (pending zóna emelkedett: {old_pending} → {new_zone})")
                        remaining = new_remaining
                elif new_zone == 0 or (self.current_zone - new_zone >= 2):
                    # Big drop or zero → halving
                    if self.can_halve:
                        new_remaining = remaining / 2
                        self.cooldown_start_time = current_time - (self.cooldown_seconds - new_remaining)
                        self.can_halve = False
                        self.can_double = True
                        print(f"🕐 Cooldown felezve: {remaining:.0f}s → {new_remaining:.0f}s (nagy zónaesés: {old_pending} → {new_zone})")
                        remaining = new_remaining

                print(f"🕐 Cooldown aktív: még {remaining:.0f}s (várakozó zóna frissítve: {new_zone})")
                self.last_cooldown_print = current_time
            elif should_print and new_zone < self.current_zone:
                print(f"🕐 Cooldown aktív: még {remaining:.0f}s (várakozó zóna: {self.pending_zone})")
                self.last_cooldown_print = current_time

        return send_zone

    def should_change_zone(self, new_zone):
        """Eldönti, hogy szükséges-e zónaváltás, és kezeli a cooldown logikát.

        Zónaváltás szabályai:
            - Zóna növelés: azonnal, cooldown nélkül
            - Zóna csökkentés: cooldown_seconds várakozás után
            - 0W (zero_power_immediate=True): azonnal, cooldown nélkül
            - 0W (zero_power_immediate=False): cooldown szükséges
            - Aktív cooldown alatt zóna emelkedés: cooldown törlése

        Paraméterek:
            new_zone (int): Az új célzóna (0–3).

        Visszaad:
            bool: True, ha azonnali zónaváltás szükséges; False, ha cooldown indul
                  vagy nincs szükség változtatásra.
        """
        current_time = time.time()

        # --- 0W (leállás) kezelés explicit ---
        # Megjegyzés: a new_zone == 0 ágat itt kezeljük le, mielőtt a cooldown_active
        # vizsgálathoz érnénk. Ezért zero_power_immediate=True + cooldown_active=True
        # esetén is ez az ág fut le, és törli a cooldown-t (azonnali leállás).
        if new_zone == 0:
            if self.zero_power_immediate:
                # Azonnali leállás (cooldown nélkül)
                if self.current_zone != 0:
                    print(f"✓ 0W detektálva: azonnali leállás (cooldown nélkül)")
                    self.cooldown_active = False
                    self.pending_zone = None
                    return True
                return False
            else:
                # Normál leállás (cooldown szükséges)
                if self.current_zone != 0:
                    if not self.cooldown_active:
                        self.cooldown_active = True
                        self.cooldown_start_time = current_time
                        self.can_halve = True
                        self.can_double = False
                        # Immediate halving at cooldown start: new_zone==0 is always a big drop
                        remaining = self.cooldown_seconds
                        new_remaining = remaining / 2
                        self.cooldown_start_time = current_time - (self.cooldown_seconds - new_remaining)
                        self.can_halve = False
                        self.can_double = True
                        print(f"🕐 0W detektálva: cooldown indítva {self.cooldown_seconds}s (cél: 0)")
                        print(f"🕐 Cooldown felezve: {remaining:.0f}s → {new_remaining:.0f}s (nagy zónaesés: None → 0)")
                    else:
                        # Már aktív cooldown, de jött 0W (pending_zone váltás)
                        old_pending = self.pending_zone
                        if old_pending is not None and old_pending != 0 and self.can_halve:
                            remaining = self.cooldown_seconds - (current_time - self.cooldown_start_time)
                            new_remaining = remaining / 2
                            self.cooldown_start_time = current_time - (self.cooldown_seconds - new_remaining)
                            self.can_halve = False
                            self.can_double = True
                            print(f"🕐 Cooldown felezve: ... (nagy zónaesés: {old_pending} → 0)")
                        print(f"🕐 0W detektálva: cooldown indítva {self.cooldown_seconds}s (cél: 0)")
                        self.pending_zone = 0
                        return False
                else:
                    # Már 0-ban vagyunk, nincs teendő
                    return False

        if self.cooldown_active:
            if new_zone >= self.current_zone:
                print(f"✓ Teljesítmény emelkedés: cooldown törölve (új zóna: {new_zone} >= jelenlegi: {self.current_zone})")
                self.cooldown_active = False
                self.pending_zone = None
                if new_zone > self.current_zone:
                    return True
                else:
                    return False
            return False

        if new_zone == self.current_zone:
            return False

        if new_zone > self.current_zone:
            return True

        if new_zone < self.current_zone:
            self.cooldown_active = True
            self.cooldown_start_time = current_time
            self.pending_zone = new_zone
            self.can_halve = True
            self.can_double = False
            print(f"🕐 Cooldown indítva: {self.cooldown_seconds}s várakozás (cél: {new_zone})")
            # Immediate halving at cooldown start if big drop
            if self.current_zone - new_zone >= 2:
                remaining = self.cooldown_seconds
                new_remaining = remaining / 2
                self.cooldown_start_time = current_time - (self.cooldown_seconds - new_remaining)
                self.can_halve = False
                self.can_double = True
                print(f"🕐 Cooldown felezve: {remaining:.0f}s → {new_remaining:.0f}s (nagy zónaesés: None → {new_zone})")
            return False

        return False

    def process_power_data(self, power):
        """Feldolgoz egy érkező teljesítmény adatpontot.

        Hozzáadja az értéket a pufferhez, kiszámítja az átlagot,
        meghatározza az új zónát, és szükség esetén BLE parancsot küld.

        Buffer/átlagolás logika:
            Az utolsó buffer_seconds×4 minta átlagát számítja. Ha még nincs
            elég minta (minimum_samples), csak gyűjt, nem dönt.

        Zónaváltás logika a zone_mode alapján:
            - "power_only" és "higher_wins": teljesítmény alapján dönt
            - "hr_only": csak tárolja (dropout detektáláshoz), nem vált
            - "higher_wins": a teljesítmény és HR zóna közül a nagyobbat veszi

        Paraméterek:
            power (int|float): Az azonnali teljesítmény wattban.
        """
        send_zone = None
        with self.state_lock:
            if not self.is_valid_power(power):
                print("⚠ FIGYELMEZTETÉS: Érvénytelen adat!")
                return

            current_time = time.time()
            self.last_data_time = current_time

            power = int(power)
            self.power_buffer.append(power)

            # minden módban (kivéve higher_wins) a bejövő adat kiírása (throttle-ölve, zóna nélkül)
            zone_mode = self.hr_zone_settings.get('zone_mode', 'power_only') if self.hr_zone_settings.get('enabled', False) else 'power_only'
            hr_is_fresh = (self.last_hr_data_time is not None and
                           current_time - self.last_hr_data_time < self.dropout_timeout)

            if zone_mode != 'higher_wins':
                if current_time - self.last_power_print_time >= self.PRINT_THROTTLE_SECONDS:
                    print(f"⚡ Teljesítmény: {power} watt")
                    self.last_power_print_time = current_time

            if len(self.power_buffer) < self.minimum_samples:
                print(f"📊 Adatok gyűjtése: {len(self.power_buffer)}/{self.minimum_samples}")
                return

            avg_power = round(sum(self.power_buffer) / len(self.power_buffer))
            new_power_zone = self.get_zone_for_power(avg_power)
            self.current_power_zone = new_power_zone
            self.current_avg_power = avg_power

            if zone_mode == 'hr_only':
                return  # nincs átlag power kiírás, nincs zónaváltás

            if zone_mode == 'higher_wins':
                if self.current_hr_zone is None or not hr_is_fresh:
                    # HR nem elérhető/friss, power egyedül vezérli a ventilátort
                    print(f"⚡ Átlag teljesítmény: {avg_power} watt | Power zóna: {new_power_zone} | Higher Wins!")
                    new_zone = new_power_zone
                else:
                    # HR elérhető, a combined sor a process_heart_rate_data-ból jön
                    new_zone = max(new_power_zone, self.current_hr_zone)
            else:
                print(f"⚡ Átlag teljesítmény: {avg_power} watt | Power zóna: {new_power_zone}")
                new_zone = new_power_zone

            cooldown_send_zone = None
            zone_change_send = None
            if self.cooldown_active:
                cooldown_send_zone = self.check_cooldown_and_apply(new_zone)
            elif self.current_zone is None or self.should_change_zone(new_zone):
                self.current_zone = new_zone
                self.last_zone_change = current_time
                zone_change_send = new_zone

        send_zone = cooldown_send_zone if cooldown_send_zone is not None else zone_change_send
        if send_zone is not None:
            self.ble.send_command_sync(send_zone)

    def process_heart_rate_data(self, hr):
        """Feldolgoz egy érkező szívfrekvencia adatpontot.

        Ha a HR zóna ki van kapcsolva (enabled=False), csak megjeleníti
        a bpm értéket. Ha be van kapcsolva, a zone_mode alapján dönt:

        zone_mode logika:
            - "power_only": csak kiírja a HR-t, nem befolyásolja a zónát
            - "hr_only":    csak a HR zóna alapján vált ventilátort
            - "higher_wins": a HR és teljesítmény zóna közül a nagyobb dönt

        Paraméterek:
            hr (int|float): A szívfrekvencia bpm-ben (érvényes: 1–220).
        """
        try:
            hr = int(hr)
        except (TypeError, ValueError):
            return
        if hr <= 0 or hr > 220:
            return

        with self.state_lock:
            self.current_heart_rate = hr
            current_time = time.time()
            self.last_hr_data_time = current_time

            # hr_only módban az HR adat is frissítse a last_data_time-ot,
            # különben a dropout checker Z0-ra kapcsol
            zone_mode = self.hr_zone_settings.get('zone_mode', 'power_only') if self.hr_zone_settings.get('enabled', False) else 'power_only'
            if zone_mode == 'hr_only':
                self.last_data_time = current_time

            if not self.hr_zone_settings.get('enabled', False):
                if current_time - self.last_hr_print_time >= self.PRINT_THROTTLE_SECONDS:
                    print(f"❤ Szívfrekvencia: {hr} bpm")
                    self.last_hr_print_time = current_time
                return

            self.hr_buffer.append(hr)

            # hr_only és power_only módban a bejövő adat kiírása (throttle-ölve, zóna nélkül)
            if zone_mode in ('hr_only', 'power_only'):
                if current_time - self.last_hr_print_time >= self.PRINT_THROTTLE_SECONDS:
                    print(f"❤ HR: {hr} bpm")
                    self.last_hr_print_time = current_time

            if len(self.hr_buffer) < self.minimum_samples:
                print(f"📊 HR adatok gyűjtése: {len(self.hr_buffer)}/{self.minimum_samples}")
                return
            avg_hr = round(sum(self.hr_buffer) / len(self.hr_buffer))
            new_hr_zone = self.get_hr_zone(avg_hr)
            self.current_hr_zone = new_hr_zone

            # zone_mode already computed above – reuse instead of re-querying
            if zone_mode == 'power_only':
                return  # nincs átlag HR kiírás, nincs zónaváltás


            if zone_mode == 'hr_only':
                print(f"❤ Átlag HR: {avg_hr} bpm | HR zóna: {new_hr_zone}")
                target_zone = new_hr_zone
            else:  # higher_wins
                power_is_fresh = (self.last_data_time is not None and
                                  current_time - self.last_data_time < self.dropout_timeout)
                if self.current_power_zone is not None and self.current_avg_power is not None and power_is_fresh:
                    avg_power = self.current_avg_power
                    power_zone = self.current_power_zone
                    target_zone = max(power_zone, new_hr_zone)
                    print(f"⚡ Átlag teljesítmény: {avg_power} watt | Power zóna: {power_zone} | ❤ Átlag HR: {avg_hr} bpm | HR zóna: {new_hr_zone} | Higher Wins!")
                else:
                    target_zone = new_hr_zone
                    print(f"❤ Átlag HR: {avg_hr} bpm | HR zóna: {new_hr_zone} | Higher Wins!")

            cooldown_send_zone = None
            zone_change_send = None
            if self.cooldown_active:
                cooldown_send_zone = self.check_cooldown_and_apply(target_zone)
            elif self.current_zone is None or self.should_change_zone(target_zone):
                self.current_zone = target_zone
                self.last_zone_change = current_time
                zone_change_send = target_zone

        send_zone = cooldown_send_zone if cooldown_send_zone is not None else zone_change_send
        if send_zone is not None:
            self.ble.send_command_sync(send_zone)



# ============================================================
# BLEPowerReceiver
# ============================================================
class BLEPowerReceiver:
    """BLE Cycling Power Service fogadó.

    Standard BLE Cycling Power Service (UUID: 0x1818) segítségével fogadja
    az azonnali teljesítmény adatokat (Cycling Power Measurement, 0x2A63).
    Saját háttérszálban fut, notification-ökön kapja az adatot.
    Teljesen független BLE kapcsolat a BLEController-től.

    Parse: flags (2 byte little-endian) → instantaneous power (2 byte LE, signed int16)
    """

    CYCLING_POWER_SERVICE_UUID = "00001818-0000-1000-8000-00805f9b34fb"
    CYCLING_POWER_MEASUREMENT_UUID = "00002a63-0000-1000-8000-00805f9b34fb"
    RETRY_RESET_SECONDS = 30
    STOP_JOIN_TIMEOUT = 5

    def __init__(self, settings, controller):
        """Inicializálja a BLEPowerReceiver-t.

        Paraméterek:
            settings (dict): A teljes beállítások dict-je.
            controller (PowerZoneController): A vezérlő példány.
        """
        ds = settings['data_source']
        self.device_name = ds.get('ble_power_device_name')
        self.scan_timeout = ds.get('ble_power_scan_timeout', 10)
        self.reconnect_interval = ds.get('ble_power_reconnect_interval', 5)
        self.max_retries = ds.get('ble_power_max_retries', 10)
        self.controller = controller

        self.running = threading.Event()
        self._is_connected = False
        self._state_lock = threading.Lock()
        self._stop_event = None
        self.thread = None
        self.loop = None
        self._retry_count = 0

    @property
    def is_connected(self):
        with self._state_lock:
            return self._is_connected

    @is_connected.setter
    def is_connected(self, value):
        with self._state_lock:
            self._is_connected = value

    def start(self):
        """Elindítja a BLE power fogadó háttérszálat."""
        if self.running.is_set():
            logger.warning("BLE Power thread már fut!")
            return
        self.running.set()
        self.thread = threading.Thread(target=self._run_loop, daemon=True, name="BLEPower-Thread")
        self.thread.start()
        logger.info("BLE Power thread elindítva")

    def _run_loop(self):
        """A BLE power háttérszál fő ciklusa."""
        try:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(self._receive_loop())
        except Exception as e:
            logger.error(f"BLE Power thread kritikus hiba: {e}")
        finally:
            if self.loop:
                self.loop.close()
            logger.info("BLE Power thread leállítva")

    async def _receive_loop(self):
        """BLE power adatfogadás ciklusa újracsatlakozási logikával."""
        self._stop_event = asyncio.Event()
        while self.running.is_set():
            try:
                await self._scan_and_subscribe()
            except Exception as e:
                if not self.running.is_set():
                    break
                self._retry_count += 1
                logger.warning(f"BLE Power kapcsolat megszakadt ({self._retry_count}/{self.max_retries}): {e}")
                self.is_connected = False
                if self._retry_count >= self.max_retries:
                    logger.warning(f"Max BLE Power újracsatlakozási kísérletek elérve! {self.RETRY_RESET_SECONDS}s várakozás...")
                    await asyncio.sleep(self.RETRY_RESET_SECONDS)
                    self._retry_count = 0
                else:
                    await asyncio.sleep(self.reconnect_interval)

    async def _scan_and_subscribe(self):
        """BLE power meter keresése, csatlakozás és notification feliratkozás."""
        if not self.device_name:
            logger.warning("BLE Power: nincs eszköznév megadva, várakozás...")
            await asyncio.sleep(self.reconnect_interval)
            return

        logger.info(f"BLE Power keresés: {self.device_name}...")
        devices = await BleakScanner.discover(timeout=self.scan_timeout)
        device_addr = None
        for d in devices:
            if d.name == self.device_name:
                device_addr = d.address
                logger.info(f"BLE Power eszköz megtalálva: {d.name} ({d.address})")
                break

        if not device_addr:
            logger.error(f"BLE Power eszköz nem található: {self.device_name}")
            raise Exception(f"Device not found: {self.device_name}")

        self._stop_event.clear()
        async with BleakClient(device_addr) as client:
            self.is_connected = True
            self._retry_count = 0
            logger.info(f"BLE Power csatlakozva: {device_addr}")

            def notification_handler(sender, data):
                try:
                    if len(data) < 4:
                        return
                    # flags: 2 bytes LE; instantaneous power: 2 bytes LE signed int16
                    power = int.from_bytes(data[2:4], byteorder='little', signed=True)
                    self.controller.process_power_data(power)
                except Exception as e:
                    logger.warning(f"BLE Power notification hiba: {e}")

            await client.start_notify(self.CYCLING_POWER_MEASUREMENT_UUID, notification_handler)
            while self.running.is_set() and client.is_connected and not self._stop_event.is_set():
                await asyncio.sleep(1)
            try:
                await client.stop_notify(self.CYCLING_POWER_MEASUREMENT_UUID)
            except Exception:
                pass

        self.is_connected = False

    def stop(self):
        """Leállítja a BLE power háttérszálat."""
        if not self.running.is_set():
            return
        logger.info("BLE Power thread leállítása...")
        self.running.clear()
        if self.loop and self._stop_event is not None:
            self.loop.call_soon_threadsafe(self._stop_event.set)
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=self.STOP_JOIN_TIMEOUT)
            if self.thread.is_alive():
                logger.warning("BLE Power thread nem állt le időben")
            else:
                logger.info("BLE Power thread leállítva")


# ============================================================
# BLEHeartRateReceiver
# ============================================================
class BLEHeartRateReceiver:
    """BLE Heart Rate Service fogadó.

    Standard BLE Heart Rate Service (UUID: 0x180D) segítségével fogadja
    a szívfrekvencia adatokat (Heart Rate Measurement, 0x2A37).
    Saját háttérszálban fut, notification-ökön kapja az adatot.
    Teljesen független BLE kapcsolat a BLEController-től.

    Parse: flags byte → 8-bit vagy 16-bit HR érték
    """

    HEART_RATE_SERVICE_UUID = "0000180d-0000-1000-8000-00805f9b34fb"
    HEART_RATE_MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"
    RETRY_RESET_SECONDS = 30
    STOP_JOIN_TIMEOUT = 5

    def __init__(self, settings, controller):
        """Inicializálja a BLEHeartRateReceiver-t.

        Paraméterek:
            settings (dict): A teljes beállítások dict-je.
            controller (PowerZoneController): A vezérlő példány.
        """
        ds = settings['data_source']
        self.device_name = ds.get('ble_hr_device_name')
        self.scan_timeout = ds.get('ble_hr_scan_timeout', 10)
        self.reconnect_interval = ds.get('ble_hr_reconnect_interval', 5)
        self.max_retries = ds.get('ble_hr_max_retries', 10)
        self.controller = controller

        self.running = threading.Event()
        self._is_connected = False
        self._state_lock = threading.Lock()
        self._stop_event = None
        self.thread = None
        self.loop = None
        self._retry_count = 0

    @property
    def is_connected(self):
        with self._state_lock:
            return self._is_connected

    @is_connected.setter
    def is_connected(self, value):
        with self._state_lock:
            self._is_connected = value

    def start(self):
        """Elindítja a BLE HR fogadó háttérszálat."""
        if self.running.is_set():
            logger.warning("BLE HR thread már fut!")
            return
        self.running.set()
        self.thread = threading.Thread(target=self._run_loop, daemon=True, name="BLEHR-Thread")
        self.thread.start()
        logger.info("BLE HR thread elindítva")

    def _run_loop(self):
        """A BLE HR háttérszál fő ciklusa."""
        try:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(self._receive_loop())
        except Exception as e:
            logger.error(f"BLE HR thread kritikus hiba: {e}")
        finally:
            if self.loop:
                self.loop.close()
            logger.info("BLE HR thread leállítva")

    async def _receive_loop(self):
        """BLE HR adatfogadás ciklusa újracsatlakozási logikával."""
        self._stop_event = asyncio.Event()
        while self.running.is_set():
            try:
                await self._scan_and_subscribe()
            except Exception as e:
                if not self.running.is_set():
                    break
                self._retry_count += 1
                logger.warning(f"BLE HR kapcsolat megszakadt ({self._retry_count}/{self.max_retries}): {e}")
                self.is_connected = False
                if self._retry_count >= self.max_retries:
                    logger.warning(f"Max BLE HR újracsatlakozási kísérletek elérve! {self.RETRY_RESET_SECONDS}s várakozás...")
                    await asyncio.sleep(self.RETRY_RESET_SECONDS)
                    self._retry_count = 0
                else:
                    await asyncio.sleep(self.reconnect_interval)

    async def _scan_and_subscribe(self):
        """BLE HR eszköz keresése, csatlakozás és notification feliratkozás."""
        if not self.device_name:
            logger.warning("BLE HR: nincs eszköznév megadva, várakozás...")
            await asyncio.sleep(self.reconnect_interval)
            return

        logger.info(f"BLE HR keresés: {self.device_name}...")
        devices = await BleakScanner.discover(timeout=self.scan_timeout)
        device_addr = None
        for d in devices:
            if d.name == self.device_name:
                device_addr = d.address
                logger.info(f"BLE HR eszköz megtalálva: {d.name} ({d.address})")
                break

        if not device_addr:
            logger.error(f"BLE HR eszköz nem található: {self.device_name}")
            raise Exception(f"Device not found: {self.device_name}")

        self._stop_event.clear()
        async with BleakClient(device_addr) as client:
            self.is_connected = True
            self._retry_count = 0
            logger.info(f"BLE HR csatlakozva: {device_addr}")

            def notification_handler(sender, data):
                try:
                    if len(data) < 2:
                        return
                    flags = data[0]
                    # bit 0 of flags: 0 = 8-bit HR value, 1 = 16-bit HR value
                    if flags & 0x01:
                        if len(data) < 3:
                            return
                        hr = int.from_bytes(data[1:3], byteorder='little')
                    else:
                        hr = data[1]
                    self.controller.process_heart_rate_data(hr)
                except Exception as e:
                    logger.warning(f"BLE HR notification hiba: {e}")

            await client.start_notify(self.HEART_RATE_MEASUREMENT_UUID, notification_handler)
            while self.running.is_set() and client.is_connected and not self._stop_event.is_set():
                await asyncio.sleep(1)
            try:
                await client.stop_notify(self.HEART_RATE_MEASUREMENT_UUID)
            except Exception:
                pass

        self.is_connected = False

    def stop(self):
        """Leállítja a BLE HR háttérszálat."""
        if not self.running.is_set():
            return
        logger.info("BLE HR thread leállítása...")
        self.running.clear()
        if self.loop and self._stop_event is not None:
            self.loop.call_soon_threadsafe(self._stop_event.set)
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=self.STOP_JOIN_TIMEOUT)
            if self.thread.is_alive():
                logger.warning("BLE HR thread nem állt le időben")
            else:
                logger.info("BLE HR thread leállítva")


# ============================================================
# ZwiftUDPReceiver
# ============================================================
class ZwiftUDPReceiver:
    """Zwift UDP adatfogadó.

    A zwift-udp-monitor programból érkező JSON csomagokat fogadja UDP-n.
    Háttérszálban fut, a bejövő power és HR adatokat validálja,
    majd átadja a PowerZoneController-nek.
    """

    RECV_BUFFER = 4096
    SOCKET_TIMEOUT = 2.0

    def __init__(self, settings, controller):
        ds = settings['data_source']
        self.host = ds.get('zwift_udp_host', '127.0.0.1')
        self.port = ds.get('zwift_udp_port', 7878)
        self.controller = controller

        self.process_power = (ds.get('power_source') == 'zwift_udp')
        self.process_hr = (ds.get('hr_source') == 'zwift_udp' and
                          settings.get('heart_rate_zones', {}).get('enabled', False))

        self.running = threading.Event()
        self.thread = None
        self.last_data_time = 0
        self._state_lock = threading.Lock()

    @property
    def has_data(self):
        """Van-e friss adat (dropout detektáláshoz)."""
        with self._state_lock:
            return self.last_data_time > 0

    @property
    def last_data(self):
        with self._state_lock:
            return self.last_data_time

    def start(self):
        if self.running.is_set():
            logger.warning("Zwift UDP thread már fut!")
            return
        self.running.set()
        self.thread = threading.Thread(target=self._receive_loop, daemon=True, name="ZwiftUDP-Thread")
        self.thread.start()
        logger.info(f"Zwift UDP listener elindítva: {self.host}:{self.port}")

    def _receive_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(self.SOCKET_TIMEOUT)
        try:
            sock.bind((self.host, self.port))
            logger.info(f"Zwift UDP socket kötve: {self.host}:{self.port}")
        except OSError as e:
            logger.error(f"Zwift UDP bind hiba: {e}")
            return

        try:
            while self.running.is_set():
                try:
                    raw, addr = sock.recvfrom(self.RECV_BUFFER)
                    self._process_packet(raw)
                except socket.timeout:
                    continue
                except Exception as e:
                    if self.running.is_set():
                        logger.warning(f"Zwift UDP fogadási hiba: {e}")
        finally:
            sock.close()
            logger.info("Zwift UDP socket lezárva")

    def _process_packet(self, raw):
        """JSON csomag feldolgozása – validáció + controller értesítés.

        FONTOS: Validálja a power és HR értékeket MIELŐTT átadná
        a controllernek (pont mint az ANT+ és BLE forrásoknál).
        Érvénytelen adatok nem kerülnek be az átlagolásba.
        """
        try:
            data = json.loads(raw.decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.debug(f"Zwift UDP: érvénytelen JSON: {e}")
            return

        if not isinstance(data, dict):
            return

        had_valid_data = False

        if self.process_power and 'power' in data:
            power = data['power']
            if isinstance(power, (int, float)) and not isinstance(power, bool):
                power = int(power)
                if 0 <= power <= 2500:
                    self.controller.process_power_data(power)
                    had_valid_data = True
                else:
                    logger.debug(f"Zwift UDP: power tartományon kívül: {power}")
            else:
                logger.debug(f"Zwift UDP: érvénytelen power típus: {type(power)}")

        if self.process_hr and 'heartrate' in data:
            hr = data['heartrate']
            if isinstance(hr, (int, float)) and not isinstance(hr, bool):
                hr = int(hr)
                if 0 <= hr <= 250:
                    self.controller.process_heart_rate_data(hr)
                    had_valid_data = True
                else:
                    logger.debug(f"Zwift UDP: heartrate tartományon kívül: {hr}")
            else:
                logger.debug(f"Zwift UDP: érvénytelen heartrate típus: {type(hr)}")

        if had_valid_data:
            with self._state_lock:
                self.last_data_time = time.time()

    def stop(self):
        if not self.running.is_set():
            return
        logger.info("Zwift UDP thread leállítása...")
        self.running.clear()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                logger.warning("Zwift UDP thread nem állt le időben")
            else:
                logger.info("Zwift UDP thread leállítva")


class DataSourceManager:
    """ANT+ adatforrás kezelője.

    Kezeli az ANT+ adatforrást, újracsatlakozási logikával.

    Osztályváltozók:
        ANTPLUS_RECONNECT_DELAY (int): ANT+ újracsatlakozási várakozás (s).
        ANTPLUS_MAX_RETRIES (int): ANT+ maximális újracsatlakozási kísérletek.
    """

    ANTPLUS_RECONNECT_DELAY = 5
    ANTPLUS_MAX_RETRIES = 10
    ANTPLUS_MAX_RETRY_COOLDOWN = 30    # ← ÚJ

    def __init__(self, settings, controller):
        """Inicializálja a DataSourceManager-t.

        Paraméterek:
            settings (dict): A teljes beállítások dict-je.
            controller (PowerZoneController): A vezérlő példány, amelynek a
                power/HR adatokat átadja.
        """
        self.settings = settings
        self.controller = controller
        self.ds_settings = settings['data_source']

        self.antplus_node = None
        self.antplus_devices = []
        self.antplus_last_data = 0

        self.ble_power_receiver = None
        self.ble_hr_receiver = None
        self.zwift_udp_receiver = None

        self.running = threading.Event()
        self.monitor_thread = None
        self.antplus_thread = None

    def _on_antplus_found(self, device):
        """Callback: ANT+ eszköz csatlakozásakor hívódik meg.

        Paraméterek:
            device: Az ANT+ eszköz objektuma.
        """
        self.antplus_last_data = time.time()

    def _on_antplus_data(self, page, page_name, data):
        """Callback: ANT+ adatcsomag érkezésekor hívódik meg.

        PowerData esetén: frissíti az utolsó adatidőt, átadja a controllernek.
        HeartRateData esetén: a controllert értesíti.

        Paraméterek:
            page (int): ANT+ adatlap száma.
            page_name (str): ANT+ adatlap neve.
            data (PowerData|HeartRateData): Az ANT+ adat objektuma.
        """
        if isinstance(data, PowerData):
            self.antplus_last_data = time.time()
            power = data.instantaneous_power
            self.controller.process_power_data(power)
        elif isinstance(data, HeartRateData):
            self.antplus_last_data = time.time()
            hr = data.heart_rate
            self.controller.process_heart_rate_data(hr)

    def _register_antplus_device(self, device):
        """ANT+ eszköz regisztrálása – callback-ek beállítása.

        Paraméterek:
            device: Az ANT+ eszköz objektuma (pl. PowerMeter, HeartRate).
        """
        self.antplus_devices.append(device)
        device.on_found = lambda: self._on_antplus_found(device)
        device.on_device_data = self._on_antplus_data

    def _init_antplus_node(self):
        """Inicializálja az ANT+ node-ot és regisztrálja az eszközöket.

        PowerMeter csak akkor regisztrálódik, ha power_source == 'antplus'.
        HeartRate monitor csak akkor regisztrálódik, ha hr_source == 'antplus'
        ÉS heart_rate_zones engedélyezett.
        """
        self.antplus_node = Node()
        self.antplus_node.set_network_key(0x00, ANTPLUS_NETWORK_KEY)

        self.antplus_devices = []

        if self.ds_settings.get('power_source', 'antplus') == 'antplus':
            meter = PowerMeter(self.antplus_node)
            self._register_antplus_device(meter)

        if (self.ds_settings.get('hr_source', 'antplus') == 'antplus' and
                self.settings.get('heart_rate_zones', {}).get('enabled', False)):
            hr_monitor = HeartRate(self.antplus_node)
            self._register_antplus_device(hr_monitor)

    def _start_antplus(self):
        """Inicializálja és elindítja az ANT+ háttérszálat.

        Visszaad:
            bool: True, ha az indítás sikeres; False egyébként.
        """
        max_init_retries = 3
        init_retry_delay = 2

        for attempt in range(1, max_init_retries + 1):
            try:
                self._init_antplus_node()

                ant_thread = threading.Thread(
                    target=self._antplus_loop,
                    daemon=True,
                    name="ANT+-Thread"
                )
                self.antplus_thread = ant_thread
                ant_thread.start()
                logger.info("ANT+ figyelés elindítva")
                return True

            except Exception as e:
                logger.warning(f"ANT+ inicializálás sikertelen ({attempt}/{max_init_retries}): {e}")
                self.antplus_node = None
                if attempt < max_init_retries:
                    logger.info(f"Újrapróbálkozás {init_retry_delay}s múlva...")
                    time.sleep(init_retry_delay)

        logger.error("ANT+ indítás sikertelen minden kísérlet után")
        return False

    def _antplus_loop(self):
        """Az ANT+ háttérszál fő ciklusa – újracsatlakozási logikával.

        Elindítja az ANT+ node-ot. Ha hiba lép fel, ANTPLUS_RECONNECT_DELAY
        másodpercenként újrapróbálkozik, maximum ANTPLUS_MAX_RETRIES kísérletig.
        Ha eléri a maximumot, 30 másodpercet vár, nullázza a számlálót és
        újrakezdi – sosem adja fel.
        """
        retry_count = 0

        while self.running.is_set():
            try:
                if self.antplus_node is None:
                    logger.warning("ANT+ node nincs inicializálva, várakozás...")
                    time.sleep(self.ANTPLUS_RECONNECT_DELAY)
                    continue
                self.antplus_node.start()
                # Ha ide ér, az ANT+ node leállt (pl. dongle kihúzva)
                if not self.running.is_set():
                    break
                # Ha volt sikeres adat a futás során, reseteljük a retry_count-ot.
                # antplus_last_data nullázása itt történik, hogy a következő
                # leálláskor csak akkor resetálódjon, ha újra volt adat.
                if self.antplus_last_data > 0:
                    retry_count = 0
                    self.antplus_last_data = 0
                else:
                    retry_count += 1
                logger.warning(f"ANT+ node leállt, újraindítás... ({retry_count}/{self.ANTPLUS_MAX_RETRIES})")

                if retry_count >= self.ANTPLUS_MAX_RETRIES:
                    logger.warning(f"Max ANT+ újracsatlakozási kísérletek elérve ({self.ANTPLUS_MAX_RETRIES})! {self.ANTPLUS_MAX_RETRY_COOLDOWN}s múlva újrapróbálkozik...")
                    time.sleep(self.ANTPLUS_MAX_RETRY_COOLDOWN)
                    if not self.running.is_set():
                        break
                    logger.info("ANT+ retry count reset, újrapróbálkozás...")
                    retry_count = 0

                try:
                    self._stop_antplus_node()
                    time.sleep(1)
                    self._init_antplus_node()
                    logger.info("ANT+ node újrainicializálva, újrapróbálkozás...")
                except Exception as re:
                    logger.error(f"ANT+ újrainicializálás hiba: {re}")
                    time.sleep(self.ANTPLUS_RECONNECT_DELAY)
                    if not self.running.is_set():
                        break

            except Exception as e:
                if not self.running.is_set():
                    break

                retry_count += 1
                logger.warning(f"ANT+ kapcsolat megszakadt ({retry_count}/{self.ANTPLUS_MAX_RETRIES}): {e}")
                self.antplus_last_data = 0

                if retry_count >= self.ANTPLUS_MAX_RETRIES:
                    logger.warning(f"Max ANT+ újracsatlakozási kísérletek elérve ({self.ANTPLUS_MAX_RETRIES})! {self.ANTPLUS_MAX_RETRY_COOLDOWN}s múlva újrapróbálkozik...")
                    time.sleep(self.ANTPLUS_MAX_RETRY_COOLDOWN)
                    if not self.running.is_set():
                        break
                    logger.info("ANT+ retry count reset, újrapróbálkozás...")
                    retry_count = 0
                    continue

                logger.info(f"ANT+ újracsatlakozás {self.ANTPLUS_RECONNECT_DELAY}s múlva...")
                time.sleep(self.ANTPLUS_RECONNECT_DELAY)

                if not self.running.is_set():
                    break

                try:
                    self._stop_antplus_node()
                    time.sleep(1)  # USB erőforrás felszabadulásra várakozás
                    self._init_antplus_node()
                    logger.info("ANT+ node újrainicializálva, újrapróbálkozás...")
                except Exception as re:
                    logger.error(f"ANT+ újrainicializálás hiba: {re}")
                    time.sleep(self.ANTPLUS_RECONNECT_DELAY)
                    if not self.running.is_set():
                        break

    def _stop_antplus_node(self):
        """Leállítja az ANT+ node-ot és felszabadítja az eszközöket."""
        try:
            if self.antplus_devices:
                for d in self.antplus_devices:
                    try:
                        d.close_channel()
                    except Exception:
                        pass
            if self.antplus_node:
                self.antplus_node.stop()
                self.antplus_node = None
            self.antplus_devices = []
        except Exception:
            pass

    def _stop_antplus(self):
        """Leállítja az ANT+ forrást és nullázza az utolsó adatidőt."""
        try:
            self._stop_antplus_node()
            self.antplus_last_data = 0
            logger.info("ANT+ leállítva")
        except Exception as e:
            logger.warning(f"ANT+ leállítási hiba: {e}")

    def _monitor_loop(self):
        """Adatforrás monitor háttérszál – státusz kiírása.

        30 másodpercenként kiírja az adatforrás státuszt a konzolra.
        """
        dropout_timeout = self.settings['dropout_timeout']
        last_source_print = 0

        while self.running.is_set():
            time.sleep(5)

            if not self.running.is_set():
                break

            current_time = time.time()

            antplus_has_data = (
                self.antplus_last_data > 0 and
                (current_time - self.antplus_last_data) < dropout_timeout
            )

            if current_time - last_source_print >= 30:
                parts = []
                power_source = self.ds_settings.get('power_source', 'antplus')
                hr_source = self.ds_settings.get('hr_source', 'antplus')

                if power_source == 'antplus' or hr_source == 'antplus':
                    parts.append(f"ANT+: {'✓' if antplus_has_data else '✗'}")
                if power_source == 'ble':
                    ble_power_ok = self.ble_power_receiver is not None and self.ble_power_receiver.is_connected
                    parts.append(f"BLE Power: {'✓' if ble_power_ok else '✗'}")
                if hr_source == 'ble':
                    ble_hr_ok = self.ble_hr_receiver is not None and self.ble_hr_receiver.is_connected
                    parts.append(f"BLE HR: {'✓' if ble_hr_ok else '✗'}")
                if power_source == 'zwift_udp' or hr_source == 'zwift_udp':
                    zwift_udp_ok = (self.zwift_udp_receiver is not None and
                                    self.zwift_udp_receiver.has_data and
                                    (current_time - self.zwift_udp_receiver.last_data) < dropout_timeout)
                    parts.append(f"Zwift UDP: {'✓' if zwift_udp_ok else '✗'}")

                print(f"📡 Adatforrás státusz | {' | '.join(parts)}")
                last_source_print = current_time

    def start(self):
        """Elindítja az adatforrás(oka)t és a monitor szálat.

        Indítási sorend:
            1. BLE Power fogadó (ha power_source == 'ble')
            2. BLE HR fogadó (ha hr_source == 'ble' és HR engedélyezett)
            3. Zwift UDP fogadó (ha legalább az egyik forrás 'zwift_udp')
            4. ANT+ szál (ha legalább az egyik forrás 'antplus')
            5. Adatforrás monitor szál
        """
        self.running.set()

        power_source = self.ds_settings.get('power_source', 'antplus')
        hr_source = self.ds_settings.get('hr_source', 'antplus')
        hr_enabled = self.settings.get('heart_rate_zones', {}).get('enabled', False)

        print(f"📡 Power forrás: {power_source.upper()} | HR forrás: {hr_source.upper()}")

        if power_source == 'ble':
            self.ble_power_receiver = BLEPowerReceiver(self.settings, self.controller)
            self.ble_power_receiver.start()

        if hr_source == 'ble' and hr_enabled:
            self.ble_hr_receiver = BLEHeartRateReceiver(self.settings, self.controller)
            self.ble_hr_receiver.start()

        needs_zwift_udp = (power_source == 'zwift_udp') or (hr_source == 'zwift_udp' and hr_enabled)
        if needs_zwift_udp:
            self.zwift_udp_receiver = ZwiftUDPReceiver(self.settings, self.controller)
            self.zwift_udp_receiver.start()

        needs_antplus = (power_source == 'antplus') or (hr_source == 'antplus' and hr_enabled)
        if needs_antplus:
            self._start_antplus()

        self.monitor_thread = threading.Thread(
            target=self._monitor_loop,
            daemon=True,
            name="DataSource-Monitor"
        )
        self.monitor_thread.start()
        logger.info("Adatforrás monitor elindítva")

    def stop(self):
        """Leállítja az összes adatforrást."""
        self.running.clear()

        if self.ble_power_receiver:
            try:
                self.ble_power_receiver.stop()
            except Exception as e:
                logger.error(f"BLE Power leállítási hiba: {e}")

        if self.ble_hr_receiver:
            try:
                self.ble_hr_receiver.stop()
            except Exception as e:
                logger.error(f"BLE HR leállítási hiba: {e}")

        if self.zwift_udp_receiver:
            try:
                self.zwift_udp_receiver.stop()
            except Exception as e:
                logger.error(f"Zwift UDP leállítási hiba: {e}")

        # Stop the ANT+ node BEFORE joining the thread so the blocking
        # antplus_node.start() call is interrupted and the thread can exit.
        try:
            self._stop_antplus()
        except Exception as e:
            logger.error(f"ANT+ leállítási hiba: {e}")

        if self.antplus_thread and self.antplus_thread.is_alive():
            self.antplus_thread.join(timeout=5)

        if self.monitor_thread and self.monitor_thread.is_alive():
            self.monitor_thread.join(timeout=10)


# ============================================================
# main()
# ============================================================
def main():
    """A program belépési pontja.

    Inicializálási sorend:
        1. Naplózás és stderr elnyomása (külső könyvtárak zajának szűrése)
        2. PowerZoneController létrehozása (settings.json betöltése)
        3. BLE szál indítása, BLE inicializálás megvárása
        4. Dropout ellenőrző szál indítása
        5. DataSourceManager indítása (ANT+)
        6. Főciklus: Ctrl+C megvárása
        7. Leállítás: DataSource, Dropout, BLE tiszta leállítása
    """
    # Saját logger beállítása
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s [%(threadName)s] %(levelname)s %(message)s'))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    # Külső könyvtárak elnyomása
    logging.getLogger('bleak').setLevel(logging.CRITICAL)
    logging.getLogger('openant').setLevel(logging.CRITICAL)

    print("=" * 60)
    print(f"  Smart Fan Controller v{__version__} - Power/HR → BLE Fan Control")
    print("=" * 60)
    print()

    controller = PowerZoneController("settings.json")

    print()
    print("-" * 60)

    controller.ble.start()
    controller.start_dropout_checker()

    ble_timeout = (controller.settings['ble']['scan_timeout'] +
                   controller.settings['ble']['connection_timeout'])

    print(f"⏳ BLE inicializálás folyamatban (max {ble_timeout}s)...")

    controller.ble.ready_event.wait(timeout=ble_timeout)
    print("✓ BLE inicializálás kész")

    print("-" * 60)
    print()

    data_manager = DataSourceManager(controller.settings, controller)
    data_manager.start()

    _cleaned_up = False

    def cleanup():
        nonlocal _cleaned_up
        if _cleaned_up:
            return
        _cleaned_up = True
        try:
            data_manager.stop()
        except Exception as e:
            print(f"DataSource leállítási hiba: {e}")
        try:
            controller.stop_dropout_checker()
        except Exception as e:
            print(f"Dropout thread leállítási hiba: {e}")
        try:
            controller.ble.stop()
        except Exception as e:
            print(f"BLE leállítási hiba: {e}")
        print()
        print("✓ Program leállítva")
        print()

    atexit.register(cleanup)

    def handle_sigterm(signum, frame):
        print("\n🛑 SIGTERM fogadva, leállítás...")
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_sigterm)

    print()
    print("🚴 Figyelés elindítva... (Ctrl+C a leállításhoz)")
    print()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n\n🛑 Leállítás...")
        cleanup()


if __name__ == "__main__":
    main()
