import Foundation
import XCTest
@testable import CcsMenuBar

final class UpdateCheckerTests: XCTestCase {
    func testVersionsCompareNumericallyIgnoringTheTagPrefix() {
        XCTAssertTrue(AppVersion.isNewer("v1.10.0", than: "1.9.9"))
        XCTAssertTrue(AppVersion.isNewer("1.1", than: "1.0.9"))
        XCTAssertFalse(AppVersion.isNewer("v1.1.0", than: "1.1.0"))
        XCTAssertFalse(AppVersion.isNewer("1.0.2", than: "1.1.0"))
        XCTAssertEqual(AppVersion.compare("1.2", "1.2.0"), .orderedSame)
        XCTAssertEqual(AppVersion.components("v2.0.1-beta.1"), [2, 0, 1])
    }

    func testCLIVersionIsReadFromVersionOutput() {
        XCTAssertEqual(AppVersion.parseCLIVersion("ccshift 1.1.0\n"), "1.1.0")
        XCTAssertNil(AppVersion.parseCLIVersion("usage: ccshift"))
    }

    func testTheLatestReleaseBecomesAnUpdateOnlyWhenNewer() throws {
        let release = try JSONDecoder().decode(ReleaseInfo.self, from: Data(Self.releaseJSON.utf8))
        let update = try XCTUnwrap(release.update(over: "1.1.0"))
        XCTAssertEqual(update.version, "1.2.0")
        XCTAssertEqual(update.releaseURL.absoluteString, "https://github.com/nam-sequence/ccshift/releases/tag/v1.2.0")
        XCTAssertEqual(
            update.downloadURL?.absoluteString,
            "https://github.com/nam-sequence/ccshift/releases/download/v1.2.0/CcsMenuBar-1.2.0-macos.zip"
        )
        XCTAssertNil(release.update(over: "1.2.0"))
        XCTAssertNil(release.update(over: "1.3.0"))
    }

    func testDraftsAndPrereleasesAreNotOffered() throws {
        let prerelease = Self.releaseJSON.replacingOccurrences(of: "\"prerelease\": false", with: "\"prerelease\": true")
        let release = try JSONDecoder().decode(ReleaseInfo.self, from: Data(prerelease.utf8))
        XCTAssertNil(release.update(over: "1.0.0"))
    }

    static let releaseJSON = """
    {
      "tag_name": "v1.2.0",
      "html_url": "https://github.com/nam-sequence/ccshift/releases/tag/v1.2.0",
      "draft": false,
      "prerelease": false,
      "assets": [
        {"name": "ccshift-1.2.0-py3-none-any.whl", "browser_download_url": "https://github.com/nam-sequence/ccshift/releases/download/v1.2.0/ccshift-1.2.0-py3-none-any.whl"},
        {"name": "CcsMenuBar-1.2.0-macos.zip", "browser_download_url": "https://github.com/nam-sequence/ccshift/releases/download/v1.2.0/CcsMenuBar-1.2.0-macos.zip"}
      ]
    }
    """
}

@MainActor
final class UpdateReminderTests: XCTestCase {
    func testAnAutomaticCheckRemindsOncePerVersionAndCanBeSkipped() async throws {
        let notifier = FakeUpdateNotifier()
        let model = makeModel(release: .success(try Self.release()), notifier: notifier)

        await model.checkForUpdates(userInitiated: false)
        XCTAssertEqual(model.visibleUpdate?.version, "1.2.0")
        XCTAssertNotNil(model.lastUpdateCheck)
        XCTAssertEqual(notifier.notified.map(\.version), ["1.2.0"])

        await model.checkForUpdates(userInitiated: false)
        XCTAssertEqual(notifier.notified.count, 1, "one notification per version")

        model.skipAvailableUpdate()
        XCTAssertNil(model.visibleUpdate)
        XCTAssertEqual(model.availableUpdate?.version, "1.2.0", "Settings still shows it")
    }

    func testAManualCheckDoesNotPostANotification() async throws {
        let notifier = FakeUpdateNotifier()
        let model = makeModel(release: .success(try Self.release()), notifier: notifier)
        await model.checkForUpdates(userInitiated: true)
        XCTAssertEqual(model.visibleUpdate?.version, "1.2.0")
        XCTAssertTrue(notifier.notified.isEmpty)
    }

    func testUpToDateAndFailedChecks() async throws {
        let upToDate = makeModel(release: .success(try Self.release()), appVersion: "1.2.0")
        await upToDate.checkForUpdates(userInitiated: true)
        XCTAssertNil(upToDate.availableUpdate)
        XCTAssertNil(upToDate.updateCheckError)

        let failed = makeModel(release: .failure(UpdateCheckError.http(403)))
        await failed.checkForUpdates(userInitiated: true)
        XCTAssertNil(failed.availableUpdate)
        XCTAssertEqual(failed.updateCheckError, UpdateCheckError.http(403).localizedDescription)
        XCTAssertNil(failed.lastUpdateCheck)
    }

    func testAnOlderCommandLineToolIsPointedOut() async throws {
        let folder = FileManager.default.temporaryDirectory
            .appendingPathComponent("ccs-update-tests-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
        addTeardownBlock { try? FileManager.default.removeItem(at: folder) }
        let executable = folder.appendingPathComponent("ccshift")
        try Data("#!/bin/sh\necho 'ccshift 1.0.0'\n".utf8).write(to: executable)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: executable.path)

        let model = makeModel(release: .success(try Self.release()), executableURL: executable)
        model.refreshCLIVersion()
        let read = await waitUntil { model.cliVersion == "1.0.0" }
        XCTAssertTrue(read)
        XCTAssertEqual(model.cliUpdateHint, "The command line tool (1.0.0) is older than this app. Run ccshift upgrade in Terminal.")
        await model.checkForUpdates(userInitiated: true)
        XCTAssertEqual(model.cliUpdateHint, "Update the command line tool too: run ccshift upgrade in Terminal.")
    }

    // MARK: Helpers

    private static func release() throws -> ReleaseInfo {
        try JSONDecoder().decode(ReleaseInfo.self, from: Data(UpdateCheckerTests.releaseJSON.utf8))
    }

    private func makeModel(
        release: Result<ReleaseInfo, Error>,
        notifier: FakeUpdateNotifier? = nil,
        appVersion: String = "1.1.0",
        executableURL: URL? = nil
    ) -> MenuBarModel {
        let suite = "ccs-update-tests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defaults.removePersistentDomain(forName: suite)
        return MenuBarModel(
            defaults: defaults,
            executableURL: executableURL,
            launchAtLoginManager: PreviewLaunchAtLoginManager(status: .notRegistered),
            releaseFetcher: FakeReleaseFetcher(result: release),
            updateNotifier: notifier,
            appVersion: appVersion
        )
    }

    private func waitUntil(timeout: TimeInterval = 3, condition: @MainActor () -> Bool) async -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if condition() { return true }
            try? await Task.sleep(for: .milliseconds(20))
        }
        return condition()
    }
}

private struct FakeReleaseFetcher: ReleaseFetching, @unchecked Sendable {
    let result: Result<ReleaseInfo, Error>

    func latestRelease() async throws -> ReleaseInfo {
        try result.get()
    }
}

@MainActor
private final class FakeUpdateNotifier: UpdateNotifying {
    var notified: [AvailableUpdate] = []

    func notify(_ update: AvailableUpdate) {
        notified.append(update)
    }
}
