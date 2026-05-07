from multiprocessing.util import info

import customtkinter as ctk
from datetime import datetime
import json
import threading
import time
import random  # Voor testwaarden
from urllib.request import urlopen
 
import requests
 
# ─────────────────────────────────────────────
#  PLATFORM DETECTIE
# ─────────────────────────────────────────────
try:
    import RPi.GPIO as GPIO
    ON_PI = True
except ImportError:
    GPIO = None
    ON_PI = False
    print("Systeem: Geen Raspberry Pi gedetecteerd. Test-modus geactiveerd.")
 
# ─────────────────────────────────────────────
#  PIN-MAPPING BOARD → BCM  (uit main.py)
# ─────────────────────────────────────────────
BOARD_TO_BCM = {
    3: 2, 5: 3, 7: 4, 8: 14, 10: 15, 11: 17, 12: 18, 13: 27,
    15: 22, 16: 23, 18: 24, 19: 10, 21: 9, 22: 25, 23: 11,
    24: 8, 26: 7, 27: 0, 28: 1, 29: 5, 31: 6, 32: 12, 33: 13,
    35: 19, 36: 16, 37: 26, 38: 20, 40: 21,
}
 
def resolve_bcm_pin(pin, numbering_mode):
    """Zet een BOARD-pin om naar BCM indien nodig."""
    mode = numbering_mode.upper()
    if mode == "BCM":
        return pin
    if mode == "BOARD":
        if pin not in BOARD_TO_BCM:
            raise ValueError(f"BOARD pin {pin} kan niet worden omgezet naar BCM.")
        return BOARD_TO_BCM[pin]
    raise ValueError("PIN_NUMBERING moet 'BOARD' of 'BCM' zijn.")
 
def _build_dht_reader(sensor_names, bcm_pin):
    """Bouw een DHT-lezer: CircuitPython met auto-fallback naar Adafruit_DHT (uit main.py)."""
    circuit_error = None
    try:
        import adafruit_dht
        import board
 
        board_pin_name = f"D{bcm_pin}"
        if not hasattr(board, board_pin_name):
            raise RuntimeError(f"board.{board_pin_name} is niet beschikbaar op dit apparaat.")
        board_pin = getattr(board, board_pin_name)
 
        sensor_types = list(sensor_names) or ["DHT22"]
        current_index = 0
        failures = 0
        max_failures = 3
 
        def _make_sensor(sensor_type):
            if sensor_type == "DHT11":
                return adafruit_dht.DHT11(board_pin, use_pulseio=False)
            return adafruit_dht.DHT22(board_pin, use_pulseio=False)
 
        sensor_obj = _make_sensor(sensor_types[current_index])
 
        def _circuit_read():
            nonlocal sensor_obj, current_index, failures
            try:
                humidity    = sensor_obj.humidity
                temperature = sensor_obj.temperature
                if humidity is None or temperature is None:
                    raise RuntimeError("DHT gaf None terug")
                failures = 0
                return humidity, temperature
            except RuntimeError:
                failures += 1
                if failures >= max_failures:
                    failures = 0
                    current_index = (current_index + 1) % len(sensor_types)
                    try:
                        sensor_obj.exit()
                    except Exception:
                        pass
                    sensor_obj = _make_sensor(sensor_types[current_index])
                return None, None
 
        label = "|".join(sensor_types)
        return _circuit_read, f"adafruit-circuitpython-dht({label})", sensor_obj
 
    except Exception as err:
        circuit_error = err
 
    try:
        import Adafruit_DHT
        sensor_obj = getattr(Adafruit_DHT, sensor_names[0])
 
        def _legacy_read():
            return Adafruit_DHT.read_retry(sensor_obj, bcm_pin, retries=3, delay_seconds=1)
 
        return _legacy_read, "Adafruit_DHT", None
    except Exception as adafruit_error:
        raise RuntimeError(
            "Geen ondersteunde DHT-backend beschikbaar. "
            "Installeer adafruit-circuitpython-dht of Adafruit_DHT. "
            f"CircuitPython fout: {circuit_error}; "
            f"Adafruit_DHT fout: {adafruit_error}"
        )
 
# ─────────────────────────────────────────────
#  CONSTANTEN  (één plek om te wijzigen)
# ─────────────────────────────────────────────
# SPI bit-bang instellingen (Grove Sound via PmodAD1)
SOUND_CLK_PIN          = 16        # BOARD pin
SOUND_CS_PIN           = 12        # BOARD pin
SOUND_D0_PIN           = 36        # BOARD pin
SOUND_PIN_MODE         = "BOARD"
HALF_CLOCK_DELAY       = 0.00001   # seconden
VREF                   = 3.3       # volt
ADC_SAMPLE_ON_FALLING  = True
 
# Bewegingssensor (BCM 14 = BOARD 8)
MOTION_BCM_PIN = 14
 
# DHT-sensor (BOARD 7 = BCM 4)
DHT_SENSOR_TYPES = ["DHT22", "DHT11"]
DHT_PIN          = 7
DHT_PIN_MODE     = "BOARD"
 
