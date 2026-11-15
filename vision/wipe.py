"""Kayitlari silme ("gecmisi temizle" / "tum verileri sil").

Telefon veya web {"type":"wipe","what":...} yollar. what:
  people   - sayilan kisilerin fotograflari (faces/)
  vehicles - arac ve plaka gunlugu + aranan plaka yakalamalari (vehicles/, captures/)
  it       - bilgi islem ve EKG kayitlari (it/)
  qr       - okunan QR gecmisi (qr/)
  sessions - sayim oturumlari (sessions/)
  videos   - video kayitlari (recordings/)
  all      - hepsi
Ayarlar (modeller, KKD listesi, aranan plakalar, envanter) silinmez.
Silinen geri gelmez; telefon her seferinde onay sorar.
"""

from __future__ import annotations

import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent

GROUPS: dict[str, list[str]] = {
    "people": ["faces"],
    "vehicles": ["vehicles", "captures"],
    "it": ["it"],
    "qr": ["qr"],
    "sessions": ["sessions"],
    "videos": ["recordings"],
}
NAMES = {"people": "kişi fotoğrafları", "vehicles": "araç ve plaka kayıtları", "it": "BT ve EKG kayıtları",
         "qr": "QR kayıtları", "sessions": "sayım oturumları", "videos": "videolar", "all": "tüm kayıtlar"}


def wipe(what: str, skip: set[Path] | None = None) -> int:
    """Secilen gruptaki dosyalari siler, klasorleri birakir. Doner: silinen dosya sayisi."""
    keys = list(GROUPS) if what == "all" else [what] if what in GROUPS else []
    skip = {p.resolve() for p in (skip or set())}
    n = 0
    for k in keys:
        for folder in GROUPS[k]:
            root = HERE / folder
            if not root.exists():
                continue
            for item in root.iterdir():
                if item.resolve() in skip:
                    continue
                if item.is_dir():
                    n += sum(1 for f in item.rglob("*") if f.is_file())
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    try:
                        item.unlink()
                        n += 1
                    except OSError:
                        pass  # kullanimdaki dosya (or. suren video)
    return n
