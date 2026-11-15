import Foundation
import UIKit
import Vision

/// QR / barkodu dogrudan iPhone'da okur (Apple Vision, ~10-20 ms). PC'ye ve internete
/// gerek yok; okundugu an sesli soylenir, kayit icin PC'ye de gonderilir.
final class QRReader: @unchecked Sendable {
  private let lock = NSLock()
  private var lastSeen: [String: TimeInterval] = [:]
  private var busy = false
  private let throttle = Throttle(perSecond: 8)
  private let queue = DispatchQueue(label: "qr", qos: .userInitiated)
  var enabled = true

  /// Yeni (son 8 sn'de okunmamis) bir kod bulununca cagrilir
  var onCode: (@Sendable (String) -> Void)?

  func process(_ image: UIImage) {
    guard enabled, throttle.allow(), let cg = image.cgImage else { return }
    lock.lock()
    if busy { lock.unlock(); return }
    busy = true
    lock.unlock()

    queue.async { [weak self] in
      guard let self else { return }
      defer { self.lock.lock(); self.busy = false; self.lock.unlock() }
      let req = VNDetectBarcodesRequest()
      let handler = VNImageRequestHandler(cgImage: cg, options: [:])
      try? handler.perform([req])
      let now = Date().timeIntervalSince1970
      for obs in req.results ?? [] {
        guard let text = obs.payloadStringValue?.trimmingCharacters(in: .whitespacesAndNewlines), !text.isEmpty
        else { continue }
        self.lock.lock()
        let fresh = now - (self.lastSeen[text] ?? 0) > 8
        self.lastSeen[text] = now
        self.lock.unlock()
        if fresh { self.onCode?(text) }
      }
    }
  }
}
