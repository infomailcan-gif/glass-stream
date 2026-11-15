"""Glass Stream goruntu isleme servisi (ISG / fabrika).

Sunucudan (ws://localhost:8000/vision) canli JPEG kareleri alir ve:
  * SH17-M KKD modeli (kisi, kafa, yuz, el, baret, yelek, eldiven, gozluk,
    maske, kulaklik...) + BoT-SORT takip
  * Kisiyi ANCAK kafa/yuz gorulurse sayar (el/kol sayilmaz)
  * Yuz tanima (YuNet + ArcFace): ayni kisi tekrar gorulunce YENIDEN SAYILMAZ;
    her kisinin en net yuz karesi kaydedilir
  * Zorunlu KKD'leri (web'den secilir, bone dahil) kisi basina oylamayla
    kontrol eder; eksikse bir kez sesli uyari
  * Genis nesne kutuphanesi (YOLOE), QR/barkod, plaka okuma
Kutular yalnizca ilgili bolgeyi (kafa/yuz) cevreler.

Calistirma: start-vision.cmd  (veya vision/models icinden: python ../detector.py)
"""

from __future__ import annotations

import argparse
import base64
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
PPE_FILE = HERE / "ppe.json"
MODELS_FILE = HERE / "models.json"

# Web ve mobilden acilip kapatilabilen modeller (anahtar -> ad, aciklama, varsayilan)
MODEL_INFO: dict[str, tuple[str, str, bool]] = {
    "ppe": ("Kişi ve KKD", "SH17-X · kişi sayımı, baret, maske, yelek…", True),
    "faces": ("Yüz tanıma", "YuNet + ArcFace R100 · tekrar sayımı önler, yüz kaydı", True),
    "objects": ("Nesne kütüphanesi", "YOLOE-L · 4.585 tür veya özel liste", True),
    "vehicles": ("Araç tanıma", "YOLO11-X · tür, renk, plaka eşleştirme, araç günlüğü", True),
    "plates": ("Plaka okuma", "YOLOv9-S-608 + CCT · tüm plakalar + aranan plaka alarmı", True),
    "qr": ("QR / Barkod", "zxing-cpp", True),
    "hairnet": ("Bone tespiti", "YOLOE metin istemi · deneysel", False),
    "claude": ("Claude asistan", "Claude Code · sesli soru-cevap", True),
}

# KKD anahtari -> (Turkce ad, "eksik" hali, kafada mi aranir)
PPE_ITEMS: dict[str, tuple[str, str, bool]] = {
    "helmet": ("Baret", "baretsiz", True),
    "hairnet": ("Bone", "bonesiz", True),  # deneysel: YOLOE metin istemiyle
    "face-mask": ("Maske", "maskesiz", True),
    "glasses": ("Gözlük", "gözlüksüz", True),
    "ear-mufs": ("Kulaklık", "kulaklıksız", True),
    "safety-vest": ("Yelek", "yeleksiz", False),
    "gloves": ("Eldiven", "eldivensiz", False),
}
HAIRNET_PROMPTS = {"hairnet": ["hairnet", "hair net cap", "disposable bouffant cap"]}
HEAD_CLASSES = ("head", "face")
COLORS = {"ok": "#3ccf7a", "bad": "#e5534b", "pending": "#8ab4ff"}


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
class Track:
    first_seen: float
    frames: int = 0
    head_frames: int = 0
    ident: int | None = None  # yuz tanima kimligi
    counted_by_track: bool = False  # yuz gorulmeden (arkadan) sayildi
    last_face_try: int = -99
    ppe_yes: dict = field(default_factory=dict)
    ppe_checks: dict = field(default_factory=dict)


def _inside(pt, box) -> bool:
    return box[0] <= pt[0] <= box[2] and box[1] <= pt[1] <= box[3]


def _center(b):
    return ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)


def _area(b) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _head_zone(p):
    """Kisi kutusunun ust %40'i (kafa bolgesi)."""
    return (p[0], p[1], p[2], p[1] + (p[3] - p[1]) * 0.4)


