"""Sesli Claude asistani - Claude Code CLI uzerinden (Max aboneligi, API kredisi gerekmez).

Akis:
  "Claude, ekrandaki hatayi nasil duzeltirim?"  (telefon -> PC: {"type":"ask", ...})
    -> gorsel secilir: soruda "ekran" geciyorsa PC ekran goruntusu,
       degilse gozluk fotografi (yoksa son canli kare)
    -> claude.exe -p (stream-json) ile soru + gorsel gonderilir
    -> cevap akarken cumle cumle {"type":"assistant","say":...} -> gozlukten okunur
    -> tam metin web sayfasinda sohbet olarak gorunur
Konusma hatirlanir (--resume). "Claude, yeni konu" -> yeni sohbet.

NOT: Claude Code'u kendi bilgisayarinda kisisel kullanim icindir. Bu servisi
baskalarina acik bir urune donusturursen API anahtarina gecmen gerekir.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
PROMPT_FILE = HERE / "assistant_prompt.txt"

SYSTEM_PROMPT = """Sen akıllı gözlük takan kullanıcının sesli asistanısın. Kullanıcı Türkçe konuşuyor; her zaman Türkçe cevap ver.
Her mesajda kullanıcının o an gördüğü görüntü (gözlük kamerası veya bilgisayar ekranı) eklenebilir; soruyu bu görüntüye göre cevapla.

