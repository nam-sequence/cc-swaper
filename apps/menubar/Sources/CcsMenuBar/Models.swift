import Foundation

struct Account: Identifiable, Equatable, Sendable {
    let id: String
    let mode: BackendMode
    let number: Int?
    let profileName: String?
    let email: String
    let organizationName: String?
    let alias: String?
    var active: Bool
    let disabled: Bool
    let signedIn: Bool?
    let usageStatus: String
    var usage: AccountUsage?
    let usageAgeSeconds: Double?
    let lastGoodUsage: AccountUsage?
    let lastGoodAgeSeconds: Double?
    let plan: String?

    var displayName: String {
        if let alias, !alias.isEmpty { return alias }
        if let profileName, !profileName.isEmpty { return profileName }
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
        case "token_expired": "This account needs to sign in again."
        case "api_key": "Usage is unavailable for an API key account."
        case "keychain_unavailable": "macOS Keychain is unavailable."
        case "relogin_required": "Sign in to this account again."
        case "foreign_credential": "This account uses an unsupported credential type."
        case "no_credentials": "This account is not signed in."
        case "unavailable": "Usage is temporarily unavailable."
        default: usageStatus
        }
    }
}

enum BackendMode: String, CaseIterable, Identifiable, Sendable {
    case engine = "Engine Accounts"
    case legacy = "Existing Profiles"

    var id: String { rawValue }
}

struct DashboardSnapshot: Sendable {
    let accounts: [Account]
    let activeAccountNumber: Int?
    let activeProfileName: String?
    let launchBackend: String?
    let warning: String?
}

struct RoutingReport: Decodable, Sendable {
    let schemaVersion: Int
    let launchBackend: String
    let selectedLegacyProfile: String?
}

struct AutoSwitchOutcome: Equatable, Sendable {
    let eventKind: String?
    let summary: String
    let threshold: Double
    let dryRun: Bool
}

struct ManualSwitchOutcome: Sendable {
    let routingWarning: String?
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
            id: "engine:\(number)",
            mode: .engine,
            number: number,
            profileName: nil,
            email: email,
            organizationName: organizationName,
            alias: alias,
            active: active,
            disabled: disabled ?? false,
            signedIn: nil,
            usageStatus: usageStatus,
            usage: usage,
            usageAgeSeconds: usageAgeSeconds,
            lastGoodUsage: lastGoodUsage,
            lastGoodAgeSeconds: lastGoodAgeSeconds,
            plan: nil
        )
    }
}

struct LegacyProfileList: Decodable, Sendable {
    let schemaVersion: Int
    let selected: String?
    let profiles: [LegacyProfile]
}

struct LegacyProfile: Decodable, Sendable {
    let name: String
    let kind: String
    let selected: Bool
    let signedIn: Bool
    let authMethod: String
    let email: String?
    let organization: String?

    func account(usage: LegacyUsageAccount?) -> Account {
        let status: String
        if let error = usage?.error, !error.isEmpty {
            status = error
        } else if !signedIn {
            status = "Sign-in required (\(authMethod))"
        } else if usage == nil {
            status = "Usage unavailable"
        } else {
            status = "ok"
        }
        let normalizedUsage = usage.map { value in
            AccountUsage(
                fiveHour: value.fiveHour,
                sevenDay: value.sevenDay,
                scoped: value.modelWeekly
            )
        }
        return Account(
            id: "legacy:\(name)",
            mode: .legacy,
            number: nil,
            profileName: name,
            email: email ?? "",
            organizationName: organization,
            alias: name,
            active: selected,
            disabled: !signedIn,
            signedIn: signedIn,
            usageStatus: status,
            usage: normalizedUsage,
            usageAgeSeconds: nil,
            lastGoodUsage: nil,
            lastGoodAgeSeconds: nil,
            plan: usage?.plan ?? (kind == "default" ? "Default profile" : nil)
        )
    }
}

struct LegacyUsageReport: Decodable, Sendable {
    let accounts: [LegacyUsageAccount]
}

struct LegacyUsageAccount: Decodable, Sendable {
    let profile: String
    let plan: String?
    let fiveHour: UsageWindow?
    let sevenDay: UsageWindow?
    let modelWeekly: [ScopedUsage]?
    let error: String?

