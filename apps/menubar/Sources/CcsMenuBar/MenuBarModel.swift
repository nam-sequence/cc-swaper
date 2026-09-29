import AppKit
import Combine
import Foundation

@MainActor
final class MenuBarModel: ObservableObject {
    @Published private(set) var accounts: [Account] = []
    @Published private(set) var launchBackend: String?
    @Published private(set) var isRefreshing = false
    @Published private(set) var switchingAccountID: String?
    @Published private(set) var lastUpdated: Date?
    @Published var alertMessage: String?
    @Published private(set) var executablePath: String?
    @Published private(set) var autoSwitchEnabled: Bool
    @Published private(set) var autoSwitchThreshold: Double
    @Published private(set) var autoSwitchDryRun: Bool
    @Published private(set) var autoSwitchIsRunning = false
    @Published private(set) var autoSwitchLastResult: String?
    @Published private(set) var launchAtLoginStatus: LaunchAtLoginStatus
    @Published private(set) var launchAtLoginError: String?

    private let defaults: UserDefaults
    private let launchAtLoginManager: any LaunchAtLoginManaging
    private let autoSwitchInterval: TimeInterval
    private var client: CSwapClient?
    private var refreshGeneration = 0
    private var autoSwitchTask: Task<Void, Never>?
    private var autoSwitchCancellation: ProcessCancellation?
    private var autoTickInFlight = false

    init(
        defaults: UserDefaults = .standard,
        executableURL: URL? = CLIResolver.executable(),
        launchAtLoginManager: (any LaunchAtLoginManaging)? = nil,
        autoSwitchInterval: TimeInterval = 60
    ) {
        self.defaults = defaults
        self.launchAtLoginManager = launchAtLoginManager ?? SystemLaunchAtLoginManager()
        self.autoSwitchInterval = max(0.1, autoSwitchInterval)
        self.autoSwitchEnabled = defaults.object(forKey: "ccsAutoSwitchEnabled") as? Bool ?? false
        self.autoSwitchThreshold = defaults.object(forKey: "ccsAutoSwitchThreshold") as? Double ?? 90
        self.autoSwitchDryRun = defaults.object(forKey: "ccsAutoSwitchDryRun") as? Bool ?? false
        self.launchAtLoginStatus = self.launchAtLoginManager.status()
        configure(executableURL: executableURL)
        if autoSwitchEnabled {
            refresh()
            startAutoSwitchLoop()
        }
    }

    var engineAccount: Account? { accounts.first(where: { $0.mode == .engine && $0.active }) }
    var legacyProfile: Account? { accounts.first(where: { $0.mode == .legacy && $0.active }) }
    var selectedAccount: Account? {
        switch launchBackend {
        case "accounts": engineAccount
        case "legacy": legacyProfile
        default: nil
        }
    }

    var menuTitle: String {
        guard let selectedAccount else { return "ccs" }
        return selectedAccount.displayName
    }

    var activeSummary: String {
        let engine = engineAccount.map { "Engine: \($0.displayName)" } ?? "Engine: none"
        let legacy = legacyProfile.map { "Existing profiles: \($0.displayName)" } ?? "Existing profiles: none"
        return "\(engine)  ·  \(legacy)"
    }

    var launchBackendTitle: String {
        switch launchBackend {
        case "accounts": "Engine Accounts"
        case "legacy": "Existing Profiles"
        default: "checking status"
        }
    }

    var rows: [AccountRow] {
        accounts.map { account in
            AccountRow(
                account: account,
                mode: account.mode,
                isActive: account.active,
                isLoading: isRefreshing && account.visibleUsage == nil,
                isStale: account.isStale,
                message: account.usageMessage
            )
        }
    }

    var launchAtLoginRequested: Bool {
        launchAtLoginStatus == .enabled || launchAtLoginStatus == .requiresApproval
    }

    var launchAtLoginMessage: String? {
        if let launchAtLoginError { return launchAtLoginError }
        return switch launchAtLoginStatus {
        case .notRegistered, .enabled: nil
        case .requiresApproval: "Allow CC Swaper in System Settings → General → Login Items."
        case .notFound: "Launch at login is unavailable for this app bundle."
        }
    }

    var autoSwitchAvailabilityMessage: String? {
        guard autoSwitchEnabled else { return nil }
        guard !accounts.filter({ $0.mode == .engine }).isEmpty else {
            return "Paused: add an account to Engine Accounts first."
        }
        guard launchBackend == "accounts" else {
            return "Paused: set Claude launches to Engine Accounts to enable auto-switching."
        }
        return nil
    }

    func configure(executableURL: URL?) {
        if let executableURL,
           let client = try? CSwapClient(executableURL: executableURL) {
            self.client = client
            self.executablePath = client.executableURL.path
        } else {
            client = nil
            executablePath = nil
        }
    }

