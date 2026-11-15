import CoreLocation
import Foundation

/// Sayim oturumu icin tek seferlik konum + okunabilir adres ("Nilufer, Bursa").
@MainActor
final class LocationHelper: NSObject, CLLocationManagerDelegate {
  private let manager = CLLocationManager()
  private var waiter: CheckedContinuation<CLLocation?, Never>?

  override init() {
    super.init()
    manager.delegate = self
    manager.desiredAccuracy = kCLLocationAccuracyHundredMeters
  }

  /// En fazla `timeout` saniye bekler; izin yoksa veya bulunamazsa nil.
  func current(timeout: Double = 4) async -> (name: String?, lat: Double, lon: Double)? {
    if manager.authorizationStatus == .notDetermined { manager.requestWhenInUseAuthorization() }
    let loc: CLLocation? = await withCheckedContinuation { cont in
      waiter = cont
      manager.requestLocation()
      Task { [weak self] in
        try? await Task.sleep(nanoseconds: UInt64(timeout * 1_000_000_000))
        self?.finish(nil)
      }
    }
    guard let loc else { return nil }
    var name: String?
    if let p = try? await CLGeocoder().reverseGeocodeLocation(loc).first {
      name = [p.name, p.subLocality, p.locality].compactMap { $0 }.reduce(into: [String]()) { acc, s in
        if !acc.contains(s) { acc.append(s) }
      }.prefix(3).joined(separator: ", ")
    }
    return (name, loc.coordinate.latitude, loc.coordinate.longitude)
  }

  private func finish(_ loc: CLLocation?) {
    guard let w = waiter else { return }
    waiter = nil
    w.resume(returning: loc)
  }

  nonisolated func locationManager(_ m: CLLocationManager, didUpdateLocations locs: [CLLocation]) {
    let l = locs.last
    Task { @MainActor in self.finish(l) }
  }

  nonisolated func locationManager(_ m: CLLocationManager, didFailWithError error: Error) {
    Task { @MainActor in self.finish(nil) }
  }
}
