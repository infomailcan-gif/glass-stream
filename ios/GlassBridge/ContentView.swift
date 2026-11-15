import AVKit
import SwiftUI

// MARK: - Kok gorunum: acilis + sekmeler

struct ContentView: View {
  @Bindable var model: BridgeModel
  @State private var splashDone = false
  @State private var tab = 0

  var body: some View {
    ZStack {
      TabView(selection: $tab) {
        PanelView(model: model).tag(0)
          .tabItem { Label("Panel", systemImage: "shield.lefthalf.filled") }
        LiveView(model: model).tag(1)
          .tabItem { Label("Canlı", systemImage: "viewfinder") }
        ModelsView(model: model).tag(2)
          .tabItem { Label("Modeller", systemImage: "cpu") }
        RecordingsView(model: model).tag(3)
          .tabItem { Label("Kayıtlar", systemImage: "film.stack") }
        SettingsView(model: model).tag(4)
          .tabItem { Label("Ayarlar", systemImage: "gearshape") }
      }
      .tint(Theme.amber)
      .onChange(of: tab) { _, t in model.liveActive = (t == 1) }

      if !splashDone {
        SplashView { withAnimation(.easeOut(duration: 0.35)) { splashDone = true } }
          .transition(.opacity)
          .zIndex(1)
      }
    }
    .preferredColorScheme(.dark)
    .alert(
      "İşlem tamamlanamadı",
      isPresented: Binding(get: { model.errorMessage != nil }, set: { if !$0 { model.errorMessage = nil } })
    ) {
      Button("Tamam", role: .cancel) {}
    } message: {
      Text(model.errorMessage ?? "")
    }
  }
}

/// Ortak sayfa iskeleti: koyu zemin + baslik
struct Page<Content: View>: View {
  var title: String
  @ViewBuilder var content: Content

  var body: some View {
    NavigationStack {
      ScrollView {
        VStack(spacing: 14) { content }
          .padding(.horizontal, 16)
          .padding(.bottom, 24)
      }
      .background(Theme.ink)
      .navigationTitle(title)
      .toolbarBackground(Theme.ink, for: .navigationBar)
    }
  }
}

// MARK: - Panel

struct PanelView: View {
  @Bindable var model: BridgeModel

  var body: some View {
    Page(title: "Panel") {
      // Marka satiri
      HStack(spacing: 12) {
        BrandMark(size: 34)
        VStack(alignment: .leading, spacing: 0) {
          Text("Canberk Şıklı").font(.headline.weight(.heavy)).foregroundStyle(Theme.text)
          Text("AI Systems").font(.subheadline).foregroundStyle(Theme.muted)
        }
        Spacer()
      }
      .padding(.top, 4)

      // Ana dugme
      Button {
        model.isRunning ? model.stop() : model.start()
      } label: {
        HStack {
          Image(systemName: model.isRunning ? "stop.fill" : "play.fill")
          Text(model.isRunning ? "Yayını durdur" : "Yayını başlat").fontWeight(.semibold)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 16)
        .foregroundStyle(model.isRunning ? Theme.text : Color(hex: 0x1A1405))
        .background(model.isRunning ? Theme.raised : Theme.amber, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous).stroke(model.isRunning ? Theme.bad : .clear, lineWidth: 1))
      }
      .disabled(!model.isRegistered)

      Card(title: "Bağlantı") {
        HStack(spacing: 16) {
          StatusDot(on: model.hasDevice, text: "Gözlük")
          StatusDot(on: model.socketConnected, text: "Bilgisayar")
          StatusDot(on: model.aiOn == true, text: "Analiz")
        }
        if model.isRunning {
          Text("\(model.sentFPS) kare/sn gönderiliyor").font(.footnote).foregroundStyle(Theme.muted)
        }
        if !model.isRegistered {
          Text("Önce Ayarlar'dan gözlüğü Meta AI ile eşleştir.").font(.footnote).foregroundStyle(Theme.amber)
        }
      }

      Card(title: "Kontrol") {
        ControlRow(icon: "brain", title: "Yapay zekâ katmanı",
                   detail: "Analiz ve Claude sohbet modu", isOn: model.aiOn == true) { model.toggleAI() }
        Divider().overlay(Theme.line)
        ControlRow(icon: "record.circle", title: "Video kaydı",
                   detail: model.recordingActive ? "Kayıt sürüyor" : "Bilgisayara kaydedilir",
                   isOn: model.recordingActive, tint: Theme.bad) { model.toggleRecord() }
        Divider().overlay(Theme.line)
        ControlRow(icon: "waveform", title: "Sesli komutlar",
                   detail: model.voiceListening ? "Gözlük mikrofonu dinleniyor" : "Kapalı",
                   isOn: model.voiceEnabled) { model.setVoice(!model.voiceEnabled) }
      }

      if model.lastQuestion != nil || model.assistantText != nil {
        Card(title: "Claude") {
          if let q = model.lastQuestion {
            Text(q).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.text)
          }
          Text(model.assistantText ?? "Bakıyor…")
            .font(.subheadline).foregroundStyle(Theme.muted)
            .textSelection(.enabled)
        }
      }

