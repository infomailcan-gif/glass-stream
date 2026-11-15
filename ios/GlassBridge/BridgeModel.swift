import Foundation
import MWDATCamera
import MWDATCore
import Network
import Observation
import UIKit

enum StreamQuality: String, CaseIterable, Identifiable {
  case low, medium, high
  var id: String { rawValue }

  var title: String {
    switch self {
    case .low: return "Düşük (360×640)"
    case .medium: return "Orta (504×896)"
    case .high: return "Yüksek (720×1280)"
    }
  }

  var sdkValue: StreamingResolution {
    switch self {
    case .low: return .low
    case .medium: return .medium
    case .high: return .high
    }
  }
}

/// JPEG sikistirma: mobil veride daha kucuk kareler = daha yuksek kare hizi
enum JpegQuality: String, CaseIterable, Identifiable {
  case high, balanced, saver
  var id: String { rawValue }
  var title: String {
    switch self {
    case .high: return "Yüksek"
    case .balanced: return "Dengeli"
    case .saver: return "Tasarruf"
    }
  }
  var value: CGFloat {
    switch self {
    case .high: return 0.7
    case .balanced: return 0.5
    case .saver: return 0.32
    }
  }
}

/// Hazir yayin profilleri. Otomatik: Wi-Fi -> Ev, mobil veri -> Mobil.
enum StreamProfile: String, CaseIterable, Identifiable {
  case auto, home, mobile, detail
  var id: String { rawValue }
  var title: String {
    switch self {
    case .auto: return "Otomatik"
    case .home: return "Ev"
    case .mobile: return "Mobil"
    case .detail: return "Detay"
    }
  }
  var detail: String {
    switch self {
    case .auto: return "Wi-Fi'da Ev, mobil veride Mobil"
    case .home: return "Orta · 24 kare/sn · Dengeli"
    case .mobile: return "Düşük · 15 kare/sn · Tasarruf"
    case .detail: return "Yüksek · 15 kare/sn · Dengeli (plaka, QR, uzak yüz)"
    }
  }
  /// (cozunurluk, kare hizi, sikistirma)
  func settings(onWiFi: Bool) -> (StreamQuality, UInt, JpegQuality) {
    switch self {
    case .auto: return onWiFi ? StreamProfile.home.settings(onWiFi: true) : StreamProfile.mobile.settings(onWiFi: false)
    case .home: return (.medium, 24, .balanced)
    case .mobile: return (.low, 15, .saver)
    case .detail: return (.high, 15, .balanced)
    }
  }
}

/// Kare isleme thread'inden okunan ayar (yaris zararsiz: tek CGFloat)
final class QualityBox: @unchecked Sendable {
  var value: CGFloat = 0.5
}

/// Son gonderilen karelerin JPEG'leri (sira no -> veri). PC kutulari hangi kare icin
/// yolladiysa Canli ekranda o kare gosterilir; boylece kutular goruntuden kaymaz.
final class FrameBuffer: @unchecked Sendable {
  private let lock = NSLock()
  private var frames: [Int: Data] = [:]
  private var order: [Int] = []
  var enabled = false  // yalnizca Canli sekmesi acikken doldur

  func put(_ n: Int, _ data: Data) {
    lock.lock(); defer { lock.unlock() }
    guard enabled else { return }
    frames[n] = data
    order.append(n)
    if order.count > 90 { frames[order.removeFirst()] = nil }
  }

  func get(_ n: Int) -> Data? {
    lock.lock(); defer { lock.unlock() }
    return frames[n]
  }

  func clear() {
    lock.lock(); defer { lock.unlock() }
    frames.removeAll()
    order.removeAll()
  }
}

/// Gozluk -> telefon -> PC kopru mantigi.
/// 1) DeviceSession baslat  2) kamera iznini al  3) kamerayi ekle ve akisi baslat
/// 4) her kareyi JPEG yapip FrameUploader ile PC'ye gonder.
@Observable
@MainActor
final class BridgeModel {
  // UI durumu
  var registrationState: RegistrationState
  var hasDevice = false
  var sessionState: DeviceSessionState = .idle
  var streamState: StreamState = .stopped
  var socketConnected = false
  var sentFPS = 0
  var preview: UIImage?
  var errorMessage: String?
  var isRunning = false
  var needsGlassesAppUpdate = false

  var serverURL: String {
    didSet { UserDefaults.standard.set(serverURL, forKey: "serverURL") }
  }
  var quality: StreamQuality = .medium
  var jpegQuality: JpegQuality = JpegQuality(rawValue: UserDefaults.standard.string(forKey: "jpegQuality") ?? "") ?? .balanced {
    didSet {
      qualityBox.value = jpegQuality.value
      UserDefaults.standard.set(jpegQuality.rawValue, forKey: "jpegQuality")
    }
  }
  @ObservationIgnored let qualityBox = QualityBox()
  var frameRate: UInt = 24
  let frameRates: [UInt] = [15, 24, 30]

