import AppKit
import SwiftUI

// Building blocks for the Control Center–style popover (the Wi-Fi / Bluetooth /
// Battery modules of macOS 26). Everything is drawn with semantic styles only:
// hierarchical foreground styles, the accent color, system status colors and
// NSColor system fills. Nothing paints an opaque background, so the system menu
// material (Liquid Glass on macOS 26) stays visible behind the content.

enum CCMetrics {
    static let popoverWidth: CGFloat = 320
    /// Leading and trailing inset of content, as in Control Center modules.
    static let contentInset: CGFloat = 14
    /// Hover highlights float this far from the window edge.
    static let hoverInset: CGFloat = 5
    static let highlightRadius: CGFloat = 10
    static let badgeSize: CGFloat = 28
    static let badgeSpacing: CGFloat = 10
    static let maxListHeight: CGFloat = 420
}

/// System fills used for tracks, badges and hover highlights. Increase Contrast
/// and Reduce Transparency both step every fill up one level, so shapes stay
/// distinguishable without a translucent backdrop.
///
/// The tiers follow the NSColor guidance: `quaternarySystemFill` is meant for
/// large areas (a group box) and is nearly invisible on a small badge or track,
/// so shapes here start one tier higher.
struct CCFill {
    let emphasized: Bool
    let increasedContrast: Bool

    /// Off-state badges and circular controls.
    var quiet: Color { Color(nsColor: emphasized ? .secondarySystemFill : .tertiarySystemFill) }
    /// The unfilled part of a thin meter ("progress indicator backing").
    var track: Color { Color(nsColor: emphasized ? .systemFill : .secondarySystemFill) }
    var hover: Color { Color(nsColor: emphasized ? .systemFill : .secondarySystemFill) }
    var pressed: Color { Color(nsColor: .systemFill) }
}

private struct CCForceEmphasisKey: EnvironmentKey {
    static let defaultValue = false
}

extension EnvironmentValues {
    /// Snapshot hook: renders as if Increase Contrast and Reduce Transparency
    /// were on. `colorSchemeContrast` and friends are read-only, so tests
    /// cannot inject them.
    var ccForceEmphasis: Bool {
        get { self[CCForceEmphasisKey.self] }
        set { self[CCForceEmphasisKey.self] = newValue }
    }
}

/// Resolves `CCFill` from the accessibility environment. Use as
/// `@CCAdaptiveFill private var fill`.
@propertyWrapper
struct CCAdaptiveFill: DynamicProperty {
    @Environment(\.colorSchemeContrast) private var contrast
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency
    @Environment(\.ccForceEmphasis) private var forced

    var wrappedValue: CCFill {
        let increased = forced || contrast == .increased
        return CCFill(emphasized: increased || reduceTransparency, increasedContrast: increased)
    }
}

// MARK: - Badge

/// The circular icon tile of a Control Center row: accent fill with a white
/// glyph when on (a connected network), a neutral system fill when off.
struct CCBadge: View {
    let symbol: String
    let isOn: Bool
    var size: CGFloat = CCMetrics.badgeSize

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @CCAdaptiveFill private var fill

    var body: some View {
        Image(systemName: symbol)
            .font(.system(size: size * 0.46, weight: .semibold))
            .foregroundStyle(isOn ? AnyShapeStyle(Color(nsColor: .alternateSelectedControlTextColor)) : AnyShapeStyle(.secondary))
            .frame(width: size, height: size)
            .background(Circle().fill(isOn ? Color.accentColor : fill.quiet))
            .animation(reduceMotion ? nil : .smooth(duration: 0.25), value: isOn)
            .accessibilityHidden(true)
    }
}

// MARK: - Section header and separator

struct CCSectionHeader: View {
    let title: String
    var trailing: String?

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(title)
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(.secondary)
            Spacer(minLength: 8)
            if let trailing {
                Text(trailing)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
        }
        .padding(.horizontal, CCMetrics.contentInset)
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isHeader)
    }
}

/// A menu-style separator: `Divider` draws with `separatorColor`; the inset
/// matches the content margins.
struct CCSeparator: View {
    var body: some View {
        Divider().padding(.horizontal, CCMetrics.contentInset)
    }
}

// MARK: - Notice

/// A symbol + short sentence. The symbol carries the status color; the text
/// stays in a hierarchical style so it keeps its contrast in both appearances.
struct CCNotice: View {
    let text: String
    let symbol: String
    var tint: Color = .secondary
    var emphasis: HierarchicalShapeStyle = .secondary

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 5) {
            Image(systemName: symbol)
                .foregroundStyle(tint)
                .accessibilityHidden(true)
            Text(text)
                .foregroundStyle(emphasis)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .font(.subheadline)
        .fixedSize(horizontal: false, vertical: true)
        .accessibilityElement(children: .combine)
    }
}

// MARK: - Menu row

extension KeyboardShortcut {
    /// "⌘," style hint in menu order (control, option, shift, command).
    var menuHint: String {
        var hint = ""
        if modifiers.contains(.control) { hint += "⌃" }
        if modifiers.contains(.option) { hint += "⌥" }
        if modifiers.contains(.shift) { hint += "⇧" }
        if modifiers.contains(.command) { hint += "⌘" }
        return hint + String(key.character).uppercased()
    }
}

