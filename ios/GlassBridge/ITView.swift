import SwiftUI

/// BT sekmesi: bilgi islem (tarama, hata) ve EKG ayri bolumler; gecmis ture gore filtrelenir.
struct ITView: View {
  @Bindable var model: BridgeModel
  @State private var open: BridgeModel.ITRecord?
  @State private var filter = "all"

  private var filtered: [BridgeModel.ITRecord] {
    filter == "all" ? model.itHistory : model.itHistory.filter { $0.kind == filter }
  }

  var body: some View {
    Page(title: "BT ve EKG") {
      Card(title: "Bilgi işlem") {
        ControlRow(icon: "server.rack", title: "Bilgi işlem modu",
                   detail: model.itActive ? "Açık · tara de ya da düğmeye bas" : "Kapalı",
                   isOn: model.itActive, tint: Theme.steel) { model.setIT(!model.itActive) }
        if model.itActive {
          Toggle(isOn: Binding(get: { model.itAuto }, set: { model.setITAuto($0) })) {
            VStack(alignment: .leading, spacing: 2) {
              Text("Bakınca kendiliğinden tara").foregroundStyle(Theme.text)
              Text("Açıksa cihaza 1 sn sabit bakınca fotoğraf çekip tarar.")
                .font(.footnote).foregroundStyle(Theme.muted)
            }
          }
          .tint(Theme.steel)
        }
        HStack(spacing: 8) {
          ActionChip(title: "Tara", icon: "viewfinder") {
            Task { await model.sendITScan() }
          }
          ActionChip(title: "Bu hata ne", icon: "exclamationmark.triangle") {
            model.itError(pc: false)
          }
          ActionChip(title: "PC hatası", icon: "desktopcomputer") {
            model.itError(pc: true)
          }
        }
        Text("Sesli: “tara”, “bu hata ne”, “bilgisayarımdaki hata ne”. Tara ve hata komutları yalnızca bunları söyleyince fotoğraf çeker.")
          .font(.footnote).foregroundStyle(Theme.muted)
      }

      Card(title: "EKG") {
        Button { model.sendECG() } label: {
          Label("EKG'yi yorumla", systemImage: "waveform.path.ecg")
            .fontWeight(.semibold).frame(maxWidth: .infinity).padding(.vertical, 12)
            .foregroundStyle(Theme.text)
            .background(Theme.raised, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
        }
        .buttonStyle(.plain)
        Text("Kâğıda düz ve yakından bak. Sesli: “bu EKG'yi yorumla”. Önce kısa sesli özet, ardından ayrıntılı yorum gelir. Tıbbi tanı yerine geçmez.")
          .font(.footnote).foregroundStyle(Theme.muted)
      }

      if let r = model.itCurrent {
        ITCard(record: r, imageURL: model.httpURL(r.image))
      }

      if let qr = model.itQR {
        Card(title: "Son okunan QR") {
          Text(qr.0).font(.headline).foregroundStyle(Theme.text)
          if let item = qr.1 {
            ForEach(["ad", "konum", "ip", "not"], id: \.self) { k in
              if let v = item[k] as? String, !v.isEmpty {
                LabeledContent(k.capitalized, value: v).font(.subheadline)
              }
            }
          }
        }
      }

      Card(title: "Geçmiş") {
        Picker("Tür", selection: $filter) {
          Text("Tümü").tag("all")
          Text("Donanım").tag("hw")
          Text("Hata").tag("error")
          Text("EKG").tag("ecg")
        }
        .pickerStyle(.segmented)
        if filtered.isEmpty {
          Text("Bu türde kayıt yok.").font(.subheadline).foregroundStyle(Theme.muted)
        }
        ForEach(filtered) { r in
          Button { open = r } label: {
            HStack(spacing: 12) {
              AsyncImage(url: model.httpURL(r.image)) { $0.resizable().scaledToFill() } placeholder: { Theme.raised }
                .frame(width: 56, height: 56)
                .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
              VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                  KindBadge(kind: r.kind)
                  if let q = r.qr { Text(q).font(.caption.weight(.semibold)).foregroundStyle(Theme.text) }
                  Spacer()
                  Text(Date(timeIntervalSince1970: r.time).formatted(date: .abbreviated, time: .shortened))
                    .font(.caption).foregroundStyle(Theme.muted)
                }
                Text(r.summary).font(.footnote).foregroundStyle(Theme.muted).lineLimit(2)
                if let p = r.place { Label(p, systemImage: "mappin").font(.caption).foregroundStyle(Theme.muted) }
              }
            }
            .contentShape(Rectangle())
          }
          .buttonStyle(.plain)
          if r.id != filtered.last?.id { Divider().overlay(Theme.line) }
        }
      }

      Card(title: "Okunan QR kodlar") {
        if model.qrHistory.isEmpty {
          Text("Okunan QR kodlar saat, yer ve fotoğrafıyla burada listelenir.")
            .font(.subheadline).foregroundStyle(Theme.muted)
        }
        ForEach(model.qrHistory.prefix(30)) { q in
          HStack(spacing: 12) {
            AsyncImage(url: q.image.flatMap { model.httpURL($0) }) { $0.resizable().scaledToFill() } placeholder: { Theme.raised }
              .frame(width: 52, height: 52)
              .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
            VStack(alignment: .leading, spacing: 2) {
              Text(q.name ?? q.text).fontWeight(.semibold).foregroundStyle(Theme.text).lineLimit(2)
              if q.name != nil {
                Text([q.place, q.ip.map { "IP " + $0 }].compactMap { $0 }.joined(separator: " · "))
                  .font(.footnote).foregroundStyle(Theme.muted)
              }
              Text([Date(timeIntervalSince1970: q.time).formatted(date: .abbreviated, time: .shortened), q.where_]
                   .compactMap { $0 }.joined(separator: " · "))
                .font(.caption).foregroundStyle(Theme.muted)
            }
            Spacer()
          }
        }
      }
    }
    .sheet(item: $open) { r in
      ScrollView {
        VStack(alignment: .leading, spacing: 10) {
          ITCard(record: r, imageURL: model.httpURL(r.image))
          if let url = model.httpURL(r.image) { SaveImageButton(url: url, title: r.summary) }
        }
        .padding(16)
      }
      .background(Theme.ink)
      .presentationDragIndicator(.visible)
    }
  }
}