  /// Secili profil (ozel ayar yapilirsa nil)
  var profile: StreamProfile? = StreamProfile(rawValue: UserDefaults.standard.string(forKey: "profile") ?? "auto") {
    didSet { UserDefaults.standard.set(profile?.rawValue ?? "custom", forKey: "profile"); applyProfile() }
  }
  var onWiFi = true
  @ObservationIgnored private let pathMonitor = NWPathMonitor()

  /// Profili cozunurluk / kare hizi / sikistirmaya uygula. Cozunurluk ve kare hizi
  /// yayin yeniden baslayinca gecerli olur; sikistirma aninda.
  func applyProfile() {
    guard let p = profile else { return }
    let (q, fps, jq) = p.settings(onWiFi: onWiFi)
    quality = q
    frameRate = fps
    jpegQuality = jq
  }

  /// Canli ekranda kutulari PC'nin isledigi kareye kilitle (tam oturur ama gecikmeli)
  var lockBoxesToFrame = UserDefaults.standard.bool(forKey: "lockBoxes") {
    didSet { UserDefaults.standard.set(lockBoxesToFrame, forKey: "lockBoxes"); frameBuffer.enabled = liveActive && lockBoxesToFrame }
  }

  // SDK nesneleri
  @ObservationIgnored private let wearables: WearablesInterface
  @ObservationIgnored private let deviceSelector: AutoDeviceSelector
  @ObservationIgnored private var deviceSession: DeviceSession?
  @ObservationIgnored private var camera: MWDATCamera.Camera?
  @ObservationIgnored private let sessionTokens = ListenerTokenBag()
  @ObservationIgnored private let streamTokens = ListenerTokenBag()
  @ObservationIgnored private let uploader = FrameUploader()
  @ObservationIgnored private let previewThrottle = Throttle(perSecond: 4)

  init(wearables: WearablesInterface) {
    self.wearables = wearables
    self.deviceSelector = AutoDeviceSelector(wearables: wearables)
    self.registrationState = wearables.registrationState
    self.serverURL = UserDefaults.standard.string(forKey: "serverURL") ?? "ws://192.168.1.100:8000/ingest"

    uploader.onConnectionChange = { [weak self] connected in
      Task { @MainActor in
        self?.socketConnected = connected
        if connected { self?.startLocationReports() }
      }
    }
    uploader.onText = { [weak self] text in
      Task { @MainActor in self?.handleServerMessage(text) }
    }

    // Sesli komutlar: TTS konusurken dinleme duraklar (kendi sesini komut sanmasin)
    speaker.onSpeakingChange = { [weak self] speaking in
      self?.isSpeaking = speaking
      self?.voice.setPaused(speaking)  // konusurken yalnizca "dur / sus / yeter" dinlenir
    }
    voice.onHeard = { [weak self] text in self?.lastHeard = text; self?.voiceHint = nil }
    voice.onUnmatched = { [weak self] _ in
      self?.voiceHint = "Komut anlaşılmadı. Claude'a sormak için “Claude” ya da “Asistan” diye başla."
    }
    voice.onStateChange = { [weak self] on in self?.voiceListening = on }
    voice.onCommand = { [weak self] cmd, raw in self?.handleVoice(cmd, raw: raw) }

    Task { [weak self] in
      guard let self else { return }
      for await state in wearables.registrationStateStream() {
        self.registrationState = state
      }
    }
    Task { [weak self] in
      guard let self else { return }
      for await deviceId in self.deviceSelector.activeDeviceStream() {
        self.hasDevice = deviceId != nil
      }
    }
    // Telefonda okunan QR: aninda soyle, PC'ye kaydet (envanter karti PC'den gelir)
    qrReader.onCode = { [weak self] text in
      Task { @MainActor in
        guard let self else { return }
        self.itQR = (text, nil)
        self.announce("QR: \(text)")
        self.uploader.sendJSON(["type": "qr", "text": text])
      }
    }

    // Wi-Fi / mobil veri algilama (Otomatik profil icin)
    pathMonitor.pathUpdateHandler = { [weak self] path in
      let wifi = path.usesInterfaceType(.wifi) || path.usesInterfaceType(.wiredEthernet)
      Task { @MainActor in
        guard let self, self.onWiFi != wifi else { return }
        self.onWiFi = wifi
        if self.profile == .auto { self.applyProfile() }
      }
    }
    pathMonitor.start(queue: DispatchQueue(label: "net"))
    applyProfile()

    // Gonderilen FPS sayaci
    Task { [weak self] in
      while !Task.isCancelled {
        try? await Task.sleep(nanoseconds: 1_000_000_000)
        guard let self else { return }
        self.sentFPS = self.uploader.takeSentCount()
      }
    }
  }

  var registrationText: String {
    switch registrationState {
    case .registered: return "Kayıtlı"
    case .registering: return "Kaydediliyor…"
    case .available: return "Kayıtlı değil"
    case .unavailable: return "Kullanılamıyor"
    default: return String(describing: registrationState)
    }
  }

  var isRegistered: Bool { registrationState == .registered }

  // MARK: - Kayit (bir kerelik)

  func register() {
    Task {
      do {
        try await wearables.startRegistration()
      } catch {
        report(error.localizedDescription)
      }
    }
  }

