import Foundation

/// Where the chat's `claude` runs and what it may touch.
public enum ClaudeSandbox {
    /// The only folder Read, Grep, and Glob may look in: where Claude Code keeps session transcripts.
    public static var projectsRoot: String { NSHomeDirectory() + "/.claude/projects" }

    static let missingBinary = "Couldn't find the claude command. Install Claude Code, or set ASSISTANT_CLAUDE to its path."

    /// The `claude` binary the operator's alias runs, resolved once: `ASSISTANT_CLAUDE`, then the login shell's
    /// `whence -p claude` (which skips aliases), then the usual install folders.
    public static let binary: String? = resolve(environment: ProcessInfo.processInfo.environment, whence: loginShellWhence,
                                                isExecutable: FileManager.default.isExecutableFile(atPath:))

    static func resolve(environment: [String: String], whence: () -> String?, isExecutable: (String) -> Bool) -> String? {
        let home = environment["HOME"] ?? NSHomeDirectory()
        let candidates = [environment["ASSISTANT_CLAUDE"], whence(), home + "/.local/bin/claude", "/opt/homebrew/bin/claude",
                          "/usr/local/bin/claude"]
        return candidates.compactMap { $0?.trimmingCharacters(in: .whitespacesAndNewlines) }
            .first { $0.hasPrefix("/") && isExecutable($0) }
    }

    private static func loginShellWhence() -> String? {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/zsh")
        p.arguments = ["-lc", "whence -p claude"]
        let out = Pipe()
        p.standardOutput = out
        p.standardError = FileHandle.nullDevice
        guard (try? p.run()) != nil else { return nil }
        let data = out.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        return String(decoding: data, as: UTF8.self).split(whereSeparator: \.isNewline).first.map(String.init)
    }

    /// `--settings` for the chat: a PreToolUse hook on every tool that runs Assistant itself as the guard.
    public static func guardSettings(tool: String) -> String {
        let command = "'" + tool.replacingOccurrences(of: "'", with: "'\\''") + "' --pretooluse-guard"
        let settings: [String: Any] = ["hooks": ["PreToolUse": [["matcher": "*", "hooks": [["type": "command", "command": command]]]]]]
        let data = try! JSONSerialization.data(withJSONObject: settings, options: [.sortedKeys, .withoutEscapingSlashes])
        return String(decoding: data, as: UTF8.self)
    }
}

/// The PreToolUse decision for the chat's Claude. It allows only Assistant's own commands, in their exact shapes, and
/// reads under the transcripts folder. Anything it allows still has to pass Claude Code's normal permission check.
public enum ToolGuard {
    public enum Decision: Equatable {
        case allow
        case deny(String)
    }

    /// Decides from the hook's stdin JSON (`tool_name`, `tool_input`, `cwd`).
    public static func decide(hookInput: Data, tool: String, root: String = ClaudeSandbox.projectsRoot) -> Decision {
        guard let event = (try? JSONSerialization.jsonObject(with: hookInput)) as? [String: Any],
              let name = event["tool_name"] as? String else { return .deny("unreadable tool call") }
        let input = event["tool_input"] as? [String: Any] ?? [:]
        let cwd = event["cwd"] as? String ?? root
        return decide(toolName: name, input: input, cwd: cwd, tool: tool, root: root)
    }

    public static func decide(toolName: String, input: [String: Any], cwd: String, tool: String, root: String) -> Decision {
        switch toolName {
        case "Bash":
            return bash(input["command"] as? String ?? "", tool: tool)
        case "Read":
            guard let path = input["file_path"] as? String, inside(path, cwd: cwd, root: root) else { return .deny(outside(root)) }
            return .allow
        case "Grep":
            guard inside(input["path"] as? String ?? cwd, cwd: cwd, root: root), safePattern(input["glob"] as? String) else {
                return .deny(outside(root))
            }
            return .allow
        case "Glob":
            guard inside(input["path"] as? String ?? cwd, cwd: cwd, root: root), safePattern(input["pattern"] as? String) else {
                return .deny(outside(root))
            }
            return .allow
        default:
            return .deny("\(toolName) is not available in Assistant's chat.")
        }
    }

