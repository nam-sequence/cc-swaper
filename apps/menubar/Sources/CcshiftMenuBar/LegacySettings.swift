import Foundation

/// Up to 1.2 the app was CcsMenuBar.app, with the bundle identifier
/// `com.namsequence.ccswaper.menubar` and `ccs`-prefixed settings. The first
/// launch under the ccshift name carries those settings over.
enum LegacySettings {
    static let bundleIdentifier = "com.namsequence.ccswaper.menubar"
    static let importedKey = "ccshiftImportedLegacySettings"

    /// The keys that were renamed. Everything else, such as the Settings
    /// window's frame or `ccshiftExecutablePath`, keeps its name.
    static let renamedKeys = [
        "ccsAutoSwitchEnabled": "ccshiftAutoSwitchEnabled",
        "ccsAutoSwitchThreshold": "ccshiftAutoSwitchThreshold",
        "ccsAutoSwitchDryRun": "ccshiftAutoSwitchDryRun",
        "ccsAutoCheckForUpdates": "ccshiftAutoCheckForUpdates",
        "ccsLastUpdateCheck": "ccshiftLastUpdateCheck",
        "ccsSkippedUpdateVersion": "ccshiftSkippedUpdateVersion",
        "ccsNotifiedUpdateVersion": "ccshiftNotifiedUpdateVersion",
        "ccsSignInOpener": "ccshiftSignInOpener",
    ]

    /// Copies the old app's settings, once. A setting already made here is
    /// kept, and the old app's own settings are left as they are.
    static func importOnce(
        into defaults: UserDefaults = .standard,
        from legacy: [String: Any]? = UserDefaults.standard.persistentDomain(forName: bundleIdentifier)
    ) {
        guard !defaults.bool(forKey: importedKey) else { return }
        for (key, value) in legacy ?? [:] {
            let name = renamedKeys[key] ?? key
            if defaults.object(forKey: name) == nil {
                defaults.set(value, forKey: name)
            }
        }
        defaults.set(true, forKey: importedKey)
    }
}
