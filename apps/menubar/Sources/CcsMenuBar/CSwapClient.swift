import Darwin
import Foundation

private final class LockedBuffer: @unchecked Sendable {
    private let lock = NSLock()
    private var data = Data()

    func replace(with value: Data) {
        lock.lock()
        data = value
        lock.unlock()
    }

    func value() -> Data {
        lock.lock()
        defer { lock.unlock() }
        return data
    }
}

struct CSwapClient: Sendable {
    let executableURL: URL
    var timeout: TimeInterval = 55

    init(executableURL: URL, timeout: TimeInterval = 55) throws {
        guard executableURL.isFileURL,
              executableURL.path.hasPrefix("/"),
              let stableLauncher = CLIResolver.stableLauncherURL(executableURL)
        else {
            throw CLIError.invalidExecutablePath
        }
        // Keep the launcher's stable path (often ~/.local/bin/ccshift -> versioned
        // uv shim). Resolve and validate its current target immediately before
        // each spawn instead of persisting the target path here.
        self.executableURL = stableLauncher
        self.timeout = timeout
    }

    func dashboard() throws -> DashboardSnapshot {
        let list: EngineAccountList = try decodeVersionOne(run(arguments: ["list", "--json"]))
        let activeNumber = list.activeAccountNumber ?? list.accounts.first(where: \.active)?.number
        let accounts = list.accounts.map { row -> Account in
            var account = row.account
            account.active = account.number == activeNumber
            return account
        }
        return DashboardSnapshot(accounts: accounts, activeAccountNumber: activeNumber, warning: nil)
    }

    func switchEngineAccount(to number: Int) throws -> EngineSwitchReport {
        guard number > 0 else {
            throw CLIError.invalidAccountNumber
        }
        let response: EngineSwitchReport = try decodeVersionOne(
            run(arguments: ["switch", String(number), "--json"])
        )
        if response.to.number == number {
            // ccshift reports switched=false / reason=already-active for the live account.
            return response
        }
        if !response.switched {
            throw CLIError.switchRejected(response.message ?? response.reason ?? "")
        }
        throw CLIError.unexpectedSelection(expected: number, actual: response.to.number)
    }

    func autoSwitchOnce(
        threshold: Double,
        dryRun: Bool,
        cancellation: ProcessCancellation
    ) throws -> AutoSwitchOutcome {
        guard (50...99.9).contains(threshold) else { throw CLIError.invalidThreshold }
        var arguments = ["auto", "--once", "--json", "--threshold", "\(threshold)"]
        if dryRun { arguments.append("--dry-run") }
        let result = try run(arguments: arguments, cancellation: cancellation)
        let lines = String(decoding: result.output, as: UTF8.self)
            .split(whereSeparator: \.isNewline)
        var events: [[String: Any]] = []
        for line in lines {
            guard let data = String(line).data(using: .utf8),
                  let event = try JSONSerialization.jsonObject(with: data) as? [String: Any]
            else {
                throw CLIError.invalidJSON("Could not read the auto-switch result.")
            }
            guard let version = event["schemaVersion"] as? Int else {
                throw CLIError.invalidJSON("The auto-switch result is missing its version.")
            }
            guard version == 1 else { throw CLIError.unsupportedSchema(version) }
            if let message = commandError(in: event) {
                throw CLIError.commandRejected(message)
            }
            events.append(event)
        }

        let meaningful = events.last(where: {
            !["poll", "sleep"].contains($0["event"] as? String ?? "")
        }) ?? events.last
        if result.terminationStatus == 1 {
            if let message = meaningful?["message"] as? String {
                throw CLIError.commandRejected(message)
            }
            throw CLIError.failed(result.standardError)
        }
        guard [0, 2, 3].contains(result.terminationStatus) else {
            throw CLIError.failed(result.standardError)
        }
        // argparse usage errors also exit 2, but print no JSON events. Only a
        // reported no-switch or all-exhausted event makes 2 or 3 a result.
        if events.isEmpty, result.terminationStatus != 0 {
            throw CLIError.failed(result.standardError)
        }
        return AutoSwitchOutcome(
            eventKind: meaningful?["event"] as? String,
            summary: describeAutoEvent(meaningful, exitCode: result.terminationStatus),
            threshold: threshold,
            dryRun: dryRun
        )
    }

