import AppKit

/// Installed browsers that can open a private window when another app asks
/// (`ccshift add --login --private --browser=<id>`). Mirrors the list in
/// ccshift's `private_browser` module. Safari and Arc cannot: neither takes a
/// command-line private flag, and Arc's AppleScript ignores the incognito mode
/// of a new window. For those, ccshift's own private sign-in window is used.
struct PrivateBrowsers: Equatable {
    struct Browser: Identifiable, Equatable {
        let bundleID: String
        let name: String
        let url: URL
        var id: String { bundleID }
    }

    static let supportedBundleIDs = [
        "com.google.Chrome", "com.brave.Browser", "com.microsoft.edgemac", "org.mozilla.firefox",
        "com.vivaldi.Vivaldi", "com.operasoftware.Opera", "org.chromium.Chromium", "app.zen-browser.zen",
        "com.google.Chrome.beta", "com.google.Chrome.dev", "com.google.Chrome.canary",
        "com.brave.Browser.beta", "com.brave.Browser.nightly",
        "com.microsoft.edgemac.Beta", "com.microsoft.edgemac.Dev", "com.microsoft.edgemac.Canary",
        "org.mozilla.firefoxdeveloperedition", "org.mozilla.nightly",
    ]

    let installed: [Browser]

    static func detect(workspace: NSWorkspace = .shared) -> PrivateBrowsers {
        PrivateBrowsers(installed: supportedBundleIDs.compactMap { bundleID -> Browser? in
            guard let url = workspace.urlForApplication(withBundleIdentifier: bundleID) else { return nil }
            let name = FileManager.default.displayName(atPath: url.path)
                .replacingOccurrences(of: ".app", with: "")
            return Browser(bundleID: bundleID, name: name, url: url)
        })
    }

    func icon(for browser: Browser) -> NSImage {
        let image = NSWorkspace.shared.icon(forFile: browser.url.path)
        image.size = NSSize(width: 16, height: 16)
        return image
    }
}
