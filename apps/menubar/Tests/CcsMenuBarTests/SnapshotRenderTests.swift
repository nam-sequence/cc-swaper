import AppKit
import SwiftUI
import XCTest
@testable import CcsMenuBar

/// Renders the popover and Settings panes to PNGs so the design can be
/// reviewed without launching the app. Skipped unless `CCS_SNAPSHOT_DIR` is set:
///
///     CCS_SNAPSHOT_DIR=/tmp/snaps swift test --filter SnapshotRenderTests
///
/// Views are hosted in an offscreen borderless window with a forced light or
/// dark appearance and drawn with `cacheDisplay`, which (unlike `ImageRenderer`)
/// renders real AppKit controls such as switches, sliders and bordered buttons.
/// The system menu material does not render offscreen, so a semantic window
/// background stands in behind the popover.
/// A borderless window that reports itself as the key and main window, so
/// controls draw in their active state (accent-colored switches) as they do in
/// the real menu window.
private final class SnapshotWindow: NSWindow {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { true }
    override var isKeyWindow: Bool { true }
    override var isMainWindow: Bool { true }
}

/// Reports the app as active so AppKit controls tint with the accent color
/// instead of drawing their inactive grey look (a test runner is never frontmost).
private final class SnapshotApplication: NSApplication {
    override var isActive: Bool { true }
}

@MainActor
final class SnapshotRenderTests: XCTestCase {
    private var outputDirectory: URL!

    override func setUp() async throws {
        guard let path = ProcessInfo.processInfo.environment["CCS_SNAPSHOT_DIR"], !path.isEmpty else {
            throw XCTSkip("Set CCS_SNAPSHOT_DIR to render UI snapshots.")
        }
        outputDirectory = URL(fileURLWithPath: path, isDirectory: true)
        try FileManager.default.createDirectory(at: outputDirectory, withIntermediateDirectories: true)
        // Controls draw their inactive (grey) look unless the app is active.
        _ = SnapshotApplication.shared
        NSApp.setActivationPolicy(.accessory)
    }

    // MARK: Popover

    func testPopoverLoaded() throws {
        try popover("popover-loaded", .init())
    }

    func testPopoverMessageOnlyRow() throws {
        var state = MenuBarModel.PreviewState()
        state.accounts = [MenuBarModel.previewAccounts[0], MenuBarModel.previewMessageOnlyAccount]
        try popover("popover-message-only-row", state)
    }

    func testPopoverHoverOnSettings() throws {
        try popover("popover-hover-settings", .init(), hover: "ccshift Settings…")
    }

    func testPopoverRefreshing() throws {
        var state = MenuBarModel.PreviewState()
        state.isRefreshing = true
        try popover("popover-refreshing", state)
    }

    func testPopoverSwitching() throws {
        var state = MenuBarModel.PreviewState()
        state.switchingAccountID = "account:2"
        try popover("popover-switching", state)
    }

    func testPopoverAutoSwitchOnWithResultAndAlert() throws {
        var state = MenuBarModel.PreviewState()
        state.autoSwitchEnabled = true
        state.autoSwitchLastResult = "Stayed on main: no other account has room. Threshold: 90.0%"
        state.alertMessage = "ccshift could not read usage for work; showing the last good numbers."
        try popover("popover-autoswitch-result-alert", state)
    }

    func testPopoverAutoSwitchRunning() throws {
        var state = MenuBarModel.PreviewState()
        state.autoSwitchEnabled = true
        state.autoSwitchIsRunning = true
        try popover("popover-autoswitch-running", state)
    }

    func testPopoverAutoSwitchPausedNoAccounts() throws {
        var state = MenuBarModel.PreviewState()
        state.accounts = []
        state.autoSwitchEnabled = true
        try popover("popover-autoswitch-paused", state)
    }

    func testPopoverAutoSwitchPausedRosterFailed() throws {
        var state = MenuBarModel.PreviewState()
        state.accounts = []
        state.autoSwitchEnabled = true
        state.rosterReadFailed = true
        state.alertMessage = "Could not read ccshift data. Update ccshift and try again."
        try popover("popover-autoswitch-paused-failed", state)
    }

    func testPopoverEmptyNotConnected() throws {
        var state = MenuBarModel.PreviewState()
        state.accounts = []
        state.executablePath = nil
        state.lastUpdated = nil
        state.alertMessage = CLIError.executableNotFound.localizedDescription
        try popover("popover-empty-not-connected", state)
    }