  func unregister() {
    Task {
      do {
        try await wearables.startUnregistration()
      } catch {
        report(error.localizedDescription)
      }
    }
  }

  // MARK: - Guncellemeler (Meta AI ilgili sayfayi acar)

  /// Gozlukteki DAT uygulamasini guncelle ("App connections" sayfasi)
  func openGlassesAppUpdate() {
    Task {
      do {
        try await wearables.openDATGlassesAppUpdate()
        needsGlassesAppUpdate = false
      } catch {
        report(error.localizedDescription)
      }
    }
  }

  /// Gozluk firmware guncellemesi
  func openFirmwareUpdate() {
    Task {
      do {
        try await wearables.openFirmwareUpdate()
      } catch {
        report(error.localizedDescription)
      }
    }
  }

  // MARK: - Yayin

  func start() {
    guard !isRunning else { return }
    let trimmed = serverURL.trimmingCharacters(in: .whitespacesAndNewlines)
    guard let url = URL(string: trimmed), url.scheme == "ws" || url.scheme == "wss", url.host != nil else {
      report("PC adresi geçersiz. Örnek: ws://192.168.1.20:8000/ingest")
      return
    }

    applyProfile()  // Otomatik profilde o anki aga gore cozunurluk/kare hizi
    isRunning = true
    UIApplication.shared.isIdleTimerDisabled = true  // ekran kapanmasin, yayin kesilmesin
    uploader.connect(to: url)

    do {
      let session = try wearables.createSession(deviceSelector: deviceSelector)
      deviceSession = session
      session.statePublisher.listen { [weak self] state in
        Task { @MainActor in self?.handleSessionState(state) }
      }.store(in: sessionTokens)
      session.errorPublisher.listen { [weak self] error in
        let needsUpdate: Bool
        if case .datAppOnTheGlassesUpdateRequired = error { needsUpdate = true } else { needsUpdate = false }
        Task { @MainActor in
          if needsUpdate { self?.needsGlassesAppUpdate = true }
          self?.report(error.localizedDescription)
        }
      }.store(in: sessionTokens)
      sessionState = .starting
      try session.start()
    } catch {
      report(error.localizedDescription)
      stop()
    }
  }

  func stop() {
    camera?.stop()
    camera = nil
    currentStream = nil
    deliverPhoto(nil)
    deviceSession?.stop()
    uploader.disconnect()
    isRunning = false
    UIApplication.shared.isIdleTimerDisabled = false
  }

  private func handleSessionState(_ state: DeviceSessionState) {
    sessionState = state
    if state == .started, camera == nil, let session = deviceSession {
      Task { await self.startCamera(on: session) }
    }
    if state == .stopped {
      sessionTokens.clear()
      streamTokens.clear()
      deviceSession = nil
      camera = nil
      if isRunning { stop() }
    }
  }

  private func startCamera(on session: DeviceSession) async {
    // Kamera izni: verilmemisse Meta AI uygulamasi acilir, onaylayip geri don.
    do {
      if try await wearables.checkPermissionStatus(.camera) != .granted {
        guard try await wearables.requestPermission(.camera) == .granted else {
          report("Kamera izni verilmedi.")
          stop()
          return
        }
      }
    } catch {
      report(error.localizedDescription)
      stop()
      return
    }

    guard isRunning, camera == nil else { return }

    let config = StreamConfiguration(
      videoCodec: .raw,
      resolution: quality.sdkValue,
      frameRate: frameRate
    )

    do {
      guard let newCamera = try session.addCamera(config: config) else {
        report("Kamera oluşturulamadı. Tekrar dene.")
        stop()
        return
      }
      camera = newCamera
      let stream = newCamera.stream
      currentStream = stream

      // Claude sorulari icin yuksek cozunurluklu fotograf
      stream.photoDataPublisher.listen { [weak self] photo in
        let data = photo.data
        Task { @MainActor in self?.deliverPhoto(data) }
      }.store(in: streamTokens)
      let uploader = self.uploader
      let throttle = self.previewThrottle
      let qbox = self.qualityBox
      qbox.value = jpegQuality.value
      let fbuf = self.frameBuffer
      let qr = self.qrReader

      stream.statePublisher.listen { [weak self] state in
        Task { @MainActor in self?.streamState = state }
      }.store(in: streamTokens)

      stream.errorPublisher.listen { [weak self] error in
        Task { @MainActor in self?.report(error.localizedDescription) }
      }.store(in: streamTokens)

      // Her kare: JPEG'e cevir -> PC'ye gonder. Onizleme saniyede 5 kez guncellenir.
      stream.videoFramePublisher.listen { [weak self] frame in
        guard let image = frame.makeUIImage(),
          let jpeg = image.jpegData(compressionQuality: qbox.value)
        else { return }
        if let n = uploader.send(jpeg) { fbuf.put(n, jpeg) }
        qr.process(image)  // QR telefonda, aninda
        if throttle.allow() {
          Task { @MainActor in self?.preview = image }
        }
      }.store(in: streamTokens)

      streamState = .starting
      stream.start()
    } catch {
      report(error.localizedDescription)
      stop()
    }
  }

