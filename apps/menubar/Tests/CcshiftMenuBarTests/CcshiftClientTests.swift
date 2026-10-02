import Darwin
import Foundation
import XCTest
@testable import CcshiftMenuBar

final class CcshiftClientTests: XCTestCase {
    func testDashboardAndSwitchSupportUnmanagedSourceAndAlreadyActiveNoOp() throws {
        let executable = try fakeCLI(engineScript)
        let client = try CcshiftClient(executableURL: executable)

        let dashboard = try client.dashboard()
        XCTAssertEqual(dashboard.activeAccountNumber, 1)
        XCTAssertEqual(dashboard.accounts.count, 2)
        XCTAssertEqual(dashboard.accounts[0].alias, "main")
        XCTAssertEqual(dashboard.accounts[0].visibleUsage?.fiveHour?.pct, 17)
        XCTAssertEqual(dashboard.accounts[1].lastGoodUsage?.sevenDay?.pct, 61)
        XCTAssertEqual(dashboard.accounts[1].usageStatus, "unavailable")
        XCTAssertTrue(dashboard.accounts[1].isStale)

        let switched = try client.switchEngineAccount(to: 2)
        XCTAssertTrue(switched.switched)
        XCTAssertNil(switched.from)
        XCTAssertEqual(switched.to.number, 2)

        let alreadyActive = try client.switchEngineAccount(to: 1)
        XCTAssertFalse(alreadyActive.switched)
        XCTAssertEqual(alreadyActive.from?.number, 1)
        XCTAssertEqual(alreadyActive.to.number, 1)
    }

    func testCachedDashboardAsksTheToolForTheStoreOnly() throws {
        let executable = try fakeCLI("""
        #!/bin/sh
        if [ "$1 $2 $3" = "list --json --cached" ]; then
          printf '%s\\n' '{"schemaVersion":1,"activeAccountNumber":1,"accounts":[{"number":1,"email":"a@example.test","organizationName":"","alias":null,"active":true,"usageStatus":"ok","usage":{"fiveHour":{"pct":41},"sevenDay":{"pct":9}}}]}'
        else
          exit 64
        fi
        """)
        let client = try CcshiftClient(executableURL: executable)

        XCTAssertEqual(try client.dashboard(cached: true).accounts.first?.visibleUsage?.fiveHour?.pct, 41)
        XCTAssertThrowsError(try client.dashboard(), "the plain form must not take the cached path")
    }

    func testNonzeroJSONErrorEnvelopeSurfacesTheUpstreamReason() throws {
        let executable = try fakeCLI("""
        #!/bin/sh
        printf '%s\\n' '{"schemaVersion":1,"error":{"type":"ConfigError","message":"No accounts are managed yet"}}'
        exit 1
        """)
        let client = try CcshiftClient(executableURL: executable)

        XCTAssertThrowsError(try client.dashboard()) { error in
            XCTAssertEqual(error as? CLIError, .commandRejected("No accounts are managed yet"))
            XCTAssertEqual(error.localizedDescription, "No accounts are managed yet")
        }
    }