    func testPopoverEmptyLoadError() throws {
        var state = MenuBarModel.PreviewState()
        state.accounts = []
        state.lastUpdated = nil
        state.alertMessage = "Could not read ccshift data. Update ccshift and try again."
        try popover("popover-empty-load-error", state)
    }

    func testPopoverEmptyNoAccounts() throws {
        var state = MenuBarModel.PreviewState()
        state.accounts = []
        state.lastUpdated = nil
        try popover("popover-empty-no-accounts", state)
    }

    func testPopoverEmptyLoading() throws {
        var state = MenuBarModel.PreviewState()
        state.accounts = []
        state.lastUpdated = nil
        state.isRefreshing = true
        try popover("popover-empty-loading", state)
    }

    func testPopoverLoadingUsageForKnownAccounts() throws {
        var state = MenuBarModel.PreviewState()
        state.isRefreshing = true
        state.accounts = MenuBarModel.previewAccounts.map { account in
            Account(
                id: account.id, number: account.number, email: account.email,
                organizationName: account.organizationName, alias: account.alias,
                active: account.active, disabled: account.disabled, usageStatus: "unavailable",
                usage: nil, usageAgeSeconds: nil, lastGoodUsage: nil, lastGoodAgeSeconds: nil
            )
        }
        try popover("popover-loading-usage", state)
    }

    func testPopoverManyAccountsScrolls() throws {
        var state = MenuBarModel.PreviewState()
        state.accounts = MenuBarModel.previewManyAccounts
        try popover("popover-many-accounts", state)
    }

    func testPopoverHighContrast() throws {
        var state = MenuBarModel.PreviewState()
        state.autoSwitchEnabled = true
        state.autoSwitchLastResult = "Stayed on main: no other account has room. Threshold: 90.0%"
        try render(
            "popover-increased-contrast",
            chrome: .popover,
            appearances: [
                ("light", NSAppearance.Name.accessibilityHighContrastAqua),
                ("dark", NSAppearance.Name.accessibilityHighContrastDarkAqua),
            ]
        ) {
            MenuBarView(model: .preview(state), refreshOnAppear: false)
                .environment(\.ccForceEmphasis, true)
        }
    }

    // MARK: Settings

    func testSettingsGeneral() throws {
        try settings("settings-general", .init(), pane: .general)
    }

    func testSettingsGeneralNeedsApproval() throws {
        var state = MenuBarModel.PreviewState()
        state.launchAtLoginStatus = .requiresApproval
        try settings("settings-general-approval", state, pane: .general)
    }

    func testSettingsGeneralNotConnectedAndError() throws {
        var state = MenuBarModel.PreviewState()
        state.executablePath = nil
        state.launchAtLoginError = "Could not update Launch at Login. Open System Settings → General → Login Items and try again."
        try settings("settings-general-error", state, pane: .general)
    }

    func testSettingsGeneralChooseExecutableRejected() throws {
        var state = MenuBarModel.PreviewState()
        state.alertMessage = CLIError.invalidExecutablePath.localizedDescription
        try settings("settings-general-choose-error", state, pane: .general)
    }

    func testSettingsGeneralIgnoresUnrelatedAlert() throws {
        var state = MenuBarModel.PreviewState()
        state.alertMessage = "ccshift could not read usage for work; showing the last good numbers."
        try settings("settings-general-unrelated-alert", state, pane: .general)
    }

    func testSettingsAutomaticSwitchingOff() throws {
        try settings("settings-auto-off", .init(), pane: .automaticSwitching)
    }

    func testSettingsAutomaticSwitchingOnWithStatus() throws {
        var state = MenuBarModel.PreviewState()
        state.autoSwitchEnabled = true
        state.autoSwitchThreshold = 85
        state.autoSwitchDryRun = true
        state.autoSwitchIsRunning = true
        state.autoSwitchLastResult = "Would switch main to work (dry run). Threshold: 85.0%"
        try settings("settings-auto-on", state, pane: .automaticSwitching)
    }

    func testSettingsAutomaticSwitchingPaused() throws {
        var state = MenuBarModel.PreviewState()
        state.accounts = []
        state.autoSwitchEnabled = true
        try settings("settings-auto-paused", state, pane: .automaticSwitching)
    }

    func testSettingsWholeWindowContent() throws {
        try render("settings-tabview", chrome: .settings) {
            SettingsView(model: .preview(.init()))
        }
    }

