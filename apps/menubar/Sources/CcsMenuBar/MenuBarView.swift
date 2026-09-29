import AppKit
import Foundation
import SwiftUI

/// The menu bar popover, modeled on a macOS 26 Control Center module: a bold
/// header with a trailing control, a section of icon-badged rows, one quick
/// toggle, and menu items with shortcut hints. It draws no background of its
/// own, so the system menu window material shows through.
struct MenuBarView: View {
    @ObservedObject var model: MenuBarModel
    /// Off only for snapshot rendering, so a fixture is not refreshed on appear.
    var refreshOnAppear = true

    @State private var listContentHeight: CGFloat?

    @Environment(\.openSettings) private var openSettings
    @Environment(\.dismiss) private var dismiss
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        VStack(spacing: 0) {
            header
            CCSeparator()
            accountsSection
            CCSeparator()
            autoSwitchRow
            CCSeparator()
            menuItems
        }
        .frame(width: CCMetrics.popoverWidth)
        .task {
            if refreshOnAppear && model.rows.isEmpty && !model.isRefreshing { model.refresh() }
        }
    }

    // MARK: Header

    private var header: some View {
        HStack(alignment: .center, spacing: 8) {
            VStack(alignment: .leading, spacing: 1) {
                Text("Claude Accounts")
                    .font(.headline)
                Text(model.activeSummary)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .help(model.activeSummary)
            }
            Spacer(minLength: 8)
            Button(action: { model.refresh() }) {
                CCRefreshGlyph(isRefreshing: model.isRefreshing)
            }
            .buttonStyle(CCCircleButtonStyle())
            .disabled(model.isRefreshing)
            .help("Refresh account usage")
            .accessibilityLabel("Refresh")
            .accessibilityValue(model.isRefreshing ? "Refreshing" : "")
        }
        .padding(.horizontal, CCMetrics.contentInset)
        .padding(.top, 14)
        .padding(.bottom, 11)
    }

    // MARK: Accounts

    private var footerCaption: String {
        if let lastUpdated = model.lastUpdated {
            return "Updated \(lastUpdated.formatted(date: .omitted, time: .shortened))"
        }
        return model.executablePath.map { URL(fileURLWithPath: $0).lastPathComponent } ?? "ccshift not connected"
    }

    /// The load error is already the body of the empty state; every other
    /// message (switch failures, CLI warnings, a missing executable) is shown here.
    private var displayedAlert: String? {
        guard let message = model.alertMessage else { return nil }
        let isShownAsEmptyState = model.rows.isEmpty && !model.isRefreshing && model.executablePath != nil
        return isShownAsEmptyState ? nil : message
    }

    private var accountsSection: some View {
        VStack(alignment: .leading, spacing: 0) {
            CCSectionHeader(title: "Accounts", trailing: footerCaption)
                .padding(.top, 9)
                .padding(.bottom, 3)

            if model.rows.isEmpty {
                emptyState
            } else {
                accountList
            }

            if let alert = displayedAlert {
                CCNotice(text: alert, symbol: "exclamationmark.triangle.fill", tint: .orange, emphasis: .primary)
                    .padding(.horizontal, CCMetrics.contentInset)
                    .padding(.top, 6)
                    .padding(.bottom, 8)
            }
        }
        .padding(.bottom, 2)
    }

    /// Widest meter title ("5h", "Sonnet") across all accounts, so meters align.
    private var usageLabelWidth: CGFloat {
        let titles = ["5h", "7d"] + model.rows.flatMap { ($0.account.visibleUsage?.scoped ?? []).map(\.displayTitle) }
        let attributes: [NSAttributedString.Key: Any] = [.font: NSFont.preferredFont(forTextStyle: .subheadline)]
        let widest = titles.map { ($0 as NSString).size(withAttributes: attributes).width }.max() ?? 0
        return min(ceil(widest), 72)
    }

    /// A `ScrollView` reports its ideal height before wrapped text has laid out,
    /// which clips the last row. The content is measured at its real width and
    /// the list is sized to it, capped at `maxListHeight`.
    private var accountList: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                ForEach(model.rows) { row in
                    AccountRowView(
                        row: row,
                        isSwitching: model.switchingAccountID == row.account.id,
                        usageLabelWidth: usageLabelWidth,
                        switchAction: { model.switchTo(row.account) }
                    )
                }
            }
            .background {
                GeometryReader { proxy in
                    Color.clear.preference(key: AccountListHeightKey.self, value: proxy.size.height)
                }
            }
        }
        .onPreferenceChange(AccountListHeightKey.self) { listContentHeight = $0 }
        .frame(height: listContentHeight.map { min($0, CCMetrics.maxListHeight) })
        .frame(maxHeight: CCMetrics.maxListHeight)
        .scrollBounceBehavior(.basedOnSize)
        // Like Control Center lists: no scroller, even with "Always" scroll
        // bars or a mouse attached, where the legacy scroller would take a
        // 15pt gutter out of the rows. The soft edge shows there is more.
        .scrollIndicators(.never)
        .ccSoftScrollEdges()
    }

    private var emptyState: some View {
        VStack(spacing: 6) {
            if model.isRefreshing {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text("Loading accounts and usage…")
                        .font(.body)
                        .foregroundStyle(.secondary)
                }
                .accessibilityElement(children: .combine)
            } else if model.executablePath == nil {
                emptyStateText(
                    symbol: "terminal",
                    title: "Connect ccshift",
                    message: "Install ccshift (uv tool install git+https://github.com/nam-sequence/ccshift) or choose the installed ccshift command."
                )
                Button("Choose ccshift executable…", action: model.chooseExecutable)
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                    .padding(.top, 4)
            } else if let error = model.alertMessage {
                emptyStateText(symbol: "exclamationmark.triangle", title: "Could not load accounts", message: error)
                Button("Try again") { model.refresh() }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                    .padding(.top, 4)
            } else {
                emptyStateText(
                    symbol: "person.crop.circle.badge.plus",
                    title: "No accounts configured",
                    message: "Run ccshift add to register an account, then refresh this menu."
                )
            }
        }
        .frame(maxWidth: .infinity)
        .padding(.horizontal, CCMetrics.contentInset)
        .padding(.vertical, 16)
    }

    @ViewBuilder
    private func emptyStateText(symbol: String, title: String, message: String) -> some View {
        Image(systemName: symbol)
            .font(.system(size: 22, weight: .regular))
            .foregroundStyle(.secondary)
            .accessibilityHidden(true)
        Text(title)
            .font(.headline)
        Text(message)
            .font(.subheadline)
            .foregroundStyle(.secondary)
            .multilineTextAlignment(.center)
            .fixedSize(horizontal: false, vertical: true)
    }

    // MARK: Automatic switching

    private var autoSwitchRow: some View {
        HStack(alignment: .top, spacing: CCMetrics.badgeSpacing) {
            CCBadge(symbol: "arrow.triangle.2.circlepath", isOn: model.autoSwitchEnabled)
            VStack(alignment: .leading, spacing: 2) {
                HStack(alignment: .center, spacing: 8) {
                    // The switch below carries the accessibility label; reading
                    // the visible title too would announce the name twice.
                    Text("Automatic Switching")
                        .font(.body)
                        .accessibilityHidden(true)
                    Spacer(minLength: 8)
                    Toggle(
                        "Automatic Switching",
                        isOn: Binding(
                            get: { model.autoSwitchEnabled },
                            set: { model.setAutoSwitchEnabled($0) }
                        )
                    )
                    .labelsHidden()
                    .toggleStyle(.switch)
                    .controlSize(.small)
                }
                .frame(minHeight: CCMetrics.badgeSize)
                autoSwitchStatus
            }
        }
        .padding(.horizontal, CCMetrics.contentInset)
        .padding(.vertical, 8)
        .animation(reduceMotion ? nil : .smooth(duration: 0.25), value: model.autoSwitchEnabled)
    }

    @ViewBuilder
    private var autoSwitchStatus: some View {
        if let paused = model.autoSwitchAvailabilityMessage {
            CCNotice(text: paused, symbol: "pause.circle", tint: .orange)
        } else if model.autoSwitchIsRunning {
            HStack(spacing: 5) {
                ProgressView().controlSize(.mini)
                Text("Checking account usage…")
            }
            .font(.subheadline)
            .foregroundStyle(.secondary)
            .accessibilityElement(children: .combine)
        } else if model.autoSwitchEnabled, let result = model.autoSwitchLastResult {
            Text(result)
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .lineLimit(2)
                .fixedSize(horizontal: false, vertical: true)
                .help(result)
        }
    }

    // MARK: Menu items

    private var menuItems: some View {
        VStack(spacing: 0) {
            CCMenuRow(title: "ccshift Settings…", shortcut: KeyboardShortcut(",", modifiers: .command)) {
                openSettingsWindow()
            }
            CCMenuRow(title: "Quit ccshift", shortcut: KeyboardShortcut("q", modifiers: .command)) {
                model.shutdown()
                NSApp.terminate(nil)
            }
        }
        .padding(.vertical, 5)
    }

    /// An accessory app is never frontmost by itself, so a plain
    /// `openSettings()` can leave the window behind other apps. Order matters:
    /// close the popover, activate the app, open the scene, then raise it.
    ///
    /// `NSApp.activate()` is cooperative and, per its header, not guaranteed to
    /// activate the app at all. `activate(ignoringOtherApps:)` activates
    /// regardless and is available on every supported system.
    private func openSettingsWindow() {
        dismiss()
        NSApp.activate(ignoringOtherApps: true)
        openSettings()
        SettingsWindowPresenter.raiseWhenReady()
    }
}

