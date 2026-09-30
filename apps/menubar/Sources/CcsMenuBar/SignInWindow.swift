import AppKit
import AuthenticationServices

/// Where the Claude sign-in page opens for "Sign In with Browser".
enum SignInOpener: Hashable, Sendable {
    /// ccshift's own private sign-in window (an ephemeral
    /// `ASWebAuthenticationSession`): no cookies shared with any browser, and
    /// it works whatever the default browser is, Safari and Arc included.
    case privateWindow
    /// A private window of an installed browser that supports one.
    case privateBrowser(String)
    /// A normal window of the default browser.
    case defaultBrowser

    var storageValue: String {
        switch self {
        case .privateWindow: "window"
        case let .privateBrowser(bundleID): bundleID
        case .defaultBrowser: "default"
        }
    }

    init(storageValue: String) {
        switch storageValue {
        case "", "window": self = .privateWindow
        case "default": self = .defaultBrowser
        default: self = .privateBrowser(storageValue)
        }
    }
}

/// Shows the sign-in page in a private window. A protocol so tests can stand in
/// for the system window.
@MainActor
protocol SignInWindowPresenting: AnyObject {
    /// `onClose` runs when the person closes the window themselves.
    func present(_ url: URL, onClose: @escaping @MainActor () -> Void)
    /// Closes the window without calling `onClose`.
    func dismiss()
}

@MainActor
final class SystemSignInWindowPresenter: NSObject, SignInWindowPresenting,
    ASWebAuthenticationPresentationContextProviding
{
    private var session: ASWebAuthenticationSession?

    func present(_ url: URL, onClose: @escaping @MainActor () -> Void) {
        dismiss()
        // No callback scheme: Claude Code's own localhost listener receives the
        // redirect, and the window is closed when `ccshift add --login` ends.
        let session = ASWebAuthenticationSession(
            url: url,
            callbackURLScheme: nil,
            completionHandler: Self.completionHandler { [weak self] in
                guard let self, self.session != nil else { return }  // dismissed by ccshift
                self.session = nil
                onClose()
            }
        )
        session.prefersEphemeralWebBrowserSession = true
        session.presentationContextProvider = self
        self.session = session
        NSApp.activate(ignoringOtherApps: true)
        if !session.start() {
            self.session = nil
            onClose()
        }
    }

    func dismiss() {
        guard let session else { return }
        self.session = nil
        session.cancel()
    }

    /// AuthenticationServices calls the completion handler on its own XPC
    /// queue, also when `dismiss()` closes the window after ccshift has added
    /// the account. A closure written inside this main-actor class would be
    /// main-actor isolated, and Swift stops the app when one runs off the main
    /// thread; this one is nonisolated and only hops to the main actor.
    nonisolated static func completionHandler(
        _ ended: @escaping @MainActor @Sendable () -> Void
    ) -> ASWebAuthenticationSession.CompletionHandler {
        { _, _ in
            Task { @MainActor in ended() }
        }
    }

    func presentationAnchor(for session: ASWebAuthenticationSession) -> ASPresentationAnchor {
        NSApp.keyWindow
            ?? NSApp.windows.first { $0.isVisible && $0.canBecomeKey }
            ?? ASPresentationAnchor()
    }
}

enum SignInURLPolicy {
    /// Only Claude's own sign-in pages are opened from a handed-off URL.
    static let allowedHosts: Set<String> = ["claude.com", "claude.ai", "platform.claude.com", "console.anthropic.com"]

    static func isAllowed(_ url: URL) -> Bool {
        guard url.scheme?.lowercased() == "https", let host = url.host?.lowercased() else { return false }
        return allowedHosts.contains(host)
    }
}
