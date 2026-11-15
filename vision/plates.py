"""Plaka okuma + aranan plaka listesi.

fast-alpr (acik kaynak): YOLOv9 plaka bulucu + CCT global OCR modeli,
ustune Turk plaka bicimi duzeltmesi (il kodu, harf/rakam konumu) ve kareler arasi oylama.
Gizlilik: SADECE aranan listedeki plakalar kaydedilir. Diger plakalar
ekranda gosterilir ama hicbir yere yazilmaz.
"""

from __future__ import annotations

import re
import threading
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


# ---------------- Turk plakasi bicim duzeltmesi ----------------
# Bicim: il kodu (01-81) + 1-3 harf + 2-4 rakam. Ornek: 34 A 1234, 06 AB 123, 35 ABC 12
# Turk plakalarinda Q, W, X ve Turkce harfler kullanilmaz.
_TR_LETTERS = set("ABCDEFGHIJKLMNOPRSTUVYZ")
_L2D = {"O": "0", "Q": "0", "D": "0", "U": "0", "I": "1", "L": "1", "J": "1", "T": "7", "Z": "2",
        "S": "5", "B": "8", "G": "6", "A": "4", "E": "8"}
_D2L = {"0": "O", "1": "I", "2": "Z", "5": "S", "8": "B", "6": "G", "4": "A", "7": "T", "3": "B"}
_TR_SHAPES = {1: (4,), 2: (3, 4), 3: (2, 3)}  # harf sayisi -> izinli rakam sayilari


def _as(ch: str, digit: bool) -> tuple[str | None, int]:
    """Karakteri rakam/harf olmaya zorla. (yeni, maliyet) - olmuyorsa (None, 0)."""
    if digit:
        return (ch, 0) if ch.isdigit() else (_L2D.get(ch), 1) if ch in _L2D else (None, 0)
    if ch in _TR_LETTERS:
        return ch, 0
    return (_D2L.get(ch), 1) if ch in _D2L else (None, 0)


def turkish_fix(text: str) -> tuple[str, int] | None:
    """Okunan metni gecerli bir Turk plakasina en az degisiklikle cevir.
    Dondurur: (plaka, degisen karakter sayisi) ya da Turk plakasina benzemiyorsa None."""
    if len(text) > 8 and text.startswith("TR"):  # mavi seritteki TR yazisi
        text = text[2:]
    best = None
    for letters, digit_opts in _TR_SHAPES.items():
        for nd in digit_opts:
            if 2 + letters + nd != len(text):
                continue
            out, cost = [], 0
            for i, ch in enumerate(text):
                c, k = _as(ch, digit=not (2 <= i < 2 + letters))
                if c is None:
                    break
                out.append(c)
                cost += k
            else:
                plate = "".join(out)
                prov_cost = sum(a != b for a, b in zip(plate[:2], text[:2]))
                if prov_cost <= 1 and 1 <= int(plate[:2]) <= 81 and (best is None or cost < best[1]):
                    best = (plate, cost)
    return best if best and best[1] <= 2 else None


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
    def __init__(self, every: int = 1, min_conf: float = 0.5, cooldown: float = 60.0,
                 detector_model: str = "yolo-v9-s-608-license-plate-end2end") -> None:
        # En buyuk plaka bulucu (s-608) + en buyuk global OCR (cct-s-v2).
        # s-608 bazi plakalari kaciriyor (testte sentetik plakayi bulamadi, t-384 buldu):
        # buyuk model bir sey bulamazsa kucuk model de bakar.
        self.alpr = ALPR(detector_model=detector_model, ocr_model="cct-s-v2-global-model")
        self.alpr_backup = ALPR(detector_model="yolo-v9-t-384-license-plate-end2end",
                                ocr_model="cct-s-v2-global-model")
        self._lock = threading.Lock()
        self._frame = None
        self._event = threading.Event()
        self._latest: tuple[list, list, float] = ([], [], 0.0)  # kutular, ham okumalar, zaman
        self._pending_ann: list[str] = []
        self._pending_caps: list[dict] = []
        threading.Thread(target=self._loop, daemon=True).start()
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

    # ---------------- arka plan (ana analiz dongusunu bekletmez) ----------------
    def submit(self, frame: np.ndarray) -> None:
        with self._lock:
            self._frame = frame
        self._event.set()

    def poll(self, max_age: float = 0.8):
        """Doner: (kutular, anonslar, yakalananlar, ham okumalar). Anons/yakalama bir kez verilir."""
        with self._lock:
            boxes, reads, at = self._latest
            ann, caps = self._pending_ann, self._pending_caps
            self._pending_ann, self._pending_caps = [], []
        if time.time() - at > max_age:
            boxes, reads = [], []
        return boxes, ann, caps, reads

    def _loop(self) -> None:
        while True:
            if not self._event.wait(1.0):
                continue
            with self._lock:
                frame, self._frame = self._frame, None
                self._event.clear()
            if frame is None:
                continue
            try:
                boxes, ann, caps = self.process(frame)
                reads = list(self.last_reads)
            except Exception as e:
                print(f"[plaka] hata: {e!r}")
                time.sleep(0.5)
                continue
            with self._lock:
                self._latest = (boxes, reads, time.time())
                self._pending_ann.extend(ann)
                self._pending_caps.extend(caps)

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
        results = self.alpr.predict(frame) or self.alpr_backup.predict(frame)
        for r in results:
            bb = r.detection.bounding_box
            xyxy = (int(bb.x1), int(bb.y1), int(bb.x2), int(bb.y2))
            if r.ocr is None or not r.ocr.text:
                continue
            conf = r.ocr.confidence
            conf = float(np.mean(conf)) if isinstance(conf, (list, tuple, np.ndarray)) else float(conf)
            text = normalize(r.ocr.text)
            if len(text) < 4 or conf < self.min_conf:
                continue
            fixed = turkish_fix(text)
            if fixed:
                text = fixed[0]
            text = self._vote(text, now)

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

    def _vote(self, text: str, now: float, window: float = 4.0) -> str:
        """Kareler arasi oylama: son birkac saniyede ayni plaka (1 karakter farkla)
        daha cok okunduysa onu kullan. Tek karedeki okuma hatasi kayda gecmez."""
        self.votes = [(p, t) for p, t in getattr(self, "votes", []) if now - t < window]
        self.votes.append((text, now))
        near: dict[str, int] = {}
        for p, _ in self.votes:
            if p == text or _dist(p, text) <= 1:
                near[p] = near.get(p, 0) + 1
        return max(near, key=lambda p: (near[p], p == text))

    def _save(self, frame: np.ndarray, xyxy, plate: str, now: float) -> str:
        img = frame.copy()
        cv2.rectangle(img, xyxy[:2], xyxy[2:], (0, 0, 255), 3)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
        cv2.putText(img, f"{pretty(plate)}  {stamp}", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
        name = f"{plate}_{stamp}.jpg"
        cv2.imwrite(str(CAPTURES / name), img)
        return name
