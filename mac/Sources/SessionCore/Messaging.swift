import Foundation

public struct Terminal: Equatable {
    public let workspace: String
    public let title: String
    public let surface: String
    public let tty: String
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
}

public enum Messaging {
    /// Terminal panes per workspace from `cmux tree --json`.
    public static func terminals(_ tree: Data) -> [Terminal] {
        guard let root = try? JSONSerialization.jsonObject(with: tree) else { return [] }
        var out: [Terminal] = []
        func walk(_ o: Any, _ ws: (String, String)?) {
            if let d = o as? [String: Any] {
                var ws = ws
                if let ref = d["ref"] as? String, ref.hasPrefix("workspace:") { ws = (ref, d["title"] as? String ?? ref) }
                if d["type"] as? String == "terminal", let ref = d["ref"] as? String, let tty = d["tty"] as? String,
                   let ws, !out.contains(where: { $0.surface == ref }) {
                    out.append(Terminal(workspace: ws.0, title: ws.1, surface: ref, tty: tty))
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
                      let text = Transcript.humanText(r), Transcript.letters(text) == want else { continue }
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
                      command.contains(sendCommand) else { continue }
                let args = shellWords(command)
                if let i = args.firstIndex(of: "--send"), i + 2 < args.count {
                    return .sent(workspace: args[i + 1], message: args[i + 2])
                }
                if let i = args.firstIndex(of: "--wait"), i + 1 < args.count {
                    return .note("Waiting on \(args[i + 1])…")
                }
            }
        }
        if r["type"] as? String == "user" {
            for b in blocks where b["type"] as? String == "tool_result" {
                let content = b["content"]
                let text = (content as? String)
                    ?? (content as? [[String: Any]])?.compactMap { $0["text"] as? String }.joined(separator: "\n") ?? ""
                if text.hasPrefix("REPLY") { return .replied(String(text.drop { $0 != "\n" }.dropFirst())) }
            }
        }
        return nil
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
            } else if c == " " {
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
