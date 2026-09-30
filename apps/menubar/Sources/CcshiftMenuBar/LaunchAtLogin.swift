import Foundation
import ServiceManagement

enum LaunchAtLoginStatus: Equatable, Sendable {
    case notRegistered
    case enabled
    case requiresApproval
}
@MainActor
protocol LaunchAtLoginManaging: AnyObject {
    func status() -> LaunchAtLoginStatus
    func setEnabled(_ enabled: Bool) throws -> LaunchAtLoginStatus
    func openLoginItemsSettings()
}

@MainActor
final class SystemLaunchAtLoginManager: LaunchAtLoginManaging {
    private let service: SMAppService

    init(service: SMAppService = .mainApp) {
        self.service = service
    }

    func status() -> LaunchAtLoginStatus {
        Self.status(for: service.status)
    }

    /// An app that was never registered reports `.notFound` (there is no
    /// Background Task Management record yet), so it is treated as off, not
    /// as unavailable. If the bundle really cannot be registered, `register()`
    /// throws and the error is shown instead.
    nonisolated static func status(for status: SMAppService.Status) -> LaunchAtLoginStatus {
        switch status {
        case .enabled: .enabled
        case .requiresApproval: .requiresApproval
        case .notRegistered, .notFound: .notRegistered
        @unknown default: .notRegistered
        }
    }

    func setEnabled(_ enabled: Bool) throws -> LaunchAtLoginStatus {
        if enabled {
            try service.register()
        } else {
            try service.unregister()
        }
        return status()
    }

    func openLoginItemsSettings() {
        SMAppService.openSystemSettingsLoginItems()
    }
}
