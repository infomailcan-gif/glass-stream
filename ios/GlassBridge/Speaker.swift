import AVFoundation

/// Metni Turkce seslendirir. Gozluk bagliyken iOS sesi gozluk hoparlorune yonlendirir.
@MainActor
final class Speaker {
  private let synth = AVSpeechSynthesizer()
  private let voice = AVSpeechSynthesisVoice(language: "tr-TR")
  private var configured = false

  func say(_ text: String) {
    configureSessionIfNeeded()
    let u = AVSpeechUtterance(string: text)
    u.voice = voice
    u.rate = AVSpeechUtteranceDefaultSpeechRate * 1.05
    synth.speak(u)  // sirayla okunur, ust uste binmez
  }

  func stop() {
    synth.stopSpeaking(at: .immediate)
  }

  private func configureSessionIfNeeded() {
    guard !configured else { return }
    let s = AVAudioSession.sharedInstance()
    do {
      try s.setCategory(.playback, mode: .spokenAudio, options: [.duckOthers, .allowBluetoothA2DP])
      try s.setActive(true)
      configured = true
    } catch {
      NSLog("[GlassBridge] ses oturumu ayarlanamadi: \(error)")
    }
  }
}
