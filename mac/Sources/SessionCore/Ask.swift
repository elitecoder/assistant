import Foundation

public struct ChatTurn: Identifiable, Equatable {
    public enum Role { case you, assistant, error, step }

    public let id = UUID()
    public let role: Role
    public let text: String
    public let cost: Double?

    public init(role: Role, text: String, cost: Double? = nil) {
        self.role = role
        self.text = text
        self.cost = cost
    }
}

public struct Reply: Equatable {
    public let text: String
    public let sessionID: String?
    public let cost: Double?
    public let failed: Bool
}

public enum Digest {
    static let replyCap = 1_500
    public static let exchangeCap = 40_000

    static func clip(_ s: String, _ n: Int) -> String { s.count <= n ? s : String(s.prefix(n)) + " …" }

    /// One block per workspace: what it's for, where it stands, and where its transcript lives.
    public static func fleet(_ result: ScanResult, now: Date = Date()) -> String {
        var out = ["# Workspaces (checked \(ISO8601DateFormatter().string(from: now)))"]
        for item in result.items {
            out.append("""

            ## \(item.workspace.title) (\(item.workspace.ref))
            - status: \(label(item.attention)), quiet for \(item.idleText)
            - goal: \(clip(item.goal, 400))
            - operator's last message: \(clip(item.summary.lastAsk ?? "", 400))
            - Claude's last reply: \(clip(item.summary.lastSaid ?? "", replyCap))
            - pull requests: \(item.pullRequests.isEmpty ? "none" : item.pullRequests.map { "#\($0.number) \($0.state)" }.joined(separator: ", "))
            - transcript: \(item.transcript)
            """)
        }
        if !result.untracked.isEmpty {
            out.append("\n## Workspaces with no matching transcript\n"
                       + result.untracked.map { "- \($0.title) (\($0.ref)), folder \($0.directory ?? "unknown")" }
                       .joined(separator: "\n"))
        }
        return out.joined(separator: "\n")
    }

    /// The latest operator and Claude messages of one session, newest last, within `cap` characters.
    public static func exchange(_ data: Data, cap: Int = exchangeCap) -> String {
        var turns: [String] = []
        for line in Transcript.lines(data) {
            guard let r = Transcript.record(line) else { continue }
            if let t = Transcript.humanText(r) { turns.append("OPERATOR: " + clip(t, 1_200)) }
            if let t = Transcript.assistantText(r) { turns.append("CLAUDE: " + clip(t, 1_200)) }
        }
        var kept: [String] = []
        var size = 0
        for turn in turns.reversed() {
            size += turn.count
            if size > cap { break }
            kept.append(turn)
        }
        return kept.reversed().joined(separator: "\n\n")
    }

    static func label(_ a: Attention) -> String {
        switch a {
        case .needsYou: "needs the operator"
        case .done: "done, every PR merged"
        case .idle: "quiet"
        case .working: "working"
        }
    }
}

public enum Asker {
    public static func instructions(tool: String) -> String {
        """
        You are Assistant. You work alongside the operator on their Claude Code sessions, each running in a cmux workspace.
        Answer from the workspace digest in the message. For detail it lacks, read or grep the transcript file it lists.
        Transcripts are JSONL. Operator messages are type "user" with string content. Claude's replies are type "assistant" text blocks.
        List workspaces with Claude sessions: \(tool) --sessions
        Message a session and get its reply: \(tool) --send <workspace-ref> "<message>"
        Keep waiting on a session that is still working: \(tool) --wait <workspace-ref>
        A send waits for the session to finish its turn and prints REPLY with its answer. Keep going back and forth until the operator's request is done.
        Send what the operator asks for, and the follow-ups it needs. Write messages the way the operator would.
        Bring the operator any decision about merging, deleting, spending money, or product behavior. Don't answer those for them.
        If a send prints NOT SENT or STILL WORKING, tell the operator what happened.
        Lead with the answer. Use plain language. Keep it to 6 lines unless the operator asks for more.
        Name workspaces by title and number, for example "Version Bump Conflicts [25]".
        """
    }