def _expand(b, k: float, W: int, H: int):
    w, h = b[2] - b[0], b[3] - b[1]
    return (max(0, b[0] - w * k), max(0, b[1] - h * k), min(W, b[2] + w * k), min(H, b[3] + h * k))


class Detector:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.device = 0 if torch.cuda.is_available() else "cpu"
        print(f"[vision] cihaz: {'GPU ' + torch.cuda.get_device_name(0) if self.device == 0 else 'CPU'}")

        model = next((m for m in (args.model, "ppe_sh17x.pt", "ppe_sh17m.pt", "ppe_sh17.pt") if (MODELS / m).exists()))
        self.model = YOLO(str(MODELS / model))
        self.names = self.model.names
        print(f"[vision] KKD modeli: {model}")

        self.required: list[str] = self._load_ppe()
        self.enabled: dict[str, bool] = self._load_models()
        self.codes = CodeReaderThread()

        from faceid import FaceWorker

        self.faces = FaceWorker(args.face_backend, args.face_min_px, args.save_faces)

        from objects import ObjectFinder, PromptFinder

        self.objects = ObjectFinder(self.device) if args.objects else None
        if self.objects is not None:
            self.objects.enabled = self.enabled["objects"]  # tek kaynak: model anahtarlari
        self.hairnet = PromptFinder(self.device, HAIRNET_PROMPTS, rate_hz=5)

        from vehicles import VehicleTracker

        self.vehicles = VehicleTracker(self.device)

        self.plates = None
        if args.plates:
            from plates import PlateReader

            self.plates = PlateReader()
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
        self.ppe_status.clear()
        for t in self.tracks.values():
            t.ppe_yes.clear()
            t.ppe_checks.clear()

    def ppe_config(self) -> dict:
        return {
            "type": "ppe",
            "required": self.required,
            "items": [{"key": k, "name": v[0] + (" (deneysel)" if k == "hairnet" else "")} for k, v in PPE_ITEMS.items()],
        }

    # ---------------- model anahtarlari ----------------
    def _load_models(self) -> dict[str, bool]:
        on = {k: v[2] for k, v in MODEL_INFO.items()}
        try:
            on.update({k: bool(v) for k, v in json.loads(MODELS_FILE.read_text(encoding="utf-8")).items() if k in on})
        except (OSError, ValueError):
            pass
        return on

    def set_models(self, patch: dict) -> None:
        for k, v in patch.items():
            if k in self.enabled:
                self.enabled[k] = bool(v)
        MODELS_FILE.write_text(json.dumps(self.enabled), encoding="utf-8")
        if self.objects is not None:
            self.objects.enabled = self.enabled["objects"]

    def faces_list(self) -> dict:
        """Sayilan kisilerin yuz kayitlari (web/mobil galerisi icin)."""
        items = [{"id": i.id, "file": i.face_file, "time": i.last_seen or i.first_seen}
                 for i in self.faces.fid.identities if i.face_file]
        return {"type": "faces_list", "items": items[::-1]}

    def models_config(self) -> dict:
        return {"type": "models", "items": [
            {"key": k, "name": n, "desc": d, "on": self.enabled[k]} for k, (n, d, _) in MODEL_INFO.items()
        ]}

    def reset(self) -> None:
        self.tracks: dict[int, Track] = {}
        self.counted_idents: set[int] = set()
        self.total_people = 0
        self.ppe_status: dict[str, dict[str, bool]] = {}  # "i3"/"t17" -> {item: var_mi}
        self.missing_totals = {k: 0 for k in PPE_ITEMS}
        self.ok_totals = {k: 0 for k in PPE_ITEMS}
        self.qr_last: dict[str, float] = {}
        self.obj_last_alert: dict[str, float] = {}
        self.obj_seen: dict[str, int] = {}
        self.frame_i = 0
        self.faces.reset()

    def _key(self, tid: int, t: Track) -> str:
        return f"i{t.ident}" if t.ident else f"t{tid}"

    # ----------------------------------------------------------------------
    def process(self, frame: np.ndarray) -> tuple[dict, list[str]]:
        H, W = frame.shape[:2]
        now = time.time()
        self.frame_i += 1
        boxes_out: list[dict] = []
        announcements: list[str] = []

        # 1) KKD modeli + takip
        persons: list[tuple[int, tuple]] = []
        parts: dict[str, list[tuple]] = {}
        res = None
        if self.enabled["ppe"]:
            res = self.model.track(
                frame, persist=True, conf=self.args.conf, tracker=str(HERE / "tracker.yaml"),
                device=self.device, verbose=False, half=self.device == 0,
            )[0]
        if res is not None and res.boxes is not None and len(res.boxes):
            xyxy = res.boxes.xyxy.cpu().numpy()
            cls = res.boxes.cls.int().cpu().tolist()
            ids = res.boxes.id.int().cpu().tolist() if res.boxes.id is not None else [None] * len(cls)
            for b, c, tid in zip(xyxy, cls, ids):
                name, b = self.names[c], tuple(float(v) for v in b)
                if name == "person":
                    if tid is not None:
                        persons.append((tid, b))
                else:
                    parts.setdefault(name, []).append(b)

        # Bone (deneysel): yalnizca zorunluysa calisir
        if "hairnet" in self.required and self.enabled["hairnet"] and self.enabled["ppe"]:
            self.hairnet.submit(frame)
            for k, bl in self.hairnet.latest().items():
                parts.setdefault(k, []).extend(bl)

        # 2) Yuz tanima isi: kimligi olmayan (veya fotografi guncellenecek) kisiler
        face_jobs = []
        heads: dict[int, tuple | None] = {}
        for tid, pb in persons:
            hz = _head_zone(pb)
            cands = [b for n in HEAD_CLASSES for b in parts.get(n, []) if _inside(_center(b), hz)]
            heads[tid] = max(cands, key=_area) if cands else None
            t = self.tracks.get(tid)
            if t is None:
                t = self.tracks[tid] = Track(first_seen=now)
            if heads[tid] is not None and (t.ident is None or self.frame_i - t.last_face_try > 12):
                face_jobs.append((tid, _expand(heads[tid], 0.35, W, H)))
                t.last_face_try = self.frame_i
        if self.enabled["faces"]:
            self.faces.submit(frame, face_jobs)

        new_faces = []
        for ev in self.faces.take_events():
            if ev["file"]:
                new_faces.append({"id": ev["ident"], "file": ev["file"], "time": ev["time"]})

        # 3) Kisi basina: sayim, KKD, kutu
        for tid, pb in persons:
            t = self.tracks[tid]
            t.frames += 1
            head = heads[tid]
            if head is not None:
                t.head_frames += 1
            tall = (pb[3] - pb[1]) >= H * self.args.min_height or head is not None and _area(head) > (W * H) * 0.004

            # Yuz kimligi geldi mi?
            ident = self.faces.ident_of(tid)
            if ident is not None and t.ident != ident:
                old_key = self._key(tid, t)
                t.ident = ident
                if ident in self.counted_idents:
                    if t.counted_by_track:  # arkadan sayilmisti, aslinda bilinen kisi -> geri al
                        self.total_people -= 1
                        t.counted_by_track = False
                else:
                    self.counted_idents.add(ident)
                    if t.counted_by_track:
                        t.counted_by_track = False  # zaten sayilmisti, kimlige bagla
                    else:
                        self.total_people += 1
                # Track uzerinde verilmis KKD kararlarini kimlige tasi (tekrar uyari olmasin)
                if old_key in self.ppe_status:
                    self.ppe_status.setdefault(self._key(tid, t), {}).update(self.ppe_status.pop(old_key))

            # Yuz hic gorunmuyorsa (arkadan) uzun sure kafa gorulunce yine say
            counted = t.ident is not None or t.counted_by_track
            if not counted and tall and t.head_frames >= self.args.noface_frames:
                t.counted_by_track = True
                self.total_people += 1
                counted = True

            # KKD oylama
            key = self._key(tid, t)
            status = self.ppe_status.setdefault(key, {})
            if counted:
                hz = _head_zone(pb)
                for item in self.required:
                    head_item = PPE_ITEMS[item][2]
                    if head_item and head is None:
                        continue
                    zone = _expand(head, 0.6, W, H) if head_item else pb
                    found = any(_inside(_center(b), zone) for b in parts.get(item, []))
                    t.ppe_checks[item] = t.ppe_checks.get(item, 0) + 1
                    t.ppe_yes[item] = t.ppe_yes.get(item, 0) + (1 if found else 0)
                    if item not in status and t.ppe_checks[item] >= self.args.ppe_frames:
                        ok = t.ppe_yes[item] / t.ppe_checks[item] >= self.args.ppe_ratio
                        status[item] = ok
                        if ok:
                            self.ok_totals[item] += 1
                        else:
                            self.missing_totals[item] += 1
                            word = PPE_ITEMS[item][1]
                            who = f"Kişi {t.ident}" if t.ident else "Bir kişi"
                            announcements.append(f"Dikkat, {who} {word}. {word.capitalize()} toplam {self.missing_totals[item]}.")

            # Kutu: yalnizca kafa/yuz bolgesi
            if head is None:
                continue
            name = f"K{t.ident}" if t.ident else f"#{tid}"
            missing = [PPE_ITEMS[i][0] for i in self.required if status.get(i) is False]
            if not counted:
                label, color = f"{name} ?", COLORS["pending"]
            elif missing:
                label, color = f"{name} EKSİK: {', '.join(missing)}", COLORS["bad"]
            elif self.required and all(i in status for i in self.required):
                label, color = f"{name} KKD tamam", COLORS["ok"]
            else:
                label, color = name, COLORS["pending"]
            boxes_out.append(_box(_expand(head, 0.12, W, H), W, H, label, color))

        # KKD parcalari: kucuk, ince kutular
        for name, blist in parts.items():
            if name not in PPE_ITEMS:
                continue
            for b in blist:
                boxes_out.append(_box(b, W, H, PPE_ITEMS[name][0], "#f2c94c", small=True))

        # 4) QR / barkod
        if self.enabled["qr"]:
            self.codes.submit(frame)
        for text, xyxy, kind in (self.codes.latest() if self.enabled["qr"] else []):
            boxes_out.append(_box(xyxy, W, H, f"{kind}: {text[:40]}", "#f2c94c"))
            if now - self.qr_last.get(text, 0) > self.args.qr_cooldown:
                announcements.append(f"{'QR kod' if kind == 'QR' else 'Barkod'}: {text}")
            self.qr_last[text] = now

        # 5) Genis nesne kutuphanesi
        objects_now: dict[str, int] = {}
        if self.objects is not None and self.enabled["objects"]:
            self.objects.submit(frame)
            for xyxy, name, conf in self.objects.latest():
                objects_now[name] = objects_now.get(name, 0) + 1
                boxes_out.append(_box(xyxy, W, H, name, "#22d3ee", small=True))
            for name in list(self.obj_seen):
                if name not in objects_now:
                    self.obj_seen[name] = 0
            for name in objects_now:
                self.obj_seen[name] = self.obj_seen.get(name, 0) + 1
                if (name in self.objects.alerts and self.obj_seen[name] == 4
                        and now - self.obj_last_alert.get(name, 0) > self.args.obj_cooldown):
                    self.obj_last_alert[name] = now
                    announcements.append(f"Dikkat: {name} görüldü.")

        # 6) Araclar (tur + renk) ve plakalar; plaka icinde bulundugu araca baglanir
        vehicles_on = self.enabled.get("vehicles", True)
        if vehicles_on:
            self.vehicles.update(frame, now)
        plate_captures: list[dict] = []
        if self.plates is not None and self.enabled["plates"]:
            self.plates.submit(frame)  # en buyuk plaka modeli ayri thread'de
            pboxes, pann, plate_captures, preads = self.plates.poll()
            if vehicles_on and preads:
                self.vehicles.attach_plates(preads)
            for xyxy, label, wanted in pboxes:
                boxes_out.append(_box(xyxy, W, H, label, "#ff4d4d" if wanted else "#c792ea", small=not wanted))
            announcements.extend(pann)
        vehicle_log: list[dict] = []
        if vehicles_on:
            for xyxy, label in self.vehicles.boxes():
                boxes_out.append(_box(xyxy, W, H, label, "#6FA8DC"))
            vehicle_log, vsay = self.vehicles.collect(now)
            announcements.extend(vsay)

        visible = sum(1 for tid, _ in persons if self.tracks[tid].ident or self.tracks[tid].counted_by_track)
        return {
            "type": "detections",
            "boxes": boxes_out,
            "counts": {
                "visible": visible,
                "total_people": self.total_people,
                "identities": self.faces.count,
                "ppe": [
                    {"key": k, "name": PPE_ITEMS[k][0], "ok": self.ok_totals[k], "missing": self.missing_totals[k]}
                    for k in self.required
                ],
            },
            "faces": new_faces,
            "plate_captures": plate_captures,
            "vehicle_log": vehicle_log,
            "objects": objects_now,
            "objects_ms": self.objects.ms if self.objects else None,
        }, announcements


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


