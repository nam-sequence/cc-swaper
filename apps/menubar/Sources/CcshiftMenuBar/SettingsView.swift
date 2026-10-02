import SwiftUI

/// The native macOS Settings window ("ccshift Settings…", ⌘,). Three tabs of
/// grouped forms; the popover keeps only the quick automatic-switching toggle.
/// The model owns the selected tab, so the popover can open Accounts directly.
struct SettingsView: View {
    @ObservedObject var model: MenuBarModel

    var body: some View {
        TabView(selection: $model.settingsTab) {
            AccountsSettingsPane(model: model)
                .tabItem { Label("Accounts", systemImage: "person.2") }
                .tag(SettingsTab.accounts)
            GeneralSettingsPane(model: model)
                .tabItem { Label("General", systemImage: "gearshape") }
                .tag(SettingsTab.general)
            AutomaticSwitchingSettingsPane(model: model)
                .tabItem { Label("Automatic Switching", systemImage: "arrow.triangle.2.circlepath") }
                .tag(SettingsTab.automaticSwitching)
        }
    }
}

enum SettingsMetrics {
    static let width: CGFloat = 480
}

// MARK: - General

struct GeneralSettingsPane: View {
    @ObservedObject var model: MenuBarModel

    /// "Choose…" reports a rejected pick only through the model's shared
    /// `alertMessage`, and the popover is closed while Settings is open. Show
    /// the executable-related messages here, and only those: the same property
    /// also carries switch failures and usage warnings that do not belong under
    /// this section. A valid pick calls `refresh()`, which clears the message.
    private var cliChoiceMessage: String? {
        guard let message = model.alertMessage else { return nil }
        let executableMessages = [CLIError.invalidExecutablePath, CLIError.executableNotFound]
            .compactMap(\.errorDescription)
        return executableMessages.contains(message) ? message : nil
    }

    var body: some View {
        Form {
            Section {
                Toggle(
                    "Launch at Login",
                    isOn: Binding(
                        get: { model.launchAtLoginRequested },
                        set: { model.setLaunchAtLoginEnabled($0) }
                    )
                )

                if model.launchAtLoginStatus == .requiresApproval || model.launchAtLoginError != nil {
                    LabeledContent("Login Items") {
                        Button("Open Login Items Settings", action: model.openLoginItemsSettings)
                            .buttonStyle(.link)
                    }
                }
            } footer: {
                if let message = model.launchAtLoginMessage {
                    // The symbol carries the status color; the text stays in the
                    // secondary style so it keeps its contrast in light mode.
                    Label {
                        Text(message)
                    } icon: {
                        if model.launchAtLoginError != nil {
                            Image(systemName: "exclamationmark.triangle.fill")
                                .foregroundStyle(.orange)
                        }
                    }
                }
            }

            Section {
                Toggle(
                    "Show usage in the menu bar",
                    isOn: Binding(
                        get: { model.showUsageInMenuBar },
                        set: { model.setShowUsageInMenuBar($0) }
                    )
                )
            } footer: {
                Text("The active account's 5-hour or 7-day usage, whichever is higher, kept current from the local usage store.")
            }

            UpdatesSection(model: model)

            Section {
                LabeledContent("ccshift") {
                    HStack(spacing: 10) {
                        Text(model.executablePath ?? "Not connected")
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .truncationMode(.middle)
                            .textSelection(.enabled)
                            .help(model.executablePath ?? "ccshift not connected")
                        Button("Choose…", action: model.chooseExecutable)
                    }
                }
            } header: {
                Text("Command Line Tool")
            } footer: {
                if let message = cliChoiceMessage {
                    Label {
                        Text(message)
                    } icon: {
                        Image(systemName: "exclamationmark.triangle.fill")
                            .foregroundStyle(.orange)
                    }
                }
            }
        }
        .formStyle(.grouped)
        .fixedSize(horizontal: false, vertical: true)
        .frame(width: SettingsMetrics.width)
    }
}

// MARK: - Updates

/// Settings › General › Updates: the running version, the command line tool's,
/// and the latest release on GitHub.
struct UpdatesSection: View {
    @ObservedObject var model: MenuBarModel

    var body: some View {
        Section {
            LabeledContent("Version", value: model.appVersion)
            if let cliVersion = model.cliVersion {
                LabeledContent("Command Line Tool", value: cliVersion)
            }
            Toggle(
                "Check for Updates Automatically",
                isOn: Binding(
                    get: { model.autoCheckForUpdates },
                    set: { model.setAutoCheckForUpdates($0) }
                )
            )
            if let update = model.availableUpdate {
                LabeledContent {
                    HStack(spacing: 8) {
                        if update.version != model.skippedUpdateVersion {
                            Button("Skip This Version", action: model.skipAvailableUpdate)
                        }
                        Button("Release Notes", action: model.openReleaseNotes)
                        Button("Download…", action: model.downloadUpdate)
                            .keyboardShortcut(.defaultAction)
                    }
                } label: {
                    Label {
                        Text("ccshift \(update.version) is available")
                    } icon: {
                        Image(systemName: "arrow.down.circle.fill")
                            .foregroundStyle(.tint)
                    }
                }
            } else {
                LabeledContent {
                    Button("Check Now") {
                        Task { await model.checkForUpdates(userInitiated: true) }
                    }
                    .disabled(model.isCheckingForUpdates)
                } label: {
                    if model.isCheckingForUpdates {
                        HStack(spacing: 8) {
                            ProgressView().controlSize(.small)
                            Text("Checking for updates…")
                        }
                    } else if model.updateCheckError == nil, model.lastUpdateCheck != nil {
                        Text("ccshift is up to date")
                    } else {
                        Text("Updates")
                    }
                }
            }
        } header: {
            Text("Updates")
        } footer: {
            updatesFooter
        }
        .onAppear { model.refreshCLIVersion() }
    }

