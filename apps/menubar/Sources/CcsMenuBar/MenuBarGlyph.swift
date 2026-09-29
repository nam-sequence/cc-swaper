import AppKit

/// The menu bar glyph: the app icon's selector dial (a knob pointing at the
/// active one of three positions), drawn as a template image so the system
/// tints it like its own menu bar items.
@MainActor
enum MenuBarGlyph {
    static let image: NSImage = {
        let image = NSImage(size: NSSize(width: 18, height: 18), flipped: true) { _ in
            let center = NSPoint(x: 9, y: 10.5)
            NSColor.black.setFill()
            NSColor.black.setStroke()

            let knobRadius = 5.25
            NSBezierPath(ovalIn: NSRect(
                x: center.x - knobRadius, y: center.y - knobRadius,
                width: knobRadius * 2, height: knobRadius * 2
            )).fill()

            // The pointer is cut out of the knob, toward the active position.
            let pointerAngle = 52 * Double.pi / 180
            let pointer = NSBezierPath()
            pointer.move(to: MenuBarGlyph.point(from: center, angle: pointerAngle, distance: 1.4))
            pointer.line(to: MenuBarGlyph.point(from: center, angle: pointerAngle, distance: 4.1))
            pointer.lineWidth = 1.7
            pointer.lineCapStyle = .round
            NSGraphicsContext.current?.compositingOperation = .destinationOut
            pointer.stroke()
            NSGraphicsContext.current?.compositingOperation = .sourceOver

            // Three positions on an arc above the knob; the active one is larger.
            for (degrees, radius) in [(-52.0, 1.0), (0, 1.0), (52, 1.45)] {
                let dot = MenuBarGlyph.point(from: center, angle: degrees * Double.pi / 180, distance: 7.6)
                NSBezierPath(ovalIn: NSRect(x: dot.x - radius, y: dot.y - radius, width: radius * 2, height: radius * 2)).fill()
            }
            return true
        }
        image.isTemplate = true
        image.accessibilityDescription = "ccshift"
        return image
    }()

    /// A point `distance` from `center`, `angle` radians clockwise from straight up.
    nonisolated private static func point(from center: NSPoint, angle: Double, distance: Double) -> NSPoint {
        NSPoint(x: center.x + sin(angle) * distance, y: center.y - cos(angle) * distance)
    }
}
