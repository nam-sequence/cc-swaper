import AppKit
import Foundation
import SwiftUI

struct MenuBarView: View {
    @ObservedObject var model: MenuBarModel

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider().padding(.vertical, 10)

            if model.rows.isEmpty {
                emptyState
            } else {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 14) {
                        accountSection(.engine)
                        accountSection(.legacy)
                    }
                    .padding(.vertical, 1)
                }
                .frame(maxHeight: 420)
            }

            if let alertMessage = model.alertMessage {
                Label(alertMessage, systemImage: "exclamationmark.triangle.fill")
                    .font(.caption)
                    .foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.top, 10)
            }

            Divider().padding(.vertical, 10)
            settings
            Divider().padding(.vertical, 10)
            footer
        }
        .padding(14)
        .frame(width: 360)
        .task {
            if model.rows.isEmpty && !model.isRefreshing { model.refresh() }
        }
    }

    private var header: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "person.crop.circle.fill")
                .font(.system(size: 23))
                .foregroundStyle(.tint)
            VStack(alignment: .leading, spacing: 3) {
                Text("Claude accounts")
                    .font(.headline)
                Text(model.activeSummary)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
                Text("Claude launches: \(model.launchBackendTitle)")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }
            Spacer(minLength: 4)
            Button(action: { model.refresh() }) {
                if model.isRefreshing {
                    ProgressView().controlSize(.small)
                        .frame(width: 17, height: 17)
                } else {
                    Image(systemName: "arrow.clockwise")
                }
            }
            .buttonStyle(.borderless)
            .disabled(model.isRefreshing)
            .help("Refresh account usage")
        }
    }

    @ViewBuilder
    private func accountSection(_ mode: BackendMode) -> some View {
        let accounts = model.rows.filter { $0.mode == mode }
        if !accounts.isEmpty {
            VStack(alignment: .leading, spacing: 8) {
                Text(mode.rawValue)
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                ForEach(accounts) { row in
                    AccountCard(
                        row: row,
                        isSwitching: model.switchingAccountID == row.account.id,
                        switchAction: { model.switchTo(row.account) }
                    )
                }
            }
        }
    }

    private var settings: some View {
        DisclosureGroup("Settings") {
            VStack(alignment: .leading, spacing: 9) {
                Toggle(
                    "Launch at login",
                    isOn: Binding(
                        get: { model.launchAtLoginRequested },
                        set: { model.setLaunchAtLoginEnabled($0) }
                    )
                )
                .disabled(model.launchAtLoginStatus == .notFound)

                if model.launchAtLoginStatus == .requiresApproval || model.launchAtLoginError != nil {
                    Button("Open Login Items Settings", action: model.openLoginItemsSettings)
                        .buttonStyle(.link)
                        .font(.caption)
                }
                if let message = model.launchAtLoginMessage {
                    Text(message)
                        .font(.caption2)
                        .foregroundStyle(model.launchAtLoginError == nil ? Color.secondary : Color.orange)
                        .fixedSize(horizontal: false, vertical: true)
                }

                Divider()

                Toggle(
                    "Automatic account switching",
                    isOn: Binding(
                        get: { model.autoSwitchEnabled },
                        set: { model.setAutoSwitchEnabled($0) }
                    )
                )
                Text("When enabled, ccs checks usage every minute and chooses whether to switch.")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                if let paused = model.autoSwitchAvailabilityMessage {
                    Label(paused, systemImage: "pause.circle")
                        .font(.caption2)
                        .foregroundStyle(.orange)
                        .fixedSize(horizontal: false, vertical: true)
                }
                HStack(spacing: 8) {
                    Text("Threshold")
                        .font(.caption)
                    Slider(
                        value: Binding(
                            get: { model.autoSwitchThreshold },
                            set: { model.setAutoSwitchThreshold($0) }
                        ),
                        in: 50...99.9,
                        step: 1
                    )
                    Text("\(model.autoSwitchThreshold, specifier: "%.1f")%")
                        .font(.caption.monospacedDigit())
                        .frame(width: 36, alignment: .trailing)
                }
                Toggle(
                    "Dry run (never switch)",
                    isOn: Binding(
                        get: { model.autoSwitchDryRun },
                        set: { model.setAutoSwitchDryRun($0) }
                    )
                )
                .disabled(!model.autoSwitchEnabled)

                if model.autoSwitchIsRunning {
                    Label("Checking account usage…", systemImage: "arrow.clockwise")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
                if let result = model.autoSwitchLastResult {
                    Text(result)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .padding(.top, 8)
        }
        .font(.subheadline.weight(.medium))
    }

    private var emptyState: some View {
        VStack(alignment: .leading, spacing: 8) {
            if model.isRefreshing {
                ProgressView("Loading accounts and usage…")
                    .controlSize(.small)
            } else if model.executablePath == nil {
                Text("Connect ccs")
                    .font(.headline)
                Text("Choose the installed ccs command to view accounts and usage.")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                Button("Choose ccs executable…", action: model.chooseExecutable)
                    .padding(.top, 3)
            } else if let error = model.alertMessage {
                Text("Could not load accounts")
                    .font(.headline)
                Text(error)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                Button("Try again") { model.refresh() }
                    .padding(.top, 3)
            } else {
                Text("No accounts configured")
                    .font(.headline)
                Text("Add an account with ccs, then refresh this menu.")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.vertical, 14)
    }

    private var footer: some View {
        HStack(spacing: 14) {
            if let lastUpdated = model.lastUpdated {
                Text("Updated \(lastUpdated.formatted(date: .omitted, time: .shortened))")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                    .lineLimit(1)
            } else {
                Text(model.executablePath.map { URL(fileURLWithPath: $0).lastPathComponent } ?? "ccs not connected")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                    .lineLimit(1)
            }
            Spacer(minLength: 0)
            Button("Choose CLI…", action: model.chooseExecutable)
                .buttonStyle(.link)
                .font(.caption)
            Button("Quit") {
                model.shutdown()
                NSApp.terminate(nil)
            }
                .buttonStyle(.link)
                .font(.caption)
        }
    }
}

private struct AccountCard: View {
    let row: AccountRow
    let isSwitching: Bool
    let switchAction: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 6) {
                        Text(row.account.displayName)
                            .font(.system(.subheadline, design: .rounded, weight: .semibold))
                            .lineLimit(1)
                            .truncationMode(.middle)
                    if row.isActive {
                        Text("ACTIVE")
                                .font(.system(size: 9, weight: .bold, design: .rounded))
                                .foregroundStyle(.green)
                        }
                    }
                    if let organization = row.account.organizationName, !organization.isEmpty {
                        Text(organization)
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    } else if row.account.displayName != row.account.email {
                        Text(row.account.email)
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .truncationMode(.middle)
                    }
                }
                Spacer(minLength: 4)
                if row.isStale {
                    Label("Stale", systemImage: "clock.arrow.circlepath")
                        .font(.caption2)
                        .foregroundStyle(.orange)
                        .labelStyle(.titleAndIcon)
                }
                if row.isActive {
                    Image(systemName: "checkmark.circle.fill")
                        .foregroundStyle(.green)
                        .accessibilityLabel("Active account")
                } else if isSwitching {
                    ProgressView().controlSize(.small)
                        .accessibilityLabel("Switching account")
                } else {
                    Button("Switch", action: switchAction)
                        .buttonStyle(.bordered)
                        .controlSize(.small)
                        .disabled(row.account.disabled == true)
                }
            }

            if let usage = row.account.visibleUsage {
                UsageWindowView(title: "5-hour", window: usage.fiveHour, isLoading: row.isLoading, isStale: row.isStale)
                UsageWindowView(title: "7-day", window: usage.sevenDay, isLoading: row.isLoading, isStale: row.isStale)
                ForEach(Array((usage.scoped ?? []).enumerated()), id: \.offset) { _, scoped in
                    UsageWindowView(
                        title: scoped.label ?? scoped.name ?? scoped.model ?? "Other",
                        window: UsageWindow(pct: scoped.pct, resetsAt: scoped.resetsAt),
                        isLoading: row.isLoading,
                        isStale: row.isStale
                    )
                }
                if let age = row.isStale
                    ? (row.account.lastGoodAgeSeconds ?? row.account.usageAgeSeconds)
                    : (row.account.usageAgeSeconds ?? row.account.lastGoodAgeSeconds) {
                    Text("\(row.isStale ? "Last good usage" : "Usage data") · \(ageText(age)) ago")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                        .lineLimit(1)
                } else {
                    Text("Usage age unavailable")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }
                if let message = row.message {
                    Label(message, systemImage: "exclamationmark.circle")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            } else if row.isLoading {
                ProgressView("Loading usage…")
                    .controlSize(.small)
                    .frame(maxWidth: .infinity, alignment: .leading)
            } else if let message = row.message {
                Label(message, systemImage: "exclamationmark.circle")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                Text(row.account.disabled == true ? "Account disabled" : "Usage unavailable")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(11)
        .background(.quaternary.opacity(0.45), in: RoundedRectangle(cornerRadius: 10))
        .overlay {
            RoundedRectangle(cornerRadius: 10)
                .strokeBorder(row.isActive ? Color.accentColor.opacity(0.45) : .clear, lineWidth: 1)
        }
    }

    private func ageText(_ seconds: Double) -> String {
        let duration = max(0, Int(seconds))
        if duration < 60 { return "\(duration)s" }
        if duration < 3_600 { return "\(duration / 60)m" }
        return "\(duration / 3_600)h"
    }
}

private struct UsageWindowView: View {
    let title: String
    let window: UsageWindow?
    let isLoading: Bool
    var isStale = false

    var body: some View {
        HStack(spacing: 8) {
            Text(title)
                .font(.caption)
                .foregroundStyle(.secondary)
                .frame(width: 47, alignment: .leading)

            if let percent = window?.pct {
                ProgressView(value: min(max(percent, 0), 100), total: 100)
                    .tint(isStale ? .secondary : color(for: percent))
                Text("\(Int(percent.rounded()))%")
                    .font(.system(.caption, design: .monospaced))
                    .frame(width: 34, alignment: .trailing)
                if let reset = window?.resetsAt, !reset.isEmpty {
                    Text(reset)
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                        .lineLimit(1)
                        .frame(maxWidth: 104, alignment: .trailing)
                        .help(reset)
                } else {
                    Text("Reset unavailable")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                        .lineLimit(1)
                        .frame(maxWidth: 104, alignment: .trailing)
                }
            } else if isLoading {
                ProgressView().controlSize(.mini)
                    .frame(maxWidth: .infinity)
                Text("—")
                    .font(.caption2.monospaced())
                    .foregroundStyle(.tertiary)
                    .frame(width: 34, alignment: .trailing)
            } else {
                ProgressView(value: 0)
                Text("—")
                    .font(.caption2.monospaced())
                    .foregroundStyle(.tertiary)
                    .frame(width: 34, alignment: .trailing)
                Text("Unavailable")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                    .lineLimit(1)
                    .frame(maxWidth: 104, alignment: .trailing)
            }
        }
        .accessibilityElement(children: .combine)
    }

    private func color(for percent: Double) -> Color {
        if percent >= 90 { return .orange }
        return .accentColor
    }

}

#Preview("Menu bar · loaded") {
    MenuBarView(model: .preview)
}

#Preview("Menu bar · legacy profiles") {
    MenuBarView(model: .legacyPreview)
}
