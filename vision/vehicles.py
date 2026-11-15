"""Arac tanima: tur (araba, kamyon, otobus, motosiklet, bisiklet) + renk + plaka.

* YOLO11 (COCO) ile araclar bulunur ve takip edilir (her araca bir numara).
* Renk: arac govdesinin orta bolgesinden HSV ile (beyaz, siyah, gri, kirmizi...).
* Plaka: plaka okuyucunun buldugu plaka, icinde bulundugu araca baglanir.
* Arac gunlugu: plakasi 2 kez tutarli okunan (ya da yeterince gorulen) her arac
  saat, tur, renk, plaka ve kucuk fotografla kaydedilir. Ayni plaka 10 dk icinde
  tekrar yazilmaz. Kayitlar: vision/vehicles/GUN/log.jsonl + jpg.
KVKK: plakalar kisisel veridir; gunluk yalnizca bu bilgisayarda tutulur.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from ultralytics import YOLO

from plates import pretty

HERE = Path(__file__).resolve().parent
MODELS = HERE / "models"
LOG_DIR = HERE / "vehicles"
CFG_FILE = HERE / "vehicles_cfg.json"

# COCO sinif no -> Turkce
TYPES = {1: "bisiklet", 2: "araba", 3: "motosiklet", 5: "otobüs", 7: "kamyon"}


def color_name(crop: np.ndarray) -> str | None:
    """Arac govdesinin baskin rengi (Turkce)."""
    if crop is None or crop.size == 0:
        return None
    h, w = crop.shape[:2]
    # Orta bolge: camlar, tekerlekler ve arka plan daha az karissin
    body = crop[int(h * 0.30):int(h * 0.75), int(w * 0.15):int(w * 0.85)]
    if body.size == 0:
        return None
    hsv = cv2.cvtColor(cv2.resize(body, (48, 32)), cv2.COLOR_BGR2HSV).reshape(-1, 3).astype(np.int32)
    hh, ss, vv = hsv[:, 0], hsv[:, 1], hsv[:, 2]
    colorful = ss > 70
    if colorful.mean() < 0.35:  # renksiz: beyaz / gri / siyah
        v = float(np.median(vv))
        if v > 175:
            return "beyaz"
        if v > 120:
            return "gümüş"
        if v > 65:
            return "gri"
        return "siyah"
    hue = int(np.median(hh[colorful]))
    val = float(np.median(vv[colorful]))
    if hue < 8 or hue >= 170:
        return "kırmızı" if val > 70 else "bordo"
    if hue < 20:
        return "turuncu" if val > 140 else "kahverengi"
    if hue < 34:
        return "sarı"
    if hue < 85:
        return "yeşil"
    if hue < 128:
        return "mavi" if val > 60 else "lacivert"
    if hue < 160:
        return "mor"
    return "pembe"


def _iou(a, b) -> float:
    x1, y1, x2, y2 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


@dataclass
class Vehicle:
    first_seen: float
    last_seen: float = 0.0
    frames: int = 0
    types: Counter = field(default_factory=Counter)
    colors: Counter = field(default_factory=Counter)
    plates: Counter = field(default_factory=Counter)
    best_crop: np.ndarray | None = None
    best_area: float = 0.0
    logged: bool = False
    last_box: tuple | None = None

    @property
    def vtype(self) -> str:
        return self.types.most_common(1)[0][0] if self.types else "araç"

    @property
    def color(self) -> str | None:
        return self.colors.most_common(1)[0][0] if self.colors else None

    @property
    def plate(self) -> str | None:
        if not self.plates:
            return None
        p, n = self.plates.most_common(1)[0]
        return p if n >= 2 else None


class VehicleTracker:
    def __init__(self, device, conf: float = 0.25) -> None:
        name = next(n for n in ("yolo26x.pt", "yolo11x.pt", "yolo11l.pt", "yolo11s.pt", "yolo11n.pt") if (MODELS / n).exists())
        self.model = YOLO(str(MODELS / name))
        self.device = device
        self.conf = conf
        self.vehicles: dict[int, Vehicle] = {}
        self.current: list[tuple[int, tuple]] = []  # bu karedeki (id, kutu)
        self.recent_plates: dict[str, float] = {}
        self.plate_hits: dict[str, list[float]] = {}
        self.speak = False  # yeni araci sesli soyle
        # Kaydetme secenekleri (telefon / web'den degisir, dosyada saklanir)
        cfg = {}
        try:
            cfg = json.loads(CFG_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        self.log_vehicles = bool(cfg.get("log_vehicles", True))
        self.log_plates = bool(cfg.get("log_plates", True))
        self.recent_logged: list[tuple[float, str, str | None, np.ndarray]] = []  # (zaman, tur, renk, histogram)
        LOG_DIR.mkdir(exist_ok=True)
        print(f"[arac] model: {name}")

    # ------------------------------------------------------------------
    def update(self, frame: np.ndarray, now: float) -> None:
        r = self.model.track(frame, persist=True, classes=list(TYPES), conf=self.conf,
                             tracker=str(HERE / "tracker_vehicles.yaml"),
                             device=self.device, verbose=False, half=self.device == 0)[0]
        self.current = []
        if r.boxes is None or r.boxes.id is None:
            return
        H, W = frame.shape[:2]
        for b, c, tid in zip(r.boxes.xyxy.cpu().numpy(), r.boxes.cls.int().cpu().tolist(),
                             r.boxes.id.int().cpu().tolist()):
            box = tuple(float(v) for v in b)
            v = self.vehicles.get(tid)
            if v is None:
                v = self.vehicles[tid] = Vehicle(first_seen=now)
                # Takip numarasi degisti mi? (bas hareketi) Az once kaybolan, ayni yerdeki kayitli arac = ayni arac
                for o in self.vehicles.values():
                    if o is not v and o.logged and o.last_box and now - o.last_seen < 2.5                             and _iou(box, o.last_box) > 0.3:
                        v.logged = True
                        break
            v.last_box = box
            v.frames += 1
            v.last_seen = now
            v.types[TYPES.get(c, "araç")] += 1
            x1, y1, x2, y2 = (int(max(0, box[0])), int(max(0, box[1])), int(min(W, box[2])), int(min(H, box[3])))
            crop = frame[y1:y2, x1:x2]
            if v.frames % 3 == 1:  # rengi birkac karede bir hesapla
                c_name = color_name(crop)
                if c_name:
                    v.colors[c_name] += 1
            area = (x2 - x1) * (y2 - y1)
            if area > v.best_area and crop.size:
                v.best_area, v.best_crop = area, crop.copy()
            self.current.append((tid, box))

    def attach_plates(self, reads: list[tuple[tuple, str, float]]) -> None:
        """Plaka okumalarini, icinde bulunduklari araca bagla."""
        for xyxy, text, _conf in reads:
            cx, cy = (xyxy[0] + xyxy[2]) / 2, (xyxy[1] + xyxy[3]) / 2
            best, best_area = None, None
            for tid, b in self.current:
                if b[0] <= cx <= b[2] and b[1] <= cy <= b[3]:
                    a = (b[2] - b[0]) * (b[3] - b[1])
                    if best_area is None or a < best_area:
                        best, best_area = tid, a
            if best is not None:
                self.vehicles[best].plates[text] += 1

    def plate_seen(self, text: str, xyxy, frame: np.ndarray, now: float, conf: float = 1.0) -> dict | None:
        """Her plaka okumasi. Gunluge yalnizca su durumda yazilir:
        - okunan yazi gecerli bir Turk plakasi (il kodu 01-81, harf/rakam duzeni) ve degisiklik gerektirmiyor,
        - OCR guveni yuksek (>= 0.8),
        - 6 sn icinde ayni plaka en az 3 kez okundu.
        Ayni plaka 10 dk icinde tekrar yazilmaz. Bir aracin icindeyse aracin turu/rengi eklenir."""
        from plates import turkish_fix

        fixed = turkish_fix(text)
        if not fixed or fixed[0] != text or conf < 0.8:
            return None  # yanlis okuma (gurultu, yazi, tabela) gunluge girmesin
        hits = [t for t in self.plate_hits.get(text, []) if now - t < 6] + [now]
        self.plate_hits[text] = hits
        if len(hits) < 3 or not self.log_plates or now - self.recent_plates.get(text, 0) < 600:
            return None
        self.recent_plates[text] = now
        cx, cy = (xyxy[0] + xyxy[2]) / 2, (xyxy[1] + xyxy[3]) / 2
        owner = next((self.vehicles[tid] for tid, b in self.current
                      if b[0] <= cx <= b[2] and b[1] <= cy <= b[3] and tid in self.vehicles), None)
        H, W = frame.shape[:2]
        w, h = xyxy[2] - xyxy[0], xyxy[3] - xyxy[1]
        x1, y1 = int(max(0, xyxy[0] - w * 1.5)), int(max(0, xyxy[1] - h * 3))
        x2, y2 = int(min(W, xyxy[2] + w * 1.5)), int(min(H, xyxy[3] + h * 1.5))
        img = frame[y1:y2, x1:x2].copy()
        cv2.rectangle(img, (int(xyxy[0]) - x1, int(xyxy[1]) - y1), (int(xyxy[2]) - x1, int(xyxy[3]) - y1), (0, 200, 255), 2)
        day = time.strftime("%Y%m%d", time.localtime(now))
        folder = LOG_DIR / day
        folder.mkdir(parents=True, exist_ok=True)
        name = time.strftime("%H%M%S", time.localtime(now)) + f"_plaka_{text}.jpg"
        cv2.imwrite(str(folder / name), img, [cv2.IMWRITE_JPEG_QUALITY, 88])
        import place

        e = {"time": now, "plate": pretty(text), "type": owner.vtype if owner else None,
             "color": owner.color if owner else None, "image": f"/vehicles/{day}/{name}", **place.fields()}
        with open(folder / "plates.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
        return e

    def boxes(self) -> list[tuple[tuple, str]]:
        """Bu karedeki araclar icin (kutu, etiket): 'Beyaz araba · 34 ABC 123'"""
        out = []
        for tid, b in self.current:
            v = self.vehicles[tid]
            label = " ".join(x for x in (v.color, v.vtype) if x).capitalize()
            if v.plate:
                label += f" · {pretty(v.plate)}"
            out.append((b, label))
        return out

    # ------------------------------------------------------------------
    def collect(self, now: float) -> tuple[list[dict], list[str]]:
        """Gunluge yazilacak yeni araclar + (istenirse) anonslar."""
        entries, says = [], []
        for tid, v in list(self.vehicles.items()):
            gone = now - v.last_seen > 3
            # Arac ~5 analizde (yaklasik 1,5 sn) goruldu ve kirpilmis fotografi var: hemen kaydet.
            # (Eskiden kadrajdan cikmasi bekleniyordu; park halindeki araclar hic kaydedilmiyordu.)
            # Dusuk cozunurluklu yayinda (360x640) araclar ~60x30 piksel kalir: esikler buna gore
            ready = v.frames >= 3 and v.best_crop is not None and v.best_area >= 24 * 24
            if not v.logged and ready and self.log_vehicles:
                v.logged = True
                e = self._write(v, v.plate, now)
                entries.append(e)
                if self.speak:
                    says.append(f"{(v.color or '').capitalize()} {v.vtype}")
            if gone and now - v.last_seen > 30:
                del self.vehicles[tid]  # bellegi temiz tut
        return entries, says

    def _seen_recently(self, v: Vehicle, now: float, window: float = 300) -> bool:
        """Son 5 dk'da ayni tur + renk + benzer gorunumlu arac yazildi mi?"""
        hsv = cv2.cvtColor(cv2.resize(v.best_crop, (64, 48)), cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist([hsv], [0, 1], None, [18, 8], [0, 180, 0, 256])
        cv2.normalize(hist, hist)
        self.recent_logged = [r for r in self.recent_logged if now - r[0] < window]
        for _, t, c, h in self.recent_logged:
            if t == v.vtype and c == v.color and cv2.compareHist(h, hist, cv2.HISTCMP_CORREL) > 0.9:
                return True
        self.recent_logged.append((now, v.vtype, v.color, hist))
        return False

    def _write(self, v: Vehicle, plate: str | None, now: float) -> dict:
        day = time.strftime("%Y%m%d", time.localtime(now))
        folder = LOG_DIR / day
        folder.mkdir(parents=True, exist_ok=True)
        name = time.strftime("%H%M%S", time.localtime(now)) + (f"_{plate}" if plate else f"_{v.vtype}") + ".jpg"
        img = None
        if v.best_crop is not None:
            crop = v.best_crop
            k = 480 / max(crop.shape[:2])
            if k < 1:
                crop = cv2.resize(crop, None, fx=k, fy=k)
            cv2.imwrite(str(folder / name), crop, [cv2.IMWRITE_JPEG_QUALITY, 85])
            img = f"/vehicles/{day}/{name}"
        import place

        e = {"time": now, "type": v.vtype, "color": v.color, "plate": pretty(plate) if plate else None, "image": img,
             **place.fields()}
        with open(folder / "log.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
        return e

    @staticmethod
    def _read(name: str, limit: int) -> list[dict]:
        items: list[dict] = []
        for day in sorted(LOG_DIR.glob("*"), reverse=True):
            log = day / name
            if not log.exists():
                continue
            for line in reversed(log.read_text(encoding="utf-8").splitlines()):
                try:
                    items.append(json.loads(line))
                except ValueError:
                    continue
                if len(items) >= limit:
                    return items
        return items

    def history(self, limit: int = 60) -> dict:
        """Araclar (tur, renk, foto) ve plakalar (ayri gunluk)."""
        return {"type": "vehicles", "items": self._read("log.jsonl", limit), "plates": self._read("plates.jsonl", limit),
                "log_vehicles": self.log_vehicles, "log_plates": self.log_plates, "speak": self.speak}

    def set_options(self, log_vehicles: bool | None = None, log_plates: bool | None = None) -> None:
        if log_vehicles is not None:
            self.log_vehicles = log_vehicles
        if log_plates is not None:
            self.log_plates = log_plates
        CFG_FILE.write_text(json.dumps({"log_vehicles": self.log_vehicles, "log_plates": self.log_plates}),
                            encoding="utf-8")
