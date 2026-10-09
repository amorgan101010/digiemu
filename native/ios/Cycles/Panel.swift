// The Model:Cycles panel, as emu/mdpanel.py lays it out: the same positions
// in the same 1380 x 884 space, over the same drawn faceplate
// (emu/assets/panels/model-cycles), and the wiring from
// devices/model-cycles.toml (which scan column and bit each key is, which
// ADC channel each pad, which LED each key lights, which the lens beside
// each parameter knob and the four page lights).
//
// One key is the app's own: PUSH, between PATTERN and TRACK. It is
// LEVEL/DATA's push switch, the only knob that has one. A finger cannot
// press a knob and turn it, so tap PUSH for a click, or hold it and drag
// LEVEL/DATA for a press and turn.
import SwiftUI
import UIKit

enum ControlKind {
    case key(column: Int32, bit: Int32)
    case pad(channel: Int32)
    case knob(encoder: Int32)
}

struct Control: Identifiable {
    let id: Int
    let label: String
    let kind: ControlKind
    /// A key's or pad's rectangle; a knob's bounding square.
    let rect: CGRect
    /// The LED this key lights, or the one beside this knob, if any
    /// (row * 8 + bit).
    var led: Int? = nil

    var isKnob: Bool {
        if case .knob = kind { return true }
        return false
    }
}

enum Panel {
    static let canvas = CGSize(width: 1380, height: 884)
    /// BODY in emu/mdpanel.py: the faceplate on that canvas.
    static let bounds = CGRect(x: 20, y: 74, width: 1340, height: 776)
    /// SCREEN: the 128 x 64 display at three times.
    static let screen = CGRect(x: 150, y: 120, width: 384, height: 192)
    static let padTop: CGFloat = 646, padHeight: CGFloat = 76

    static let background = Color(hex: 0x111214)
    static let amber = Color(hex: 0xffc23d)
    /// LEGEND in emu/mdpanel.py: a key held, a knob touched.
    static let legend = Color(hex: 0xef726b)
    /// MODEL_LED in emu/gui.py.
    static let lit = Color(red: 255 / 255, green: 72 / 255, blue: 48 / 255)

    /// Key code -> scan column and bit ([panel.exceptions]).
    private static let wire: [Int: (Int32, Int32)] = [
        1: (1, 2), 2: (1, 5), 3: (1, 3), 4: (1, 4), 5: (0, 0), 6: (0, 1), 7: (0, 2), 8: (0, 3),
        9: (1, 1), 10: (0, 7), 11: (0, 5), 12: (1, 0), 13: (0, 6), 14: (0, 4), 15: (3, 6),
        16: (1, 6), 17: (1, 7), 18: (2, 0), 19: (2, 1), 20: (2, 2), 21: (2, 3), 22: (2, 4),
        23: (2, 5), 24: (2, 6), 25: (2, 7), 26: (3, 0), 27: (3, 1), 28: (3, 2), 29: (3, 3),
        30: (3, 4), 31: (3, 5), 32: (3, 7),
    ]
    /// LEVEL/DATA's push switch: key 32 of the device file. The PUSH key
    /// is that switch.
    static let pushSwitch: (column: Int32, bit: Int32) = (3, 7)
    static let pushKey = 200
    /// [panel] page_leds: the lights above PAGE, 1:4 to 4:4, placed as
    /// emu/panellayout.py's page_lights places them.
    static let pageLights: [(led: Int, at: CGPoint)] = [52, 48, 49, 50].enumerated().map {
        (led: $1, at: CGPoint(x: 1236 + 43 + (CGFloat($0) - 1.5) * 18, y: 658 - 30))
    }
    /// [panel.knob_leds], by the knob's name: the lens beside each
    /// parameter knob, lit while a step is held for each parameter locked
    /// on it.
    private static let knobLed: [String: Int] = [
        "PITCH": 26, "DECAY": 34, "COLOR": 32, "SHAPE": 41, "SWEEP": 27, "CONTOUR": 24,
        "DELAY SEND": 33, "REVERB SEND": 42, "LFO SPEED": 15, "VOL+DIST": 25, "SWING": 35,
        "CHANCE": 43,
    ]
    /// emu/panellayout.py's knob_light: where that lens is.
    static func knobLight(_ knob: CGRect) -> CGPoint {
        CGPoint(x: knob.minX - 16, y: knob.minY + 10)
    }