      if let a = model.lastAnnouncement {
        Card(title: "Son anons") {
          Text(a).font(.subheadline).foregroundStyle(Theme.text)
        }
      }

      if let heard = model.lastHeard, model.voiceListening {
        Text("Duyulan: “\(heard)”").font(.footnote).foregroundStyle(Theme.muted)
          .frame(maxWidth: .infinity, alignment: .leading)
      }
    }
  }
}

struct ControlRow: View {
  var icon: String
  var title: String
  var detail: String
  var isOn: Bool
  var tint: Color = Theme.ok
  var action: () -> Void

  var body: some View {
    Button(action: action) {
      HStack(spacing: 12) {
        Image(systemName: icon)
          .font(.body.weight(.semibold))
          .foregroundStyle(isOn ? tint : Theme.muted)
          .frame(width: 28)
        VStack(alignment: .leading, spacing: 2) {
          Text(title).foregroundStyle(Theme.text).fontWeight(.semibold)
          Text(detail).font(.footnote).foregroundStyle(Theme.muted)
        }
        Spacer()
        Text(isOn ? "Açık" : "Kapalı")
          .font(.footnote.weight(.semibold))
          .padding(.horizontal, 10).padding(.vertical, 5)
          .foregroundStyle(isOn ? tint : Theme.muted)
          .background((isOn ? tint : Theme.line).opacity(isOn ? 0.15 : 0.6), in: Capsule())
      }
      .contentShape(Rectangle())
    }
    .buttonStyle(.plain)
  }
}

// MARK: - Canli (PC'deki kutularla)

struct LiveView: View {
  @Bindable var model: BridgeModel

  var body: some View {
    NavigationStack {
      ZStack {
        Theme.ink.ignoresSafeArea()
        if let img = model.preview {
          VStack(spacing: 12) {
            Image(uiImage: img)
              .resizable()
              .scaledToFit()
              .overlay { BoxesOverlay(boxes: model.detBoxes) }
              .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
              .overlay(ViewfinderCorners())
              .padding(.horizontal, 16)
            HStack(spacing: 18) {
              Label("\(model.visibleCount) görünen", systemImage: "person")
              Label("\(model.totalPeople) toplam", systemImage: "person.3")
              if let ms = model.analysisLatency, model.aiOn == true {
                Label("\(ms) ms", systemImage: "bolt")
              } else {
                Label("Analiz kapalı", systemImage: "bolt.slash")
              }
            }
            .font(.footnote)
            .foregroundStyle(Theme.muted)
          }
        } else {
          VStack(spacing: 12) {
            BrandMark(size: 52).opacity(0.6)
            Text("Görüntü yok").font(.headline).foregroundStyle(Theme.text)
            Text("Panel'den yayını başlat. Kutular yapay zekâ katmanı açıkken görünür.")
              .font(.subheadline).foregroundStyle(Theme.muted).multilineTextAlignment(.center)
          }
          .padding(32)
        }
      }
      .navigationTitle("Canlı")
      .navigationBarTitleDisplayMode(.inline)
    }
  }
}

struct BoxesOverlay: View {
  var boxes: [BridgeModel.DetBox]

  var body: some View {
    Canvas { ctx, size in
      for b in boxes {
        let r = CGRect(x: b.x1 * size.width, y: b.y1 * size.height,
                       width: (b.x2 - b.x1) * size.width, height: (b.y2 - b.y1) * size.height)
        let color = Color(css: b.color)
        ctx.stroke(Path(roundedRect: r, cornerRadius: b.small ? 3 : 6), with: .color(color), lineWidth: b.small ? 1 : 2)
        let text = Text(b.label).font(.system(size: b.small ? 9 : 11, weight: .semibold))
          .foregroundColor(b.small ? color : Color(hex: 0x0F1720))
        let resolved = ctx.resolve(text)
        let ts = resolved.measure(in: size)
        let tag = CGRect(x: r.minX, y: max(0, r.minY - ts.height - 6), width: ts.width + 8, height: ts.height + 4)
        ctx.fill(Path(roundedRect: tag, cornerRadius: 3),
                 with: .color(b.small ? Color(hex: 0x0F1720).opacity(0.8) : color))
        ctx.draw(resolved, at: CGPoint(x: tag.minX + 4, y: tag.minY + 2), anchor: .topLeading)
      }
    }
    .allowsHitTesting(false)
  }
}

