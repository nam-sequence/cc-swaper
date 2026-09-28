import Foundation
import ServiceManagement

enum LaunchAtLoginStatus: Equatable, Sendable {
    case notRegistered
    case enabled
    case requiresApproval
    case notFound
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
        switch service.status {
        case .notRegistered: .notRegistered
        case .enabled: .enabled
        case .requiresApproval: .requiresApproval
        case .notFound: .notFound
        @unknown default: .notFound
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