    static func unavailable(_ profile: String) -> LegacyUsageAccount {
        LegacyUsageAccount(
            profile: profile,
            plan: nil,
            fiveHour: nil,
            sevenDay: nil,
            modelWeekly: nil,
            error: "Usage unavailable"
        )
    }

    enum CodingKeys: String, CodingKey {
        case profile
        case plan
        case fiveHour = "five_hour"
        case sevenDay = "seven_day"
        case modelWeekly = "model_weekly"
        case error
    }
}

struct UsageWindow: Decodable, Equatable, Sendable {
    let pct: Double?
    let resetsAt: String?

    enum CodingKeys: String, CodingKey {
        case pct
        case usedPercent = "used_percent"
        case usedPercentCamel = "usedPercent"
        case resetsAt
        case resetsAtSnake = "resets_at"
    }

    init(pct: Double?, resetsAt: String?) {
        self.pct = pct
        self.resetsAt = resetsAt
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        pct = try values.decodeIfPresent(Double.self, forKey: .pct)
            ?? values.decodeIfPresent(Double.self, forKey: .usedPercent)
            ?? values.decodeIfPresent(Double.self, forKey: .usedPercentCamel)
        resetsAt = try values.decodeIfPresent(String.self, forKey: .resetsAt)
            ?? values.decodeIfPresent(String.self, forKey: .resetsAtSnake)
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

    enum CodingKeys: String, CodingKey {
        case name
        case label
        case model
        case pct
        case usedPercent = "used_percent"
        case usedPercentCamel = "usedPercent"
        case resetsAt
        case resetsAtSnake = "resets_at"
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
    }
}

struct EngineStatusReport: Decodable, Sendable {
    let schemaVersion: Int
    let active: ActiveAccount?
    let launchBackend: String?

    enum CodingKeys: String, CodingKey {
        case schemaVersion
        case active
        case launchBackend
        case launchBackendSnake = "launch_backend"
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        schemaVersion = try values.decode(Int.self, forKey: .schemaVersion)
        active = try values.decodeIfPresent(ActiveAccount.self, forKey: .active)
        launchBackend = try values.decodeIfPresent(String.self, forKey: .launchBackend)
            ?? values.decodeIfPresent(String.self, forKey: .launchBackendSnake)
    }
}

struct ActiveAccount: Decodable, Sendable {
    let number: Int?
    let email: String?
    let managed: Bool
    let usageStatus: String?
    let usage: AccountUsage?
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
    let launchBackend: String?
    let routingChanged: Bool?
    let routingWarning: String?
}

struct AccountReference: Decodable, Sendable {
    let number: Int?
    let email: String?
}

struct LegacySwitchReport: Decodable, Sendable {
    let schemaVersion: Int
    let selected: String
    let launchBackend: String?
}

struct CommandResult: Sendable {
    let output: Data
    let standardError: String
    let terminationStatus: Int32
}

struct AccountRow: Identifiable, Equatable, Sendable {
    let account: Account
    let mode: BackendMode
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
    case invalidProfileName
    case timedOut
    case cancelled
    case invalidThreshold
    case failed(String)
    case invalidJSON(String)
    case commandRejected(String)
    case unsupportedSchema(Int)
    case unexpectedSelection(expected: Int, actual: Int?)
    case switchRejected(String)

    var errorDescription: String? {
        switch self {
        case .executableNotFound:
            "Could not find the ccs command. Choose its installed executable."
        case .invalidExecutablePath:
            "The configured ccs path must be an absolute executable file."
        case .invalidAccountNumber:
            "That account number is invalid."
        case .invalidProfileName:
            "That profile name is invalid."
        case .timedOut:
            "The ccs command took too long to respond."
        case .cancelled:
            "The automatic check was stopped."
        case .invalidThreshold:
            "Choose a threshold from 50% to 99.9%."
        case .failed:
            "ccs could not complete the request."
        case .invalidJSON:
            "Could not read ccs data. Update ccs and try again."
        case let .commandRejected(message):
            message
        case let .unsupportedSchema(version):
            "This ccs version uses unsupported JSON schema version \(version). Update ccs and try again."
        case let .unexpectedSelection(expected, actual):
            "ccs selected account \(actual.map(String.init) ?? "unknown") instead of account \(expected)."
        case let .switchRejected(message):
            message.isEmpty ? "ccs could not switch accounts." : message
        }
    }
}
