"""Genis nesne kutuphanesi (YOLOE, acik sozluklu).

Iki mod:
  * "list"   : web'den yazilan nesneler aranir (or. "forklift, yangin tupu").
               Turkce ISG terimleri sozlukle Ingilizceye cevrilir; sozlukte
               olmayanlar oldugu gibi (Ingilizce yazilmissa) kullanilir.
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
                        "conf": self.conf, "enabled": self.enabled}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )

    def configure(self, cfg: dict) -> None:
        with self._lock:
            if "mode" in cfg and cfg["mode"] in ("list", "catalog"):
                self.mode = cfg["mode"]
            if "items" in cfg:
                self.items = [s.strip() for s in cfg["items"] if s and s.strip()][:80]
            if "alerts" in cfg:
                self.alerts = [s for s in cfg["alerts"] if s in self.items]
            if "enabled" in cfg:
                self.enabled = bool(cfg["enabled"])
            self._dirty = True
        self._save()

    def config(self) -> dict:
        return {
            "type": "objects_cfg", "mode": self.mode, "items": self.items, "alerts": self.alerts,
            "enabled": self.enabled, "dictionary": sorted(TR_EN.keys()),
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
            self._dirty = False
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
                self._model = YOLOE(str(MODELS / "yoloe-11s-seg.pt"))
                self._model_mode = "list"
            prompts = [to_prompt(i) for i in items] or ["person"]
            self._model.set_classes(prompts, self._model.get_text_pe(prompts))
            self._display = items or ["person"]
            print(f"[objects] liste modu: {list(zip(self._display, prompts))}")

    def _loop(self) -> None:
        while True:
            if not self._event.wait(1.0):
                continue
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
                r = self._model.predict(frame, device=self.device, conf=self.conf, verbose=False,
                                        half=self.device == 0)[0]
                out = []
                H, W = frame.shape[:2]
                if r.boxes is not None:
                    for b, c, s in zip(r.boxes.xyxy.cpu().numpy(), r.boxes.cls.int().cpu().tolist(),
                                       r.boxes.conf.cpu().tolist()):
                        name = self._display[c] if self._display else CATALOG_TR.get(r.names[c], r.names[c])
                        if r.names[c] == "person" or name == "person" or (not self._display and is_scene(r.names[c])):
                            continue  # kisileri KKD modeli isliyor; sahne siniflari anlamsiz
                        area = (b[2] - b[0]) * (b[3] - b[1]) / (W * H)
                        if area > MAX_AREA:
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
