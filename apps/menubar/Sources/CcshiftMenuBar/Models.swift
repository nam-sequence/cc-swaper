import Foundation

struct Account: Identifiable, Equatable, Sendable {
    let id: String
    let number: Int?
    let email: String
    let organizationName: String?
    let alias: String?
    var active: Bool
    let disabled: Bool
    let usageStatus: String
    var usage: AccountUsage?
    let usageAgeSeconds: Double?
    let lastGoodUsage: AccountUsage?
    let lastGoodAgeSeconds: Double?

    var displayName: String {
        if let alias, !alias.isEmpty { return alias }
        if !email.isEmpty { return email }
        return number.map(String.init) ?? "Account"
    }

    var isStale: Bool {
        usageStatus != "ok" && lastGoodUsage != nil
    }

    var visibleUsage: AccountUsage? { usage ?? lastGoodUsage }

    var usageMessage: String? {
        switch usageStatus {
        case "ok": nil
        case "token_expired": "Token expired; ccshift refreshes it automatically."
        case "api_key": "Usage is unavailable for an API key account."
        case "keychain_unavailable": "macOS Keychain is unavailable."
        case "relogin_required": "Login expired. Log in with Claude Code, then run ccshift add."
        case "foreign_credential": "The live login belongs to another account; switching repairs it."
        case "no_credentials": "This account is not signed in."
        case "unavailable": "Usage is temporarily unavailable."
        default: usageStatus
        }
    }
}

struct DashboardSnapshot: Sendable {
    let accounts: [Account]
    let activeAccountNumber: Int?
    let warning: String?
}

struct AutoSwitchOutcome: Equatable, Sendable {
    let eventKind: String?
    let summary: String
    /// Where the check switches, per window: the 5-hour and the 7-day limit.
    let threshold5h: Double
    let threshold7d: Double
    let dryRun: Bool

    /// "90%" when both windows share a switch point, else "5h 80% · 7d 95%".
    var thresholdText: String {
        threshold5h == threshold7d
            ? "\(threshold5h)%"
            : "5h \(threshold5h)% · 7d \(threshold7d)%"
    }
}

struct ManualSwitchOutcome: Sendable {
    let warning: String?
}

final class ProcessCancellation: @unchecked Sendable {
    private let lock = NSLock()
    private var cancelled = false

    var isCancelled: Bool {
        lock.lock()
        defer { lock.unlock() }
        return cancelled
    }

    func cancel() {
        lock.lock()
        cancelled = true
        lock.unlock()
    }
}

struct EngineAccountList: Decodable, Sendable {
    let schemaVersion: Int
    let activeAccountNumber: Int?
    let accounts: [EngineAccount]
}

struct EngineAccount: Decodable, Sendable {
    let number: Int
    let email: String
    let organizationName: String?
    let alias: String?
    let active: Bool
    let disabled: Bool?
    let usageStatus: String
    let usage: AccountUsage?
    let usageAgeSeconds: Double?
    let lastGoodUsage: AccountUsage?
    let lastGoodAgeSeconds: Double?

    var account: Account {
        Account(
            id: "account:\(number)",
            number: number,
            email: email,
            organizationName: organizationName,
            alias: alias,
            active: active,
            disabled: disabled ?? false,
            usageStatus: usageStatus,
            usage: usage,
            usageAgeSeconds: usageAgeSeconds,
            lastGoodUsage: lastGoodUsage,
            lastGoodAgeSeconds: lastGoodAgeSeconds
        )
    }
}

struct UsageWindow: Decodable, Equatable, Sendable {
    let pct: Double?
    let resetsAt: String?
    /// ccshift's "21h 50m" / "Sep 30 06:48" strings, computed when it printed the JSON.
    let countdown: String?
    let clock: String?

    enum CodingKeys: String, CodingKey {
        case pct
        case usedPercent = "used_percent"
        case usedPercentCamel = "usedPercent"
        case resetsAt
        case resetsAtSnake = "resets_at"
        case countdown
        case clock
    }