    func chooseExecutable() {
        let panel = NSOpenPanel()
        panel.title = "Choose ccs executable"
        panel.message = "Select the installed ccs command."
        panel.prompt = "Choose"
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        panel.directoryURL = URL(fileURLWithPath: NSHomeDirectory())
        guard panel.runModal() == .OK, let url = panel.url else { return }
        guard let valid = CLIResolver.validExecutable(url.path),
              let newClient = try? CSwapClient(executableURL: valid) else {
            alertMessage = CLIError.invalidExecutablePath.localizedDescription
            return
        }
        defaults.set(newClient.executableURL.path, forKey: "ccsExecutablePath")
        configure(executableURL: newClient.executableURL)
        refresh()
    }

    func refresh(afterSwitchWarning: String? = nil) {
        launchAtLoginStatus = launchAtLoginManager.status()
        if launchAtLoginStatus == .enabled { launchAtLoginError = nil }
        guard let client else {
            alertMessage = CLIError.executableNotFound.localizedDescription
            return
        }
        refreshGeneration += 1
        let generation = refreshGeneration
        isRefreshing = true
        alertMessage = nil

        Task { [weak self] in
            let result = await Self.load { try client.dashboard() }
            guard let self, self.refreshGeneration == generation else { return }
            switch result {
            case let .success(snapshot):
                self.launchBackend = snapshot.launchBackend
                self.accounts = snapshot.accounts
                self.lastUpdated = Date()
                self.alertMessage = afterSwitchWarning ?? snapshot.warning
                if self.autoSwitchEnabled, self.launchBackend != "accounts" {
                    self.autoSwitchCancellation?.cancel()
                }
            case let .failure(message):
                self.alertMessage = afterSwitchWarning ?? message
            }
            self.isRefreshing = false
        }
    }

    func switchTo(_ account: Account) {
        guard switchingAccountID == nil else { return }
        guard let client else {
            alertMessage = CLIError.executableNotFound.localizedDescription
            return
        }
        guard let known = accounts.first(where: { $0.id == account.id }), !known.disabled else {
            alertMessage = "That account is unavailable. Refresh the account list and try again."
            return
        }

        switchingAccountID = account.id
        alertMessage = nil
        Task { [weak self] in
            let result = await Self.load { () throws -> ManualSwitchOutcome in
                switch known.mode {
                case .engine:
                    guard let number = known.number else { throw CLIError.invalidAccountNumber }
                    let report = try client.switchEngineAccount(to: number)
                    let routingWarning = report.routingWarning
                        ?? (report.routingChanged == false && report.launchBackend == "legacy"
                            ? "The engine account changed, but Claude launches still use Existing Profiles."
                            : nil)
                    return ManualSwitchOutcome(routingWarning: routingWarning)
                case .legacy:
                    guard let name = known.profileName else { throw CLIError.invalidProfileName }
                    _ = try client.switchLegacyProfile(to: name)
                    return ManualSwitchOutcome(routingWarning: nil)
                }
            }
            guard let self else { return }
            self.switchingAccountID = nil
            switch result {
            case let .success(outcome):
                self.refresh(afterSwitchWarning: outcome.routingWarning)
            case let .failure(message):
                self.refresh(afterSwitchWarning: message)
            }
        }
    }

    func setAutoSwitchEnabled(_ enabled: Bool) {
        guard autoSwitchEnabled != enabled else { return }
        autoSwitchEnabled = enabled
        defaults.set(enabled, forKey: "ccsAutoSwitchEnabled")
        if enabled {
            startAutoSwitchLoop()
        } else {
            stopAutoSwitchLoop()
        }
    }

    func setAutoSwitchThreshold(_ threshold: Double) {
        let bounded = min(max(threshold, 50), 99.9)
        autoSwitchThreshold = bounded
        defaults.set(bounded, forKey: "ccsAutoSwitchThreshold")
    }

    func setAutoSwitchDryRun(_ dryRun: Bool) {
        autoSwitchDryRun = dryRun
        defaults.set(dryRun, forKey: "ccsAutoSwitchDryRun")
    }

    func setLaunchAtLoginEnabled(_ enabled: Bool) {
        launchAtLoginError = nil
        do {
            launchAtLoginStatus = try launchAtLoginManager.setEnabled(enabled)
        } catch {
            launchAtLoginStatus = launchAtLoginManager.status()
            launchAtLoginError = "Could not update Launch at Login. Open System Settings → General → Login Items and try again."
        }
    }

    func openLoginItemsSettings() {
        launchAtLoginManager.openLoginItemsSettings()
    }

    func shutdown() {
        stopAutoSwitchLoop()
    }