    private func describeAutoEvent(_ event: [String: Any]?, exitCode: Int32) -> String {
        guard let event, let kind = event["event"] as? String else {
            return exitCode == 3 ? "No account is available below the threshold." : "No switch was needed."
        }
        switch kind {
        case "switch":
            let to = event["to"] as? [String: Any]
            let destination = to?["email"] as? String
                ?? to?["number"].map { String(describing: $0) }
                ?? "another account"
            let prefix = (event["dryRun"] as? Bool == true) ? "Would switch to" : "Switched to"
            return "\(prefix) \(destination)."
        case "no-switch":
            let reason = event["reason"] as? String ?? "No change needed"
            let detail = event["detail"] as? String
            if let detail, !detail.isEmpty {
                let ended = detail.hasSuffix(".") || detail.hasSuffix("!") || detail.hasSuffix("?")
                return "\(reason): \(detail)\(ended ? "" : ".")"
            }
            return "\(reason)."
        case "all-exhausted":
            if let reset = event["earliestResetAt"] as? String, !reset.isEmpty {
                return "All accounts are at their limit. Earliest reset: \(reset)."
            }
            return "All accounts are at their limit; no reset time is available."
        case "error":
            return event["message"] as? String ?? "Automatic switching reported an error."
        case "config-warning":
            return event["message"] as? String ?? "Check the automatic switching settings."
        default:
            return "Automatic switching checked the accounts (\(kind))."
        }
    }

    private func run(
        arguments: [String],
        cancellation: ProcessCancellation? = nil,
        timeout commandTimeout: TimeInterval? = nil
    ) throws -> CommandResult {
        if cancellation?.isCancelled == true { throw CLIError.cancelled }
        guard let targetExecutable = CLIResolver.validatedTarget(for: executableURL) else {
            throw CLIError.invalidExecutablePath
        }

        let standardOutput = Pipe()
        let standardError = Pipe()
        let outputGroup = DispatchGroup()
        let outputCapture = LockedBuffer()
        let errorCapture = LockedBuffer()

        outputGroup.enter()
        DispatchQueue.global(qos: .userInitiated).async {
            outputCapture.replace(with: standardOutput.fileHandleForReading.readDataToEndOfFile())
            outputGroup.leave()
        }
        outputGroup.enter()
        DispatchQueue.global(qos: .userInitiated).async {
            errorCapture.replace(with: standardError.fileHandleForReading.readDataToEndOfFile())
            outputGroup.leave()
        }

        let pid: pid_t
        do {
            pid = try spawnInOwnProcessGroup(
                executablePath: targetExecutable.path,
                arguments: arguments,
                standardOutput: standardOutput,
                standardError: standardError
            )
        } catch {
            standardOutput.fileHandleForWriting.closeFile()
            standardError.fileHandleForWriting.closeFile()
            _ = outputGroup.wait(timeout: .now() + 1)
            throw error
        }

        standardOutput.fileHandleForWriting.closeFile()
        standardError.fileHandleForWriting.closeFile()

        guard let waitStatus = waitForChild(
            pid,
            timeout: commandTimeout ?? timeout,
            cancellation: cancellation
        ) else {
            terminateProcessGroup(pid, outputGroup: outputGroup)
            _ = outputGroup.wait(timeout: .now() + 1)
            if cancellation?.isCancelled == true { throw CLIError.cancelled }
            throw CLIError.timedOut
        }

        guard outputGroup.wait(timeout: .now() + 2) == .success else {
            // A child spawned by ccshift still holds one of the output descriptors.
            // They inherit the dedicated process group, so kill only this tree.
            terminateProcessGroup(pid, outputGroup: outputGroup)
            if cancellation?.isCancelled == true { throw CLIError.cancelled }
            throw CLIError.timedOut
        }

        let exitCode = decodedExitCode(waitStatus)
        return CommandResult(
            output: outputCapture.value(),
            standardError: String(decoding: errorCapture.value(), as: UTF8.self)
                .trimmingCharacters(in: .whitespacesAndNewlines),
            terminationStatus: exitCode
        )
    }

