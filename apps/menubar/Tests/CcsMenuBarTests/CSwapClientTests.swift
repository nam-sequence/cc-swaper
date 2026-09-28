import Darwin
import Foundation
import XCTest
@testable import CcsMenuBar

final class CSwapClientTests: XCTestCase {
    func testEngineDashboardAndSwitchSupportUnmanagedSourceAndAlreadyActiveNoOp() throws {
        let executable = try fakeCLI(engineScript)
        let client = try CSwapClient(executableURL: executable)

        let dashboard = try client.dashboard()
        XCTAssertEqual(dashboard.activeAccountNumber, 1)
        XCTAssertEqual(dashboard.accounts.filter { $0.mode == .engine }.count, 2)
        XCTAssertEqual(dashboard.accounts.filter { $0.mode == .legacy }.count, 0)
        XCTAssertEqual(dashboard.accounts[0].alias, "main")
        XCTAssertEqual(dashboard.accounts[0].visibleUsage?.fiveHour?.pct, 17)
        XCTAssertEqual(dashboard.accounts[1].lastGoodUsage?.sevenDay?.pct, 61)
        XCTAssertTrue(dashboard.accounts[1].isStale)
        XCTAssertEqual(dashboard.launchBackend, "accounts")

        let switched = try client.switchEngineAccount(to: 2)
        XCTAssertTrue(switched.switched)
        XCTAssertNil(switched.from?.number)
        XCTAssertEqual(switched.to.number, 2)

        let alreadyActive = try client.switchEngineAccount(to: 1)
        XCTAssertFalse(alreadyActive.switched)
        XCTAssertNil(alreadyActive.from?.number)
        XCTAssertEqual(alreadyActive.to.number, 1)
    }

    func testEmptyEngineListFallsBackToLegacyProfilesAndUsage() throws {
        let executable = try fakeCLI(legacyScript)
        let client = try CSwapClient(executableURL: executable)

        let dashboard = try client.dashboard()
        XCTAssertEqual(dashboard.launchBackend, "legacy")
        XCTAssertEqual(dashboard.activeProfileName, "main")
        XCTAssertEqual(dashboard.accounts.map(\.profileName), ["main", "team"])
        XCTAssertTrue(dashboard.accounts.allSatisfy { $0.mode == .legacy })
        XCTAssertEqual(dashboard.accounts[0].visibleUsage?.fiveHour?.pct, 17)
        XCTAssertEqual(dashboard.accounts[0].visibleUsage?.scoped?.first?.model, "Sonnet")
        XCTAssertEqual(dashboard.accounts[1].usageStatus, "Usage query failed")

        XCTAssertEqual(try client.switchLegacyProfile(to: "team"), "team")
    }

    func testCombinedDashboardKeepsBothCollectionsAndTheirIndependentSelection() throws {
        let client = try CSwapClient(executableURL: fakeCLI(combinedScript))

        let dashboard = try client.dashboard()
        XCTAssertEqual(dashboard.accounts.count, 2)
        XCTAssertEqual(dashboard.accounts.map(\.mode), [.engine, .legacy])
        XCTAssertEqual(dashboard.accounts.map(\.active), [true, true])
        XCTAssertEqual(dashboard.launchBackend, "legacy")
        XCTAssertEqual(try client.switchEngineAccount(to: 7).to.number, 7)
        XCTAssertEqual(try client.switchLegacyProfile(to: "old-main"), "old-main")

        let routeRace = try client.switchEngineAccount(to: 8)
        XCTAssertFalse(routeRace.routingChanged ?? true)
        XCTAssertEqual(routeRace.launchBackend, "legacy")
        XCTAssertEqual(routeRace.routingWarning, "A newer Existing Profiles selection remained active.")
    }

    func testNonzeroJSONErrorEnvelopeSurfacesTheUpstreamReason() throws {
        let executable = try fakeCLI("""
        #!/bin/sh
        printf '%s\\n' '{"schemaVersion":1,"error":{"code":"not-ready","message":"Engine is not ready"}}'
        exit 1
        """)
        let client = try CSwapClient(executableURL: executable)

        XCTAssertThrowsError(try client.dashboard()) { error in
            XCTAssertEqual(error as? CLIError, .commandRejected("Engine is not ready"))
            XCTAssertEqual(error.localizedDescription, "Engine is not ready")
        }
    }

