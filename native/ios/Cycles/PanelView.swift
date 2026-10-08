// Draws the panel and takes its touches. Every finger is its own control:
// hold FUNCTION or a trig with one and turn a knob with another.
import SwiftUI
import UIKit

/// What the fingers are doing, for the drawing.
final class Touches: ObservableObject {
    @Published var held: Set<Int> = []
    /// Each knob's pointer, in detents turned since the app started.
    @Published var turned: [Int: Int] = [:]
}

struct PanelView: View {
    @ObservedObject var engine: Engine
    @StateObject private var touches = Touches()

    var body: some View {
        GeometryReader { geo in
            let scale = min(geo.size.width / Panel.bounds.width, geo.size.height / Panel.bounds.height)
            let size = CGSize(width: Panel.bounds.width * scale, height: Panel.bounds.height * scale)
            ZStack {
                Canvas { context, _ in
                    context.scaleBy(x: scale, y: scale)
                    context.translateBy(x: -Panel.bounds.minX, y: -Panel.bounds.minY)
                    draw(&context)
                }
                TouchSurface(engine: engine, touches: touches, scale: scale)
            }
            .frame(width: size.width, height: size.height)
            .position(x: geo.size.width / 2, y: geo.size.height / 2)
        }
        .background(Panel.background)
    }

    private func draw(_ context: inout GraphicsContext) {
        guard let skin = Skin.shared else {
            context.draw(
                Text("The panel artwork is missing from the app").foregroundColor(Panel.amber),
                at: CGPoint(x: Panel.bounds.midX, y: Panel.bounds.midY))
            return
        }
        context.draw(Image(decorative: skin.plate, scale: 1), in: CGRect(origin: .zero, size: Panel.canvas))
        if let image = screenImage(engine.screen) {
            context.draw(Image(decorative: image, scale: 1).interpolation(.none), in: Panel.screen)
        }
        if !engine.status.isEmpty {
            context.draw(
                Text(engine.status).font(.system(size: 14)).foregroundColor(Panel.amber),
                in: Panel.screen.insetBy(dx: 14, dy: 14))
        }
        for control in Panel.controls {
            let pressed = touches.held.contains(control.id)
            if control.isKnob {
                drawKnob(&context, control, pressed)
                continue
            }
            // The cap, up or down, then its light: the LED's colour, or the
            // legend colour while it is held dark (emu/dtpanel.py's _paint).
            guard let caps = skin.caps[control.label], let masks = skin.masks[control.label] else { continue }
            let tile = CGRect(
                x: control.rect.minX - skin.pad, y: control.rect.minY - skin.pad,
                width: skin.tile.width, height: skin.tile.height)
            context.draw(Image(decorative: caps[pressed ? 1 : 0], scale: 1), in: tile)
            let lit = control.led.map { engine.leds >> UInt64($0) & 1 != 0 } ?? false
            guard lit || pressed else { continue }
            context.drawLayer { layer in
                layer.addFilter(.colorMultiply(lit ? Panel.lit : Panel.legend))
                layer.draw(Image(decorative: masks[pressed ? 1 : 0], scale: 1), in: tile)
            }
        }
    }

    private func drawKnob(_ context: inout GraphicsContext, _ control: Control, _ pressed: Bool) {
        let r = control.rect.width / 2
        let centre = CGPoint(x: control.rect.midX, y: control.rect.midY)
        if pressed {
            context.stroke(Path(ellipseIn: control.rect), with: .color(Panel.legend), lineWidth: 2)
        }
        // The pointer: a detent is 15 degrees. The three pale knobs take a
        // dark one.
        let angle = CGFloat(touches.turned[control.id] ?? 0) * .pi / 12
        let pale = ["VOLUME", "REVERB SIZE", "DELAY TIME"].contains(control.label)
        var mark = Path()
        mark.move(to: CGPoint(x: centre.x + sin(angle) * (r - 11), y: centre.y - cos(angle) * (r - 11)))
        mark.addLine(to: CGPoint(x: centre.x + sin(angle) * (r - 7), y: centre.y - cos(angle) * (r - 7)))
        context.stroke(
            mark, with: .color(Color(hex: pale ? 0x292e2c : 0xf1f2f3)),
            style: StrokeStyle(lineWidth: 3, lineCap: .round))
    }