    private func spawnInOwnProcessGroup(
        executablePath: String,
        arguments: [String],
        standardOutput: Pipe,
        standardError: Pipe
    ) throws -> pid_t {
        var actions: posix_spawn_file_actions_t?
        var attributes: posix_spawnattr_t?
        let actionsInit = posix_spawn_file_actions_init(&actions)
        guard actionsInit == 0 else { throw CLIError.failed(posixMessage(actionsInit)) }
        defer { posix_spawn_file_actions_destroy(&actions) }

        let attributesInit = posix_spawnattr_init(&attributes)
        guard attributesInit == 0 else { throw CLIError.failed(posixMessage(attributesInit)) }
        defer { posix_spawnattr_destroy(&attributes) }

        let outRead = standardOutput.fileHandleForReading.fileDescriptor
        let outWrite = standardOutput.fileHandleForWriting.fileDescriptor
        let errRead = standardError.fileHandleForReading.fileDescriptor
        let errWrite = standardError.fileHandleForWriting.fileDescriptor
        for action in [
            posix_spawn_file_actions_adddup2(&actions, outWrite, STDOUT_FILENO),
            posix_spawn_file_actions_adddup2(&actions, errWrite, STDERR_FILENO),
            posix_spawn_file_actions_addclose(&actions, outRead),
            posix_spawn_file_actions_addclose(&actions, errRead),
            posix_spawn_file_actions_addclose(&actions, outWrite),
            posix_spawn_file_actions_addclose(&actions, errWrite),
        ] where action != 0 {
            throw CLIError.failed(posixMessage(action))
        }

        let groupFlag = posix_spawnattr_setflags(&attributes, Int16(POSIX_SPAWN_SETPGROUP))
        guard groupFlag == 0 else { throw CLIError.failed(posixMessage(groupFlag)) }
        let groupResult = posix_spawnattr_setpgroup(&attributes, 0)
        guard groupResult == 0 else { throw CLIError.failed(posixMessage(groupResult)) }

        var strings: [UnsafeMutablePointer<CChar>] = []
        for value in [executablePath] + arguments {
            guard let copy = strdup(value) else {
                strings.forEach { free($0) }
                throw CLIError.failed("Could not prepare ccshift arguments.")
            }
            strings.append(copy)
        }
        defer { strings.forEach { free($0) } }
        var argv = strings.map { Optional($0) }
        argv.append(nil)

        var environmentStrings: [UnsafeMutablePointer<CChar>] = []
        for (key, value) in ccshiftEnvironment() {
            guard let copy = strdup("\(key)=\(value)") else {
                environmentStrings.forEach { free($0) }
                throw CLIError.failed("Could not prepare the ccshift environment.")
            }
            environmentStrings.append(copy)
        }
        defer { environmentStrings.forEach { free($0) } }
        var environment = environmentStrings.map { Optional($0) }
        environment.append(nil)

        var child: pid_t = 0
        let spawnResult = executablePath.withCString { path in
            posix_spawn(&child, path, &actions, &attributes, &argv, &environment)
        }
        guard spawnResult == 0 else {
            throw CLIError.failed(posixMessage(spawnResult))
        }
        return child
    }

