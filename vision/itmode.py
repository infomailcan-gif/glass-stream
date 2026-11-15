"""Bilgi Islem modu.

Iki is:
  * Donanim taramasi ("tara" / goruntu sabit kalinca): switch, firewall, load balancer,
    sunucu, patch panel, UPS... -> cihaz/model, port durumu (bos portlar), isiklar,
    kablolama, etiket/QR, bilinmesi gerekenler.
  * Hata analizi ("bu hata ne"): gozlukle cekilen ekran/cihaz fotografi (sahada) veya
    "bilgisayarimdaki hata" denirse bu PC'nin on plandaki penceresi -> neden + cozum.

Hiz icin: Claude surekli acik (ClaudeRunner), cevabin ILK SATIRI "OZET: ..." -> o satir
gelir gelmez sesli okunur; ayrintilar akarken mobil/web kartina yazilir.
Her kayit vision/it/GUN/<id>.jpg + .json olarak saklanir (envanter/gecmis).
"""

from __future__ import annotations

import base64
import ctypes
import io
import json
import re
import threading
import time
from pathlib import Path

from claude_runner import ClaudeRunner

HERE = Path(__file__).resolve().parent
IT_DIR = HERE / "it"
INVENTORY = HERE / "inventory.json"

HW_PROMPT = """Sen sahadaki bir bilgi işlem / ağ uzmanının akıllı gözlük asistanısın. Türkçe yaz.
Görüntüdeki donanımı analiz et (switch, router, firewall, load balancer, sunucu, storage, patch panel, UPS, kabinet).

Biçim (kesin uy):
İlk satır: "ÖZET: " ile başlayan, sesli okunacak tek cümle (en fazla 20 kelime). Cihaz + en önemli bulgu.
Sonra şu başlıklar, her biri kısa maddeler halinde ("- " ile):
Cihaz: tür, marka, model (etiketten/logodan; emin değilsen "muhtemelen")
Portlar: toplam, dolu, boş; boş port numaraları (soldan sağa, üst sıra tek/alt sıra çift olabilir; emin değilsen belirt); SFP/uplink durumu
Işıklar: link/aktivite, turuncu/kırmızı uyarılar ve hangi portta
Kablolama: düzen, etiket, renk kodu, gevşek/çıkmış kablo, risk
Etiket/QR: okunabilen etiket, hostname, seri no, IP
Bilinmesi gerekenler: bakım, risk, öneri (en fazla 3 madde)
Görüntü yetersizse (uzak, eğik, bulanık) bunu söyle ve nasıl bakılacağını yaz. Uydurma: görmediğini yazma."""

ERR_PROMPT = """Sen sahadaki bir bilgi işlem uzmanının akıllı gözlük asistanısın. Türkçe yaz.
Görüntüde bir hata var (ekran, hata penceresi, mavi ekran, cihaz uyarı ışığı, log, POST/BIOS mesajı vb.).

Biçim (kesin uy):
İlk satır: "ÖZET: " ile başlayan, sesli okunacak tek cümle (en fazla 25 kelime): hata ne + ilk yapılacak şey.
Sonra:
Hata: görüntüdeki hata metni/kodu aynen (okunabildiği kadar)
Neden: 1-2 cümle, olası nedenler
Çözüm: numaralı adımlar (en fazla 6), en olası çözümden başla
Gerekirse komutları ``` içinde ver.
Okunamıyorsa söyle ve yaklaşmasını/açıyı değiştirmesini iste. Uydurma."""


def _stamp(t: float) -> tuple[str, str]:
    lt = time.localtime(t)
    return time.strftime("%Y%m%d", lt), time.strftime("%H%M%S", lt)


def foreground_window_jpeg(max_side: int = 1600) -> bytes | None:
    """Bu PC'nin on plandaki penceresinin goruntusu (yoksa tum ekran)."""
    try:
        from PIL import ImageGrab

        bbox = None
        try:
            import ctypes.wintypes as wt

            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            rect = wt.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            w, h = rect.right - rect.left, rect.bottom - rect.top
            if w > 200 and h > 150:
                bbox = (rect.left, rect.top, rect.right, rect.bottom)
        except Exception:
            bbox = None
        img = ImageGrab.grab(bbox=bbox, all_screens=True) if bbox else ImageGrab.grab(all_screens=True)
        img.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=88)
        return buf.getvalue()
    except Exception as e:
        print(f"[it] ekran goruntusu alinamadi: {e!r}")
        return None


