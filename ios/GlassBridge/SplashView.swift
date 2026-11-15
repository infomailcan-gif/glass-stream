import SwiftUI

/// Acilis: kalkan cizilir, vizor halkasi bir tur doner, marka belirir. ~1.8 sn, bir kez.
struct SplashView: View {
  var onFinish: () -> Void
  @State private var draw: CGFloat = 0
  @State private var ring: Double = 0
  @State private var showText = false
  @Environment(\.accessibilityReduceMotion) private var reduceMotion

  var body: some View {
    ZStack {
      Theme.ink.ignoresSafeArea()
      VStack(spacing: 28) {
        ZStack {
          // Tarama halkasi: tek tur, sonra durur
          Circle()
            .trim(from: 0, to: 0.22)
            .stroke(Theme.amber.opacity(0.55), style: StrokeStyle(lineWidth: 2, lineCap: .round))
            .frame(width: 132, height: 132)
            .rotationEffect(.degrees(ring))
            .opacity(draw > 0.95 ? 0 : 1)
          Circle().stroke(Theme.line, lineWidth: 1).frame(width: 132, height: 132)
          BrandMark(size: 72, progress: draw)
        }
        VStack(spacing: 6) {
          Text("Canberk Şıklı")
            .font(.system(size: 28, weight: .heavy, design: .default))
            .foregroundStyle(Theme.text)
          Text("AI Systems")
            .font(.system(size: 17, weight: .medium))
            .foregroundStyle(Theme.muted)
          Text("İş güvenliği, güvenlik ve yapay zekâ için saha gözü")
            .font(.footnote)
            .foregroundStyle(Theme.muted.opacity(0.8))
            .padding(.top, 10)
        }
        .opacity(showText ? 1 : 0)
        .offset(y: showText ? 0 : 6)
      }
    }
    .onAppear {
      if reduceMotion {
        draw = 1; showText = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.6, execute: onFinish)
        return
      }
      withAnimation(.easeInOut(duration: 0.9)) { draw = 1 }
      withAnimation(.easeInOut(duration: 1.0)) { ring = 360 }
      withAnimation(.easeOut(duration: 0.5).delay(0.6)) { showText = true }
      DispatchQueue.main.asyncAfter(deadline: .now() + 1.8, execute: onFinish)
    }
  }
}