    /// Key code -> name ([panel.labels]); the trigs are 16 to 31.
    private static let keyCode: [String: Int] = [
        "FUNCTION": 1, "TRACK": 2, "PATTERN": 3, "RETRIG": 4, "MACHINE": 5, "PUNCH": 6,
        "GATE": 7, "LFO": 8, "RECORD": 9, "PLAY": 10, "STOP": 11, "BACK": 12, "SETTINGS": 13,
        "TEMPO": 14, "PAGE": 15, "PITCH": 32,
    ]
    /// LED id -> key or pad code ([panel.leds]).
    private static let ledCode: [Int: Int] = [
        0: 10, 1: 13, 2: 12, 3: 9, 4: 1, 5: 3, 6: 16, 7: 2, 8: 7, 9: 6, 10: 5, 11: 14, 12: 11,
        13: 4, 14: 8, 16: 22, 17: 33, 18: 34, 19: 17, 20: 18, 21: 19, 22: 20, 23: 21, 28: 23,
        29: 24, 30: 35, 31: 25, 36: 36, 37: 26, 38: 27, 39: 37, 40: 31, 44: 28, 45: 38, 46: 29,
        47: 30, 51: 15,
    ]
    /// Encoder name -> code ([panel.encoder_labels]); the scan's encoder is
    /// the code less one.
    private static let encoderCode: [String: Int] = [
        "DECAY": 1, "COLOR": 2, "SHAPE": 3, "LEVEL/DATA": 4, "CONTOUR": 5, "DELAY SEND": 6,
        "REVERB SEND": 7, "REVERB SIZE": 8, "VOL+DIST": 9, "SWING": 10, "CHANCE": 11,
        "DELAY TIME": 12, "PITCH": 13, "SWEEP": 14, "LFO SPEED": 15, "VOLUME": 16,
    ]

    static let controls: [Control] = {
        var out: [Control] = []
        let ledOf = Dictionary(uniqueKeysWithValues: ledCode.map { ($1, $0) })
        func key(_ label: String, _ code: Int, _ x: CGFloat, _ y: CGFloat, _ w: CGFloat, _ h: CGFloat) {
            let (column, bit) = wire[code]!
            out.append(Control(
                id: code, label: label, kind: .key(column: column, bit: bit),
                rect: CGRect(x: x, y: y, width: w, height: h), led: ledOf[code]))
        }
        func knob(_ label: String, _ x: CGFloat, _ y: CGFloat, _ r: CGFloat) {
            let code = encoderCode[label]!
            out.append(Control(
                id: 100 + code, label: label, kind: .knob(encoder: Int32(code - 1)),
                rect: CGRect(x: x - r, y: y - r, width: 2 * r, height: 2 * r), led: knobLed[label]))
        }
        for (i, label) in ["BACK", "SETTINGS", "TEMPO"].enumerated() {
            key(label, keyCode[label]!, 64 + CGFloat(i) * 178, 362, 58, 48)
        }
        for (i, label) in ["RECORD", "PLAY", "STOP"].enumerated() {
            key(label, keyCode[label]!, 64 + CGFloat(i) * 178, 458, 58, 48)
        }
        key("FUNCTION", 1, 64, 554, 86, 48)
        key("RETRIG", 4, 392, 554, 86, 48)
        key("PATTERN", 3, 64, 658, 86, 48)
        key("TRACK", 2, 392, 658, 86, 48)
        out.append(Control(
            id: pushKey, label: "PUSH",
            kind: .key(column: pushSwitch.column, bit: pushSwitch.bit), rect: CGRect(x: 228, y: 658, width: 86, height: 48)))
        key("PAGE", 15, 1236, 658, 86, 48)
        for (i, label) in ["MACHINE", "PUNCH", "GATE", "LFO"].enumerated() {
            key(label, keyCode[label]!, 568, 128 + CGFloat(i) * 132, 56, 48)
        }
        // The six pads: pad Tn is ADC channel 6 - n, code 32 + n.
        for i in 0..<6 {
            out.append(Control(
                id: 33 + i, label: "T\(i + 1)", kind: .pad(channel: Int32(5 - i)),
                rect: CGRect(x: 546 + CGFloat(i) * 112, y: padTop, width: 84, height: padHeight),
                led: ledOf[33 + i]))
        }
        for i in 0..<16 {
            key("\(i + 1)", 16 + i, 64 + CGFloat(i) * 80, 778, 58, 48)
        }
        knob("LEVEL/DATA", 92, 158, 32)
        knob("VOLUME", 1278, 158, 32)
        knob("REVERB SIZE", 1278, 350, 32)
        knob("DELAY TIME", 1278, 542, 32)
        let order = [
            "PITCH", "DECAY", "COLOR", "SHAPE", "SWEEP", "CONTOUR", "DELAY SEND", "REVERB SEND",
            "LFO SPEED", "VOL+DIST", "SWING", "CHANCE",
        ]
        for (i, label) in order.enumerated() {
            knob(label, 710 + CGFloat(i % 4) * 140, 158 + CGFloat(i / 4) * 192, 32)
        }
        return out
    }()

