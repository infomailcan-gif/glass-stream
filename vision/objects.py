"""Genis nesne kutuphanesi (YOLOE, acik sozluklu).

Uc mod:
  * "list"   : web'den yazilan nesneler aranir (or. "forklift, yangin tupu").
               Turkce ISG terimleri sozlukle Ingilizceye cevrilir; sozlukte
               olmayanlar oldugu gibi (Ingilizce yazilmissa) kullanilir.
  * "preset" : hazir ortam secimi (or. "Sunucu odasi"); ortam alt dallara ayrilir
               (Ag cihazlari, Kablolama, Guc...), her dal ayri acilip kapatilir.
  * "catalog": hicbir sey yazmadan ~4.585 nesne turu (prompt-free model).

Model ayri thread'de calisir; ana analiz dongusunu yavaslatmaz.
NOT: Metin kodlayici (mobileclip_blt.ts) calisma dizininden yuklenir;
detector.py bu yuzden vision/models icinden calistirilir (start-vision.cmd).
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
MODELS = HERE / "models"
CONFIG = HERE / "objects.json"

# Turkce (gosterilen ad) -> Ingilizce (modele verilen ifade)
TR_EN = {
    "forklift": "forklift",
    "yangın tüpü": "fire extinguisher",
    "merdiven": "ladder",
    "palet": "pallet",
    "transpalet": "pallet jack",
    "kaynak makinesi": "welding machine",
    "gaz tüpü": "gas cylinder",
    "kablo": "cable",
    "koli": "cardboard box",
    "kova": "bucket",
    "uyarı levhası": "warning sign",
    "ıslak zemin levhası": "wet floor sign",
    "acil çıkış levhası": "emergency exit sign",
    "ilk yardım dolabı": "first aid kit",
    "iskele": "scaffolding",
    "vinç": "crane",
    "kamyon": "truck",
    "araba": "car",
    "el arabası": "wheelbarrow",
    "matkap": "power drill",
    "taşlama": "angle grinder",
    "çekiç": "hammer",
    "tornavida": "screwdriver",
    "makas": "scissors",
    "bıçak": "knife",
    "sigara": "cigarette",
    "telefon": "cell phone",
    "ateş": "fire",
    "duman": "smoke",
    "sıvı dökülmesi": "liquid spill on floor",
    "emniyet şeridi": "safety barrier tape",
    "trafik konisi": "traffic cone",
    "bariyer": "barrier",
    "raf": "storage rack",
    "konveyör": "conveyor belt",
    "pano": "electrical panel",
    "priz": "power outlet",
    "hortum": "hose",
    "varil": "barrel",
    "baret": "hard hat",
    "yelek": "reflective safety vest",
    "eldiven": "work gloves",
    "kulaklık": "ear muffs",
    "gözlük": "safety goggles",
    "maske": "face mask",
    "bone": "hairnet",
}

# Genis katalog sinif adlarinin Turkcesi (bir kez uretilir: make_catalog_tr.py)
try:
    CATALOG_TR: dict[str, str] = json.loads((HERE / "catalog_tr.json").read_text(encoding="utf-8"))
except (OSError, ValueError):
    CATALOG_TR = {}

# Hazir ortamlar: ortam -> alt dallar -> (gosterilen ad, modele verilen ifade).
# Yeni ortam / alt dal eklemek icin buraya yazmak yeter; telefon ve web otomatik gosterir.
PRESETS: dict[str, dict] = {
    "server_room": {
        "name": "Sunucu odası",
        # Hazir modeller ag donanimini zayif taniyor; puanlar dusuk gelir, esik bu ortam icin dusuk.
        "conf": 0.15,
        "groups": {
            "network": {"name": "Ağ cihazları", "items": [
                ("switch / rack cihazı", ("cisco switch", "rack mount network switch with ports", "ethernet switch")),
                ("patch panel", ("patch panel", "rj45 patch panel")),
                ("router", ("network router",)),
                ("firewall", ("network firewall appliance",)),
                ("access point", ("wireless access point",)),
                ("modem", ("modem",)),
            ]},
            "rack": {"name": "Sunucu ve kabinet", "items": [
                ("kabinet", ("server rack", "rack cabinet")),
                ("sunucu", ("rack mounted server",)),
                ("depolama ünitesi", ("storage array",)),
                ("monitör", ("computer monitor",)),
            ]},
            "cabling": {"name": "Kablolama", "items": [
                ("kablo demeti", ("bundle of cables", "tangled cables")),
                ("ethernet portu", ("ethernet port", "row of ethernet ports")),
                ("fiber kablo", ("fiber optic cable",)),
                ("SFP modülü", ("sfp transceiver module",)),
            ]},
            "power": {"name": "Güç", "items": [
                ("UPS", ("uninterruptible power supply",)),
                ("PDU / priz grubu", ("power distribution unit", "power strip")),
                ("akü", ("battery",)),
            ]},
            "safety": {"name": "Ortam güvenliği", "items": [
                ("yangın tüpü", ("fire extinguisher",)),
                ("duman dedektörü", ("smoke detector",)),
                ("klima", ("air conditioner",)),
                ("su sızıntısı", ("water leak on floor",)),
                ("koli", ("cardboard box",)),
            ]},
        },
    },
}


def preset_items(key: str, groups: list[str] | None) -> list[tuple[str, str]]:
    """Secili alt dallardaki (gosterilen ad, ifade) ciftleri; dal secilmemisse hepsi.
    Bir nesnenin birden cok ifadesi varsa her biri ayri cift olur (ayni gosterilen ad)."""
    pr = PRESETS.get(key)
    if not pr:
        return []
    keys = [g for g in (groups or []) if g in pr["groups"]] or list(pr["groups"])
    return [(n, e) for g in keys for n, es in pr["groups"][g]["items"] for e in es]


def presets_info() -> list[dict]:
    return [{"key": k, "name": v["name"],
             "groups": [{"key": gk, "name": g["name"], "items": [n for n, _ in g["items"]]}
                        for gk, g in v["groups"].items()]}
            for k, v in PRESETS.items()]


DEFAULT_LIST = ["forklift", "yangın tüpü", "merdiven", "palet", "gaz tüpü", "sıvı dökülmesi", "sigara", "telefon"]


def to_prompt(name: str) -> str:
    key = name.strip().lower().replace("I", "ı")
    return TR_EN.get(key, name.strip())


# Kataloglarda bircok "sahne" sinifi var (oda, tavan, magaza...). Bunlar butun kareyi
# kaplayan anlamsiz kutular cizer; elenir.
SCENE_WORDS = (
    "interior", "room", "ceiling", "floor", "wall", "indoor", "outdoor", "store", "shop", "gym", "office",
    "kitchen", "hall", "building", "street", "sky", "scene", "space", "area", "house", "home", "studio",
    "mezzanine", "warehouse", "factory", "plant", "restaurant", "corridor", "lobby", "classroom",
)
MAX_AREA = 0.30  # karenin %30'undan buyuk kutulari gosterme


def is_scene(name: str) -> bool:
    n = name.lower()
    return any(w in n for w in SCENE_WORDS)


class PromptFinder:
    """Sabit metin istemleriyle (or. bone) arka planda calisan kucuk YOLOE dedektoru."""

    def __init__(self, device, prompts: dict[str, list[str]], rate_hz: float = 5.0, conf: float = 0.25) -> None:
        self.device = device
        self.prompts = prompts  # anahtar -> [ifade, ...]
        self.period = 1.0 / rate_hz
        self.conf = conf
        self._lock = threading.Lock()
        self._frame = None
        self._event = threading.Event()
        self._results: dict[str, list] = {}
        self._at = 0.0
        self._model = None
        threading.Thread(target=self._loop, daemon=True).start()

    def submit(self, frame) -> None:
        with self._lock:
            self._frame = frame
        self._event.set()

    def latest(self, max_age: float = 1.0) -> dict[str, list]:
        with self._lock:
            return dict(self._results) if time.time() - self._at < max_age else {}

    def _loop(self) -> None:
        from ultralytics import YOLOE

        while True:
            if not self._event.wait(1.0):
                continue
            with self._lock:
                frame, self._frame = self._frame, None
                self._event.clear()
            if frame is None:
                continue
            t = time.time()
            try:
                if self._model is None:
                    self._model = YOLOE(str(MODELS / "yoloe-11s-seg.pt"))
                    phrases = [p for ps in self.prompts.values() for p in ps]
                    self._keys = [k for k, ps in self.prompts.items() for _ in ps]
                    self._model.set_classes(phrases, self._model.get_text_pe(phrases))
                r = self._model.predict(frame, device=self.device, conf=self.conf, verbose=False,
                                        half=self.device == 0)[0]
                out: dict[str, list] = {}
                for b, c in zip(r.boxes.xyxy.cpu().numpy(), r.boxes.cls.int().cpu().tolist()):
                    out.setdefault(self._keys[c], []).append(tuple(float(v) for v in b))
                with self._lock:
                    self._results, self._at = out, time.time()
            except Exception as e:
                print(f"[prompt] hata: {e!r}")
                time.sleep(1)
            time.sleep(max(0.0, self.period - (time.time() - t)))


class ObjectFinder:
    def __init__(self, device) -> None:
        self.device = device
        self._lock = threading.Lock()
        self._frame = None
        self._event = threading.Event()
        self._results: list = []
        self._at = 0.0
        self.ms = 0
        cfg = self._load()
        self.mode: str = cfg.get("mode", "list")
        self.items: list[str] = cfg.get("items", DEFAULT_LIST)
        self.alerts: list[str] = cfg.get("alerts", [])  # gorulunce sesli bildirilecekler
        self.conf: float = cfg.get("conf", 0.3)
        self.preset: str = cfg.get("preset", "server_room")
        if self.preset not in PRESETS:
            self.preset = "server_room"
        self.groups: list[str] = cfg.get("groups", [])  # bos = ortamin tum alt dallari
        self._model = None
        self._model_mode = None
        self._dirty = True
        self.enabled = cfg.get("enabled", True)
        threading.Thread(target=self._loop, daemon=True).start()

    # ---------------- ayarlar ----------------
    def _load(self) -> dict:
        try:
            return json.loads(CONFIG.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        CONFIG.write_text(
            json.dumps({"mode": self.mode, "items": self.items, "alerts": self.alerts,
                        "conf": self.conf, "enabled": self.enabled, "preset": self.preset,
                        "groups": self.groups}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )

    def configure(self, cfg: dict) -> None:
        with self._lock:
            if "mode" in cfg and cfg["mode"] in ("list", "preset", "catalog"):
                self.mode = cfg["mode"]
            if cfg.get("preset") in PRESETS:
                if cfg["preset"] != self.preset:
                    self.groups = []
                self.preset = cfg["preset"]
                self.mode = "preset"
            if "groups" in cfg:
                gs = PRESETS[self.preset]["groups"]
                self.groups = [g for g in cfg["groups"] if g in gs]
            if "items" in cfg:
                self.items = [s.strip() for s in cfg["items"] if s and s.strip()][:80]
            if "alerts" in cfg:
                known = set(self.items) | {n for n, _ in preset_items(self.preset, None)}
                self.alerts = [s for s in cfg["alerts"] if s in known]
            if "enabled" in cfg:
                self.enabled = bool(cfg["enabled"])
            self._dirty = True
        self._save()

    def config(self) -> dict:
        return {
            "type": "objects_cfg", "mode": self.mode, "items": self.items, "alerts": self.alerts,
            "enabled": self.enabled, "dictionary": sorted(TR_EN.keys()),
            "preset": self.preset, "groups": self.groups or list(PRESETS[self.preset]["groups"]),
            "presets": presets_info(),
        }

    # ---------------- calisma ----------------
    def submit(self, frame: np.ndarray) -> None:
        with self._lock:
            self._frame = frame
        self._event.set()

    def latest(self, max_age: float = 0.5) -> list:
        """[(xyxy, gosterilen_ad, guven)]"""
        with self._lock:
            return list(self._results) if time.time() - self._at < max_age else []

    def _prepare(self) -> None:
        from ultralytics import YOLOE

        with self._lock:
            mode, items = self.mode, list(self.items)
            preset, groups = self.preset, list(self.groups)
            self._dirty = False
        self._conf = PRESETS[preset].get("conf", self.conf) if mode == "preset" else self.conf
        if mode == "catalog":
            if self._model_mode != "catalog":
                # En buyuk (en isabetli) katalog modeli; yoksa kucuge dus
                for name in ("yoloe-11l-seg-pf.pt", "yoloe-11m-seg-pf.pt", "yoloe-11s-seg-pf.pt"):
                    if (MODELS / name).exists():
                        break
                self._model = YOLOE(str(MODELS / name))
                self._model_mode = "catalog"
            self._display = None
            print(f"[objects] genis katalog modu: {len(self._model.names)} sinif")
        else:
            if self._model_mode != "list":
                # Metinle arama: en buyuk / en yeni model (YOLOE-26-L), yoksa kucuge dus
                for name in ("yoloe-26l-seg.pt", "yoloe-11l-seg.pt", "yoloe-11s-seg.pt"):
                    if (MODELS / name).exists():
                        break
                self._model = YOLOE(str(MODELS / name))
                self._model_mode = "list"
                print(f"[objects] metin modeli: {name}")
            if mode == "preset":
                pairs = preset_items(preset, groups)
                display, prompts = [n for n, _ in pairs], [e for _, e in pairs]
            else:
                display, prompts = items, [to_prompt(i) for i in items]
            if not prompts:
                display, prompts = ["person"], ["person"]
            self._model.set_classes(prompts, self._model.get_text_pe(prompts))
            self._display = display
            print(f"[objects] {'ortam ' + preset if mode == 'preset' else 'liste'} modu: {list(zip(display, prompts))}")

    def _loop(self) -> None:
        while True:
            if not self._event.wait(1.0):
                continue
            # En fazla ~8 kez/sn: diger modellerle ekran kartini paylasir, canli gecikmeyi artirmasin.
            # Bekleme kare ALINMADAN once: kutular en yeni kareye ait olsun.
            wait = 0.125 - (time.time() - getattr(self, "_last_run", 0.0))
            if wait > 0:
                time.sleep(wait)
            with self._lock:
                frame, self._frame = self._frame, None
                self._event.clear()
                enabled = self.enabled
            if frame is None or not enabled:
                with self._lock:
                    self._results = []
                continue
            try:
                if self._dirty or self._model is None:
                    self._prepare()
                t = time.time()
                self._last_run = t
                r = self._model.predict(frame, device=self.device, conf=getattr(self, "_conf", self.conf),
                                        verbose=False, half=self.device == 0,
                                        agnostic_nms=bool(self._display))[0]  # ayni nesneye iki ifadeden iki kutu cizme
                out = []
                H, W = frame.shape[:2]
                if r.boxes is not None:
                    for b, c, s in zip(r.boxes.xyxy.cpu().numpy(), r.boxes.cls.int().cpu().tolist(),
                                       r.boxes.conf.cpu().tolist()):
                        name = self._display[c] if self._display else CATALOG_TR.get(r.names[c], r.names[c])
                        if r.names[c] == "person" or name == "person" or (not self._display and is_scene(r.names[c])):
                            continue  # kisileri KKD modeli isliyor; sahne siniflari anlamsiz
                        area = (b[2] - b[0]) * (b[3] - b[1]) / (W * H)
                        if not self._display and area > MAX_AREA:  # yalniz katalogda (sahne kutulari)
                            continue  # butun kareyi kaplayan dev kutular
                        out.append((tuple(float(v) for v in b), name, s))
                with self._lock:
                    self._results, self._at = out, time.time()
                self.ms = round((time.time() - t) * 1000)
                # Nesne taramasi en fazla ~16/sn (KKD modeline de GPU kalsin)
                time.sleep(max(0.0, 0.06 - (time.time() - t)))
            except Exception as e:
                print(f"[objects] hata: {e!r}")
                time.sleep(1)
