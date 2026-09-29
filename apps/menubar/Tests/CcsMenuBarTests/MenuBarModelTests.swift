import Foundation
import XCTest
@testable import CcsMenuBar

@MainActor
final class MenuBarModelTests: XCTestCase {
    func testLaunchAtLoginToggleUsesInjectableServiceAndApprovalState() {
        let service = FakeLaunchAtLoginManager()
        let model = MenuBarModel(
            defaults: isolatedDefaults(),
            executableURL: nil,
            launchAtLoginManager: service
        )

        XCTAssertFalse(model.launchAtLoginRequested)
        XCTAssertEqual(service.changeCount, 0)
        model.setLaunchAtLoginEnabled(true)
        XCTAssertEqual(service.changeCount, 1)
        XCTAssertEqual(model.launchAtLoginStatus, .requiresApproval)
        XCTAssertTrue(model.launchAtLoginRequested)
        XCTAssertNotNil(model.launchAtLoginMessage)

        model.openLoginItemsSettings()
        XCTAssertEqual(service.openSettingsCount, 1)
        model.setLaunchAtLoginEnabled(false)
        XCTAssertEqual(model.launchAtLoginStatus, .notRegistered)
        XCTAssertFalse(model.launchAtLoginRequested)
    }

    func testLaunchAtLoginFailureIsShownWithoutRegisteringAnythingOnInit() {
        let service = FakeLaunchAtLoginManager()
        service.failure = FakeLaunchError.denied
        let model = MenuBarModel(
            defaults: isolatedDefaults(),
            executableURL: nil,
            launchAtLoginManager: service
        )

        XCTAssertEqual(service.changeCount, 0)
        model.setLaunchAtLoginEnabled(true)
        XCTAssertFalse(model.launchAtLoginRequested)
        XCTAssertTrue(model.launchAtLoginError?.contains("System Settings") == true)
    }

    func testNeverRegisteredAppIsShownAsOffNotUnavailable() {
        XCTAssertEqual(SystemLaunchAtLoginManager.status(for: .notFound), .notRegistered)
        XCTAssertEqual(SystemLaunchAtLoginManager.status(for: .notRegistered), .notRegistered)
        XCTAssertEqual(SystemLaunchAtLoginManager.status(for: .enabled), .enabled)
        XCTAssertEqual(SystemLaunchAtLoginManager.status(for: .requiresApproval), .requiresApproval)
    }

    func testAutoSwitchIsOptInSerialAndPassesThresholdAndDryRunToFakeCLI() async throws {
        let folder = try makeFolder()
        let countPath = folder.appendingPathComponent("ticks")
        let lockPath = folder.appendingPathComponent("tick-lock")
        let overlapPath = folder.appendingPathComponent("overlap")
        let argsPath = folder.appendingPathComponent("args")
        let script = autoSwitchScript()
        let executable = try fakeCLI(script, replacements: [
            "__TICK_COUNT__": countPath.path,
            "__TICK_LOCK__": lockPath.path,
            "__OVERLAP__": overlapPath.path,
            "__AUTO_ARGS__": argsPath.path,
        ])
        let defaults = isolatedDefaults()
        let model = MenuBarModel(
            defaults: defaults,
            executableURL: executable,
            launchAtLoginManager: FakeLaunchAtLoginManager(),
            autoSwitchInterval: 0.05
        )

        XCTAssertFalse(model.autoSwitchEnabled)
        model.refresh()
        let didLoadAccounts = await waitUntil { !model.accounts.isEmpty }
        XCTAssertTrue(didLoadAccounts)
        XCTAssertFalse(FileManager.default.fileExists(atPath: countPath.path))

        model.setAutoSwitchThreshold(84)
        model.setAutoSwitchDryRun(true)
        model.setAutoSwitchEnabled(true)
        let didRunMultipleTicks = await waitUntil(timeout: 4) {
            let value = try? String(contentsOf: countPath, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)
            return (Int(value ?? "0") ?? 0) >= 2
        }
        XCTAssertTrue(didRunMultipleTicks)
        XCTAssertFalse(FileManager.default.fileExists(atPath: overlapPath.path))
        let arguments = try String(contentsOf: argsPath, encoding: .utf8)
        XCTAssertTrue(arguments.contains("--threshold 84.0"))
        XCTAssertTrue(arguments.contains("--dry-run"))

        model.setAutoSwitchEnabled(false)
        XCTAssertFalse(model.autoSwitchEnabled)
        XCTAssertFalse(defaults.bool(forKey: "ccsAutoSwitchEnabled"))
        let didStop = await waitUntil(timeout: 3) { !model.autoSwitchIsRunning }
        XCTAssertTrue(didStop)
    }

