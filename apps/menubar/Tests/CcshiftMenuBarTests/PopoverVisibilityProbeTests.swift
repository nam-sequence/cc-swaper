import AppKit
import XCTest
@testable import CcshiftMenuBar

@MainActor
final class PopoverVisibilityProbeTests: XCTestCase {
    func testReportsTheHostingWindowBeingShownAndHidden() async throws {
        let probe = PopoverVisibilityProbe.ProbeView()
        var reports: [Bool] = []
        probe.onChange = { reports.append($0) }

        let window = NSWindow(
            contentRect: NSRect(x: 100, y: 100, width: 80, height: 60),
            styleMask: [.borderless],
            backing: .buffered,
            defer: false
        )
        window.contentView = probe
        XCTAssertEqual(reports.last, false, "a window that is not on screen is not visible")

        window.orderFrontRegardless()
        let shown = await waitFor { reports.last == true }
        try XCTSkipUnless(shown, "no window server session to show a window on")

        window.orderOut(nil)
        let hidden = await waitFor { reports.last == false }
        XCTAssertTrue(hidden, "ordering the window out is reported")

        window.orderFrontRegardless()
        let shownAgain = await waitFor { reports.last == true }
        XCTAssertTrue(shownAgain, "and showing it again is reported again")

        window.close()
        let closed = await waitFor { reports.last == false }
        XCTAssertTrue(closed)
    }

    private func waitFor(timeout: TimeInterval = 2, _ condition: @MainActor () -> Bool) async -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if condition() { return true }
            try? await Task.sleep(for: .milliseconds(20))
        }
        return condition()
    }
}
