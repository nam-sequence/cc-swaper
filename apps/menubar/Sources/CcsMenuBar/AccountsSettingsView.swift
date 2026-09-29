import AppKit
import SwiftUI

// MARK: - Accounts pane

/// Settings › Accounts: the managed accounts, each with an actions menu like
/// the Known Networks list in Wi-Fi settings, and "Add Account…" below.
struct AccountsSettingsPane: View {
    @ObservedObject var model: MenuBarModel

    var body: some View {
        Form {
            Section {
                if model.accounts.isEmpty {
                    Text(model.isRefreshing ? "Loading accounts…" : "No accounts yet. Add the account Claude Code is signed in to.")
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                } else {
                    ForEach(model.accounts) { account in
                        AccountSettingsRow(
                            account: account,
                            isSwitching: model.switchingAccountID == account.id,
                            isBusy: model.isChangingAccounts || model.switchingAccountID != nil
                                || model.isAddAccountSheetPresented,
                            switchAction: { model.switchTo(account) },
                            removeAction: { model.requestRemoval(of: account) }
                        )
                    }
                }
            } footer: {
                HStack(alignment: .firstTextBaseline, spacing: 12) {
                    footerStatus
                    Spacer(minLength: 12)
                    if model.isChangingAccounts && !model.isAddAccountSheetPresented {
                        ProgressView()
                            .controlSize(.small)
                            .accessibilityLabel("Updating accounts")
                    }
                    Button("Add Account…", action: model.beginAddingAccount)
                        .disabled(model.executablePath == nil || model.isChangingAccounts || model.isSigningIn)
                }
            }
        }
        .formStyle(.grouped)
        .fixedSize(horizontal: false, vertical: true)
        .frame(width: SettingsMetrics.width)
        .sheet(isPresented: $model.isAddAccountSheetPresented, onDismiss: model.addAccountSheetDismissed) {
            AddAccountSheet(model: model)
        }
        .confirmationDialog(
            removalTitle,
            isPresented: removalPresented,
            titleVisibility: .visible,
            presenting: model.accountPendingRemoval
        ) { account in
            Button("Remove", role: .destructive) { model.removeAccount(account) }
            Button("Cancel", role: .cancel) {}
        } message: { account in
            Text(removalMessage(for: account))
        }
    }

