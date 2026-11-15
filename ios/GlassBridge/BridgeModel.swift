import Foundation
import MWDATCamera
import MWDATCore
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

/// Kare isleme thread'inden okunan ayar (yaris zararsiz: tek CGFloat)
final class QualityBox: @unchecked Sendable {
  var value: CGFloat = 0.5
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
      Task { @MainActor in self?.socketConnected = connected }
    }
    uploader.onText = { [weak self] text in
      Task { @MainActor in self?.handleServerMessage(text) }
    }

    // Sesli komutlar: TTS konusurken dinleme duraklar (kendi sesini komut sanmasin)
    speaker.onSpeakingChange = { [weak self] speaking in
      self?.voice.setPaused(speaking)
    }
    voice.onHeard = { [weak self] text in self?.lastHeard = text }
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
        uploader.send(jpeg)
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
    didSet { previewThrottle.setRate(perSecond: liveActive ? 15 : 4) }
  }

  func toggleAI() {
    uploader.sendJSON(["type": "ai", "on": !(aiOn ?? false)])
  }

  func toggleRecord() {
    if recordingActive {
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
      // Yapay zeka katmani acikken Claude sohbet modunda: "Claude" demeden her cumle soru olur
      voice.conversationMode = aiOn ?? false
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
      detBoxes = boxes.enumerated().map { i, b in
        DetBox(id: i, x1: b["x1"] as? Double ?? 0, y1: b["y1"] as? Double ?? 0,
               x2: b["x2"] as? Double ?? 0, y2: b["y2"] as? Double ?? 0,
               label: b["label"] as? String ?? "", color: b["color"] as? String ?? "#8AB4FF",
               small: b["small"] as? Bool ?? false)
      }
    case "assistant":
      // Claude: {"event":"question"|"delta"|"done", "text":...} ve okunacak {"say":...}
      let event = obj["event"] as? String
      if event == "question" { lastQuestion = obj["text"] as? String; assistantText = nil }
      if event == "delta" || event == "done", let t = obj["text"] as? String { assistantText = t }
      if let say = obj["say"] as? String, !say.isEmpty { announce(say) }
    default:
      break
    }
  }

  // MARK: - Sesli komutlar

  var voiceEnabled = false
  var voiceListening = false
  var lastHeard: String?
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
    case .ask(let question):
      let stop = ["sus", "dur", "yeter", "kes"].contains(VoiceCommands.normalize(question))
      if stop {
        speaker.stop()  // okunmakta olan cevabi hemen kes
        uploader.sendJSON(["type": "ask_cancel"])
        return
      }
      speaker.say("Bakıyorum.")
      Task { await self.ask(question) }
    }
  }

  /// Claude'a soru: gozlukle fotograf cek (yayin aciksa), soruyla birlikte PC'ye gonder
  func ask(_ question: String) async {
    var payload: [String: Any] = ["type": "ask", "text": question, "source": "voice"]
    if let photo = await capturePhoto(), let small = Self.shrink(photo, maxSide: 1600) {
      payload["photo"] = small.base64EncodedString()
    }
    uploader.sendJSON(payload)
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

  // MARK: - Hata

  func report(_ message: String) {
    errorMessage = message
  }
}