private struct AccountListHeightKey: PreferenceKey {
    static let defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) {
        value = max(value, nextValue())
    }
}

/// Brings the SwiftUI Settings window to the front once `openSettings()` has
/// created it. If the window cannot be found this does nothing; activation and
/// `openSettings()` have already done the required work.
@MainActor
enum SettingsWindowPresenter {
    static func raiseWhenReady() {
        Task { @MainActor in
            for _ in 0..<20 {
                if let window = NSApp.windows.first(where: isSettingsWindow) {
                    NSApp.activate(ignoringOtherApps: true)
                    window.makeKeyAndOrderFront(nil)
                    // Without this the window can open behind another app's
                    // windows while the app is still becoming active.
                    window.orderFrontRegardless()
                    return
                }
                try? await Task.sleep(for: .milliseconds(25))
            }
        }
    }

    private static func isSettingsWindow(_ window: NSWindow) -> Bool {
        window.identifier?.rawValue.localizedCaseInsensitiveContains("settings") == true
    }
}

#Preview("Menu bar · loaded") {
    MenuBarView(model: .preview, refreshOnAppear: false)
}

#Preview("Menu bar · switching, auto-switch on") {
    var state = MenuBarModel.PreviewState()
    state.switchingAccountID = "account:2"
    state.autoSwitchEnabled = true
    state.autoSwitchLastResult = "Stayed on main: no other account has room. Threshold: 90.0%"
    return MenuBarView(model: .preview(state), refreshOnAppear: false)
}

#Preview("Menu bar · not connected") {
    var state = MenuBarModel.PreviewState()
    state.accounts = []
    state.executablePath = nil
    state.lastUpdated = nil
    state.alertMessage = CLIError.executableNotFound.localizedDescription
    return MenuBarView(model: .preview(state), refreshOnAppear: false)
}
