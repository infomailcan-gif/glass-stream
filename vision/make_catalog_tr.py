"""Genis katalog (YOLOE prompt-free, 4.585 sinif) adlarini Turkceye cevirir.

Bir kez calistirilir; sonuc vision/catalog_tr.json. Claude Code CLI (Max aboneligi)
ile 400'luk parcalar halinde cevirir; yarida kalirsa kaldigi yerden devam eder.
Calistirma: python vision/make_catalog_tr.py
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from assistant import find_claude

HERE = Path(__file__).resolve().parent
NAMES = HERE / "catalog_names.json"
OUT = HERE / "catalog_tr.json"
CHUNK = 400

PROMPT = """Aşağıdaki İngilizce nesne/sahne adlarını Türkçeye çevir. Bunlar bir görüntü tanıma modelinin etiketleri;
akıllı gözlükte kutu etiketi olarak gösterilecek. Kısa, yaygın Türkçe karşılığı kullan (1-3 kelime), küçük harfle başla
(özel adlar hariç). Yalnızca JSON nesnesi döndür: {"english": "türkçe", ...}. Açıklama yazma.

"""


def main() -> None:
    names: list[str] = json.loads(NAMES.read_text(encoding="utf-8"))
    done: dict[str, str] = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    todo = [n for n in names if n not in done]
    exe = find_claude()
    print(f"toplam {len(names)}, kalan {len(todo)}")
    for i in range(0, len(todo), CHUNK):
        part = todo[i:i + CHUNK]
        prompt = PROMPT + "\n".join(part)
        r = subprocess.run([exe, "-p", "--model", "sonnet", "--tools", ""], input=prompt.encode("utf-8"),
                           capture_output=True, timeout=600)
        text = r.stdout.decode("utf-8", "replace")
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            print("cevap okunamadi:", text[:200])
            continue
        try:
            got = json.loads(m.group(0))
        except ValueError as e:
            print("JSON hatasi:", e)
            continue
        done.update({k: v for k, v in got.items() if k in part and isinstance(v, str) and v.strip()})
        OUT.write_text(json.dumps(done, ensure_ascii=False, indent=0), encoding="utf-8")
        print(f"{min(i + CHUNK, len(todo))}/{len(todo)} parca tamam, sozlukte {len(done)}")
    print("bitti:", len(done))


if __name__ == "__main__":
    main()