    func testUnsupportedSchemaIsRejectedBeforePayloadDecoding() throws {
        let executable = try fakeCLI("""
        #!/bin/sh
        printf '%s\\n' '{"schemaVersion":9,"accounts":[]}'
        """)
        let client = try CcshiftClient(executableURL: executable)

        XCTAssertThrowsError(try client.dashboard()) { error in
            XCTAssertEqual(error as? CLIError, .unsupportedSchema(9))
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
        let client = try CcshiftClient(executableURL: executable, timeout: 0.5)

        let started = Date()
        XCTAssertThrowsError(try client.dashboard()) { error in
            XCTAssertEqual(error as? CLIError, .timedOut)
        }
        XCTAssertLessThan(Date().timeIntervalSince(started), 3)
        let groupText = try String(contentsOf: groupPath, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)
        let groupID = try XCTUnwrap(Int32(groupText))
        XCTAssertEqual(kill(-groupID, 0), -1, "the timed-out ccshift child process group must be gone")
    }

    func testStableLauncherSymlinkIsPreservedAndTargetIsRevalidated() throws {
        let folder = try tempFolder()
        let first = try fakeCLI(switchReply(1))
        let second = try fakeCLI(switchReply(2))
        let launcher = folder.appendingPathComponent("ccshift")
        try FileManager.default.createSymbolicLink(at: launcher, withDestinationURL: first)
        let client = try CcshiftClient(executableURL: launcher)

        let stableLauncher = launcher.deletingLastPathComponent()
            .resolvingSymlinksInPath()
            .appendingPathComponent(launcher.lastPathComponent)
            .standardizedFileURL
        XCTAssertEqual(client.executableURL.path, stableLauncher.path)
        XCTAssertEqual(try client.switchEngineAccount(to: 1).to.number, 1)

        try FileManager.default.removeItem(at: launcher)
        try FileManager.default.createSymbolicLink(at: launcher, withDestinationURL: second)
        XCTAssertEqual(try client.switchEngineAccount(to: 2).to.number, 2)

        try FileManager.default.removeItem(at: second)
        XCTAssertThrowsError(try client.switchEngineAccount(to: 2)) { error in
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
        let pathCandidate = try writeFakeExecutable(at: unsafeBin.appendingPathComponent("ccshift"), permissions: 0o700)
        let defaults = UserDefaults(suiteName: "ccshift-menubar-resolver-\(UUID().uuidString)")!
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
        let protectedExecutable = try writeFakeExecutable(at: protectedChild.appendingPathComponent("ccshift"), permissions: 0o700)
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
        let unsafeTarget = try writeFakeExecutable(at: unsafeTargetDirectory.appendingPathComponent("ccshift-real"), permissions: 0o700)
        let chosenSymlink = safeBin.appendingPathComponent("ccshift")
        try FileManager.default.createSymbolicLink(at: chosenSymlink, withDestinationURL: unsafeTarget)
        XCTAssertNil(CLIResolver.validExecutable(chosenSymlink.path))

        let unsafeFile = try writeFakeExecutable(at: safeBin.appendingPathComponent("ccshift-world-writable"), permissions: 0o777)
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
            at: safeBin.appendingPathComponent("ccshift-read-acl"),
            permissions: 0o700
        )
        try addACL("everyone allow read", to: readOnlyACLExecutable)
        XCTAssertNotNil(CLIResolver.validExecutable(readOnlyACLExecutable.path))

        let writableACLExecutable = try writeFakeExecutable(
            at: safeBin.appendingPathComponent("ccshift-write-acl"),
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
            at: aclParent.appendingPathComponent("ccshift"),
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

        let child = stickyDirectory.appendingPathComponent("ccshift-menubar-sticky-test-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(
            at: child,
            withIntermediateDirectories: false,
            attributes: [.posixPermissions: 0o700]
        )
        addTeardownBlock { try? FileManager.default.removeItem(at: child) }
        let executable = try writeFakeExecutable(at: child.appendingPathComponent("ccshift"), permissions: 0o700)
        XCTAssertNotNil(CLIResolver.validExecutable(executable.path))
    }

    func testDefaultTimeoutCoversAFullUpstreamUsageFetch() throws {
        let client = try CcshiftClient(executableURL: fakeCLI(engineScript))
        XCTAssertGreaterThanOrEqual(client.timeout, 45)
    }

    func testAutoSwitchOnceParsesJSONLinesAndExitCodes() throws {
        let executable = try fakeCLI(autoScript)
        let client = try CcshiftClient(executableURL: executable)
        let outcome = try client.autoSwitchOnce(threshold: 84, dryRun: true, cancellation: ProcessCancellation())
        XCTAssertEqual(outcome.eventKind, "switch")
        XCTAssertTrue(outcome.dryRun)
        XCTAssertEqual(outcome.summary, "Would switch to two@example.test.")
    }

    func testEachWindowGetsItsOwnSwitchPointWhenTheyDiffer() throws {
        let log = FileManager.default.temporaryDirectory
            .appendingPathComponent("ccshift-args-\(UUID().uuidString)")
        addTeardownBlock { try? FileManager.default.removeItem(at: log) }
        let executable = try fakeCLI("""
        #!/bin/sh
        printf '%s\\n' "$*" >> '\(log.path)'
        printf '%s\\n' '{"schemaVersion":1,"event":"no-switch","reason":"below-threshold","detail":"5h 70% < 80%"}'
        exit 2
        """)
        let client = try CcshiftClient(executableURL: executable)

        let split = try client.autoSwitchOnce(
            threshold5h: 80, threshold7d: 95, dryRun: false, cancellation: ProcessCancellation()
        )
        XCTAssertEqual(split.thresholdText, "5h 80.0% · 7d 95.0%")
        let same = try client.autoSwitchOnce(
            threshold5h: 90, threshold7d: 90, dryRun: false, cancellation: ProcessCancellation()
        )
        XCTAssertEqual(same.thresholdText, "90.0%")

        let lines = try String(contentsOf: log, encoding: .utf8).split(separator: "\n").map(String.init)
        XCTAssertEqual(lines[0], "auto --once --json --threshold-5h 80.0 --threshold-7d 95.0")
        XCTAssertEqual(lines[1], "auto --once --json --threshold 90.0", "equal values keep the form every ccshift understands")
        XCTAssertThrowsError(try client.autoSwitchOnce(
            threshold5h: 49, threshold7d: 95, dryRun: false, cancellation: ProcessCancellation()
        ))
        XCTAssertThrowsError(try client.autoSwitchOnce(
            threshold5h: 80, threshold7d: 100, dryRun: false, cancellation: ProcessCancellation()
        ))
    }

    func testResetLabelIsRecomputedFromResetsAtLikeCcshift() throws {
        let json = #"{"pct":40,"resetsAt":"2026-09-29T23:29:59.123456+00:00","countdown":"21h 51m","clock":"Sep 30 06:29"}"#
        let window = try JSONDecoder().decode(UsageWindow.self, from: Data(json.utf8))
        let now = try XCTUnwrap(ResetTime.parse("2026-09-29T01:39:00+00:00"))
        XCTAssertEqual(window.resetLabel(now: now), "in 21h 50m")
        XCTAssertEqual(window.resetTooltip, "Resets Sep 30 06:29")
        let afterReset = try XCTUnwrap(ResetTime.parse("2026-09-30T00:00:00+00:00"))
        XCTAssertEqual(window.resetLabel(now: afterReset), "in 0m")
        let daysBefore = try XCTUnwrap(ResetTime.parse("2026-09-24T20:00:00+00:00"))
        XCTAssertEqual(window.resetLabel(now: daysBefore), "in 5d 3h")

        let usage = try JSONDecoder().decode(AccountUsage.self, from: Data(#"""
        {"fiveHour":{"pct":4},"scoped":[{"name":"Opus","pct":9,"resetsAt":"2026-10-04T04:57:00+00:00","countdown":"5d 2h","clock":"Oct 4 11:57"}]}
        """#.utf8))
        XCTAssertEqual(usage.scoped?.first?.window.resetTooltip, "Resets Oct 4 11:57")
        XCTAssertEqual(usage.scoped?.first?.window.resetLabel(now: daysBefore), "in 9d 8h")
    }

    func testResetLabelFallsBackToCountdownThenRawText() throws {
        let countdownOnly = try JSONDecoder().decode(
            UsageWindow.self, from: Data(#"{"pct":1,"countdown":"47m","clock":"09:45"}"#.utf8)
        )
        XCTAssertEqual(countdownOnly.resetLabel(), "in 47m")
        XCTAssertEqual(UsageWindow(pct: 1, resetsAt: "tomorrow").resetLabel(), "tomorrow")
        XCTAssertNil(UsageWindow(pct: 1, resetsAt: nil).resetLabel())
    }

    func testAutoSwitchNoSwitchEventWithExitTwoIsAResult() throws {
        let executable = try fakeCLI(#"""
        #!/bin/sh
        printf '%s\n' '{"schemaVersion":1,"event":"no-switch","ts":"2026-09-29T00:00:00Z","reason":"active-api-key","detail":"API-key accounts have no quota to watch"}'
        exit 2
        """#)
        let client = try CcshiftClient(executableURL: executable)
        let outcome = try client.autoSwitchOnce(threshold: 90, dryRun: false, cancellation: ProcessCancellation())
        XCTAssertEqual(outcome.eventKind, "no-switch")
        XCTAssertEqual(outcome.summary, "active-api-key: API-key accounts have no quota to watch.")
    }

    func testAutoSwitchUsageErrorWithoutEventsIsAFailure() throws {
        let executable = try fakeCLI(#"""
        #!/bin/sh
        printf '%s\n' 'ccshift: error: unrecognized arguments: accounts' >&2
        exit 2
        """#)
        let client = try CcshiftClient(executableURL: executable)
        XCTAssertThrowsError(
            try client.autoSwitchOnce(threshold: 90, dryRun: false, cancellation: ProcessCancellation())
        ) { error in
            guard case CLIError.failed = error else {
                return XCTFail("expected CLIError.failed, got \(error)")
            }
        }
    }

    func testAccountCommandsSendExactArgumentsAndDecodeTheirReports() throws {
        let folder = try tempFolder()
        let argsPath = folder.appendingPathComponent("args")
        let script = #"""
        #!/bin/sh
        printf '%s\n' "$*" >> '__ARGS__'
        case "$1" in
          status) printf '%s\n' '{"schemaVersion":1,"active":{"email":"new@example.test","managed":false}}' ;;
          add) printf '%s\n' '{"schemaVersion":1,"action":"added","account":{"number":3,"email":"new@example.test","alias":"dev"}}' ;;
          remove) printf '%s\n' '{"schemaVersion":1,"action":"removed","account":{"number":2,"email":"b@example.test"},"wasActive":true}' ;;
        esac
        """#
        let client = try CcshiftClient(executableURL: fakeCLI(script, replacements: ["__ARGS__": argsPath.path]))

        let login = try XCTUnwrap(client.currentLogin())
        XCTAssertEqual(login.email, "new@example.test")
        XCTAssertFalse(login.managed)

        let added = try client.addCurrentAccount(alias: "dev")
        XCTAssertEqual(added.action, "added")
        XCTAssertEqual(added.account?.number, 3)
        XCTAssertEqual(added.account?.displayName, "dev")

        let removed = try client.removeAccount(account(2, "b@example.test"), emailIsShared: false)
        XCTAssertEqual(removed.action, "removed")
        XCTAssertEqual(removed.wasActive, true)

        _ = try client.addCurrentAccount(alias: "-dash")
        let calls = try String(contentsOf: argsPath, encoding: .utf8).split(separator: "\n").map(String.init)
        // Removal names the account by email, so a stale list cannot hit
        // another account that now sits in the same slot.
        XCTAssertEqual(calls, [
            "status --json", "add --json --alias=dev", "remove b@example.test --yes --json", "add --json --alias=-dash",
        ])
    }

    func testSharedEmailIsRemovedByNumberOnlyAfterTheSlotIsRechecked() throws {
        let folder = try tempFolder()
        let argsPath = folder.appendingPathComponent("args")
        let script = #"""
        #!/bin/sh
        printf '%s\n' "$*" >> '__ARGS__'
        case "$1" in
          list) printf '%s\n' '{"schemaVersion":1,"activeAccountNumber":1,"accounts":[{"number":1,"email":"same@example.test","organizationName":"A","active":true,"usageStatus":"ok"},{"number":2,"email":"same@example.test","organizationName":"B","active":false,"usageStatus":"ok"}]}' ;;
          remove) printf '%s\n' '{"schemaVersion":1,"action":"removed","account":{"number":2,"email":"same@example.test"},"wasActive":false}' ;;
        esac
        """#
        let client = try CcshiftClient(executableURL: fakeCLI(script, replacements: ["__ARGS__": argsPath.path]))

        _ = try client.removeAccount(account(2, "same@example.test", organization: "B"), emailIsShared: true)
        // Slot 2 now holds a different organization's account: nothing is removed.
        XCTAssertThrowsError(
            try client.removeAccount(account(2, "same@example.test", organization: "A"), emailIsShared: true)
        ) { error in
            XCTAssertEqual(error as? CLIError, .accountListChanged)
        }
        let calls = try String(contentsOf: argsPath, encoding: .utf8).split(separator: "\n").map(String.init)
        XCTAssertEqual(calls, ["list --json", "remove 2 --yes --json", "list --json"])
    }

    func testOtherUsageErrorsAreNotMistakenForAnOldCcshift() throws {
        let client = try CcshiftClient(executableURL: fakeCLI("""
        #!/bin/sh
        echo "ccshift: error: argument --alias: expected one argument" >&2
        exit 2
        """))
        XCTAssertThrowsError(try client.addCurrentAccount(alias: "dev")) { error in
            XCTAssertEqual(error as? CLIError, .failed("ccshift: error: argument --alias: expected one argument"))
        }
    }

    private func account(_ number: Int, _ email: String, organization: String? = nil) -> Account {
        Account(
            id: "account:\(number)", number: number, email: email, organizationName: organization,
            alias: nil, active: false, disabled: false, usageStatus: "ok",
            usage: nil, usageAgeSeconds: nil, lastGoodUsage: nil, lastGoodAgeSeconds: nil
        )
    }

    func testEnableAndDisableNameTheAccountByEmail() throws {
        let folder = try tempFolder()
        let argsPath = folder.appendingPathComponent("args")
        let script = #"""
        #!/bin/sh
        printf '%s\n' "$*" >> '__ARGS__'
        case "$1" in
          disable) printf '%s\n' '{"schemaVersion":1,"action":"disabled","account":{"number":2,"email":"b@example.test","alias":"work"},"changed":true,"rotationEmpty":true}' ;;
          enable) printf '%s\n' '{"schemaVersion":1,"action":"enabled","account":{"number":2,"email":"b@example.test"},"changed":false,"rotationEmpty":false}' ;;
        esac
        """#
        let client = try CcshiftClient(executableURL: fakeCLI(script, replacements: ["__ARGS__": argsPath.path]))

        let disabled = try client.setAccountDisabled(account(2, "b@example.test"), disabled: true, emailIsShared: false)
        XCTAssertEqual(disabled.action, "disabled")
        XCTAssertEqual(disabled.changed, true)
        XCTAssertEqual(disabled.rotationEmpty, true)
        let enabled = try client.setAccountDisabled(account(2, "b@example.test"), disabled: false, emailIsShared: false)
        XCTAssertEqual(enabled.changed, false)
        let calls = try String(contentsOf: argsPath, encoding: .utf8).split(separator: "\n").map(String.init)
        XCTAssertEqual(calls, ["disable b@example.test --json", "enable b@example.test --json"])
    }

    func testOlderCcshiftWithoutEnableDisableJSONAsksForAnUpgrade() throws {
        let client = try CcshiftClient(executableURL: fakeCLI("""
        #!/bin/sh
        echo "ccshift: error: --json can only be used with 'list', 'status', 'switch', 'add' or 'remove'" >&2
        exit 2
        """))
        XCTAssertThrowsError(try client.setAccountDisabled(account(2, "b@example.test"), disabled: true, emailIsShared: false)) { error in
            XCTAssertEqual(error as? CLIError, .accountCommandsUnsupported)
        }
    }

    func testBrowserSignInPassesItsOptionsAsSingleTokens() throws {
        let folder = try tempFolder()
        let argsPath = folder.appendingPathComponent("args")
        let script = #"""
        #!/bin/sh
        printf '%s\n' "$*" >> '__ARGS__'
        printf '%s\n' '{"schemaVersion":1,"action":"refreshed","account":{"number":1,"email":"a@example.test"}}'
        """#
        let client = try CcshiftClient(executableURL: fakeCLI(script, replacements: ["__ARGS__": argsPath.path]))

        let report = try client.signInAndAddAccount(
            email: "a@example.test", sso: true, alias: "work", cancellation: ProcessCancellation()
        )
        XCTAssertEqual(report.action, "refreshed")
        _ = try client.signInAndAddAccount(email: nil, sso: false, alias: nil, cancellation: ProcessCancellation())
        _ = try client.signInAndAddAccount(
            email: nil, sso: false, alias: nil, handoffFile: "/tmp/ccshift-signin/url", cancellation: ProcessCancellation()
        )
        _ = try client.signInAndAddAccount(
            email: nil, sso: false, alias: nil, privateBrowser: "com.google.Chrome", cancellation: ProcessCancellation()
        )
        let calls = try String(contentsOf: argsPath, encoding: .utf8).split(separator: "\n").map(String.init)
        XCTAssertEqual(calls, [
            "add --login --json --sso --email=a@example.test --alias=work",
            "add --login --json",
            "add --login --json --handoff-file=/tmp/ccshift-signin/url",
            "add --login --json --private --browser=com.google.Chrome",
        ])
        XCTAssertGreaterThanOrEqual(CcshiftClient.signInTimeout, 15 * 60)
    }

    func testOnlyClaudeSignInPagesAreOpenedFromAHandoff() {
        XCTAssertTrue(SignInURLPolicy.isAllowed(URL(string: "https://claude.com/cai/oauth/authorize?x=1")!))
        XCTAssertTrue(SignInURLPolicy.isAllowed(URL(string: "https://platform.claude.com/oauth/authorize")!))
        XCTAssertFalse(SignInURLPolicy.isAllowed(URL(string: "http://claude.com/cai/oauth/authorize")!))
        XCTAssertFalse(SignInURLPolicy.isAllowed(URL(string: "https://claude.com.evil.test/")!))
        XCTAssertFalse(SignInURLPolicy.isAllowed(URL(string: "file:///etc/passwd")!))
    }

    func testSignInOpenerIsRememberedAsAString() {
        for opener in [SignInOpener.privateWindow, .defaultBrowser, .privateBrowser("com.google.Chrome")] {
            XCTAssertEqual(SignInOpener(storageValue: opener.storageValue), opener)
        }
        XCTAssertEqual(SignInOpener(storageValue: ""), .privateWindow)
    }

    func testSignedOutClaudeCodeIsReportedAsNoLogin() throws {
        let client = try CcshiftClient(executableURL: fakeCLI("""
        #!/bin/sh
        printf '%s\n' '{"schemaVersion":1,"active":null}'
        """))
        XCTAssertNil(try client.currentLogin())
    }

    func testOlderCcshiftWithoutAccountJSONAsksForAnUpgrade() throws {
        let client = try CcshiftClient(executableURL: fakeCLI("""
        #!/bin/sh
        echo "ccshift: error: --json can only be used with 'list', 'status', or 'switch'" >&2
        exit 2
        """))
        XCTAssertThrowsError(try client.removeAccount(account(2, "b@example.test"), emailIsShared: false)) { error in
            XCTAssertEqual(error as? CLIError, .accountCommandsUnsupported)
        }
        XCTAssertThrowsError(try client.addCurrentAccount(alias: nil)) { error in
            XCTAssertEqual(error as? CLIError, .accountCommandsUnsupported)
        }
    }

    func testAccountCommandErrorEnvelopeSurfacesTheReason() throws {
        let client = try CcshiftClient(executableURL: fakeCLI("""
        #!/bin/sh
        printf '%s\n' '{"schemaVersion":1,"error":{"type":"ConfigError","message":"No active Claude account found. Please log in first."}}'
        exit 1
        """))
        XCTAssertThrowsError(try client.addCurrentAccount(alias: nil)) { error in
            XCTAssertEqual(error.localizedDescription, "No active Claude account found. Please log in first.")
        }
        XCTAssertThrowsError(try client.removeAccount(account(0, "a@example.test"), emailIsShared: false)) { error in
            XCTAssertEqual(error as? CLIError, .invalidAccountNumber)
        }
    }

    private func fakeCLI(_ script: String, replacements: [String: String] = [:]) throws -> URL {
        let folder = try tempFolder()
        let executable = folder.appendingPathComponent("ccshift-fake")
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
            .appendingPathComponent("ccshift-menubar-tests-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(
            at: folder,
            withIntermediateDirectories: false,
            attributes: [.posixPermissions: 0o700]
        )
        addTeardownBlock { try? FileManager.default.removeItem(at: folder) }
        return folder
    }

    private func switchReply(_ number: Int) -> String {
        """
        #!/bin/sh
        printf '{"schemaVersion":1,"switched":true,"from":null,"to":{"number":%s,"email":"x@example.test"}}\n' '\(number)'
        """
    }

    private var autoScript: String {
        #"""
        #!/bin/sh
        printf '%s\n' '{"schemaVersion":1,"event":"poll","ts":"2026-09-29T00:00:00Z","active":{"number":1,"email":"one@example.test"},"headroomPct":{"1":5.0,"2":90.0},"threshold":84.0}'
        printf '%s\n' '{"schemaVersion":1,"event":"switch","ts":"2026-09-29T00:00:00Z","trigger":"proactive","from":{"number":1,"email":"one@example.test"},"to":{"number":2,"email":"two@example.test"},"warnings":[],"dryRun":true}'
        exit 0
        """#
    }

    private var engineScript: String {
        #"""
        #!/bin/sh
        if [ "$1" = "list" ]; then
          cat <<'JSON'
        {"schemaVersion":1,"activeAccountNumber":1,"accounts":[{"number":1,"email":"main@example.test","organizationName":"Max","organizationUuid":"","isOrganization":false,"alias":"main","active":true,"usageStatus":"ok","usage":{"fiveHour":{"pct":17,"resetsAt":"2026-06-22T23:29:59Z"},"sevenDay":{"pct":32,"resetsAt":"2026-06-26T17:59:59Z"}},"usageAgeSeconds":5},{"number":2,"email":"team@example.test","organizationName":"Team","alias":"work","active":false,"usageStatus":"unavailable","usage":null,"lastGoodAgeSeconds":300,"lastGoodUsage":{"sevenDay":{"pct":61,"resetsAt":"2026-06-26T17:59:59Z"}}}]}
        JSON
        elif [ "$1" = "switch" ]; then
          if [ "$2" = "1" ]; then
            printf '%s\n' '{"schemaVersion":1,"switched":false,"from":{"number":1,"email":"main@example.test"},"to":{"number":1,"email":"main@example.test"},"strategy":"direct","reason":"already-active","message":"Already on Account-1 (main@example.test)","warnings":[]}'
          else
            printf '{"schemaVersion":1,"switched":true,"from":null,"to":{"number":%s,"email":"target@example.test"},"strategy":"direct","reason":"switched","warnings":[]}\n' "$2"
          fi
        else
          exit 64
        fi
        """#
    }
}