    init(pct: Double?, resetsAt: String?, countdown: String? = nil, clock: String? = nil) {
        self.pct = pct
        self.resetsAt = resetsAt
        self.countdown = countdown
        self.clock = clock
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        pct = try values.decodeIfPresent(Double.self, forKey: .pct)
            ?? values.decodeIfPresent(Double.self, forKey: .usedPercent)
            ?? values.decodeIfPresent(Double.self, forKey: .usedPercentCamel)
        resetsAt = try values.decodeIfPresent(String.self, forKey: .resetsAt)
            ?? values.decodeIfPresent(String.self, forKey: .resetsAtSnake)
        countdown = try values.decodeIfPresent(String.self, forKey: .countdown)
        clock = try values.decodeIfPresent(String.self, forKey: .clock)
    }

    /// Time left until the reset, formatted like ccshift ("5d 2h", "21h 50m", "47m").
    /// Recomputed from `resetsAt` so an open menu does not show a frozen countdown.
    func resetLabel(now: Date = Date()) -> String? {
        if let resetsAt, let date = ResetTime.parse(resetsAt) {
            return "in \(ResetTime.countdown(until: date, now: now))"
        }
        if let countdown, !countdown.isEmpty { return "in \(countdown)" }
        if let resetsAt, !resetsAt.isEmpty { return resetsAt }
        return nil
    }

    var resetTooltip: String? {
        if let clock, !clock.isEmpty { return "Resets \(clock)" }
        return resetsAt
    }
}

enum ResetTime {
    static func parse(_ value: String) -> Date? {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = formatter.date(from: value) { return date }
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.date(from: value)
    }

    /// Mirrors ccshift's oauth.format_reset countdown.
    static func countdown(until reset: Date, now: Date) -> String {
        let total = max(0, Int(reset.timeIntervalSince(now)))
        let days = total / 86_400
        let hours = total % 86_400 / 3_600
        let minutes = total % 3_600 / 60
        if days > 0 { return "\(days)d \(hours)h" }
        if hours > 0 { return "\(hours)h \(minutes)m" }
        return "\(minutes)m"
    }
}

struct AccountUsage: Decodable, Equatable, Sendable {
    let fiveHour: UsageWindow?
    let sevenDay: UsageWindow?
    let scoped: [ScopedUsage]?

    enum CodingKeys: String, CodingKey {
        case fiveHour
        case fiveHourSnake = "five_hour"
        case sevenDay
        case sevenDaySnake = "seven_day"
        case scoped
    }

    init(fiveHour: UsageWindow?, sevenDay: UsageWindow?, scoped: [ScopedUsage]?) {
        self.fiveHour = fiveHour
        self.sevenDay = sevenDay
        self.scoped = scoped
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        fiveHour = try values.decodeIfPresent(UsageWindow.self, forKey: .fiveHour)
            ?? values.decodeIfPresent(UsageWindow.self, forKey: .fiveHourSnake)
        sevenDay = try values.decodeIfPresent(UsageWindow.self, forKey: .sevenDay)
            ?? values.decodeIfPresent(UsageWindow.self, forKey: .sevenDaySnake)
        scoped = try values.decodeIfPresent([ScopedUsage].self, forKey: .scoped)
    }
}

struct ScopedUsage: Decodable, Equatable, Sendable {
    let name: String?
    let label: String?
    let model: String?
    let pct: Double?
    let resetsAt: String?
    let countdown: String?
    let clock: String?

    enum CodingKeys: String, CodingKey {
        case name
        case label
        case model
        case pct
        case usedPercent = "used_percent"
        case usedPercentCamel = "usedPercent"
        case resetsAt
        case resetsAtSnake = "resets_at"
        case countdown
        case clock
    }

    var window: UsageWindow {
        UsageWindow(pct: pct, resetsAt: resetsAt, countdown: countdown, clock: clock)
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        name = try values.decodeIfPresent(String.self, forKey: .name)
        label = try values.decodeIfPresent(String.self, forKey: .label)
        model = try values.decodeIfPresent(String.self, forKey: .model)
        pct = try values.decodeIfPresent(Double.self, forKey: .pct)
            ?? values.decodeIfPresent(Double.self, forKey: .usedPercent)
            ?? values.decodeIfPresent(Double.self, forKey: .usedPercentCamel)
        resetsAt = try values.decodeIfPresent(String.self, forKey: .resetsAt)
            ?? values.decodeIfPresent(String.self, forKey: .resetsAtSnake)
        countdown = try values.decodeIfPresent(String.self, forKey: .countdown)
        clock = try values.decodeIfPresent(String.self, forKey: .clock)
    }
}