    private func ccshiftEnvironment() -> [(String, String)] {
        let inherited = ProcessInfo.processInfo.environment
        let safeKeys: Set<String> = ["HOME", "USER", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE"]
        var environment = inherited.filter { key, _ in safeKeys.contains(key) || key.hasPrefix("LC_") }
        var searchPaths = [URL(fileURLWithPath: executableURL.path).deletingLastPathComponent().path]
        for candidate in ["\(NSHomeDirectory())/.local/bin", "/opt/homebrew/bin", "/usr/local/bin"] {
            if !searchPaths.contains(candidate) { searchPaths.append(candidate) }
        }
        if let path = inherited["PATH"] {
            searchPaths.append(contentsOf: path.split(separator: ":").map(String.init).filter { $0.hasPrefix("/") })
        }
        environment["PATH"] = searchPaths
            .filter { !$0.isEmpty }
            .reduce(into: [String]()) { result, path in
                if !result.contains(path) { result.append(path) }
            }
            .joined(separator: ":")
        return environment.sorted { $0.key < $1.key }
    }

    private func waitForChild(
        _ pid: pid_t,
        timeout: TimeInterval,
        cancellation: ProcessCancellation? = nil
    ) -> Int32? {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline, cancellation?.isCancelled != true {
            var status: Int32 = 0
            let result = waitpid(pid, &status, WNOHANG)
            if result == pid { return status }
            if result == -1, errno != EINTR { return nil }
            usleep(10_000)
        }
        return nil
    }

    private func terminateProcessGroup(_ pid: pid_t, outputGroup: DispatchGroup) {
        _ = kill(-pid, SIGTERM)
        _ = waitForChild(pid, timeout: 0.5)
        if outputGroup.wait(timeout: .now() + 0.5) != .success {
            _ = kill(-pid, SIGKILL)
            _ = waitForChild(pid, timeout: 1)
            _ = outputGroup.wait(timeout: .now() + 1)
        }
    }

    private func decodedExitCode(_ status: Int32) -> Int32 {
        if status & 0x7f == 0 { return (status >> 8) & 0xff }
        return 128 + (status & 0x7f)
    }

    private func posixMessage(_ code: Int32) -> String {
        String(cString: strerror(code))
    }

    private func decodeVersionOne<Value: Decodable & Sendable>(
        _ result: CommandResult,
        as type: Value.Type = Value.self
    ) throws -> Value {
        do {
            guard let root = try JSONSerialization.jsonObject(with: result.output) as? [String: Any] else {
                throw CLIError.invalidJSON("Could not read ccshift data. Update ccshift and try again.")
            }
            if let message = commandError(in: root) {
                throw CLIError.commandRejected(message)
            }
            guard let schemaVersion = root["schemaVersion"] as? Int else {
                throw CLIError.invalidJSON("The ccshift response is missing schemaVersion.")
            }
            guard schemaVersion == 1 else {
                throw CLIError.unsupportedSchema(schemaVersion)
            }
            return try JSONDecoder().decode(type, from: result.output)
        } catch let error as CLIError {
            throw error
        } catch {
            throw CLIError.invalidJSON("Could not read ccshift data. Update ccshift and try again.")
        }
    }

    private func commandError(in root: [String: Any]) -> String? {
        if let message = root["error"] as? String, !message.isEmpty { return message }
        if let error = root["error"] as? [String: Any] {
            for key in ["message", "reason", "detail", "description"] {
                if let value = error[key] as? String, !value.isEmpty { return value }
            }
        }
        let indicatesFailure = (root["ok"] as? Bool == false)
            || (root["success"] as? Bool == false)
        if indicatesFailure {
            for key in ["message", "reason", "detail"] {
                if let value = root[key] as? String, !value.isEmpty { return value }
            }
            return "ccshift rejected the request."
        }
        return nil
    }
}

enum CLIResolver {
    static func executable(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        defaults: UserDefaults = .standard,
        info: [String: Any] = Bundle.main.infoDictionary ?? [:],
        homeDirectory: String = NSHomeDirectory(),
        standardBinDirectories: [String] = ["/opt/homebrew/bin", "/usr/local/bin"]
    ) -> URL? {
        if let configured = defaults.string(forKey: "ccshiftExecutablePath"),
           let url = validExecutable(configured) {
            return url
        }
        if let bundled = info["CSwapExecutablePath"] as? String,
           let url = validExecutable(bundled) {
            return url
        }

        let preferredPaths = ["\(homeDirectory)/.local/bin/ccshift"]
            + standardBinDirectories.map { URL(fileURLWithPath: $0).appendingPathComponent("ccshift").path }
        for path in preferredPaths {
            if let url = validExecutable(path) { return url }
        }
        let directories = (environment["PATH"] ?? "")
            .split(separator: ":")
            .map(String.init)
            .filter { $0.hasPrefix("/") }
        for directory in directories {
            if let url = validExecutable(URL(fileURLWithPath: directory).appendingPathComponent("ccshift").path) {
                return url
            }
        }
        return nil
    }