def _box(xyxy, w: int, h: int, label: str, color: str, small: bool = False) -> dict:
    x1, y1, x2, y2 = (float(v) for v in xyxy)
    return {"x1": x1 / w, "y1": y1 / h, "x2": x2 / w, "y2": y2 / h, "label": label, "color": color, "small": small}


# --------------------------------------------------------------------------
def run(args: argparse.Namespace) -> None:
    from recorder import Recorder

    from assistant import Assistant

    det = Detector(args)
    rec = Recorder()
    state = {"ai": args.ai_on, "frame": None, "seq": None}  # yapay zeka katmani acik mi; son kare (asistan icin)
    sender = {"fn": None}  # aktif baglantinin send fonksiyonu (asistan thread'i kullanir)
    assistant = Assistant(lambda obj: sender["fn"] and sender["fn"](obj), model=args.claude_model)

    # Bilgi Islem modu (donanim taramasi + hata analizi)
    from assistant import frame_jpeg
    from itmode import ITMode, StabilityWatcher, foreground_window_jpeg

    def gsend(obj: dict) -> None:
        if sender["fn"]:
            sender["fn"](obj)

    def gsay(text: str) -> None:
        print(f"[it] anons: {text}")
        gsend({"type": "announce", "text": text})

    itm = ITMode(gsend, gsay)
    still = StabilityWatcher()
    pending = {"capture": 0.0}  # telefondan foto beklenen an

    def it_image(cmd: dict) -> tuple[bytes | None, str]:
        if cmd.get("photo"):
            return base64.b64decode(cmd["photo"]), "gözlük fotoğrafı"
        if state["frame"] is not None:
            return frame_jpeg(state["frame"]), "canlı görüntü"
        return None, "yok"
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
                        try:
                            ws.send(json.dumps(obj, ensure_ascii=False))
                        except Exception:
                            pass  # baglanti kapandiysa (asistan thread'i) sessizce gec

                sender["fn"] = send

                def say(text: str) -> None:
                    print(f"[vision] anons: {text}")
                    send({"type": "announce", "text": text})

                def send_state() -> None:
                    send({"type": "ai_state", "on": state["ai"]})
                    send(rec.status())

                def send_config() -> None:
                    send_state()
                    send(det.models_config())
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
                                latest.put((msg, state.get("seq")))  # kare + telefonun sira numarasi
                                continue
                            if msg.startswith('{"type":"seq"'):  # her kareden once gelir (hizli yol)
                                try:
                                    state["seq"] = json.loads(msg).get("n")
                                except ValueError:
                                    pass
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
                                send_config()
                            elif t == "ppe_set":
                                det.set_required(cmd.get("required", []))
                                print(f"[vision] zorunlu KKD: {det.required}")
                                send_config()
                            elif t == "objects_set" and det.objects is not None:
                                det.objects.configure(cmd)
                                send_config()
                            elif t == "ai":  # {"type":"ai","on":true/false} - web veya sesli komut
                                on = bool(cmd.get("on"))
                                if on != state["ai"]:
                                    state["ai"] = on
                                    say("Yapay zekâ katmanı çalışıyor. Seni dinliyorum." if on
                                        else "Yapay zekâ katmanı kapatıldı.")
                                elif cmd.get("source") == "voice":
                                    say("Yapay zekâ katmanı zaten açık." if on else "Yapay zekâ katmanı zaten kapalı.")
                                send_state()
                            elif t == "record":  # {"type":"record","action":"start"|"stop","mode":...}
                                if cmd.get("action") == "start":
                                    ok, msg = rec.start(cmd.get("mode", "annotated"))
                                else:
                                    ok, msg = rec.stop(on_saved=send_state)  # aninda; dosya arka planda biter
                                say(msg)
                                send_state()
                            elif t == "models_set":  # {"type":"models_set","enabled":{"faces":false,...}}
                                det.set_models(cmd.get("enabled", {}))
                                print(f"[vision] modeller: {det.enabled}")
                                send(det.models_config())
                            elif t == "ask":  # Claude'a soru (sesli komut veya web)
                                print(f"[asistan] soru: {cmd.get('text', '')[:120]}")
                                if det.enabled["claude"]:
                                    assistant.ask(cmd.get("text", ""), cmd.get("photo"), state["frame"])
                                else:
                                    say("Claude asistanı kapalı. Modeller bölümünden açabilirsin.")
                            elif t == "ask_cancel":
                                assistant.cancel()
                            elif t == "it_mode":  # {"type":"it_mode","on":true}
                                on = bool(cmd.get("on"))
                                itm.set_active(on)
                                say("Bilgi işlem modu açık. Cihaza bak ya da tara de; hata için bu hata ne de." if on
                                    else "Bilgi işlem modu kapatıldı.")
                            elif t == "it_scan":  # donanim taramasi (telefon fotografiyla)
                                pending["capture"] = 0.0
                                img, src = it_image(cmd)
                                itm.scan(img, src)
                            elif t == "it_error":  # hata analizi; pc=true -> bu PC'nin ekrani
                                if cmd.get("pc"):
                                    itm.error(foreground_window_jpeg(), "bu bilgisayarın ekranı", cmd.get("text", ""))
                                else:
                                    img, src = it_image(cmd)
                                    itm.error(img, src, cmd.get("text", ""))
                            elif t == "qr":  # telefonda okunan QR (anlik)
                                txt = str(cmd.get("text", ""))[:300]
                                det.qr_last[txt] = time.time()  # PC ayni QR'i tekrar anons etmesin
                                itm.on_qr(txt)
                            elif t == "vehicles_set":  # {"log_all": bool, "speak": bool}
                                if "log_all" in cmd:
                                    det.vehicles.log_all = bool(cmd["log_all"])
                                if "speak" in cmd:
                                    det.vehicles.speak = bool(cmd["speak"])
                                send(det.vehicles.history())
                            elif t == "it_history":
                                send(itm.history())
                            elif t == "hello":
                                send_config()
                                send(det.faces_list())
                                send(itm.state())
                                send(itm.history())
                                send(det.vehicles.history())
                    finally:
                        stop.set()

                threading.Thread(target=receiver, daemon=True).start()

                n, t0 = 0, time.time()
                while not stop.is_set():
                    item = latest.take(timeout=1.0)
                    if item is None:
                        continue
                    data, seq = item
                    frame = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
                    if frame is None:
                        continue
                    state["frame"] = frame
                    t = time.time()
                    if state["ai"]:
                        result, announcements = det.process(frame)
                    else:  # katman kapali: analiz yok, sadece (varsa) kayit
                        result, announcements = {"type": "detections", "boxes": [], "counts": None, "ai_off": True}, []
                    result["latency_ms"] = round((time.time() - t) * 1000)
                    result["seq"] = seq  # telefon bu kareyi kutularla birlikte gosterir (kayma olmaz)
                    n += 1
                    if time.time() - t0 >= 1:
                        result["fps"] = round(n / (time.time() - t0), 1)
                        n, t0 = 0, time.time()
                        if rec.active:
                            result["recording_s"] = int(time.time() - rec.started)
                    c = result.get("counts") or {}
                    footer = time.strftime("%d.%m.%Y %H:%M:%S")
                    if c:
                        footer += f"  |  Kişi: {c.get('total_people', 0)}" + "".join(
                            f"  {p['name']} eksik: {p['missing']}" for p in c.get("ppe", []))
                    rec.feed(frame, result["boxes"], footer)
                    # Bilgi islem modu: goruntu sabitlenince telefondan yuksek cozunurluklu foto iste
                    if itm.active and not itm.busy["hw"]:
                        if pending["capture"] and time.time() - pending["capture"] > 2.5:
                            pending["capture"] = 0.0  # telefon cevap vermedi: canli kareyle tara
                            itm.scan(frame_jpeg(frame), "canlı görüntü")
                        elif not pending["capture"] and still.update(frame):
                            pending["capture"] = time.time()
                            send({"type": "it_capture_request"})
                    send(result)
                    if result.get("faces"):
                        send(det.faces_list())
                    if result.get("vehicle_log"):
                        send(det.vehicles.history())
                    for text in announcements:
                        say(text)
                if rec.active:  # baglanti koptuysa kaydi duzgun kapat
                    rec.stop()
        except Exception as e:
            print(f"[vision] baglanti hatasi: {e!r}; 2 sn sonra tekrar")
            time.sleep(2)


