import AppKit
import Combine
import Foundation

/// The Settings window's tabs; the model owns the selection so the popover
/// can open Settings on the Accounts tab.
enum SettingsTab: Hashable, Sendable {
    case accounts
    case general
    case automaticSwitching
}

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
    /// Where automatic switching moves off an account, per window: at this
    /// share of the 5-hour limit, and of the 7-day limit.
    @Published private(set) var autoSwitchThreshold5h: Double
    @Published private(set) var autoSwitchThreshold7d: Double
    @Published private(set) var autoSwitchDryRun: Bool
    @Published private(set) var autoSwitchIsRunning = false
    @Published private(set) var autoSwitchLastResult: String?
    @Published private(set) var launchAtLoginStatus: LaunchAtLoginStatus
    @Published private(set) var launchAtLoginError: String?
    @Published var settingsTab: SettingsTab = .accounts
    @Published var isAddAccountSheetPresented = false
    /// The account whose removal is waiting for the confirmation dialog.
    @Published var accountPendingRemoval: Account?
    @Published private(set) var currentLogin: CurrentLoginState = .unknown
    @Published private(set) var isChangingAccounts = false
    /// A browser sign-in (`ccshift add --login`) is waiting for the person.
    /// Kept apart from `isChangingAccounts`: it can take minutes, and nothing
    /// else needs to wait for it.
    @Published private(set) var isSigningIn = false
    @Published private(set) var accountChangeError: String?
    @Published private(set) var accountChangeNotice: String?
    @Published private(set) var availableUpdate: AvailableUpdate?
    @Published private(set) var isCheckingForUpdates = false
    @Published private(set) var lastUpdateCheck: Date?
    @Published private(set) var updateCheckError: String?
    @Published private(set) var autoCheckForUpdates: Bool
    @Published private(set) var skippedUpdateVersion: String?
    /// The installed command line tool's version, once read.
    @Published private(set) var cliVersion: String?
    /// Show the active account's usage next to its name in the menu bar.
    @Published private(set) var showUsageInMenuBar: Bool
    let appVersion: String

    private let defaults: UserDefaults
    private let launchAtLoginManager: any LaunchAtLoginManaging
    private let signInPresenter: any SignInWindowPresenting
    private let releaseFetcher: any ReleaseFetching
    private let updateNotifier: (any UpdateNotifying)?
    private var updateCheckTask: Task<Void, Never>?
    static let updateCheckInterval: TimeInterval = 24 * 60 * 60
    private let autoSwitchInterval: TimeInterval
    private var client: CcshiftClient?
    private var refreshGeneration = 0
    private var autoSwitchTask: Task<Void, Never>?
    private var autoSwitchCancellation: ProcessCancellation?
    private var autoTickInFlight = false
    private var loginCheckGeneration = 0
    private var signInCancellation: ProcessCancellation?
    private var signInURLWatch: Task<Void, Never>?
    private var liveTask: Task<Void, Never>?
    private var liveReadInFlight = false
    /// A tool older than 1.3.1 rejects `--cached`; after a few failures in a
    /// row the live view stops asking instead of failing every few seconds.
    private var liveFailures = 0
    private var liveSupported = true
    private var isPopoverVisible = false
    private var lastFullRefresh: Date?
    static let liveFailureLimit = 3
    /// A full (possibly fetching) refresh on open only when the last one is
    /// older than this; the store-only live reads cover the gap.
    static let fullRefreshOnOpenAfter: TimeInterval = 30

    init(
        defaults: UserDefaults = .standard,
        executableURL: URL? = CLIResolver.executable(),
        launchAtLoginManager: (any LaunchAtLoginManaging)? = nil,
        signInPresenter: (any SignInWindowPresenting)? = nil,
        releaseFetcher: (any ReleaseFetching)? = nil,
        updateNotifier: (any UpdateNotifying)? = nil,
        appVersion: String = AppVersion.current,
        autoSwitchInterval: TimeInterval = 60
    ) {
        self.defaults = defaults
        self.releaseFetcher = releaseFetcher ?? GitHubReleaseFetcher()
        self.updateNotifier = updateNotifier
        self.appVersion = appVersion
        self.autoCheckForUpdates = defaults.object(forKey: "ccshiftAutoCheckForUpdates") as? Bool ?? true
        self.skippedUpdateVersion = defaults.string(forKey: "ccshiftSkippedUpdateVersion")
        self.lastUpdateCheck = defaults.object(forKey: "ccshiftLastUpdateCheck") as? Date
        self.launchAtLoginManager = launchAtLoginManager ?? SystemLaunchAtLoginManager()
        self.signInPresenter = signInPresenter ?? SystemSignInWindowPresenter()
        self.autoSwitchInterval = max(0.1, autoSwitchInterval)
        self.autoSwitchEnabled = defaults.object(forKey: "ccshiftAutoSwitchEnabled") as? Bool ?? false
        // One threshold served both windows before they were split: it seeds
        // whichever of the two has not been set yet.
        let sharedThreshold = defaults.object(forKey: "ccshiftAutoSwitchThreshold") as? Double ?? 90
        self.autoSwitchThreshold5h = defaults.object(forKey: "ccshiftAutoSwitchThreshold5h") as? Double ?? sharedThreshold
        self.autoSwitchThreshold7d = defaults.object(forKey: "ccshiftAutoSwitchThreshold7d") as? Double ?? sharedThreshold
        self.autoSwitchDryRun = defaults.object(forKey: "ccshiftAutoSwitchDryRun") as? Bool ?? false
        self.showUsageInMenuBar = defaults.object(forKey: "ccshiftShowUsageInMenuBar") as? Bool ?? true
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

    /// The active account's binding usage for the menu bar, e.g. "54%" — the
    /// higher of its 5-hour and 7-day windows, the same one auto-switching
    /// acts on. "~" marks a last-known reading that is not current.
    var menuBarUsage: String? {
        guard showUsageInMenuBar, let account = selectedAccount,
              let usage = account.visibleUsage else { return nil }
        let top = [usage.fiveHour?.pct, usage.sevenDay?.pct].compactMap { $0 }.max()
        guard let top else { return nil }
        return "\(account.isStale ? "~" : "")\(Int(top.rounded()))%"
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
        liveFailures = 0
        liveSupported = true
        if let executableURL,
           let client = try? CcshiftClient(executableURL: executableURL) {
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
              let newClient = try? CcshiftClient(executableURL: valid) else {
            alertMessage = CLIError.invalidExecutablePath.localizedDescription
            return
        }
        defaults.set(newClient.executableURL.path, forKey: "ccshiftExecutablePath")
        configure(executableURL: newClient.executableURL)
        refresh()
    }

    /// `keepingAlert` is for background retries: the current error stays on
    /// screen until the retry finishes instead of flickering away.
    /// `force` is the refresh button: fetch every account now. Everything else
    /// (opening the menu, after a switch, retries) takes the ordinary refresh,
    /// which fetches only what the poll plans say is due.
    func refresh(afterSwitchWarning: String? = nil, keepingAlert: Bool = false, force: Bool = false) {
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
            let result = await Self.load { try client.dashboard(force: force) }
            guard let self, self.refreshGeneration == generation else { return }
            switch result {
            case let .success(snapshot):
                self.accounts = snapshot.accounts
                self.lastUpdated = Date()
                self.lastFullRefresh = Date()
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
        guard switchingAccountID == nil, !isChangingAccounts else { return }
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

    // MARK: Adding and removing accounts

    /// Opens the Add Account sheet on the Settings window's Accounts tab.
    func beginAddingAccount() {
        settingsTab = .accounts
        accountChangeError = nil
        accountChangeNotice = nil
        isAddAccountSheetPresented = true
    }

    /// Selects the Accounts tab and asks for confirmation before removing.
    func requestRemoval(of account: Account) {
        settingsTab = .accounts
        accountChangeError = nil
        accountChangeNotice = nil
        accountPendingRemoval = account
    }

    func checkCurrentLogin() {
        guard let client else {
            currentLogin = .failed(CLIError.executableNotFound.localizedDescription)
            return
        }
        loginCheckGeneration += 1
        let generation = loginCheckGeneration
        currentLogin = .checking
        Task { [weak self] in
            let result = await Self.load { try client.currentLogin() }
            guard let self, self.loginCheckGeneration == generation else { return }
            switch result {
            case let .success(login):
                self.currentLogin = login.map(CurrentLoginState.signedIn) ?? .signedOut
            case let .failure(message):
                self.currentLogin = .failed(message)
            }
        }
    }

    /// Saves the current Claude Code login with ccshift. An empty alias keeps
    /// ccshift's default name (the email, or an alias set earlier).
    func addCurrentAccount(alias: String) {
        guard !isChangingAccounts, !isSigningIn, switchingAccountID == nil else { return }
        guard let client else {
            accountChangeError = CLIError.executableNotFound.localizedDescription
            return
        }
        let alias = alias.trimmingCharacters(in: .whitespacesAndNewlines)
        isChangingAccounts = true
        accountChangeError = nil
        accountChangeNotice = nil
        Task { [weak self] in
            let result = await Self.load {
                try client.addCurrentAccount(alias: alias.isEmpty ? nil : alias)
            }
            guard let self else { return }
            self.isChangingAccounts = false
            switch result {
            case let .success(report):
                self.isAddAccountSheetPresented = false
                self.accountChangeNotice = Self.notice(for: report)
                self.refresh()
            case let .failure(message):
                self.accountChangeError = message
                self.refresh(keepingAlert: true)
            }
        }
    }

    /// Signs in with the browser and adds that account. Empty fields are left
    /// out: no email hint, ccshift's default name.
    func signInAndAddAccount(
        email: String,
        sso: Bool,
        alias: String,
        opener: SignInOpener = .defaultBrowser
    ) {
        guard !isChangingAccounts, !isSigningIn else { return }
        guard let client else {
            accountChangeError = CLIError.executableNotFound.localizedDescription
            return
        }
        let email = email.trimmingCharacters(in: .whitespacesAndNewlines)
        let alias = alias.trimmingCharacters(in: .whitespacesAndNewlines)
        var handoffDirectory: URL?
        var privateBrowser: String?
        switch opener {
        case .privateWindow:
            let directory = FileManager.default.temporaryDirectory
                .appendingPathComponent("ccshift-signin-\(UUID().uuidString)", isDirectory: true)
            do {
                try FileManager.default.createDirectory(
                    at: directory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700]
                )
                handoffDirectory = directory
            } catch {
                accountChangeError = "Could not prepare the sign-in window."
                return
            }
        case let .privateBrowser(bundleID):
            privateBrowser = bundleID
        case .defaultBrowser:
            break
        }
        let handoffFile = handoffDirectory?.appendingPathComponent("url")
        let browser = privateBrowser
        let cancellation = ProcessCancellation()
        signInCancellation = cancellation
        isSigningIn = true
        accountChangeError = nil
        accountChangeNotice = nil
        if let handoffFile {
            signInURLWatch = Task { [weak self] in
                await self?.openSignInWindow(whenWrittenTo: handoffFile, cancellation: cancellation)
            }
        }
        Task { [weak self] in
            let result = await Self.load {
                try client.signInAndAddAccount(
                    email: email.isEmpty ? nil : email,
                    sso: sso,
                    alias: alias.isEmpty ? nil : alias,
                    handoffFile: handoffFile?.path,
                    privateBrowser: browser,
                    cancellation: cancellation
                )
            }
            guard let self else { return }
            if let handoffDirectory {
                self.signInURLWatch?.cancel()
                self.signInURLWatch = nil
                self.signInPresenter.dismiss()
                try? FileManager.default.removeItem(at: handoffDirectory)
            }
            self.isSigningIn = false
            if self.signInCancellation === cancellation { self.signInCancellation = nil }
            // Stopped by the person: nothing to report.
            guard !cancellation.isCancelled else { return }
            switch result {
            case let .success(report):
                self.isAddAccountSheetPresented = false
                self.accountChangeNotice = Self.notice(for: report)
                self.refresh()
            case let .failure(message):
                self.accountChangeError = message
            }
        }
    }

    func cancelSignIn() {
        signInCancellation?.cancel()
    }

    /// Waits for ccshift to hand over the sign-in page's URL, then opens it in
    /// the private sign-in window. Closing that window stops the sign-in.
    private func openSignInWindow(whenWrittenTo file: URL, cancellation: ProcessCancellation) async {
        while !Task.isCancelled, !cancellation.isCancelled {
            if let text = try? String(contentsOf: file, encoding: .utf8) {
                try? FileManager.default.removeItem(at: file)
                let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
                guard let url = URL(string: trimmed), SignInURLPolicy.isAllowed(url) else {
                    accountChangeError = "ccshift handed over an unexpected sign-in address, so it was not opened."
                    cancelSignIn()
                    return
                }
                signInPresenter.present(url) { [weak self] in self?.cancelSignIn() }
                return
            }
            try? await Task.sleep(for: .milliseconds(150))
        }
    }

    /// Cancel or Escape: a failed attempt's error belongs to the sheet only,
    /// and a sign-in still waiting in the browser is stopped.
    func addAccountSheetDismissed() {
        if isSigningIn { cancelSignIn() }
        if !isChangingAccounts { accountChangeError = nil }
    }

    func removeAccount(_ account: Account) {
        accountPendingRemoval = nil
        guard !isChangingAccounts, !isSigningIn, switchingAccountID == nil else { return }
        guard let client else {
            accountChangeError = CLIError.executableNotFound.localizedDescription
            return
        }
        guard let known = accounts.first(where: { $0.id == account.id && $0.email == account.email }) else {
            accountChangeError = "That account is unavailable. Refresh the account list and try again."
            return
        }
        let emailIsShared = accounts.filter { $0.email == known.email }.count > 1
        isChangingAccounts = true
        accountChangeError = nil
        accountChangeNotice = nil
        Task { [weak self] in
            let result = await Self.load { try client.removeAccount(known, emailIsShared: emailIsShared) }
            guard let self else { return }
            self.isChangingAccounts = false
            switch result {
            case let .success(report):
                self.accountChangeNotice = Self.notice(for: report)
                self.refresh()
            case let .failure(message):
                self.accountChangeError = message
                self.refresh(keepingAlert: true)
            }
        }
    }

    /// Holds an account out of automatic switching, or puts it back. A
    /// failure is also shown in the menu, where the toggle is too.
    func setAccountDisabled(_ account: Account, _ disabled: Bool) {
        guard !isChangingAccounts, !isSigningIn, switchingAccountID == nil else { return }
        guard let client else {
            accountChangeError = CLIError.executableNotFound.localizedDescription
            return
        }
        guard let known = accounts.first(where: { $0.id == account.id && $0.email == account.email }) else {
            accountChangeError = "That account is unavailable. Refresh the account list and try again."
            return
        }
        let emailIsShared = accounts.filter { $0.email == known.email }.count > 1
        isChangingAccounts = true
        accountChangeError = nil
        accountChangeNotice = nil
        Task { [weak self] in
            let result = await Self.load {
                try client.setAccountDisabled(known, disabled: disabled, emailIsShared: emailIsShared)
            }
            guard let self else { return }
            self.isChangingAccounts = false
            switch result {
            case let .success(report):
                self.accountChangeNotice = Self.notice(for: report)
                self.refresh()
            case let .failure(message):
                self.accountChangeError = message
                self.alertMessage = message
                // Still shown once the list is read again, like a failed switch.
                self.refresh(afterSwitchWarning: message, keepingAlert: true)
            }
        }
    }

    static func notice(for report: AccountChangeReport) -> String {
        guard let account = report.account else { return "ccshift updated the accounts." }
        let name = account.displayName
        switch report.action {
        case "added":
            return "Added \(name) as account \(account.number)."
        case "refreshed":
            return "\(name) is already account \(account.number). ccshift saved its current login."
        case "removed":
            let stillSignedIn = report.wasActive == true ? " Claude Code is still signed in to it." : ""
            return "Removed \(name) (account \(account.number)).\(stillSignedIn)"
        case "disabled":
            let empty = report.rotationEmpty == true ? " No account is left for automatic switching." : ""
            return "\(name) won’t be used for automatic switching.\(empty)"
        case "enabled":
            return "\(name) is back in automatic switching."
        default:
            return "ccshift updated the accounts."
        }
    }

    // MARK: Updates

    /// The app's model, with the settings saved under the app's old name and
    /// automatic update checks. Tests and previews build the model directly,
    /// so they never read those settings or reach GitHub.
    static func makeForApp() -> MenuBarModel {
        LegacySettings.importOnce()
        SystemUpdateNotifier.shared.install()
        let model = MenuBarModel(updateNotifier: SystemUpdateNotifier.shared)
        model.startAutomaticUpdateChecks()
        model.startLiveUsage()
        return model
    }

    func setShowUsageInMenuBar(_ enabled: Bool) {
        showUsageInMenuBar = enabled
        defaults.set(enabled, forKey: "ccshiftShowUsageInMenuBar")
    }

    // MARK: Live usage

    /// Keeps the shown numbers current without spending anything: every
    /// `openInterval` seconds while the popover is open, every `idleInterval`
    /// while it is closed and the menu bar shows usage, and not at all when
    /// neither needs it. Each read is `list --json --cached` — the local usage
    /// store, which the status-line feed and the poller keep current — so it
    /// never touches the usage endpoint's budget. Starts at once, so the menu
    /// bar has its numbers at launch instead of after the first click.
    func startLiveUsage(openInterval: TimeInterval = 4, idleInterval: TimeInterval = 15) {
        guard liveTask == nil else { return }
        let tick = max(0.02, min(1, openInterval / 4))
        liveTask = Task { [weak self] in
            var lastRead = Date.distantPast
            while !Task.isCancelled {
                guard let self else { return }
                let wanted = self.isPopoverVisible ? openInterval : idleInterval
                let needed = self.isPopoverVisible || self.showUsageInMenuBar
                if needed, self.liveSupported, Date().timeIntervalSince(lastRead) >= wanted {
                    lastRead = Date()
                    await self.refreshCached()
                }
                try? await Task.sleep(for: .seconds(tick))
            }
        }
    }

    /// The popover opened or closed. Opening shows what is on file right away
    /// and, when the last full refresh is stale, runs one (the plan-bounded
    /// kind that may fetch what is due).
    func popoverVisibilityChanged(_ visible: Bool) {
        guard visible != isPopoverVisible else { return }
        isPopoverVisible = visible
        guard visible else { return }
        let stale = lastFullRefresh.map {
            Date().timeIntervalSince($0) > Self.fullRefreshOnOpenAfter
        } ?? true
        if stale, !isRefreshing, !isChangingAccounts {
            refresh(keepingAlert: true)
        }
    }

    /// One store-only read. Quiet by design: it never shows the spinner, never
    /// raises an alert, and yields to anything that changes the roster.
    func refreshCached() async {
        guard let client, liveSupported, !liveReadInFlight, !isRefreshing,
              !isChangingAccounts, switchingAccountID == nil else { return }
        liveReadInFlight = true
        defer { liveReadInFlight = false }
        let generation = refreshGeneration
        let result = await Self.load { try client.dashboard(cached: true) }
        guard refreshGeneration == generation, !isRefreshing,
              !isChangingAccounts, switchingAccountID == nil else { return }
        switch result {
        case let .success(snapshot):
            liveFailures = 0
            if snapshot.accounts != accounts { accounts = snapshot.accounts }
        case .failure:
            liveFailures += 1
            if liveFailures >= Self.liveFailureLimit { liveSupported = false }
        }
    }

    /// The update to show in the menu: none once the person skipped that version.
    var visibleUpdate: AvailableUpdate? {
        guard let availableUpdate, availableUpdate.version != skippedUpdateVersion else { return nil }
        return availableUpdate
    }

    /// Why the command line tool should be upgraded too, if it should.
    var cliUpdateHint: String? {
        guard let cliVersion else { return nil }
        if let availableUpdate, AppVersion.isNewer(availableUpdate.version, than: cliVersion) {
            return "Update the command line tool too: run ccshift upgrade in Terminal."
        }
        if AppVersion.isNewer(appVersion, than: cliVersion) {
            return "The command line tool (\(cliVersion)) is older than this app. Run ccshift upgrade in Terminal."
        }
        return nil
    }

    /// Checks shortly after launch, then whenever the last check is a day old.
    func startAutomaticUpdateChecks(initialDelay: TimeInterval = 8, pollInterval: TimeInterval = 60 * 60) {
        guard updateCheckTask == nil else { return }
        updateCheckTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(initialDelay))
            while !Task.isCancelled {
                guard let self else { return }
                let due = self.lastUpdateCheck.map { Date().timeIntervalSince($0) >= Self.updateCheckInterval } ?? true
                if self.autoCheckForUpdates, due {
                    await self.checkForUpdates(userInitiated: false)
                }
                try? await Task.sleep(for: .seconds(pollInterval))
            }
        }
    }

    func checkForUpdates(userInitiated: Bool) async {
        guard !isCheckingForUpdates else { return }
        isCheckingForUpdates = true
        updateCheckError = nil
        refreshCLIVersion()
        do {
            let release = try await releaseFetcher.latestRelease()
            let update = release.update(over: appVersion)
            availableUpdate = update
            let now = Date()
            lastUpdateCheck = now
            defaults.set(now, forKey: "ccshiftLastUpdateCheck")
            if let update, update.version != skippedUpdateVersion,
               defaults.string(forKey: "ccshiftNotifiedUpdateVersion") != update.version {
                defaults.set(update.version, forKey: "ccshiftNotifiedUpdateVersion")
                if !userInitiated { updateNotifier?.notify(update) }
            }
        } catch {
            updateCheckError = error.localizedDescription
        }
        isCheckingForUpdates = false
    }

    func setAutoCheckForUpdates(_ enabled: Bool) {
        autoCheckForUpdates = enabled
        defaults.set(enabled, forKey: "ccshiftAutoCheckForUpdates")
    }

    func skipAvailableUpdate() {
        guard let availableUpdate else { return }
        skippedUpdateVersion = availableUpdate.version
        defaults.set(availableUpdate.version, forKey: "ccshiftSkippedUpdateVersion")
    }

    func downloadUpdate() {
        guard let availableUpdate else { return }
        NSWorkspace.shared.open(availableUpdate.downloadURL ?? availableUpdate.releaseURL)
    }

    func openReleaseNotes() {
        guard let availableUpdate else { return }
        NSWorkspace.shared.open(availableUpdate.releaseURL)
    }

    func refreshCLIVersion() {
        guard let client else { return }
        Task { [weak self] in
            let result = await Self.load { try client.cliVersion() }
            guard let self else { return }
            if case let .success(version) = result { self.cliVersion = version }
        }
    }

    func setAutoSwitchEnabled(_ enabled: Bool) {
        guard autoSwitchEnabled != enabled else { return }
        autoSwitchEnabled = enabled
        defaults.set(enabled, forKey: "ccshiftAutoSwitchEnabled")
        if enabled {
            startAutoSwitchLoop()
        } else {
            stopAutoSwitchLoop()
        }
    }

    /// One switch point for both windows.
    func setAutoSwitchThreshold(_ threshold: Double) {
        setAutoSwitchThreshold5h(threshold)
        setAutoSwitchThreshold7d(threshold)
    }

    func setAutoSwitchThreshold5h(_ threshold: Double) {
        let bounded = min(max(threshold, 50), 99.9)
        autoSwitchThreshold5h = bounded
        defaults.set(bounded, forKey: "ccshiftAutoSwitchThreshold5h")
    }

    func setAutoSwitchThreshold7d(_ threshold: Double) {
        let bounded = min(max(threshold, 50), 99.9)
        autoSwitchThreshold7d = bounded
        defaults.set(bounded, forKey: "ccshiftAutoSwitchThreshold7d")
    }

    func setAutoSwitchDryRun(_ dryRun: Bool) {
        autoSwitchDryRun = dryRun
        defaults.set(dryRun, forKey: "ccshiftAutoSwitchDryRun")
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
        liveTask?.cancel()
        liveTask = nil
        updateCheckTask?.cancel()
        cancelSignIn()
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
                if self.isRefreshing || self.isChangingAccounts {
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
        let threshold5h = autoSwitchThreshold5h
        let threshold7d = autoSwitchThreshold7d
        let dryRun = autoSwitchDryRun
        let result = await Self.load {
            try client.autoSwitchOnce(
                threshold5h: threshold5h,
                threshold7d: threshold7d,
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
            autoSwitchLastResult = "\(outcome.summary) Threshold: \(outcome.thresholdText)"
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
        var autoSwitchThreshold5h = 90.0
        var autoSwitchThreshold7d = 90.0
        var autoSwitchDryRun = false
        var autoSwitchIsRunning = false
        var autoSwitchLastResult: String?
        var launchAtLoginStatus: LaunchAtLoginStatus = .notRegistered
        var launchAtLoginError: String?
        var settingsTab: SettingsTab = .accounts
        var isAddAccountSheetPresented = false
        var accountPendingRemoval: Account?
        var currentLogin: CurrentLoginState = .unknown
        var isChangingAccounts = false
        var isSigningIn = false
        var availableUpdate: AvailableUpdate?
        var lastUpdateCheck: Date?
        var updateCheckError: String?
        var cliVersion: String?
        var appVersion = "1.1.0"
        var accountChangeError: String?
        var accountChangeNotice: String?
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
            launchAtLoginManager: PreviewLaunchAtLoginManager(status: state.launchAtLoginStatus),
            appVersion: state.appVersion
        )
        model.accounts = state.accounts
        model.isRefreshing = state.isRefreshing
        model.rosterReadFailed = state.rosterReadFailed
        model.switchingAccountID = state.switchingAccountID
        model.lastUpdated = state.lastUpdated
        model.alertMessage = state.alertMessage
        model.executablePath = state.executablePath
        model.autoSwitchEnabled = state.autoSwitchEnabled
        model.autoSwitchThreshold5h = state.autoSwitchThreshold5h
        model.autoSwitchThreshold7d = state.autoSwitchThreshold7d
        model.autoSwitchDryRun = state.autoSwitchDryRun
        model.autoSwitchIsRunning = state.autoSwitchIsRunning
        model.autoSwitchLastResult = state.autoSwitchLastResult
        model.launchAtLoginError = state.launchAtLoginError
        model.settingsTab = state.settingsTab
        model.isAddAccountSheetPresented = state.isAddAccountSheetPresented
        model.accountPendingRemoval = state.accountPendingRemoval
        model.currentLogin = state.currentLogin
        model.isChangingAccounts = state.isChangingAccounts
        model.isSigningIn = state.isSigningIn
        model.availableUpdate = state.availableUpdate
        model.lastUpdateCheck = state.lastUpdateCheck
        model.updateCheckError = state.updateCheckError
        model.cliVersion = state.cliVersion
        model.accountChangeError = state.accountChangeError
        model.accountChangeNotice = state.accountChangeNotice
        return model
    }

    static var preview: MenuBarModel { preview(PreviewState()) }
}