    static func validExecutable(_ path: String) -> URL? {
        guard path.hasPrefix("/") else { return nil }
        return stableLauncherURL(URL(fileURLWithPath: path))
    }

    static func stableLauncherURL(_ url: URL) -> URL? {
        guard url.isFileURL, url.path.hasPrefix("/") else { return nil }
        let standardized = url.standardizedFileURL
        guard let parentPath = realPath(standardized.deletingLastPathComponent().path) else { return nil }
        let physicalParent = URL(fileURLWithPath: parentPath, isDirectory: true).standardizedFileURL
        guard trustedDirectoryChain(physicalParent) else { return nil }
        let launcher = physicalParent.appendingPathComponent(standardized.lastPathComponent)
        guard validatedTarget(for: launcher) != nil else { return nil }
        return launcher
    }

    static func validatedTarget(for launcherURL: URL) -> URL? {
        guard launcherURL.isFileURL, launcherURL.path.hasPrefix("/") else { return nil }
        let manager = FileManager.default
        let launcher = launcherURL.standardizedFileURL
        guard let launcherParentPath = realPath(launcher.deletingLastPathComponent().path) else { return nil }
        let launcherParent = URL(fileURLWithPath: launcherParentPath, isDirectory: true)
        guard trustedDirectoryChain(launcherParent),
              let launcherInfo = lstatEntry(at: launcher.path)
        else { return nil }

        let launcherType = launcherInfo.st_mode & mode_t(S_IFMT)
        if launcherType == mode_t(S_IFLNK) {
            guard trustedOwner(launcherInfo.st_uid) else { return nil }
        } else {
            guard launcherType == mode_t(S_IFREG),
                  trustedOwner(launcherInfo.st_uid),
                  noSharedWritePermission(launcherInfo.st_mode),
                  manager.isExecutableFile(atPath: launcher.path)
            else { return nil }
        }

        guard let targetPath = realPath(launcher.path) else { return nil }
        let target = URL(fileURLWithPath: targetPath).standardizedFileURL
        let targetParent = target.deletingLastPathComponent()
        guard trustedDirectoryChain(targetParent),
              let targetInfo = lstatEntry(at: target.path),
              targetInfo.st_mode & mode_t(S_IFMT) == mode_t(S_IFREG),
              trustedOwner(targetInfo.st_uid),
              noSharedWritePermission(targetInfo.st_mode),
              noDangerousExtendedACL(at: target.path),
              manager.isExecutableFile(atPath: target.path)
        else { return nil }
        return target
    }

    private static func trustedDirectoryChain(_ url: URL) -> Bool {
        guard let physicalPath = realPath(url.path) else { return false }
        let components = URL(fileURLWithPath: physicalPath).pathComponents
        var currentPath = "/"
        guard let rootInfo = lstatEntry(at: currentPath),
              rootInfo.st_mode & mode_t(S_IFMT) == mode_t(S_IFDIR),
              trustedOwner(rootInfo.st_uid),
              noSharedWritePermission(rootInfo.st_mode),
              noDangerousExtendedACL(at: currentPath)
        else { return false }

        for index in 1..<components.count {
            let component = components[index]
            currentPath = currentPath == "/" ? "/\(component)" : "\(currentPath)/\(component)"
            guard let info = lstatEntry(at: currentPath),
                  info.st_mode & mode_t(S_IFMT) == mode_t(S_IFDIR),
                  trustedOwner(info.st_uid),
                  noDangerousExtendedACL(at: currentPath)
            else { return false }

            if !noSharedWritePermission(info.st_mode) {
                guard index + 1 < components.count,
                      isProtectedTemporaryDirectory(
                        currentPath,
                        owner: info.st_uid,
                        mode: info.st_mode,
                        child: components[index + 1]
                      )
                else { return false }
            }
        }
        return true
    }