  // MARK: - Sesli anons

  var speechEnabled = true
  var lastAnnouncement: String?
  @ObservationIgnored private let speaker = Speaker()

  func announce(_ text: String) {
    lastAnnouncement = text
    if speechEnabled { speaker.say(text) }
  }

  func testSpeech() {
    speaker.say("Glass Bridge sesli bildirim testi.")
  }

  // MARK: - Canli kutular, modeller, kayitlar

  struct DetBox: Identifiable {
    let id: Int
    let x1, y1, x2, y2: Double
    let label: String
    let color: String
    let small: Bool
  }

  struct ModelItem: Identifiable {
    let key: String
    let name: String
    let desc: String
    var on: Bool
    var id: String { key }
  }

  struct RecFile: Identifiable {
    let file: String
    let mb: Double
    let time: Double
    var id: String { file }
    var annotated: Bool { file.contains("-kkd") }
  }

  var detBoxes: [DetBox] = []
  var visibleCount = 0
  var totalPeople = 0
  var analysisLatency: Int?
  var models: [ModelItem] = []
  var recordings: [RecFile] = []
  var recordingSince: Double?
  var recordAnnotated = true

  /// Canli sekmesi acikken onizleme 15 kare/sn, degilse 4 (pil ve islemci icin)
  var liveActive = false {
    didSet {
      previewThrottle.setRate(perSecond: liveActive ? 15 : 4)
      frameBuffer.enabled = liveActive && lockBoxesToFrame
      if !liveActive { frameBuffer.clear(); syncedFrame = nil }
    }
  }
  /// PC'nin analiz ettigi kare (kutularla birebir eslesir)
  var syncedFrame: UIImage?
  @ObservationIgnored let frameBuffer = FrameBuffer()

  // Sayilan kisiler (yuz galerisi)
  struct FaceItem: Identifiable {
    let id: Int
    let file: String
    let time: Double
    var face: Bool = true
    var place: String? = nil
  }
  var faces: [FaceItem] = []

  /// PC'deki bir dosyanin http adresi (ws://host:8000/... -> http://host:8000/<path>)
  func httpURL(_ path: String) -> URL? {
    guard var c = URLComponents(string: serverURL.trimmingCharacters(in: .whitespaces)) else { return nil }
    c.scheme = c.scheme == "wss" ? "https" : "http"
    c.path = path
    return c.url
  }

  func toggleAI() {
    uploader.sendJSON(["type": "ai", "on": !(aiOn ?? false)])
  }

  func toggleRecord() {
    if recordingActive {
      recordingActive = false  // dugme aninda tepki versin; PC durumu ayrica dogrular
      uploader.sendJSON(["type": "record", "action": "stop"])
    } else {
      uploader.sendJSON(["type": "record", "action": "start", "mode": recordAnnotated ? "annotated" : "raw"])
    }
  }

  func setModel(_ key: String, on: Bool) {
    if let i = models.firstIndex(where: { $0.key == key }) { models[i].on = on }
    uploader.sendJSON(["type": "models_set", "enabled": [key: on]])
  }

  // MARK: - KKD, nesne listesi, plakalar (web ile ortak)

  struct PPEItem: Identifiable {
    let key: String
    let name: String
    var id: String { key }
  }

  var ppeItems: [PPEItem] = []
  var ppeRequired: [String] = []
  var objMode = "list"
  var objItems: [String] = []
  var objAlerts: [String] = []
  var watchlist: [String] = []

  func setPPE(_ key: String, required: Bool) {
    var req = ppeRequired.filter { $0 != key }
    if required { req.append(key) }
    ppeRequired = req
    uploader.sendJSON(["type": "ppe_set", "required": req])
  }

  func setObjMode(_ mode: String) {
    objMode = mode
    uploader.sendJSON(["type": "objects_set", "mode": mode])
  }

  func addObjects(_ text: String) {
    let add = text.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
    guard !add.isEmpty else { return }
    var items = objItems
    for a in add where !items.contains(a) { items.append(a) }
    uploader.sendJSON(["type": "objects_set", "items": items])
  }

  func removeObject(_ name: String) {
    uploader.sendJSON(["type": "objects_set", "items": objItems.filter { $0 != name },
                       "alerts": objAlerts.filter { $0 != name }])
  }

  func toggleObjAlert(_ name: String) {
    let alerts = objAlerts.contains(name) ? objAlerts.filter { $0 != name } : objAlerts + [name]
    uploader.sendJSON(["type": "objects_set", "alerts": alerts])
  }

  func addPlate(_ plate: String) {
    let p = plate.trimmingCharacters(in: .whitespaces)
    guard !p.isEmpty else { return }
    uploader.sendJSON(["type": "watchlist_set", "plates": watchlist + [p]])
  }

  func removePlate(_ plate: String) {
    uploader.sendJSON(["type": "watchlist_set", "plates": watchlist.filter { $0 != plate }])
  }

