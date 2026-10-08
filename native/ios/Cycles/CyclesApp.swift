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
                .onChange(of: phase) { _, now in
                    // Foreground only: no background audio yet.
                    if now == .active {
                        engine.resume()
                    } else if now == .background {
                        engine.pause()
                        // Time to write the machine out before the app is
                        // suspended.
                        let app = UIApplication.shared
                        var task = UIBackgroundTaskIdentifier.invalid
                        let finish = {
                            DispatchQueue.main.async {
                                if task != .invalid {
                                    app.endBackgroundTask(task)
                                    task = .invalid
                                }
                            }
                        }
                        task = app.beginBackgroundTask(withName: "save", expirationHandler: finish)
                        engine.save(then: finish)
                    }
                }
        }
    }
}
