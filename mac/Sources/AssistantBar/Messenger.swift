import Foundation
import SessionCore

/// Sends a message into a workspace's Claude session and waits for its reply. Used by the chat's Claude
/// through `AssistantBar --send` and `--wait`.
enum Messenger {
    struct Target {
        let terminal: Terminal
        let transcript: URL
    }

    static let backOff = URL(fileURLWithPath: NSHomeDirectory() + "/.assistant/back-off.json")

    static func cmux(_ args: [String]) -> (Int32, Data) {
        guard let cmux = SessionScanner.cmux else { return (-1, Data()) }
        return Shell.run(cmux, args)
    }

    static func backedOff(_ workspace: String) -> Bool {
        guard let data = FileManager.default.contents(atPath: backOff.path),
              let d = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let list = d["workspaces"] as? [Any] else { return false }
        return list.contains { item in
            if let s = item as? String { return s == workspace }
            if let o = item as? [String: Any] { return o.values.contains { ($0 as? String) == workspace } }
            return false
        }
    }

    static let projects = URL(fileURLWithPath: NSHomeDirectory() + "/.claude/projects")

    /// The session's transcript, or where Claude will write it once the session gets its first message.
    static func transcript(_ session: Messaging.Session) -> URL? {
        let folders = (try? FileManager.default.contentsOfDirectory(atPath: projects.path)) ?? []
        if let found = folders.filter({ !$0.hasPrefix("-private-") })
            .map({ projects.appendingPathComponent($0).appendingPathComponent(session.id + ".jsonl") })
            .first(where: { FileManager.default.fileExists(atPath: $0.path) }) { return found }
        let (_, out) = Shell.run("/usr/sbin/lsof", ["-a", "-p", String(session.pid), "-d", "cwd", "-Fn"])
        guard let cwd = String(decoding: out, as: UTF8.self).split(separator: "\n")
            .first(where: { $0.hasPrefix("n") })?.dropFirst() else { return nil }
        return projects.appendingPathComponent(Messaging.projectFolder(String(cwd)))
            .appendingPathComponent(session.id + ".jsonl")
    }

    /// Every workspace pane running Claude, with its transcript.
    static func targets() -> [Target] {
        let (_, tree) = cmux(["tree", "--json"])
        let (_, ps) = Shell.run("/bin/ps", ["-A", "-o", "pid=,tty=,command="])
        let sessions = Messaging.claudeSessions(String(decoding: ps, as: UTF8.self))
        return Messaging.terminals(tree).compactMap { t in
            sessions[t.tty].flatMap(transcript).map { Target(terminal: t, transcript: $0) }
        }
    }

    static func target(_ workspace: String) -> Target? {
        targets().first { $0.terminal.workspace == workspace || $0.terminal.title == workspace }
    }

    static func list() -> String {
        targets().map { "\($0.terminal.workspace)  \($0.terminal.title)  (pane \($0.terminal.surface))" }
            .joined(separator: "\n")
    }

    /// Pastes `message` into the session, presses Enter, confirms it arrived, then waits for the reply.
    static func send(_ workspace: String, _ message: String, timeout: TimeInterval) -> String {
        guard !backedOff(workspace) else { return "NOT SENT\n\(workspace) is on the operator's back-off list." }
        guard let target = target(workspace) else {
            return "NOT SENT\nNo Claude session found in \(workspace). Run --sessions to list them."
        }
        let pane = ["--workspace", target.terminal.workspace, "--surface", target.terminal.surface]
        let since = Date()
        let (rc, out) = cmux(["send"] + pane + ["--paste", message])
        guard rc == 0 else {
            return "NOT SENT\nThe session is busy: someone is typing there, or a question or permission prompt is open.\n"
                + String(decoding: out, as: UTF8.self)
        }
        _ = cmux(["send"] + pane + ["\\r"])
        if !waitFor(target.transcript, timeout: 15, until: { Messaging.turn($0, sent: message, since: since).delivered }) {
            _ = cmux(["send"] + pane + ["\\r"])
            guard waitFor(target.transcript, timeout: 15, until: { Messaging.turn($0, sent: message, since: since).delivered })
            else { return "NOT DELIVERED\nThe text is in \(workspace)'s prompt but didn't submit." }
        }
        return wait(target, sent: message, since: since, timeout: timeout)
    }

    static func wait(_ workspace: String, timeout: TimeInterval) -> String {
        guard let target = target(workspace) else { return "No Claude session found in \(workspace)." }
        let data = (try? Data(contentsOf: target.transcript)) ?? Data()
        let last = Transcript.summarize(data).lastAsk ?? ""
        return wait(target, sent: last, since: .distantPast, timeout: timeout)
    }

    static func wait(_ target: Target, sent: String, since: Date, timeout: TimeInterval) -> String {
        let done = waitFor(target.transcript, timeout: timeout) { Messaging.turn($0, sent: sent, since: since).finished }
        let data = (try? Data(contentsOf: target.transcript)) ?? Data()
        let state = Messaging.turn(data, sent: sent, since: since)
        let reply = state.replies.last ?? "(no reply text yet)"
        return done ? "REPLY\n\(reply)"
            : "STILL WORKING\nNo final reply after \(Int(timeout)) seconds. Latest:\n\(reply)\n"
                + "It may be waiting on a permission prompt. Run --wait \(target.terminal.workspace) to keep waiting."
    }

    /// Re-reads `file` each time it changes until `until` holds or `timeout` passes.
    static func waitFor(_ file: URL, timeout: TimeInterval, until: @escaping (Data) -> Bool) -> Bool {
        let check = { until((try? Data(contentsOf: file)) ?? Data()) }
        if check() { return true }
        let deadline = Date().addingTimeInterval(timeout)
        if !FileManager.default.fileExists(atPath: file.path) {
            try? FileManager.default.createDirectory(at: file.deletingLastPathComponent(), withIntermediateDirectories: true)
            guard waitForChanges(file.deletingLastPathComponent(), until: deadline, check: {
                FileManager.default.fileExists(atPath: file.path)
            }) else { return false }
            if check() { return true }
        }
        return waitForChanges(file, until: deadline, check: check)
    }

    /// Re-runs `check` whenever `path` (a file or folder) changes, until it holds or `deadline` passes.
    static func waitForChanges(_ path: URL, until deadline: Date, check: @escaping () -> Bool) -> Bool {
        let fd = open(path.path, O_EVTONLY)
        guard fd >= 0 else { return false }
        let changed = DispatchSemaphore(value: 0)
        let source = DispatchSource.makeFileSystemObjectSource(fileDescriptor: fd, eventMask: [.write, .extend],
                                                               queue: .global())
        source.setEventHandler { changed.signal() }
        source.setCancelHandler { close(fd) }
        source.resume()
        defer { source.cancel() }
        while Date() < deadline {
            _ = changed.wait(timeout: .now() + min(5, deadline.timeIntervalSinceNow))
            if check() { return true }
        }
        return false
    }
}
