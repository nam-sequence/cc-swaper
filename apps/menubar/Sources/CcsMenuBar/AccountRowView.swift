import SwiftUI

/// One account in the Accounts section: icon badge, name · organization,
/// active check / Switch button / progress, then compact usage meters.
struct AccountRowView: View {
    let row: AccountRow
    let isSwitching: Bool
    /// Shared by every row so the meters line up from account to account.
    var usageLabelWidth: CGFloat = 20
    let switchAction: () -> Void

    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        HStack(alignment: .top, spacing: CCMetrics.badgeSpacing) {
            CCBadge(symbol: "person.fill", isOn: row.isActive)
            VStack(alignment: .leading, spacing: 5) {
                titleRow
                details
            }
        }
        .padding(.horizontal, CCMetrics.contentInset)
        .padding(.vertical, 6)
        .animation(reduceMotion ? nil : .smooth(duration: 0.25), value: row.isActive)
        .animation(reduceMotion ? nil : .smooth(duration: 0.25), value: isSwitching)
        .accessibilityElement(children: .contain)
    }

    // MARK: Title

    /// Organization, else the email when the account is named by an alias:
    /// shown beside the name ("main · Max").
    private var detailText: String? {
        if let organization = row.account.organizationName, !organization.isEmpty {
            return organization
        }
        if !row.account.email.isEmpty, row.account.displayName != row.account.email {
            return row.account.email
        }
        return nil
    }

    private var titleRow: some View {
        HStack(alignment: .center, spacing: 8) {
            VStack(alignment: .leading, spacing: 1) {
                HStack(alignment: .firstTextBaseline, spacing: 4) {
                    Text(row.account.displayName)
                        .font(.body.weight(.medium))
                        .lineLimit(1)
                        .truncationMode(.middle)
                        .layoutPriority(2)
                    if let detailText {
                        Text("·")
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                            .accessibilityHidden(true)
                        Text(detailText)
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .truncationMode(.middle)
                            .layoutPriority(1)
                            .help(detailText)
                    }
                }
                if row.account.disabled {
                    Text("Not in auto-switch")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                        .help("Disabled with ccshift disable. You can still switch to it here.")
                }
            }
            .accessibilityElement(children: .combine)
            .accessibilityValue(row.isActive ? "Active account" : "")

            Spacer(minLength: 4)
            status
        }
        .frame(minHeight: CCMetrics.badgeSize)
    }

    @ViewBuilder
    private var status: some View {
        Group {
            if row.isActive {
                Image(systemName: "checkmark")
                    .font(.body.weight(.semibold))
                    .foregroundStyle(.tint)
                    .help("Active account")
                    .accessibilityHidden(true)
                    .transition(.opacity)
            } else if isSwitching {
                ProgressView()
                    .controlSize(.small)
                    .accessibilityLabel("Switching account")
                    .transition(.opacity)
            } else {
                Button("Switch", action: switchAction)
                    .buttonStyle(.bordered)
                    .buttonBorderShape(.capsule)
                    .controlSize(.small)
                    .accessibilityLabel("Switch to \(row.account.displayName)")
                    .transition(.opacity)
            }
        }
        .frame(minWidth: 52, alignment: .trailing)
    }

    // MARK: Usage

    @ViewBuilder
    private var details: some View {
        if let usage = row.account.visibleUsage {
            VStack(alignment: .leading, spacing: 5) {
                usageGrid(usage)
                ageLine
                if let message = row.message {
                    CCNotice(text: message, symbol: "exclamationmark.circle")
                }
            }
        } else if row.isLoading {
            HStack(spacing: 6) {
                ProgressView().controlSize(.small)
                Text("Loading usage…")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
            .accessibilityElement(children: .combine)
        } else if let message = row.message {
            CCNotice(text: message, symbol: "exclamationmark.circle")
        } else {
            Text("Usage unavailable")
                .font(.subheadline)
                .foregroundStyle(.secondary)
        }
    }

    private func usageGrid(_ usage: AccountUsage) -> some View {
        Grid(alignment: .leading, horizontalSpacing: 8, verticalSpacing: 3) {
            usageRow(title: "5h", accessibilityTitle: "5-hour usage", window: usage.fiveHour)
            usageRow(title: "7d", accessibilityTitle: "7-day usage", window: usage.sevenDay)
            ForEach(Array((usage.scoped ?? []).enumerated()), id: \.offset) { _, scoped in
                let title = scoped.displayTitle
                usageRow(title: title, accessibilityTitle: "\(title) usage", window: scoped.window)
            }
        }
    }

    private func usageRow(title: String, accessibilityTitle: String, window: UsageWindow?) -> GridRow<some View> {
        let percent = window?.pct
        let reset = window?.resetLabel()
        return GridRow {
            Text(title)
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .truncationMode(.tail)
                .frame(width: usageLabelWidth, alignment: .leading)
                .help(accessibilityTitle)
                .accessibilityHidden(true)

            if let percent {
                CCUsageBar(fraction: percent / 100, tint: barTint(percent))
                    .frame(minWidth: 56)
                    .accessibilityElement()
                    .accessibilityLabel(accessibilityTitle)
                    .accessibilityValue(spokenValue(percent: percent, reset: reset))
                Text("\(Int(percent.rounded()))%")
                    .font(.subheadline.monospacedDigit())
                    .contentTransition(.numericText(value: percent))
                    .animation(reduceMotion ? nil : .snappy, value: percent)
                    .frame(minWidth: 34, alignment: .trailing)
                    .accessibilityHidden(true)
                Text(reset ?? "Reset unavailable")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .frame(minWidth: 64, alignment: .trailing)
                    .help(window?.resetTooltip ?? reset ?? "Reset unavailable")
                    .accessibilityHidden(true)
            } else if row.isLoading {
                ProgressView()
                    .controlSize(.mini)
                    .frame(maxWidth: .infinity, minHeight: CCUsageBar.height)
                    .accessibilityLabel("\(accessibilityTitle), loading")
                Text("—")
                    .font(.subheadline.monospacedDigit())
                    .foregroundStyle(.tertiary)
                    .frame(minWidth: 34, alignment: .trailing)
                    .accessibilityHidden(true)
                Color.clear.frame(width: 64, height: 0)
            } else {
                CCUsageBar(fraction: 0, tint: AnyShapeStyle(.secondary))
                    .frame(minWidth: 56)
                    .accessibilityElement()
                    .accessibilityLabel(accessibilityTitle)
                    .accessibilityValue("Unavailable")
                Text("—")
                    .font(.subheadline.monospacedDigit())
                    .foregroundStyle(.tertiary)
                    .frame(minWidth: 34, alignment: .trailing)
                    .accessibilityHidden(true)
                Text("Unavailable")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .frame(minWidth: 64, alignment: .trailing)
                    .accessibilityHidden(true)
            }
        }
    }

    private func barTint(_ percent: Double) -> AnyShapeStyle {
        if row.isStale { return AnyShapeStyle(.secondary) }
        if percent >= 90 { return AnyShapeStyle(Color.orange) }
        return AnyShapeStyle(Color.accentColor)
    }

    private func spokenValue(percent: Double, reset: String?) -> String {
        let used = "\(Int(percent.rounded())) percent used"
        guard let reset else { return "\(used), reset unavailable" }
        return "\(used), resets \(reset)"
    }

    // MARK: Age

    @ViewBuilder
    private var ageLine: some View {
        let age = row.isStale
            ? (row.account.lastGoodAgeSeconds ?? row.account.usageAgeSeconds)
            : (row.account.usageAgeSeconds ?? row.account.lastGoodAgeSeconds)
        HStack(spacing: 6) {
            if row.isStale {
                // Orange on the symbol only: orange 11pt text is about 2:1 on a
                // light surface. The label stays in the secondary style.
                Label {
                    Text("Stale").fontWeight(.medium)
                } icon: {
                    Image(systemName: "clock.arrow.circlepath")
                        .foregroundStyle(.orange)
                }
                .foregroundStyle(.secondary)
                .labelStyle(.titleAndIcon)
            }
            if let age {
                Text("\(row.isStale ? "Last good usage" : "Usage data") · \(Self.ageText(age)) ago")
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            } else {
                Text("Usage age unavailable")
                    .foregroundStyle(.secondary)
            }
        }
        .font(.subheadline)
        .accessibilityElement(children: .combine)
    }

    static func ageText(_ seconds: Double) -> String {
        let duration = max(0, Int(seconds))
        if duration < 60 { return "\(duration)s" }
        if duration < 3_600 { return "\(duration / 60)m" }
        return "\(duration / 3_600)h"
    }
}

extension ScopedUsage {
    /// The label the meter row shows: label, then name, then model.
    var displayTitle: String { label ?? name ?? model ?? "Other" }
}