    // MARK: Scenarios

    private func popover(_ name: String, _ state: MenuBarModel.PreviewState, hover: String? = nil) throws {
        try render(name, chrome: .popover) {
            MenuBarView(model: .preview(state), refreshOnAppear: false)
                .environment(\.ccForcedHoverTitle, hover)
        }
    }

    private func settings(
        _ name: String,
        _ state: MenuBarModel.PreviewState,
        pane: SettingsView.Tab
    ) throws {
        let model = MenuBarModel.preview(state)
        try render(name, chrome: .settings) {
            switch pane {
            case .general:
                GeneralSettingsPane(model: model)
            case .automaticSwitching:
                AutomaticSwitchingSettingsPane(model: model)
            }
        }
    }

    // MARK: Rendering

    private enum Chrome {
        /// A rounded stand-in for the menu window, floating on the desktop.
        case popover
        /// A plain window background.
        case settings
    }

    private struct ChromeView<Content: View>: View {
        let chrome: Chrome
        let content: Content

        var body: some View {
            switch chrome {
            case .popover:
                content
                    .background(Color(nsColor: .windowBackgroundColor))
                    .clipShape(RoundedRectangle(cornerRadius: 22, style: .continuous))
                    .overlay(
                        RoundedRectangle(cornerRadius: 22, style: .continuous)
                            .strokeBorder(Color(nsColor: .separatorColor), lineWidth: 0.5)
                    )
                    .padding(28)
                    .background(Color(nsColor: .underPageBackgroundColor))
            case .settings:
                content
                    .background(Color(nsColor: .windowBackgroundColor))
            }
        }
    }

    private static let defaultAppearances: [(String, NSAppearance.Name)] = [
        ("light", .aqua),
        ("dark", .darkAqua),
    ]

    private func render<Content: View>(
        _ name: String,
        chrome: Chrome,
        appearances: [(String, NSAppearance.Name)] = SnapshotRenderTests.defaultAppearances,
        @ViewBuilder content: () -> Content
    ) throws {
        let view = ChromeView(chrome: chrome, content: content())
        for (suffix, appearanceName) in appearances {
            let hosting = NSHostingView(rootView: view)
            hosting.sizingOptions = [.intrinsicContentSize]
            let window = SnapshotWindow(
                contentRect: NSRect(x: -20_000, y: -20_000, width: 800, height: 1_200),
                styleMask: [.borderless],
                backing: .buffered,
                defer: false
            )
            window.appearance = NSAppearance(named: appearanceName)
            window.contentView = hosting
            window.isReleasedWhenClosed = false
            window.makeKeyAndOrderFront(nil)
            // Two passes: measured heights (preferences) land after the first
            // layout, exactly as they do when the menu window resizes to fit.
            var size = hosting.fittingSize
            for _ in 0..<3 {
                window.setContentSize(size)
                hosting.frame = NSRect(origin: .zero, size: size)
                hosting.layoutSubtreeIfNeeded()
                RunLoop.current.run(until: Date().addingTimeInterval(0.2))
                hosting.layoutSubtreeIfNeeded()
                size = hosting.fittingSize
            }
            window.setContentSize(size)
            hosting.frame = NSRect(origin: .zero, size: size)
            hosting.layoutSubtreeIfNeeded()
            RunLoop.current.run(until: Date().addingTimeInterval(0.35))
            hosting.displayIfNeeded()

            let scale: CGFloat = 2
            guard let rep = NSBitmapImageRep(
                bitmapDataPlanes: nil,
                pixelsWide: Int(size.width * scale),
                pixelsHigh: Int(size.height * scale),
                bitsPerSample: 8,
                samplesPerPixel: 4,
                hasAlpha: true,
                isPlanar: false,
                colorSpaceName: .deviceRGB,
                bytesPerRow: 0,
                bitsPerPixel: 0
            ) else {
                XCTFail("Could not allocate a bitmap for \(name)")
                continue
            }
            rep.size = size
            hosting.cacheDisplay(in: hosting.bounds, to: rep)
            let png = try XCTUnwrap(rep.representation(using: .png, properties: [:]))
            let url = outputDirectory.appendingPathComponent("\(name)-\(suffix).png")
            try png.write(to: url)
            XCTAssertGreaterThan(size.width, 100, "\(name) \(suffix) collapsed")
            XCTAssertGreaterThan(size.height, 100, "\(name) \(suffix) collapsed")
            window.close()
        }
    }
}