DEFAULT_CITY = "Kortrijk"
DEFAULT_LAT   = 50.828
DEFAULT_LON   = 3.265
DEFAULT_WIND_BUITEN  = 10   # km/h
DEFAULT_VOCHT_BUITEN = 60   # % (fallback als API geen vocht geeft)
MAX_DROOGTIJD_UREN   = 24
MIN_DROOGTIJD_UREN   = 0.5
 
 
class LaundryApp(ctk.CTk):
 
    # ─────────────────────────────────────────
    #  KLASSE-CONSTANTEN
    # ─────────────────────────────────────────
    STOF_FACTOREN = {"Licht": 0.6, "Gemiddeld": 1.0, "Zwaar": 1.5}
 
    # Droogkast-basistijden in seconden (buiten/binnen worden live berekend)
    KAST_SECONDEN = {"Licht": 2700, "Gemiddeld": 4500, "Zwaar": 7200}
 
    KLEUREN = {
        "bg_dark":      "#1a1c2c",
        "bg_light":     "#f0f8ff",
        "accent_green": "#00d056",
        "accent_orange":"#ff9f00",
        "accent_red":   "#ff3b3b",
        "text_blue":    "#1e3a5f",
        "active_blue":  "#00daff",
    }
   
    # ─────────────────────────────────────────
    #  INIT
    # ─────────────────────────────────────────
    def __init__(self):
        super().__init__()
 
        self.title("Laundry Dashboard")
        self.geometry("800x480")
        self.attributes("-fullscreen", True)
 
        self.actieve_timers = []
        self.current_screen = None  # Cruciaal: dit voorkomt de AttributeError
        self.current_timer = None
        self.huidig_stoftype = "Gemiddeld"
        self.sidebar_buttons = {}
        self.sidebar_visible = True
        self.popup_time_label = None
        self._overlay_alpha = 0.0
        self._last_motion_time = time.monotonic()
        self._is_dimmed = False
        self._fade_after_id = None
        self._dim_overlay = None
 
        # Snelkoppelingen naar kleuren
        for k, v in self.KLEUREN.items():
            setattr(self, k, v)
 
        self.configure(fg_color=self.bg_light)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.actieve_timers = []  # Lijst met dictionaries: {'naam': ..., 'resterend': ..., 'totaal': ..., 'type': ...}
        self.update_timers_loop() # Start het tikken van de klok
 
        # ── State ──────────────────────────────
        self.huidig_stoftype = "Gemiddeld"
        self.current_timer   = None
        self.sidebar_buttons = {}
        self.sidebar_visible = True
        self.popup_time_label = None
 
        # ── GPIO + sensoren initialiseren (uit main.py) ────────────────
        self._gpio_clk  = None
        self._gpio_cs   = None
        self._gpio_d0   = None
        self._dht_read  = None      # callable: () -> (humidity, temperature)
        self._dht_obj   = None      # sensor-object voor exit() bij afsluiten
        self._last_temp = None
        self._last_hum  = None
        self._dht_error_reported = False
        self._next_dht_time = 0.0
 
        if ON_PI:
            try:
                GPIO.setmode(GPIO.BCM)
                GPIO.setwarnings(False)
 
                self._gpio_clk = resolve_bcm_pin(SOUND_CLK_PIN, SOUND_PIN_MODE)
                self._gpio_cs  = resolve_bcm_pin(SOUND_CS_PIN,  SOUND_PIN_MODE)
                self._gpio_d0  = resolve_bcm_pin(SOUND_D0_PIN,  SOUND_PIN_MODE)
 
                GPIO.setup(self._gpio_clk, GPIO.OUT, initial=GPIO.LOW)
                GPIO.setup(self._gpio_cs,  GPIO.OUT, initial=GPIO.HIGH)
                GPIO.setup(self._gpio_d0,  GPIO.IN,  pull_up_down=GPIO.PUD_DOWN)
                GPIO.setup(MOTION_BCM_PIN, GPIO.IN,  pull_up_down=GPIO.PUD_DOWN)
 
                self.gpio_available = True
                print(
                    f"GPIO: CLK=BCM{self._gpio_clk}, CS=BCM{self._gpio_cs}, "
                    f"D0=BCM{self._gpio_d0}, Motion=BCM{MOTION_BCM_PIN}"
                )
            except Exception as e:
                self.gpio_available = False
                print(f"GPIO niet beschikbaar: {e}")
 
            try:
                bcm_dht = resolve_bcm_pin(DHT_PIN, DHT_PIN_MODE)
                self._dht_read, backend, self._dht_obj = _build_dht_reader(
                    DHT_SENSOR_TYPES, bcm_dht
                )
                print(f"Sensor: DHT geïnitialiseerd via {backend} op BCM{bcm_dht}.")
            except Exception as e:
                self._dht_read = None
                print(f"DHT sensor niet beschikbaar: {e}")
        else:
            self.gpio_available = False
            print("Systeem: Geen Raspberry Pi – test-modus actief.")
 
        # Gecachte weerdata
        self.locatie = {"city": DEFAULT_CITY, "lat": DEFAULT_LAT, "lon": DEFAULT_LON}
        
        self.huidige_temp = "--°C"
        self.weer_code    = 0
 
        self.live_energieprijs = 0.28
        self.fetch_energy_prices()
 
        # ── Ham-knop aanmaken VÓÓR sidebar (fix crash) ──
        self.ham_btn = ctk.CTkButton(
            self, text="≡", width=40, height=40,
            fg_color=self.bg_dark, text_color="white",
            font=("Arial", 30), corner_radius=10, border_width=0,
            command=self.toggle_sidebar
        )
        
        self.METHODE_STYLING = {
            "Buiten": {"icoon": "🌲", "kleur": "#27ae60"},
            "Binnen": {"icoon": "🏠", "kleur": "#f39c12"},
            "Droger": {"icoon": "🌀", "kleur": "#e74c3c"}
        }
 
        # ── UI opbouwen ────────────────────────
        self.setup_sidebar()
        self._init_frames()
        self.setup_home_screen()
        self.setup_selection_screen()
        self.setup_bovenhoek()
        self.show_home()
 
        # ── Sluit-handler ──────────────────────
        self.protocol("WM_DELETE_WINDOW", self.on_closing)
        
 
        # ── Weerdata laden op achtergrond ───────
        threading.Thread(target=self._load_weather_async, daemon=True).start()

        # ── Motion fade loop ───────────────────
        self._init_dim_overlay()
        self._motion_fade_loop()
 
    def on_closing(self):
        """Veilig afsluiten: annuleer actieve timer en sluit sensoren."""
        if self.current_timer:
            self.after_cancel(self.current_timer)
        if self._dht_obj is not None:
            try:
                self._dht_obj.exit()
            except Exception:
                pass
        if ON_PI:
            try:
                pins = [p for p in [
                    self._gpio_clk, self._gpio_cs, self._gpio_d0, MOTION_BCM_PIN
                ] if p is not None]
                GPIO.cleanup(pins)
            except Exception:
                pass
        self.destroy()
 
    # ─────────────────────────────────────────
    #  WEER & LOCATIE  (gecentraliseerd)
    # ─────────────────────────────────────────
    def _load_weather_async(self):
        """Laad weer op achtergrond; update UI via after() zodat tkinter veilig blijft."""
        locatie   = self._fetch_location()
        weer_code = self._fetch_weather(locatie["lat"], locatie["lon"])
        # Terugkoppelen naar main thread
        self.after(0, lambda: self._apply_weather(locatie, weer_code))
 
    def _fetch_location(self) -> dict:
        try:
            with urlopen("http://ip-api.com/json/", timeout=5) as r:
                data = json.load(r)
                lat = data.get("lat", DEFAULT_LAT)
                lon = data.get("lon", DEFAULT_LON)
                city = data.get("city", DEFAULT_CITY)

                try:
                    geo_url = (
                        "https://geocoding-api.open-meteo.com/v1/reverse"
                        f"?latitude={lat}&longitude={lon}&count=1&language=nl"
                    )
                    geo_resp = requests.get(geo_url, timeout=5)
                    geo_data = geo_resp.json()
                    results = geo_data.get("results") or []
                    if results:
                        city = results[0].get("name", city)
                except Exception:
                    pass

                return {
                    "city": city,
                    "lat":  lat,
                    "lon":  lon,
                }
        except Exception as e:
            print(f"Locatie fout: {e}")
            return {"city": DEFAULT_CITY, "lat": DEFAULT_LAT, "lon": DEFAULT_LON}
 
    def _fetch_weather(self, lat, lon):
        try:
            url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current_weather=true&hourly=relative_humidity_2m,precipitation&forecast_days=3"
            response = requests.get(url, timeout=5)
            data = response.json()
        
            current = data["current_weather"]

            # Huidig uur bepalen zodat we het juiste uurvak pakken
            huidig_uur = datetime.now().hour

            return {
                "temp":     current["temperature"],
                "code":     int(current["weathercode"]),
                "humidity": data["hourly"]["relative_humidity_2m"][huidig_uur],
                "precip":   data["hourly"]["precipitation"][huidig_uur],
                "precip_uurlijks": data["hourly"]["precipitation"]
            }
        except Exception as e:
            print(f"Weather error: {e}")
            return {"temp": 15, "code": 0, "humidity": 60, "precip": 0, "precip_uurlijks": [0] * 72}

    def _apply_weather(self, locatie, weer_data):
        if isinstance(weer_data, dict):
            self.locatie = locatie
            self.weer_code = weer_data.get("code", 0)
            self.huidige_temp = f"{weer_data.get('temp', 15)}°C"
            self.huidige_vocht_buiten = weer_data.get("humidity", 60)
            self.huidige_neerslag = weer_data.get("precip", 0)
            self.neerslag_uurlijks = weer_data.get("precip_uurlijks", [0] * 72)
        else:
            self.weer_code = 0
            self.neerslag_uurlijks = [0] * 72
        # Update alleen de labels, herbouw NIET het hele scherm
        if hasattr(self, 'stad_label'):
            self.stad_label.configure(text=self.locatie.get("city", DEFAULT_CITY))
        if hasattr(self, 'weer_icon'):
            self.weer_icon.configure(text=self._weer_icon(self.weer_code))
 
    @staticmethod
    def _weer_icon(code: int) -> str:
        if code == 0:          return "☀️"
        if 1 <= code <= 3:     return "☁️"
        if code >= 51:         return "🌧️"
        return "⛅"
 
    # ─────────────────────────────────────────
    #  SENSOR (intern)
    # ─────────────────────────────────────────
    def read_pmodad1_bitbang(self) -> int:
        """Lees 12-bit sample van PmodAD1 (ADCS7476) via software SPI (uit main.py).
        16 bits worden uitgeschoven; bit-sampling op falling edge zoals in main.py.
        """
        value = 0
        GPIO.output(self._gpio_cs, GPIO.LOW)
        time.sleep(HALF_CLOCK_DELAY)
 
        for _ in range(16):
            GPIO.output(self._gpio_clk, GPIO.HIGH)
            time.sleep(HALF_CLOCK_DELAY)
            GPIO.output(self._gpio_clk, GPIO.LOW)
            time.sleep(HALF_CLOCK_DELAY)
            if ADC_SAMPLE_ON_FALLING:
                value = (value << 1) | int(GPIO.input(self._gpio_d0))
            else:
                GPIO.output(self._gpio_clk, GPIO.HIGH)
                time.sleep(HALF_CLOCK_DELAY)
                value = (value << 1) | int(GPIO.input(self._gpio_d0))
 
        GPIO.output(self._gpio_cs, GPIO.HIGH)
        return value & 0x0FFF
 
    def get_internal_sensor_data(self) -> dict:
        """Leest DHT temp/vocht (met caching), PmodAD1 geluidssensor en motion.
        DHT wordt max. eens per 2 seconden uitgelezen (timing uit main.py).
        Zonder Pi worden vaste testwaarden teruggegeven.
        """
        # --- Geluidssensor via PmodAD1 ---
        geluid = 0.0
        if self.gpio_available:
            try:
                raw_value = self.read_pmodad1_bitbang()
                geluid    = round((raw_value / 4095.0) * VREF, 3)
            except Exception as e:
                print(f"Fout bij uitlezen geluidssensor: {e}")

        # --- Bewegingssensor ---
        motion = 0
        if self.gpio_available:
            try:
                motion = int(GPIO.input(MOTION_BCM_PIN))
            except Exception as e:
                print(f"Fout bij uitlezen bewegingssensor: {e}")
 
        # --- DHT temperatuur & vochtigheid (gecached, max 1x per 2s) ---
        now = time.monotonic()
        if self._dht_read is not None and now >= self._next_dht_time:
            self._next_dht_time = now + 2.0
            try:
                hum, temp = self._dht_read()
                if hum is not None and temp is not None:
                    self._last_hum  = hum
                    self._last_temp = temp
            except Exception as e:
                if not self._dht_error_reported:
                    print(f"DHT driver fout: {e}")
                    self._dht_error_reported = True
 
        # Gebruik gecachte waarden; valt terug op veilige standaardwaarden
        temp  = self._last_temp if self._last_temp is not None else 15.0
        vocht = self._last_hum  if self._last_hum  is not None else 50.0
 
        return {
            "temp":   round(temp,  1),
            "vocht":  round(vocht, 1),
            "geluid": geluid,
            "motion": motion,
        }

    def _read_motion_state(self):
        if not self.gpio_available:
            return None
        try:
            return int(GPIO.input(MOTION_BCM_PIN))
        except Exception:
            return None

    def _init_dim_overlay(self):
        if self._dim_overlay is not None:
            return
        self._dim_overlay = ctk.CTkToplevel(self)
        self._dim_overlay.overrideredirect(True)
        self._dim_overlay.attributes("-fullscreen", True)
        self._dim_overlay.attributes("-topmost", True)
        self._dim_overlay.attributes("-alpha", 0.0)
        self._dim_overlay.configure(fg_color="black")
        self._dim_overlay.withdraw()

    def _fade_to(self, target_alpha, steps=30, step_ms=100):
        if self._fade_after_id is not None:
            try:
                self.after_cancel(self._fade_after_id)
            except Exception:
                pass
            self._fade_after_id = None

        if self._dim_overlay is None:
            return

        if target_alpha > 0 and not self._dim_overlay.winfo_viewable():
            self._dim_overlay.deiconify()
            self._dim_overlay.lift()

        start_alpha = self._overlay_alpha
        delta = (target_alpha - start_alpha) / max(1, steps)

        def _step(i=1):
            new_alpha = start_alpha + delta * i
            self._overlay_alpha = new_alpha
            self._dim_overlay.attributes("-alpha", new_alpha)
            if i < steps:
                self._fade_after_id = self.after(step_ms, _step, i + 1)
            else:
                self._fade_after_id = None
                if target_alpha <= 0:
                    self._dim_overlay.withdraw()

        _step()

    def _motion_fade_loop(self):
        if not self.gpio_available:
            if self._overlay_alpha != 0.0:
                self._fade_to(0.0)
            self.after(1000, self._motion_fade_loop)
            return

        motion_state = self._read_motion_state()
        if motion_state == 1:
            self._last_motion_time = time.monotonic()

        idle_time = time.monotonic() - self._last_motion_time
        if idle_time >= 30 and not self._is_dimmed:
            self._fade_to(1.0)
            self._is_dimmed = True
        elif idle_time < 30 and self._is_dimmed:
            self._fade_to(0.0)
            self._is_dimmed = False

        self.after(500, self._motion_fade_loop)
    
    def fetch_energy_prices(self):
        try:
            # Energy-Charts API voor België
            url = "https://api.energy-charts.info/price?country=be" 
            response = requests.get(url, timeout=5)
            data = response.json()
        
            # We halen de huidige tijd in Unix seconden op
            now_ts = time.time()
            actuele_prijs = None

            # Energy-Charts geeft een lijst met timestamps en een lijst met prijzen
            # We zoeken de index van het huidige uur
            for i, timestamp in enumerate(data['unix_seconds']):
                # Als de huidige tijd tussen deze timestamp en de volgende (1 uur later) ligt
                if timestamp <= now_ts < (timestamp + 3600):
                    # De prijs is in Euro/MWh, dus we delen door 1000 voor kWh
                    actuele_prijs = data['price'][i] / 1000
                    break

            if actuele_prijs is not None:
                # Optioneel: Belgische BTW (6%) toevoegen
                self.live_energieprijs = round(actuele_prijs * 1.06, 4)
                print(f"Systeem: Live energieprijs bijgewerkt naar €{self.live_energieprijs} per kWh (incl. BTW)")
            else:
                raise ValueError("Geen prijs gevonden voor het huidige tijdstip")

        except Exception as e:
            print(f"Fout bij ophalen prijs: {e}")
            self.live_energieprijs = 0.28  # Veilige backup
    # ─────────────────────────────────────────
    #  DROOGTIJD BEREKENING
    # ─────────────────────────────────────────
    def bereken_droogtijd(
        self,
        temp: float,
        vocht: float,
        wind: float = 0,
        is_buiten: bool = True,
        stof_type: str = "Gemiddeld",
    ) -> float:
        basis_min   = 240
        stof_factor = self.STOF_FACTOREN.get(stof_type, 1.0)
        temp_factor = max(0.5, min(2.0, 1 - (temp - 20) * 0.05))  # Nu ook bovengeklemd
        vocht_factor = 1.0 if vocht < 60 else 1 + (vocht - 60) * 0.04
        wind_factor  = max(0.6, 1 - wind * 0.02) if is_buiten else 1.0
 
        uren = (basis_min * stof_factor * temp_factor * vocht_factor * wind_factor) / 60
        return round(max(MIN_DROOGTIJD_UREN, min(MAX_DROOGTIJD_UREN, uren)), 1)
 
    def _bereken_alle_tijden(self, was_type: str) -> tuple[int, int, int]:
        """Geeft (sec_buiten, sec_binnen, sec_kast) terug."""
        try:
            # Haal alleen het getal uit de temperatuur string
            temp_buiten = float("".join(filter(lambda x: x in "0123456789.-", str(self.huidige_temp))))
        except (ValueError, AttributeError):
            temp_buiten = 15.0

        binnen = self.get_internal_sensor_data()

        # Gebruik live vochtigheid of fallback naar 60%
        vocht_buiten = getattr(self, 'huidige_vocht_buiten', 60)

        # 1. Buiten berekenen
        sec_buiten = int(self.bereken_droogtijd(
            temp_buiten, vocht_buiten, 
            wind=DEFAULT_WIND_BUITEN, is_buiten=True, stof_type=was_type
        ) * 3600)
        
        # Als het regent of gaat regenen, zet de tijd op "onmogelijk" (999 uur)
        # Zo wordt het in bepaal_beste_optie direct naar de laatste plek verwezen.
       

        # 2. Binnen berekenen
        sec_binnen = int(self.bereken_droogtijd(
            binnen["temp"], binnen["vocht"],
            wind=0, is_buiten=False, stof_type=was_type
        ) * 3600)
    
        # 3. Droogkast berekenen (DE FIX VOOR DE KEYERROR)
        # We halen het woord ' was' weg als dat erachter staat (bijv. "Licht was" -> "Licht")
        zoek_term = was_type.replace(" was", "").strip()
        
        # Gebruik .get() zodat het programma nooit meer crasht op een naamfout
        sec_kast = self.KAST_SECONDEN.get(zoek_term, self.KAST_SECONDEN.get("Gemiddeld", 4500))

        return sec_buiten, sec_binnen, sec_kast

        return sec_buiten, sec_binnen, sec_kast
    def add_timer(self, methode, was_type, seconden):
        # Voeg een nieuwe timer toe aan de lijst
        self.actieve_timers.append({
            "methode": methode,
            "was_type": was_type,
            "resterend": seconden,
            "totaal": seconden
        })
 
    def update_timers_loop(self):
        for timer in self.actieve_timers:
            if timer["resterend"] > 0:
                timer["resterend"] -= 1
        
        # Gebruik hasattr() als extra veiligheid[cite: 1]
        if getattr(self, "current_screen", None) == "timers":
            self.refresh_timer_display()
            
        self.after(1000, self.update_timers_loop)
 
    def refresh_timer_display(self):
        # Alleen uitvoeren als we op het timer-scherm zijn en de elementen bestaan
        if self.current_screen == "timers" and hasattr(self, 'timer_ui_elements'):
            for i, timer in enumerate(self.actieve_timers):
                if i in self.timer_ui_elements:
                    # Bereken voortgang
                    procent = (timer["totaal"] - timer["resterend"]) / timer["totaal"]
                    tijd_str = f"{timer['resterend']//3600:02d}:{(timer['resterend']%3600)//60:02d}:{timer['resterend']%60:02d}"
                    
                    # Update de bestaande widgets (GEEN destroy!)
                    try:
                        self.timer_ui_elements[i]["pb"].set(procent)
                        self.timer_ui_elements[i]["tijd"].configure(text=tijd_str)
                    except Exception:
                        self.timer_ui_elements = {}
                        break

    def bepaal_beste_optie(self):
        """Analyseert data volgens strikte hiërarchie: Buiten > Binnen > Droger."""
        binnen_data = self.get_internal_sensor_data()
        vocht_binnen = binnen_data["vocht"]
    
        # Haal de berekende tijden op (deze houden al rekening met de nieuwe API data)
        sec_buiten, sec_binnen, sec_kast = self._bereken_alle_tijden(self.huidig_stoftype)
    
        # 1. Controleer of BUITEN mogelijk is
        # Regels: niet regenen (weercode < 51), geen neerslag in API, en tijd < 10 uur
        is_aan_het_regenen = self.weer_code >= 51

        huidig_uur = datetime.now().hour
        droogtijd_uur = max(1, round(sec_buiten / 3600))
        neerslag_lijst = getattr(self, 'neerslag_uurlijks', [0] * 72)
        interval_einde = min(huidig_uur + droogtijd_uur, len(neerslag_lijst))
        gaat_regenen = any(n > 0 for n in neerslag_lijst[huidig_uur:interval_einde])

        tijd_ok_buiten = (sec_buiten / 3600) <= 10
        buiten_mogelijk = not is_aan_het_regenen and not gaat_regenen and tijd_ok_buiten

        # 2. Controleer of BINNEN mogelijk is
        # Regels: vochtigheid niet te hoog (< 65%) en tijd < 15 uur
        tijd_ok_binnen = (sec_binnen / 3600) <= 15
        vocht_ok_binnen = vocht_binnen < 65
    
        binnen_mogelijk = tijd_ok_binnen and vocht_ok_binnen

        # 3. Rangschikking bepalen volgens jouw voorkeur
        # We gebruiken een hele hoge score (strafpunten) om opties die niet mogen te blokkeren
        scores = []

        # Buiten is voorkeur 1
        if buiten_mogelijk:
            scores.append(("Buiten", 1)) # Laagste score = hoogste prioriteit
        else:
            scores.append(("Buiten", 999999)) # Wordt nooit aanbevolen

        # Binnen is voorkeur 2
        if binnen_mogelijk:
            scores.append(("Binnen", 2))
        else:
            scores.append(("Binnen", 888888)) # Wordt nooit aanbevolen

        # Droger is voorkeur 3 (altijd mogelijk als noodoplossing)
        scores.append(("Droger", 3))

        # Sorteer de lijst
        gerangschikt = sorted(scores, key=lambda x: x[1])
    
        # Filter de gerangschikte lijst zodat we de 'onmogelijke' opties niet tonen
        # or onderaan zetten in de UI.
        return gerangschikt
    # ─────────────────────────────────────────
    #  SIDEBAR
    # ─────────────────────────────────────────
    def setup_sidebar(self):
        self.sidebar = ctk.CTkFrame(self, width=80, corner_radius=0, fg_color=self.bg_dark)
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_propagate(False)
 
        ctk.CTkButton(
            self.sidebar, text="≡", width=40, height=40,
            fg_color=self.bg_dark, hover_color="#2d2f3f",
            text_color="white", font=("Arial", 32),
            corner_radius=10, border_width=0, border_spacing=0,
            command=self.toggle_sidebar
        ).pack(side="top", anchor="n", pady=(20, 10))
 
        menu_items = [
            ("✧", "home",    self.show_home),
            ("⚡", "energy", None),
            ("⚖", "balance", self.show_comparison),
            ("⌛", "timer",  self.show_timers_screen),
            ("⚙", "settings", self.show_debug_info),
        ]
        for icon, name, actie in menu_items:
            btn = ctk.CTkButton(
                self.sidebar, text=icon, width=40, height=40,
                fg_color="transparent", hover_color="#2d2f3f",
                text_color="white", font=("Arial", 24),
                command=actie if actie else lambda: None
            )
            pady_val = (30, 15) if name == "home" else 15
            btn.pack(pady=pady_val, padx=10)
            self.sidebar_buttons[name] = btn
 
    def toggle_sidebar(self):
        if self.sidebar_visible:
            self.sidebar.grid_forget()
            self.sidebar_visible = False
            self.ham_btn.place(x=20, y=20)
            self.ham_btn.lift()
        else:
            self.ham_btn.place_forget()
            self.sidebar.grid(row=0, column=0, sticky="nsew")
            self.sidebar_visible = True
 
    def update_sidebar_selection(self, active_name):
        for name, btn in self.sidebar_buttons.items():
            color = self.active_blue if name == active_name else "white"
            btn.configure(text_color=color)
 
    # ─────────────────────────────────────────
    #  BOVENHOEK (weer-widget)
    # ─────────────────────────────────────────
    def setup_bovenhoek(self):
        """Bouwt het widget in een horizontale lijn met een verticale divider."""
        # Weerframe aanpassen: transparant of wit (jouw keuze), relx/rely zoals voorheen
        self.weer_frame = ctk.CTkFrame(self, fg_color="white", corner_radius=15)
        self.weer_frame.place(relx=0.97, rely=0.03, anchor="ne") # Iets hoger gezet (0.03) voor ademruimte

        # 1. Weer-icoon
        self.weer_icon = ctk.CTkLabel(
            self.weer_frame, text="☀️", # Placeholder zonnetje
            font=("Arial", 24), text_color=self.bg_dark
        )
        self.weer_icon.pack(side="left", padx=(15, 0), pady=8)

        # 2. Stad Label
        self.stad_label = ctk.CTkLabel(
            self.weer_frame, text="Laden…",
            font=("Arial Bold", 16), text_color=self.bg_dark
        )
        self.stad_label.pack(side="left", padx=(0,0))

        # 3. Verticale Divider (Het streepje)
        divider = ctk.CTkFrame(self.weer_frame, width=2, height=20, fg_color="#ccc")
        divider.pack(side="left", padx=10)

        # 4. Tijd Label
        # Format aangepast naar je foto: "HH:MM zo d/m"
        nu_tijd = datetime.now().strftime("%H:%M %a %d/%m").lower()
        self.tijd_label = ctk.CTkLabel(
            self.weer_frame, text=nu_tijd,
            font=("Arial Bold", 16), text_color=self.bg_dark
        )
        self.tijd_label.pack(side="left", padx=(5, 15))
 
    # ─────────────────────────────────────────
    #  FRAMES INITIALISEREN
    # ─────────────────────────────────────────
    def _init_frames(self):
        self.home_frame     = ctk.CTkFrame(self, fg_color="transparent")
        self.selection_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.drying_frame   = ctk.CTkFrame(self, fg_color="transparent")
        self.confirm_frame  = ctk.CTkFrame(self, fg_color="transparent")
        self.timer_frame    = ctk.CTkFrame(self, fg_color="transparent")
        self.compare_frame  = ctk.CTkFrame(self, fg_color="transparent")
 
    # ─────────────────────────────────────────
    #  NAVIGATIE HELPERS
    # ─────────────────────────────────────────
    def hide_all(self):
        if self.current_timer:
            self.after_cancel(self.current_timer)
            self.current_timer = None
        for f in [
            self.home_frame, self.selection_frame, self.drying_frame,
            self.confirm_frame, self.timer_frame, self.compare_frame,
        ]:
            f.grid_forget()
 
    def show_home(self):
        self.hide_all()
        # Verwijder oude widgets zodat de nieuwe setup_home_screen alles vers tekent
        for widget in self.home_frame.winfo_children():
            widget.destroy()
        self.setup_home_screen()
        self.home_frame.grid(row=0, column=1, sticky="nsew")
        self.update_sidebar_selection("home")

    def show_selection(self):
        self.hide_all()
        self.selection_frame.grid(row=0, column=1, sticky="nsew")
        self.update_sidebar_selection(None)
 
    def show_comparison(self):
        self.hide_all()
        for w in self.compare_frame.winfo_children():
            w.destroy()
        self.build_comparison_ui()
        self.compare_frame.grid(row=0, column=1, sticky="nsew")
        self.update_sidebar_selection("balance")
 
    # ─────────────────────────────────────────
    #  SCHERM 1 – HOME
    # ─────────────────────────────────────────
    def setup_home_screen(self):

        # 1. Haal de gerangschikte lijst op
        ranking = self.bepaal_beste_optie()
    
        # De winnaar is altijd het eerste item in de lijst: (methode, score)
        beste_methode = ranking[0][0]
    
        # 2. Kies icoon, tekst en de 'waarom'-uitleg
        # We maken hier een kleine dictionary voor de teksten
        uitleg_map = {
            "Buiten": {
                "text": "Hang de was buiten",
                "icon": "🌲",
                "waarom": "Het zonnetje schijnt! Ideaal om gratis buiten te drogen."
            },
            "Binnen": {
                "text": "Hang de was binnen",
                "icon": "🏠",
                "waarom": f"Buiten is niet optimaal, maar binnen is de vochtigheid momenteel prima."
            },
            "Droger": {
                "text": "Steek de was in de droogkast",
                "icon": "🌀",
                "waarom": f"Het regent of de luchtvochtigheid is te hoog. Huidige stroomprijs: €{self.live_energieprijs:.4f}/kWh."
            }
        }

        display_data = uitleg_map.get(beste_methode)
    
        # 3. UI elementen opbouwen
        # Gebruik de data uit de map
        ctk.CTkLabel(
            self.home_frame, text=display_data["icon"], font=("Arial", 120),
            text_color=self.accent_green
        ).pack(expand=True, pady=(60, 0))

        ctk.CTkLabel(
            self.home_frame, text=display_data["text"],
            font=("Arial Bold", 42), text_color="black"
        ).pack(expand=True)

        # Waarom-tekst
        ctk.CTkLabel(
            self.home_frame, text=display_data["waarom"],
            font=("Arial", 18), text_color="#4a5568"
        ).pack(expand=True, pady=(0, 20))

        # Knoppen
        btn_row = ctk.CTkFrame(self.home_frame, fg_color="transparent")
        btn_row.pack(expand=True, pady=(0, 60))

        ctk.CTkButton(
            btn_row, text="TIMER INSTELLEN",
            fg_color=self.accent_green, hover_color="#00b34a", text_color="white",
            height=60, width=150, corner_radius=15, font=("Arial Bold", 18),
            command=self.show_selection
        ).pack(side="left", padx=10)

        ctk.CTkButton(
            btn_row, text="VERGELIJKING",
            fg_color="#4a5568", hover_color="#2d3748", text_color="white",
            height=60, width=150, corner_radius=15, font=("Arial Bold", 18),
            command=self.show_comparison
        ).pack(side="left", padx=10)
 
    # ─────────────────────────────────────────
    #  SCHERM 2 – SELECTIE WAS-SOORT
    # ─────────────────────────────────────────
    def setup_selection_screen(self):
        ctk.CTkButton(
            self.selection_frame, text="←", width=40, height=40,
            fg_color="white", text_color="black", command=self.show_home
        ).place(relx=0.05, rely=0.05)
 
        ctk.CTkLabel(
            self.selection_frame,
            text="Voor welk type was wil je een timer zetten?",
            font=("Arial Bold", 32), text_color="black"
        ).pack(pady=(60, 40))
 
        container = ctk.CTkFrame(self.selection_frame, fg_color="transparent")
        container.pack(expand=True, fill="both", padx=40)
        container.grid_columnconfigure((0, 1, 2), weight=1)
 
        cards = [
            ("Licht",    "#cce0ff", "black", "🪶", "#bbd6ff"),
            ("Gemiddeld","#66a3ff", "white", "👕", "#5594ff"),
            ("Zwaar",    "#1a66ff", "white", "👖", "#0052cc"),
        ]
        for i, (label, color, t_col, icon, hover_c) in enumerate(cards):
            btn = ctk.CTkButton(
                container, fg_color=color, hover_color=hover_c,
                corner_radius=25, height=250, text="",
                command=lambda l=label: self.show_drying_options(l)
            )
            btn.grid(row=0, column=i, sticky="nsew", padx=15)
            btn.grid_columnconfigure(0, weight=1)
 
            l1 = ctk.CTkLabel(btn, text=icon, font=("Arial Bold", 80), text_color=t_col)
            l1.grid(row=0, column=0, pady=(40, 0))
            l2 = ctk.CTkLabel(btn, text=label, font=("Arial Bold", 22), text_color=t_col)
            l2.grid(row=1, column=0, pady=(10, 40))
 
            for lbl in [l1, l2]:
                lbl.bind("<Button-1>", lambda e, b=btn: b.invoke())
 
    # ─────────────────────────────────────────
    #  SCHERM 3 – DROOGOPTIES
    # ─────────────────────────────────────────
    def show_drying_options(self, was_type: str):
        self.hide_all()
        self.huidig_stoftype = was_type
        
        for w in self.drying_frame.winfo_children():
            w.destroy()
        
        # Terug-knop
        ctk.CTkButton(
            self.drying_frame, text="←", width=40, height=40,
            fg_color="white", text_color="black", command=self.show_selection
        ).place(relx=0.05, rely=0.05)

        # Titel
        ctk.CTkLabel(
            self.drying_frame,
            text=f"Kies een methode voor {was_type} was",
            font=("Arial Bold", 32), text_color="black"
        ).pack(pady=(60, 40))

        # --- DATA & RANKING OPHALEN ---
        sec_buiten, sec_binnen, sec_kast = self._bereken_alle_tijden(was_type)
        ranking = self.bepaal_beste_optie() # Dit geeft je [("Naam", score), ...]
        
        # Maak een lijst van namen gesorteerd op beste keuze (laagste score eerst)
        ranking_namen = [r[0] for r in ranking]

        def fmt(s: int) -> str:
            # Als de tijd de strafscore heeft (99u of meer), toon "Niet mogelijk"
            
            return f"~{s // 3600}u {(s % 3600) // 60}m"

        # Kleuren toewijzen op basis van RANK (0=Groen, 1=Oranje, 2=Rood)
        rank_kleuren_map = [
            (self.accent_green, "#00b34a"),  # Beste keuze
            (self.accent_orange, "#e68f00"), # Tweede keuze
            (self.accent_red, "#e63535")     # Derde keuze
        ]

        # VASTE VOLGORDE voor de knoppen op het scherm
        vaste_volgorde = ["Buiten", "Binnen", "Droger"]
        data_map = {
            "Buiten": {"icon": "🌲", "sec": sec_buiten},
            "Binnen": {"icon": "🏠", "sec": sec_binnen},
            "Droger": {"icon": "🌀", "sec": sec_kast}
        }

        # Container bouwen
        container = ctk.CTkFrame(self.drying_frame, fg_color="transparent")
        container.pack(expand=True, fill="both", padx=40)
        container.grid_columnconfigure((0, 1, 2), weight=1)

        # Knoppen tekenen in de vaste volgorde
        for i, methode in enumerate(vaste_volgorde):
            info = data_map[methode]
            
            # Bepaal kleur: kijk waar de methode staat in de gesorteerde ranking_namen lijst
            rank_pos = ranking_namen.index(methode)
            kleur, h_kleur = rank_kleuren_map[rank_pos]

            btn = ctk.CTkButton(
                container, fg_color=kleur, hover_color=h_kleur,
                corner_radius=25, height=250, text="",
                command=lambda l=methode, s=info["sec"], k=kleur: self.show_confirmation(was_type, l, s, k)
            )
            btn.grid(row=0, column=i, sticky="nsew", padx=15)
            btn.grid_columnconfigure(0, weight=1)

            # Icon en Labels
            l1 = ctk.CTkLabel(btn, text=info["icon"], font=("Arial Bold", 70), text_color="white")
            l1.grid(row=0, column=0, pady=(30, 0))
            
            l2 = ctk.CTkLabel(btn, text=methode, font=("Arial Bold", 22), text_color="white")
            l2.grid(row=1, column=0, pady=(5, 0))
            
            l3 = ctk.CTkLabel(btn, text=fmt(info["sec"]), font=("Arial Bold", 18), text_color="white")
            l3.grid(row=2, column=0, pady=(0, 30))

            # Zorg dat klikken op tekst ook de knop activeert
            for lbl in [l1, l2, l3]:
                lbl.bind("<Button-1>", lambda e, b=btn: b.invoke())

        self.drying_frame.grid(row=0, column=1, sticky="nsew")
    # ─────────────────────────────────────────
    #  SCHERM 4 – BEVESTIGING
    # ─────────────────────────────────────────
    def show_confirmation(self, was_type: str, methode: str, seconden: int, kleur: str):
        self.hide_all()
        for w in self.confirm_frame.winfo_children():
            w.destroy()

        # Terug-knop
        ctk.CTkButton(
            self.confirm_frame, text="←", width=40, height=40,
            fg_color="white", text_color="black",
            command=lambda: self.show_drying_options(was_type)
        ).place(relx=0.05, rely=0.05)

        # Icoon bepalen (kleur komt uit het argument)
        icon = {"Buiten": "🌲", "Binnen": "🏠"}.get(methode, "🌀")
    
        # Dynamische hover kleur: 
        # We maken de hover kleur een tikkeltje donkerder dan de meegegeven kleur
        # Of we gebruiken een simpele logica:
        h_col = "#00b34a" if kleur == self.accent_green else "#e68f00" if kleur == self.accent_orange else "#e63535"

        # UI Elementen
        # Grote icoon gebruikt nu de 'kleur' van de ranking (Groen als het de beste is!)
        ctk.CTkLabel(
            self.confirm_frame, text=icon,
            font=("Arial Bold", 120), text_color=kleur
        ).pack(pady=(40, 10))

        ctk.CTkLabel(
            self.confirm_frame,
            text=f"{was_type} was {methode.lower()} drogen",
            font=("Arial Bold", 36), text_color=self.text_blue
        ).pack()

        t_txt = f"~{seconden // 3600}u {(seconden % 3600) // 60}m"
        ctk.CTkLabel(
            self.confirm_frame,
            text=f"Verwachte droogtijd: {t_txt}",
            font=("Arial Bold", 22), text_color="black"
        ).pack(pady=20)

        # De BEVESTIGEN knop krijgt ook de kleur van de ranking
        ctk.CTkButton(
            self.confirm_frame, text="START TIMER",
            fg_color=kleur, hover_color=h_col, height=70, width=500,
            corner_radius=20, font=("Arial Bold", 24), text_color="white",
            command=lambda: self.start_timer(was_type, methode, seconden)
        ).pack(pady=40)

        self.confirm_frame.grid(row=0, column=1, sticky="nsew")
        
 
    # ─────────────────────────────────────────
    #  SCHERM 5 – TIMER
    # ─────────────────────────────────────────
    def start_timer(self, was_type: str, methode: str, seconden: int):
        self.hide_all()
        for w in self.timer_frame.winfo_children():
            w.destroy()

        # Grote tikkende klok
        self.time_label = ctk.CTkLabel(
            self.timer_frame, text="",
            font=("Arial Bold", 80), text_color=self.text_blue
        )
        self.time_label.pack(pady=(80, 10))

        # --- ÉÉN GROOT WIT VAK (Tabel layout) ---
        table_frame = ctk.CTkFrame(self.timer_frame, fg_color="white", corner_radius=20)
        table_frame.pack(fill="both", expand=True, padx=80, pady=20)
        
        # Grid configuratie voor de tabel
        table_frame.grid_columnconfigure(0, weight=1)
        table_frame.grid_columnconfigure(1, weight=1)

        def add_table_row(row, label_left, value_left, label_right=None, value_right=None, is_last=False):
            # Linker kolom data
            ctk.CTkLabel(table_frame, text=f"{label_left} {value_left}", 
                         font=("Arial Bold", 18), text_color="black").grid(row=row, column=0, pady=15)
            
            # Rechter kolom data (indien aanwezig)
            if label_right:
                ctk.CTkLabel(table_frame, text=f"{label_right} {value_right}", 
                             font=("Arial Bold", 18), text_color="black").grid(row=row, column=1, pady=15)
            
            # Teken een scheidingslijn als het niet de laatste rij is
            if not is_last:
                line = ctk.CTkFrame(table_frame, height=1, fg_color="#E0E0E0")
                line.grid(row=row, column=0, columnspan=2, sticky="swe", padx=20)

        # Voorbereiden van de data
        icon_m = {"Buiten": "🌲", "Binnen": "🏠"}.get(methode, "🌀")
        icon_w = {"Licht": "🪶", "Gemiddeld": "👕"}.get(was_type.replace(" was", ""), "👖")
        
        # Rij 1: Methode en Was-type
        add_table_row(0, icon_m, methode, icon_w, was_type)

        # Rij 2: Sensordata (Live gecorrigeerd)
        if methode == "Buiten":
            neerslag = getattr(self, 'huidige_neerslag', 0)
            regen_status = "Geen regen" if neerslag == 0 else f"{neerslag}mm regen"
            add_table_row(1, "☀️", self.huidige_temp, "🌧️", regen_status, is_last=True)
        else:
            binnen = self.get_internal_sensor_data()
            add_table_row(1, "🌡️", f"{binnen['temp']}°C", "💧", f"{binnen['vocht']}%", is_last=True)

        # Annuleer knop onder het witte vak
        ctk.CTkButton(
            self.timer_frame, text="ANNULEREN",
            fg_color="#4a5568", hover_color="#2d3748",
            height=50, width=400, corner_radius=15,
            font=("Arial Bold", 18), text_color="white",
            command=lambda: self.confirm_cancel(was_type, methode)
        ).pack(pady=(10, 30))

        # Timer starten
        self.remaining_sec = seconden
        self.add_timer(methode, was_type, seconden)
        self._tick()
        self.timer_frame.grid(row=0, column=1, sticky="nsew")
 
    def _tick(self):
        """Eén tick per seconde; stopt netjes op 0."""
        if self.remaining_sec > 0:
            h = self.remaining_sec // 3600
            m = (self.remaining_sec % 3600) // 60
            s = self.remaining_sec % 60
            tijd_str = f"{h:02d}:{m:02d}:{s:02d}"
            self.time_label.configure(text=tijd_str)
            if (
                self.popup_time_label is not None
                and self.popup_time_label.winfo_exists()
            ):
                self.popup_time_label.configure(text=tijd_str)
            self.remaining_sec -= 1
            # Sync met actieve_timers zodat het timerscherm klopt
            if self.actieve_timers:
                self.actieve_timers[-1]["resterend"] = self.remaining_sec
            self.current_timer = self.after(1000, self._tick)
        else:
            self.time_label.configure(text="00:00:00")
            # Timer klaar: verwijder uit de lijst
            if self.actieve_timers:
                self.actieve_timers.pop()
            self._timer_klaar()
 
    def _timer_klaar(self):
        """Wordt aangeroepen wanneer de timer op 0 komt."""
        # Sluit eventuele popup
        if hasattr(self, "overlay") and self.overlay.winfo_exists():
            self.close_popup()
        self.show_home()
 
    # ─────────────────────────────────────────
    #  ANNULEER-POPUP
    # ─────────────────────────────────────────
    def confirm_cancel(self, was_type: str, methode: str):
        self.overlay = ctk.CTkFrame(self, fg_color="#2b2b2b")
        self.overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
 
        popup = ctk.CTkFrame(self.overlay, fg_color="white",
                             corner_radius=25, width=500, height=380)
        popup.place(relx=0.5, rely=0.5, anchor="center")
        popup.pack_propagate(False)
 
        ctk.CTkLabel(popup, text="Timer annuleren?",
                     font=("Arial Bold", 20), text_color="black").pack(pady=(30, 5))
        self.popup_time_label = ctk.CTkLabel(
            popup, text="", font=("Arial Bold", 32), text_color="black"
        )
        self.popup_time_label.pack(pady=10)
 
        ctk.CTkButton(popup, text="STOP TIMER",
                      fg_color=self.accent_red, height=60, width=400,
                      text_color="white", corner_radius=15,
                      command=self.final_cancel).pack(pady=10)
        ctk.CTkButton(popup, text="GA TERUG",
                      fg_color="#4a5568", height=60, width=400,
                      text_color="white", corner_radius=15,
                      command=self.close_popup).pack(pady=10)
 
    def close_popup(self):
        self.popup_time_label = None
        self.overlay.destroy()
 
    def final_cancel(self):
        self.close_popup()
        self.show_home()
 
    def show_timers_screen(self):
        self.hide_all()
        self.current_screen = "timers"
        self.timer_ui_elements = {} # Reset de referenties voor de nieuwe widgets

        for w in self.timer_frame.winfo_children(): 
            w.destroy()

        # Titel
        ctk.CTkLabel(self.timer_frame, text="Timers:", font=("Arial Bold", 32), text_color="black").pack(pady=(80, 10))

        # Check of de lijst leeg is[cite: 1]
        if not self.actieve_timers:
            ctk.CTkLabel(self.timer_frame, text="Nog geen timers ingesteld.", font=("Arial", 20), text_color="#4a5568").pack(expand=True)
        else:
            scroll = ctk.CTkScrollableFrame(self.timer_frame, fg_color="transparent", width=700, height=350)
            scroll.pack(expand=True, fill="both", padx=50, pady=20)

            # Bouw de widgets voor ELKE actieve timer opnieuw op[cite: 1]
            for i, timer in enumerate(self.actieve_timers):
                stijl = self.METHODE_STYLING.get(timer["methode"], {"icoon": "⏳", "kleur": "gray"})
                
                card = ctk.CTkFrame(scroll, fg_color="white", corner_radius=20, height=100)
                card.pack(fill="x", pady=10, padx=10)
                card.pack_propagate(False)

                ctk.CTkLabel(card, text=stijl["icoon"], font=("Arial", 40), text_color=stijl["kleur"]).pack(side="left", padx=20)
                
                info_text = f"{timer['methode']} - {timer['was_type']}"
                ctk.CTkLabel(card, text=info_text, font=("Arial Bold", 16), text_color="black").pack(side="left")

                pb = ctk.CTkProgressBar(card, width=200, progress_color=stijl["kleur"])
                pb.pack(side="left", padx=20)
                
                # Bereken start-progressie[cite: 1]
                progress = (timer["totaal"] - timer["resterend"]) / timer["totaal"]
                pb.set(progress)
                
                lbl_tijd = ctk.CTkLabel(card, text="00:00:00", font=("Consolas", 20, "bold"), text_color="black")
                lbl_tijd.pack(side="right", padx=20)

                # Sla de referenties op zodat de achtergrond-loop ze kan vinden[cite: 1]
                self.timer_ui_elements[i] = {"pb": pb, "tijd": lbl_tijd}
            
        self.timer_frame.grid(row=0, column=1, sticky="nsew")
        self.update_sidebar_selection("timer")
        # Update direct de tekst van de klokjes[cite: 1]
        self.refresh_timer_display()
 
    # ─────────────────────────────────────────
    #  SCHERM 6 – VERGELIJKING
    # ─────────────────────────────────────────
    def build_comparison_ui(self):
        # 1. Check of de frame wel bestaat
        if not hasattr(self, 'compare_frame') or self.compare_frame is None:
            return

        # 2. Verwijder oude inhoud
        for widget in self.compare_frame.winfo_children():
            widget.destroy()

        # 3. Basis layout
        inner = ctk.CTkFrame(self.compare_frame, fg_color="#f0f8ff", corner_radius=0)
        inner.pack(fill="both", expand=True)
        inner.grid_columnconfigure((0, 1, 2), weight=1)

        # 4. Data ophalen
        binnen = self.get_internal_sensor_data()
        v_buiten = getattr(self, 'huidige_vocht_buiten', 60)
        neerslag = getattr(self, 'huidige_neerslag', 0)
        
        ranking = self.bepaal_beste_optie()
        ranking_namen = [r[0] for r in ranking]
        
        # Kleuren toewijzen op basis van de NIEUWE rangschikking
        # De nummer 1 (ranking_namen[0]) krijgt altijd groen, mits niet 'onmogelijk'
        rank_kleuren = {}
        for naam, score in ranking:
            
            if naam == ranking_namen[0]:
                rank_kleuren[naam] = self.accent_green
            elif naam == ranking_namen[1]:
                rank_kleuren[naam] = self.accent_orange
            else:
                rank_kleuren[naam] = self.accent_red

        # 5. Droogtijden
        sec_buiten, sec_binnen, sec_kast = self._bereken_alle_tijden(self.huidig_stoftype)
        tijd_buiten = round(sec_buiten / 3600, 1)
        tijd_binnen = round(sec_binnen / 3600, 1)
        tijd_droger = round(sec_kast / 3600, 1)

        # 6. Data lijst configureren
        tijden = {
            "Buiten": tijd_buiten,
            "Binnen": tijd_binnen,
            "Droger": tijd_droger
        }
        snelste_id = min(tijden, key=tijden.get)
        # We passen hier de vochtigheid en de regen-uitleg aan
        regen_tekst = "Droog voorspeld"
        regen_kleur = "transparent"
        if neerslag > 0:
            regen_tekst = f"REGEN: {neerslag}mm"
            regen_kleur = "#ff3b3b" # Fel rood bij regen
        elif tijd_buiten > 10:
            regen_tekst = "Te traag buiten"
            regen_kleur = "#f39c12"
        droogkast_kost = self.live_energieprijs * 2.5
        kost_bg_kleur = "#ff3b3b" if droogkast_kost > 0 else "#27ae60"

        data_lijst = [
            {
                "id":   "Buiten",
                "t":    "Buiten drogen",
                "d":    f"droogtijd {tijd_buiten}u",
                "k":    "Gratis",
                "temp": self.huidige_temp,
                "v":    f"vocht {v_buiten}%", # LIVE VOCHT
                "ex":   regen_tekst,          # REGEN DATA[cite: 2]
                "ex_c": regen_kleur,
                "h":    snelste_id == "Buiten",
            },
            {
                "id":   "Binnen",
                "t":    "Binnen drogen",
                "d":    f"droogtijd {tijd_binnen}u",
                "k":    "Gratis",
                "temp": f"{binnen['temp']}°C",
                "v":    f"vocht {binnen['vocht']}%",
                "ex":   "Lucht te vochtig" if binnen['vocht'] > 65 else "Sensor data Pi",
                "ex_c": "#f39c12" if binnen['vocht'] > 65 else "transparent",
                "h":    snelste_id == "Binnen",
            },
            {
                "id":   "Droger",
                "t":    "Droogkast",
                "d":    f"droogtijd {tijd_droger}u",
                "k":    f"kost: €{self.live_energieprijs * 2.5:.2f}",
                "k_bg": kost_bg_kleur,
                "temp": "/",
                "v":    "/",
                "ex":   f"Stroom: €{self.live_energieprijs}/kWh",
                "ex_c": "transparent",
                "h":    snelste_id == "Droger",
            },
        ]

       
        # 7. UI Tekenen (Titel en Tabel)
        titel_container = ctk.CTkFrame(inner, fg_color="transparent")
        titel_container.grid(row=0, column=0, columnspan=3, pady=(80, 30))
        
        ctk.CTkLabel(
            titel_container, 
            text="Vergelijking: ", 
            font=("Arial Bold", 28), 
            text_color="black"
        ).pack(side="left")

        # DE HERSTELDE KNOP:
        self.stof_button = ctk.CTkButton(
            titel_container,
            text=f"{self.huidig_stoftype} was",
            command=self._open_stof_menu,
            font=("Arial Bold", 24),
            fg_color="#1a66ff",
            hover_color="#0052cc",
            text_color="white",
            width=160,
            height=45,
            corner_radius=12
        )
        self.stof_button.pack(side="left", padx=10)

        
        for i, item in enumerate(data_lijst):
            col = ctk.CTkFrame(inner, fg_color="transparent")
            col.grid(row=1, column=i, sticky="nsew", padx=10)

            titel_kleur = rank_kleuren.get(item["id"], "black")
            ctk.CTkLabel(col, text=item["t"], font=("Arial Bold", 22), text_color=titel_kleur).pack()
            ctk.CTkFrame(col, height=2, width=140, fg_color=titel_kleur).pack(pady=10)

            # Highlight de beste optie met een badge[cite: 2]
            tijd_bg = "#27ae60" if item["h"] else "transparent"
            tijd_fg = "white" if item["h"] else "black"

            ctk.CTkLabel(col, text=item["d"], font=("Arial", 18), text_color=tijd_fg, 
                         fg_color=tijd_bg, corner_radius=6, width=170, height=35).pack(pady=5)
            
            huidige_kost_bg = item.get("k_bg", "#27ae60")
            ctk.CTkLabel(col, text=item["k"], font=("Arial Bold", 18), text_color="white", 
                         fg_color=huidige_kost_bg, corner_radius=6, width=180, height=35).pack(pady=15)

            ctk.CTkLabel(col, text=item["temp"], font=("Arial", 18), text_color="black").pack()
            ctk.CTkLabel(col, text=item["v"], font=("Arial", 18), text_color="black").pack(pady=5)

            # Extra info box (Regen / Vocht waarschuwingen)[cite: 2]
            if item["ex"]:
                box = ctk.CTkFrame(col, fg_color=item["ex_c"], corner_radius=8)
                box.pack(pady=20, padx=10)
                ctk.CTkLabel(box, text=item["ex"], font=("Arial Bold", 14), text_color="black" if item["ex_c"] == "transparent" else "white", padx=10, pady=5).pack()

            if i < 2:
                sep = ctk.CTkFrame(inner, width=2, fg_color="#ccc")
                sep.grid(row=1, column=i, sticky="nse", pady=(0, 40))

        self.compare_frame.update_idletasks()
    
    def _open_stof_menu(self):
        """Opent een dropdown menu zonder pijltjes-icoon."""
        import tkinter as tk
    
        # Maak het menu aan
        menu = tk.Menu(self, tearoff=0, font=("Arial", 14))
    
        # Voeg de opties toe
        for optie in ["Licht was", "Gemiddeld was", "Zwaar was"]:
            menu.add_command(
                label=optie, 
                command=lambda o=optie: self._update_comparison_type(o)
            )
    
        # Toon het menu direct onder de muisklik
        try:
            menu.tk_popup(self.winfo_pointerx(), self.winfo_pointery())
        finally:
            menu.grab_release()

    def _update_comparison_type(self, nieuw_type):
        """Update het stoftype en ververs direct het scherm."""
        self.huidig_stoftype = nieuw_type.replace(" was", "").strip()
        self.show_comparison()

    def show_debug_info(self):
        debug_win = ctk.CTkToplevel(self)
        debug_win.title("Sensor Debugger")
        debug_win.geometry("400x300")
        debug_win.attributes("-topmost", True)
        debug_win.transient(self)
        debug_win.lift()
        debug_win.focus_force()

        info_label = ctk.CTkLabel(debug_win, text="", font=("Consolas", 14), justify="left")
        info_label.pack(pady=20)
        ctk.CTkButton(debug_win, text="Sluit", command=debug_win.destroy).pack(pady=10)
        ctk.CTkButton(debug_win, text="close interface", command=self.on_closing).pack(pady=10)

        def _refresh():
            if not debug_win.winfo_exists():
                return
            binnen = self.get_internal_sensor_data()
            info  = f"DHT Temp: {binnen['temp']}\n"
            info += f"DHT Vocht: {binnen['vocht']}\n"
            info += f"Sound ADC: {binnen['geluid']}V\n"
            info += f"Motion: {binnen['motion']}\n"
            info += f"On Pi: {ON_PI}\n"
            info += f"Next DHT Read: {round(self._next_dht_time - time.monotonic(), 1)}s"
            info_label.configure(text=info)
            debug_win.after(2000, _refresh)  # Elke 2s verversen

        _refresh()
    
if __name__ == "__main__":
    app = LaundryApp()
    app.mainloop()