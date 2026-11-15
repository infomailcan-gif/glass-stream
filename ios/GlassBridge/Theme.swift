import SwiftUI

/// Canberk Sikli AI Systems - web ile ayni palet: arduvaz zemin, ISG ikaz sarisi tek vurgu.
enum Theme {
  static let ink = Color(hex: 0x0F1720)
  static let surface = Color(hex: 0x16212C)
  static let raised = Color(hex: 0x1C2A37)
  static let line = Color(hex: 0x26384A)
  static let text = Color(hex: 0xE6EDF3)
  static let muted = Color(hex: 0x8CA0B3)
  static let amber = Color(hex: 0xF2B33D)
  static let ok = Color(hex: 0x4FB286)
  static let bad = Color(hex: 0xE5604D)
  static let steel = Color(hex: 0x6FA8DC)
}

extension Color {
  init(hex: UInt32) {
    self.init(
      red: Double((hex >> 16) & 0xFF) / 255,
      green: Double((hex >> 8) & 0xFF) / 255,
      blue: Double(hex & 0xFF) / 255)
  }

  /// "#RRGGBB" metninden renk (PC'den gelen kutu renkleri)
  init(css: String) {
    let s = css.trimmingCharacters(in: CharacterSet(charactersIn: "#"))
    self.init(hex: UInt32(s, radix: 16) ?? 0x8AB4FF)
  }
}

/// Kalkan + vizor markasi (web'deki SVG ile ayni)
struct BrandMark: View {
  var size: CGFloat = 30
  var progress: CGFloat = 1  // 0...1 cizim (acilis animasyonu)

  var body: some View {
    ZStack {
      ShieldShape()
        .trim(from: 0, to: progress)
        .stroke(Theme.amber, style: StrokeStyle(lineWidth: size / 15, lineCap: .round, lineJoin: .round))
      Circle()
        .trim(from: 0, to: progress)
        .stroke(Theme.text, lineWidth: size / 15)
        .frame(width: size * 0.27, height: size * 0.27)
        .offset(y: -size * 0.016)
    }
    .frame(width: size, height: size)
  }
}

struct ShieldShape: Shape {
  func path(in r: CGRect) -> Path {
    let w = r.width, h = r.height
    var p = Path()
    p.move(to: CGPoint(x: w * 0.5, y: h * 0.08))
    p.addLine(to: CGPoint(x: w * 0.14, y: h * 0.22))
    p.addLine(to: CGPoint(x: w * 0.14, y: h * 0.48))
    p.addCurve(to: CGPoint(x: w * 0.5, y: h * 0.92),
               control1: CGPoint(x: w * 0.14, y: h * 0.70), control2: CGPoint(x: w * 0.29, y: h * 0.86))
    p.addCurve(to: CGPoint(x: w * 0.86, y: h * 0.48),
               control1: CGPoint(x: w * 0.71, y: h * 0.86), control2: CGPoint(x: w * 0.86, y: h * 0.70))
    p.addLine(to: CGPoint(x: w * 0.86, y: h * 0.22))
    p.closeSubpath()
    return p
  }
}

/// Bolum karti: ince cizgili, sakin yuzey
struct Card<Content: View>: View {
  var title: String?
  @ViewBuilder var content: Content

  var body: some View {
    VStack(alignment: .leading, spacing: 12) {
      if let title {
        Text(title).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.muted)
      }
      content
    }
    .padding(16)
    .frame(maxWidth: .infinity, alignment: .leading)
    .background(Theme.surface, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
    .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous).stroke(Theme.line, lineWidth: 1))
  }
}

/// Durum noktasi + metin
struct StatusDot: View {
  var on: Bool
  var text: String
  var body: some View {
    HStack(spacing: 6) {
      Circle().fill(on ? Theme.ok : Theme.line).frame(width: 8, height: 8)
      Text(text).font(.footnote).foregroundStyle(on ? Theme.text : Theme.muted)
    }
  }
}
