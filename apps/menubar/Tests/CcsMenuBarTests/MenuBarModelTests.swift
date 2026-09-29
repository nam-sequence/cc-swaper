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
