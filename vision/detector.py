"""Glass Stream goruntu isleme servisi (ISG / fabrika).

Sunucudan (ws://localhost:8000/vision) canli JPEG kareleri alir ve:
  * SH17 KKD modeli (17 sinif: kisi, kafa, yuz, el, baret, yelek, eldiven,
    gozluk, maske, kulaklik...) + ByteTrack ile kisileri takip eder
  * Bir kisiyi ANCAK icinde kafa/yuz goruldugunde sayar (el/kol sayilmaz)
  * Zorunlu KKD'leri (web'den secilir) kisi basina oylamayla kontrol eder,
    eksikse sesli uyari uretir
  * Sayilan her kisinin en net yuz karesini kaydeder
  * QR / barkod okur (ayri thread)
  * Plaka okur, aranan plakalari yakalar
Sonuclar sunucuya JSON olarak gider:
  {"type": "detections", ...}  -> web sayfasi (kutular, sayaclar)
  {"type": "announce", "text"} -> gozlukten sesli okunur

Calistirma:  python vision/detector.py   (ayrinti: --help)
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
import torch
import zxingcpp
from ultralytics import YOLO
from websockets.sync.client import connect

HERE = Path(__file__).resolve().parent
MODELS = HERE / "models"
FACES = HERE / "faces"
PPE_FILE = HERE / "ppe.json"

# SH17 sinif adi -> (Turkce ad, "eksik" hali, kafada mi aranir)
PPE_ITEMS: dict[str, tuple[str, str, bool]] = {
    "helmet": ("Baret", "baretsiz", True),
    "safety-vest": ("Yelek", "yeleksiz", False),
    "gloves": ("Eldiven", "eldivensiz", False),
    "glasses": ("Gözlük", "gözlüksüz", True),
    "face-mask": ("Maske", "maskesiz", True),
    "ear-mufs": ("Kulaklık", "kulaklıksız", True),
}
HEAD_CLASSES = ("head", "face")
COLORS = {"ok": "#3ccf7a", "bad": "#e5534b", "pending": "#8ab4ff"}


# --------------------------------------------------------------------------
class LatestFrame:
    """Yalnizca EN SON kareyi tutar (gecikme birikmesin)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data = None
        self._event = threading.Event()

    def put(self, data) -> None:
        with self._lock:
            self._data = data
        self._event.set()

    def take(self, timeout: float = 1.0):
        if not self._event.wait(timeout):
            return None
        with self._lock:
            data, self._data = self._data, None
            self._event.clear()
        return data


@dataclass
class Person:
    first_seen: float
    frames: int = 0
    head_frames: int = 0  # icinde kafa/yuz goruldugu kare sayisi
    counted: bool = False
    ppe_yes: dict = field(default_factory=dict)  # item -> kac karede goruldu
    ppe_checks: dict = field(default_factory=dict)  # item -> kac karede kontrol edildi
    ppe_status: dict = field(default_factory=dict)  # item -> True (var) / False (eksik)
    face_quality: float = 0.0
    face_saves: int = 0
    face_file: str | None = None


def _inside(pt, box) -> bool:
    return box[0] <= pt[0] <= box[2] and box[1] <= pt[1] <= box[3]


def _center(b):
    return ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)


def _head_region(p):
    """Kisi kutusunun ust %40'i (kafa bolgesi)."""
    return (p[0], p[1], p[2], p[1] + (p[3] - p[1]) * 0.4)