    private static func isProtectedTemporaryDirectory(
        _ path: String,
        owner: uid_t,
        mode: mode_t,
        child: String
    ) -> Bool {
        let temporaryPaths: Set<String> = ["/tmp", "/var/tmp", "/private/tmp", "/private/var/tmp"]
        guard owner == 0,
              mode & mode_t(0o1000) != 0,
              temporaryPaths.contains(path)
        else { return false }

        let childPath = path == "/" ? "/\(child)" : "\(path)/\(child)"
        guard let childInfo = lstatEntry(at: childPath),
              childInfo.st_mode & mode_t(S_IFMT) == mode_t(S_IFDIR),
              childInfo.st_uid == getuid(),
              noSharedWritePermission(childInfo.st_mode)
        else { return false }
        return true
    }

    private static func noSharedWritePermission(_ mode: mode_t) -> Bool {
        mode & mode_t(0o022) == 0
    }

    private static func noDangerousExtendedACL(at path: String) -> Bool {
        errno = 0
        guard let acl = path.withCString({ acl_get_file($0, ACL_TYPE_EXTENDED) }) else {
            // On macOS, ENOENT means the object has no extended ACL. Any
            // inability to inspect ACLs fails closed.
            return errno == ENOENT
        }
        defer { acl_free(UnsafeMutableRawPointer(acl)) }

        let writablePermissions: acl_permset_mask_t =
            acl_permset_mask_t(ACL_WRITE_DATA.rawValue)
            | acl_permset_mask_t(ACL_ADD_FILE.rawValue)
            | acl_permset_mask_t(ACL_DELETE.rawValue)
            | acl_permset_mask_t(ACL_DELETE_CHILD.rawValue)
            | acl_permset_mask_t(ACL_APPEND_DATA.rawValue)
            | acl_permset_mask_t(ACL_ADD_SUBDIRECTORY.rawValue)
            | acl_permset_mask_t(ACL_WRITE_ATTRIBUTES.rawValue)
            | acl_permset_mask_t(ACL_WRITE_EXTATTRIBUTES.rawValue)
            | acl_permset_mask_t(ACL_WRITE_SECURITY.rawValue)
            | acl_permset_mask_t(ACL_CHANGE_OWNER.rawValue)

        var entryID = Int32(ACL_FIRST_ENTRY.rawValue)
        while true {
            var entry: acl_entry_t?
            let result = acl_get_entry(acl, entryID, &entry)
            guard result == 0 else {
                // acl_get_entry uses EINVAL to signal that there are no more
                // entries after the final one has been returned.
                return errno == EINVAL
            }
            guard let entry else { return false }
            var tag = ACL_UNDEFINED_TAG
            guard acl_get_tag_type(entry, &tag) == 0 else { return false }
            if tag == ACL_EXTENDED_ALLOW {
                var permissions: acl_permset_mask_t = 0
                guard acl_get_permset_mask_np(entry, &permissions) == 0 else { return false }
                if permissions & writablePermissions != 0 { return false }
            } else if tag != ACL_EXTENDED_DENY {
                return false
            }
            entryID = Int32(ACL_NEXT_ENTRY.rawValue)
        }
    }

    private static func trustedOwner(_ owner: uid_t) -> Bool {
        owner == 0 || owner == getuid()
    }

    private static func lstatEntry(at path: String) -> Darwin.stat? {
        var info = Darwin.stat()
        let result = path.withCString { Darwin.lstat($0, &info) }
        return result == 0 ? info : nil
    }

    private static func realPath(_ path: String) -> String? {
        path.withCString { source in
            guard let resolved = Darwin.realpath(source, nil) else { return nil }
            defer { free(resolved) }
            return String(cString: resolved)
        }
    }
}
