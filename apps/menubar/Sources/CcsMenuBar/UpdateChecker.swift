import Foundation

/// A newer ccshift release than the running app.
struct AvailableUpdate: Equatable, Sendable {
    let version: String
    let releaseURL: URL
    /// The menu bar app's zip, when the release carries one.
    let downloadURL: URL?
}

/// ccshift's versions are MAJOR.MINOR.PATCH; a leading "v" (a git tag) is ignored.
enum AppVersion {
    static var current: String {
        Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "0.0.0"
    }

    static func compare(_ lhs: String, _ rhs: String) -> ComparisonResult {
        let left = components(lhs)
        let right = components(rhs)
        for index in 0..<max(left.count, right.count) {
            let a = index < left.count ? left[index] : 0
            let b = index < right.count ? right[index] : 0
            if a != b { return a < b ? .orderedAscending : .orderedDescending }
        }
        return .orderedSame
    }

    static func isNewer(_ candidate: String, than installed: String) -> Bool {
        compare(candidate, installed) == .orderedDescending
    }

    /// "v1.2.3" → [1, 2, 3]; anything after a "-" or "+" (pre-release, build) is dropped.
    static func components(_ version: String) -> [Int] {
        var text = version.trimmingCharacters(in: .whitespaces)
        if text.hasPrefix("v") || text.hasPrefix("V") { text.removeFirst() }
        if let cut = text.firstIndex(where: { $0 == "-" || $0 == "+" }) { text = String(text[..<cut]) }
        return text.split(separator: ".").map { Int($0) ?? 0 }
    }

    /// The version out of `ccshift --version` ("ccshift 1.1.0").
    static func parseCLIVersion(_ output: String) -> String? {
        output.split(whereSeparator: \.isWhitespace)
            .map(String.init)
            .last { $0.first?.isNumber == true && $0.contains(".") }
    }
}

/// The latest published release, from the GitHub releases API.
struct ReleaseInfo: Decodable, Equatable, Sendable {
    struct Asset: Decodable, Equatable, Sendable {
        let name: String
        let browserDownloadURL: URL

        enum CodingKeys: String, CodingKey {
            case name
            case browserDownloadURL = "browser_download_url"
        }
    }

    let tagName: String
    let htmlURL: URL
    let draft: Bool
    let prerelease: Bool
    let assets: [Asset]

    enum CodingKeys: String, CodingKey {
        case tagName = "tag_name"
        case htmlURL = "html_url"
        case draft
        case prerelease
        case assets
    }

    /// An update when this release is newer than `installed`.
    func update(over installed: String) -> AvailableUpdate? {
        guard !draft, !prerelease, AppVersion.isNewer(tagName, than: installed) else { return nil }
        let zip = assets.first { $0.name.hasPrefix("CcsMenuBar-") && $0.name.hasSuffix("-macos.zip") }
        return AvailableUpdate(
            version: AppVersion.components(tagName).map(String.init).joined(separator: "."),
            releaseURL: htmlURL,
            downloadURL: zip?.browserDownloadURL
        )
    }
}

protocol ReleaseFetching: Sendable {
    func latestRelease() async throws -> ReleaseInfo
}

/// `GET /repos/nam-sequence/ccshift/releases/latest`, unauthenticated. The API
/// already leaves out drafts and pre-releases.
struct GitHubReleaseFetcher: ReleaseFetching {
    static let latestReleaseURL = URL(string: "https://api.github.com/repos/nam-sequence/ccshift/releases/latest")!

    func latestRelease() async throws -> ReleaseInfo {
        var request = URLRequest(url: Self.latestReleaseURL, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 20)
        request.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")
        request.setValue("ccshift-menubar/\(AppVersion.current)", forHTTPHeaderField: "User-Agent")
        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse else { throw UpdateCheckError.unreadable }
        guard http.statusCode == 200 else { throw UpdateCheckError.http(http.statusCode) }
        do {
            return try JSONDecoder().decode(ReleaseInfo.self, from: data)
        } catch {
            throw UpdateCheckError.unreadable
        }
    }
}

enum UpdateCheckError: LocalizedError, Equatable {
    case http(Int)
    case unreadable

    var errorDescription: String? {
        switch self {
        case .http(403), .http(429): "GitHub is limiting requests right now. ccshift tries again later."
        case let .http(code): "Could not check for updates (GitHub answered \(code))."
        case .unreadable: "Could not read the latest release from GitHub."
        }
    }
}

/// Posts the one-time "update available" notification.
@MainActor
protocol UpdateNotifying: AnyObject {
    func notify(_ update: AvailableUpdate)
}