struct EngineSwitchReport: Decodable, Sendable {
    let schemaVersion: Int
    let switched: Bool
    let from: AccountReference?
    let to: AccountReference
    let strategy: String?
    let reason: String?
    let message: String?
    let warnings: [String]?
}

struct AccountReference: Decodable, Sendable {
    let number: Int?
    let email: String?
}

/// `ccshift status --json`: the login Claude Code is using right now, if any.
struct EngineStatus: Decodable, Sendable {
    let schemaVersion: Int
    let active: CurrentLogin?
}

struct CurrentLogin: Decodable, Equatable, Sendable {
    let email: String
    let managed: Bool
    let number: Int?
    let alias: String?
    let organizationName: String?

    /// The name ccshift shows for the saved account, else the email.
    var displayName: String {
        if let alias, !alias.isEmpty { return alias }
        return email
    }
}

/// `ccshift add --json` / `ccshift remove N --yes --json`.
struct AccountChangeReport: Decodable, Equatable, Sendable {
    struct ChangedAccount: Decodable, Equatable, Sendable {
        let number: Int
        let email: String
        let alias: String?

        var displayName: String {
            if let alias, !alias.isEmpty { return alias }
            return email
        }
    }

    let schemaVersion: Int
    /// "added", "refreshed", "removed", "disabled", "enabled" or "cancelled".
    let action: String
    let account: ChangedAccount?
    let wasActive: Bool?
    /// enable/disable: false when the account already was in that state.
    let changed: Bool?
    /// disable: no account is left for automatic switching.
    let rotationEmpty: Bool?
}

/// What the Add Account sheet knows about the current Claude Code login.
enum CurrentLoginState: Equatable, Sendable {
    case unknown
    case checking
    case signedOut
    case signedIn(CurrentLogin)
    case failed(String)
}

struct CommandResult: Sendable {
    let output: Data
    let standardError: String
    let terminationStatus: Int32
}

struct AccountRow: Identifiable, Equatable, Sendable {
    let account: Account
    let isActive: Bool
    let isLoading: Bool
    let isStale: Bool
    let message: String?

    var id: String { account.id }
}

enum CLIError: LocalizedError, Equatable {
    case executableNotFound
    case invalidExecutablePath
    case invalidAccountNumber
    case timedOut
    case cancelled
    case invalidThreshold
    case failed(String)
    case invalidJSON(String)
    case commandRejected(String)
    case unsupportedSchema(Int)
    case unexpectedSelection(expected: Int, actual: Int?)
    case switchRejected(String)
    case accountCommandsUnsupported
    case accountListChanged

    var errorDescription: String? {
        switch self {
        case .executableNotFound:
            "Could not find the ccshift command. Install ccshift or choose its executable."
        case .invalidExecutablePath:
            "The configured ccshift path must be an absolute executable file."
        case .invalidAccountNumber:
            "That account number is invalid."
        case .timedOut:
            "The ccshift command took too long to respond."
        case .cancelled:
            "The automatic check was stopped."
        case .invalidThreshold:
            "Choose a threshold from 50% to 99.9%."
        case .failed:
            "ccshift could not complete the request."
        case .invalidJSON:
            "Could not read ccshift data. Update ccshift and try again."
        case let .commandRejected(message):
            message
        case let .unsupportedSchema(version):
            "This ccshift version uses unsupported JSON schema version \(version). Update the menu bar app or ccshift and try again."
        case let .unexpectedSelection(expected, actual):
            "ccshift selected account \(actual.map(String.init) ?? "unknown") instead of account \(expected)."
        case let .switchRejected(message):
            message.isEmpty ? "ccshift could not switch accounts." : message
        case .accountListChanged:
            "The account list changed since it was shown. Check the refreshed list and try again."
        case .accountCommandsUnsupported:
            "This ccshift version is too old for this action in the menu bar app. Run ccshift upgrade, then try again."
        }
    }
}
