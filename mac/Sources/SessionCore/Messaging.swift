import Foundation

public struct Terminal: Equatable {
    public let workspace: String
    public let title: String
    public let surface: String
    public let tty: String
    /// The workspace's id, which a back-off entry can name instead of its ref.
    public var workspaceID: String? = nil
}

public struct TurnState: Equatable {
    public var delivered = false
    public var finished = false
    public var replies: [String] = []
}

public enum ChatStep: Equatable {
    case sent(workspace: String, message: String)
    case replied(String)
    case note(String)

    /// The line the panel and the terminal show for the step.
    public var text: String {
        switch self {
        case let .sent(workspace, message): "Sent to \(workspace): \(message)"
        case let .replied(text): "Reply: " + String(text.prefix(300))
        case let .note(text): text
        }
    }
}

public enum Messaging {
    /// Terminal panes per workspace from `cmux tree --json`.
    public static func terminals(_ tree: Data) -> [Terminal] {
        guard let root = try? JSONSerialization.jsonObject(with: tree) else { return [] }
        var out: [Terminal] = []
        func walk(_ o: Any, _ ws: (String, String, String?)?) {
            if let d = o as? [String: Any] {
                var ws = ws
                if let ref = d["ref"] as? String, ref.hasPrefix("workspace:") { ws = (ref, d["title"] as? String ?? ref, d["id"] as? String) }
                if d["type"] as? String == "terminal", let ref = d["ref"] as? String, let tty = d["tty"] as? String,
                   let ws, !out.contains(where: { $0.surface == ref }) {
                    out.append(Terminal(workspace: ws.0, title: ws.1, surface: ref, tty: tty, workspaceID: ws.2))
                }
                d.values.forEach { walk($0, ws) }
            } else if let a = o as? [Any] {
                a.forEach { walk($0, ws) }
            }
        }
        walk(root, nil)
        return out
    }

    public struct Session: Equatable {
        public let id: String
        public let pid: Int32
    }

    /// Claude sessions by terminal, from `ps -A -o pid=,tty=,command=`.
    public static func claudeSessions(_ ps: String) -> [String: Session] {
        var out: [String: Session] = [:]
        for line in ps.split(separator: "\n") {
            let parts = line.split(separator: " ", omittingEmptySubsequences: true).map(String.init)
            guard parts.count > 4, let pid = Int32(parts[0]), parts[1] != "??",
                  parts[2].hasSuffix("/claude") || parts[2] == "claude" else { continue }
            for flag in ["--session-id", "--resume"] {
                if let i = parts.firstIndex(of: flag), i + 1 < parts.count, UUID(uuidString: parts[i + 1]) != nil {
                    out[parts[1]] = Session(id: parts[i + 1], pid: pid)
                }
            }
        }
        return out
    }

    /// The first character that counts in a message: leading whitespace (including no-break space), control
    /// characters, and invisible format characters (zero-width space, BOM) are skipped, since `cmux send --paste`
    /// turns control characters into spaces and Claude Code ignores leading blanks.
    static func firstVisible(_ message: String) -> Character? {
        let skipped = message.unicodeScalars.drop { scalar in
            scalar.properties.isWhitespace || [.control, .format].contains(scalar.properties.generalCategory)
        }
        return String(String.UnicodeScalarView(skipped)).first
    }

    /// Why a message can't be sent into another session, or nil when it can. A message starting with "/" runs as a
    /// slash command there (/clear, /cleanup, /merge-when-ready…), one starting with "!" runs in its shell, and one
    /// starting with "#" can save a standing memory, all with that session's permissions. Plain requests are fine:
    /// the session's own permission checks cover them.
    public static func sendRefusal(_ message: String) -> String? {
        switch firstVisible(message) {
        case "#": return "Messages can't start with \"#\": that can save a standing instruction to the session's memory. Ask in plain words, or ask the operator."
        case "/": return "Messages can't start with \"/\": that would run a slash command in the session. Describe what you want in plain words, or ask the operator to run the command."
        case "!": return "Messages can't start with \"!\": that would run a shell command in the session. Ask in plain words, or ask the operator."
        case nil: return "The message is empty."
        default: return nil
        }
    }

    /// What `--send` says when Enter was pressed but the transcript doesn't show the message: it may have gone in.
    public static func unconfirmed(_ workspace: String) -> String {
        "NOT CONFIRMED\nThe message was pasted into \(workspace) and Enter was pressed, but it doesn't show in the session's "
            + "transcript yet. Don't send it again. Run --wait \(workspace) to see whether the session is working on it."
    }

