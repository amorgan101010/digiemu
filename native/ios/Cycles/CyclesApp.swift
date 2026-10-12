// Model:Cycles on the phone: the native machine (native/cfcore) with its
// panel, screen and sound. It starts where it was left (Engine.swift keeps
// the machine in Documents/cycles.save), or from the bundled state, the
// factory pattern playing.
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
                .onChange(of: phase) { was, now in
                    // Foreground only: no background audio yet.
                    if now == .active {
                        engine.resume()
                    } else if now == .inactive, was == .active {
                        // The app switcher, Control Centre: the app may be
                        // closed from here with no time given, and what the
                        // firmware saved to its +Drive is only in the kept
                        // machine. Written now, while it still runs.
                        engine.save()
                    } else if now == .background {
                        engine.pause()
                        // Wait for the file here: the app is suspended, or
                        // closed, once this returns.
                        engine.saveNow()
                    }
                }
                .onReceive(NotificationCenter.default.publisher(for: UIApplication.willTerminateNotification)) { _ in
                    engine.saveNow()
                }
        }
    }
}