class ITMode:
    def __init__(self, send, say, hw_model: str = "sonnet", err_model: str = "haiku") -> None:
        self.send = send  # JSON -> sunucu
        self.say = say  # sesli anons
        self.active = False
        self.hw = ClaudeRunner("it_hw", hw_model, HW_PROMPT, max_turns=8)
        self.err = ClaudeRunner("it_err", err_model, ERR_PROMPT, max_turns=8)
        self.last_qr: tuple[str, float] | None = None
        self.busy = {"hw": False, "error": False}
        IT_DIR.mkdir(exist_ok=True)
        if not INVENTORY.exists():
            INVENTORY.write_text(json.dumps({
                "CS-SW-0001": {"ad": "Örnek switch", "konum": "Kat 1 / Rack A", "ip": "10.0.1.10",
                               "not": "QR içeriği bu anahtarla eşleşir"}
            }, ensure_ascii=False, indent=2), encoding="utf-8")

    # ------------------------------------------------------------------
    def set_active(self, on: bool) -> None:
        self.active = on
        if on:
            self.hw.warm()
            self.err.warm()
        self.send({"type": "it_state", "active": on})

    def state(self) -> dict:
        return {"type": "it_state", "active": self.active}

    # ---------------- QR + envanter ----------------
    def inventory(self) -> dict:
        try:
            return json.loads(INVENTORY.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def on_qr(self, text: str) -> None:
        """Telefonda okunan QR: envanterde varsa kartini yolla ve soyle."""
        self.last_qr = (text, time.time())
        item = self.inventory().get(text.strip())
        self.send({"type": "it_qr", "text": text, "item": item})
        if item:
            parts = [item.get("ad"), item.get("konum"), item.get("ip") and f"IP {item['ip']}"]
            self.say(", ".join(p for p in parts if p))

    # ---------------- tarama / hata ----------------
    def scan(self, img: bytes | None, source: str) -> None:
        self._start("hw", img, source, "Bu cihazı analiz et.")

    def error(self, img: bytes | None, source: str, question: str = "") -> None:
        self._start("error", img, source, question or "Bu hata ne, nasıl çözülür?")

    def _start(self, kind: str, img: bytes | None, source: str, question: str) -> None:
        if img is None:
            self.say("Görüntü alınamadı, tekrar dene.")
            return
        if self.busy[kind]:
            self.say("Önceki analiz sürüyor.")
            return
        self.busy[kind] = True
        now = time.time()
        day, hms = _stamp(now)
        rid = f"{hms}-{'tarama' if kind == 'hw' else 'hata'}"
        folder = IT_DIR / day
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{rid}.jpg").write_bytes(img)
        qr = self.last_qr[0] if self.last_qr and now - self.last_qr[1] < 30 else None
        rec = {"id": rid, "day": day, "kind": kind, "time": now, "source": source,
               "image": f"/it/{day}/{rid}.jpg", "qr": qr, "summary": "", "text": ""}
        self.send({"type": "it_event", "event": "start", **rec})
        threading.Thread(target=self._run, args=(kind, img, question, rec, folder), daemon=True).start()

    def _run(self, kind: str, img: bytes, question: str, rec: dict, folder: Path) -> None:
        runner = self.hw if kind == "hw" else self.err
        t0 = time.time()
        spoken = {"done": False}
        last_send = [0.0]

        def on_delta(full: str) -> None:
            # Ozet satiri tamamlaninca HEMEN sesli oku
            if not spoken["done"]:
                m = re.match(r"\s*ÖZET:\s*(.+?)\n", full)
                if m:
                    spoken["done"] = True
                    rec["summary"] = m.group(1).strip()
                    self.say(rec["summary"])
            # Karti akista guncelle (saniyede ~8 kez yeter)
            if time.time() - last_send[0] > 0.12:
                last_send[0] = time.time()
                self.send({"type": "it_event", "event": "delta", "id": rec["id"], "text": full,
                           "summary": rec["summary"]})

        b64 = base64.b64encode(img).decode()
        extra = f"\nOkunan QR/cihaz kimliği: {rec['qr']}" if rec["qr"] else ""
        content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                   {"type": "text", "text": question + extra}]
        full, err = runner.ask(content, on_delta)
        if not spoken["done"]:
            first = full.strip().splitlines()[0] if full.strip() else ""
            rec["summary"] = re.sub(r"^ÖZET:\s*", "", first)[:200]
            if rec["summary"]:
                self.say(rec["summary"])
        rec["text"] = full
        rec["seconds"] = round(time.time() - t0, 1)
        if err:
            rec["error"] = err
            self.say("Analiz tamamlanamadı." if "limit" not in err.lower() else "Claude kullanım limitine ulaşıldı.")
        (folder / f"{rec['id']}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
        self.send({"type": "it_event", "event": "done", **rec})
        self.busy[kind] = False
        self.send(self.history())

    # ---------------- gecmis ----------------
    def history(self, limit: int = 40) -> dict:
        items = []
        for day in sorted(IT_DIR.glob("*"), reverse=True):
            for j in sorted(day.glob("*.json"), reverse=True):
                try:
                    items.append(json.loads(j.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    continue
                if len(items) >= limit:
                    return {"type": "it_list", "items": items}
        return {"type": "it_list", "items": items}


class StabilityWatcher:
    """Goruntu ~1 sn sabit kalip onceki taramadan farkli bir sahneyse True doner."""

    def __init__(self, still_s: float = 1.0, cooldown_s: float = 5.0) -> None:
        self.still_s = still_s
        self.cooldown_s = cooldown_s
        self.prev = None
        self.still_since = None
        self.last_scene = None
        self.last_fire = 0.0

    def update(self, frame) -> bool:
        import cv2
        import numpy as np

        small = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (32, 32)).astype(np.float32)
        now = time.time()
        if self.prev is None:
            self.prev = small
            return False
        motion = float(np.mean(np.abs(small - self.prev)))
        self.prev = small
        if motion > 6:  # kafa hareket ediyor
            self.still_since = None
            return False
        if self.still_since is None:
            self.still_since = now
        if now - self.still_since < self.still_s or now - self.last_fire < self.cooldown_s:
            return False
        if self.last_scene is not None and float(np.mean(np.abs(small - self.last_scene))) < 14:
            return False  # ayni cihaza bakmaya devam ediyor
        self.last_scene = small
        self.last_fire = now
        return True