    /// `cmux send` arguments that paste `message` into a pane; `--` keeps a message starting with "-" from being read
    /// as a flag.
    public static func pasteArguments(workspace: String, surface: String, message: String) -> [String] {
        ["send", "--workspace", workspace, "--surface", surface, "--paste", "--", message]
    }

    /// Whether the operator's back-off list names this workspace: by the name it was asked for, its resolved ref, its
    /// id, or its title.
    public static func backedOff(_ list: [Any], names: [String]) -> Bool {
        let names = Set(names.filter { !$0.isEmpty })
        return list.contains { item in
            if let s = item as? String { return names.contains(s) }
            if let o = item as? [String: Any] { return o.values.contains { ($0 as? String).map(names.contains) ?? false } }
            return false
        }
    }

    /// A transcript in the session's project folder that might be the one it writes after /clear.
    public struct TranscriptCandidate: Equatable {
        public let url: URL
        public let created: Date
        public let modified: Date
        /// The working directory its first entries record.
        public let cwd: String?
        /// Its first entries hold the /clear command, as a transcript started by /clear does.
        public let startsWithClear: Bool

        public init(url: URL, created: Date, modified: Date, cwd: String?, startsWithClear: Bool) {
            self.url = url
            self.created = created
            self.modified = modified
            self.cwd = cwd
            self.startsWithClear = startsWithClear
        }
    }

    public struct LiveTranscript: Equatable {
        public let url: URL
        /// Set when there may have been a /clear but the new transcript can't be told apart from another session's.
        public let note: String?

        public init(url: URL, note: String?) {
            self.url = url
            self.note = note
        }
    }

    public static let ambiguousTranscriptNote = "This session may have been cleared, and its new transcript can't be told "
        + "apart from another session's in the same folder, so this uses the transcript its process names. Its reply may be old."

    /// The transcript a session writes now. After /clear Claude writes to a new file, but the process arguments still
    /// name the old one. Switch only on evidence: a file in the same folder whose first entries are the /clear
    /// command in this session's working directory, created after this process started and after the current file
    /// stopped growing, and not named by any other live `claude` process. If another live `claude` process works in
    /// the same directory (it could own a file it doesn't name), or two files that fit overlap in time, keep the
    /// current file and say so. Follows a session cleared more than once, one file after another.
    public static func liveTranscript(named: URL, namedModified: Date?, cwd: String, processStart: Date,
                                      candidates: [TranscriptCandidate], ownedByOthers: Set<String>,
                                      othersInSameDirectory: Bool) -> LiveTranscript {
        var current = named
        var stopped = namedModified ?? .distantPast
        var seen: Set<URL> = [named]
        while true {
            let next = candidates.filter { c in
                !seen.contains(c.url) && c.startsWithClear && c.cwd == cwd && c.created >= processStart
                    && c.created >= stopped.addingTimeInterval(-2)
                    && !ownedByOthers.contains(c.url.deletingPathExtension().lastPathComponent)
            }
            let ordered = next.sorted { $0.created < $1.created }
            guard let first = ordered.first else { return LiveTranscript(url: current, note: nil) }
            // A later file is a second /clear only if it began after this one stopped growing; overlapping files
            // are two sessions.
            let overlaps = ordered.dropFirst().contains { $0.created < first.modified.addingTimeInterval(-2) }
            guard !overlaps, !othersInSameDirectory else {
                return LiveTranscript(url: current, note: ambiguousTranscriptNote)
            }
            current = first.url
            stopped = first.modified
            seen.insert(first.url)
        }
    }

    /// Every live `claude` process, with the session ID its arguments name, if any: interactive sessions with or
    /// without an ID, and headless `claude -p` runs with no terminal.
    public static func claudeProcesses(_ ps: String) -> [(pid: Int32, sessionID: String?)] {
        ps.split(separator: "\n").compactMap { line in
            let parts = line.split(separator: " ", omittingEmptySubsequences: true).map(String.init)
            guard parts.count > 2, let pid = Int32(parts[0]), parts[2].hasSuffix("/claude") || parts[2] == "claude" else { return nil }
            let id = ["--session-id", "--resume"].lazy.compactMap { flag -> String? in
                guard let i = parts.firstIndex(of: flag), i + 1 < parts.count, UUID(uuidString: parts[i + 1]) != nil else { return nil }
                return parts[i + 1]
            }.first
            return (pid, id)
        }
    }