    func testUnsupportedSchemaIsRejectedBeforePayloadDecoding() throws {
        let executable = try fakeCLI("""
        #!/bin/sh
        printf '%s\\n' '{"schemaVersion":9,"accounts":[]}'
        """)
        let client = try CSwapClient(executableURL: executable)

        XCTAssertThrowsError(try client.dashboard()) { error in
            XCTAssertEqual(error as? CLIError, .unsupportedSchema(9))
        }
    }

    func testInvalidLegacyProfileNameNeverRunsACommand() throws {
        let executable = try fakeCLI(engineScript)
        let client = try CSwapClient(executableURL: executable)

        XCTAssertThrowsError(try client.switchLegacyProfile(to: "../../unexpected")) { error in
            XCTAssertEqual(error as? CLIError, .invalidProfileName)
        }
    }

    func testTimeoutKillsTheEntireDedicatedProcessGroup() throws {
        let folder = try tempFolder()
        let groupPath = folder.appendingPathComponent("process-group")
        let script = #"""
        #!/bin/sh
        /bin/sleep 30 &
        child=$!
        group=$$
        printf '%s\n' "$group" > '__PROCESS_GROUP_FILE__'
        wait "$child"
        """#
        let executable = try fakeCLI(script, replacements: ["__PROCESS_GROUP_FILE__": groupPath.path])
        let client = try CSwapClient(executableURL: executable, timeout: 0.5)

        let started = Date()
        XCTAssertThrowsError(try client.dashboard()) { error in
            XCTAssertEqual(error as? CLIError, .timedOut)
        }
        XCTAssertLessThan(Date().timeIntervalSince(started), 3)
        let groupText = try String(contentsOf: groupPath, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)
        let groupID = try XCTUnwrap(Int32(groupText))
        XCTAssertEqual(kill(-groupID, 0), -1, "the timed-out ccs child process group must be gone")
    }

    func testStableLauncherSymlinkIsPreservedAndTargetIsRevalidated() throws {
        let folder = try tempFolder()
        let first = try fakeCLI(switchReply("alpha"))
        let second = try fakeCLI(switchReply("beta"))
        let launcher = folder.appendingPathComponent("ccs")
        try FileManager.default.createSymbolicLink(at: launcher, withDestinationURL: first)
        let client = try CSwapClient(executableURL: launcher)

        let stableLauncher = launcher.deletingLastPathComponent()
            .resolvingSymlinksInPath()
            .appendingPathComponent(launcher.lastPathComponent)
            .standardizedFileURL
        XCTAssertEqual(client.executableURL.path, stableLauncher.path)
        XCTAssertEqual(try client.switchLegacyProfile(to: "alpha"), "alpha")

        try FileManager.default.removeItem(at: launcher)
        try FileManager.default.createSymbolicLink(at: launcher, withDestinationURL: second)
        XCTAssertEqual(try client.switchLegacyProfile(to: "beta"), "beta")

        try FileManager.default.removeItem(at: second)
        XCTAssertThrowsError(try client.switchLegacyProfile(to: "beta")) { error in
            XCTAssertEqual(error as? CLIError, .invalidExecutablePath)
        }
    }

    func testResolverRejectsUnsafePathFallbackAndChosenExecutables() throws {
        let folder = try tempFolder()
        let fakeHome = folder.appendingPathComponent("fake-home", isDirectory: true)
        try FileManager.default.createDirectory(at: fakeHome, withIntermediateDirectories: false)
        let unsafeBin = folder.appendingPathComponent("unsafe-bin", isDirectory: true)
        try FileManager.default.createDirectory(
            at: unsafeBin,
            withIntermediateDirectories: false,
            attributes: [.posixPermissions: 0o777]
        )
        let pathCandidate = try writeFakeExecutable(at: unsafeBin.appendingPathComponent("ccs"), permissions: 0o700)
        let defaults = UserDefaults(suiteName: "ccs-menubar-resolver-\(UUID().uuidString)")!
        let resolved = CLIResolver.executable(
            environment: ["PATH": unsafeBin.path],
            defaults: defaults,
            info: [:],
            homeDirectory: fakeHome.path,
            standardBinDirectories: []
        )
        XCTAssertNil(resolved)
        XCTAssertNil(CLIResolver.validExecutable(pathCandidate.path))

        let unsafeGrandparent = folder.appendingPathComponent("unsafe-grandparent", isDirectory: true)
        let protectedChild = unsafeGrandparent.appendingPathComponent("protected", isDirectory: true)
        try FileManager.default.createDirectory(
            at: unsafeGrandparent,
            withIntermediateDirectories: false,
            attributes: [.posixPermissions: 0o777]
        )
        try FileManager.default.createDirectory(
            at: protectedChild,
            withIntermediateDirectories: false,
            attributes: [.posixPermissions: 0o700]
        )
        let protectedExecutable = try writeFakeExecutable(at: protectedChild.appendingPathComponent("ccs"), permissions: 0o700)
        XCTAssertNil(CLIResolver.validExecutable(protectedExecutable.path))

        let safeBin = folder.appendingPathComponent("safe-bin", isDirectory: true)
        try FileManager.default.createDirectory(
            at: safeBin,
            withIntermediateDirectories: false,
            attributes: [.posixPermissions: 0o700]
        )
        let unsafeTargetDirectory = folder.appendingPathComponent("unsafe-target", isDirectory: true)
        try FileManager.default.createDirectory(
            at: unsafeTargetDirectory,
            withIntermediateDirectories: false,
            attributes: [.posixPermissions: 0o777]
        )
        let unsafeTarget = try writeFakeExecutable(at: unsafeTargetDirectory.appendingPathComponent("ccs-real"), permissions: 0o700)
        let chosenSymlink = safeBin.appendingPathComponent("ccs")
        try FileManager.default.createSymbolicLink(at: chosenSymlink, withDestinationURL: unsafeTarget)
        XCTAssertNil(CLIResolver.validExecutable(chosenSymlink.path))

        let unsafeFile = try writeFakeExecutable(at: safeBin.appendingPathComponent("ccs-world-writable"), permissions: 0o777)
        XCTAssertNil(CLIResolver.validExecutable(unsafeFile.path))
    }

    func testResolverRejectsExtendedACLWriteGrantsOnFilesAndAncestors() throws {
        let folder = try tempFolder()
        let safeBin = folder.appendingPathComponent("acl-bin", isDirectory: true)
        try FileManager.default.createDirectory(
            at: safeBin,
            withIntermediateDirectories: false,
            attributes: [.posixPermissions: 0o700]
        )

        let readOnlyACLExecutable = try writeFakeExecutable(
            at: safeBin.appendingPathComponent("ccs-read-acl"),
            permissions: 0o700
        )
        try addACL("everyone allow read", to: readOnlyACLExecutable)
        XCTAssertNotNil(CLIResolver.validExecutable(readOnlyACLExecutable.path))

        let writableACLExecutable = try writeFakeExecutable(
            at: safeBin.appendingPathComponent("ccs-write-acl"),
            permissions: 0o700
        )
        try addACL("everyone allow write", to: writableACLExecutable)
        XCTAssertNil(CLIResolver.validExecutable(writableACLExecutable.path))

        let aclParent = folder.appendingPathComponent("acl-parent", isDirectory: true)
        try FileManager.default.createDirectory(
            at: aclParent,
            withIntermediateDirectories: false,
            attributes: [.posixPermissions: 0o700]
        )
        try addACL("everyone allow delete_child", to: aclParent)
        let childExecutable = try writeFakeExecutable(
            at: aclParent.appendingPathComponent("ccs"),
            permissions: 0o700
        )
        XCTAssertNil(CLIResolver.validExecutable(childExecutable.path))
    }

    func testRootOwnedStickyTempDirectoryAllowsProtectedCurrentUserChild() throws {
        let stickyDirectory = URL(fileURLWithPath: "/private/tmp", isDirectory: true)
        var metadata = Darwin.stat()
        guard stickyDirectory.path.withCString({ Darwin.lstat($0, &metadata) }) == 0,
              metadata.st_uid == 0,
              metadata.st_mode & mode_t(0o1000) != 0
        else {
            throw XCTSkip("No root-owned sticky /private/tmp directory is available.")
        }

        let child = stickyDirectory.appendingPathComponent("ccs-menubar-sticky-test-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(
            at: child,
            withIntermediateDirectories: false,
            attributes: [.posixPermissions: 0o700]
        )
        addTeardownBlock { try? FileManager.default.removeItem(at: child) }
        let executable = try writeFakeExecutable(at: child.appendingPathComponent("ccs"), permissions: 0o700)
        XCTAssertNotNil(CLIResolver.validExecutable(executable.path))
    }

    func testLegacyUsageTimeoutExceedsCcsFortyFiveSecondLimit() throws {
        let client = try CSwapClient(executableURL: fakeCLI(engineScript))
        XCTAssertGreaterThanOrEqual(client.timeout, 45)
    }

    func testLegacyUsageTimeoutScalesPastOneEightAccountBatch() throws {
        let profileJSON = (1...9).map { index in
            "{\"name\":\"old-\(index)\",\"kind\":\"managed\",\"selected\":\(index == 1 ? "true" : "false"),\"signedIn\":true,\"authMethod\":\"Claude subscription\"}"
        }.joined(separator: ",")
        let executable = try fakeCLI(largeLegacyScript, replacements: ["__PROFILES__": profileJSON])
        let client = try CSwapClient(executableURL: executable, timeout: 0.5)

        let dashboard = try client.dashboard()
        XCTAssertEqual(dashboard.accounts.filter { $0.mode == .legacy }.count, 9)
        XCTAssertGreaterThanOrEqual(client.legacyUsageTimeout(profileCount: 9), 125)
    }

    private func fakeCLI(_ script: String, replacements: [String: String] = [:]) throws -> URL {
        let folder = try tempFolder()
        let executable = folder.appendingPathComponent("ccs-fake")
        var resolvedScript = script
        for (placeholder, value) in replacements {
            resolvedScript = resolvedScript.replacingOccurrences(of: placeholder, with: value)
        }
        try Data(resolvedScript.utf8).write(to: executable, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: executable.path)
        return executable
    }

    private func writeFakeExecutable(at url: URL, permissions: Int) throws -> URL {
        try Data("#!/bin/sh\nexit 0\n".utf8).write(to: url, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: permissions], ofItemAtPath: url.path)
        return url
    }

    private func addACL(_ entry: String, to url: URL) throws {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/chmod")
        process.arguments = ["+a", entry, url.path]
        try process.run()
        process.waitUntilExit()
        XCTAssertEqual(process.terminationStatus, 0, "the test filesystem must support extended ACLs")
    }

    private func tempFolder() throws -> URL {
        let folder = FileManager.default.temporaryDirectory
            .appendingPathComponent("ccs-menubar-tests-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(
            at: folder,
            withIntermediateDirectories: false,
            attributes: [.posixPermissions: 0o700]
        )
        addTeardownBlock { try? FileManager.default.removeItem(at: folder) }
        return folder
    }

    private func switchReply(_ selected: String) -> String {
        """
        #!/bin/sh
        printf '{"schemaVersion":1,"selected":"%s"}\n' '\(selected)'
        """
    }

    private var engineScript: String {
        #"""
        #!/bin/sh
        if [ "$1" = "routing" ]; then
          printf '%s\n' '{"schemaVersion":1,"launchBackend":"accounts","selectedLegacyProfile":null}'
        elif [ "$1" = "accounts" ] && [ "$2" = "list" ]; then
          cat <<'JSON'
        {"schemaVersion":1,"activeAccountNumber":1,"accounts":[{"number":1,"email":"main@example.test","organizationName":"Max","alias":"main","active":true,"usageStatus":"ok","usage":{"fiveHour":{"pct":17,"resetsAt":"in 2h"},"sevenDay":{"pct":32,"resetsAt":"Friday"}},"usageAgeSeconds":5},{"number":2,"email":"team@example.test","organizationName":"Team","alias":"work","active":false,"usageStatus":"stale","usageAgeSeconds":300,"lastGoodUsage":{"sevenDay":{"pct":61,"resetsAt":"Monday"}}}]}
        JSON
        elif [ "$1" = "accounts" ] && [ "$2" = "status" ]; then
          cat <<'JSON'
        {"schemaVersion":1,"active":{"number":1,"email":"main@example.test","managed":true,"usageStatus":"ok"}}
        JSON
        elif [ "$1" = "accounts" ] && [ "$2" = "switch" ]; then
          if [ "$3" = "1" ]; then
            printf '%s\n' '{"schemaVersion":1,"switched":false,"from":{"number":null,"email":"unmanaged@example.test"},"to":{"number":1,"email":"main@example.test"},"reason":"already-active"}'
          else
            printf '{"schemaVersion":1,"switched":true,"from":{"number":null,"email":"unmanaged@example.test"},"to":{"number":%s,"email":"target@example.test"}}\n' "$3"
          fi
        elif [ "$1" = "list" ]; then
          printf '%s\n' '{"schemaVersion":1,"selected":null,"profiles":[],"launchBackend":"accounts","selectedLegacyProfile":null}'
        else
          exit 64
        fi
        """#
    }

    private var legacyScript: String {
        #"""
        #!/bin/sh
        if [ "$1" = "routing" ]; then
          printf '%s\n' '{"schemaVersion":1,"launchBackend":"legacy","selectedLegacyProfile":"main"}'
        elif [ "$1" = "accounts" ] && [ "$2" = "list" ]; then
          printf '%s\n' '{"schemaVersion":1,"activeAccountNumber":null,"accounts":[]}'
        elif [ "$1" = "accounts" ] && [ "$2" = "status" ]; then
          printf '%s\n' '{"schemaVersion":1,"active":null}'
        elif [ "$1" = "list" ]; then
          cat <<'JSON'
        {"schemaVersion":1,"selected":"main","profiles":[{"name":"main","kind":"default","selected":true,"signedIn":true,"authMethod":"Claude subscription","email":"main@example.test","organization":"Max"},{"name":"team","kind":"managed","selected":false,"signedIn":true,"authMethod":"Claude subscription","email":"team@example.test"}]}
        JSON
        elif [ "$1" = "usage" ]; then
          cat <<'JSON'
        {"source":"Claude Code /usage","checked_at":"2026-09-29T00:00:00Z","accounts":[{"profile":"main","plan":"Max","five_hour":{"used_percent":17,"resets_at":"in 2h"},"seven_day":{"used_percent":32,"resets_at":"Friday"},"model_weekly":[{"model":"Sonnet","used_percent":0,"resets_at":"Monday"}]},{"profile":"team","error":"Usage query failed"}]}
        JSON
          exit 1
        elif [ "$1" = "switch" ]; then
          printf '{"schemaVersion":1,"selected":"%s","launchBackend":"legacy"}\n' "$2"
        else
          exit 64
        fi
        """#
    }

    private var combinedScript: String {
        #"""
        #!/bin/sh
        if [ "$1" = "routing" ]; then
          printf '%s\n' '{"schemaVersion":1,"launchBackend":"legacy","selectedLegacyProfile":"old-main"}'
        elif [ "$1" = "accounts" ] && [ "$2" = "list" ]; then
          printf '%s\n' '{"schemaVersion":1,"activeAccountNumber":7,"accounts":[{"number":7,"email":"engine@example.test","organizationName":"Team","alias":"engine","active":true,"usageStatus":"ok","usage":{"fiveHour":{"pct":4},"sevenDay":{"pct":5}}}]}'
        elif [ "$1" = "accounts" ] && [ "$2" = "status" ]; then
          printf '%s\n' '{"schemaVersion":1,"active":{"number":7,"email":"engine@example.test","managed":true}}'
        elif [ "$1" = "accounts" ] && [ "$2" = "switch" ]; then
          if [ "$3" = "8" ]; then
            printf '%s\n' '{"schemaVersion":1,"switched":true,"from":{"number":7},"to":{"number":8},"launchBackend":"legacy","routingChanged":false,"routingWarning":"A newer Existing Profiles selection remained active."}'
          else
            printf '{"schemaVersion":1,"switched":true,"from":{"number":null},"to":{"number":%s},"launchBackend":"accounts","routingChanged":true}\n' "$3"
          fi
        elif [ "$1" = "list" ]; then
          printf '%s\n' '{"schemaVersion":1,"selected":"old-main","profiles":[{"name":"old-main","kind":"managed","selected":true,"signedIn":true,"authMethod":"Claude subscription","email":"old@example.test"}]}'
        elif [ "$1" = "usage" ]; then
          printf '%s\n' '{"accounts":[{"profile":"old-main","five_hour":{"used_percent":8},"seven_day":{"used_percent":9}}]}'
        elif [ "$1" = "switch" ]; then
          printf '{"schemaVersion":1,"selected":"%s","launchBackend":"legacy"}\n' "$2"
        else
          exit 64
        fi
        """#
    }

    private var largeLegacyScript: String {
        #"""
        #!/bin/sh
        if [ "$1" = "routing" ]; then
          printf '%s\n' '{"schemaVersion":1,"launchBackend":"legacy","selectedLegacyProfile":"old-1"}'
        elif [ "$1" = "accounts" ] && [ "$2" = "list" ]; then
          printf '%s\n' '{"schemaVersion":1,"activeAccountNumber":null,"accounts":[]}'
        elif [ "$1" = "accounts" ] && [ "$2" = "status" ]; then
          printf '%s\n' '{"schemaVersion":1,"active":null}'
        elif [ "$1" = "list" ]; then
          printf '%s\n' '{"schemaVersion":1,"selected":"old-1","profiles":[__PROFILES__]}'
        elif [ "$1" = "usage" ]; then
          /bin/sleep 0.8
          printf '%s\n' '{"accounts":[]}'
        else
          exit 64
        fi
        """#
    }
}
