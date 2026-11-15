import AVFoundation

/// Metni Turkce seslendirir. Gozluk bagliyken ses gozluk hoparlorunden cikar.
/// Konusma basladiginda/bittiginde haber verir (dinleme o sirada duraklatilir).
@MainActor
final class Speaker: NSObject, AVSpeechSynthesizerDelegate {
  private let synth = AVSpeechSynthesizer()
  private let voice = AVSpeechSynthesisVoice(language: "tr-TR")
  var onSpeakingChange: ((Bool) -> Void)?

  override init() {
    super.init()
    synth.delegate = self
  }

  func say(_ text: String) {
    AudioSessionManager.shared.activate()
    let u = AVSpeechUtterance(string: text)
    u.voice = voice
    u.rate = AVSpeechUtteranceDefaultSpeechRate * 1.05
    synth.speak(u)  // sirayla okunur, ust uste binmez
  }

  func stop() {
    synth.stopSpeaking(at: .immediate)
  }

  var isSpeaking: Bool { synth.isSpeaking }

  nonisolated func speechSynthesizer(_ s: AVSpeechSynthesizer, didStart u: AVSpeechUtterance) {
    Task { @MainActor in self.onSpeakingChange?(true) }
  }

  nonisolated func speechSynthesizer(_ s: AVSpeechSynthesizer, didFinish u: AVSpeechUtterance) {
    Task { @MainActor in if !self.synth.isSpeaking { self.onSpeakingChange?(false) } }
  }

  nonisolated func speechSynthesizer(_ s: AVSpeechSynthesizer, didCancel u: AVSpeechUtterance) {
    Task { @MainActor in self.onSpeakingChange?(false) }
  }
}
