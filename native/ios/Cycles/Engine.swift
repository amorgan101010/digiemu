// The Model:Cycles machine (native/cfcore) running live: one thread runs the
// emulation a slice at a time, paced by how much audio the output has taken,
// and an AVAudioEngine source node plays what it produces. The panel's input
// is queued to that thread; the screen and LEDs are read on it and handed to
// the main thread when they change.
//
// The machine is kept: that thread writes the whole of it to
// Documents/cycles.save when the app goes to the background, and a minute
// after the panel was last touched, and the app starts from that file when
// there is one. Deleting the file (it shows in the Files app) starts from
// the bundled state again.
import AVFoundation
import SwiftUI

/// Model:Cycles OS 1.13 on this core's clock (docs/MODEL-CYCLES-IPAD-PLAN.md),
/// and where its display's frame-buffer pointer is (MODELS.md).
let instructionsPerSecond: Int64 = 211_700_000
let screenPointer: UInt32 = 0x4014_92f0

let screenWidth = 128, screenHeight = 64

enum PanelInput {
    case key(column: Int32, bit: Int32, down: Bool)
    case pad(index: Int32, velocity: Int32)
    case turn(encoder: Int32, steps: Int32)
}

final class Engine: ObservableObject {
    /// The screen, a byte a pixel (0 or 1), row-major.
    @Published var screen = [UInt8](repeating: 0, count: screenWidth * screenHeight)
    /// The LEDs lit: bit `row * 8 + bit` (devices/model-cycles.toml).
    @Published var leds: UInt64 = 0
    @Published var status = "Starting"

    /// Audio the emulation keeps ahead of the output, in frames: 16 ms.
    private let lead: UInt32 = 768
    private let slice = 128
    private let ring = ring_new(8192)
    private let audio = AVAudioEngine()
    private var source: AVAudioSourceNode?
    private let lock = NSLock()
    private var inbox: [PanelInput] = []
    private var running = false
    private var underruns = 0
    /// Asked for from the main thread, done on the emulation's: each is
    /// called once the machine has been written, or could not be.
    private var saves: [() -> Void] = []

    /// Where the machine is kept between launches.
    static let savePath: String = {
        let dir = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        return dir.appendingPathComponent("cycles.save").path
    }()

    /// Write the machine out, then call `done` (on the emulation's thread).
    func save(then done: @escaping () -> Void = {}) {
        lock.lock()
        saves.append(done)
        lock.unlock()
    }

    private func takeSaves() -> [() -> Void] {
        lock.lock()
        defer { lock.unlock() }
        let out = saves
        saves.removeAll()
        return out
    }

    func send(_ input: PanelInput) {
        lock.lock()
        inbox.append(input)
        lock.unlock()
    }

    private func takeInput() -> [PanelInput] {
        lock.lock()
        defer { lock.unlock() }
        let out = inbox
        inbox.removeAll(keepingCapacity: true)
        return out
    }

    func start() {
        guard !running else { return }
        running = true
        startAudio()
        NotificationCenter.default.addObserver(
            forName: AVAudioSession.interruptionNotification, object: nil, queue: .main
        ) { [weak self] note in
            let ended = note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt
                == AVAudioSession.InterruptionType.ended.rawValue
            if ended { self?.resume() }
        }
        NotificationCenter.default.addObserver(
            forName: AVAudioSession.routeChangeNotification, object: nil, queue: .main
        ) { [weak self] _ in self?.resume() }
        let thread = Thread { [self] in emulate() }
        // The translated blocks are one large function: do not rely on a
        // secondary thread's default 512 KB.
        thread.stackSize = 16 << 20
        thread.qualityOfService = .userInteractive
        thread.name = "cfcore"
        thread.start()
    }

    private func startAudio() {
        let session = AVAudioSession.sharedInstance()
        try? session.setCategory(.playback, mode: .default)
        // The firmware plays at 48 kHz. The rate is a preference: the mixer
        // converts if the route runs at another.
        try? session.setPreferredSampleRate(48_000)
        try? session.setPreferredIOBufferDuration(0.005)
        try? session.setActive(true)
        let format = AVAudioFormat(standardFormatWithSampleRate: 48_000, channels: 2)!
        let ring = self.ring
        let node = AVAudioSourceNode(format: format) { _, _, frames, buffers -> OSStatus in
            // The audio thread: no locks, no allocation, no Swift objects.
            let list = UnsafeMutableAudioBufferListPointer(buffers)
            guard list.count >= 2,
                let left = list[0].mData?.assumingMemoryBound(to: Float.self),
                let right = list[1].mData?.assumingMemoryBound(to: Float.self)
            else { return noErr }
            _ = ring_read(ring, left, right, frames)
            return noErr
        }
        source = node
        audio.attach(node)
        audio.connect(node, to: audio.mainMixerNode, format: format)
        resume()
    }

    /// Start the output again: after an interruption, a route change, or a
    /// return from the background.
    func resume() {
        guard running, !audio.isRunning else { return }
        try? AVAudioSession.sharedInstance().setActive(true)
        do {
            try audio.start()
        } catch {
            status = "Audio did not start: \(error.localizedDescription)"
        }
    }

    /// Stop the output. The emulation waits: it only runs to keep the ring
    /// ahead of the output.
    func pause() {
        audio.pause()
    }

