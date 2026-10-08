// Times the native Model:Cycles machine (native/cfcore) on this device:
// it runs the bundled starting state a chunk at a time off the main thread
// and logs each chunk's wall and CPU time, the thermal state and memory.
// The log is also written to Documents/cfbench.log, which the Files app and
// `devicectl device copy from` can read.
//
// Launch arguments, for a run started from the Mac: --seconds N.
import SwiftUI
import UIKit

/// Instructions per emulated second: the rate the recording ran at while
/// not idle (docs/MODEL-CYCLES-IPAD-PLAN.md).
let instructionsPerSecond: Int64 = 211_700_000
let chunkSeconds = 10.0

func machineName() -> String {
    var size = 0
    sysctlbyname("hw.machine", nil, &size, nil, 0)
    var name = [CChar](repeating: 0, count: size)
    sysctlbyname("hw.machine", &name, &size, nil, 0)
    return String(cString: name)
}

func threadCPUSeconds() -> Double {
    var t = timespec()
    clock_gettime(CLOCK_THREAD_CPUTIME_ID, &t)
    return Double(t.tv_sec) + Double(t.tv_nsec) / 1e9
}

func wallSeconds() -> Double {
    var t = timespec()
    clock_gettime(CLOCK_MONOTONIC, &t)
    return Double(t.tv_sec) + Double(t.tv_nsec) / 1e9
}

/// The process's physical footprint in MB, the figure iOS limits.
func footprintMB() -> Double {
    var info = task_vm_info_data_t()
    var count = mach_msg_type_number_t(MemoryLayout<task_vm_info_data_t>.size / MemoryLayout<natural_t>.size)
    let result = withUnsafeMutablePointer(to: &info) {
        $0.withMemoryRebound(to: integer_t.self, capacity: Int(count)) {
            task_info(mach_task_self_, task_flavor_t(TASK_VM_INFO), $0, &count)
        }
    }
    return result == KERN_SUCCESS ? Double(info.phys_footprint) / 1_048_576 : 0
}

func thermalName() -> String {
    switch ProcessInfo.processInfo.thermalState {
    case .nominal: return "nominal"
    case .fair: return "fair"
    case .serious: return "serious"
    case .critical: return "critical"
    @unknown default: return "unknown"
    }
}

final class Runner: ObservableObject {
    @Published var lines: [String] = []
    @Published var running = false
    private var stop = false
    private let logURL = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        .appendingPathComponent("cfbench.log")

    private func log(_ line: String) {
        print(line)
        if let handle = try? FileHandle(forWritingTo: logURL) {
            handle.seekToEndOfFile()
            handle.write((line + "\n").data(using: .utf8)!)
            try? handle.close()
        } else {
            try? (line + "\n").write(to: logURL, atomically: true, encoding: .utf8)
        }
        DispatchQueue.main.async { self.lines.append(line) }
    }

    func cancel() {
        stop = true
    }

    func start(seconds: Double) {
        guard !running else { return }
        running = true
        stop = false
        UIApplication.shared.isIdleTimerDisabled = true
        let thread = Thread { [self] in
            run(seconds: seconds)
            DispatchQueue.main.async {
                self.running = false
                UIApplication.shared.isIdleTimerDisabled = false
            }
        }
        // The translated blocks are one large function: do not rely on a
        // secondary thread's default 512 KB.
        thread.stackSize = 16 << 20
        thread.qualityOfService = .userInteractive
        thread.start()
    }

    private func run(seconds: Double) {
        let device = UIDevice.current
        log("== \(machineName()), \(device.systemName) \(device.systemVersion), \(Date())")
        log("\(cfcore_blocks()) translated blocks; \(Int(seconds)) emulated s in chunks of \(Int(chunkSeconds)); low power mode \(ProcessInfo.processInfo.isLowPowerModeEnabled ? "on" : "off")")
        guard let path = Bundle.main.path(forResource: "cycles-play", ofType: "start") else {
            log("no starting state in the bundle")
            return
        }
        let t0 = wallSeconds()
        guard let bench = cfcore_open(path, instructionsPerSecond) else {
            log("open failed: \(String(cString: cfcore_error()))")
            return
        }
        defer { cfcore_close(bench) }
        log(String(format: "opened in %.2f s, footprint %.0f MB", wallSeconds() - t0, footprintMB()))
        var (emulated, wall, cpu, worst, n) = (0.0, 0.0, 0.0, 0.0, 0)
        // Whole chunks only, so each one's audio hash can be compared with
        // cfchunks' on the Mac.
        let chunks = Int((seconds / chunkSeconds).rounded(.up))
        while n < chunks && !stop {
            var chunk = cf_chunk()
            let (w0, c0) = (wallSeconds(), threadCPUSeconds())
            let stopped = cfcore_run(bench, chunkSeconds, &chunk)
            let (w, c) = (wallSeconds() - w0, threadCPUSeconds() - c0)
            n += 1
            emulated += chunk.emulated_s
            wall += w
            cpu += c
            let per = w / max(chunk.emulated_s, 1e-9)
            worst = max(worst, per)
            log(String(
                format: "%d %.3f s: wall %.4f, cpu %.4f s per emulated s (%.2fx); %.1fM instructions, %lld interpreted, %llu interrupts, %llu frames, level %.0f, hash %016llx; %@, %.0f MB",
                n, chunk.emulated_s, per, c / max(chunk.emulated_s, 1e-9), 1 / per,
                Double(chunk.instructions) / 1e6, chunk.interpreted, chunk.interrupts, chunk.frames,
                chunk.level, chunk.hash, thermalName(), footprintMB()))
            if stopped != 0 {
                log("STOPPED: \(String(cString: cfcore_error()))")
                break
            }
        }
        if emulated > 0 {
            log(String(
                format: "total %.1f emulated s: wall %.4f, cpu %.4f s per emulated s (%.2fx real time); slowest chunk %.4f (%.2fx)%@",
                emulated, wall / emulated, cpu / emulated, emulated / wall, worst, 1 / worst,
                stop ? "; cancelled" : ""))
        }
    }
}

struct ContentView: View {
    @StateObject private var runner = Runner()

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Model:Cycles native benchmark").font(.headline)
            HStack {
                Button("2 min") { runner.start(seconds: 120) }
                Button("10 min") { runner.start(seconds: 600) }
                Button("30 min") { runner.start(seconds: 1800) }
                Spacer()
                Button("Stop") { runner.cancel() }.disabled(!runner.running)
            }
            .buttonStyle(.bordered)
            .disabled(runner.running)
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 4) {
                        ForEach(Array(runner.lines.enumerated()), id: \.offset) { index, line in
                            Text(line).font(.system(size: 11, design: .monospaced)).id(index)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .onChange(of: runner.lines.count) { _, count in
                    proxy.scrollTo(count - 1, anchor: .bottom)
                }
            }
        }
        .padding()
        .onAppear {
            let args = CommandLine.arguments
            if let i = args.firstIndex(of: "--seconds"), i + 1 < args.count, let s = Double(args[i + 1]) {
                runner.start(seconds: s)
            }
        }
    }
}

@main
struct CyclesBenchApp: App {
    var body: some Scene {
        WindowGroup { ContentView() }
    }
}
