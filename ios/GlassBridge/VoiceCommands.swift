import AVFoundation
import Foundation
import Speech

/// Gozluk mikrofonundan (Bluetooth) Turkce surekli dinleme ve komut algilama.
/// Komutlar:
///   "yapay zeka katmanini calistir / ac / baslat"  -> .aiOn
///   "yapay zeka katmanini kapat / durdur"           -> .aiOff
///   "kaydi baslat" / "kaydi durdur"                 -> .recordStart / .recordStop
///   "Claude, <soru>"                                -> .ask(soru)  (susunca gonderilir)
enum VoiceCommand: Equatable {
  case aiOn, aiOff, recordStart, recordStop
  case ask(String)
}

@MainActor
final class VoiceCommands: NSObject {
  var onCommand: ((VoiceCommand, String) -> Void)?
  var onHeard: ((String) -> Void)?
  var onStateChange: ((Bool) -> Void)?

  private let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "tr-TR"))
  private let engine = AVAudioEngine()
  private var request: SFSpeechAudioBufferRecognitionRequest?
  private var task: SFSpeechRecognitionTask?
  private var restartTimer: Timer?
  private var silenceTimer: Timer?
  private(set) var isListening = false
  private var paused = false  // TTS konusurken kendi sesimizi duymayalim
  private var askBuffer: String?
  private var generation = 0

  // MARK: - Izinler

  func requestPermissions() async -> Bool {
    let speechOK = await withCheckedContinuation { cont in
      SFSpeechRecognizer.requestAuthorization { cont.resume(returning: $0 == .authorized) }
    }
    let micOK = await AVAudioApplication.requestRecordPermission()
    return speechOK && micOK
  }

  // MARK: - Baslat / durdur

  func start() {
    guard !isListening, recognizer?.isAvailable == true else { return }
    isListening = true
    onStateChange?(true)
    beginTask()
  }

  func stop() {
    isListening = false
    endTask()
    onStateChange?(false)
  }

  /// TTS konusurken dinlemeyi duraklat (gozluk hoparloru mikrofona cok yakin)
  func setPaused(_ p: Bool) {
    guard isListening, p != paused else { return }
    paused = p
    if p { endTask() } else { beginTask() }
  }

  private func beginTask() {
    endTask()
    guard isListening, !paused, let recognizer else { return }
    AudioSessionManager.shared.activate()

    let req = SFSpeechAudioBufferRecognitionRequest()
    req.shouldReportPartialResults = true
    if recognizer.supportsOnDeviceRecognition { req.requiresOnDeviceRecognition = true }
    request = req

    let input = engine.inputNode
    let format = input.outputFormat(forBus: 0)
    guard format.sampleRate > 0 else { return }
    input.removeTap(onBus: 0)
    input.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak req] buffer, _ in
      req?.append(buffer)
    }
    engine.prepare()
    do { try engine.start() } catch {
      NSLog("[voice] motor baslamadi: \(error)")
      return
    }

    generation += 1
    let gen = generation
    task = recognizer.recognitionTask(with: req) { [weak self] result, error in
      let text = result?.bestTranscription.formattedString
      let isFinal = result?.isFinal ?? false
      let failed = error != nil
      Task { @MainActor in
        // Iptal edilmis eski oturumlarin geri cagrilarini yok say (sonsuz yeniden baslatma olmasin)
        guard let self, gen == self.generation else { return }
        if let text { self.handle(text, isFinal: isFinal) }
        if failed || isFinal { self.scheduleRestart(after: 0.3) }
      }
    }
    // Apple tanima oturumlari ~1 dk ile sinirli: duzenli yenile
    restartTimer?.invalidate()
    restartTimer = Timer.scheduledTimer(withTimeInterval: 50, repeats: false) { [weak self] _ in
      Task { @MainActor in self?.beginTask() }
    }
  }

  private func endTask() {
    generation += 1  // bekleyen geri cagrilari gecersiz kil
    restartTimer?.invalidate()
    silenceTimer?.invalidate()
    task?.cancel()
    task = nil
    request?.endAudio()
    request = nil
    if engine.isRunning { engine.stop() }
    engine.inputNode.removeTap(onBus: 0)
  }

  private func scheduleRestart(after s: Double) {
    guard isListening, !paused else { return }
    let gen = generation
    Timer.scheduledTimer(withTimeInterval: s, repeats: false) { [weak self] _ in
      Task { @MainActor in
        guard let self, gen == self.generation else { return }
        self.beginTask()
      }
    }
  }

  // MARK: - Komut algilama

  private func handle(_ raw: String, isFinal: Bool) {
    onHeard?(raw)
    let t = Self.normalize(raw)

    // "Claude, ..." sorusu: anahtar kelimeden sonrasini topla, 1.3 sn sessizlikte gonder
    if let range = Self.claudeRange(in: t) {
      let q = String(t[range.upperBound...]).trimmingCharacters(in: .whitespacesAndNewlines.union(.punctuationCharacters))
      askBuffer = q
      silenceTimer?.invalidate()
      silenceTimer = Timer.scheduledTimer(withTimeInterval: isFinal ? 0.1 : 1.3, repeats: false) { [weak self] _ in
        Task { @MainActor in self?.flushAsk(raw) }
      }
      return
    }

    let ai = t.contains("yapay zeka") || t.contains("katman")
    let rec = t.contains("kayd") || t.contains("kayit") || t.contains("video")
    let on = ["calistir", "ac", "baslat", "aktif"].contains { t.contains($0) }
    let off = ["kapat", "durdur", "bitir", "kes"].contains { t.contains($0) }

    var cmd: VoiceCommand?
    if ai && off { cmd = .aiOff } else if ai && on { cmd = .aiOn }
    else if rec && off { cmd = .recordStop } else if rec && on { cmd = .recordStart }

    if let cmd { fire(cmd, raw) }
  }

  private func flushAsk(_ raw: String) {
    guard let q = askBuffer, !q.isEmpty else { return }
    askBuffer = nil
    fire(.ask(q), raw)
  }

  private func fire(_ cmd: VoiceCommand, _ raw: String) {
    onCommand?(cmd, raw)
    beginTask()  // metni sifirla ki ayni komut tekrar tetiklenmesin
  }

  /// Kucuk harf, Turkce karakterleri sadelestir (ı->i, ş->s ...)
  static func normalize(_ s: String) -> String {
    let lower = s.lowercased(with: Locale(identifier: "tr-TR"))
    let map: [Character: Character] = ["ı": "i", "ş": "s", "ğ": "g", "ü": "u", "ö": "o", "ç": "c", "â": "a", "î": "i", "û": "u"]
    return String(lower.map { map[$0] ?? $0 })
  }

  /// "claude", "klod", "klot", "clod" gibi duyulma bicimleri
  static func claudeRange(in t: String) -> Range<String.Index>? {
    for key in ["claude", "klod", "klot", "clod", "kload", "cloud"] {
      if let r = t.range(of: key) { return r }
    }
    return nil
  }
}

/// Ortak ses oturumu: gozluk mikrofonu (HFP) + gozluk hoparloru
@MainActor
final class AudioSessionManager {
  static let shared = AudioSessionManager()
  private var active = false

  func activate() {
    guard !active else { return }
    let s = AVAudioSession.sharedInstance()
    do {
      try s.setCategory(.playAndRecord, mode: .default,
                        options: [.allowBluetooth, .allowBluetoothA2DP, .duckOthers, .defaultToSpeaker])
      try s.setActive(true)
      // Bluetooth mikrofon varsa (gozluk) onu sec
      if let bt = s.availableInputs?.first(where: { $0.portType == .bluetoothHFP }) {
        try s.setPreferredInput(bt)
      }
      active = true
    } catch {
      NSLog("[audio] oturum ayarlanamadi: \(error)")
    }
  }
}
