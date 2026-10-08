// Model:Cycles on the phone: the native machine (native/cfcore) with its
// panel, screen and sound. It starts from the bundled state, the factory
// pattern playing, and keeps nothing when it closes.
import SwiftUI

@main
struct CyclesApp: App {
    @StateObject private var engine = Engine()
    @Environment(\.scenePhase) private var phase

    var body: some Scene {
        WindowGroup {
            PanelView(engine: engine)
                .ignoresSafeArea(edges: .bottom)
                .persistentSystemOverlays(.hidden)
                .statusBarHidden()
                .preferredColorScheme(.dark)
                .onAppear {
                    UIApplication.shared.isIdleTimerDisabled = true
                    engine.start()
                }
                .onChange(of: phase) { _, now in
                    // Foreground only: no background audio yet.
                    if now == .active { engine.resume() } else if now == .background { engine.pause() }
                }
        }
    }
}
