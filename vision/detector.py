"""Glass Stream goruntu isleme servisi.

Sunucudan (ws://localhost:8000/vision) canli JPEG kareleri alir ve:
  * YOLO11 + ByteTrack ile kisileri bulur, her kisiye kalici bir numara verir
  * (varsa) maske modeli ile her kisinin maskeli / maskesiz oldugunu belirler
  * QR kodlari okur
Sonuclari JSON olarak sunucuya geri yollar:
  {"type": "detections", ...}  -> web sayfasinda kutular ve sayaclar
  {"type": "announce", "text"} -> gozlukten sesli okunacak anons

Calistirma:  python vision/detector.py   (ayrinti: --help)
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
import torch
from ultralytics import YOLO
from websockets.sync.client import connect

HERE = Path(__file__).resolve().parent
MODELS = HERE / "models"


# --------------------------------------------------------------------------
# Kare alici: her zaman yalnizca EN SON kareyi tutar (gecikme birikmesin)
# --------------------------------------------------------------------------
class LatestFrame:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: bytes | None = None
        self._event = threading.Event()

    def put(self, data: bytes) -> None:
        with self._lock:
            self._data = data
        self._event.set()

    def take(self, timeout: float = 1.0) -> bytes | None:
        if not self._event.wait(timeout):
            return None
        with self._lock:
            data, self._data = self._data, None
            self._event.clear()
        return data


# --------------------------------------------------------------------------
# Kisi durumu (takip numarasi basina)
# --------------------------------------------------------------------------
@dataclass
class Person:
    first_seen: float
    frames: int = 0
    votes: dict = field(default_factory=lambda: defaultdict(int))
    status: str | None = None  # "mask" | "no_mask" | None (henuz belli degil)
    counted: bool = False


def classify_mask_label(name: str) -> str | None:
    """Model sinif adini 'mask' / 'no_mask' olarak normallestir."""
    n = name.lower().replace("-", "_").replace(" ", "_")
    if "incorrect" in n or "improper" in n or "wrong" in n:
        return "no_mask"  # yanlis takilmis maske = maskesiz say
    if n.startswith(("without", "no_", "nomask", "no")) or "without" in n or "unmask" in n:
        return "no_mask"
    if "mask" in n:
        return "mask"
    return None


class Detector:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.device = 0 if torch.cuda.is_available() else "cpu"
        print(f"[vision] cihaz: {'GPU ' + torch.cuda.get_device_name(0) if self.device == 0 else 'CPU'}")

        self.person_model = YOLO(str(MODELS / args.person_model))
        self.mask_model = None
        mask_path = MODELS / args.mask_model
        if mask_path.exists():
            self.mask_model = YOLO(str(mask_path))
            print(f"[vision] maske modeli: {mask_path.name} siniflar={self.mask_model.names}")
        else:
            print(f"[vision] maske modeli yok ({mask_path.name}) - maske tespiti kapali")

        self.qr = cv2.QRCodeDetector()

        self.plates = None
        if args.plates:
            from plates import PlateReader

            self.plates = PlateReader(every=args.plate_every)
            print(f"[vision] plaka okuma acik; aranan: {self.plates.watchlist_pretty() or '-'}")
        self.reset()

    def reset(self) -> None:
        self.people: dict[int, Person] = {}
        self.total_people = 0
        self.total_mask = 0
        self.total_no_mask = 0
        self.qr_last: dict[str, float] = {}

    # ----------------------------------------------------------------------
    def process(self, frame: np.ndarray) -> tuple[dict, list[str]]:
        h, w = frame.shape[:2]
        now = time.time()
        boxes_out: list[dict] = []
        announcements: list[str] = []

        # 1) Kisiler + takip
        res = self.person_model.track(
            frame, persist=True, classes=[0], conf=self.args.conf,
            tracker="bytetrack.yaml", device=self.device, verbose=False,
        )[0]
        tracks: list[tuple[int, np.ndarray]] = []
        if res.boxes is not None and res.boxes.id is not None:
            for xyxy, tid in zip(res.boxes.xyxy.cpu().numpy(), res.boxes.id.int().cpu().tolist()):
                tracks.append((tid, xyxy))

        # 2) Maske (yuz kutulari) -> kisilere ata
        faces: list[tuple[np.ndarray, str, float]] = []
        if self.mask_model is not None:
            mres = self.mask_model.predict(frame, conf=self.args.mask_conf, device=self.device, verbose=False)[0]
            for xyxy, cls, conf in zip(
                mres.boxes.xyxy.cpu().numpy(), mres.boxes.cls.int().cpu().tolist(), mres.boxes.conf.cpu().tolist()
            ):
                label = classify_mask_label(self.mask_model.names[cls])
                if label:
                    faces.append((xyxy, label, conf))

        face_owner: dict[int, str] = {}
        for fxyxy, label, _ in faces:
            cx, cy = (fxyxy[0] + fxyxy[2]) / 2, (fxyxy[1] + fxyxy[3]) / 2
            best, best_area = None, None
            for tid, pxyxy in tracks:
                if pxyxy[0] <= cx <= pxyxy[2] and pxyxy[1] <= cy <= pxyxy[3]:
                    area = (pxyxy[2] - pxyxy[0]) * (pxyxy[3] - pxyxy[1])
                    if best_area is None or area < best_area:
                        best, best_area = tid, area
            if best is not None:
                face_owner[best] = label

        # 3) Kisi durumlarini guncelle, sayac ve anonslar
        for tid, pxyxy in tracks:
            p = self.people.get(tid)
            if p is None:
                p = self.people[tid] = Person(first_seen=now)
            p.frames += 1
            if tid in face_owner:
                p.votes[face_owner[tid]] += 1

            # Kisa sureli yanlis tespitleri saymamak icin birkac kare bekle
            if not p.counted and p.frames >= self.args.min_frames:
                p.counted = True
                self.total_people += 1

            # Maske durumu: ayni sonuc N kez gorulunce kesinlesir
            if p.status is None and self.mask_model is not None:
                for label in ("no_mask", "mask"):
                    if p.votes[label] >= self.args.mask_votes and p.votes[label] > p.votes[_other(label)]:
                        p.status = label
                        if label == "mask":
                            self.total_mask += 1
                            announcements.append(f"Yeni maskeli kişi. Maskeli toplam {self.total_mask}.")
                        else:
                            self.total_no_mask += 1
                            announcements.append(f"Dikkat, maskesiz kişi. Maskesiz toplam {self.total_no_mask}.")
                        break

            label_txt = {"mask": "Maskeli", "no_mask": "MASKESİZ"}.get(p.status or "", "Kişi")
            color = {"mask": "#3ccf7a", "no_mask": "#e5534b"}.get(p.status or "", "#8ab4ff")
            boxes_out.append(_box(pxyxy, w, h, f"#{tid} {label_txt}", color))

        # Yuz kutularini da ince cizgiyle goster
        for fxyxy, label, conf in faces:
            color = "#3ccf7a" if label == "mask" else "#e5534b"
            boxes_out.append(_box(fxyxy, w, h, f"{'maske' if label == 'mask' else 'maskesiz'} {conf:.2f}", color, thin=True))

        # 4) QR
        try:
            ok, texts, points, _ = self.qr.detectAndDecodeMulti(frame)
        except cv2.error:
            ok, texts, points = False, (), None
        if ok and points is not None:
            for text, pts in zip(texts, points):
                if not text:
                    continue
                xs, ys = pts[:, 0], pts[:, 1]
                boxes_out.append(_box((xs.min(), ys.min(), xs.max(), ys.max()), w, h, f"QR: {text[:40]}", "#f2c94c"))
                if now - self.qr_last.get(text, 0) > self.args.qr_cooldown:
                    announcements.append(f"QR kod: {text}")
                self.qr_last[text] = now

        # 5) Plakalar
        plate_captures: list[dict] = []
        if self.plates is not None:
            pboxes, pann, plate_captures = self.plates.process(frame)
            for xyxy, label, wanted in pboxes:
                boxes_out.append(_box(xyxy, w, h, label, "#ff4d4d" if wanted else "#c792ea"))
            announcements.extend(pann)

        visible = len(tracks)
        result = {
            "plate_captures": plate_captures,
            "type": "detections",
            "boxes": boxes_out,
            "counts": {
                "visible": visible,
                "total_people": self.total_people,
                "mask": self.total_mask,
                "no_mask": self.total_no_mask,
            },
            "mask_model": self.mask_model is not None,
        }
        return result, announcements


def _other(label: str) -> str:
    return "mask" if label == "no_mask" else "no_mask"


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

                def send_watchlist() -> None:
                    if det.plates is not None:
                        ws.send(json.dumps({"type": "watchlist", "plates": det.plates.watchlist_pretty()}, ensure_ascii=False))

                send_watchlist()

                def receiver() -> None:
                    try:
                        for msg in ws:
                            if isinstance(msg, bytes):
                                latest.put(msg)
                            else:
                                try:
                                    cmd = json.loads(msg)
                                except json.JSONDecodeError:
                                    continue
                                if cmd.get("type") == "reset":
                                    det.reset()
                                    print("[vision] sayaclar sifirlandi")
                                elif cmd.get("type") == "watchlist_set" and det.plates is not None:
                                    det.plates.set_watchlist(cmd.get("plates", []))
                                    print(f"[vision] aranan plakalar: {det.plates.watchlist_pretty()}")
                                    send_watchlist()
                                elif cmd.get("type") == "hello":
                                    send_watchlist()
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
                    ws.send(json.dumps(result, ensure_ascii=False))
                    for text in announcements:
                        print(f"[vision] anons: {text}")
                        ws.send(json.dumps({"type": "announce", "text": text}, ensure_ascii=False))
        except (OSError, Exception) as e:  # sunucu kapali vb.
            print(f"[vision] baglanti hatasi: {e!r}; 2 sn sonra tekrar")
            time.sleep(2)


def main() -> None:
    ap = argparse.ArgumentParser(description="Glass Stream goruntu isleme servisi")
    ap.add_argument("--server", default="ws://localhost:8000/vision")
    ap.add_argument("--person-model", default="yolo11n.pt")
    ap.add_argument("--mask-model", default="mask.pt")
    ap.add_argument("--conf", type=float, default=0.4, help="kisi tespiti esigi")
    ap.add_argument("--mask-conf", type=float, default=0.45, help="maske tespiti esigi")
    ap.add_argument("--min-frames", type=int, default=5, help="kisiyi saymak icin gereken kare")
    ap.add_argument("--mask-votes", type=int, default=3, help="maske durumunu kesinlestirmek icin gereken kare")
    ap.add_argument("--qr-cooldown", type=float, default=15.0, help="ayni QR'i tekrar okumadan once bekleme (sn)")
    ap.add_argument("--no-plates", dest="plates", action="store_false", help="plaka okumayi kapat")
    ap.add_argument("--plate-every", type=int, default=3, help="plaka okumayi kac karede bir yap")
    run(ap.parse_args())


if __name__ == "__main__":
    main()