  /// PC'deki kayit dosyasinin adresi (ws://host:8000/ingest -> http://host:8000/recordings/...)
  func recordingURL(_ f: RecFile) -> URL? {
    guard var c = URLComponents(string: serverURL.trimmingCharacters(in: .whitespaces)) else { return nil }
    c.scheme = c.scheme == "wss" ? "https" : "http"
    c.path = "/recordings/" + f.file
    return c.url
  }

  // MARK: - PC'den gelen mesajlar

  var aiOn: Bool?
  var recordingActive = false
  var assistantText: String?
  var lastQuestion: String?

  private func handleServerMessage(_ text: String) {
    guard let data = text.data(using: .utf8),
      let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
      let type = obj["type"] as? String
    else { return }
    switch type {
    case "announce":
      if let say = obj["text"] as? String { announce(say) }
    case "ai_state":
      aiOn = obj["on"] as? Bool
    case "recording":
      recordingActive = obj["active"] as? Bool ?? false
      recordingSince = obj["since"] as? Double
      if let files = obj["files"] as? [[String: Any]] {
        recordings = files.compactMap { f in
          guard let name = f["file"] as? String else { return nil }
          return RecFile(file: name, mb: f["mb"] as? Double ?? 0, time: f["time"] as? Double ?? 0)
        }
      }
    case "ppe":
      ppeRequired = obj["required"] as? [String] ?? []
      if let items = obj["items"] as? [[String: Any]] {
        ppeItems = items.compactMap { i in
          guard let k = i["key"] as? String else { return nil }
          return PPEItem(key: k, name: i["name"] as? String ?? k)
        }
      }
    case "objects_cfg":
      objMode = obj["mode"] as? String ?? "list"
      objItems = obj["items"] as? [String] ?? []
      objAlerts = obj["alerts"] as? [String] ?? []
    case "watchlist":
      watchlist = obj["plates"] as? [String] ?? []
    case "it_state":
      itActive = obj["active"] as? Bool ?? false
      itAuto = obj["auto"] as? Bool ?? false
    case "it_event":
      guard let id = obj["id"] as? String else { break }
      if obj["event"] as? String == "start" {
        itCurrent = ITRecord(obj)
      } else if itCurrent?.id == id {
        itCurrent?.update(obj)
      }
    case "it_list":
      if let items = obj["items"] as? [[String: Any]] { itHistory = items.map { ITRecord($0) } }
    case "it_capture_request":
      // Bilgi islem: goruntu sabitlendi -> yuksek cozunurluklu foto cek, taramaya yolla
      Task { await self.sendITScan() }
    case "it_qr":
      itQR = (obj["text"] as? String ?? "", obj["item"] as? [String: Any])
    case "vehicles":
      if let items = obj["items"] as? [[String: Any]] {
        vehicleLog = items.map { v in
          VehicleEntry(time: v["time"] as? Double ?? 0, type: v["type"] as? String ?? "araç",
                       color: v["color"] as? String, plate: v["plate"] as? String, image: v["image"] as? String,
                       place: v["place"] as? String)
        }
      }
      if let on = obj["log_vehicles"] as? Bool { logVehicles = on }
      if let on = obj["log_plates"] as? Bool { logPlates = on }
      if let items = obj["plates"] as? [[String: Any]] {
        plateLog = items.map { v in
          VehicleEntry(time: v["time"] as? Double ?? 0, type: v["type"] as? String ?? "",
                       color: v["color"] as? String, plate: v["plate"] as? String, image: v["image"] as? String,
                       place: v["place"] as? String)
        }
      }
    case "count_state":
      countActive = obj["active"] as? Bool ?? false
      countSince = obj["start"] as? Double
      countLocation = obj["location"] as? String
    case "sessions":
      if let items = obj["items"] as? [[String: Any]] {
        countSessions = items.map { CountSession($0) }
      }
    case "qr_list":
      if let items = obj["items"] as? [[String: Any]] {
        qrHistory = items.map { q in
          let item = q["item"] as? [String: Any]
          return QREntry(time: q["time"] as? Double ?? 0, text: q["text"] as? String ?? "",
                         name: item?["ad"] as? String, place: item?["konum"] as? String,
                         ip: item?["ip"] as? String, image: q["image"] as? String, where_: q["place"] as? String)
        }
      }
    case "faces_list":
      if let items = obj["items"] as? [[String: Any]] {
        faces = items.compactMap { f in
          guard let id = f["id"] as? Int, let file = f["file"] as? String else { return nil }
          return FaceItem(id: id, file: file, time: f["time"] as? Double ?? 0,
                          face: f["face"] as? Bool ?? true, place: f["place"] as? String)
        }
      }
    case "models":
      if let items = obj["items"] as? [[String: Any]] {
        models = items.compactMap { m in
          guard let key = m["key"] as? String else { return nil }
          return ModelItem(key: key, name: m["name"] as? String ?? key, desc: m["desc"] as? String ?? "",
                           on: m["on"] as? Bool ?? false)
        }
      }
    case "detections":
      if let c = obj["counts"] as? [String: Any] {
        visibleCount = c["visible"] as? Int ?? 0
        totalPeople = c["total_people"] as? Int ?? 0
      }
      analysisLatency = obj["latency_ms"] as? Int
      // Kutulari yalnizca Canli sekmesi acikken coz (gereksiz is yapma)
      guard liveActive, let boxes = obj["boxes"] as? [[String: Any]] else { return }
      // Analiz edilen kareyi tampondan bul: kutu ve goruntu ayni anin
      if lockBoxesToFrame, let n = obj["seq"] as? Int, let data = frameBuffer.get(n), let img = UIImage(data: data) {
        syncedFrame = img
      } else if obj["counts"] is NSNull || obj["counts"] == nil {
        syncedFrame = nil  // analiz kapali: canli onizlemeye don
      }
      detBoxes = boxes.enumerated().map { i, b in
        DetBox(id: i, x1: b["x1"] as? Double ?? 0, y1: b["y1"] as? Double ?? 0,
               x2: b["x2"] as? Double ?? 0, y2: b["y2"] as? Double ?? 0,
               label: b["label"] as? String ?? "", color: b["color"] as? String ?? "#8AB4FF",
               small: b["small"] as? Bool ?? false)
      }
    case "assistant":
      // Claude: {"event":"question"|"delta"|"done", "text":...} ve okunacak {"say":...}
      let event = obj["event"] as? String
      if event == "question" { lastQuestion = obj["text"] as? String; assistantText = nil; assistantBusy = true }
      if event == "delta" || event == "done", let t = obj["text"] as? String { assistantText = t }
      // Durdurulduysa yoldaki cumleleri okuma
      if let say = obj["say"] as? String, !say.isEmpty, !stoppedByUser { announce(say) }
      if event == "done" || event == "cancel" || event == "error" { assistantBusy = false; stoppedByUser = false }
      if event == "done" { voice.allowFollowUp(seconds: 12) }
    default:
      break
    }
  }

