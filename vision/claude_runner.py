"""Surekli acik Claude Code oturumu (stream-json giris/cikis).

Her soruda claude.exe'yi yeniden baslatmak 1-2 sn kaybettirir. Bu sinif sureci bir kez
acar, sorulari stdin'den satir satir yollar ve her soru icin "result" olayina kadar
akisi okur. Baglam buyuyup yavaslamasin diye belirli sayida sorudan sonra yenilenir.
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from assistant import find_claude

HERE = Path(__file__).resolve().parent


class ClaudeRunner:
    def __init__(self, name: str, model: str, system_prompt: str, max_turns: int = 6) -> None:
        self.name = name
        self.model = model
        self.prompt_file = HERE / f"prompt_{name}.txt"
        self.prompt_file.write_text(system_prompt, encoding="utf-8")
        self.max_turns = max_turns
        self.exe = find_claude()
        self._proc: subprocess.Popen | None = None
        self._events: queue.Queue = queue.Queue()
        self._turns = 0
        self._lock = threading.Lock()  # ayni anda tek soru

    # ------------------------------------------------------------------
    def warm(self) -> None:
        """Sureci onceden ac (ilk soruda bekleme olmasin)."""
        threading.Thread(target=self._ensure, daemon=True).start()

    def _ensure(self) -> None:
        if self._proc and self._proc.poll() is None and self._turns < self.max_turns:
            return
        self._stop()
        cmd = [self.exe, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
               "--verbose", "--include-partial-messages", "--model", self.model,
               "--system-prompt-file", str(self.prompt_file), "--tools", ""]
        self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, cwd=str(HERE),
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self._events = queue.Queue()
        self._turns = 0
        threading.Thread(target=self._reader, args=(self._proc, self._events), daemon=True).start()

    @staticmethod
    def _reader(proc: subprocess.Popen, q: queue.Queue) -> None:
        for raw in proc.stdout:
            try:
                q.put(json.loads(raw))
            except ValueError:
                continue
        q.put({"type": "_eof"})

    def _stop(self) -> None:
        p = self._proc
        self._proc = None
        if p and p.poll() is None:
            try:
                p.stdin.close()
            except OSError:
                pass
            try:
                p.kill()
            except OSError:
                pass

    def cancel(self) -> None:
        self._stop()

    # ------------------------------------------------------------------
    def ask(self, content: list[dict], on_delta: Callable[[str], None], timeout: float = 90) -> tuple[str, str | None]:
        """content: Messages API icerik bloklari (image/text). Doner: (tam_metin, hata)."""
        if not self.exe:
            return "", "Claude Code bulunamadı."
        with self._lock:
            for attempt in (1, 2):
                self._ensure()
                p, q = self._proc, self._events
                msg = {"type": "user", "message": {"role": "user", "content": content}}
                try:
                    p.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
                    p.stdin.flush()
                except OSError:
                    self._stop()
                    continue
                self._turns += 1
                full, err, t_end = "", None, time.time() + timeout
                while True:
                    try:
                        ev = q.get(timeout=max(0.1, t_end - time.time()))
                    except queue.Empty:
                        self._stop()
                        return full, "Claude zaman aşımına uğradı."
                    et = ev.get("type")
                    if et == "_eof":
                        if full:
                            return full, None
                        break  # surec oldu: yeniden dene
                    if et == "stream_event":
                        e = ev.get("event", {})
                        if e.get("type") == "content_block_delta" and e.get("delta", {}).get("type") == "text_delta":
                            full += e["delta"]["text"]
                            on_delta(full)
                    elif et == "result":
                        if ev.get("is_error"):
                            err = str(ev.get("result") or ev.get("subtype"))
                        return full or str(ev.get("result") or ""), err
                self._stop()
            return "", "Claude oturumu başlatılamadı."
