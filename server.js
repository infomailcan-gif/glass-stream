// Glass Stream relay sunucusu
//
//  iPhone (Meta DAT SDK) --JPEG kareler--> ws://PC:8000/ingest
//  Tarayici             <--JPEG kareler--  ws://PC:8000/view
//
// Ileride goruntu isleme / nesne siniflandirma servisi de /view'a
// abone olup kareleri alabilir.

const http = require("http");
const fs = require("fs");
const path = require("path");
const os = require("os");
const { WebSocketServer } = require("ws");

const PORT = Number(process.env.PORT) || 8000;
const PUBLIC_DIR = path.join(__dirname, "public");
const CAPTURES_DIR = path.join(__dirname, "vision", "captures");
const FACES_DIR = path.join(__dirname, "vision", "faces");
const REC_DIR = path.join(__dirname, "vision", "recordings");
// URL oneki -> klasor
const DIRS = { "/captures/": CAPTURES_DIR, "/faces/": FACES_DIR, "/recordings/": REC_DIR };

const MIME = {
  ".jpg": "image/jpeg",
  ".mp4": "video/mp4",
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
};

const server = http.createServer((req, res) => {
  const urlPath = decodeURIComponent(req.url.split("?")[0]);
  const prefix = Object.keys(DIRS).find((p) => urlPath.startsWith(p));
  const baseDir = prefix ? DIRS[prefix] : PUBLIC_DIR;
  const file = urlPath === "/" ? "index.html" : urlPath.slice(prefix ? prefix.length : 1);
  const full = path.normalize(path.join(baseDir, file));
  if (!full.startsWith(baseDir)) {
    res.writeHead(403).end();
    return;
  }
  fs.stat(full, (err, st) => {
    if (err || !st.isFile()) {
      res.writeHead(404).end("Bulunamadi");
      return;
    }
    const type = MIME[path.extname(full)] || "application/octet-stream";
    // Video ileri/geri sarma icin Range destegi
    const m = /bytes=(\d*)-(\d*)/.exec(req.headers.range || "");
    if (m) {
      const start = m[1] ? Number(m[1]) : 0;
      const end = m[2] ? Number(m[2]) : st.size - 1;
      res.writeHead(206, {
        "Content-Type": type, "Accept-Ranges": "bytes",
        "Content-Range": `bytes ${start}-${end}/${st.size}`, "Content-Length": end - start + 1,
      });
      fs.createReadStream(full, { start, end }).pipe(res);
    } else {
      res.writeHead(200, { "Content-Type": type, "Content-Length": st.size, "Accept-Ranges": "bytes" });
      fs.createReadStream(full).pipe(res);
    }
  });
});

const ingestWss = new WebSocketServer({ noServer: true, maxPayload: 8 * 1024 * 1024 });
const viewWss = new WebSocketServer({ noServer: true });
// Goruntu isleme servisi (Python): kareleri alir, JSON sonuc yollar
const visionWss = new WebSocketServer({ noServer: true, maxPayload: 4 * 1024 * 1024 });

const stats = { sources: 0, frames: 0, lastFrameAt: 0, lastSize: 0 };

server.on("upgrade", (req, socket, head) => {
  const p = req.url.split("?")[0];
  if (p === "/ingest") {
    ingestWss.handleUpgrade(req, socket, head, (ws) => ingestWss.emit("connection", ws, req));
  } else if (p === "/view") {
    viewWss.handleUpgrade(req, socket, head, (ws) => viewWss.emit("connection", ws, req));
  } else if (p === "/vision") {
    visionWss.handleUpgrade(req, socket, head, (ws) => visionWss.emit("connection", ws, req));
  } else {
    socket.destroy();
  }
});