def main() -> None:
    ap = argparse.ArgumentParser(description="Glass Stream goruntu isleme servisi (ISG)")
    ap.add_argument("--server", default="ws://localhost:8000/vision")
    ap.add_argument("--claude-model", default="sonnet", help="Claude Code modeli: sonnet | haiku | opus")
    ap.add_argument("--ai-on", dest="ai_on", action="store_true",
                    help="yapay zeka katmani acik baslasin (varsayilan: kapali; 'yapay zeka katmanini baslat' ile acilir)")
    ap.add_argument("--model", default="ppe_sh17x.pt", help="SH17 KKD modeli (vision/models icinde)")
    ap.add_argument("--conf", type=float, default=0.35, help="tespit esigi")
    ap.add_argument("--min-height", type=float, default=0.12, help="sayilacak kisinin min. yuksekligi (kare orani)")
    ap.add_argument("--noface-frames", type=int, default=25, help="yuz gorunmeden saymak icin gereken kafa karesi")
    ap.add_argument("--ppe-frames", type=int, default=8, help="KKD kararini vermek icin kontrol edilen kare")
    ap.add_argument("--ppe-ratio", type=float, default=0.3, help="KKD 'var' sayilmasi icin gorulme orani")
    ap.add_argument("--face-backend", default="arcface", choices=["arcface", "sface"])
    ap.add_argument("--no-faces", dest="save_faces", action="store_false", help="yuz fotografi kaydini kapat")
    ap.add_argument("--face-min-px", type=int, default=28, help="kaydedilecek yuzun min. boyutu (piksel)")
    ap.add_argument("--qr-cooldown", type=float, default=15.0)
    ap.add_argument("--no-objects", dest="objects", action="store_false")
    ap.add_argument("--obj-cooldown", type=float, default=30.0)
    ap.add_argument("--no-plates", dest="plates", action="store_false")
    ap.add_argument("--plate-every", type=int, default=3)
    run(ap.parse_args())


if __name__ == "__main__":
    main()
