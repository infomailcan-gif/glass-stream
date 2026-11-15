"""Plaka okuma + aranan plaka listesi.

fast-alpr (acik kaynak): YOLOv9 plaka bulucu + CCT global OCR modeli.
Gizlilik: SADECE aranan listedeki plakalar kaydedilir. Diger plakalar
ekranda gosterilir ama hicbir yere yazilmaz.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import cv2
import numpy as np
from fast_alpr import ALPR

HERE = Path(__file__).resolve().parent
WATCHLIST_FILE = HERE / "watchlist.txt"
CAPTURES = HERE / "captures"

# OCR'in sik karistirdigi karakterler -> ayni sinifa indir
_CONFUSE = str.maketrans({"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "Z": "2", "S": "5", "B": "8", "G": "6"})


def normalize(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper().replace("İ", "I"))


def pretty(plate: str) -> str:
    """16ABP320 -> '16 ABP 320'"""
    return " ".join(re.findall(r"\d+|[A-Z]+", plate))


def spoken(plate: str) -> str:
    """Seslendirme icin: '16, A B P, 320'"""
    parts = re.findall(r"\d+|[A-Z]+", plate)
    return ", ".join(" ".join(p) if p.isalpha() else p for p in parts)


def _dist(a: str, b: str) -> int:
    if len(a) != len(b):
        return 99
    return sum(x != y for x, y in zip(a, b))


def matches(read: str, wanted: str) -> bool:
    """Okunan plaka arananla esit mi? Benzer harf karisikliklarina ve
    (7+ karakterli plakalarda) tek karakter hataya izin verir."""
    if read == wanted:
        return True
    ra, wa = read.translate(_CONFUSE), wanted.translate(_CONFUSE)
    if ra == wa:
        return True
    return len(wanted) >= 7 and _dist(ra, wa) <= 1


class PlateReader:
    def __init__(self, every: int = 3, min_conf: float = 0.5, cooldown: float = 60.0) -> None:
        self.alpr = ALPR(
            detector_model="yolo-v9-t-384-license-plate-end2end",
            ocr_model="cct-s-v2-global-model",
        )
        self.every = every
        self.min_conf = min_conf
        self.cooldown = cooldown
        self.frame_i = 0
        self.last_boxes: list[tuple[tuple[int, int, int, int], str]] = []
        self.last_reads: list[tuple[tuple[int, int, int, int], str, float]] = []  # bu turdaki ham okumalar
        self.watchlist: list[str] = self._load()
        self.hits: dict[str, list[float]] = {}  # aranan plaka -> son gorulme zamanlari
        self.last_alert: dict[str, float] = {}
        CAPTURES.mkdir(exist_ok=True)

    # ---------------- aranan liste ----------------
    def _load(self) -> list[str]:
        if not WATCHLIST_FILE.exists():
            return []
        return [p for p in (normalize(l) for l in WATCHLIST_FILE.read_text(encoding="utf-8").splitlines()) if p]

    def set_watchlist(self, plates: list[str]) -> None:
        self.watchlist = sorted({p for p in (normalize(x) for x in plates) if len(p) >= 4})
        WATCHLIST_FILE.write_text("\n".join(self.watchlist) + "\n", encoding="utf-8")

    def watchlist_pretty(self) -> list[str]:
        return [pretty(p) for p in self.watchlist]

    # ---------------- kare isleme ----------------
    def process(self, frame: np.ndarray) -> tuple[list[tuple[tuple[int, int, int, int], str, bool]], list[str], list[dict]]:
        """Dondurur: (kutular[(xyxy, etiket, aranan_mi)], anonslar, yakalananlar)"""
        self.frame_i += 1
        if self.frame_i % self.every:
            self.last_reads = []
            return [(b, t, False) for b, t in self.last_boxes], [], []

        now = time.time()
        boxes, announcements, captures = [], [], []
        self.last_boxes = []
        self.last_reads = []
        for r in self.alpr.predict(frame):
            bb = r.detection.bounding_box
            xyxy = (int(bb.x1), int(bb.y1), int(bb.x2), int(bb.y2))
            if r.ocr is None or not r.ocr.text:
                continue
            conf = r.ocr.confidence
            conf = float(np.mean(conf)) if isinstance(conf, (list, tuple, np.ndarray)) else float(conf)
            text = normalize(r.ocr.text)
            if len(text) < 4 or conf < self.min_conf:
                continue

            self.last_reads.append((xyxy, text, conf))
            wanted = next((w for w in self.watchlist if matches(text, w)), None)
            label = pretty(text)
            if wanted is None:
                self.last_boxes.append((xyxy, label))
                boxes.append((xyxy, label, False))
                continue

            boxes.append((xyxy, f"ARANAN: {pretty(wanted)}", True))
            # Yanlis alarmi azaltmak icin 5 sn icinde 2 kez gorulmeli
            seen = [t for t in self.hits.get(wanted, []) if now - t < 5] + [now]
            self.hits[wanted] = seen
            if len(seen) >= 2 and now - self.last_alert.get(wanted, 0) > self.cooldown:
                self.last_alert[wanted] = now
                fname = self._save(frame, xyxy, wanted, now)
                announcements.append(f"Aranan plaka bulundu: {spoken(wanted)}")
                captures.append({"plate": pretty(wanted), "read": label, "file": fname, "time": now})
        return boxes, announcements, captures

    def _save(self, frame: np.ndarray, xyxy, plate: str, now: float) -> str:
        img = frame.copy()
        cv2.rectangle(img, xyxy[:2], xyxy[2:], (0, 0, 255), 3)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
        cv2.putText(img, f"{pretty(plate)}  {stamp}", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
        name = f"{plate}_{stamp}.jpg"
        cv2.imwrite(str(CAPTURES / name), img)
        return name