class Detector:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.device = 0 if torch.cuda.is_available() else "cpu"
        print(f"[vision] cihaz: {'GPU ' + torch.cuda.get_device_name(0) if self.device == 0 else 'CPU'}")

        self.model = YOLO(str(MODELS / args.model))
        self.names = self.model.names
        self.cls_id = {v: k for k, v in self.names.items()}
        print(f"[vision] KKD modeli: {args.model} siniflar={list(self.names.values())}")

        self.required: list[str] = self._load_ppe()
        self.codes = CodeReaderThread()

        self.objects = None
        if args.objects:
            from objects import ObjectFinder

            self.objects = ObjectFinder(self.device)
            print(f"[vision] genis nesne kutuphanesi acik (mod: {self.objects.mode})")

        self.plates = None
        if args.plates:
            from plates import PlateReader

            self.plates = PlateReader(every=args.plate_every)
            print(f"[vision] plaka okuma acik; aranan: {self.plates.watchlist_pretty() or '-'}")
        self.reset()

    # ---------------- ayarlar ----------------
    def _load_ppe(self) -> list[str]:
        try:
            req = json.loads(PPE_FILE.read_text(encoding="utf-8")).get("required", [])
            return [r for r in req if r in PPE_ITEMS]
        except (OSError, ValueError):
            return ["face-mask"]

    def set_required(self, items: list[str]) -> None:
        self.required = [i for i in items if i in PPE_ITEMS]
        PPE_FILE.write_text(json.dumps({"required": self.required}), encoding="utf-8")
        # Durumlari yeniden degerlendir
        for p in self.people.values():
            p.ppe_status.clear()

    def ppe_config(self) -> dict:
        return {
            "type": "ppe",
            "required": self.required,
            "items": [{"key": k, "name": v[0]} for k, v in PPE_ITEMS.items()],
        }

    def reset(self) -> None:
        self.people: dict[int, Person] = {}
        self.total_people = 0
        self.missing_totals: dict[str, int] = {k: 0 for k in PPE_ITEMS}
        self.ok_totals: dict[str, int] = {k: 0 for k in PPE_ITEMS}
        self.qr_last: dict[str, float] = {}
        self.obj_last_alert: dict[str, float] = {}
        self.obj_seen: dict[str, int] = {}  # ardisik gorulme (yanlis alarmi azaltmak icin)

    # ----------------------------------------------------------------------
    def process(self, frame: np.ndarray) -> tuple[dict, list[str]]:
        h, w = frame.shape[:2]
        now = time.time()
        boxes_out: list[dict] = []
        announcements: list[str] = []
        new_faces: list[dict] = []

        # 1) Tum KKD siniflari + takip (tek model, tek gecis)
        res = self.model.track(
            frame, persist=True, conf=self.args.conf, tracker=str(HERE / "tracker.yaml"),
            device=self.device, verbose=False, half=self.device == 0,
        )[0]
        persons: list[tuple[int, tuple]] = []
        parts: dict[str, list[tuple]] = {}
        if res.boxes is not None and len(res.boxes):
            xyxy = res.boxes.xyxy.cpu().numpy()
            cls = res.boxes.cls.int().cpu().tolist()
            ids = res.boxes.id.int().cpu().tolist() if res.boxes.id is not None else [None] * len(cls)
            for b, c, tid in zip(xyxy, cls, ids):
                name = self.names[c]
                b = tuple(float(v) for v in b)
                if name == "person":
                    if tid is not None:
                        persons.append((tid, b))
                else:
                    parts.setdefault(name, []).append(b)

        # 2) Her kisi icin: kafa/yuz var mi, KKD'ler, yuz kaydi
        for tid, pb in persons:
            p = self.people.get(tid)
            if p is None:
                p = self.people[tid] = Person(first_seen=now)
            p.frames += 1
            head_zone = _head_region(pb)
            heads = [b for n in HEAD_CLASSES for b in parts.get(n, []) if _inside(_center(b), head_zone)]
            has_head = bool(heads)
            if has_head:
                p.head_frames += 1

            # Sayim: yeterli boyut + birkac karede kafa/yuz gorulmus olmali (el/kol sayilmaz)
            tall_enough = (pb[3] - pb[1]) >= h * self.args.min_height
            if not p.counted and tall_enough and p.head_frames >= self.args.min_frames:
                p.counted = True
                self.total_people += 1

            # KKD oylama (yalnizca sayilmis kisiler icin)
            if p.counted:
                for item in self.required:
                    head_item = PPE_ITEMS[item][2]
                    if head_item and not has_head:
                        continue  # kafa gorunmuyorsa kafa KKD'si hakkinda karar verme
                    zone = head_zone if head_item else pb
                    found = any(_inside(_center(b), zone) for b in parts.get(item, []))
                    p.ppe_checks[item] = p.ppe_checks.get(item, 0) + 1
                    p.ppe_yes[item] = p.ppe_yes.get(item, 0) + (1 if found else 0)
                    if item not in p.ppe_status and p.ppe_checks[item] >= self.args.ppe_frames:
                        ok = p.ppe_yes[item] / p.ppe_checks[item] >= self.args.ppe_ratio
                        p.ppe_status[item] = ok
                        name, missing_word, _ = PPE_ITEMS[item]
                        if ok:
                            self.ok_totals[item] += 1
                        else:
                            self.missing_totals[item] += 1
                            announcements.append(
                                f"Dikkat, {missing_word} kişi. {missing_word.capitalize()} toplam {self.missing_totals[item]}."
                            )

            # Yuz kaydi
            if p.counted and self.args.save_faces:
                faces = [b for b in parts.get("face", []) if _inside(_center(b), head_zone)]
                if faces:
                    fb = max(faces, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
                    saved = self._maybe_save_face(tid, p, frame, fb, now)
                    if saved:
                        new_faces.append(saved)

            # Kutu + etiket
            missing = [PPE_ITEMS[i][0] for i, ok in p.ppe_status.items() if not ok and i in self.required]
            if not p.counted:
                label, color = f"#{tid} ?", COLORS["pending"]
            elif missing:
                label, color = f"#{tid} EKSİK: {', '.join(missing)}", COLORS["bad"]
            elif self.required and all(i in p.ppe_status for i in self.required):
                label, color = f"#{tid} KKD tamam", COLORS["ok"]
            else:
                label, color = f"#{tid} Kişi", COLORS["pending"]
            boxes_out.append(_box(pb, w, h, label, color))

        # KKD parcalarini ince cizgiyle goster
        for name, blist in parts.items():
            tr = PPE_ITEMS.get(name, (name,))[0] if name in PPE_ITEMS else {
                "head": "kafa", "face": "yüz", "hands": "el", "tool": "alet", "shoes": "ayakkabı",
            }.get(name)
            if not tr:
                continue
            for b in blist:
                boxes_out.append(_box(b, w, h, tr, "#f2c94c" if name in PPE_ITEMS else "#6b7280", thin=True))

        # 3) QR / barkod (ayri thread)
        self.codes.submit(frame)
        for text, xyxy, kind in self.codes.latest():
            boxes_out.append(_box(xyxy, w, h, f"{kind}: {text[:40]}", "#f2c94c"))
            if now - self.qr_last.get(text, 0) > self.args.qr_cooldown:
                announcements.append(f"{'QR kod' if kind == 'QR' else 'Barkod'}: {text}")
            self.qr_last[text] = now

        # 4) Genis nesne kutuphanesi (ayri thread)
        objects_now: dict[str, int] = {}
        if self.objects is not None:
            self.objects.submit(frame)
            for xyxy, name, conf in self.objects.latest():
                objects_now[name] = objects_now.get(name, 0) + 1
                boxes_out.append(_box(xyxy, w, h, f"{name} {conf:.2f}", "#22d3ee"))
            for name in list(self.obj_seen):
                if name not in objects_now:
                    self.obj_seen[name] = 0
            for name in objects_now:
                self.obj_seen[name] = self.obj_seen.get(name, 0) + 1
                if (name in self.objects.alerts and self.obj_seen[name] == 4
                        and now - self.obj_last_alert.get(name, 0) > self.args.obj_cooldown):
                    self.obj_last_alert[name] = now
                    announcements.append(f"Dikkat: {name} görüldü.")

        # 5) Plakalar
        plate_captures: list[dict] = []
        if self.plates is not None:
            pboxes, pann, plate_captures = self.plates.process(frame)
            for xyxy, label, wanted in pboxes:
                boxes_out.append(_box(xyxy, w, h, label, "#ff4d4d" if wanted else "#c792ea"))
            announcements.extend(pann)

        visible = sum(1 for tid, _ in persons if self.people[tid].counted)
        result = {
            "type": "detections",
            "boxes": boxes_out,
            "counts": {
                "visible": visible,
                "total_people": self.total_people,
                "ppe": [
                    {"key": k, "name": PPE_ITEMS[k][0], "ok": self.ok_totals[k], "missing": self.missing_totals[k]}
                    for k in self.required
                ],
            },
            "faces": new_faces,
            "plate_captures": plate_captures,
            "objects": objects_now,
            "objects_ms": self.objects.ms if self.objects else None,
        }
        return result, announcements

    # ----------------------------------------------------------------------
    def _maybe_save_face(self, tid: int, p: Person, frame: np.ndarray, fb, now: float) -> dict | None:
        """Kisinin en net yuzunu kaydet (ilk kayit + belirgin iyilesmede en fazla 3 guncelleme)."""
        x1, y1, x2, y2 = (int(v) for v in fb)
        fw, fh = x2 - x1, y2 - y1
        if fw < self.args.face_min_px or fh < self.args.face_min_px:
            return None
        # Biraz pay birak
        pad = int(max(fw, fh) * 0.35)
        H, W = frame.shape[:2]
        crop = frame[max(0, y1 - pad):min(H, y2 + pad), max(0, x1 - pad):min(W, x2 + pad)]
        if crop.size == 0:
            return None
        sharp = cv2.Laplacian(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()
        quality = fw * fh * min(sharp, 500) / 500
        if p.face_file and (quality < p.face_quality * 1.5 or p.face_saves >= 3):
            return None
        day = time.strftime("%Y%m%d", time.localtime(now))
        folder = FACES / day
        folder.mkdir(parents=True, exist_ok=True)
        name = p.face_file or f"kisi_{time.strftime('%H%M%S', time.localtime(p.first_seen))}_{tid}.jpg"
        cv2.imwrite(str(folder / name), crop, [cv2.IMWRITE_JPEG_QUALITY, 92])
        first = p.face_file is None
        p.face_file, p.face_quality, p.face_saves = name, quality, p.face_saves + 1
        return {"id": tid, "file": f"{day}/{name}", "time": now, "updated": not first}


# --------------------------------------------------------------------------
class CodeReaderThread:
    """QR/barkod okumayi ayri thread'de yapar; ana analiz dongusunu yavaslatmaz."""

    def __init__(self) -> None:
        self._frame = LatestFrame()
        self._lock = threading.Lock()
        self._results: list = []
        self._at = 0.0
        threading.Thread(target=self._loop, daemon=True).start()

    def submit(self, frame: np.ndarray) -> None:
        self._frame.put(frame)

    def latest(self, max_age: float = 0.7) -> list:
        with self._lock:
            return list(self._results) if time.time() - self._at < max_age else []

    def _loop(self) -> None:
        while True:
            frame = self._frame.take(timeout=1.0)
            if frame is None:
                continue
            try:
                res = read_codes(frame)
            except Exception as e:
                print(f"[vision] QR okuma hatasi: {e!r}")
                res = []
            with self._lock:
                self._results, self._at = res, time.time()


_code_frame = 0


def read_codes(frame: np.ndarray) -> list[tuple[str, tuple[float, float, float, float], str]]:
    """QR ve barkodlari oku. Dondurur: [(metin, (x1,y1,x2,y2), 'QR'|'Barkod')]"""
    global _code_frame
    _code_frame += 1
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    out, seen = [], set()
    # Her karede normal boyut; 3 karede bir de 2x buyutulmus hali (uzaktaki kucuk QR'lar icin)
    passes = [(1.0, gray)] + ([(2.0, None)] if _code_frame % 3 == 0 else [])
    for scale, img in passes:
        if img is None:
            if out:
                break
            img = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_LINEAR)
        for r in zxingcpp.read_barcodes(img):
            if not r.text or r.text in seen:
                continue
            seen.add(r.text)
            p = r.position
            xs = [p.top_left.x, p.top_right.x, p.bottom_left.x, p.bottom_right.x]
            ys = [p.top_left.y, p.top_right.y, p.bottom_left.y, p.bottom_right.y]
            kind = "QR" if "QR" in str(r.format) else "Barkod"
            out.append((r.text, (min(xs) / scale, min(ys) / scale, max(xs) / scale, max(ys) / scale), kind))
    return out


def _box(xyxy, w: int, h: int, label: str, color: str, thin: bool = False) -> dict:
    x1, y1, x2, y2 = (float(v) for v in xyxy)
    return {"x1": x1 / w, "y1": y1 / h, "x2": x2 / w, "y2": y2 / h, "label": label, "color": color, "thin": thin}


# --------------------------------------------------------------------------
def run(args: argparse.Namespace) -> None:
    det = Detector(args)
    url = args.server
    while True:
        try:
            print(f"[vision] baglaniliyor: {url}")
            with connect(url, max_size=8 * 1024 * 1024) as ws:
                print("[vision] baglandi, kare bekleniyor")
                latest = LatestFrame()
                stop = threading.Event()
                send_lock = threading.Lock()

                def send(obj: dict) -> None:
                    with send_lock:
                        ws.send(json.dumps(obj, ensure_ascii=False))

                def send_config() -> None:
                    send(det.ppe_config())
                    if det.objects is not None:
                        send(det.objects.config())
                    if det.plates is not None:
                        send({"type": "watchlist", "plates": det.plates.watchlist_pretty()})

                send_config()

                def receiver() -> None:
                    try:
                        for msg in ws:
                            if isinstance(msg, bytes):
                                latest.put(msg)
                                continue
                            try:
                                cmd = json.loads(msg)
                            except json.JSONDecodeError:
                                continue
                            t = cmd.get("type")
                            if t == "reset":
                                det.reset()
                                print("[vision] sayaclar sifirlandi")
                            elif t == "watchlist_set" and det.plates is not None:
                                det.plates.set_watchlist(cmd.get("plates", []))
                                print(f"[vision] aranan plakalar: {det.plates.watchlist_pretty()}")
                                send_config()
                            elif t == "ppe_set":
                                det.set_required(cmd.get("required", []))
                                print(f"[vision] zorunlu KKD: {det.required}")
                                send_config()
                            elif t == "objects_set" and det.objects is not None:
                                det.objects.configure(cmd)
                                print(f"[vision] nesne ayari: mod={det.objects.mode} liste={det.objects.items}")
                                send_config()
                            elif t == "hello":
                                send_config()
                    finally:
                        stop.set()

                threading.Thread(target=receiver, daemon=True).start()

                n, t0 = 0, time.time()
                while not stop.is_set():
                    data = latest.take(timeout=1.0)
                    if data is None:
                        continue
                    frame = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
                    if frame is None:
                        continue
                    t = time.time()
                    result, announcements = det.process(frame)
                    result["latency_ms"] = round((time.time() - t) * 1000)
                    n += 1
                    if time.time() - t0 >= 1:
                        result["fps"] = round(n / (time.time() - t0), 1)
                        n, t0 = 0, time.time()
                    send(result)
                    for text in announcements:
                        print(f"[vision] anons: {text}")
                        send({"type": "announce", "text": text})
        except Exception as e:  # sunucu kapali vb.
            print(f"[vision] baglanti hatasi: {e!r}; 2 sn sonra tekrar")
            time.sleep(2)


def main() -> None:
    ap = argparse.ArgumentParser(description="Glass Stream goruntu isleme servisi (ISG)")
    ap.add_argument("--server", default="ws://localhost:8000/vision")
    ap.add_argument("--model", default="ppe_sh17.pt", help="SH17 KKD modeli (vision/models icinde)")
    ap.add_argument("--conf", type=float, default=0.35, help="tespit esigi")
    ap.add_argument("--min-frames", type=int, default=3, help="kisiyi saymak icin kafa/yuz goruldugu kare sayisi")
    ap.add_argument("--min-height", type=float, default=0.12, help="sayilacak kisinin min. yuksekligi (kare orani)")
    ap.add_argument("--ppe-frames", type=int, default=8, help="KKD kararini vermek icin kontrol edilen kare")
    ap.add_argument("--ppe-ratio", type=float, default=0.3, help="KKD 'var' sayilmasi icin gorulme orani")
    ap.add_argument("--no-faces", dest="save_faces", action="store_false", help="yuz kaydini kapat")
    ap.add_argument("--face-min-px", type=int, default=40, help="kaydedilecek yuzun min. boyutu (piksel)")
    ap.add_argument("--qr-cooldown", type=float, default=15.0, help="ayni QR'i tekrar okumadan once bekleme (sn)")
    ap.add_argument("--no-objects", dest="objects", action="store_false", help="genis nesne kutuphanesini kapat")
    ap.add_argument("--obj-cooldown", type=float, default=30.0, help="ayni nesne uyarisi arasi bekleme (sn)")
    ap.add_argument("--no-plates", dest="plates", action="store_false", help="plaka okumayi kapat")
    ap.add_argument("--plate-every", type=int, default=3, help="plaka okumayi kac karede bir yap")
    run(ap.parse_args())


if __name__ == "__main__":
    main()
