"""Kisi sayim oturumlari.

"Kisi saymayi baslat" -> sayaclar sifirlanir, oturum acilir (zaman + telefonun konumu).
"Kisi saymayi bitir"  -> o alandaki sonuc kaydedilir: sure, toplam kisi, KKD eksikleri,
sayilan herkesin yuz fotografi. Kayitlar: vision/sessions/GUN/<id>.json
"""

from __future__ import annotations

import json
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SESS_DIR = HERE / "sessions"


# "var" hali (Turkce ek uyumuyla)
WITH = {"face-mask": "maskeli", "helmet": "baretli", "safety-vest": "yelekli", "gloves": "eldivenli",
        "glasses": "gözlüklü", "hairnet": "boneli", "ear-mufs": "kulaklıklı"}


class CountSessions:
    def __init__(self) -> None:
        self.current: dict | None = None
        SESS_DIR.mkdir(exist_ok=True)

    @property
    def active(self) -> bool:
        return self.current is not None

    def start(self, location: str | None, lat: float | None, lon: float | None, turned_ai_on: bool) -> dict:
        now = time.time()
        self.current = {
            "id": time.strftime("%H%M%S", time.localtime(now)),
            "day": time.strftime("%Y%m%d", time.localtime(now)),
            "start": now, "location": location, "lat": lat, "lon": lon,
            "turned_ai_on": turned_ai_on,
        }
        return self.current

    def stop(self, det, ppe_items: dict) -> dict | None:
        """Oturumu bitir ve kaydet. det: Detector (sayaclar + kimlikler), ppe_items: PPE_ITEMS."""
        if not self.current:
            return None
        s = self.current
        self.current = None
        now = time.time()
        faces = [{"id": p["n"], "file": p["img"], "face": p["face"]} for p in det.people if p.get("img")]
        ppe = [{"key": k, "name": ppe_items[k][0], "with": WITH.get(k, ppe_items[k][0].lower() + "li"),
                "without": ppe_items[k][1], "ok": det.ok_totals.get(k, 0), "missing": det.missing_totals.get(k, 0)}
               for k in det.required if k in ppe_items]
        s.update({
            "end": now,
            "minutes": round((now - s["start"]) / 60, 1),
            "total": det.total_people,
            "identities": len(det.faces.fid.identities),
            "ppe": ppe,
            "faces": faces,
        })
        folder = SESS_DIR / s["day"]
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{s['id']}.json").write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
        return s

    def summary_text(self, s: dict) -> str:
        mins = s["minutes"]
        dur = f"{int(mins)} dakikada" if mins >= 1 else f"{int(mins * 60)} saniyede"
        text = f"Sayım bitti. {dur} {s['total']} kişi sayıldı."
        for p in s["ppe"]:
            checked = p["ok"] + p["missing"]
            if checked:
                text += f" {p['ok']} kişi {p['with']}, {p['missing']} kişi {p['without']}."
                if checked < s["total"]:
                    text += f" {s['total'] - checked} kişinin {p['name'].lower()} durumu görülemedi."
        return text

    def state(self) -> dict:
        c = self.current
        return {"type": "count_state", "active": bool(c), "start": c["start"] if c else None,
                "location": c.get("location") if c else None}

    def history(self, limit: int = 40) -> dict:
        items = []
        for day in sorted(SESS_DIR.glob("*"), reverse=True):
            for j in sorted(day.glob("*.json"), reverse=True):
                try:
                    items.append(json.loads(j.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    continue
                if len(items) >= limit:
                    return {"type": "sessions", "items": items}
        return {"type": "sessions", "items": items}