/// Web ile ayni imza: goruntu kosesinde ikaz sarisi vizor isaretleri
struct ViewfinderCorners: View {
  var body: some View {
    GeometryReader { g in
      let l: CGFloat = 22
      Path { p in
        let w = g.size.width, h = g.size.height
        p.move(to: CGPoint(x: 0, y: l)); p.addLine(to: .zero); p.addLine(to: CGPoint(x: l, y: 0))
        p.move(to: CGPoint(x: w - l, y: 0)); p.addLine(to: CGPoint(x: w, y: 0)); p.addLine(to: CGPoint(x: w, y: l))
        p.move(to: CGPoint(x: 0, y: h - l)); p.addLine(to: CGPoint(x: 0, y: h)); p.addLine(to: CGPoint(x: l, y: h))
        p.move(to: CGPoint(x: w - l, y: h)); p.addLine(to: CGPoint(x: w, y: h)); p.addLine(to: CGPoint(x: w, y: h - l))
      }
      .stroke(Theme.amber, style: StrokeStyle(lineWidth: 2, lineCap: .round))
    }
    .padding(.horizontal, 10)
    .padding(.vertical, -6)
    .allowsHitTesting(false)
  }
}

// MARK: - Modeller

struct ModelsView: View {
  @Bindable var model: BridgeModel

  var body: some View {
    Page(title: "Modeller") {
      if model.models.isEmpty {
        Card {
          Text("Bilgisayara bağlanınca modeller burada listelenir.")
            .font(.subheadline).foregroundStyle(Theme.muted)
        }
      } else {
        Card(title: "Bilgisayardaki yapay zekâ modelleri") {
          ForEach(model.models) { m in
            Toggle(isOn: Binding(get: { m.on }, set: { model.setModel(m.key, on: $0) })) {
              VStack(alignment: .leading, spacing: 2) {
                Text(m.name).foregroundStyle(Theme.text).fontWeight(.semibold)
                Text(m.desc).font(.footnote).foregroundStyle(Theme.muted)
              }
            }
            .tint(Theme.ok)
            if m.id != model.models.last?.id { Divider().overlay(Theme.line) }
          }
        }
        Text("Kapattığın model bilgisayarda hiç çalışmaz. Ayar web paneliyle ortaktır.")
          .font(.footnote).foregroundStyle(Theme.muted)
          .frame(maxWidth: .infinity, alignment: .leading)
      }

      if !model.ppeItems.isEmpty {
        Card(title: "Zorunlu KKD") {
          FlowChips(items: model.ppeItems.map { ($0.key, $0.name) },
                    selected: Set(model.ppeRequired)) { key, on in model.setPPE(key, required: on) }
          Text("Seçilen ekipman eksikse gözlükten bir kez uyarı verilir.")
            .font(.footnote).foregroundStyle(Theme.muted)
        }
      }

      Card(title: "Nesne kütüphanesi") {
        Picker("Mod", selection: Binding(get: { model.objMode }, set: { model.setObjMode($0) })) {
          Text("Özel liste").tag("list")
          Text("Geniş katalog").tag("catalog")
        }
        .pickerStyle(.segmented)
        if model.objMode == "list" {
          AddField(placeholder: "forklift, yangın tüpü, ladder") { model.addObjects($0) }
          ForEach(model.objItems, id: \.self) { item in
            HStack {
              Button { model.toggleObjAlert(item) } label: {
                Image(systemName: model.objAlerts.contains(item) ? "bell.fill" : "bell.slash")
                  .foregroundStyle(model.objAlerts.contains(item) ? Theme.amber : Theme.muted)
              }
              .buttonStyle(.plain)
              Text(item).foregroundStyle(Theme.text)
              Spacer()
              Button { model.removeObject(item) } label: {
                Image(systemName: "xmark").font(.footnote).foregroundStyle(Theme.muted)
              }
              .buttonStyle(.plain)
            }
          }
          Text("Zili açık olan nesne görülünce sesli bildirilir.")
            .font(.footnote).foregroundStyle(Theme.muted)
        } else {
          Text("4.585 nesne türü yazmadan tanınır.").font(.footnote).foregroundStyle(Theme.muted)
        }
      }

      Card(title: "Aranan plakalar") {
        AddField(placeholder: "16 ABP 320") { model.addPlate($0) }
        ForEach(model.watchlist, id: \.self) { p in
          HStack {
            Text(p).fontWeight(.semibold).foregroundStyle(Theme.text)
            Spacer()
            Button { model.removePlate(p) } label: {
              Image(systemName: "xmark").font(.footnote).foregroundStyle(Theme.muted)
            }
            .buttonStyle(.plain)
          }
        }
        Text("Yalnızca bu plakalar kaydedilir.").font(.footnote).foregroundStyle(Theme.muted)
      }
    }
  }
}

