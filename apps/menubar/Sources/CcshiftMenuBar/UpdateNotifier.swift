import AppKit
import UserNotifications

/// A macOS notification per new version. Permission is asked the first time an
/// update is found, not at launch; clicking the notification opens the release.
@MainActor
final class SystemUpdateNotifier: NSObject, UpdateNotifying, UNUserNotificationCenterDelegate {
    static let shared = SystemUpdateNotifier()

    /// Set as the notification center's delegate at launch, so a click on a
    /// notification from an earlier run is handled too.
    func install() {
        UNUserNotificationCenter.current().delegate = self
    }

    func notify(_ update: AvailableUpdate) {
        let installed = AppVersion.current
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { granted, _ in
            guard granted else { return }
            let content = UNMutableNotificationContent()
            content.title = "ccshift \(update.version) is available"
            content.body = "You have \(installed). Open ccshift Settings to download it."
            content.userInfo = ["url": update.releaseURL.absoluteString]
            let request = UNNotificationRequest(
                identifier: "ccshift-update-\(update.version)", content: content, trigger: nil
            )
            UNUserNotificationCenter.current().add(request)
        }
    }

    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        if let text = response.notification.request.content.userInfo["url"] as? String,
           let url = URL(string: text) {
            DispatchQueue.main.async { NSWorkspace.shared.open(url) }
        }
        completionHandler()
    }

    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .sound])
    }
}