    func testPersistedAutoSwitchRefreshesAtStartupWithoutOpeningMenuAndTicks() async throws {
        let folder = try makeFolder()
        let countPath = folder.appendingPathComponent("ticks")
        let executable = try fakeCLI(autoSwitchScript(), replacements: [
            "__TICK_COUNT__": countPath.path,
            "__TICK_LOCK__": folder.appendingPathComponent("tick-lock").path,
            "__OVERLAP__": folder.appendingPathComponent("overlap").path,
            "__AUTO_ARGS__": folder.appendingPathComponent("args").path,
        ])
        let defaults = isolatedDefaults()
        defaults.set(true, forKey: "ccsAutoSwitchEnabled")
        let model = MenuBarModel(
            defaults: defaults,
            executableURL: executable,
            launchAtLoginManager: FakeLaunchAtLoginManager(),
            autoSwitchInterval: 0.05
        )
        let loaded = await waitUntil { !model.accounts.isEmpty }
        XCTAssertTrue(loaded)
        let ticked = await waitUntil(timeout: 3) { FileManager.default.fileExists(atPath: countPath.path) }
        XCTAssertTrue(ticked)
        model.setAutoSwitchEnabled(false)
    }

    func testAutoSwitchIsPausedUntilAnAccountExists() async throws {
        let executable = try fakeCLI(#"""
        #!/bin/sh
        if [ "$1" = "list" ]; then
          printf '%s\n' '{"schemaVersion":1,"activeAccountNumber":null,"accounts":[]}'
        else
          exit 64
        fi
        """#, replacements: [:])
        let defaults = isolatedDefaults()
        defaults.set(true, forKey: "ccsAutoSwitchEnabled")
        let model = MenuBarModel(
            defaults: defaults,
            executableURL: executable,
            launchAtLoginManager: FakeLaunchAtLoginManager(),
            autoSwitchInterval: 0.05
        )
        let refreshed = await waitUntil { model.lastUpdated != nil }
        XCTAssertTrue(refreshed)
        XCTAssertTrue(model.accounts.isEmpty)
        XCTAssertNotNil(model.autoSwitchAvailabilityMessage)
        model.setAutoSwitchEnabled(false)
    }

    func testSwitchRefreshesActiveAccountAndMenuTitleAndSurfacesCLIWarnings() async throws {
        let folder = try makeFolder()
        let activePath = folder.appendingPathComponent("active")
        try Data("7".utf8).write(to: activePath)
        let executable = try fakeCLI(switchScript, replacements: ["__ACTIVE_FILE__": activePath.path])
        let model = MenuBarModel(
            defaults: isolatedDefaults(),
            executableURL: executable,
            launchAtLoginManager: FakeLaunchAtLoginManager()
        )

        model.refresh()
        let loaded = await waitUntil { model.accounts.count == 2 }
        XCTAssertTrue(loaded)
        XCTAssertEqual(model.selectedAccount?.number, 7)
        XCTAssertEqual(model.menuTitle, "engine-main")

        let target = try XCTUnwrap(model.accounts.first(where: { $0.number == 8 }))
        model.switchTo(target)
        let switched = await waitUntil {
            model.selectedAccount?.number == 8
                && model.alertMessage == "Claude Code sessions are running; restart them to use the new account."
        }
        XCTAssertTrue(switched)
        XCTAssertEqual(model.menuTitle, "engine-target")
    }

    func testAutoSwitchRetriesAFailedRosterReadAndTicksWhenAccountsAppear() async throws {
        let folder = try makeFolder()
        let stage = folder.appendingPathComponent("stage")
        try Data("fail".utf8).write(to: stage)
        let executable = try fakeCLI(#"""
        #!/bin/sh
        stage=$(/bin/cat '__STAGE__')
        if [ "$1" = "list" ]; then
          if [ "$stage" = "fail" ]; then
            printf '%s\n' 'Keychain is locked' >&2
            exit 1
          fi
          printf '%s\n' '{"schemaVersion":1,"activeAccountNumber":1,"accounts":[{"number":1,"email":"one@example.test","organizationName":"","active":true,"usageStatus":"ok","usage":{"fiveHour":{"pct":4},"sevenDay":{"pct":5}}}]}'
        elif [ "$1" = "auto" ]; then
          printf '%s\n' '{"schemaVersion":1,"event":"no-switch","ts":"2026-09-29T00:00:00Z","reason":"below-threshold","detail":"4% < 90%"}'
          exit 2
        else
          exit 64
        fi
        """#, replacements: ["__STAGE__": stage.path])
        let defaults = isolatedDefaults()
        defaults.set(true, forKey: "ccsAutoSwitchEnabled")
        let model = MenuBarModel(
            defaults: defaults,
            executableURL: executable,
            launchAtLoginManager: FakeLaunchAtLoginManager(),
            autoSwitchInterval: 0.1
        )
        let failed = await waitUntil { model.rosterReadFailed && !model.isRefreshing }
        XCTAssertTrue(failed)
        XCTAssertEqual(model.autoSwitchAvailabilityMessage, "Paused: could not read accounts from ccshift. Retrying every minute.")
        XCTAssertNotNil(model.alertMessage)

        try Data("ok".utf8).write(to: stage)
        let ticked = await waitUntil { model.autoSwitchLastResult?.hasPrefix("below-threshold") == true }
        XCTAssertTrue(ticked)
        XCTAssertEqual(model.accounts.count, 1)
        XCTAssertFalse(model.rosterReadFailed)
        XCTAssertNil(model.autoSwitchAvailabilityMessage)
        model.setAutoSwitchEnabled(false)
    }

    func testDisabledAccountCanStillBeSwitchedToExplicitly() async throws {
        let folder = try makeFolder()
        let activePath = folder.appendingPathComponent("active")
        try Data("7".utf8).write(to: activePath)
        let executable = try fakeCLI(switchScript, replacements: [
            "__ACTIVE_FILE__": activePath.path,
            #""alias":"engine-target","#: #""alias":"engine-target","disabled":true,"#,
        ])
        let model = MenuBarModel(
            defaults: isolatedDefaults(),
            executableURL: executable,
            launchAtLoginManager: FakeLaunchAtLoginManager()
        )

        model.refresh()
        let loaded = await waitUntil { model.accounts.count == 2 }
        XCTAssertTrue(loaded)
        let target = try XCTUnwrap(model.accounts.first(where: { $0.number == 8 }))
        XCTAssertTrue(target.disabled)
        model.switchTo(target)
        let switched = await waitUntil { model.selectedAccount?.number == 8 }
        XCTAssertTrue(switched)
    }

    func testMenuTitleFallsBackToCommandNameWhenNoAccountIsActive() async throws {
        let executable = try fakeCLI(#"""
        #!/bin/sh
        if [ "$1" = "list" ]; then
          printf '%s\n' '{"schemaVersion":1,"activeAccountNumber":null,"accounts":[{"number":1,"email":"a@example.test","organizationName":"","active":false,"usageStatus":"api_key","usage":null}]}'
        else
          exit 64
        fi
        """#, replacements: [:])
        let model = MenuBarModel(
            defaults: isolatedDefaults(),
            executableURL: executable,
            launchAtLoginManager: FakeLaunchAtLoginManager()
        )
        model.refresh()
        let loaded = await waitUntil { model.accounts.count == 1 }
        XCTAssertTrue(loaded)
        XCTAssertNil(model.selectedAccount)
        XCTAssertEqual(model.menuTitle, "ccshift")
        XCTAssertEqual(model.accounts.first?.usageMessage, "Usage is unavailable for an API key account.")
    }

    func testAddingTheCurrentLoginClosesTheSheetAndRefreshesTheList() async throws {
        let folder = try makeFolder()
        let statePath = folder.appendingPathComponent("accounts")
        try Data("1".utf8).write(to: statePath)
        let executable = try fakeCLI(accountsScript, replacements: ["__STATE__": statePath.path])
        let model = MenuBarModel(
            defaults: isolatedDefaults(),
            executableURL: executable,
            launchAtLoginManager: FakeLaunchAtLoginManager()
        )
        model.refresh()
        let loaded = await waitUntil { model.accounts.count == 1 }
        XCTAssertTrue(loaded)

        model.settingsTab = .general
        model.beginAddingAccount()
        XCTAssertEqual(model.settingsTab, .accounts)
        XCTAssertTrue(model.isAddAccountSheetPresented)
        model.checkCurrentLogin()
        let checked = await waitUntil {
            model.currentLogin == .signedIn(CurrentLogin(
                email: "new@example.test", managed: false, number: nil, alias: nil, organizationName: nil
            ))
        }
        XCTAssertTrue(checked)

        model.addCurrentAccount(alias: "  dev  ")
        let added = await waitUntil { model.accounts.count == 2 && !model.isAddAccountSheetPresented }
        XCTAssertTrue(added)
        XCTAssertEqual(model.accountChangeNotice, "Added dev as account 2.")
        XCTAssertNil(model.accountChangeError)
        XCTAssertEqual(model.accounts.last?.alias, "dev")
    }

    func testRemovingAnAccountConfirmsThroughTheModelAndReportsFailures() async throws {
        let folder = try makeFolder()
        let statePath = folder.appendingPathComponent("accounts")
        try Data("2".utf8).write(to: statePath)
        let executable = try fakeCLI(accountsScript, replacements: ["__STATE__": statePath.path])
        let model = MenuBarModel(
            defaults: isolatedDefaults(),
            executableURL: executable,
            launchAtLoginManager: FakeLaunchAtLoginManager()
        )
        model.refresh()
        let loaded = await waitUntil { model.accounts.count == 2 }
        XCTAssertTrue(loaded)
        let second = try XCTUnwrap(model.accounts.last)

        model.requestRemoval(of: second)
        XCTAssertEqual(model.accountPendingRemoval?.id, second.id)
        model.removeAccount(second)
        XCTAssertNil(model.accountPendingRemoval)
        let removed = await waitUntil { model.accounts.count == 1 && !model.isChangingAccounts }
        XCTAssertTrue(removed)
        XCTAssertEqual(model.accountChangeNotice, "Removed dev (account 2).")

        // The fake refuses to remove the last account, like a live session would.
        let first = try XCTUnwrap(model.accounts.first)
        model.removeAccount(first)
        let failed = await waitUntil { model.accountChangeError != nil && !model.isChangingAccounts }
        XCTAssertTrue(failed)
        XCTAssertEqual(model.accountChangeError, "Account 1 is in use by a running session.")
        XCTAssertEqual(model.accounts.count, 1)
    }

    func testTurningAutomaticSwitchingOffAndOnForAnAccount() async throws {
        let folder = try makeFolder()
        let statePath = folder.appendingPathComponent("disabled")
        try Data("no".utf8).write(to: statePath)
        let script = #"""
        #!/bin/sh
        state=$(cat '__STATE__')
        case "$1" in
          list)
            if [ "$state" = "yes" ]; then flag=',"disabled":true'; else flag=''; fi
            printf '%s\n' '{"schemaVersion":1,"activeAccountNumber":1,"accounts":[{"number":1,"email":"main@example.test","alias":"main","active":true,"usageStatus":"ok"},{"number":2,"email":"work@example.test","alias":"work","active":false,"usageStatus":"ok"'"$flag"'}]}' ;;
          disable)
            if [ "$2" = "main@example.test" ]; then
              printf '%s\n' '{"schemaVersion":1,"error":{"type":"ConfigError","message":"Nope."}}'; exit 1
            fi
            printf 'yes' > '__STATE__'
            printf '%s\n' '{"schemaVersion":1,"action":"disabled","account":{"number":2,"email":"work@example.test","alias":"work"},"changed":true,"rotationEmpty":false}' ;;
          enable)
            printf 'no' > '__STATE__'
            printf '%s\n' '{"schemaVersion":1,"action":"enabled","account":{"number":2,"email":"work@example.test","alias":"work"},"changed":true,"rotationEmpty":false}' ;;
        esac
        """#
        let model = MenuBarModel(
            defaults: isolatedDefaults(),
            executableURL: try fakeCLI(script, replacements: ["__STATE__": statePath.path]),
            launchAtLoginManager: FakeLaunchAtLoginManager()
        )
        model.refresh()
        let loaded = await waitUntil { model.accounts.count == 2 }
        XCTAssertTrue(loaded)
        let work = try XCTUnwrap(model.accounts.last)
        XCTAssertFalse(work.disabled)

        model.setAccountDisabled(work, true)
        let disabled = await waitUntil { model.accounts.last?.disabled == true && !model.isChangingAccounts }
        XCTAssertTrue(disabled)
        XCTAssertEqual(model.accountChangeNotice, "work won’t be used for automatic switching.")

        model.setAccountDisabled(try XCTUnwrap(model.accounts.last), false)
        let enabled = await waitUntil { model.accounts.last?.disabled == false && !model.isChangingAccounts }
        XCTAssertTrue(enabled)
        XCTAssertEqual(model.accountChangeNotice, "work is back in automatic switching.")

        // A failure shows in Settings and in the menu, where the toggle also is.
        model.setAccountDisabled(try XCTUnwrap(model.accounts.first), true)
        let failed = await waitUntil { model.accountChangeError == "Nope." && !model.isChangingAccounts }
        XCTAssertTrue(failed)
        XCTAssertEqual(model.alertMessage, "Nope.")
    }

    func testBrowserSignInAddsTheAccountWithoutTouchingTheBusyFlag() async throws {
        let folder = try makeFolder()
        let statePath = folder.appendingPathComponent("accounts")
        try Data("1".utf8).write(to: statePath)
        let executable = try fakeCLI(accountsScript, replacements: ["__STATE__": statePath.path])
        let model = MenuBarModel(
            defaults: isolatedDefaults(),
            executableURL: executable,
            launchAtLoginManager: FakeLaunchAtLoginManager()
        )
        model.refresh()
        let loaded = await waitUntil { model.accounts.count == 1 }
        XCTAssertTrue(loaded)

        model.beginAddingAccount()
        model.signInAndAddAccount(email: " new@example.test ", sso: true, alias: "dev")
        XCTAssertTrue(model.isSigningIn)
        // A browser sign-in can take minutes; switching and auto-switch go on.
        XCTAssertFalse(model.isChangingAccounts)
        let added = await waitUntil { model.accounts.count == 2 && !model.isSigningIn }
        XCTAssertTrue(added)
        XCTAssertFalse(model.isAddAccountSheetPresented)
        XCTAssertEqual(model.accountChangeNotice, "Added dev as account 2.")
    }

    func testStoppingABrowserSignInReportsNothing() async throws {
        let folder = try makeFolder()
        let statePath = folder.appendingPathComponent("accounts")
        try Data("1".utf8).write(to: statePath)
        let executable = try fakeCLI(accountsScript, replacements: ["__STATE__": statePath.path])
        let model = MenuBarModel(
            defaults: isolatedDefaults(),
            executableURL: executable,
            launchAtLoginManager: FakeLaunchAtLoginManager()
        )

        model.beginAddingAccount()
        model.signInAndAddAccount(email: "slow@example.test", sso: false, alias: "")
        XCTAssertTrue(model.isSigningIn)
        try await Task.sleep(for: .milliseconds(200))
        model.addAccountSheetDismissed()
        let stopped = await waitUntil(timeout: 4) { !model.isSigningIn }
        XCTAssertTrue(stopped)
        XCTAssertNil(model.accountChangeError)
        XCTAssertNil(model.accountChangeNotice)
    }

    func testPrivateWindowSignInOpensTheHandedOffPageAndClosesItWhenDone() async throws {
        let presenter = FakeSignInPresenter()
        let model = MenuBarModel(
            defaults: isolatedDefaults(),
            executableURL: try fakeCLI(handoffScript(url: "https://claude.com/cai/oauth/authorize?state=abc", then: "finish"), replacements: [:]),
            launchAtLoginManager: FakeLaunchAtLoginManager(),
            signInPresenter: presenter
        )

        model.signInAndAddAccount(email: "", sso: false, alias: "", opener: .privateWindow)
        let presented = await waitUntil { presenter.presented.count == 1 }
        XCTAssertTrue(presented)
        XCTAssertEqual(presenter.presented.first?.absoluteString, "https://claude.com/cai/oauth/authorize?state=abc")
        let finished = await waitUntil(timeout: 4) { !model.isSigningIn }
        XCTAssertTrue(finished)
        XCTAssertEqual(presenter.dismissals, 1)
        XCTAssertEqual(model.accountChangeNotice, "Added fresh@example.test as account 5.")
    }

    func testClosingThePrivateWindowStopsTheSignIn() async throws {
        let presenter = FakeSignInPresenter()
        presenter.closesAfterPresenting = true
        let model = MenuBarModel(
            defaults: isolatedDefaults(),
            executableURL: try fakeCLI(handoffScript(url: "https://claude.com/cai/oauth/authorize", then: "wait"), replacements: [:]),
            launchAtLoginManager: FakeLaunchAtLoginManager(),
            signInPresenter: presenter
        )

        model.signInAndAddAccount(email: "", sso: false, alias: "", opener: .privateWindow)
        let stopped = await waitUntil(timeout: 4) { !model.isSigningIn }
        XCTAssertTrue(stopped)
        XCTAssertEqual(presenter.presented.count, 1)
        XCTAssertNil(model.accountChangeError)
        XCTAssertNil(model.accountChangeNotice)
    }

    func testAHandedOffAddressOutsideClaudeIsNotOpened() async throws {
        let presenter = FakeSignInPresenter()
        let model = MenuBarModel(
            defaults: isolatedDefaults(),
            executableURL: try fakeCLI(handoffScript(url: "https://example.test/phish", then: "wait"), replacements: [:]),
            launchAtLoginManager: FakeLaunchAtLoginManager(),
            signInPresenter: presenter
        )

        model.signInAndAddAccount(email: "", sso: false, alias: "", opener: .privateWindow)
        let stopped = await waitUntil(timeout: 4) { !model.isSigningIn }
        XCTAssertTrue(stopped)
        XCTAssertTrue(presenter.presented.isEmpty)
        XCTAssertEqual(model.accountChangeError, "ccshift handed over an unexpected sign-in address, so it was not opened.")
    }

    /// A fake `ccshift add --login --handoff-file=PATH`: writes the sign-in URL
    /// like the $BROWSER hook does, then finishes or waits to be stopped.
    private func handoffScript(url: String, then ending: String) -> String {
        #"""
        #!/bin/sh
        for arg in "$@"; do
          case "$arg" in --handoff-file=*) file="${arg#--handoff-file=}" ;; esac
        done
        [ -n "$file" ] || { echo "no handoff file" >&2; exit 2; }
        printf '%s' '__URL__' > "$file"
        if [ '__ENDING__' = wait ]; then exec /bin/sleep 30; fi
        /bin/sleep 0.4
        printf '%s\n' '{"schemaVersion":1,"action":"added","account":{"number":5,"email":"fresh@example.test"}}'
        """#
        .replacingOccurrences(of: "__URL__", with: url)
        .replacingOccurrences(of: "__ENDING__", with: ending)
    }

    private var accountsScript: String {
        #"""
        #!/bin/sh
        count=$(cat '__STATE__')
        row1='{"number":1,"email":"main@example.test","alias":"main","active":true,"usageStatus":"ok","usage":{"fiveHour":{"pct":12},"sevenDay":{"pct":18}}}'
        row2='{"number":2,"email":"new@example.test","alias":"dev","active":false,"usageStatus":"ok","usage":{"fiveHour":{"pct":3},"sevenDay":{"pct":4}}}'
        case "$1" in
          list)
            if [ "$count" = "2" ]; then rows="$row1,$row2"; else rows="$row1"; fi
            printf '%s\n' "{\"schemaVersion\":1,\"activeAccountNumber\":1,\"accounts\":[$rows]}" ;;
          status)
            printf '%s\n' '{"schemaVersion":1,"active":{"email":"new@example.test","managed":false}}' ;;
          add)
            case "$*" in
              *--email=slow@example.test*) exec /bin/sleep 30 ;;
              *"--login --json --sso --email=new@example.test --alias=dev"*) ;;
              *--login*) printf '%s\n' '{"schemaVersion":1,"error":{"type":"ConfigError","message":"unexpected arguments"}}'; exit 1 ;;
            esac
            printf '2' > '__STATE__'
            printf '%s\n' '{"schemaVersion":1,"action":"added","account":{"number":2,"email":"new@example.test","alias":"dev"}}' ;;
          remove)
            if [ "$2" = "main@example.test" ]; then
              printf '%s\n' '{"schemaVersion":1,"error":{"type":"ConfigError","message":"Account 1 is in use by a running session."}}'
              exit 1
            fi
            printf '1' > '__STATE__'
            printf '%s\n' '{"schemaVersion":1,"action":"removed","account":{"number":2,"email":"new@example.test","alias":"dev"},"wasActive":false}' ;;
        esac
        """#
    }

    private func isolatedDefaults() -> UserDefaults {
        let suite = "ccs-menubar-tests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defaults.removePersistentDomain(forName: suite)
        return defaults
    }

    private func makeFolder() throws -> URL {
        let folder = FileManager.default.temporaryDirectory
            .appendingPathComponent("ccs-menubar-model-tests-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(
            at: folder,
            withIntermediateDirectories: false,
            attributes: [.posixPermissions: 0o700]
        )
        addTeardownBlock { try? FileManager.default.removeItem(at: folder) }
        return folder
    }

    private func fakeCLI(_ script: String, replacements: [String: String]) throws -> URL {
        let folder = try makeFolder()
        var contents = script
        for (placeholder, value) in replacements {
            contents = contents.replacingOccurrences(of: placeholder, with: value)
        }
        let executable = folder.appendingPathComponent("ccs-fake")
        try Data(contents.utf8).write(to: executable, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: executable.path)
        return executable
    }

    private func waitUntil(
        timeout: TimeInterval = 2,
        condition: @MainActor () -> Bool
    ) async -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if condition() { return true }
            try? await Task.sleep(for: .milliseconds(20))
        }
        return condition()
    }

    private func autoSwitchScript() -> String {
        #"""
        #!/bin/sh
        if [ "$1" = "list" ]; then
          printf '%s\n' '{"schemaVersion":1,"activeAccountNumber":1,"accounts":[{"number":1,"email":"main@example.test","organizationName":"Max","alias":"main","active":true,"usageStatus":"ok","usage":{"fiveHour":{"pct":12},"sevenDay":{"pct":18}}}]}'
        elif [ "$1" = "auto" ]; then
          count=0
          if [ -f '__TICK_COUNT__' ]; then count=$(/bin/cat '__TICK_COUNT__'); fi
          count=$((count + 1))
          printf '%s' "$count" > '__TICK_COUNT__'
          if ! /bin/mkdir '__TICK_LOCK__' 2>/dev/null; then printf overlap > '__OVERLAP__'; fi
          printf '%s\n' "$*" > '__AUTO_ARGS__'
          /bin/sleep 0.2
          /bin/rmdir '__TICK_LOCK__' 2>/dev/null || true
          printf '%s\n' '{"schemaVersion":1,"event":"no-switch","reason":"below-threshold","detail":"plenty of usage remains"}'
          exit 2
        else
          exit 64
        fi
        """#
    }

    private var switchScript: String {
        #"""
        #!/bin/sh
        active=$(/bin/cat '__ACTIVE_FILE__')
        if [ "$1" = "list" ]; then
          a7=false; a8=false
          if [ "$active" = "7" ]; then a7=true; else a8=true; fi
          printf '{"schemaVersion":1,"activeAccountNumber":%s,"accounts":[{"number":7,"email":"engine@example.test","organizationName":"","alias":"engine-main","active":%s,"usageStatus":"ok","usage":{"fiveHour":{"pct":4},"sevenDay":{"pct":5}}},{"number":8,"email":"target@example.test","organizationName":"","alias":"engine-target","active":%s,"usageStatus":"ok","usage":{"fiveHour":{"pct":6},"sevenDay":{"pct":7}}}]}\n' "$active" "$a7" "$a8"
        elif [ "$1" = "switch" ] && [ "$2" = "8" ]; then
          printf 8 > '__ACTIVE_FILE__'
          printf '%s\n' '{"schemaVersion":1,"switched":true,"from":{"number":7,"email":"engine@example.test"},"to":{"number":8,"email":"target@example.test"},"strategy":"direct","reason":"switched","message":"Switched to Account-8","warnings":["Claude Code sessions are running; restart them to use the new account."]}'
        else
          exit 64
        fi
        """#
    }
}

@MainActor
private final class FakeSignInPresenter: SignInWindowPresenting {
    var presented: [URL] = []
    var dismissals = 0
    var closesAfterPresenting = false

    func present(_ url: URL, onClose: @escaping @MainActor () -> Void) {
        presented.append(url)
        if closesAfterPresenting {
            Task { @MainActor in
                try? await Task.sleep(for: .milliseconds(100))
                onClose()
            }
        }
    }

    func dismiss() {
        dismissals += 1
    }
}

@MainActor
private final class FakeLaunchAtLoginManager: LaunchAtLoginManaging {
    var currentStatus: LaunchAtLoginStatus = .notRegistered
    var changeCount = 0
    var openSettingsCount = 0
    var failure: FakeLaunchError?

    func status() -> LaunchAtLoginStatus { currentStatus }

    func setEnabled(_ enabled: Bool) throws -> LaunchAtLoginStatus {
        changeCount += 1
        if let failure { throw failure }
        currentStatus = enabled ? .requiresApproval : .notRegistered
        return currentStatus
    }

    func openLoginItemsSettings() { openSettingsCount += 1 }
}

private enum FakeLaunchError: Error {
    case denied
}
