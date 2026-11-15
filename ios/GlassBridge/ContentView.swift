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
        ITView(model: model).tag(2)
          .tabItem { Label("BT", systemImage: "server.rack") }
        RecordingsView(model: model).tag(3)
          .tabItem { Label("Kayıtlar", systemImage: "film.stack") }
        NavigationStack { ModelsView(model: model) }.tag(4)
          .tabItem { Label("Modeller", systemImage: "cpu") }
      }
      .tint(Theme.amber)
      .onChange(of: tab) { _, t in model.liveActive = (t == 1) }

      // Ses cubugu: hangi sekmede olursan ol duyulani, durumu ve Durdur dugmesini gosterir
      if model.voiceEnabled || model.isSpeaking || model.assistantBusy {
        VStack {
          Spacer()
          VoiceBar(model: model)
            .padding(.horizontal, 12)
            .padding(.bottom, 58)
        }
        .zIndex(0.5)
      }

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
  var embed = true  // baska bir NavigationStack icinden aciliyorsa false
  @ViewBuilder var content: Content

  var body: some View {
    if embed {
      NavigationStack { scroll }
    } else {
      scroll
    }
  }

  private var scroll: some View {
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

// MARK: - Panel

struct PanelView: View {
  @Bindable var model: BridgeModel
  @State private var showSettings = false

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
        ControlRow(icon: "person.3.sequence", title: "Kişi sayımı",
                   detail: model.countActive
                     ? "Sürüyor · \(model.totalPeople) kişi" + (model.countLocation.map { " · " + $0 } ?? "")
                     : "Alanı say, bitince kaydedilir",
                   isOn: model.countActive, tint: Theme.amber) {
          model.toggleCount()
        }
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
    .overlay(alignment: .topTrailing) {
      Button { showSettings = true } label: {
        Image(systemName: "gearshape.fill")
          .font(.title3)
          .foregroundStyle(Theme.muted)
          .padding(12)
      }
      .accessibilityLabel("Ayarlar")
    }
    .sheet(isPresented: $showSettings) {
      SettingsView(model: model)
        .presentationDragIndicator(.visible)
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
        // Varsayilan: telefonun anlik goruntusu + son analizin kutulari (gecikmesiz).
        // "Kareye kilitle" aciksa PC'nin isledigi kare gosterilir (tam oturur, gecikmeli).
        if let img = (model.aiOn == true && model.lockBoxesToFrame ? model.syncedFrame : nil) ?? model.preview {
          VStack(spacing: 12) {
            if model.aiOn != true {
              Button { model.toggleAI() } label: {
                Label("Kutular için yapay zekâ katmanını aç", systemImage: "brain")
                  .font(.subheadline.weight(.semibold))
                  .padding(.horizontal, 14).padding(.vertical, 9)
                  .foregroundStyle(Color(hex: 0x1A1405))
                  .background(Theme.amber, in: Capsule())
              }
              .buttonStyle(.plain)
            }
            Image(uiImage: img)
              .resizable()
              .scaledToFit()
              .overlay { if model.aiOn == true { BoxesOverlay(boxes: model.detBoxes) } }
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

            Toggle(isOn: $model.lockBoxesToFrame) {
              Text("Kutuları kareye kilitle").font(.footnote).foregroundStyle(Theme.muted)
            }
            .tint(Theme.amber)
            .padding(.horizontal, 24)
            Text(model.lockBoxesToFrame
                 ? "Kutular tam oturur, görüntü mobil veride yarım saniye kadar geriden gelir."
                 : "Görüntü anlık; kutular son analizden gelir, hızlı harekette az kayabilir.")
              .font(.caption).foregroundStyle(Theme.muted.opacity(0.8))
              .multilineTextAlignment(.center).padding(.horizontal, 24)
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
    Page(title: "Modeller", embed: false) {
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
  @State private var section = "counts"
  @State private var playing: BridgeModel.RecFile?
  @State private var openSession: BridgeModel.CountSession?
  @State private var detail: MediaItem?
  @State private var pendingWipe: String?

  private var wipeTitle: String {
    ["sessions": "Tüm sayım oturumları silinsin mi?", "people": "Sayılan kişilerin fotoğrafları silinsin mi?",
     "vehicles": "Araç ve plaka kayıtları silinsin mi?", "videos": "Tüm videolar silinsin mi?",
     "it": "BT ve EKG kayıtları silinsin mi?", "qr": "QR kayıtları silinsin mi?"][pendingWipe ?? ""] ?? "Silinsin mi?"
  }

  private func clearButton(_ what: String, _ title: String) -> some View {
    Button(role: .destructive) { pendingWipe = what } label: {
      Label(title, systemImage: "trash")
        .font(.subheadline.weight(.semibold))
        .frame(maxWidth: .infinity).padding(.vertical, 10)
        .foregroundStyle(Theme.bad)
        .background(Theme.raised, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
    }
    .buttonStyle(.plain)
  }

  var body: some View {
    Page(title: "Kayıtlar") {
      Picker("Bölüm", selection: $section) {
        Text("Sayım").tag("counts")
        Text("Kişi").tag("people")
        Text("Araç").tag("cars")
        Text("Plaka").tag("plates")
        Text("Foto").tag("photos")
        Text("Video").tag("videos")
      }
      .pickerStyle(.segmented)

      switch section {
      case "people": peopleSection
      case "cars": carsSection
      case "plates": platesSection
      case "photos": photosSection
      case "videos": videosSection
      default: countsSection
      }
    }
    .confirmationDialog(wipeTitle, isPresented: Binding(get: { pendingWipe != nil }, set: { if !$0 { pendingWipe = nil } }),
                        titleVisibility: .visible) {
      Button("Sil", role: .destructive) {
        if let w = pendingWipe { model.wipe(w) }
        pendingWipe = nil
      }
      Button("Vazgeç", role: .cancel) { pendingWipe = nil }
    } message: {
      Text("Bilgisayardaki fotoğraflar ve kayıtlar kalıcı olarak silinir, geri alınamaz.")
    }
    .sheet(item: $openSession) { s in sessionSheet(s) }
    .sheet(item: $detail) { d in MediaDetailView(item: d, url: model.httpURL(d.path)) }
    .sheet(item: $playing) { f in
      if let url = model.recordingURL(f) {
        VStack(spacing: 12) {
          VideoPlayer(player: AVPlayer(url: url))
          SaveVideoButton(url: url).padding(.horizontal, 16).padding(.bottom, 16)
        }
        .background(Theme.ink)
        .presentationDragIndicator(.visible)
      }
    }
  }

  // MARK: Sayim oturumlari

  @ViewBuilder private var countsSection: some View {
    if model.countActive {
      Card(title: "Süren sayım") {
        HStack(alignment: .firstTextBaseline) {
          Text("\(model.totalPeople)").font(.system(size: 40, weight: .heavy)).foregroundStyle(Theme.amber)
          Text("kişi").foregroundStyle(Theme.muted)
          Spacer()
          if let since = model.countSince {
            Text(Date(timeIntervalSince1970: since), style: .timer).font(.headline).foregroundStyle(Theme.text)
          }
        }
        if let loc = model.countLocation { Label(loc, systemImage: "mappin").font(.footnote).foregroundStyle(Theme.muted) }
        Button { model.toggleCount() } label: {
          Label("Sayımı bitir ve kaydet", systemImage: "stop.circle.fill")
            .fontWeight(.semibold).frame(maxWidth: .infinity).padding(.vertical, 10)
            .foregroundStyle(Theme.bad)
            .background(Theme.raised, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
        }
        .buttonStyle(.plain)
      }
    }
    Card(title: "Sayım oturumları") {
      if model.countSessions.isEmpty {
        Text("“Kişi saymayı başlat / bitir” ile yapılan sayımlar burada: kaç kişi, ne kadar sürdü, nerede, kimler.")
          .font(.subheadline).foregroundStyle(Theme.muted)
      }
      ForEach(model.countSessions) { s in
        Button { openSession = s } label: {
          HStack(spacing: 12) {
            VStack(spacing: 0) {
              Text("\(s.total)").font(.title2.weight(.heavy)).foregroundStyle(Theme.amber)
              Text("kişi").font(.caption2).foregroundStyle(Theme.muted)
            }
            .frame(minWidth: 48)
            VStack(alignment: .leading, spacing: 2) {
              Text(Date(timeIntervalSince1970: s.start).formatted(date: .abbreviated, time: .shortened))
                .fontWeight(.semibold).foregroundStyle(Theme.text)
              Text(String(format: "%.1f dakika", s.minutes) + ", \(s.faces.count) fotoğraf")
                .font(.footnote).foregroundStyle(Theme.muted)
              if let l = s.location { Label(l, systemImage: "mappin").font(.caption).foregroundStyle(Theme.muted).lineLimit(1) }
              if !s.ppeText.isEmpty {
                Text(s.ppeText).font(.footnote.weight(.semibold)).foregroundStyle(Theme.amber)
              }
            }
            Spacer()
            Image(systemName: "chevron.right").font(.footnote).foregroundStyle(Theme.muted)
          }
          .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        if s.id != model.countSessions.last?.id { Divider().overlay(Theme.line) }
      }
    }
    if !model.countSessions.isEmpty { clearButton("sessions", "Sayım geçmişini sil") }
  }

  private func sessionSheet(_ s: BridgeModel.CountSession) -> some View {
    ScrollView {
      VStack(alignment: .leading, spacing: 12) {
        Text("\(s.total) kişi").font(.largeTitle.weight(.heavy)).foregroundStyle(Theme.text)
        Text(Date(timeIntervalSince1970: s.start).formatted(date: .complete, time: .shortened))
          .font(.subheadline).foregroundStyle(Theme.muted)
        Text(String(format: "Süre: %.1f dakika", s.minutes)).font(.subheadline).foregroundStyle(Theme.muted)
        if let l = s.location { Label(l, systemImage: "mappin").font(.subheadline).foregroundStyle(Theme.muted) }
        if !s.ppeText.isEmpty { Text(s.ppeText).font(.headline).foregroundStyle(Theme.amber) }
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 96), spacing: 10)], spacing: 10) {
          ForEach(s.faces) { f in
            Button {
              openSession = nil
              detail = MediaItem(path: "/faces/" + f.file, title: "Kişi \(f.id)",
                                 lines: [f.time > 0 ? Self.when(f.time) : nil, s.location,
                                         f.face ? "Yüz fotoğrafı" : "Yüzü net görülemedi, gövde fotoğrafı"])
            } label: {
              PhotoTile(url: model.httpURL("/faces/" + f.file), caption: "Kişi \(f.id)", size: 96)
            }
            .buttonStyle(.plain)
          }
        }
        if s.faces.isEmpty {
          Text("Bu sayımda fotoğraf yok.").font(.subheadline).foregroundStyle(Theme.muted)
        }
      }
      .padding(16)
    }
    .background(Theme.ink)
    .presentationDragIndicator(.visible)
  }

  // MARK: Kisiler

  @ViewBuilder private var peopleSection: some View {
    Card(title: "Sayılan kişiler") {
      if model.faces.isEmpty {
        Text("Sayılan her kişi fotoğrafı, saati ve yeriyle burada görünür.").font(.subheadline).foregroundStyle(Theme.muted)
      } else {
        Text("\(model.faces.count) kişi").font(.footnote).foregroundStyle(Theme.muted)
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 96), spacing: 10)], spacing: 12) {
          ForEach(model.faces) { f in
            Button {
              detail = MediaItem(path: "/faces/" + f.file, title: "Kişi \(f.id)",
                                 lines: [Self.when(f.time), f.place,
                                         f.face ? "Yüz fotoğrafı" : "Yüzü net görülemedi, gövde fotoğrafı"])
            } label: {
              PhotoTile(url: model.httpURL("/faces/" + f.file), caption: "Kişi \(f.id), " + Self.hour(f.time), size: 96)
            }
            .buttonStyle(.plain)
          }
        }
      }
    }
    if !model.faces.isEmpty { clearButton("people", "Kişi fotoğraflarını sil") }
  }

  // MARK: Araclar ve plakalar

  @ViewBuilder private var carsSection: some View {
    Card(title: "Görülen araçlar") {
      if model.vehicleLog.isEmpty {
        Text("Birkaç saniye bakılan her araç; türü, rengi, saati, yeri ve fotoğrafıyla burada.")
          .font(.subheadline).foregroundStyle(Theme.muted)
      }
      ForEach(model.vehicleLog.prefix(60)) { v in
        Button {
          detail = MediaItem(path: v.image ?? "", title: v.plate ?? "Plaka okunamadı",
                             lines: [v.title, Self.when(v.time), v.place])
        } label: {
          HStack(spacing: 12) {
            AsyncImage(url: v.image.flatMap { model.httpURL($0) }) { $0.resizable().scaledToFill() } placeholder: { Theme.raised }
              .frame(width: 64, height: 48)
              .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
            VStack(alignment: .leading, spacing: 2) {
              Text(v.plate ?? "Plaka okunamadı").fontWeight(.semibold).foregroundStyle(Theme.text)
              Text(v.title).font(.footnote).foregroundStyle(Theme.muted)
              if let p = v.place { Label(p, systemImage: "mappin").font(.caption).foregroundStyle(Theme.muted).lineLimit(1) }
            }
            Spacer()
            Text(Date(timeIntervalSince1970: v.time).formatted(date: .abbreviated, time: .shortened))
              .font(.caption).foregroundStyle(Theme.muted)
          }
          .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
      }
    }
    if !model.vehicleLog.isEmpty || !model.plateLog.isEmpty { clearButton("vehicles", "Araç ve plaka kayıtlarını sil") }
  }

  // MARK: Plakalar

  @ViewBuilder private var platesSection: some View {
    Card(title: "Okunan plakalar") {
      if model.plateLog.isEmpty {
        Text("Okunan her plaka; saati, yeri, varsa araç türü ve fotoğrafıyla burada. Aynı plaka 10 dakikada bir yazılır.")
          .font(.subheadline).foregroundStyle(Theme.muted)
      }
      ForEach(model.plateLog.prefix(80)) { v in
        Button {
          detail = MediaItem(path: v.image ?? "", title: v.plate ?? "Plaka",
                             lines: [v.type.isEmpty ? nil : v.title, Self.when(v.time), v.place])
        } label: {
          HStack(spacing: 12) {
            AsyncImage(url: v.image.flatMap { model.httpURL($0) }) { $0.resizable().scaledToFill() } placeholder: { Theme.raised }
              .frame(width: 72, height: 48)
              .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
            VStack(alignment: .leading, spacing: 2) {
              Text(v.plate ?? "Plaka").font(.headline.monospaced()).foregroundStyle(Theme.text)
              if !v.type.isEmpty { Text(v.title).font(.footnote).foregroundStyle(Theme.muted) }
              if let p = v.place { Label(p, systemImage: "mappin").font(.caption).foregroundStyle(Theme.muted).lineLimit(1) }
            }
            Spacer()
            Text(Date(timeIntervalSince1970: v.time).formatted(date: .abbreviated, time: .shortened))
              .font(.caption).foregroundStyle(Theme.muted)
          }
          .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
      }
    }
  }

  // MARK: Tum fotograflar

  private var allPhotos: [MediaItem] {
    var items: [MediaItem] = []
    items += model.faces.map {
      MediaItem(path: "/faces/" + $0.file, title: "Kişi \($0.id)", lines: [Self.when($0.time), $0.place],
                time: $0.time, kind: "Kişi")
    }
    items += model.vehicleLog.compactMap { v in
      v.image.map {
        MediaItem(path: $0, title: v.plate ?? v.title, lines: [v.title, Self.when(v.time), v.place], time: v.time, kind: "Araç")
      }
    }
    items += model.plateLog.compactMap { v in
      v.image.map {
        MediaItem(path: $0, title: v.plate ?? "Plaka", lines: [Self.when(v.time), v.place], time: v.time, kind: "Plaka")
      }
    }
    items += model.itHistory.map { r in
      MediaItem(path: r.image, title: ["hw": "Donanım taraması", "error": "Hata", "ecg": "EKG"][r.kind] ?? "BT",
                lines: [r.summary, Self.when(r.time), r.place], time: r.time, kind: r.kind == "ecg" ? "EKG" : "BT")
    }
    items += model.qrHistory.compactMap { q in
      q.image.map {
        MediaItem(path: $0, title: q.name ?? q.text, lines: [q.text, Self.when(q.time), q.where_], time: q.time, kind: "QR")
      }
    }
    return items.sorted { $0.time > $1.time }
  }

  @ViewBuilder private var photosSection: some View {
    let photos = allPhotos
    Card(title: "Tüm fotoğraflar") {
      if photos.isEmpty {
        Text("Kişi, araç, BT, EKG ve QR fotoğrafları burada toplanır.").font(.subheadline).foregroundStyle(Theme.muted)
      } else {
        Text("\(photos.count) fotoğraf. Dokununca büyür, telefona kaydedebilirsin.").font(.footnote).foregroundStyle(Theme.muted)
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 100), spacing: 8)], spacing: 10) {
          ForEach(photos) { p in
            Button { detail = p } label: {
              PhotoTile(url: model.httpURL(p.path), caption: p.kind + ", " + Self.hour(p.time), size: 100)
            }
            .buttonStyle(.plain)
          }
        }
      }
    }
  }

  // MARK: Videolar

  @ViewBuilder private var videosSection: some View {
    Card(title: "Yeni video kaydı") {
      Picker("Tür", selection: $model.recordAnnotated) {
        Text("Kutularla").tag(true)
        Text("Ham görüntü").tag(false)
      }
      .pickerStyle(.segmented)
      .disabled(model.recordingActive)
      Button { model.toggleRecord() } label: {
        Label(model.recordingActive ? "Kaydı durdur" : "Kaydı başlat",
              systemImage: model.recordingActive ? "stop.circle.fill" : "record.circle")
          .fontWeight(.semibold).frame(maxWidth: .infinity).padding(.vertical, 12)
          .foregroundStyle(model.recordingActive ? Theme.bad : Theme.text)
          .background(Theme.raised, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
      }
      .buttonStyle(.plain)
      .disabled(!model.socketConnected)
      Text("Sesli: “kaydı başlat / durdur”. Video bilgisayara kaydedilir; buradan izleyip telefona indirebilirsin.")
        .font(.footnote).foregroundStyle(Theme.muted)
    }
    Card(title: "Videolar") {
      if model.recordings.isEmpty {
        Text("Henüz video yok.").font(.subheadline).foregroundStyle(Theme.muted)
      }
      ForEach(model.recordings) { f in
        Button { playing = f } label: {
          HStack {
            Image(systemName: "play.rectangle.fill").font(.title2).foregroundStyle(Theme.amber)
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
    if !model.recordings.isEmpty { clearButton("videos", "Videoları sil") }
  }

  static func when(_ t: Double) -> String {
    Date(timeIntervalSince1970: t).formatted(date: .abbreviated, time: .shortened)
  }

  static func hour(_ t: Double) -> String {
    Date(timeIntervalSince1970: t).formatted(date: .omitted, time: .shortened)
  }
}

/// Kayitlardaki bir fotograf: buyuk gosterim + bilgiler
struct MediaItem: Identifiable {
  let path: String
  let title: String
  var lines: [String?] = []
  var time: Double = 0
  var kind: String = ""
  var id: String { path + title }
}

struct PhotoTile: View {
  var url: URL?
  var caption: String
  var size: CGFloat

  var body: some View {
    VStack(spacing: 4) {
      AsyncImage(url: url) { $0.resizable().scaledToFill() } placeholder: { Theme.raised }
        .frame(width: size, height: size)
        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
      Text(caption).font(.caption).foregroundStyle(Theme.muted).lineLimit(1)
    }
  }
}

struct MediaDetailView: View {
  var item: MediaItem
  var url: URL?

  var body: some View {
    ScrollView {
      VStack(alignment: .leading, spacing: 10) {
        AsyncImage(url: url) { $0.resizable().scaledToFit() } placeholder: {
          Theme.raised.frame(height: 240)
        }
        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
        Text(item.title).font(.title2.weight(.bold)).foregroundStyle(Theme.text)
        ForEach(Array(item.lines.compactMap { $0 }.filter { !$0.isEmpty }.enumerated()), id: \.offset) { _, l in
          Text(l).font(.subheadline).foregroundStyle(Theme.muted)
        }
        if let url { SaveImageButton(url: url, title: item.title) }
      }
      .padding(16)
    }
    .background(Theme.ink)
    .presentationDragIndicator(.visible)
  }
}

/// Fotografi PC'den indirip telefona kaydet / paylas
struct SaveImageButton: View {
  var url: URL
  var title: String
  @State private var image: UIImage?

  var body: some View {
    Group {
      if let image {
        ShareLink(item: Image(uiImage: image),
                  preview: SharePreview(title.isEmpty ? "Fotoğraf" : title, image: Image(uiImage: image))) {
          Label("Telefona kaydet / paylaş", systemImage: "square.and.arrow.down")
            .fontWeight(.semibold).frame(maxWidth: .infinity).padding(.vertical, 12)
            .foregroundStyle(Theme.text)
            .background(Theme.raised, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
        }
      } else {
        ProgressView().frame(maxWidth: .infinity)
      }
    }
    .task(id: url) {
      if let r = try? await URLSession.shared.data(from: url) { image = UIImage(data: r.0) }
    }
  }
}

/// Videoyu PC'den indir, sonra Fotograflar'a kaydet / paylas
struct SaveVideoButton: View {
  var url: URL
  @State private var file: URL?
  @State private var loading = false

  var body: some View {
    if let file {
      ShareLink(item: file) {
        Label("Telefona kaydet / paylaş", systemImage: "square.and.arrow.down")
          .fontWeight(.semibold).frame(maxWidth: .infinity).padding(.vertical, 12)
          .foregroundStyle(Theme.text)
          .background(Theme.raised, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
      }
    } else {
      Button {
        loading = true
        Task {
          file = await download()
          loading = false
        }
      } label: {
        Label(loading ? "İndiriliyor…" : "Telefona indir", systemImage: "arrow.down.circle")
          .fontWeight(.semibold).frame(maxWidth: .infinity).padding(.vertical, 12)
          .foregroundStyle(Theme.text)
          .background(Theme.raised, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
      }
      .buttonStyle(.plain)
      .disabled(loading)
    }
  }

  private func download() async -> URL? {
    guard let r = try? await URLSession.shared.download(from: url) else { return nil }
    let dest = FileManager.default.temporaryDirectory.appendingPathComponent(url.lastPathComponent)
    try? FileManager.default.removeItem(at: dest)
    do { try FileManager.default.moveItem(at: r.0, to: dest) } catch { return nil }
    return dest
  }
}

// MARK: - Ayarlar

struct SettingsView: View {
  @Bindable var model: BridgeModel
  @State private var confirmWipeAll = false

  var body: some View {
    NavigationStack {
      Form {
        Section {
          NavigationLink {
            ModelsView(model: model)
          } label: {
            Label("Yapay zekâ modelleri ve filtreler", systemImage: "cpu")
          }
        } footer: {
          Text("Modelleri aç/kapat, zorunlu KKD, nesne listesi, aranan plakalar.")
        }

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
          ForEach(StreamProfile.allCases) { p in
            Button { model.profile = p } label: {
              HStack {
                VStack(alignment: .leading, spacing: 2) {
                  Text(p.title).foregroundStyle(Theme.text).fontWeight(.semibold)
                  Text(p.detail).font(.footnote).foregroundStyle(Theme.muted)
                }
                Spacer()
                if model.profile == p {
                  Image(systemName: "checkmark").foregroundStyle(Theme.amber).fontWeight(.semibold)
                }
              }
              .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
          }
        } header: {
          Text("Yayın profili")
        } footer: {
          Text(model.profile == .auto
               ? "Şu an \(model.onWiFi ? "Wi-Fi" : "mobil veri") algılandı. Çözünürlük değişikliği yayın yeniden başlayınca geçerli olur."
               : "Çözünürlük ve kare hızı değişikliği yayın yeniden başlayınca geçerli olur.")
        }

        Section {
          Picker("Çözünürlük", selection: Binding(get: { model.quality }, set: { model.profile = nil; model.quality = $0 })) {
            ForEach(StreamQuality.allCases) { Text($0.title).tag($0) }
          }
          .disabled(model.isRunning)
          Picker("Kare hızı", selection: Binding(get: { model.frameRate }, set: { model.profile = nil; model.frameRate = $0 })) {
            ForEach(model.frameRates, id: \.self) { Text("\($0) kare/sn").tag($0) }
          }
          .disabled(model.isRunning)
          Picker("Sıkıştırma", selection: Binding(get: { model.jpegQuality }, set: { model.profile = nil; model.jpegQuality = $0 })) {
            ForEach(JpegQuality.allCases) { Text($0.title).tag($0) }
          }
        } header: {
          Text("Özel ayar")
        } footer: {
          Text("Buradan değiştirirsen profil seçimi kalkar.")
        }

        Section {
          Toggle("Anonsları sesli oku", isOn: $model.speechEnabled)
          Toggle("Sesli komutları dinle", isOn: Binding(get: { model.voiceEnabled }, set: { model.setVoice($0) }))
          Button("Sesi test et") { model.testSpeech() }
        } header: {
          Text("Ses")
        } footer: {
          Text("Komutlar: “yapay zekâ katmanını başlat / kapat”, “kaydı başlat / durdur”, “kişi saymayı başlat / bitir”, “tara”, “bu hata ne”. Claude için cümleye “Claude” ya da “Asistan” diye başla; cevaptan sonra 12 sn devam sorusu sorabilirsin. Diğer konuşmalar yok sayılır.")
        }

        Section {
          Button(model.needsGlassesAppUpdate ? "Gözlük uygulamasını güncelle (gerekli)" : "Gözlük uygulamasını güncelle") {
            model.openGlassesAppUpdate()
          }
          Button("Gözlük yazılımını güncelle") { model.openFirmwareUpdate() }
        } header: { Text("Güncellemeler") }

        Section {
          Button("Tüm verileri sil", role: .destructive) { confirmWipeAll = true }
        } header: {
          Text("Veriler")
        } footer: {
          Text("Kişi fotoğrafları, araç ve plaka kayıtları, BT, EKG ve QR kayıtları, sayımlar ve videolar bilgisayardan silinir. Ayarlar, aranan plakalar ve envanter kalır.")
        }
        .confirmationDialog("Tüm veriler silinsin mi?", isPresented: $confirmWipeAll, titleVisibility: .visible) {
          Button("Hepsini sil", role: .destructive) { model.wipe("all") }
          Button("Vazgeç", role: .cancel) {}
        } message: {
          Text("Bu işlem geri alınamaz. Süren sayım da iptal edilir.")
        }

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


/// Her sekmenin altinda: dinleme durumu, son duyulan cumle, Claude'un durumu ve Durdur dugmesi
struct VoiceBar: View {
  @Bindable var model: BridgeModel

  private var status: String {
    if model.isSpeaking { return "Konuşuyor… araya girmek için “dur” de" }
    if model.assistantBusy { return "Claude düşünüyor…" }
    if let h = model.voiceHint { return h }
    if let heard = model.lastHeard, !heard.isEmpty { return "“\(heard)”" }
    return model.voiceListening ? "Dinliyorum" : "Ses kapalı"
  }

  var body: some View {
    HStack(spacing: 10) {
      Image(systemName: model.isSpeaking ? "speaker.wave.2.fill" : model.voiceListening ? "mic.fill" : "mic.slash")
        .foregroundStyle(model.isSpeaking || model.assistantBusy ? Theme.amber : Theme.muted)
        .frame(width: 22)
      Text(status)
        .font(.footnote)
        .foregroundStyle(model.voiceHint != nil && !model.isSpeaking ? Theme.amber : Theme.text)
        .lineLimit(2)
        .frame(maxWidth: .infinity, alignment: .leading)
      if model.isSpeaking || model.assistantBusy {
        Button { model.stopTalking() } label: {
          Label("Durdur", systemImage: "stop.fill")
            .font(.footnote.weight(.bold))
            .padding(.horizontal, 12).padding(.vertical, 7)
            .foregroundStyle(Theme.ink)
            .background(Theme.amber, in: Capsule())
        }
        .buttonStyle(.plain)
      }
    }
    .padding(.horizontal, 12).padding(.vertical, 9)
    .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
    .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous).stroke(Theme.line))
  }
}