  // MARK: - Sesli komutlar

  var voiceEnabled = false
  var voiceListening = false
  var lastHeard: String?
  var voiceHint: String?
  var isSpeaking = false
  var assistantBusy = false
  @ObservationIgnored private var stoppedByUser = false

  /// AI konusurken araya gir: sesi hemen kes, Claude'un cevabini durdur
  func stopTalking() {
    speaker.stop()
    if assistantBusy {
      stoppedByUser = true
      uploader.sendJSON(["type": "ask_cancel"])
    }
    assistantBusy = false
  }

  /// Kayitlari sil: "people" | "vehicles" | "it" | "qr" | "sessions" | "videos" | "all"
  func wipe(_ what: String) {
    uploader.sendJSON(["type": "wipe", "what": what])
  }
  @ObservationIgnored private let voice = VoiceCommands()

  func setVoice(_ on: Bool) {
    voiceEnabled = on
    if on {
      Task {
        if await voice.requestPermissions() {
          voice.start()
        } else {
          voiceEnabled = false
          report("Mikrofon veya konuşma tanıma izni verilmedi. Ayarlar > Glass Bridge'den aç.")
        }
      }
    } else {
      voice.stop()
    }
  }

  private func handleVoice(_ cmd: VoiceCommand, raw: String) {
    uploader.sendJSON(["type": "voice", "text": raw])  // web'de "duyulan" olarak gorunur
    switch cmd {
    case .aiOn:
      uploader.sendJSON(["type": "ai", "on": true, "source": "voice"])
    case .aiOff:
      uploader.sendJSON(["type": "ai", "on": false, "source": "voice"])
    case .recordStart:
      uploader.sendJSON(["type": "record", "action": "start", "mode": "annotated", "source": "voice"])
    case .recordStop:
      uploader.sendJSON(["type": "record", "action": "stop", "source": "voice"])
    case .itOn:
      uploader.sendJSON(["type": "it_mode", "on": true])
    case .itOff:
      uploader.sendJSON(["type": "it_mode", "on": false])
    case .itScan:
      speaker.say("Tarıyorum.")
      Task { await self.sendITScan() }
    case .itError(let pc, let text):
      itError(pc: pc, text: text)
    case .ecg:
      sendECG()
    case .countStart(let ppe):
      startCount(ppe: ppe)
    case .countStop:
      stopCount()
    case .stopTalking:
      stopTalking()
    case .ask(let question):
      let stop = ["sus", "dur", "yeter", "kes"].contains(VoiceCommands.normalize(question))
      if stop {
        stopTalking()  // okunmakta olan cevabi hemen kes
        return
      }
      assistantBusy = true
      speaker.say("Bakıyorum.")
      Task { await self.ask(question) }
    }
  }

  /// Claude'a soru: gozlukle fotograf cek (yayin aciksa), soruyla birlikte PC'ye gonder
  func ask(_ question: String) async {
    var payload: [String: Any] = ["type": "ask", "text": question, "source": "voice"]
    if Self.needsPhoto(question), let photo = await capturePhoto(), let small = Self.shrink(photo, maxSide: 1600) {
      payload["photo"] = small.base64EncodedString()
    }
    uploader.sendJSON(payload)
  }