    private func startAutoSwitchLoop() {
        guard autoSwitchTask == nil else { return }
        autoSwitchTask = Task { [weak self] in
            guard let self else { return }
            while !Task.isCancelled {
                while self.autoTickInFlight && !Task.isCancelled {
                    try? await Task.sleep(for: .milliseconds(100))
                }
                guard !Task.isCancelled else { break }
                if self.isRefreshing {
                    try? await Task.sleep(for: .milliseconds(100))
                    continue
                }
                if self.accounts.contains(where: { $0.mode == .engine }),
                   self.launchBackend == "accounts" {
                    await self.runAutoSwitchTick()
                } else if self.autoSwitchLastResult == nil {
                    self.autoSwitchLastResult = self.autoSwitchAvailabilityMessage
                        ?? "Waiting for account status."
                }
                do {
                    try await Task.sleep(for: .seconds(self.autoSwitchInterval))
                } catch {
                    break
                }
            }
            self.autoSwitchTask = nil
        }
    }

    private func stopAutoSwitchLoop() {
        autoSwitchTask?.cancel()
        autoSwitchTask = nil
        autoSwitchCancellation?.cancel()
        autoSwitchLastResult = "Automatic switching is off."
    }

    private func runAutoSwitchTick() async {
        guard !autoTickInFlight,
              autoSwitchEnabled,
              launchBackend == "accounts",
              let client
        else { return }
        autoTickInFlight = true
        autoSwitchIsRunning = true
        let cancellation = ProcessCancellation()
        autoSwitchCancellation = cancellation
        let threshold = autoSwitchThreshold
        let dryRun = autoSwitchDryRun
        let result = await Self.load {
            try client.autoSwitchOnce(
                threshold: threshold,
                dryRun: dryRun,
                cancellation: cancellation
            )
        }
        autoSwitchCancellation = nil
        autoSwitchIsRunning = false
        autoTickInFlight = false
        guard autoSwitchEnabled, !cancellation.isCancelled else { return }
        switch result {
        case let .success(outcome):
            autoSwitchLastResult = "\(outcome.summary) Threshold: \(outcome.threshold)%"
            if outcome.eventKind == "switch" && !outcome.dryRun { refresh() }
        case let .failure(message):
            autoSwitchLastResult = message
        }
    }

    private static func load<Value: Sendable>(
        _ operation: @escaping @Sendable () throws -> Value
    ) async -> LoadResult<Value> {
        await Task.detached(priority: .userInitiated) {
            do {
                return .success(try operation())
            } catch {
                return .failure(error.localizedDescription)
            }
        }.value
    }
}

private enum LoadResult<Value: Sendable>: Sendable {
    case success(Value)
    case failure(String)
}

extension MenuBarModel {
    static var preview: MenuBarModel {
        let defaults = UserDefaults(suiteName: "com.namsequence.ccswaper.menubar.preview")!
        defaults.set(false, forKey: "ccsAutoSwitchEnabled")
        let model = MenuBarModel(defaults: defaults, executableURL: nil)
        model.accounts = [
            Account(
                id: "engine:1", mode: .engine,
                number: 1, profileName: nil, email: "nam@example.com", organizationName: "Max",
                alias: "main", active: true, disabled: false, signedIn: nil, usageStatus: "ok",
                usage: AccountUsage(fiveHour: UsageWindow(pct: 17, resetsAt: "in 2h 10m"), sevenDay: UsageWindow(pct: 32, resetsAt: "Fri 2:00 AM"), scoped: []),
                usageAgeSeconds: 18, lastGoodUsage: nil, lastGoodAgeSeconds: nil, plan: nil
            ),
            Account(
                id: "engine:2", mode: .engine,
                number: 2, profileName: nil, email: "team@example.com", organizationName: "Team",
                alias: "work", active: false, disabled: false, signedIn: nil, usageStatus: "stale",
                usage: nil,
                usageAgeSeconds: 9_300,
                lastGoodUsage: AccountUsage(fiveHour: UsageWindow(pct: 74, resetsAt: "in 48m"), sevenDay: UsageWindow(pct: 61, resetsAt: "Mon 12:00 AM"), scoped: []),
                lastGoodAgeSeconds: 9_300, plan: nil
            ),
            Account(
                id: "legacy:main", mode: .legacy,
                number: nil, profileName: "main", email: "old-main@example.com", organizationName: "Max",
                alias: "main", active: true, disabled: false, signedIn: true, usageStatus: "ok",
                usage: AccountUsage(fiveHour: UsageWindow(pct: 22, resetsAt: "in 1h"), sevenDay: UsageWindow(pct: 28, resetsAt: "Friday"), scoped: []),
                usageAgeSeconds: nil, lastGoodUsage: nil, lastGoodAgeSeconds: nil, plan: "Max"
            ),
        ]
        model.launchBackend = "accounts"
        model.lastUpdated = Date()
        return model
    }

    static var legacyPreview: MenuBarModel {
        let defaults = UserDefaults(suiteName: "com.namsequence.ccswaper.menubar.preview.legacy")!
        defaults.set(false, forKey: "ccsAutoSwitchEnabled")
        let model = MenuBarModel(defaults: defaults, executableURL: nil)
        model.accounts = preview.accounts.filter { $0.mode == .legacy }
        model.accounts.indices.forEach { model.accounts[$0].active = true }
        model.launchBackend = "legacy"
        return model
    }
}
