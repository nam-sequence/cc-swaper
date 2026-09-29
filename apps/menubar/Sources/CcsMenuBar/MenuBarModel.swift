import AppKit
import Combine
import Foundation

@MainActor
final class MenuBarModel: ObservableObject {
    @Published private(set) var accounts: [Account] = []
    @Published private(set) var isRefreshing = false
    /// The last completed account-list read failed (drives the paused reason).
    @Published private(set) var rosterReadFailed = false
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

    var selectedAccount: Account? { accounts.first(where: \.active) }

    var menuTitle: String {
        guard let selectedAccount else { return "ccshift" }
        return selectedAccount.displayName
    }

    var activeSummary: String {
        selectedAccount.map { "Active: \($0.displayName)" } ?? "No active account"
    }

    var rows: [AccountRow] {
        accounts.map { account in
            AccountRow(
                account: account,
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
        case .requiresApproval: "Allow ccshift in System Settings → General → Login Items."
        }
    }

    var autoSwitchAvailabilityMessage: String? {
        guard autoSwitchEnabled else { return nil }
        guard !accounts.isEmpty else {
            return rosterReadFailed
                ? "Paused: could not read accounts from ccshift. Retrying every minute."
                : "Paused: add an account with ccshift first."
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
        panel.title = "Choose ccshift executable"
        panel.message = "Select the installed ccshift command."
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
        defaults.set(newClient.executableURL.path, forKey: "ccshiftExecutablePath")
        configure(executableURL: newClient.executableURL)
        refresh()
    }

    /// `keepingAlert` is for background retries: the current error stays on
    /// screen until the retry finishes instead of flickering away.
    func refresh(afterSwitchWarning: String? = nil, keepingAlert: Bool = false) {
        launchAtLoginStatus = launchAtLoginManager.status()
        if launchAtLoginStatus == .enabled { launchAtLoginError = nil }
        guard let client else {
            alertMessage = CLIError.executableNotFound.localizedDescription
            rosterReadFailed = true
            return
        }
        refreshGeneration += 1
        let generation = refreshGeneration
        isRefreshing = true
        if !keepingAlert { alertMessage = nil }

        Task { [weak self] in
            let result = await Self.load { try client.dashboard() }
            guard let self, self.refreshGeneration == generation else { return }
            switch result {
            case let .success(snapshot):
                self.accounts = snapshot.accounts
                self.lastUpdated = Date()
                self.rosterReadFailed = false
                self.alertMessage = afterSwitchWarning ?? snapshot.warning
            case let .failure(message):
                self.rosterReadFailed = true
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
        guard let known = accounts.first(where: { $0.id == account.id }) else {
            alertMessage = "That account is unavailable. Refresh the account list and try again."
            return
        }

        switchingAccountID = account.id
        alertMessage = nil
        Task { [weak self] in
            let result = await Self.load { () throws -> ManualSwitchOutcome in
                guard let number = known.number else { throw CLIError.invalidAccountNumber }
                let report = try client.switchEngineAccount(to: number)
                return ManualSwitchOutcome(warning: report.warnings?.first(where: { !$0.isEmpty }))
            }
            guard let self else { return }
            self.switchingAccountID = nil
            switch result {
            case let .success(outcome):
                self.refresh(afterSwitchWarning: outcome.warning)
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
                if !self.accounts.isEmpty {
                    await self.runAutoSwitchTick()
                } else {
                    // The live paused label already explains why; drop any
                    // older result. Retry the roster so a failed startup read
                    // or an account added from the terminal does not pause
                    // auto-switching until the menu is opened, and check at
                    // once when accounts appear.
                    self.autoSwitchLastResult = nil
                    self.refresh(keepingAlert: true)
                    while self.isRefreshing && !Task.isCancelled {
                        try? await Task.sleep(for: .milliseconds(100))
                    }
                    if !Task.isCancelled, !self.accounts.isEmpty {
                        await self.runAutoSwitchTick()
                    }
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

/// Launch-at-login stand-in for previews and snapshots; never touches ServiceManagement.
@MainActor
final class PreviewLaunchAtLoginManager: LaunchAtLoginManaging {
    private var current: LaunchAtLoginStatus

    init(status: LaunchAtLoginStatus = .notRegistered) {
        current = status
    }

    func status() -> LaunchAtLoginStatus { current }

    func setEnabled(_ enabled: Bool) throws -> LaunchAtLoginStatus {
        current = enabled ? .enabled : .notRegistered
        return current
    }

    func openLoginItemsSettings() {}
}

// MARK: - Preview and snapshot fixtures

extension ScopedUsage {
    init(label: String, pct: Double?, resetsAt: String?) {
        self.name = nil
        self.label = label
        self.model = nil
        self.pct = pct
        self.resetsAt = resetsAt
        self.countdown = nil
        self.clock = nil
    }
}

extension MenuBarModel {
    /// Everything a fixture can pin. `MenuBarModel.preview(_:)` assigns these
    /// directly, so no CLI process, timer, defaults write or ServiceManagement
    /// call ever runs.
    @MainActor
    struct PreviewState {
        var accounts: [Account] = MenuBarModel.previewAccounts
        var isRefreshing = false
        var rosterReadFailed = false
        var switchingAccountID: String?
        var lastUpdated: Date? = Date()
        var alertMessage: String?
        var executablePath: String? = "/Users/example/.local/bin/ccshift"
        var autoSwitchEnabled = false
        var autoSwitchThreshold = 90.0
        var autoSwitchDryRun = false
        var autoSwitchIsRunning = false
        var autoSwitchLastResult: String?
        var launchAtLoginStatus: LaunchAtLoginStatus = .notRegistered
        var launchAtLoginError: String?
    }

    private static func previewReset(hours: Double) -> String {
        // The extra 30 s keeps "2h 12m" stable while a snapshot is rendered.
        ISO8601DateFormatter().string(from: Date().addingTimeInterval(hours * 3_600 + 30))
    }

    /// Active, stale with a row message, and disabled with a scoped window.
    static var previewAccounts: [Account] {
        [
            Account(
                id: "account:1",
                number: 1, email: "nam@example.com", organizationName: "Max",
                alias: "main", active: true, disabled: false, usageStatus: "ok",
                usage: AccountUsage(
                    fiveHour: UsageWindow(pct: 17, resetsAt: previewReset(hours: 2.2)),
                    sevenDay: UsageWindow(pct: 32, resetsAt: previewReset(hours: 75)),
                    scoped: []
                ),
                usageAgeSeconds: 18, lastGoodUsage: nil, lastGoodAgeSeconds: nil
            ),
            Account(
                id: "account:2",
                number: 2, email: "team@example.com", organizationName: "Team",
                alias: "work", active: false, disabled: false, usageStatus: "unavailable",
                usage: nil,
                usageAgeSeconds: 9_300,
                lastGoodUsage: AccountUsage(
                    fiveHour: UsageWindow(pct: 74, resetsAt: previewReset(hours: 0.8)),
                    sevenDay: UsageWindow(pct: 61, resetsAt: previewReset(hours: 130)),
                    scoped: []
                ),
                lastGoodAgeSeconds: 9_300
            ),
            Account(
                id: "account:3",
                number: 3, email: "research@example.com", organizationName: nil,
                alias: "research", active: false, disabled: true, usageStatus: "ok",
                usage: AccountUsage(
                    fiveHour: UsageWindow(pct: 93, resetsAt: previewReset(hours: 1.4)),
                    sevenDay: UsageWindow(pct: 48, resetsAt: previewReset(hours: 52)),
                    scoped: [ScopedUsage(label: "Sonnet", pct: 12, resetsAt: previewReset(hours: 52))]
                ),
                usageAgeSeconds: 42, lastGoodUsage: nil, lastGoodAgeSeconds: nil
            ),
        ]
    }

    /// An account whose only content is its status message (no usage at all).
    static var previewMessageOnlyAccount: Account {
        Account(
            id: "account:4",
            number: 4, email: "personal@example.com", organizationName: nil,
            alias: nil, active: false, disabled: false, usageStatus: "token_expired",
            usage: nil, usageAgeSeconds: nil, lastGoodUsage: nil, lastGoodAgeSeconds: nil
        )
    }

    /// Enough accounts to exercise the scrolling cap.
    static var previewManyAccounts: [Account] {
        var accounts = previewAccounts + [previewMessageOnlyAccount]
        for number in 5...8 {
            accounts.append(
                Account(
                    id: "account:\(number)",
                    number: number, email: "seat\(number)@example.com", organizationName: "Team",
                    alias: "seat-\(number)", active: false, disabled: false, usageStatus: "ok",
                    usage: AccountUsage(
                        fiveHour: UsageWindow(pct: Double(number * 9), resetsAt: previewReset(hours: 3.1)),
                        sevenDay: UsageWindow(pct: Double(number * 6), resetsAt: previewReset(hours: 90)),
                        scoped: []
                    ),
                    usageAgeSeconds: 30, lastGoodUsage: nil, lastGoodAgeSeconds: nil
                )
            )
        }
        return accounts
    }

    static func preview(_ state: PreviewState) -> MenuBarModel {
        let suite = "com.namsequence.ccshift.menubar.preview.\(UUID().uuidString)"
        let model = MenuBarModel(
            defaults: UserDefaults(suiteName: suite)!,
            executableURL: nil,
            launchAtLoginManager: PreviewLaunchAtLoginManager(status: state.launchAtLoginStatus)
        )
        model.accounts = state.accounts
        model.isRefreshing = state.isRefreshing
        model.rosterReadFailed = state.rosterReadFailed
        model.switchingAccountID = state.switchingAccountID
        model.lastUpdated = state.lastUpdated
        model.alertMessage = state.alertMessage
        model.executablePath = state.executablePath
        model.autoSwitchEnabled = state.autoSwitchEnabled
        model.autoSwitchThreshold = state.autoSwitchThreshold
        model.autoSwitchDryRun = state.autoSwitchDryRun
        model.autoSwitchIsRunning = state.autoSwitchIsRunning
        model.autoSwitchLastResult = state.autoSwitchLastResult
        model.launchAtLoginError = state.launchAtLoginError
        return model
    }

    static var preview: MenuBarModel { preview(PreviewState()) }
}
