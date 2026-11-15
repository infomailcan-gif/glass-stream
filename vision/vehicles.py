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
    def __init__(self, device, conf: float = 0.4) -> None:
        name = next(n for n in ("yolo11x.pt", "yolo11l.pt", "yolo11s.pt", "yolo11n.pt") if (MODELS / n).exists())
        self.model = YOLO(str(MODELS / name))
        self.device = device
        self.conf = conf
        self.vehicles: dict[int, Vehicle] = {}
        self.current: list[tuple[int, tuple]] = []  # bu karedeki (id, kutu)
        self.recent_plates: dict[str, float] = {}
        self.log_all = True  # tum plakalari gunluge yaz
        self.speak = False  # yeni araci sesli soyle
        LOG_DIR.mkdir(exist_ok=True)
        print(f"[arac] model: {name}")

    # ------------------------------------------------------------------
    def update(self, frame: np.ndarray, now: float) -> None:
        r = self.model.track(frame, persist=True, classes=list(TYPES), conf=self.conf,
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
            ready = v.plate is not None or (gone and v.frames >= 12)
            if not v.logged and ready:
                plate = v.plate
                if plate and now - self.recent_plates.get(plate, 0) < 600:
                    v.logged = True  # ayni plaka 10 dk icinde tekrar yazilmaz
                elif self.log_all and (plate or v.frames >= 12):
                    v.logged = True
                    if plate:
                        self.recent_plates[plate] = now
                    e = self._write(v, plate, now)
                    entries.append(e)
                    if self.speak:
                        says.append(f"{(v.color or '').capitalize()} {v.vtype}" + (f", {pretty(plate)}" if plate else ""))
            if gone and now - v.last_seen > 30:
                del self.vehicles[tid]  # bellegi temiz tut
        return entries, says

    def _write(self, v: Vehicle, plate: str | None, now: float) -> dict:
        day = time.strftime("%Y%m%d", time.localtime(now))
        folder = LOG_DIR / day
        folder.mkdir(parents=True, exist_ok=True)
        name = time.strftime("%H%M%S", time.localtime(now)) + (f"_{plate}" if plate else f"_{v.vtype}") + ".jpg"
        img = None
        if v.best_crop is not None:
            crop = v.best_crop
            k = 320 / max(crop.shape[:2])
            if k < 1:
                crop = cv2.resize(crop, None, fx=k, fy=k)
            cv2.imwrite(str(folder / name), crop, [cv2.IMWRITE_JPEG_QUALITY, 85])
            img = f"/vehicles/{day}/{name}"
        e = {"time": now, "type": v.vtype, "color": v.color, "plate": pretty(plate) if plate else None, "image": img}
        with open(folder / "log.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
        return e

    def history(self, limit: int = 60) -> dict:
        items = []
        for day in sorted(LOG_DIR.glob("*"), reverse=True):
            log = day / "log.jsonl"
            if not log.exists():
                continue
            lines = log.read_text(encoding="utf-8").splitlines()
            for line in reversed(lines):
                try:
                    items.append(json.loads(line))
                except ValueError:
                    continue
                if len(items) >= limit:
                    return {"type": "vehicles", "items": items, "log_all": self.log_all, "speak": self.speak}
        return {"type": "vehicles", "items": items, "log_all": self.log_all, "speak": self.speak}