// Kaynak (iPhone) baglantisi: her binary mesaj = bir JPEG kare
ingestWss.on("connection", (ws, req) => {
  stats.sources++;
  console.log(`[ingest] kaynak baglandi: ${req.socket.remoteAddress}`);
  broadcastStatus();
  // Analiz servisinden guncel durumu (yapay zeka acik mi, kayit) iste -> telefona da gider
  for (const v of visionWss.clients) if (v.readyState === 1) v.send(JSON.stringify({ type: "hello" }));

  ws.on("message", (data, isBinary) => {
    if (!isBinary) {
      // Telefondan komut (sesli komut: yapay zeka ac/kapat, kayit, Claude sorusu...) -> analiz servisine
      const text = data.toString();
      // Kare sira numarasi: yalnizca analiz servisine (her kareden once gelir, kayda yazma)
      if (text.startsWith('{"type":"seq"')) {
        for (const v of visionWss.clients) if (v.readyState === 1) v.send(text);
        return;
      }
      console.log(`[telefon] ${text.slice(0, 200)}`);
      for (const v of visionWss.clients) if (v.readyState === 1) v.send(text);
      for (const v of viewWss.clients) if (v.readyState === 1) v.send(text); // web'de de goster
      return;
    }
    stats.frames++;
    stats.lastFrameAt = Date.now();
    stats.lastSize = data.length;
    for (const viewer of [...viewWss.clients, ...visionWss.clients]) {
      // Yavas izleyiciyi bekletme: arabellek doluysa bu kareyi atla (her zaman en guncel kare)
      if (viewer.readyState === 1 && viewer.bufferedAmount < 1024 * 1024) {
        viewer.send(data, { binary: true });
      }
    }
  });

  ws.on("close", () => {
    stats.sources--;
    console.log("[ingest] kaynak ayrildi");
    broadcastStatus();
  });
});

viewWss.on("connection", (ws) => {
  console.log(`[view] izleyici baglandi (toplam ${viewWss.clients.size})`);
  ws.send(JSON.stringify({ type: "status", sources: stats.sources }));
  // Web sayfasindan gelen komutlar (or. sayaclari sifirla) -> goruntu isleme servisine
  ws.on("message", (data, isBinary) => {
    if (isBinary) return;
    for (const v of visionWss.clients) if (v.readyState === 1) v.send(data.toString());
  });
});

// Python servisinden gelen JSON:
//   {type:"detections", ...} -> web izleyicilere (kutular, sayaclar)
//   {type:"announce", text}  -> web izleyicilere + telefona (gozlukten sesli okunur)
visionWss.on("connection", (ws) => {
  console.log("[vision] goruntu isleme servisi baglandi");
  ws.on("message", (data, isBinary) => {
    if (isBinary) return;
    const text = data.toString();
    let msg;
    try {
      msg = JSON.parse(text);
    } catch {
      return;
    }
    for (const v of viewWss.clients) if (v.readyState === 1) v.send(text);
    // Telefona gidenler: anonslar + durum bilgisi (yapay zeka acik mi, kayit suruyor mu)
    // detections: telefondaki "Canli" sekmesi kutulari cizer; models: modeller sekmesi
    if (["announce", "ai_state", "recording", "assistant", "models", "detections",
         "ppe", "objects_cfg", "watchlist", "faces_list"].includes(msg.type)) {
      if (msg.type === "announce") console.log(`[anons] ${msg.text}`);
      for (const phone of ingestWss.clients) if (phone.readyState === 1) phone.send(text);
    }
  });
  ws.on("close", () => console.log("[vision] servis ayrildi"));
});

function broadcastStatus() {
  const msg = JSON.stringify({ type: "status", sources: stats.sources });
  for (const v of viewWss.clients) if (v.readyState === 1) v.send(msg);
}

function lanAddresses() {
  return Object.values(os.networkInterfaces())
    .flat()
    .filter((i) => i && i.family === "IPv4" && !i.internal)
    .map((i) => i.address);
}

server.listen(PORT, "0.0.0.0", () => {
  console.log(`\nGlass Stream calisiyor`);
  console.log(`  Izleme sayfasi : http://localhost:${PORT}`);
  console.log(`  Test gonderici : http://localhost:${PORT}/sender.html`);
  for (const ip of lanAddresses()) {
    console.log(`  iPhone icin    : ws://${ip}:${PORT}/ingest`);
  }
  console.log("");
});