struct ITCard: View {
  var record: BridgeModel.ITRecord
  var imageURL: URL?

  var body: some View {
    Card {
      HStack(spacing: 8) {
        KindBadge(kind: record.kind)
        if let q = record.qr { Text(q).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.text) }
        Spacer()
        Text(record.done
             ? [record.model, record.seconds.map { String(format: "%.1f sn", $0) }].compactMap { $0 }.joined(separator: ", ")
             : "inceleniyor…")
          .font(.caption).foregroundStyle(Theme.muted)
      }
      AsyncImage(url: imageURL) { $0.resizable().scaledToFit() } placeholder: {
        Theme.raised.frame(height: 160)
      }
      .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
      if let u = record.urgent, !u.isEmpty {
        Label("ACİL: \(u)", systemImage: "exclamationmark.octagon.fill")
          .font(.subheadline.weight(.bold))
          .foregroundStyle(Color(hex: 0xFFD2CB))
          .padding(10)
          .frame(maxWidth: .infinity, alignment: .leading)
          .background(Theme.bad.opacity(0.18), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
      }
      if !record.summary.isEmpty {
        Text(record.summary).font(.subheadline.weight(.bold)).foregroundStyle(Theme.text)
      }
      ITBody(text: record.body)
    }
  }
}

/// ``` bloklarini kod kutusu, gerisini duz metin olarak gosterir
struct ITBody: View {
  var text: String
  var body: some View {
    let parts = text.components(separatedBy: "```")
    VStack(alignment: .leading, spacing: 8) {
      ForEach(Array(parts.enumerated()), id: \.offset) { i, part in
        if i % 2 == 1 {
          let code = part.replacingOccurrences(of: #"^[a-zA-Z0-9]*\n"#, with: "", options: .regularExpression)
          Text(code)
            .font(.system(.footnote, design: .monospaced))
            .foregroundStyle(Theme.text)
            .padding(8)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Theme.ink, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
            .textSelection(.enabled)
        } else if !part.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
          Text(part.trimmingCharacters(in: .whitespacesAndNewlines))
            .font(.subheadline).foregroundStyle(Theme.text.opacity(0.92))
            .textSelection(.enabled)
        }
      }
    }
  }
}

struct KindBadge: View {
  var kind: String
  private var title: String { ["hw": "Donanım", "error": "Hata", "ecg": "EKG"][kind] ?? kind }
  private var color: Color { kind == "hw" ? Theme.steel : kind == "ecg" ? Theme.amber : Theme.bad }
  var body: some View {
    Text(title)
      .font(.caption.weight(.bold))
      .padding(.horizontal, 8).padding(.vertical, 3)
      .foregroundStyle(color)
      .background(color.opacity(0.16), in: Capsule())
  }
}

struct ActionChip: View {
  var title: String
  var icon: String
  var action: () -> Void
  var body: some View {
    Button(action: action) {
      VStack(spacing: 4) {
        Image(systemName: icon).font(.body.weight(.semibold))
        Text(title).font(.caption.weight(.semibold))
      }
      .frame(maxWidth: .infinity)
      .padding(.vertical, 10)
      .foregroundStyle(Theme.text)
      .background(Theme.raised, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
    }
    .buttonStyle(.plain)
  }
}
