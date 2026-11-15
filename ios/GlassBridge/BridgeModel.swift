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
  @ObservationIgnored private let previewThrottle = Throttle(perSecond: 5)

  init(wearables: WearablesInterface) {
    self.wearables = wearables
    self.deviceSelector = AutoDeviceSelector(wearables: wearables)
    self.registrationState = wearables.registrationState
    self.serverURL = UserDefaults.standard.string(forKey: "serverURL") ?? "ws://192.168.1.100:8000/ingest"

    uploader.onConnectionChange = { [weak self] connected in
      Task { @MainActor in self?.socketConnected = connected }
    }

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
      let uploader = self.uploader
      let throttle = self.previewThrottle

      stream.statePublisher.listen { [weak self] state in
        Task { @MainActor in self?.streamState = state }
      }.store(in: streamTokens)

      stream.errorPublisher.listen { [weak self] error in
        Task { @MainActor in self?.report(error.localizedDescription) }
      }.store(in: streamTokens)

      // Her kare: JPEG'e cevir -> PC'ye gonder. Onizleme saniyede 5 kez guncellenir.
      stream.videoFramePublisher.listen { [weak self] frame in
        guard let image = frame.makeUIImage(),
          let jpeg = image.jpegData(compressionQuality: 0.7)
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

  // MARK: - Hata

  func report(_ message: String) {
    errorMessage = message
  }
}