    private func screenImage(_ pixels: [UInt8]) -> CGImage? {
        // Lit and dark, as the window draws them.
        var rgba = [UInt8](repeating: 255, count: pixels.count * 4)
        for (i, on) in pixels.enumerated() {
            let (r, g, b): (UInt8, UInt8, UInt8) = on != 0 ? (0xe6, 0xed, 0x78) : (0x08, 0x0b, 0x04)
            rgba[4 * i] = r
            rgba[4 * i + 1] = g
            rgba[4 * i + 2] = b
        }
        guard let provider = CGDataProvider(data: Data(rgba) as CFData) else { return nil }
        return CGImage(
            width: screenWidth, height: screenHeight, bitsPerComponent: 8, bitsPerPixel: 32,
            bytesPerRow: screenWidth * 4, space: CGColorSpaceCreateDeviceRGB(),
            bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.noneSkipLast.rawValue),
            provider: provider, decode: nil, shouldInterpolate: false, intent: .defaultIntent)
    }
}

struct TouchSurface: UIViewRepresentable {
    let engine: Engine
    let touches: Touches
    let scale: CGFloat

    func makeUIView(context: Context) -> TouchView {
        let view = TouchView()
        view.isMultipleTouchEnabled = true
        view.backgroundColor = .clear
        return view
    }

    func updateUIView(_ view: TouchView, context: Context) {
        view.engine = engine
        view.state = touches
        view.scale = scale
    }
}

final class TouchView: UIView {
    var engine: Engine?
    var state: Touches?
    var scale: CGFloat = 1

    private struct Finger {
        let control: Control
        let first: CGPoint
        var last: CGPoint
        var carry: CGFloat = 0
        var dragged = false
    }
    private var fingers: [UITouch: Finger] = [:]
    /// Points of drag for one detent: emu/dtpanel.py's six pixels.
    private let detent: CGFloat = 6

    private func panelPoint(_ touch: UITouch) -> CGPoint {
        let p = touch.location(in: self)
        return CGPoint(x: p.x / scale + Panel.bounds.minX, y: p.y / scale + Panel.bounds.minY)
    }

    override func touchesBegan(_ touches: Set<UITouch>, with event: UIEvent?) {
        for touch in touches {
            let point = panelPoint(touch)
            // One finger a control: a second on the same key would let it
            // go when the first lifts.
            guard let control = Panel.control(at: point),
                !fingers.values.contains(where: { $0.control.id == control.id })
            else { continue }
            fingers[touch] = Finger(control: control, first: touch.location(in: self), last: touch.location(in: self))
            state?.held.insert(control.id)
            switch control.kind {
            case let .key(column, bit):
                engine?.send(.key(column: column, bit: bit, down: true))
            case let .pad(channel):
                // Where the pad is touched is how hard: 127 at its top
                // edge, 30 at its bottom (emu/mdpanel.py).
                let depth = min(1, max(0, (point.y - Panel.padTop) / Panel.padHeight))
                engine?.send(.pad(index: channel, velocity: Int32((127 - depth * 97).rounded())))
            case .knob:
                break
            }
        }
    }

    override func touchesMoved(_ touches: Set<UITouch>, with event: UIEvent?) {
        for touch in touches {
            guard var finger = fingers[touch], case let .knob(encoder, _) = finger.control.kind else { continue }
            // Up or right is clockwise.
            let now = touch.location(in: self)
            if abs(now.x - finger.first.x) > 4 || abs(now.y - finger.first.y) > 4 {
                finger.dragged = true
            }
            finger.carry += ((now.x - finger.last.x) - (now.y - finger.last.y)) / detent
            finger.last = now
            let steps = Int(finger.carry)
            if steps != 0 {
                finger.carry -= CGFloat(steps)
                engine?.send(.turn(encoder: encoder, steps: Int32(steps)))
                state?.turned[finger.control.id, default: 0] += steps
            }
            fingers[touch] = finger
        }
    }

    private func lift(_ touches: Set<UITouch>) {
        for touch in touches {
            guard let finger = fingers.removeValue(forKey: touch) else { continue }
            state?.held.remove(finger.control.id)
            switch finger.control.kind {
            case let .key(column, bit):
                engine?.send(.key(column: column, bit: bit, down: false))
            case let .pad(channel):
                engine?.send(.pad(index: channel, velocity: 0))
            case let .knob(_, push):
                // A tap pushes a knob; a drag turns it (emu/dtpanel.py).
                // The panel holds the press for as long as the firmware
                // needs, so both halves can go at once.
                if let push, !finger.dragged {
                    engine?.send(.key(column: push.column, bit: push.bit, down: true))
                    engine?.send(.key(column: push.column, bit: push.bit, down: false))
                }
            }
        }
    }

    override func touchesEnded(_ touches: Set<UITouch>, with event: UIEvent?) {
        lift(touches)
    }

    override func touchesCancelled(_ touches: Set<UITouch>, with event: UIEvent?) {
        lift(touches)
    }
}