    private func say(_ text: String) {
        DispatchQueue.main.async { self.status = text }
    }

    private func emulate() {
        guard let path = Bundle.main.path(forResource: "cycles-play", ofType: "start") else {
            say("No starting state in the app")
            return
        }
        // The kept machine if there is one that opens, else the bundled
        // state. A kept one that stops in its first seconds is set aside.
        let kept = Engine.savePath
        var fromKept = FileManager.default.fileExists(atPath: kept)
        var opened = fromKept ? cfcore_open(kept, instructionsPerSecond) : nil
        if opened == nil {
            if fromKept {
                // It does not open: out of the way of the next save.
                try? FileManager.default.removeItem(atPath: kept + ".bad")
                try? FileManager.default.moveItem(atPath: kept, toPath: kept + ".bad")
            }
            fromKept = false
            opened = cfcore_open(path, instructionsPerSecond)
        }
        guard var bench = opened else {
            say("Could not open the starting state: \(String(cString: cfcore_error()))")
            return
        }
        defer { cfcore_close(bench) }
        cfcore_live(bench)
        say("")
        var played = 0
        var touched = false
        var lastTouch = DispatchTime.now().uptimeNanoseconds
        let capacity = slice * 4
        var pcm = [Float](repeating: 0, count: capacity * 2)
        var raw = [UInt8](repeating: 0, count: 1024)
        var lastRaw = raw
        var lastLeds: UInt64 = ~0
        var shown = DispatchTime.now().uptimeNanoseconds
        while running {
            let inputs = takeInput()
            if !inputs.isEmpty {
                touched = true
                lastTouch = DispatchTime.now().uptimeNanoseconds
            }
            for input in inputs {
                switch input {
                case let .key(column, bit, down): cfcore_key(bench, column, bit, down ? 1 : 0)
                case let .pad(index, velocity): cfcore_pad(bench, index, velocity)
                case let .turn(encoder, steps): cfcore_turn(bench, encoder, steps)
                }
            }
            if ring_fill(ring) < lead {
                let frames = cfcore_advance(bench, slice, &pcm, capacity)
                if frames < 0 {
                    let why = String(cString: cfcore_error())
                    // Two seconds of sound from a kept machine before it
                    // stops: start from the bundled state instead.
                    if fromKept, played < 96_000, let fresh = cfcore_open(path, instructionsPerSecond) {
                        try? FileManager.default.removeItem(atPath: kept + ".bad")
                        try? FileManager.default.moveItem(atPath: kept, toPath: kept + ".bad")
                        cfcore_close(bench)
                        bench = fresh
                        cfcore_live(bench)
                        fromKept = false
                        played = 0
                        continue
                    }
                    say("The machine stopped: \(why)")
                    for done in takeSaves() { done() }
                    return
                }
                played += frames
                // The firmware's VOLUME sets the codec's level, which the
                // output follows.
                let gain = cfcore_gain(bench)
                if gain != 1 {
                    for i in 0..<frames * 2 { pcm[i] *= gain }
                }
                _ = ring_write(ring, pcm, UInt32(frames))
            } else {
                usleep(1000)
            }
            // Keep the machine: when asked, and a minute after the panel
            // was last touched (not while it only plays: nothing changes
            // that a later save would miss, and each save is 50 MB).
            var waiting = takeSaves()
            let asked = !waiting.isEmpty
            if !asked, touched, DispatchTime.now().uptimeNanoseconds - lastTouch >= 60_000_000_000 {
                // While it plays: only once the output has all the sound
                // the ring holds (about 150 ms) to cover the copy, and the
                // file is written by another thread.
                if ring_fill(ring) < 7168 {
                    let frames = cfcore_advance(bench, slice, &pcm, capacity)
                    if frames > 0 {
                        let gain = cfcore_gain(bench)
                        if gain != 1 {
                            for i in 0..<frames * 2 { pcm[i] *= gain }
                        }
                        _ = ring_write(ring, pcm, UInt32(frames))
                        played += frames
                        continue
                    }
                }
                waiting = [{}]
            }
            if !waiting.isEmpty {
                if cfcore_save(bench, kept, asked ? 1 : 0) != 0 {
                    say("Could not save: \(String(cString: cfcore_error()))")
                } else {
                    touched = false
                }
                for done in waiting { done() }
            }
            // The screen and LEDs, at most 30 times a second.
            let now = DispatchTime.now().uptimeNanoseconds
            guard now - shown >= 33_000_000 else { continue }
            shown = now
            let lit = cfcore_leds(bench)
            if lit != lastLeds {
                lastLeds = lit
                DispatchQueue.main.async { self.leds = lit }
            }
            if cfcore_screen(bench, screenPointer, &raw) != 0, raw != lastRaw {
                lastRaw = raw
                // emu/panel.py: byte page + 8 * column, page 0 the bottom
                // eight rows, bit n the row 8 * (7 - page) + n.
                var pixels = [UInt8](repeating: 0, count: screenWidth * screenHeight)
                for y in 0..<screenHeight {
                    for x in 0..<screenWidth where raw[(7 - y / 8) + 8 * x] >> (y % 8) & 1 != 0 {
                        pixels[y * screenWidth + x] = 1
                    }
                }
                DispatchQueue.main.async { self.screen = pixels }
            }
        }
    }
}