    /// Which of the sixteen step keys `control` is, 0 to 15: their key
    /// codes are 16 to 31.
    static func step(of control: Control) -> Int? {
        if case .key = control.kind, (16...31).contains(control.id) { return control.id - 16 }
        return nil
    }

    static func stepKey(_ step: Int) -> Control? {
        controls.first { $0.id == 16 + step }
    }

    /// The step key `point` is over. Its drawn rectangle, not the wider
    /// one a first touch gets: a finger holding a step for a lock must not
    /// slip onto its neighbour.
    static func stepKey(at point: CGPoint) -> Control? {
        controls.first { step(of: $0) != nil && $0.rect.contains(point) }
    }

    /// The control a touch at `point` (in mdpanel's space) is on: keys and
    /// pads by their rectangles, a little generous, then the nearest knob.
    static func control(at point: CGPoint) -> Control? {
        if let hit = controls.first(where: { !$0.isKnob && $0.rect.insetBy(dx: -8, dy: -8).contains(point) }) {
            return hit
        }
        let knobs = controls.filter(\.isKnob).map { c -> (Control, CGFloat) in
            (c, hypot(point.x - c.rect.midX, point.y - c.rect.midY) - c.rect.width / 2)
        }
        return knobs.filter { $0.1 < 18 }.min { $0.1 < $1.1 }?.0
    }
}

/// The drawn faceplate (emu/assets/panels/README.md): the plate, each key's
/// cap up and down, and the mask its light shows through.
struct Skin {
    let plate: CGImage
    let pad: CGFloat
    let tile: CGSize
    /// Key name -> [up, down].
    let caps: [String: [CGImage]]
    let masks: [String: [CGImage]]

    private struct Meta: Decodable {
        struct Key: Decodable { let row: Int }
        let tile: [Int]
        let pad: Int
        let keys: [String: Key]
    }

    static let shared: Skin? = {
        guard let plate = UIImage(named: "plate.png")?.cgImage,
            let keys = UIImage(named: "keys.png")?.cgImage,
            let masks = UIImage(named: "masks.png")?.cgImage,
            let url = Bundle.main.url(forResource: "skin", withExtension: "json"),
            let data = try? Data(contentsOf: url),
            let meta = try? JSONDecoder().decode(Meta.self, from: data)
        else { return nil }
        let (w, h) = (meta.tile[0], meta.tile[1])
        func tiles(_ atlas: CGImage) -> [String: [CGImage]] {
            meta.keys.compactMapValues { key in
                let pair = (0..<2).compactMap {
                    atlas.cropping(to: CGRect(x: $0 * w, y: key.row * h, width: w, height: h))
                }
                return pair.count == 2 ? pair : nil
            }
        }
        return Skin(
            plate: plate, pad: CGFloat(meta.pad), tile: CGSize(width: w, height: h),
            caps: tiles(keys), masks: tiles(masks))
    }()
}

extension Color {
    init(hex: UInt32) {
        self.init(
            red: Double(hex >> 16 & 0xff) / 255, green: Double(hex >> 8 & 0xff) / 255,
            blue: Double(hex & 0xff) / 255)
    }
}
