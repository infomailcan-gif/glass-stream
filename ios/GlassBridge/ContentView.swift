import SwiftUI

struct ContentView: View {
  @Bindable var model: BridgeModel

  var body: some View {
    NavigationStack {
      Form {
        Section("1 · Gözlük") {
          LabeledContent("Meta AI kaydı", value: model.registrationText)
          LabeledContent("Gözlük", value: model.hasDevice ? "Bağlı" : "Bulunamadı")
          if model.isRegistered {
            Button("Kaydı kaldır", role: .destructive) { model.unregister() }
              .disabled(model.isRunning)
          } else {
            Button("Meta AI ile eşleştir") { model.register() }
          }
        }

        Section {
          Button(model.needsGlassesAppUpdate ? "⚠️ Gözlük uygulamasını güncelle (gerekli)" : "Gözlük uygulamasını güncelle") {
            model.openGlassesAppUpdate()
          }
          Button("Gözlük yazılımını (firmware) güncelle") { model.openFirmwareUpdate() }
        } header: {
          Text("Güncellemeler")
        } footer: {
          Text("Meta AI'de ilgili güncelleme sayfası açılır. Gözlük şarjda ve yakında olsun.")
        }

        Section {
          TextField("ws://192.168.1.20:8000/ingest", text: $model.serverURL)
            .keyboardType(.URL)
            .textInputAutocapitalization(.never)
            .autocorrectionDisabled()
            .disabled(model.isRunning)
          LabeledContent("Bağlantı", value: model.socketConnected ? "Bağlı" : "Bağlı değil")
        } header: {
          Text("2 · PC sunucusu")
        } footer: {
          Text("PC'de `npm start` çalıştırınca ekrana yazılan “iPhone için” adresini gir.")
        }

        Section("3 · Görüntü ayarı") {
          Picker("Çözünürlük", selection: $model.quality) {
            ForEach(StreamQuality.allCases) { Text($0.title).tag($0) }
          }
          Picker("FPS", selection: $model.frameRate) {
            ForEach(model.frameRates, id: \.self) { Text("\($0)").tag($0) }
          }
        }
        .disabled(model.isRunning)

        Section {
          if model.isRunning {
            Button("Yayını durdur", role: .destructive) { model.stop() }
          } else {
            Button("Yayını başlat") { model.start() }
              .disabled(!model.isRegistered)
          }
          LabeledContent("Oturum", value: String(describing: model.sessionState))
          LabeledContent("Akış", value: String(describing: model.streamState))
          LabeledContent("PC'ye giden", value: "\(model.sentFPS) FPS")
        } footer: {
          Text("Yayın sırasında bu uygulama açık kalmalı; ekran otomatik kapanmaz.")
        }

        if let image = model.preview {
          Section("Önizleme") {
            Image(uiImage: image)
              .resizable()
              .scaledToFit()
              .frame(maxHeight: 360)
          }
        }
      }
      .navigationTitle("Glass Bridge")
      .alert(
        "Bir sorun oluştu",
        isPresented: Binding(
          get: { model.errorMessage != nil },
          set: { if !$0 { model.errorMessage = nil } }
        )
      ) {
        Button("Tamam", role: .cancel) {}
      } message: {
        Text(model.errorMessage ?? "")
      }
    }
  }
}