private struct CCForcedHoverKey: EnvironmentKey {
    static let defaultValue: String? = nil
}

extension EnvironmentValues {
    /// Snapshot hook: the title of the menu row to draw as hovered.
    var ccForcedHoverTitle: String? {
        get { self[CCForcedHoverKey.self] }
        set { self[CCForcedHoverKey.self] = newValue }
    }
}

/// A full-width menu item with a Control Center hover highlight and a trailing
/// shortcut hint in the secondary color.
struct CCMenuRow: View {
    let title: String
    var shortcut: KeyboardShortcut?
    let action: @MainActor () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 8) {
                Text(title)
                Spacer(minLength: 12)
                if let shortcut {
                    Text(shortcut.menuHint)
                        .foregroundStyle(.secondary)
                        .accessibilityHidden(true)
                }
            }
            .font(.body)
            .padding(.horizontal, CCMetrics.contentInset - CCMetrics.hoverInset)
            .padding(.vertical, 5)
            .contentShape(Rectangle())
        }
        .buttonStyle(CCMenuRowStyle(title: title))
        .keyboardShortcut(shortcut)
        .padding(.horizontal, CCMetrics.hoverInset)
    }
}

struct CCMenuRowStyle: ButtonStyle {
    let title: String

    func makeBody(configuration: Configuration) -> some View {
        RowBody(configuration: configuration, title: title)
    }

    private struct RowBody: View {
        let configuration: ButtonStyleConfiguration
        let title: String

        @State private var isHovering = false
        @Environment(\.isEnabled) private var isEnabled
        @Environment(\.ccForcedHoverTitle) private var forcedHoverTitle
        @Environment(\.accessibilityReduceMotion) private var reduceMotion
        @CCAdaptiveFill private var fill

        var body: some View {
            let hovered = isHovering || forcedHoverTitle == title
            configuration.label
                .foregroundStyle(isEnabled ? AnyShapeStyle(.primary) : AnyShapeStyle(.tertiary))
                .background {
                    RoundedRectangle(cornerRadius: CCMetrics.highlightRadius, style: .continuous)
                        .fill(configuration.isPressed ? fill.pressed : (hovered && isEnabled ? fill.hover : Color.clear))
                }
                .animation(reduceMotion ? nil : .easeOut(duration: 0.1), value: hovered)
                .onHover { isHovering = $0 }
        }
    }
}

// MARK: - Circular icon button

/// The small circular control of a module header (refresh).
struct CCCircleButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        CircleBody(configuration: configuration)
    }

    private struct CircleBody: View {
        let configuration: ButtonStyleConfiguration

        @State private var isHovering = false
        @Environment(\.isEnabled) private var isEnabled
        @CCAdaptiveFill private var fill

        var body: some View {
            configuration.label
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(isEnabled ? AnyShapeStyle(.primary) : AnyShapeStyle(.secondary))
                .frame(width: 24, height: 24)
                .background(
                    Circle().fill(
                        configuration.isPressed ? fill.pressed : (isHovering && isEnabled ? fill.hover : fill.quiet)
                    )
                )
                .contentShape(Circle())
                .onHover { isHovering = $0 }
        }
    }
}

// MARK: - Usage bar

/// A compact capsule meter. Decorative on its own: callers attach the
/// accessibility label and value.
struct CCUsageBar: View {
    /// 0...1
    let fraction: Double
    let tint: AnyShapeStyle

    static let height: CGFloat = 5

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @CCAdaptiveFill private var fill

    var body: some View {
        let clamped = min(max(fraction, 0), 1)
        Capsule()
            .fill(fill.track)
            .overlay {
                if fill.increasedContrast {
                    Capsule().strokeBorder(Color(nsColor: .separatorColor), lineWidth: 0.5)
                }
            }
            .overlay(alignment: .leading) {
                GeometryReader { proxy in
                    Capsule()
                        .fill(tint)
                        .frame(width: clamped == 0 ? 0 : max(Self.height, proxy.size.width * clamped))
                        .animation(reduceMotion ? nil : .smooth(duration: 0.4), value: clamped)
                }
            }
            .frame(height: Self.height)
            .accessibilityHidden(true)
    }
}

// MARK: - macOS 26 refinements with macOS 14 fallbacks

extension View {
    /// Soft scroll edges on macOS 26 (content fades under the edge instead of
    /// being cut); earlier systems keep the plain edge.
    @ViewBuilder
    func ccSoftScrollEdges() -> some View {
        if #available(macOS 26, *) {
            scrollEdgeEffectStyle(.soft, for: .vertical)
        } else {
            self
        }
    }
}

/// The refresh glyph. macOS 15+ rotates the symbol while refreshing; macOS 14
/// and Reduce Motion fall back to the standard small spinner.
struct CCRefreshGlyph: View {
    let isRefreshing: Bool

    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        if isRefreshing, reduceMotion || !Self.supportsRotation {
            ProgressView().controlSize(.mini)
        } else if #available(macOS 15, *) {
            Image(systemName: "arrow.clockwise")
                .symbolEffect(.rotate, options: .repeating, isActive: isRefreshing)
        } else {
            Image(systemName: "arrow.clockwise")
        }
    }

    private static var supportsRotation: Bool {
        if #available(macOS 15, *) { true } else { false }
    }
}