  /// Soru gordugumuz bir seyle mi ilgili? ("bu ne", "suna bak", "ne yaziyor"...) Degilse foto cekme.
  static func needsPhoto(_ q: String) -> Bool {
    let t = " " + VoiceCommands.normalize(q) + " "
    let keys = [" bu ", " bunu", " bunun", " buna", " su ", " sunu", " sunun", " suna", " bak", " gor", " oku",
                " yaziyor", " ekran", " resim", " foto", " goruntu", " karsimda", " onumde", " nedir", " ne bu",
                " hangi", " kac tane", " say "]
    return keys.contains { t.contains($0) }
  }

  // MARK: - Fotograf

  @ObservationIgnored private var currentStream: MWDATCamera.Stream?
  @ObservationIgnored private var photoWaiter: CheckedContinuation<Data?, Never>?

  private func capturePhoto(timeout: Double = 4) async -> Data? {
    guard let stream = currentStream, photoWaiter == nil else { return nil }
    return await withCheckedContinuation { (cont: CheckedContinuation<Data?, Never>) in
      photoWaiter = cont
      if !stream.capturePhoto(format: .jpeg) {
        deliverPhoto(nil)
        return
      }
      Task { [weak self] in
        try? await Task.sleep(nanoseconds: UInt64(timeout * 1_000_000_000))
        self?.deliverPhoto(nil)
      }
    }
  }

  private func deliverPhoto(_ data: Data?) {
    guard let w = photoWaiter else { return }
    photoWaiter = nil
    w.resume(returning: data)
  }

  /// Buyuk fotografi kucult (Claude icin 1600 px yeterli, ag yukunu azaltir)
  static func shrink(_ data: Data, maxSide: CGFloat) -> Data? {
    guard let img = UIImage(data: data) else { return nil }
    let s = img.size
    let k = min(1, maxSide / max(s.width, s.height))
    if k >= 1 { return img.jpegData(compressionQuality: 0.85) }
    let size = CGSize(width: s.width * k, height: s.height * k)
    let out = UIGraphicsImageRenderer(size: size).image { _ in img.draw(in: CGRect(origin: .zero, size: size)) }
    return out.jpegData(compressionQuality: 0.85)
  }

  // MARK: - Kisi sayim oturumlari

  struct CountSession: Identifiable {
    let id: String
    let start: Double
    let minutes: Double
    let total: Int
    let location: String?
    let ppeText: String
    let faces: [FaceItem]

    init(_ o: [String: Any]) {
      start = o["start"] as? Double ?? 0
      id = (o["day"] as? String ?? "") + "-" + (o["id"] as? String ?? UUID().uuidString)
      minutes = o["minutes"] as? Double ?? 0
      total = o["total"] as? Int ?? 0
      location = o["location"] as? String
      let ppe = o["ppe"] as? [[String: Any]] ?? []
      ppeText = ppe.compactMap { p -> String? in
        guard let name = p["name"] as? String else { return nil }
        let ok = p["ok"] as? Int ?? 0
        let miss = p["missing"] as? Int ?? 0
        return "\(name): \(ok) var, \(miss) eksik"
      }.joined(separator: " · ")
      faces = (o["faces"] as? [[String: Any]] ?? []).compactMap { f in
        guard let id = f["id"] as? Int, let file = f["file"] as? String else { return nil }
        return FaceItem(id: id, file: file, time: f["time"] as? Double ?? 0, face: f["face"] as? Bool ?? true)
      }
    }
  }

  var countActive = false
  var countSince: Double?
  var countLocation: String?
  var countSessions: [CountSession] = []
  @ObservationIgnored private lazy var locator = LocationHelper()

  /// "Kisi saymayi baslat": konumu al, PC'de oturumu ac (YZ katmani + kisi/yuz modelleri acilir).
  /// ppe: cumlede gecen ekipmanlar (or. maske) -> bu sayimda var/yok olarak sayilir.
  func startCount(ppe: [String] = []) {
    Task {
      var payload: [String: Any] = ["type": "count_start", "ppe": ppe]
      if let loc = await locator.current() {
        payload["lat"] = loc.lat
        payload["lon"] = loc.lon
        if let n = loc.name { payload["location"] = n }
      }
      uploader.sendJSON(payload)
    }
  }

  func stopCount() {
    uploader.sendJSON(["type": "count_stop"])
  }

  /// Panel dugmesi: PC hangi durumdaysa tersine cevirir (telefonun bildigi durum yanilsa da)
  func toggleCount() {
    if countActive {
      countActive = false
      uploader.sendJSON(["type": "count_toggle"])
    } else {
      startCount()
    }
  }

  // MARK: - QR gecmisi

  struct QREntry: Identifiable {
    let time: Double
    let text: String
    let name: String?
    let place: String?
    let ip: String?
    let image: String?
    let where_: String?
    var id: String { "\(time)-\(text)" }
  }

  var qrHistory: [QREntry] = []

  // MARK: - Arac gunlugu

  struct VehicleEntry: Identifiable {
    let time: Double
    let type: String
    let color: String?
    let plate: String?
    let image: String?
    let place: String?
    var id: String { "\(time)-\(plate ?? type)" }
    var title: String {
      let s = [color, type].compactMap { $0 }.joined(separator: " ")
      return s.prefix(1).uppercased() + s.dropFirst()
    }
  }

