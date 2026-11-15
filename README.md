# Glass Stream

Meta Ray-Ban gözlüğün kamerasını bilgisayarın web tarayıcısında canlı gösterir.

```
Gözlük ──Bluetooth──► iPhone (Glass Bridge) ──Wi-Fi / WebSocket──► PC (Node sunucu) ──► Tarayıcı
```

| Klasör | Ne işe yarar |
|---|---|
| `server.js`, `public/` | PC sunucusu ve izleme sayfası (`http://localhost:8000`) |
| `ios/` | iPhone köprü uygulaması (Meta DAT SDK 1.0.1, Swift) |
| `.github/workflows/build-ios.yml` | Mac olmadan iPhone uygulamasını GitHub'da derler |

## 1. PC sunucusu

```bash
npm install
npm start
```

Ekrana `iPhone için : ws://192.168.x.x:8000/ingest` satırı yazılır. Bu adresi iPhone uygulamasına gireceksin.
Windows Güvenlik Duvarı sorarsa **Özel ağlar** için izin ver.

Test: `http://localhost:8000/sender.html` → "Test deseni gönder" → `http://localhost:8000` sayfasında görüntü akmalı.

## 2. iPhone uygulamasını derle (GitHub, ücretsiz)

1. GitHub'da **public** yeni bir depo aç (public depolarda Mac derleme dakikaları ücretsiz).
2. Bu klasörün tamamını o depoya yükle (`node_modules` hariç).
3. Depoda **Actions** sekmesi → "Build iOS bridge" çalışmasını bekle (~5–10 dk).
4. Bitince çalışmanın sayfasında **Artifacts → GlassBridge-ipa** dosyasını indir, zip'ten `GlassBridge.ipa`'yı çıkar.

## 3. iPhone'a yükle (Sideloadly)

1. PC'ye **iTunes** (Apple sitesindeki sürüm, Microsoft Store değil) ve **Sideloadly** kur.
2. iPhone'u USB ile bağla, "Bu bilgisayara güven" de.
3. Sideloadly'de `GlassBridge.ipa`'yı seç, Apple ID'ni gir, **Start**.
4. iPhone'da: Ayarlar → Genel → VPN ve Cihaz Yönetimi → Apple ID'ne **Güven**.
5. iPhone'da: Ayarlar → Gizlilik ve Güvenlik → **Geliştirici Modu**'nu aç (iPhone yeniden başlar).

Ücretsiz Apple ID ile yüklenen uygulama 7 günde bir Sideloadly ile yeniden yüklenmeli.

## 4. Kullanım

1. Meta AI uygulamasında gözlük bağlı ve **Developer Mode** açık olsun.
2. Glass Bridge'i aç → **Meta AI ile eşleştir** → Meta AI açılır, onayla, geri dön.
3. PC adresini gir (`ws://192.168.x.x:8000/ingest`).
4. **Yayını başlat** → ilk seferde kamera izni için Meta AI tekrar açılır, onayla.
5. PC'de `http://localhost:8000` → gözlüğün görüntüsü.

Yayın sırasında Glass Bridge **açık (ön planda)** kalmalı; uygulama ekranı kapatmaz.
