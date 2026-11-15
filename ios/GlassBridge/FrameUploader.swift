import Foundation

/// Kareleri PC'deki sunucuya WebSocket ile (binary JPEG) gonderir.
/// Ayni anda en fazla `maxInFlight` kare yolda olur; ag yavassa yeni kareler atlanir,
/// boylece gecikme birikmez. (Tek kare yerine 3: yuksek gecikmeli mobil/Tailscale
/// baglantida gidis-donus suresi kare hizini dusurmesin.)
final class FrameUploader: NSObject, URLSessionWebSocketDelegate, @unchecked Sendable {
  private let lock = NSLock()
  private var session: URLSession!
  private var task: URLSessionWebSocketTask?
  private var url: URL?
  private var wantConnected = false
  private var inFlight = 0
  var maxInFlight = 3
  private var sentCount = 0

  /// Baglanti durumu degisince cagrilir (ana thread garanti degil).
  var onConnectionChange: (@Sendable (Bool) -> Void)?

  override init() {
    super.init()
    let config = URLSessionConfiguration.default
    config.waitsForConnectivity = false
    session = URLSession(configuration: config, delegate: self, delegateQueue: nil)
  }

  func connect(to url: URL) {
    lock.lock()
    self.url = url
    wantConnected = true
    lock.unlock()
    open()
  }

  func disconnect() {
    lock.lock()
    wantConnected = false
    let t = task
    task = nil
    inFlight = 0
    lock.unlock()
    t?.cancel(with: .goingAway, reason: nil)
    onConnectionChange?(false)
  }

  /// Bir JPEG kare gonder. Onceki kare hala yoldaysa bu kare atlanir.
  func send(_ jpeg: Data) {
    lock.lock()
    guard let t = task, inFlight < maxInFlight else {
      lock.unlock()
      return
    }
    inFlight += 1
    lock.unlock()

    t.send(.data(jpeg)) { [weak self] error in
      guard let self else { return }
      self.lock.lock()
      self.inFlight = max(0, self.inFlight - 1)
      if error == nil { self.sentCount += 1 }
      self.lock.unlock()
      if error != nil { self.handleDrop(of: t) }
    }
  }

  /// Son cagridan beri gonderilen kare sayisi (FPS gostergesi icin).
  func takeSentCount() -> Int {
    lock.lock()
    defer { lock.unlock() }
    let n = sentCount
    sentCount = 0
    return n
  }

  // MARK: - Ic isler

  private func open() {
    lock.lock()
    guard wantConnected, let url else {
      lock.unlock()
      return
    }
    let t = session.webSocketTask(with: url)
    task = t
    inFlight = 0
    lock.unlock()
    t.resume()
    listen(on: t)
  }

  /// Sunucudan gelen mesajlari oku. {"type":"announce","text":"..."} -> onAnnounce
  private func listen(on t: URLSessionWebSocketTask) {
    t.receive { [weak self] result in
      guard let self else { return }
      switch result {
      case .success(let message):
        if case .string(let text) = message {
          self.onText?(text)  // JSON: announce, ai_state, recording, assistant...
        }
        self.listen(on: t)
      case .failure:
        self.handleDrop(of: t)
      }
    }
  }

  /// PC'den gelen metin (JSON) mesajlari (ana thread garanti degil).
  var onText: (@Sendable (String) -> Void)?

  /// PC'ye komut gonder (JSON metni). Kareden bagimsiz, atlanmaz.
  func sendText(_ text: String) {
    lock.lock()
    let t = task
    lock.unlock()
    t?.send(.string(text)) { error in
      if let error { NSLog("[uploader] komut gonderilemedi: \(error)") }
    }
  }

  /// Sozlugu JSON'a cevirip gonder
  func sendJSON(_ obj: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: obj),
      let text = String(data: data, encoding: .utf8)
    else { return }
    sendText(text)
  }

  private func handleDrop(of t: URLSessionWebSocketTask) {
    lock.lock()
    guard task === t else {
      lock.unlock()
      return
    }
    task = nil
    inFlight = 0
    let retry = wantConnected
    lock.unlock()
    t.cancel()
    onConnectionChange?(false)
    if retry {
      DispatchQueue.global().asyncAfter(deadline: .now() + 1) { [weak self] in self?.open() }
    }
  }

  // MARK: - URLSessionWebSocketDelegate

  func urlSession(
    _ session: URLSession, webSocketTask: URLSessionWebSocketTask,
    didOpenWithProtocol protocol: String?
  ) {
    onConnectionChange?(true)
  }

  func urlSession(
    _ session: URLSession, webSocketTask: URLSessionWebSocketTask,
    didCloseWith closeCode: URLSessionWebSocketTask.CloseCode, reason: Data?
  ) {
    handleDrop(of: webSocketTask)
  }

  func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
    if let ws = task as? URLSessionWebSocketTask { handleDrop(of: ws) }
  }
}

/// Onizlemeyi saniyede birkac kez guncellemek icin basit zaman sinirlayici.
final class Throttle: @unchecked Sendable {
  private let lock = NSLock()
  private var last: TimeInterval = 0
  private var interval: TimeInterval

  init(perSecond: Double) { interval = 1.0 / perSecond }

  func setRate(perSecond: Double) {
    lock.lock()
    interval = 1.0 / perSecond
    lock.unlock()
  }

  func allow() -> Bool {
    lock.lock()
    defer { lock.unlock() }
    let now = Date().timeIntervalSince1970
    guard now - last >= interval else { return false }
    last = now
    return true
  }
}
