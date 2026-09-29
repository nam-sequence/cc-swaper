import AppKit
import SwiftUI

private final class MenuBarApplicationDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
    }
}

@main
struct CcsMenuBarApp: App {
    @NSApplicationDelegateAdaptor(MenuBarApplicationDelegate.self) private var delegate
    @StateObject private var model = MenuBarModel()

    var body: some Scene {
        MenuBarExtra {
            MenuBarView(model: model)
        } label: {
            HStack(spacing: 4) {
                Image(systemName: "person.crop.circle")
                Text(model.menuTitle)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
        }
        .menuBarExtraStyle(.window)
    }
}