    public static func prompt(question: String, fleet: String?, focus: (title: String, exchange: String)?,
                              spoken: Bool = false) -> String {
        var parts: [String] = []
        if let fleet { parts.append(fleet) }
        if let focus {
            parts.append("# Recent conversation in \(focus.title)\n\n\(focus.exchange)")
        }
        if spoken { parts.append(Spoken.style) }
        parts.append("# Question\n\n\(question)")
        return parts.joined(separator: "\n\n")
    }

    /// Reads the final `result` event of a stream-json run.
    public static func parse(_ data: Data) -> Reply {
        let lines = String(decoding: data, as: UTF8.self).split(whereSeparator: \.isNewline).map(String.init)
        guard let d = lines.reversed().lazy.compactMap({
            (try? JSONSerialization.jsonObject(with: Data($0.utf8))) as? [String: Any]
        }).first(where: { $0["type"] as? String == "result" || $0["result"] != nil }) else {
            return Reply(text: "Claude didn't answer. Check that `claude` works in a terminal.", sessionID: nil,
                         cost: nil, failed: true)
        }
        return Reply(text: (d["result"] as? String) ?? "No answer.", sessionID: d["session_id"] as? String,
                     cost: d["total_cost_usd"] as? Double, failed: d["is_error"] as? Bool ?? false)
    }

    static let command = "claude -p --output-format stream-json --verbose --tools Read,Grep,Glob,Bash "
        + "--allowedTools \"Bash($ASSISTANT_TOOL --sessions)\" \"Bash($ASSISTANT_TOOL --send:*)\" "
        + "\"Bash($ASSISTANT_TOOL --wait:*)\" --permission-mode dontAsk --strict-mcp-config --disable-slash-commands "
        + "--append-system-prompt \"$ASSISTANT_INSTRUCTIONS\" ${ASSISTANT_RESUME:+--resume} $ASSISTANT_RESUME"

    /// Runs one turn through the operator's `claude` alias. Its only Bash access is `tool`'s messaging commands.
    public static func ask(_ prompt: String, resume: String?, tool: String, step: @escaping (ChatStep) -> Void,
                           running: ((Process) -> Void)? = nil) -> Reply {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/zsh")
        p.arguments = ["-lc", command]
        var env = ProcessInfo.processInfo.environment
        env["ASSISTANT_INSTRUCTIONS"] = instructions(tool: tool)
        env["ASSISTANT_TOOL"] = tool
        env["ASSISTANT_RESUME"] = resume ?? ""
        p.environment = env
        p.currentDirectoryURL = URL(fileURLWithPath: NSHomeDirectory() + "/.claude/projects")
        let input = Pipe()
        let output = Pipe()
        p.standardInput = input
        p.standardOutput = output
        p.standardError = FileHandle.nullDevice
        do { try p.run() } catch {
            return Reply(text: "Couldn't start claude: \(error.localizedDescription)", sessionID: nil, cost: nil,
                         failed: true)
        }
        running?(p)
        DispatchQueue.global().async {
            input.fileHandleForWriting.write(Data(prompt.utf8))
            try? input.fileHandleForWriting.close()
        }
        var all = Data()
        var pending = Data()
        while true {
            let chunk = output.fileHandleForReading.availableData
            if chunk.isEmpty { break }
            all.append(chunk)
            pending.append(chunk)
            while let nl = pending.firstIndex(of: UInt8(ascii: "\n")) {
                let line = String(decoding: pending[pending.startIndex..<nl], as: UTF8.self)
                pending.removeSubrange(pending.startIndex...nl)
                if let s = Messaging.step(line, sendCommand: tool) { step(s) }
            }
        }
        p.waitUntilExit()
        if p.terminationReason == .uncaughtSignal {
            return Reply(text: "Stopped.", sessionID: nil, cost: nil, failed: true)
        }
        return parse(all)
    }
}
