import MWDATCore
import SwiftUI

@main
struct GlassBridgeApp: App {
  @State private var model: BridgeModel

  init() {
    do {
      try Wearables.configure()
    } catch {
      NSLog("[GlassBridge] Wearables.configure() basarisiz: \(error)")
    }
    _model = State(wrappedValue: BridgeModel(wearables: Wearables.shared))
  }

  var body: some Scene {
    WindowGroup {
      ContentView(model: model)
        // Meta AI uygulamasi kayit/izin sonrasi bu URL ile geri doner
        .onOpenURL { url in
          guard
            let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
            components.queryItems?.contains(where: { $0.name == "metaWearablesAction" }) == true
          else { return }
          Task {
            do {
              _ = try await Wearables.shared.handleUrl(url)
            } catch {
              model.report(error.localizedDescription)
            }
          }
        }
    }
  }
}