  var vehicleLog: [VehicleEntry] = []
  var plateLog: [VehicleEntry] = []
  var logVehicles = true
  var logPlates = true

  /// Araclari / plakalari kaydet. Acilinca ilgili model de PC'de acilir.
  func setVehicleLogging(vehicles: Bool? = nil, plates: Bool? = nil) {
    var p: [String: Any] = ["type": "vehicles_set"]
    if let vehicles { logVehicles = vehicles; p["log_vehicles"] = vehicles }
    if let plates { logPlates = plates; p["log_plates"] = plates }
    uploader.sendJSON(p)
  }

  // MARK: - Bilgi islem

  struct ITRecord: Identifiable {
    var id: String
    var kind: String  // "hw" | "error"
    var time: Double
    var image: String
    var source: String
    var qr: String?
    var summary: String
    var text: String
    var seconds: Double?
    var done: Bool
    var urgent: String?
    var model: String?
    var place: String?

    init(_ o: [String: Any]) {
      id = o["id"] as? String ?? UUID().uuidString
      kind = o["kind"] as? String ?? "hw"
      time = o["time"] as? Double ?? Date().timeIntervalSince1970
      image = o["image"] as? String ?? ""
      source = o["source"] as? String ?? ""
      qr = o["qr"] as? String
      summary = o["summary"] as? String ?? ""
      text = o["text"] as? String ?? ""
      seconds = o["seconds"] as? Double
      done = (o["event"] as? String).map { $0 == "done" } ?? true
      urgent = o["urgent"] as? String
      model = o["model"] as? String
      place = o["place"] as? String
    }

    mutating func update(_ o: [String: Any]) {
      if let t = o["text"] as? String { text = t }
      if let s = o["summary"] as? String, !s.isEmpty { summary = s }
      if let s = o["seconds"] as? Double { seconds = s }
      if o["event"] as? String == "done" { done = true }
      if let u = o["urgent"] as? String { urgent = u }
      if let m = o["model"] as? String { model = m }
    }

    /// Ozet satiri haric ayrinti metni
    var body: String {
      text.replacingOccurrences(of: "**", with: "").replacingOccurrences(of: #"^\s*ÖZET:[^\n]*\n?"#, with: "", options: .regularExpression)
    }
  }

  var itActive = false
  var itAuto = false
  var itCurrent: ITRecord?
  var itHistory: [ITRecord] = []
  var itQR: (String, [String: Any]?)?
  @ObservationIgnored let qrReader = QRReader()

  func setIT(_ on: Bool) {
    uploader.sendJSON(["type": "it_mode", "on": on])
  }

  /// Cihaza bir saniye sabit bakinca kendiliginden tara (varsayilan kapali)
  func setITAuto(_ on: Bool) {
    itAuto = on
    uploader.sendJSON(["type": "it_auto", "on": on])
  }

  // MARK: - Konum (kayitlara "nerede" bilgisi)

  @ObservationIgnored private var locationTask: Task<Void, Never>?

  func startLocationReports() {
    locationTask?.cancel()
    locationTask = Task { [weak self] in
      while !Task.isCancelled {
        guard let self, self.socketConnected else { return }
        if let loc = await self.locator.current() {
          var p: [String: Any] = ["type": "loc", "lat": loc.lat, "lon": loc.lon]
          if let n = loc.name { p["name"] = n }
          self.uploader.sendJSON(p)
        }
        try? await Task.sleep(nanoseconds: 120 * 1_000_000_000)
      }
    }
  }

  /// Donanim taramasi: gozlukle yuksek cozunurluklu foto (yoksa PC canli kareyi kullanir)
  func sendITScan() async {
    var payload: [String: Any] = ["type": "it_scan"]
    if let photo = await capturePhoto(), let small = Self.shrink(photo, maxSide: 1800) {
      payload["photo"] = small.base64EncodedString()
    }
    uploader.sendJSON(payload)
  }

  /// EKG: gozlukle yuksek cozunurluklu foto -> Opus hizli sesli ozet + Fable 5.2 ayrintili yorum
  func sendECG() {
    speaker.say("EKG'ye bakıyorum.")
    Task {
      var payload: [String: Any] = ["type": "ecg"]
      if let photo = await capturePhoto(), let small = Self.shrink(photo, maxSide: 2200) {
        payload["photo"] = small.base64EncodedString()
      }
      uploader.sendJSON(payload)
    }
  }

  /// "Bu hata ne": sahada gozluk fotografi; "bilgisayarimdaki" -> PC ekrani
  func itError(pc: Bool, text: String = "") {
    speaker.say("Hataya bakıyorum.")
    if pc {
      uploader.sendJSON(["type": "it_error", "pc": true, "text": text])
      return
    }
    Task {
      var payload: [String: Any] = ["type": "it_error", "text": text]
      if let photo = await capturePhoto(), let small = Self.shrink(photo, maxSide: 1800) {
        payload["photo"] = small.base64EncodedString()
      }
      uploader.sendJSON(payload)
    }
  }

  // MARK: - Hata

  func report(_ message: String) {
    errorMessage = message
  }
}
