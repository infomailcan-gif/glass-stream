import SwiftUI

/// Bilgi Islem sekmesi: donanim taramalari, hata analizleri, QR envanter karti.
struct ITView: View {
  @Bindable var model: BridgeModel
  @State private var open: BridgeModel.ITRecord?

  var body: some View {
    Page(title: "Bilgi İşlem") {
      Card {
        ControlRow(icon: "server.rack", title: "Bilgi işlem modu",
                   detail: model.itActive ? "Cihaza bir saniye sabit bakınca tarar" : "Kapalı",
                   isOn: model.itActive, tint: Theme.steel) { model.setIT(!model.itActive) }
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
        Text("Sesli: “bilgi işlem modunu başlat”, “tara”, “bu hata ne”, “bilgisayarımdaki hata ne”.")
          .font(.footnote).foregroundStyle(Theme.muted)
      }

      if let qr = model.itQR {
        Card(title: "QR") {
          Text(qr.0).font(.headline).foregroundStyle(Theme.text)
          if let item = qr.1 {
            ForEach(["ad", "konum", "ip", "not"], id: \.self) { k in
              if let v = item[k] as? String, !v.isEmpty {
                LabeledContent(k.capitalized, value: v).font(.subheadline)
              }
            }
          } else {
            Text("Envanterde kayıt yoksa sadece kod gösterilir.").font(.footnote).foregroundStyle(Theme.muted)
          }
        }
      }

      if let r = model.itCurrent {
        ITCard(record: r, imageURL: model.httpURL(r.image))
      }

      Card(title: "Geçmiş") {
        if model.itHistory.isEmpty {
          Text("Henüz tarama yok.").font(.subheadline).foregroundStyle(Theme.muted)
        }
        ForEach(model.itHistory) { r in
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
                  Text(Date(timeIntervalSince1970: r.time).formatted(date: .omitted, time: .shortened))
                    .font(.caption).foregroundStyle(Theme.muted)
                }
                Text(r.summary).font(.footnote).foregroundStyle(Theme.muted).lineLimit(2)
              }
            }
            .contentShape(Rectangle())
          }
          .buttonStyle(.plain)
          if r.id != model.itHistory.last?.id { Divider().overlay(Theme.line) }
        }
      }
    }
    .sheet(item: $open) { r in
      ScrollView { ITCard(record: r, imageURL: model.httpURL(r.image)).padding(16) }
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
        Text(record.done ? (record.seconds.map { String(format: "%.1f sn", $0) } ?? "") : "inceleniyor…")
          .font(.caption).foregroundStyle(Theme.muted)
      }
      AsyncImage(url: imageURL) { $0.resizable().scaledToFit() } placeholder: {
        Theme.raised.frame(height: 160)
      }
      .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
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
  var body: some View {
    Text(kind == "hw" ? "Donanım" : "Hata")
      .font(.caption.weight(.bold))
      .padding(.horizontal, 8).padding(.vertical, 3)
      .foregroundStyle(kind == "hw" ? Theme.steel : Theme.bad)
      .background((kind == "hw" ? Theme.steel : Theme.bad).opacity(0.16), in: Capsule())
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