    @ViewBuilder
    private var updatesFooter: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let error = model.updateCheckError {
                Label {
                    Text(error)
                } icon: {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .foregroundStyle(.orange)
                }
            } else if model.availableUpdate != nil {
                Text("Unzip the download and replace ccshift.app in Applications with it.")
            } else if let lastCheck = model.lastUpdateCheck {
                Text("Last checked \(lastCheck.formatted(date: .abbreviated, time: .shortened)).")
            }
            if let hint = model.cliUpdateHint {
                Label {
                    Text(hint)
                } icon: {
                    Image(systemName: "terminal")
                        .foregroundStyle(.secondary)
                }
            }
        }
    }
}

// MARK: - Automatic Switching

struct AutomaticSwitchingSettingsPane: View {
    @ObservedObject var model: MenuBarModel

    var body: some View {
        Form {
            Section {
                Toggle(
                    "Automatic Switching",
                    isOn: Binding(
                        get: { model.autoSwitchEnabled },
                        set: { model.setAutoSwitchEnabled($0) }
                    )
                )
            } footer: {
                VStack(alignment: .leading, spacing: 6) {
                    Text("When enabled, ccshift checks usage every minute and chooses whether to switch.")
                    if let paused = model.autoSwitchAvailabilityMessage {
                        Label {
                            Text(paused)
                        } icon: {
                            Image(systemName: "pause.circle")
                                .foregroundStyle(.orange)
                        }
                    }
                }
            }

            Section {
                LabeledContent("Threshold") {
                    HStack(spacing: 10) {
                        // Continuous, snapped to whole points in the binding: a
                        // `step:` slider draws one tick mark per step (about 50
                        // here), which native Settings sliders never do.
                        Slider(
                            value: Binding(
                                get: { model.autoSwitchThreshold },
                                set: { newValue in
                                    let snapped = min(max(newValue.rounded(), 50), 99.9)
                                    if snapped != model.autoSwitchThreshold {
                                        model.setAutoSwitchThreshold(snapped)
                                    }
                                }
                            ),
                            in: 50...99.9
                        ) {
                            Text("Threshold")
                        }
                        .labelsHidden()
                        .frame(minWidth: 150)
                        .accessibilityValue("\(model.autoSwitchThreshold, specifier: "%.1f") percent")
                        Text("\(model.autoSwitchThreshold, specifier: "%.1f")%")
                            .monospacedDigit()
                            .foregroundStyle(.secondary)
                            .frame(width: 48, alignment: .trailing)
                    }
                }

                Toggle(
                    isOn: Binding(
                        get: { model.autoSwitchDryRun },
                        set: { model.setAutoSwitchDryRun($0) }
                    )
                ) {
                    // The disabled switch dims by itself; the label and caption
                    // follow the same condition, as in native Settings rows.
                    Text("Dry Run")
                        .foregroundStyle(model.autoSwitchEnabled ? AnyShapeStyle(.primary) : AnyShapeStyle(.tertiary))
                    Text("Never switch accounts.")
                        .font(.subheadline)
                        .foregroundStyle(model.autoSwitchEnabled ? AnyShapeStyle(.secondary) : AnyShapeStyle(.tertiary))
                }
                .disabled(!model.autoSwitchEnabled)
            }

            if model.autoSwitchIsRunning || model.autoSwitchLastResult != nil {
                Section("Status") {
                    if model.autoSwitchIsRunning {
                        HStack(spacing: 8) {
                            ProgressView().controlSize(.small)
                            Text("Checking account usage…")
                                .foregroundStyle(.secondary)
                        }
                        .accessibilityElement(children: .combine)
                    }
                    if let result = model.autoSwitchLastResult {
                        Text(result)
                            .foregroundStyle(.secondary)
                            .textSelection(.enabled)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        }
        .formStyle(.grouped)
        .fixedSize(horizontal: false, vertical: true)
        .frame(width: SettingsMetrics.width)
    }
}

#Preview("Settings · Accounts") {
    SettingsView(model: .preview)
}

#Preview("Settings · General") {
    var state = MenuBarModel.PreviewState()
    state.settingsTab = .general
    return SettingsView(model: .preview(state))
}

#Preview("Settings · Automatic Switching") {
    var state = MenuBarModel.PreviewState()
    state.settingsTab = .automaticSwitching
    return SettingsView(model: .preview(state))
}