    /// The hook's stdout: nothing for allow (the normal permission check still runs), a deny decision otherwise.
    public static func hookOutput(_ decision: Decision) -> String {
        guard case .deny(let reason) = decision else { return "" }
        let output: [String: Any] = ["hookSpecificOutput": ["hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                            "permissionDecisionReason": reason]]
        let data = try! JSONSerialization.data(withJSONObject: output, options: [.sortedKeys])
        return String(decoding: data, as: UTF8.self)
    }

    static func outside(_ root: String) -> String { "Read, Grep, and Glob work only under \(root)." }

    static let onlyOwnCommands = "Bash runs only Assistant's own --sessions, --send, --wait, --search, and --memory, "
        + "exactly as the instructions show them, with no other commands, pipes, or redirection."

    static func bash(_ command: String, tool: String) -> Decision {
        guard isPlain(command) else { return .deny(onlyOwnCommands) }
        let words = Messaging.shellWords(command.trimmingCharacters(in: .whitespaces))
        guard words.first == tool, words.count >= 2 else { return .deny(onlyOwnCommands) }
        let args = Array(words.dropFirst())
        let ok: Bool
        switch args[0] {
        case "--sessions": ok = args.count == 1
        case "--send": ok = args.count == 3 && isRef(args[1]) && !args[2].isEmpty
        case "--wait": ok = args.count == 2 && isRef(args[1])
        case "--search", "--memory": ok = args.count == 2 && isJSONObject(args[1])
        default: ok = false
        }
        return ok ? .allow : .deny(onlyOwnCommands)
    }

    /// True when the command is only words and quoted strings: no operators, redirection, substitution, globbing,
    /// comments, escapes outside quotes, or line breaks. Inside double quotes only `\"` and `\\` escapes are allowed.
    static func isPlain(_ command: String) -> Bool {
        var quote: Character?
        var escaped = false
        for c in command {
            if c.isNewline || c == "\0" { return false }
            if escaped {
                guard c == "\"" || c == "\\" else { return false }
                escaped = false
                continue
            }
            switch quote {
            case "'":
                if c == "'" { quote = nil }
            case "\"":
                if c == "\"" { quote = nil } else if c == "\\" { escaped = true } else if "$`!".contains(c) { return false }
            default:
                if c == "'" || c == "\"" { quote = c } else if ";&|<>()`$*?[]{}~#!\\".contains(c) { return false }
            }
        }
        return quote == nil && !escaped
    }

    static func isRef(_ word: String) -> Bool {
        !word.hasPrefix("-") && !word.isEmpty && word.allSatisfy { $0.isASCII && ($0.isLetter || $0.isNumber || ":_.-".contains($0)) }
    }

    static func isJSONObject(_ word: String) -> Bool {
        (try? JSONSerialization.jsonObject(with: Data(word.utf8))) is [String: Any]
    }

    static func safePattern(_ pattern: String?) -> Bool {
        guard let pattern else { return true }
        return !pattern.hasPrefix("/") && !pattern.hasPrefix("~") && !pattern.contains("..")
    }

    /// `path`, made absolute against `cwd` with symlinks resolved, is `root` or inside it. A path with a `..` part is
    /// refused: after a symlink, the kernel resolves `..` differently from the text.
    static func inside(_ path: String, cwd: String, root: String) -> Bool {
        guard !path.isEmpty, !path.hasPrefix("~"), !path.split(separator: "/").contains("..") else { return false }
        let absolute = path.hasPrefix("/") ? path : cwd + "/" + path
        let resolved = real(absolute)
        let base = real(root)
        return resolved == base || resolved.hasPrefix(base + "/")
    }

    /// `path` with `.` and `..` removed and every symlink resolved, including links in front of parts that don't
    /// exist yet: the longest existing prefix goes through realpath and the rest is appended.
    static func real(_ path: String) -> String {
        var parts: [String] = []
        for part in path.split(separator: "/", omittingEmptySubsequences: true) {
            if part == "." { continue }
            if part == ".." { _ = parts.popLast() } else { parts.append(String(part)) }
        }
        var rest: [String] = []
        while true {
            let prefix = "/" + parts.joined(separator: "/")
            if let resolved = realpath(prefix, nil) {
                defer { free(resolved) }
                let head = String(cString: resolved)
                return rest.isEmpty ? head : (head == "/" ? "" : head) + "/" + rest.reversed().joined(separator: "/")
            }
            guard let last = parts.popLast() else { return "/" + rest.reversed().joined(separator: "/") }
            rest.append(last)
        }
    }
}

/// Assistant's command line. A command is recognized only as the first argument, with its exact argument count, so a
/// command the chat's guard allowed can't smuggle another one (`--search '{}' --render x /tmp/f`).
public enum AppCommand: Equatable {
    case app
    case print
    case sessions
    case send(ref: String, message: String, timeout: TimeInterval?)
    case wait(ref: String, timeout: TimeInterval?)
    case search(String)
    case memory(String)
    case render(text: String, file: String)
    case transcribe(file: String, legacy: Bool)
    case ask(question: String, session: String?, spoken: Bool, sayTo: String?)
    case compress(session: String?, focus: String?)
    case toolGuard

    public static let usage = """
    Usage: AssistantBar [--show] [--listen]
           AssistantBar --print | --sessions
           AssistantBar --send <workspace> "<message>" [--timeout <seconds>] | --wait <workspace> [--timeout <seconds>]
           AssistantBar --ask "<question>" [--session <name>] [--spoken] [--say-to <file.wav>]
           AssistantBar --compress [--session <name>] [--focus "<topic>"]
           AssistantBar --search '<json>' | --memory '<json>' | --render "<text>" <file.wav>
           AssistantBar --transcribe <file.wav> [--legacy]
    """

    /// nil for anything that isn't exactly one of the forms in `usage`.
    public static func parse(_ args: [String]) -> AppCommand? {
        let rest = Array(args.dropFirst())
        guard let first = rest.first else { return .app }
        let tail = Array(rest.dropFirst())
        switch first {
        case "--show", "--listen":
            return Set(rest).isSubset(of: ["--show", "--listen"]) && Set(rest).count == rest.count ? .app : nil
        case "--print": return tail.isEmpty ? .print : nil
        case "--sessions": return tail.isEmpty ? .sessions : nil
        case "--pretooluse-guard": return tail.isEmpty ? .toolGuard : nil
        case "--search": return tail.count == 1 ? .search(tail[0]) : nil
        case "--memory": return tail.count == 1 ? .memory(tail[0]) : nil
        case "--render": return tail.count == 2 ? .render(text: tail[0], file: tail[1]) : nil
        case "--transcribe":
            guard let file = tail.first, let options = options(Array(tail.dropFirst()), values: [], flags: ["--legacy"]) else {
                return nil
            }
            return .transcribe(file: file, legacy: options.keys.contains("--legacy"))
        case "--send":
            guard tail.count >= 2, let timeout = timeout(Array(tail.dropFirst(2))) else { return nil }
            return .send(ref: tail[0], message: tail[1], timeout: timeout.value)
        case "--wait":
            guard tail.count >= 1, let timeout = timeout(Array(tail.dropFirst())) else { return nil }
            return .wait(ref: tail[0], timeout: timeout.value)
        case "--ask":
            guard let question = tail.first,
                  let options = options(Array(tail.dropFirst()), values: ["--session", "--say-to"], flags: ["--spoken"]) else {
                return nil
            }
            return .ask(question: question, session: options["--session"] ?? nil, spoken: options.keys.contains("--spoken"),
                        sayTo: options["--say-to"] ?? nil)
        case "--compress":
            guard let options = options(tail, values: ["--session", "--focus"], flags: []) else { return nil }
            return .compress(session: options["--session"] ?? nil, focus: options["--focus"] ?? nil)
        default:
            return nil
        }
    }

    private struct Timeout { let value: TimeInterval? }

    private static func timeout(_ rest: [String]) -> Timeout? {
        if rest.isEmpty { return Timeout(value: nil) }
        guard rest.count == 2, rest[0] == "--timeout", let seconds = TimeInterval(rest[1]), seconds > 0 else { return nil }
        return Timeout(value: seconds)
    }

    /// Each option at most once; a value option takes the next argument.
    private static func options(_ rest: [String], values: Set<String>, flags: Set<String>) -> [String: String?]? {
        var out: [String: String?] = [:]
        var i = 0
        while i < rest.count {
            let name = rest[i]
            guard out[name] == nil else { return nil }
            if flags.contains(name) {
                out[name] = .some(nil)
                i += 1
            } else if values.contains(name), i + 1 < rest.count {
                out[name] = rest[i + 1]
                i += 2
            } else {
                return nil
            }
        }
        return out
    }
}
