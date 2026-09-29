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
                Image(nsImage: MenuBarGlyph.image)
                Text(model.menuTitle)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
        }
        .menuBarExtraStyle(.window)

        Settings {
            SettingsView(model: model)
        }
        .commands {
            // The app menu's default Quit (⌘Q with the Settings window key)
            // calls `terminate:` directly. Route it through `shutdown()` first,
            // exactly like the Quit row in the popover, so an in-flight
            // automatic check is stopped instead of outliving the app.
            CommandGroup(replacing: .appTermination) {
                Button("Quit ccshift") {
                    model.shutdown()
                    NSApp.terminate(nil)
                }
                .keyboardShortcut("q", modifiers: .command)
            }
        }
    }
}
