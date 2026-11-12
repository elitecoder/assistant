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
            // With both flags, Claude Code writes the --session-id transcript (checked on a live session); --resume
            // names the conversation it was started from.
            if let id = ["--session-id", "--resume"].lazy.compactMap({ flag -> String? in
                guard let i = parts.firstIndex(of: flag), i + 1 < parts.count, UUID(uuidString: parts[i + 1]) != nil else { return nil }
                return parts[i + 1]
            }).first {
                out[parts[1]] = Session(id: id, pid: pid)
            }
        }
        return out
    }

    /// Besides letters and digits, the only scalars a message may start with.
    static let allowedFirst: Set<Unicode.Scalar> = ["\"", "'", "(", "@"]
    /// How many scalars at the start may hold no combining mark or invisible character.
    static let cleanStart = 3

    static func isLetterOrDigit(_ scalar: Unicode.Scalar) -> Bool {
        switch scalar.properties.generalCategory {
        case .uppercaseLetter, .lowercaseLetter, .titlecaseLetter, .modifierLetter, .otherLetter,
             .decimalNumber, .letterNumber, .otherNumber: true
        default: false
        }
    }

    /// Combining marks (Mn, Me, which include U+FE0F and U+034F) and invisible format characters (Cf, which include
    /// U+200D).
    static func hidesSomething(_ scalar: Unicode.Scalar) -> Bool {
        [.nonspacingMark, .enclosingMark, .format].contains(scalar.properties.generalCategory)
    }

    /// Why a message can't be sent into another session, or nil when it can. A message starting with "/" runs as a
    /// slash command there (/clear, /cleanup, /merge-when-ready…), one starting with "!" runs in its shell, and one
    /// starting with "#" can save a standing memory, all with that session's permissions. Policy: the operator accepts
    /// plain-language messages sent without asking them first; only plain-language starts are allowed. Working on
    /// Unicode scalars after leading whitespace, the first must be a letter or digit, or one of `" ' ( @`, and none of
    /// the first three may be a combining mark or an invisible character that could join with or hide a prefix.
    public static func sendRefusal(_ message: String) -> String? {
        let start = Array(message.unicodeScalars.drop { $0.properties.isWhitespace }.prefix(cleanStart))
        guard let first = start.first else { return "The message is empty." }
        switch first {
        case "#": return "Messages can't start with \"#\": that can save a standing instruction to the session's memory. Ask in plain words, or ask the operator."
        case "/": return "Messages can't start with \"/\": that would run a slash command in the session. Describe what you want in plain words, or ask the operator to run the command."
        case "!": return "Messages can't start with \"!\": that would run a shell command in the session. Ask in plain words, or ask the operator."
        default: break
        }
        guard isLetterOrDigit(first) || allowedFirst.contains(first) else {
            return "Messages must start with a letter, a digit, or one of \" ' ( @. Write it in plain words."
        }
        if start.contains(where: hidesSomething) {
            return "Messages can't have combining marks or invisible characters in their first three characters. Write it in plain words."
        }
        return nil
    }

    /// How long one `--send` or `--wait` call waits in all, kept under the 120-second default timeout of Claude Code's
    /// Bash tool, so the chat's Claude always gets an answer from the call instead of a timeout.
    public static let callLimit: TimeInterval = 100

    /// The first line `--send` and `--wait` print when the session got the message but hasn't finished replying.
    public static func stillWorking(_ workspace: String) -> String {
        "STILL WORKING: the message was delivered; run --wait \(workspace) to keep waiting. Don't send it again."
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
        /// Its first entries hold the /clear command, as a transcript started by /clear does.
        public let startsWithClear: Bool
        /// The process-level session IDs its entries carry (`session_id`) other than the file's own: after /clear,
        /// Claude Code keeps stamping the new file's entries with the ID the process was started with.
        public let linkedSessions: Set<String>

        public init(url: URL, created: Date, modified: Date, startsWithClear: Bool, linkedSessions: Set<String>) {
            self.url = url
            self.created = created
            self.modified = modified
            self.startsWithClear = startsWithClear
            self.linkedSessions = linkedSessions
        }

        var id: String { url.deletingPathExtension().lastPathComponent }
    }

    public struct LiveTranscript: Equatable {
        public let url: URL
        /// Set when there may have been a /clear but the new transcript can't be tied to this session.
        public let note: String?

        public init(url: URL, note: String?) {
            self.url = url
            self.note = note
        }
    }

    public static let ambiguousTranscriptNote = "This session may have been cleared, and no newer transcript can be tied "
        + "to it yet, so this uses the transcript its process names. Its reply may be old."

    public static let sharedSessionNote = "More than one running session was started from this conversation, so its "
        + "newer transcripts can't be told apart, and this uses the transcript its process names. Its reply may be old."

    /// The transcript a session writes now. After /clear Claude writes to a new file, but the process arguments still
    /// name the old one. Switch only to a file whose own entries name this session's process: they carry its session
    /// ID, or the ID of a file already followed (a session cleared more than once). A file whose entries name another
    /// session is someone else's, whether that session still runs or has exited, and is never followed. A file that
    /// starts with /clear after the current one stopped growing but doesn't name any session yet (a cleared session
    /// that hasn't had a prompt, another session in the folder, or this one after changing directory) can't be told
    /// apart: keep the current file and say so. A linked file is followed only once the current one stopped growing,
    /// and never when more than one live process claims this session's ID (two resumes of one conversation stamp the
    /// same ID), since then either could have written it.
    public static func liveTranscript(named: URL, sessionID: String, namedModified: Date?, processStart: Date,
                                      candidates: [TranscriptCandidate], others: [String?]) -> LiveTranscript {
        let ownedByOthers = Set(others.compactMap { $0 })
        let sharedID = ownedByOthers.contains(sessionID)
        var current = named
        var stopped = namedModified ?? .distantPast
        var chain: Set<String> = [sessionID, named.deletingPathExtension().lastPathComponent]
        var seen: Set<URL> = [named]
        while true {
            let pool = candidates.filter { !seen.contains($0.url) && $0.created >= processStart && !ownedByOthers.contains($0.id) }
            let linked = pool.filter { !$0.linkedSessions.isDisjoint(with: chain) }
            if sharedID && !linked.isEmpty { return LiveTranscript(url: current, note: sharedSessionNote) }
            if let next = linked.filter({ $0.created >= stopped.addingTimeInterval(-2) }).min(by: { $0.created < $1.created }) {
                current = next.url
                stopped = next.modified
                chain.insert(next.id)
                seen.insert(next.url)
                continue
            }
            // A linked file that began while the current one still grew can't be this session's clear, but names it.
            let unsure = !linked.isEmpty
                || pool.contains { $0.startsWithClear && $0.linkedSessions.isEmpty && $0.created >= stopped.addingTimeInterval(-2) }
            return LiveTranscript(url: current, note: unsure ? ambiguousTranscriptNote : nil)
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

    /// Reads a candidate's first entries: whether they hold /clear, and the process-level session IDs they carry
    /// other than the file's own.
    public static func transcriptStart(_ data: Data) -> (startsWithClear: Bool, linkedSessions: Set<String>) {
        var clear = false
        var own: Set<String> = []
        var stamped: Set<String> = []
        for (i, line) in Transcript.lines(data).prefix(400).enumerated() {
            guard let r = Transcript.record(line) else { continue }
            if let id = r["sessionId"] as? String { own.insert(id) }
            if let id = r["session_id"] as? String { stamped.insert(id) }
            if i < 12, r["type"] as? String == "user", let text = Transcript.humanText(r, skipping: false),
               text.contains("<command-name>/clear</command-name>") { clear = true }
        }
        return (clear, stamped.subtracting(own))
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
                if let step = sendResult(text) { return step }
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
        if let i = args.firstIndex(of: "--transcript"), i + 1 < args.count {
            return .note("Reading \(args[i + 1])'s conversation…")
        }
        if args.contains("--find") { return .note("Searching the sessions' transcripts…") }
        if args.contains("--search") { return .note("Searching earlier chats…") }
        if args.contains("--memory") { return .note("Updating memory…") }
        return nil
    }

    /// What a `--send` or `--wait` result shows in the panel: the reply, or why there isn't one. Every send's result
    /// shows, so the operator sees what was delivered and what wasn't.
    public static func sendResult(_ text: String) -> ChatStep? {
        let first = text.prefix { $0 != "\n" }
        let rest = replyBody(text).trimmingCharacters(in: .whitespacesAndNewlines)
        switch first {
        case "REPLY": return .replied(rest)
        case "NOT SENT": return .note("Not sent: " + rest)
        case "NOT CONFIRMED": return .note("Not confirmed: " + rest)
        default:
            guard first.hasPrefix("STILL WORKING") else { return nil }
            return .note("Delivered, no reply yet: " + String(rest.prefix(300)))
        }
    }

    /// A `--send` result without its first line, which is the REPLY marker.
    public static func replyBody(_ text: String) -> String {
        let lines = text.split(separator: "\n", omittingEmptySubsequences: false).dropFirst()
        guard let close = lines.firstIndex(where: { $0.hasPrefix("</\(Untrusted.tag) id=\"") }) else { return lines.joined(separator: "\n") }
        let block = Untrusted.unwrap(lines[...close].joined(separator: "\n"))
        let rest = lines[(close + 1)...].joined(separator: "\n")
        return rest.isEmpty ? block : block + "\n" + rest
    }

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

/// `--find`: a literal search over sessions' transcripts, for the chat's Claude instead of Grep.
public enum TranscriptSearch {
    public struct Query: Equatable {
        public let text: String
        public let workspace: String?
        public let limit: Int
    }

    /// `{"text": "...", "workspace": "workspace:3", "limit": 20}`; nil without text.
    public static func query(_ json: String) -> Query? {
        guard let d = (try? JSONSerialization.jsonObject(with: Data(json.utf8))) as? [String: Any],
              let text = (d["text"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines), !text.isEmpty else { return nil }
        let limit = min(50, max(1, d["limit"] as? Int ?? 20))
        return Query(text: text, workspace: d["workspace"] as? String, limit: limit)
    }

    /// The operator messages and Claude replies in one transcript that contain `text`, ignoring case, newest last,
    /// each clipped around the match.
    public static func matches(_ data: Data, text: String, limit: Int) -> [String] {
        var found: [String] = []
        for line in Transcript.lines(data) {
            guard let r = Transcript.record(line) else { continue }
            let pair: (String, String)? = Transcript.humanText(r).map { ("OPERATOR", $0) } ?? Transcript.assistantText(r).map { ("CLAUDE", $0) }
            guard let (who, body) = pair, let range = body.range(of: text, options: .caseInsensitive) else { continue }
            let start = body.index(range.lowerBound, offsetBy: -150, limitedBy: body.startIndex) ?? body.startIndex
            let end = body.index(range.upperBound, offsetBy: 150, limitedBy: body.endIndex) ?? body.endIndex
            let when = (r["timestamp"] as? String).map { " " + $0 } ?? ""
            found.append("\(who)\(when): " + (start > body.startIndex ? "…" : "") + body[start..<end] + (end < body.endIndex ? "…" : ""))
        }
        return Array(found.suffix(limit))
    }
}
