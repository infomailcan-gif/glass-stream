"""Yuz bulma (YuNet) + yuz tanima (ArcFace R50 veya SFace).

Amac: kafaya takili kamerada takip numarasi (track id) sik kaybolur; ayni kisi
tekrar gorundugunde yuzunden taninir ve YENIDEN SAYILMAZ. Her kisinin en net
yuz karesi kaydedilir.

Modeller (vision/models):
  face_detection_yunet_2023mar.onnx   - yuz + 5 nokta (OpenCV Zoo, MIT)
  arcface_r50.onnx                     - InsightFace buffalo_l w600k_r50
                                         (LISANS: ticari olmayan / arastirma)
  face_recognition_sface_2021dec.onnx  - SFace (OpenCV Zoo, Apache-2.0) yedek

Kimlikler yalnizca bellekte tutulur; yuz vektorleri diske yazilmaz.
Isler ayri thread'de yapilir; ana analiz dongusu beklemez.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
MODELS = HERE / "models"
FACES = HERE / "faces"

# ArcFace 112x112 hizalama sablonu (goruntunun solundaki goz once)
ARC_TEMPLATE = np.array(
    [[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366], [41.5493, 92.3655], [70.7299, 92.2041]],
    dtype=np.float32,
)


@dataclass
class Identity:
    id: int
    feats: list = field(default_factory=list)
    first_seen: float = 0.0
    last_seen: float = 0.0
    face_file: str | None = None
    face_quality: float = 0.0
    face_saves: int = 0

    def score(self, f: np.ndarray) -> float:
        return max(float(np.dot(f, g)) for g in self.feats)

    def add(self, f: np.ndarray) -> None:
        if len(self.feats) < 10:
            self.feats.append(f)
        else:
            self.feats[1 + (len(self.feats) % 9)] = f  # ilk vektoru koru


class FaceID:
    def __init__(self, backend: str = "arcface") -> None:
        self.det = cv2.FaceDetectorYN.create(
            str(MODELS / "face_detection_yunet_2023mar.onnx"), "", (320, 320), 0.75, 0.3, 20
        )
        self.backend = backend if (MODELS / "arcface_r50.onnx").exists() else "sface"
        if self.backend == "arcface":
            import onnxruntime as ort

            self.sess = ort.InferenceSession(str(MODELS / "arcface_r50.onnx"), providers=["CPUExecutionProvider"])
            self.inp = self.sess.get_inputs()[0].name
            self.same = 0.42  # ArcFace kosinus esigi
        else:
            self.rec = cv2.FaceRecognizerSF.create(str(MODELS / "face_recognition_sface_2021dec.onnx"), "")
            self.same = 0.40
        self.identities: list[Identity] = []
        print(f"[faceid] yuz tanima: {self.backend}")

    def reset(self) -> None:
        self.identities = []

    # ------------------------------------------------------------------
    def find_face(self, frame: np.ndarray, region) -> np.ndarray | None:
        """Bolge icinde en buyuk yuzu bul. Dondurur: YuNet satiri (tam kare koordinatinda)"""
        H, W = frame.shape[:2]
        x1, y1, x2, y2 = (int(v) for v in region)
        x1, y1, x2, y2 = max(0, x1), max(0, y1), min(W, x2), min(H, y2)
        if x2 - x1 < 16 or y2 - y1 < 16:
            return None
        crop = frame[y1:y2, x1:x2]
        scale = max(1.0, 160.0 / min(crop.shape[:2]))  # kucuk (uzak) yuzleri buyut
        if scale > 1.0:
            crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_LINEAR)
        self.det.setInputSize((crop.shape[1], crop.shape[0]))
        _, faces = self.det.detect(crop)
        if faces is None or len(faces) == 0:
            return None
        f = max(faces, key=lambda r: r[2] * r[3]).copy()
        f[:14] = f[:14] / scale
        f[0] += x1
        f[1] += y1
        f[4:14:2] += x1
        f[5:14:2] += y1
        return f

    def embed(self, frame: np.ndarray, f: np.ndarray) -> np.ndarray:
        if self.backend == "arcface":
            pts = f[4:14].reshape(5, 2).astype(np.float32)
            M, _ = cv2.estimateAffinePartial2D(pts, ARC_TEMPLATE, method=cv2.LMEDS)
            face = cv2.warpAffine(frame, M, (112, 112), borderValue=0.0)
            blob = cv2.dnn.blobFromImage(face, 1.0 / 127.5, (112, 112), (127.5, 127.5, 127.5), swapRB=True)
            feat = self.sess.run(None, {self.inp: blob})[0].flatten()
        else:
            feat = self.rec.feature(self.rec.alignCrop(frame, f)).flatten()
        feat = feat.astype(np.float32)
        return feat / (np.linalg.norm(feat) + 1e-9)

    def identify(self, feat: np.ndarray, now: float) -> tuple[Identity, bool]:
        """Bilinen bir kisiyse onu, degilse yeni kimlik dondurur. (kimlik, yeni_mi)"""
        best, best_s = None, -1.0
        for ident in self.identities:
            s = ident.score(feat)
            if s > best_s:
                best, best_s = ident, s
        if best is not None and best_s >= self.same:
            best.add(feat)
            best.last_seen = now
            return best, False
        ident = Identity(id=len(self.identities) + 1, feats=[feat], first_seen=now, last_seen=now)
        self.identities.append(ident)
        return ident, True

    def save_face(self, ident: Identity, frame: np.ndarray, f: np.ndarray, now: float, min_px: int) -> str | None:
        """Kimligin en net yuzunu kaydet (ilk + belirgin iyilesmede en fazla 4 guncelleme)."""
        x, y, w, h = (int(v) for v in f[:4])
        if w < min_px or h < min_px:
            return None
        H, W = frame.shape[:2]
        pad = int(max(w, h) * 0.4)
        crop = frame[max(0, y - pad):min(H, y + h + pad), max(0, x - pad):min(W, x + w + pad)]
        if crop.size == 0:
            return None
        sharp = cv2.Laplacian(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()
        frontal = float(f[14])  # YuNet guveni (cepheden bakista yuksek)
        quality = w * h * min(sharp, 400) / 400 * frontal
        if ident.face_file and (quality < ident.face_quality * 1.3 or ident.face_saves >= 4):
            return None
        day = time.strftime("%Y%m%d", time.localtime(now))
        (FACES / day).mkdir(parents=True, exist_ok=True)
        if not ident.face_file:
            ident.face_file = f"{day}/kisi{ident.id:03d}_{time.strftime('%H%M%S', time.localtime(now))}.jpg"
        cv2.imwrite(str(FACES / ident.face_file), crop, [cv2.IMWRITE_JPEG_QUALITY, 92])
        ident.face_quality, ident.face_saves = quality, ident.face_saves + 1
        return ident.face_file


class FaceWorker:
    """Ayri thread: (kare, [(track_id, kafa_bolgesi)]) isini alir, kimlikleri cikarir."""

    def __init__(self, backend: str, min_px: int, save: bool) -> None:
        self.fid = FaceID(backend)
        self.min_px = min_px
        self.save = save
        self._lock = threading.Lock()
        self._job = None
        self._event = threading.Event()
        self.track_ident: dict[int, int] = {}  # track id -> kimlik id
        self.events: list[dict] = []  # yeni/guncellenen yuz kayitlari (ana dongu toplar)
        self.boxes: dict[int, tuple] = {}  # track id -> son yuz kutusu (x1,y1,x2,y2)
        threading.Thread(target=self._loop, daemon=True).start()

    def reset(self) -> None:
        with self._lock:
            self.fid.reset()
            self.track_ident.clear()
            self.boxes.clear()

    def submit(self, frame: np.ndarray, regions: list[tuple[int, tuple]]) -> None:
        if not regions:
            return
        with self._lock:
            if self._job is None:  # mesgulse yeni isi atla (bir sonraki karede tekrar gelir)
                self._job = (frame, regions[:3])
                self._event.set()

    def ident_of(self, tid: int) -> int | None:
        with self._lock:
            return self.track_ident.get(tid)

    def take_events(self) -> list[dict]:
        with self._lock:
            ev, self.events = self.events, []
        return ev

    @property
    def count(self) -> int:
        return len(self.fid.identities)

    def _loop(self) -> None:
        while True:
            if not self._event.wait(1.0):
                continue
            with self._lock:
                frame, regions = self._job
                self._event.clear()
            now = time.time()
            try:
                for tid, region in regions:
                    f = self.fid.find_face(frame, region)
                    if f is None:
                        continue
                    feat = self.fid.embed(frame, f)
                    with self._lock:
                        ident, is_new = self.fid.identify(feat, now)
                        self.track_ident[tid] = ident.id
                        self.boxes[tid] = (f[0], f[1], f[0] + f[2], f[1] + f[3], now)
                    saved = self.fid.save_face(ident, frame, f, now, self.min_px) if self.save else None
                    with self._lock:
                        self.events.append({"tid": tid, "ident": ident.id, "new": is_new,
                                            "file": saved, "time": now})
            except Exception as e:
                print(f"[faceid] hata: {e!r}")
            with self._lock:
                self._job = None