/// Metin + Ekle dugmesi
struct AddField: View {
  var placeholder: String
  var onAdd: (String) -> Void
  @State private var text = ""

  var body: some View {
    HStack(spacing: 8) {
      TextField(placeholder, text: $text)
        .textInputAutocapitalization(.never)
        .autocorrectionDisabled()
        .padding(.horizontal, 12).padding(.vertical, 10)
        .background(Theme.raised, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
        .onSubmit(add)
      Button("Ekle", action: add)
        .fontWeight(.semibold)
        .padding(.horizontal, 14).padding(.vertical, 10)
        .background(Theme.raised, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
        .foregroundStyle(Theme.text)
    }
  }

  private func add() {
    let t = text.trimmingCharacters(in: .whitespaces)
    guard !t.isEmpty else { return }
    onAdd(t)
    text = ""
  }
}

/// Secilebilir etiketler (KKD)
struct FlowChips: View {
  var items: [(String, String)]
  var selected: Set<String>
  var onToggle: (String, Bool) -> Void

  var body: some View {
    LazyVGrid(columns: [GridItem(.adaptive(minimum: 100), spacing: 8)], alignment: .leading, spacing: 8) {
      ForEach(items, id: \.0) { key, name in
        let on = selected.contains(key)
        Button { onToggle(key, !on) } label: {
          HStack(spacing: 6) {
            Image(systemName: on ? "checkmark.circle.fill" : "circle")
              .foregroundStyle(on ? Theme.amber : Theme.muted)
            Text(name).lineLimit(1).minimumScaleFactor(0.8)
          }
          .font(.subheadline.weight(.medium))
          .foregroundStyle(Theme.text)
          .padding(.horizontal, 10).padding(.vertical, 8)
          .frame(maxWidth: .infinity, alignment: .leading)
          .background(on ? Theme.amber.opacity(0.12) : Theme.raised, in: Capsule())
          .overlay(Capsule().stroke(on ? Theme.amber.opacity(0.6) : Theme.line, lineWidth: 1))
        }
        .buttonStyle(.plain)
      }
    }
  }
}

// MARK: - Kayitlar

struct RecordingsView: View {
  @Bindable var model: BridgeModel
  @State private var playing: BridgeModel.RecFile?

  var body: some View {
    Page(title: "Kayıtlar") {
      Card(title: "Yeni kayıt") {
        Picker("Tür", selection: $model.recordAnnotated) {
          Text("Kutularla").tag(true)
          Text("Ham görüntü").tag(false)
        }
        .pickerStyle(.segmented)
        .disabled(model.recordingActive)

        Button { model.toggleRecord() } label: {
          Label(model.recordingActive ? "Kaydı durdur" : "Kaydı başlat",
                systemImage: model.recordingActive ? "stop.circle.fill" : "record.circle")
            .fontWeight(.semibold)
            .frame(maxWidth: .infinity)
            .padding(.vertical, 12)
            .foregroundStyle(model.recordingActive ? Theme.bad : Theme.text)
            .background(Theme.raised, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
        }
        .buttonStyle(.plain)
        .disabled(!model.socketConnected)
        Text("Kayıt bilgisayara yapılır. Kutulu kayıtta tarih, saat ve kişi sayısı da yazılır.")
          .font(.footnote).foregroundStyle(Theme.muted)
      }

      Card(title: "Kayıtlar") {
        if model.recordings.isEmpty {
          Text("Henüz kayıt yok.").font(.subheadline).foregroundStyle(Theme.muted)
        }
        ForEach(model.recordings) { f in
          Button { playing = f } label: {
            HStack {
              Image(systemName: "play.rectangle").foregroundStyle(Theme.amber)
              VStack(alignment: .leading, spacing: 2) {
                Text(Date(timeIntervalSince1970: f.time).formatted(date: .abbreviated, time: .shortened))
                  .foregroundStyle(Theme.text).fontWeight(.semibold)
                Text("\(f.annotated ? "Kutularla" : "Ham"), \(String(format: "%.1f", f.mb)) MB")
                  .font(.footnote).foregroundStyle(Theme.muted)
              }
              Spacer()
              Image(systemName: "chevron.right").font(.footnote).foregroundStyle(Theme.muted)
            }
            .contentShape(Rectangle())
          }
          .buttonStyle(.plain)
          if f.id != model.recordings.last?.id { Divider().overlay(Theme.line) }
        }
      }
    }
    .sheet(item: $playing) { f in
      if let url = model.recordingURL(f) {
        VideoPlayer(player: AVPlayer(url: url))
          .ignoresSafeArea()
          .presentationDragIndicator(.visible)
      }
    }
  }
}

// MARK: - Ayarlar

struct SettingsView: View {
  @Bindable var model: BridgeModel

  var body: some View {
    NavigationStack {
      Form {
        Section {
          LabeledContent("Meta AI kaydı", value: model.registrationText)
          LabeledContent("Gözlük", value: model.hasDevice ? "Bağlı" : "Bulunamadı")
          if model.isRegistered {
            Button("Kaydı kaldır", role: .destructive) { model.unregister() }.disabled(model.isRunning)
          } else {
            Button("Meta AI ile eşleştir") { model.register() }
          }
        } header: { Text("Gözlük") }

        Section {
          TextField("ws://desktop-adı:8000/ingest", text: $model.serverURL)
            .keyboardType(.URL)
            .textInputAutocapitalization(.never)
            .autocorrectionDisabled()
            .disabled(model.isRunning)
          LabeledContent("Durum", value: model.socketConnected ? "Bağlı" : "Bağlı değil")
        } header: {
          Text("Bilgisayar adresi")
        } footer: {
          Text("Her yerden bağlanmak için Tailscale açıkken bilgisayar adını kullan, örneğin ws://desktop-4fh0ap7:8000/ingest.")
        }

        Section {
          Picker("Çözünürlük", selection: $model.quality) {
            ForEach(StreamQuality.allCases) { Text($0.title).tag($0) }
          }
          Picker("Kare hızı", selection: $model.frameRate) {
            ForEach(model.frameRates, id: \.self) { Text("\($0) kare/sn").tag($0) }
          }
        } header: {
          Text("Görüntü")
        } footer: {
          Text("Mobil veride Düşük ve 15 kare/sn önerilir.")
        }
        .disabled(model.isRunning)

        Section {
          Picker("Sıkıştırma", selection: $model.jpegQuality) {
            ForEach(JpegQuality.allCases) { Text($0.title).tag($0) }
          }
          .pickerStyle(.segmented)
        } header: {
          Text("Görüntü kalitesi")
        } footer: {
          Text("Wi-Fi'da Yüksek, mobil veride Tasarruf seç. Yayın sırasında da değiştirilebilir.")
        }

        Section {
          Toggle("Anonsları sesli oku", isOn: $model.speechEnabled)
          Toggle("Sesli komutları dinle", isOn: Binding(get: { model.voiceEnabled }, set: { model.setVoice($0) }))
          Button("Sesi test et") { model.testSpeech() }
        } header: {
          Text("Ses")
        } footer: {
          Text("“Yapay zekâ katmanını başlat / kapat”, “Kaydı başlat / durdur”, “Claude, …”, “sus”, “yeni konu”.")
        }

        Section {
          Button(model.needsGlassesAppUpdate ? "Gözlük uygulamasını güncelle (gerekli)" : "Gözlük uygulamasını güncelle") {
            model.openGlassesAppUpdate()
          }
          Button("Gözlük yazılımını güncelle") { model.openFirmwareUpdate() }
        } header: { Text("Güncellemeler") }

        Section {
          LabeledContent("Oturum", value: String(describing: model.sessionState))
          LabeledContent("Akış", value: String(describing: model.streamState))
        } header: { Text("Tanılama") }
      }
      .scrollContentBackground(.hidden)
      .background(Theme.ink)
      .navigationTitle("Ayarlar")
    }
  }
}