    /// A removal failure, else the last change's confirmation. The sheet shows
    /// its own errors while it is open.
    @ViewBuilder
    private var footerStatus: some View {
        if let error = model.accountChangeError, !model.isAddAccountSheetPresented {
            Label {
                Text(error)
            } icon: {
                Image(systemName: "exclamationmark.triangle.fill")
                    .foregroundStyle(.orange)
            }
            .fixedSize(horizontal: false, vertical: true)
        } else if let notice = model.accountChangeNotice {
            Label {
                Text(notice)
            } icon: {
                Image(systemName: "checkmark.circle.fill")
                    .foregroundStyle(.green)
            }
            .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var removalPresented: Binding<Bool> {
        Binding(
            get: { model.accountPendingRemoval != nil },
            set: { if !$0 { model.accountPendingRemoval = nil } }
        )
    }

    private var removalTitle: String {
        guard let account = model.accountPendingRemoval else { return "Remove Account?" }
        return "Remove “\(account.displayName)” from ccshift?"
    }

    private func removalMessage(for account: Account) -> String {
        var message = "ccshift deletes the login it saved for \(account.email). "
            + "The Claude account itself is not affected, and you can add it again later."
        if account.active {
            message += " Claude Code stays signed in to it."
        }
        return message
    }
}

/// One managed account: badge, name, details, active status and actions.
struct AccountSettingsRow: View {
    let account: Account
    let isSwitching: Bool
    let isBusy: Bool
    let switchAction: () -> Void
    let removeAction: () -> Void

    private var details: String? {
        var parts: [String] = []
        if !account.email.isEmpty, account.email != account.displayName { parts.append(account.email) }
        if let organization = account.organizationName, !organization.isEmpty { parts.append(organization) }
        if account.disabled { parts.append("Not in auto-switch") }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    var body: some View {
        HStack(spacing: 10) {
            CCBadge(symbol: "person.fill", isOn: account.active, size: 26)
            VStack(alignment: .leading, spacing: 1) {
                Text(account.displayName)
                    .lineLimit(1)
                    .truncationMode(.middle)
                if let details {
                    Text(details)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                        .help(details)
                }
            }
            Spacer(minLength: 8)
            if isSwitching {
                ProgressView()
                    .controlSize(.small)
                    .accessibilityLabel("Switching account")
            } else if account.active {
                HStack(spacing: 5) {
                    Circle()
                        .fill(.green)
                        .frame(width: 7, height: 7)
                        .accessibilityHidden(true)
                    Text("Active")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
            }
            Menu {
                actions
            } label: {
                Image(systemName: "ellipsis.circle")
                    .foregroundStyle(.secondary)
            }
            .menuStyle(.borderlessButton)
            .menuIndicator(.hidden)
            .fixedSize()
            .help("Account actions")
            .accessibilityLabel("Actions for \(account.displayName)")
        }
        .contextMenu { actions }
        .accessibilityElement(children: .contain)
    }

    @ViewBuilder
    private var actions: some View {
        Button("Switch to This Account", action: switchAction)
            .disabled(account.active || isBusy)
        Divider()
        Button("Remove Account…", role: .destructive, action: removeAction)
            .disabled(isBusy)
    }
}

// MARK: - Add Account sheet

/// Two ways to add an account. Signing in with the browser runs Claude Code's
/// own sign-in in a separate profile (`ccshift add --login`), so the current
/// Claude Code login stays as it is. Or ccshift saves the login Claude Code is
/// using now (`ccshift add`).
struct AddAccountSheet: View {
    @ObservedObject var model: MenuBarModel

    enum Method: Hashable {
        case signIn
        case currentLogin
    }

    @State private var method: Method
    @State private var email = ""
    @State private var alias = ""
    @State private var useSSO = false
    @State private var browsers: PrivateBrowsers
    @AppStorage("ccsSignInOpener") private var storedOpener = SignInOpener.privateWindow.storageValue

    init(model: MenuBarModel, method: Method = .signIn, browsers: PrivateBrowsers = .detect()) {
        self.model = model
        _method = State(initialValue: method)
        _browsers = State(initialValue: browsers)
    }

    /// The remembered choice while it is still available (a browser can be
    /// uninstalled), else ccshift's private sign-in window.
    private var opener: SignInOpener {
        let stored = SignInOpener(storageValue: storedOpener)
        if case let .privateBrowser(bundleID) = stored,
           !browsers.installed.contains(where: { $0.bundleID == bundleID }) {
            return .privateWindow
        }
        return stored
    }

    private var openerBinding: Binding<SignInOpener> {
        Binding(get: { opener }, set: { storedOpener = $0.storageValue })
    }

    private var isBusy: Bool { model.isSigningIn || model.isChangingAccounts }

    private var canSaveCurrentLogin: Bool {
        if case let .signedIn(login) = model.currentLogin, !login.managed {
            return !isBusy
        }
        return false
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            VStack(alignment: .leading, spacing: 4) {
                Text("Add Account")
                    .font(.headline)
                Text("Your current Claude Code login stays as it is.")
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            Picker("Method", selection: $method) {
                Text("Sign In with Browser").tag(Method.signIn)
                Text("Current Login").tag(Method.currentLogin)
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .fixedSize()
            .frame(maxWidth: .infinity)
            .disabled(isBusy)

            switch method {
            case .signIn: signInContent
            case .currentLogin: currentLoginContent
            }

            if let error = model.accountChangeError {
                Label {
                    Text(error)
                        .fixedSize(horizontal: false, vertical: true)
                } icon: {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .foregroundStyle(.orange)
                }
                .foregroundStyle(.secondary)
            }

            buttons
        }
        .padding(20)
        .frame(width: 460)
        .onChange(of: method) { _, newValue in
            if newValue == .currentLogin { model.checkCurrentLogin() }
        }
        // Back from another app (a browser, or /login in Terminal): read the
        // current login again for the Current Login tab.
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification)) { _ in
            if method == .currentLogin, !isBusy { model.checkCurrentLogin() }
        }
    }

    // MARK: Sign in with the browser

    @ViewBuilder
    private var signInContent: some View {
        VStack(alignment: .leading, spacing: 10) {
            fieldRow("Email") {
                TextField("Email", text: $email, prompt: Text("Optional, pre-fills the sign-in page"))
                    .labelsHidden()
                    .textFieldStyle(.roundedBorder)
                    .textContentType(.emailAddress)
                    .disableAutocorrection(true)
            }
            fieldRow("Alias") {
                TextField("Alias", text: $alias, prompt: Text("Optional, e.g. work"))
                    .labelsHidden()
                    .textFieldStyle(.roundedBorder)
            }
            fieldRow("Open in") {
                Picker("Open in", selection: openerBinding) {
                    Label("Private Sign-In Window", systemImage: "lock.shield")
                        .tag(SignInOpener.privateWindow)
                    if !browsers.installed.isEmpty {
                        Divider()
                        ForEach(browsers.installed) { browser in
                            Label {
                                Text("\(browser.name) (Private)")
                            } icon: {
                                Image(nsImage: browsers.icon(for: browser))
                            }
                            .tag(SignInOpener.privateBrowser(browser.bundleID))
                        }
                    }
                    Divider()
                    Label("Default Browser", systemImage: "safari")
                        .tag(SignInOpener.defaultBrowser)
                }
                .labelsHidden()
                .fixedSize()
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            fieldRow("") {
                Toggle("Use single sign-on (SSO)", isOn: $useSSO)
                    .toggleStyle(.checkbox)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .disabled(isBusy)

        if model.isSigningIn {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                ProgressView().controlSize(.small)
                Text(opener == .privateWindow
                     ? "Finish signing in in the sign-in window. ccshift adds the account when you’re done."
                     : "Finish signing in in your browser. ccshift adds the account when you’re done.")
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .accessibilityElement(children: .combine)
        } else {
            Text(signInNote)
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var signInNote: String {
        switch opener {
        case .privateWindow:
            return "The Claude sign-in page opens in a private window that shares nothing with your browsers, so an account already signed in there isn’t used by mistake. Works with Safari, Chrome, Arc and any other browser."
        case let .privateBrowser(bundleID):
            let name = browsers.installed.first { $0.bundleID == bundleID }?.name ?? "the browser"
            return "The Claude sign-in page opens in a private \(name) window, so an account already signed in there isn’t used by mistake."
        case .defaultBrowser:
            return "The Claude sign-in page opens in your default browser. If it’s signed in to a different Claude account, switch accounts there first."
        }
    }

    /// A trailing-aligned label column, so the fields line up.
    private func fieldRow<Field: View>(_ label: String, @ViewBuilder field: () -> Field) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(label)
                .frame(width: 56, alignment: .trailing)
                .accessibilityHidden(true)
            field()
        }
    }

    // MARK: Save the current login

    @ViewBuilder
    private var currentLoginContent: some View {
        switch model.currentLogin {
        case .unknown, .checking:
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text("Checking the Claude Code login…")
                    .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
            .accessibilityElement(children: .combine)

        case let .failed(message):
            Label {
                Text(message)
                    .fixedSize(horizontal: false, vertical: true)
            } icon: {
                Image(systemName: "exclamationmark.triangle.fill")
                    .foregroundStyle(.orange)
            }
            .foregroundStyle(.secondary)
            Button("Check Again", action: model.checkCurrentLogin)
                .controlSize(.small)

        case .signedOut:
            loginCard(title: "Claude Code is not signed in", detail: nil, isSaved: false)
            useBrowserHint

        case let .signedIn(login) where login.managed:
            loginCard(
                title: login.email,
                detail: "Already saved as \(login.displayName)\(login.number.map { " (account \($0))" } ?? "").",
                isSaved: true
            )
            useBrowserHint

        case let .signedIn(login):
            loginCard(
                title: login.email,
                detail: login.organizationName.flatMap { $0.isEmpty ? nil : $0 } ?? "Not saved in ccshift yet.",
                isSaved: false
            )
            fieldRow("Alias") {
                TextField("Alias", text: $alias, prompt: Text("Optional, e.g. work"))
                    .labelsHidden()
                    .textFieldStyle(.roundedBorder)
                    .onSubmit { if canSaveCurrentLogin { model.addCurrentAccount(alias: alias) } }
            }
        }
    }

    private var useBrowserHint: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text("To add a different account, sign in with the browser.")
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            Button("Sign In with Browser") { method = .signIn }
                .buttonStyle(.link)
                .font(.subheadline)
        }
    }

    private func loginCard(title: String, detail: String?, isSaved: Bool) -> some View {
        HStack(spacing: 10) {
            CCBadge(symbol: "person.fill", isOn: isSaved, size: 32)
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(.body.weight(.medium))
                    .lineLimit(1)
                    .truncationMode(.middle)
                if let detail {
                    Text(detail)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: 0)
        }
        .accessibilityElement(children: .combine)
    }

    // MARK: Buttons

    private var buttons: some View {
        HStack(spacing: 8) {
            if model.isChangingAccounts {
                ProgressView()
                    .controlSize(.small)
                    .accessibilityLabel("Adding account")
            }
            Spacer()
            if model.isSigningIn {
                // Esc stops the sign-in; the sheet stays open to try again.
                Button("Stop Signing In", role: .cancel, action: model.cancelSignIn)
                    .keyboardShortcut(.cancelAction)
            } else {
                Button("Cancel", role: .cancel) {
                    model.isAddAccountSheetPresented = false
                }
                .keyboardShortcut(.cancelAction)
            }
            switch method {
            case .signIn:
                Button("Sign In…") {
                    model.signInAndAddAccount(email: email, sso: useSSO, alias: alias, opener: opener)
                }
                .keyboardShortcut(.defaultAction)
                .disabled(isBusy)
            case .currentLogin:
                Button("Add Account") {
                    model.addCurrentAccount(alias: alias)
                }
                .keyboardShortcut(.defaultAction)
                .disabled(!canSaveCurrentLogin)
            }
        }
    }
}
