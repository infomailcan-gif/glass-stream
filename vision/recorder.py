"""Video kaydi (PC'de, H.264 MP4 - her tarayicida/telefonda oynar).

Iki mod:
  raw       : gozluk goruntusu oldugu gibi
  annotated : kutular + etiketler cizilmis (ISG raporu / kanit icin)

Kareler degisken hizda gelir; kayit sabit FPS'te tutulur (gerekirse son kare
tekrarlanir), boylece video gercek surede oynar. Kodlama ayri thread'de ffmpeg
ile yapilir; analiz dongusu beklemez.
"""

from __future__ import annotations

import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
RECORDINGS = HERE / "recordings"
FPS = 20


def _find_ffmpeg() -> str | None:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _font(size: int):
    for name in ("segoeuib.ttf", "arialbd.ttf", "arial.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _hex(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def draw_boxes(frame: np.ndarray, boxes: list[dict], footer: str = "") -> np.ndarray:
    """Web'deki kutularin aynisini kareye ciz (Turkce karakter destekli)."""
    h, w = frame.shape[:2]
    img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    d = ImageDraw.Draw(img)
    f_big, f_small = _font(max(12, w // 40)), _font(max(10, w // 55))
    for b in boxes:
        x1, y1, x2, y2 = b["x1"] * w, b["y1"] * h, b["x2"] * w, b["y2"] * h
        col = _hex(b["color"])
        small = b.get("small")
        d.rectangle([x1, y1, x2, y2], outline=col, width=1 if small else 3)
        font = f_small if small else f_big
        tw = d.textlength(b["label"], font=font) + 6
        th = font.size + 4
        ly = y1 - th if y1 - th >= 0 else y1
        d.rectangle([x1, ly, x1 + tw, ly + th], fill=col)
        d.text((x1 + 3, ly + 1), b["label"], fill=(0, 0, 0), font=font)
    if footer:
        th = f_small.size + 8
        d.rectangle([0, h - th, w, h], fill=(0, 0, 0))
        d.text((6, h - th + 3), footer, fill=(255, 255, 255), font=f_small)
    return cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2BGR)


class Recorder:
    def __init__(self) -> None:
        self.ffmpeg = _find_ffmpeg()
        self.active = False
        self.mode = "annotated"
        self.file: str | None = None
        self.started = 0.0
        self._q: queue.Queue = queue.Queue(maxsize=60)
        self._thread: threading.Thread | None = None
        RECORDINGS.mkdir(exist_ok=True)
        print(f"[kayit] ffmpeg: {self.ffmpeg or 'BULUNAMADI'}")

    # ---------------- kontrol ----------------
    def start(self, mode: str = "annotated") -> tuple[bool, str]:
        if self.active:
            return False, "Kayıt zaten sürüyor."
        if not self.ffmpeg:
            return False, "ffmpeg bulunamadı, kayıt yapılamıyor."
        self.mode = mode if mode in ("raw", "annotated") else "annotated"
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.file = f"{stamp}{'-kkd' if self.mode == 'annotated' else ''}.mp4"
        self.started = time.time()
        self.active = True
        self._q = queue.Queue(maxsize=60)
        self._thread = threading.Thread(target=self._encode, args=(RECORDINGS / self.file,), daemon=True)
        self._thread.start()
        return True, "Kayıt başladı."

    def stop(self, on_saved=None) -> tuple[bool, str]:
        """Aninda doner; dosya arka planda tamamlanir, bitince on_saved() cagrilir."""
        if not self.active:
            return False, "Kayıt yok."
        self.active = False
        q, th = self._q, self._thread
        # Bekleyen kareleri atla: durdur komutu geldiyse kuyrugu bosalt, hemen bitir
        try:
            while True:
                q.get_nowait()
        except queue.Empty:
            pass
        q.put(None)
        secs = int(time.time() - self.started)

        def finish() -> None:
            if th:
                th.join(timeout=30)
            if on_saved:
                on_saved()

        threading.Thread(target=finish, daemon=True).start()
        return True, f"Kayıt durdu. Süre {secs // 60} dakika {secs % 60} saniye."

    def status(self) -> dict:
        return {"type": "recording", "active": self.active, "mode": self.mode, "file": self.file,
                "since": self.started if self.active else None, "files": self.list_files()}

    def list_files(self, limit: int = 30) -> list[dict]:
        files = sorted(RECORDINGS.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
        return [{"file": p.name, "mb": round(p.stat().st_size / 1e6, 1), "time": p.stat().st_mtime} for p in files
                if not (self.active and p.name == self.file)]

    # ---------------- kare besleme ----------------
    def feed(self, frame: np.ndarray, boxes: list[dict], footer: str = "") -> None:
        if not self.active:
            return
        try:
            self._q.put_nowait((frame, boxes if self.mode == "annotated" else None, footer, time.time()))
        except queue.Full:
            pass  # disk/islemci yetismezse kare atla; zaman damgasi bosluga gore tekrar eder

    def _encode(self, path: Path) -> None:
        proc = None
        size = None
        written = 0
        t0 = None
        last = None
        try:
            while True:
                item = self._q.get()
                if item is None:
                    break
                frame, boxes, footer, ts = item
                if size is None:
                    h, w = frame.shape[:2]
                    size = (w - w % 2, h - h % 2)  # H.264 cift boyut ister
                    proc = subprocess.Popen(
                        [self.ffmpeg, "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
                         "-s", f"{size[0]}x{size[1]}", "-r", str(FPS), "-i", "-",
                         "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                         "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(path)],
                        stdin=subprocess.PIPE,
                    )
                    t0 = ts
                if (frame.shape[1], frame.shape[0]) != size:
                    frame = cv2.resize(frame, size)  # SDK cozunurlugu degistirirse
                if boxes is not None:
                    frame = draw_boxes(frame, boxes, footer)
                last = frame
                # Gercek zamana gore kac kare yazilmali? Eksikse son kareyi tekrarla.
                due = int((ts - t0) * FPS) + 1
                while written < due:
                    proc.stdin.write(last.tobytes())
                    written += 1
        except Exception as e:
            print(f"[kayit] hata: {e!r}")
        finally:
            if proc:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
                proc.wait(timeout=30)
            print(f"[kayit] kaydedildi: {path.name} ({written} kare)")