    /// Reads a candidate's first entries: the working directory they record and whether they hold /clear.
    public static func transcriptStart(_ data: Data) -> (cwd: String?, startsWithClear: Bool) {
        var cwd: String?
        var clear = false
        for line in Transcript.lines(data).prefix(12) {
            guard let r = Transcript.record(line) else { continue }
            if cwd == nil { cwd = r["cwd"] as? String }
            if r["type"] as? String == "user", let text = Transcript.humanText(r, skipping: false),
               text.contains("<command-name>/clear</command-name>") { clear = true }
        }
        return (cwd, clear)
    }

    /// The ~/.claude/projects folder name Claude uses for a working directory.
    public static func projectFolder(_ cwd: String) -> String {
        String(cwd.map { $0.isASCII && ($0.isLetter || $0.isNumber) ? $0 : "-" })
    }

    /// Where a session's reply stands after the operator-side message `sent` went in at `since`.
    public static func turn(_ data: Data, sent: String, since: Date) -> TurnState {
        var state = TurnState()
        let want = Transcript.letters(sent)
        for line in Transcript.lines(data) {
            guard let r = Transcript.record(line) else { continue }
            if !state.delivered {
                guard let t = r["timestamp"] as? String, let at = Timestamps.parse(t), at >= since.addingTimeInterval(-5),
                      let text = Transcript.humanText(r, skipping: false), Transcript.letters(text) == want else { continue }
                state.delivered = true
                continue
            }
            if let text = Transcript.assistantText(r) {
                state.replies.append(text)
                state.finished = false
            }
            if r["type"] as? String == "user", Transcript.humanText(r) != nil { state.finished = false }
            if r["type"] as? String == "system", r["subtype"] as? String == "turn_duration" { state.finished = true }
        }
        return state
    }

    /// One line of `claude -p --output-format stream-json` as a step to show the operator.
    public static func step(_ line: String, sendCommand: String) -> ChatStep? {
        guard let r = (try? JSONSerialization.jsonObject(with: Data(line.utf8))) as? [String: Any] else { return nil }
        let blocks = (r["message"] as? [String: Any])?["content"] as? [[String: Any]] ?? []
        if r["type"] as? String == "assistant" {
            for b in blocks where b["type"] as? String == "tool_use" {
                guard let command = (b["input"] as? [String: Any])?["command"] as? String,
                      let step = step(command: command, sendCommand: sendCommand) else { continue }
                return step
            }
        }
        if r["type"] as? String == "user" {
            for b in blocks where b["type"] as? String == "tool_result" {
                let content = b["content"]
                let text = (content as? String)
                    ?? (content as? [[String: Any]])?.compactMap { $0["text"] as? String }.joined(separator: "\n") ?? ""
                if text.hasPrefix("REPLY") { return .replied(replyBody(text)) }
            }
        }
        return nil
    }

    /// The step a Bash command of Assistant's own shows, or nil for any other command.
    public static func step(command: String, sendCommand: String) -> ChatStep? {
        guard command.contains(sendCommand) else { return nil }
        let args = shellWords(command)
        if let i = args.firstIndex(of: "--send"), i + 2 < args.count {
            return .sent(workspace: args[i + 1], message: args[i + 2])
        }
        if let i = args.firstIndex(of: "--wait"), i + 1 < args.count {
            return .note("Waiting on \(args[i + 1])…")
        }
        if args.contains("--search") { return .note("Searching earlier chats…") }
        if args.contains("--memory") { return .note("Updating memory…") }
        return nil
    }

    /// A `--send` result without its first line, which is the REPLY marker.
    public static func replyBody(_ text: String) -> String { String(text.drop { $0 != "\n" }.dropFirst()) }

    /// Splits a shell command into words, honoring single and double quotes.
    public static func shellWords(_ command: String) -> [String] {
        var words: [String] = []
        var current = ""
        var quote: Character?
        var inWord = false
        var escaped = false
        for c in command {
            if escaped { current.append(c); escaped = false; continue }
            if c == "\\" && quote != "'" { escaped = true; inWord = true; continue }
            if let q = quote {
                if c == q { quote = nil } else { current.append(c) }
            } else if c == "\"" || c == "'" {
                quote = c
                inWord = true
            } else if c == " " || c == "\t" {
                if inWord { words.append(current); current = ""; inWord = false }
            } else {
                current.append(c)
                inWord = true
            }
        }
        if inWord { words.append(current) }
        return words
    }
}
