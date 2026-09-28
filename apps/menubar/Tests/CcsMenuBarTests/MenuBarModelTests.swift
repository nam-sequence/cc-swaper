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

    func testAutoSwitchIsOptInSerialAndPassesThresholdAndDryRunToFakeCLI() async throws {
        let folder = try makeFolder()
        let countPath = folder.appendingPathComponent("ticks")
        let lockPath = folder.appendingPathComponent("tick-lock")
        let overlapPath = folder.appendingPathComponent("overlap")
        let argsPath = folder.appendingPathComponent("args")
        let script = autoSwitchScript(routing: "accounts")
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
        let didLoadAccounts = await waitUntil { model.launchBackend == "accounts" && !model.accounts.isEmpty }
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

    func testAutoSwitchStaysPausedWhenLegacyBackendIsSelected() async throws {
        let folder = try makeFolder()
        let countPath = folder.appendingPathComponent("ticks")
        let lockPath = folder.appendingPathComponent("tick-lock")
        let overlapPath = folder.appendingPathComponent("overlap")
        let argsPath = folder.appendingPathComponent("args")
        let executable = try fakeCLI(autoSwitchScript(routing: "legacy"), replacements: [
            "__TICK_COUNT__": countPath.path,
            "__TICK_LOCK__": lockPath.path,
            "__OVERLAP__": overlapPath.path,
            "__AUTO_ARGS__": argsPath.path,
        ])
        let defaults = isolatedDefaults()
        defaults.set(true, forKey: "ccsAutoSwitchEnabled")
        let model = MenuBarModel(
            defaults: defaults,
            executableURL: executable,
            launchAtLoginManager: FakeLaunchAtLoginManager(),
            autoSwitchInterval: 0.05
        )
        XCTAssertTrue(model.autoSwitchEnabled)
        let didLoadLegacyRouting = await waitUntil { model.launchBackend == "legacy" && !model.accounts.isEmpty }
        XCTAssertTrue(didLoadLegacyRouting)
        try await Task.sleep(for: .milliseconds(250))
        XCTAssertFalse(FileManager.default.fileExists(atPath: countPath.path))
        XCTAssertTrue(model.autoSwitchAvailabilityMessage?.contains("Engine Accounts") == true)
        model.setAutoSwitchEnabled(false)
    }

    func testPersistedAutoSwitchRefreshesRoutingAtStartupWithoutOpeningMenu() async throws {
        let folder = try makeFolder()
        let countPath = folder.appendingPathComponent("ticks")
        let lockPath = folder.appendingPathComponent("tick-lock")
        let overlapPath = folder.appendingPathComponent("overlap")
        let argsPath = folder.appendingPathComponent("args")
        let executable = try fakeCLI(autoSwitchScript(routing: "legacy"), replacements: [
            "__TICK_COUNT__": countPath.path,
            "__TICK_LOCK__": lockPath.path,
            "__OVERLAP__": overlapPath.path,
            "__AUTO_ARGS__": argsPath.path,
        ])
        let defaults = isolatedDefaults()
        defaults.set(true, forKey: "ccsAutoSwitchEnabled")
        let model = MenuBarModel(
            defaults: defaults,
            executableURL: executable,
            launchAtLoginManager: FakeLaunchAtLoginManager(),
            autoSwitchInterval: 0.05
        )

        let loadedWithoutOpeningMenu = await waitUntil { model.launchBackend == "legacy" && !model.accounts.isEmpty }
        XCTAssertTrue(loadedWithoutOpeningMenu)
        XCTAssertFalse(FileManager.default.fileExists(atPath: countPath.path))
        model.setAutoSwitchEnabled(false)
    }

    func testRoutingRaceRefreshesSelectionAndMenuTitleFollowsCurrentBackend() async throws {
        let folder = try makeFolder()
        let backendPath = folder.appendingPathComponent("backend")
        try Data("accounts".utf8).write(to: backendPath)
        let executable = try fakeCLI(routingRaceScript, replacements: ["__BACKEND_FILE__": backendPath.path])
        let model = MenuBarModel(
            defaults: isolatedDefaults(),
            executableURL: executable,
            launchAtLoginManager: FakeLaunchAtLoginManager()
        )

        model.refresh()
        let engineLoaded = await waitUntil { model.launchBackend == "accounts" && model.accounts.count == 3 }
        XCTAssertTrue(engineLoaded)
        XCTAssertEqual(model.selectedAccount?.mode, .engine)
        XCTAssertEqual(model.menuTitle, "engine-main")

        let target = try XCTUnwrap(model.accounts.first(where: { $0.number == 8 }))
        model.switchTo(target)
        let legacyWonRace = await waitUntil {
            model.launchBackend == "legacy"
                && model.alertMessage == "A newer Existing Profiles selection remained active."
        }
        XCTAssertTrue(legacyWonRace)
        XCTAssertEqual(model.selectedAccount?.mode, .legacy)
        XCTAssertEqual(model.menuTitle, "old-main")

        try Data("accounts".utf8).write(to: backendPath)
        model.refresh()
        let engineRouteRestored = await waitUntil { model.launchBackend == "accounts" }
        XCTAssertTrue(engineRouteRestored)
        XCTAssertEqual(model.selectedAccount?.mode, .engine)
        XCTAssertEqual(model.menuTitle, "engine-main")

        try Data("unknown".utf8).write(to: backendPath)
        model.refresh()
        let unknownRouteLoaded = await waitUntil { model.launchBackend == "unknown" }
        XCTAssertTrue(unknownRouteLoaded)
        XCTAssertNil(model.selectedAccount)
        XCTAssertEqual(model.menuTitle, "ccs")
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

    private func autoSwitchScript(routing: String) -> String {
        #"""
        #!/bin/sh
        if [ "$1" = "routing" ]; then
          printf '%s\n' '{"schemaVersion":1,"launchBackend":"__ROUTING__","selectedLegacyProfile":"main"}'
        elif [ "$1" = "accounts" ] && [ "$2" = "list" ]; then
          printf '%s\n' '{"schemaVersion":1,"activeAccountNumber":1,"accounts":[{"number":1,"email":"main@example.test","organizationName":"Max","alias":"main","active":true,"usageStatus":"ok","usage":{"fiveHour":{"pct":12},"sevenDay":{"pct":18}}}]}'
        elif [ "$1" = "accounts" ] && [ "$2" = "status" ]; then
          printf '%s\n' '{"schemaVersion":1,"active":{"number":1,"email":"main@example.test","managed":true}}'
        elif [ "$1" = "list" ]; then
          printf '%s\n' '{"schemaVersion":1,"selected":"main","profiles":[]}'
        elif [ "$1" = "accounts" ] && [ "$2" = "auto" ]; then
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
        """#.replacingOccurrences(of: "__ROUTING__", with: routing)
    }

    private var routingRaceScript: String {
        #"""
        #!/bin/sh
        if [ "$1" = "routing" ]; then
          backend=$(/bin/cat '__BACKEND_FILE__')
          printf '{"schemaVersion":1,"launchBackend":"%s","selectedLegacyProfile":"old-main"}\n' "$backend"
        elif [ "$1" = "accounts" ] && [ "$2" = "list" ]; then
          printf '%s\n' '{"schemaVersion":1,"activeAccountNumber":7,"accounts":[{"number":7,"email":"engine@example.test","organizationName":"Team","alias":"engine-main","active":true,"usageStatus":"ok","usage":{"fiveHour":{"pct":4},"sevenDay":{"pct":5}}},{"number":8,"email":"target@example.test","organizationName":"Team","alias":"engine-target","active":false,"usageStatus":"ok","usage":{"fiveHour":{"pct":6},"sevenDay":{"pct":7}}}]}'
        elif [ "$1" = "accounts" ] && [ "$2" = "status" ]; then
          printf '%s\n' '{"schemaVersion":1,"active":{"number":7,"email":"engine@example.test","managed":true}}'
        elif [ "$1" = "accounts" ] && [ "$2" = "switch" ] && [ "$3" = "8" ]; then
          printf legacy > '__BACKEND_FILE__'
          printf '%s\n' '{"schemaVersion":1,"switched":true,"from":{"number":7},"to":{"number":8},"launchBackend":"legacy","routingChanged":false,"routingWarning":"A newer Existing Profiles selection remained active."}'
        elif [ "$1" = "list" ]; then
          printf '%s\n' '{"schemaVersion":1,"selected":"old-main","profiles":[{"name":"old-main","kind":"managed","selected":true,"signedIn":true,"authMethod":"Claude subscription","email":"old@example.test"}]}'
        elif [ "$1" = "usage" ]; then
          printf '%s\n' '{"accounts":[{"profile":"old-main","five_hour":{"used_percent":8},"seven_day":{"used_percent":9}}]}'
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
