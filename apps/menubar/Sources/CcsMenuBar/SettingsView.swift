import SwiftUI

/// The native macOS Settings window ("ccshift Settings…", ⌘,). Two tabs of
/// grouped forms; the popover keeps only the quick automatic-switching toggle.
struct SettingsView: View {
    @ObservedObject var model: MenuBarModel
    @State private var selection: Tab

    enum Tab: Hashable {
        case general
        case automaticSwitching
    }

    init(model: MenuBarModel, selection: Tab = .general) {
        self.model = model
        _selection = State(initialValue: selection)
    }

    var body: some View {
        TabView(selection: $selection) {
            GeneralSettingsPane(model: model)
                .tabItem { Label("General", systemImage: "gearshape") }
                .tag(Tab.general)
            AutomaticSwitchingSettingsPane(model: model)
                .tabItem { Label("Automatic Switching", systemImage: "arrow.triangle.2.circlepath") }
                .tag(Tab.automaticSwitching)
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

#Preview("Settings · General") {
    SettingsView(model: .preview)
}

#Preview("Settings · Automatic Switching") {
    SettingsView(model: .preview, selection: .automaticSwitching)
}