Cevapların gözlük hoparlöründen SESLİ okunacak:
- Kısa ve net konuş. İlk cümlede doğrudan cevabı ver.
- Markdown, madde işareti, tablo, emoji kullanma. Düz cümleler kur.
- Adım adım anlatman istenirse: "Birinci adım: ... İkinci adım: ..." şeklinde, her adımı tek cümleyle söyle. Uzun işlerde ilk 3-4 adımı söyle ve kullanıcının "devam" demesini bekle.
- Komut, dosya yolu veya kod gerekiyorsa ayrıca tam halini ``` içinde ver; o kısım okunmaz, ekranda gösterilir. Sesli kısımda sadece ne yapılacağını söyle.
- Görüntüde okuyamadığın bir şey varsa bunu söyle ve yaklaşmasını / tekrar bakmasını iste.
- Tıbbi belge (tahlil, EKG vb.) yorumlarken genel bilgi ver, teşhis koyma; önemli veya acil bulgularda hekime başvurmasını söyle.
"""

SENT_END = re.compile(r"(.+?[\.\!\?…:])(\s+|$)", re.S)


def find_claude() -> str | None:
    appdata = os.environ.get("APPDATA", "")
    exe = Path(appdata) / "npm" / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    if exe.exists():
        return str(exe)
    return shutil.which("claude")


def to_speech(text: str) -> str:
    """Okunacak metin: kod bloklarini, markdown isaretlerini cikar."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"[*_#>|]+", "", text)
    text = re.sub(r"^\s*[-•]\s*", "", text, flags=re.M)
    return re.sub(r"\s+", " ", text).strip()


def screen_jpeg(max_side: int = 1600) -> bytes | None:
    """PC ekran goruntusu (tum ekranlar)."""
    try:
        from PIL import ImageGrab

        img = ImageGrab.grab(all_screens=True)
        img.thumbnail((max_side, max_side))
        import io

        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=85)
        return buf.getvalue()
    except Exception as e:
        print(f"[asistan] ekran goruntusu alinamadi: {e!r}")
        return None


def frame_jpeg(frame: np.ndarray, max_side: int = 1600) -> bytes:
    h, w = frame.shape[:2]
    k = min(1.0, max_side / max(h, w))
    if k < 1:
        frame = cv2.resize(frame, (int(w * k), int(h * k)))
    return cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 88])[1].tobytes()


class Assistant:
    def __init__(self, send, model: str = "sonnet") -> None:
        self.send = send  # JSON gonderme fonksiyonu (sunucuya)
        self.model = model
        self.exe = find_claude()
        self.session_id: str | None = None
        self.busy = False
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._cancelled = False
        PROMPT_FILE.write_text(SYSTEM_PROMPT, encoding="utf-8")
        print(f"[asistan] claude: {self.exe or 'BULUNAMADI'} (model: {model})")
        # Surekli acik oturum: her soruda claude.exe'yi yeniden baslatmak birkac saniye kaybettiriyordu.
        # Sohbet baglami surecte kalir; 12 sorudan sonra (ya da "yeni konu") temiz oturum acilir.
        from claude_runner import ClaudeRunner

        self.runner = ClaudeRunner("asistan", model, SYSTEM_PROMPT, max_turns=12)
        if self.exe:
            self.runner.warm()

    # ------------------------------------------------------------------
    def ask(self, question: str, photo_b64: str | None, latest_frame: np.ndarray | None) -> None:
        q = question.strip()
        low = q.lower()
        if any(w in low for w in ("yeni konu", "yeni sohbet", "sohbeti sıfırla", "sohbeti sifirla")):
            self.session_id = None
            self.runner.cancel()
            self.runner.warm()
            self._say("Tamam, yeni bir konuya başladık.")
            self.send({"type": "assistant", "event": "reset"})
            return
        if set(re.findall(r"\w+", low)) & {"sus", "dur", "kes", "yeter"} and len(low) < 15:
            self.cancel()
            return
        if not self.exe:
            self._say("Claude bulunamadı. Bilgisayara Claude Code kurulmalı.")
            return
        with self._lock:
            if self.busy:
                self._say("Önceki soruyu yanıtlıyorum, biraz bekle.")
                return
            self.busy = True
            self._cancelled = False

        # Gorsel secimi
        source, img = "yok", None
        if "ekran" in low or "bilgisayar" in low or "monitör" in low:
            img, source = screen_jpeg(), "PC ekranı"
        if img is None and photo_b64:
            img, source = base64.b64decode(photo_b64), "gözlük fotoğrafı"
        if img is None and latest_frame is not None:
            img, source = frame_jpeg(latest_frame), "canlı görüntü"

        self.send({"type": "assistant", "event": "question", "text": q, "image": source,
                   "thumb": base64.b64encode(_thumb(img)).decode() if img else None})
        threading.Thread(target=self._run, args=(q, img), daemon=True).start()

    def cancel(self) -> None:
        """Konusmayi kes: Claude cevabi yarida birakilir, kalan cumleler okunmaz."""
        self._cancelled = True
        self.runner.cancel()
        self.runner.warm()  # sonraki soru beklemesin
        self.send({"type": "assistant", "event": "cancel"})

    # ------------------------------------------------------------------
    def _run(self, question: str, img: bytes | None) -> None:
        t0 = time.time()
        st = {"spoken": 0, "in_code": False, "first": None}
        full = ""

        def on_delta(text: str) -> None:
            if self._cancelled:
                return
            if st["first"] is None:
                st["first"] = time.time() - t0
            # Tamamlanan cumleleri hemen oku (kod blogu icindekiler haric)
            pending = text[st["spoken"]:]
            while True:
                m = SENT_END.match(pending)
                if not m:
                    break
                chunk = m.group(1)
                st["in_code"] ^= chunk.count("```") % 2 == 1
                st["spoken"] += m.end()
                pending = text[st["spoken"]:]
                say = to_speech(chunk)
                if say and not st["in_code"]:
                    self._say(say)
            self.send({"type": "assistant", "event": "delta", "text": text})

        try:
            content = []
            if img:
                content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                            "data": base64.b64encode(img).decode()}})
            content.append({"type": "text", "text": question})
            full, err = self.runner.ask(content, on_delta, timeout=120)
            if not self._cancelled:
                if err:
                    self._error(err)
                rest = to_speech(full[st["spoken"]:])
                if rest and not st["in_code"]:
                    self._say(rest)
        except Exception as e:
            self._error(repr(e))
        finally:
            first = st["first"]
            self.send({"type": "assistant", "event": "cancel" if self._cancelled else "done", "text": full,
                       "first_s": round(first, 1) if first else None, "total_s": round(time.time() - t0, 1)})
            with self._lock:
                self.busy = False

    def _say(self, text: str) -> None:
        self.send({"type": "assistant", "say": text})

    def _error(self, err: str) -> None:
        print(f"[asistan] hata: {err[:300]}")
        if "login" in err.lower() or "not logged" in err.lower():
            self._say("Claude'a giriş yapılmamış. Bilgisayarda claude komutuyla giriş yap.")
        elif "limit" in err.lower():
            self._say("Claude kullanım limitine ulaşıldı, biraz sonra tekrar dene.")
        else:
            self._say("Claude'dan cevap alınamadı.")
        self.send({"type": "assistant", "event": "error", "text": err[:500]})


def _thumb(img: bytes, side: int = 240) -> bytes:
    arr = cv2.imdecode(np.frombuffer(img, np.uint8), cv2.IMREAD_COLOR)
    if arr is None:
        return b""
    h, w = arr.shape[:2]
    k = side / max(h, w)
    arr = cv2.resize(arr, (int(w * k), int(h * k)))
    return cv2.imencode(".jpg", arr, [cv2.IMWRITE_JPEG_QUALITY, 70])[1].tobytes()
