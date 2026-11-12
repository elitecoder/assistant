import Foundation
import SessionCore

/// Sends a message into a workspace's Claude session and waits for its reply. Used by the chat's Claude
/// through `AssistantBar --send` and `--wait`.
enum Messenger {
    struct Target {
        let terminal: Terminal
        let transcript: URL
        /// Said along with a reply when the transcript might not be the session's current one.
        var note: String? = nil
    }

    static let backOff = URL(fileURLWithPath: NSHomeDirectory() + "/.assistant/back-off.json")

    static func cmux(_ args: [String]) -> (Int32, Data) {
        guard let cmux = SessionScanner.cmux else { return (-1, Data()) }
        return Shell.run(cmux, args)
    }

    /// Checked against the workspace a name resolves to, so a title can't reach a workspace backed off by ref.
    static func backedOff(_ names: [String]) -> Bool {
        guard let data = FileManager.default.contents(atPath: backOff.path),
              let d = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let list = d["workspaces"] as? [Any] else { return false }
        return Messaging.backedOff(list, names: names)
    }

    static let projects = URL(fileURLWithPath: NSHomeDirectory() + "/.claude/projects")

    /// The live `claude` processes this Mac runs, for telling one session's transcript from another's.
    struct Processes {
        let sessions: [String: Messaging.Session]
        let all: [(pid: Int32, sessionID: String?)]
        let cwds: [Int32: String]

        static func now() -> Processes {
            let (_, ps) = Shell.run("/bin/ps", ["-A", "-o", "pid=,tty=,command="])
            let text = String(decoding: ps, as: UTF8.self)
            let all = Messaging.claudeProcesses(text)
            var cwds: [Int32: String] = [:]
            if !all.isEmpty {
                let (_, out) = Shell.run("/usr/sbin/lsof", ["-a", "-d", "cwd", "-p", all.map { String($0.pid) }.joined(separator: ","), "-Fpn"])
                var pid: Int32?
                for line in String(decoding: out, as: UTF8.self).split(separator: "\n") {
                    if line.hasPrefix("p") { pid = Int32(line.dropFirst()) } else if line.hasPrefix("n"), let pid { cwds[pid] = String(line.dropFirst()) }
                }
            }
            return Processes(sessions: Messaging.claudeSessions(text), all: all, cwds: cwds)
        }
    }

    /// The session's transcript, or where Claude will write it once the session gets its first message. After /clear
    /// the session writes a new file the process arguments don't name; `live` finds it when the evidence is clear.
    static func transcript(_ session: Messaging.Session, _ processes: Processes) -> (URL, String?)? {
        let cwd = processes.cwds[session.pid]
        let folders = (try? FileManager.default.contentsOfDirectory(atPath: projects.path)) ?? []
        if let found = folders.filter({ !$0.hasPrefix("-private-") })
            .map({ projects.appendingPathComponent($0).appendingPathComponent(session.id + ".jsonl") })
            .first(where: { FileManager.default.fileExists(atPath: $0.path) }) {
            let live = live(found, session, processes)
            return (live.url, live.note)
        }
        guard let cwd else { return nil }
        return (projects.appendingPathComponent(Messaging.projectFolder(cwd)).appendingPathComponent(session.id + ".jsonl"), nil)
    }

    static func live(_ named: URL, _ session: Messaging.Session, _ processes: Processes) -> Messaging.LiveTranscript {
        guard let start = processStart(session.pid) else { return Messaging.LiveTranscript(url: named, note: nil) }
        let folder = named.deletingLastPathComponent()
        let keys: Set<URLResourceKey> = [.creationDateKey, .contentModificationDateKey]
        let urls = ((try? FileManager.default.contentsOfDirectory(at: folder, includingPropertiesForKeys: Array(keys))) ?? [])
            .filter { $0.pathExtension == "jsonl" }
        var namedModified: Date?
        let candidates = urls.compactMap { url -> Messaging.TranscriptCandidate? in
            guard let v = try? url.resourceValues(forKeys: keys), let c = v.creationDate, let m = v.contentModificationDate
            else { return nil }
            if url == named { namedModified = m }
            guard url != named, c >= start, let handle = try? FileHandle(forReadingFrom: url) else { return nil }
            defer { try? handle.close() }
            let head = Messaging.transcriptStart((try? handle.read(upToCount: 1 << 20)) ?? Data())
            return Messaging.TranscriptCandidate(url: url, created: c, modified: m, startsWithClear: head.startsWithClear,
                                                 linkedSessions: head.linkedSessions)
        }
        let others = processes.all.filter { $0.pid != session.pid }
        return Messaging.liveTranscript(named: named, sessionID: session.id, namedModified: namedModified, processStart: start,
                                        candidates: candidates, ownedByOthers: Set(others.compactMap(\.sessionID)))
    }

    static func processStart(_ pid: Int32) -> Date? {
        let (_, out) = Shell.run("/bin/ps", ["-o", "lstart=", "-p", String(pid)], timeout: 5)
        let text = String(decoding: out, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "EEE MMM d HH:mm:ss yyyy"
        return f.date(from: text.split(separator: " ", omittingEmptySubsequences: true).joined(separator: " "))
    }

    /// Every workspace pane running Claude, with its transcript.
    static func targets() -> [Target] {
        let (_, tree) = cmux(["tree", "--json"])
        let processes = Processes.now()
        return Messaging.terminals(tree).compactMap { t in
            processes.sessions[t.tty].flatMap { transcript($0, processes) }.map { Target(terminal: t, transcript: $0.0, note: $0.1) }
        }
    }

    static func target(_ workspace: String) -> Target? {
        targets().first { $0.terminal.workspace == workspace || $0.terminal.title == workspace }
    }

    /// Workspace titles come from the sessions, so the list is printed as untrusted session text.
    static func list() -> String {
        Untrusted.wrap(targets().map { "\($0.terminal.workspace)  \($0.terminal.title)  (pane \($0.terminal.surface))" }
            .joined(separator: "\n"))
    }

    /// `--transcript`: a session's recent conversation, as untrusted session text.
    static func conversation(_ workspace: String) -> String {
        guard let target = target(workspace) else { return "No Claude session found in \(workspace). Run --sessions to list them." }
        let data = (try? Data(contentsOf: target.transcript)) ?? Data()
        let note = target.note.map { "\nNOTE: " + $0 } ?? ""
        return "Recent conversation in \(target.terminal.workspace):\n" + Untrusted.wrap(Digest.exchange(data)) + note
    }

    /// `--find`: matching lines from the sessions' transcripts, as untrusted session text.
    static func find(_ json: String) -> String {
        guard let query = TranscriptSearch.query(json) else { return #"--find needs {"text": "<text>"}, with an optional "workspace"."# }
        let scope = targets().filter { t in query.workspace.map { t.terminal.workspace == $0 || t.terminal.title == $0 } ?? true }
        var found: [String] = []
        for t in scope {
            let data = (try? Data(contentsOf: t.transcript)) ?? Data()
            found += TranscriptSearch.matches(data, text: query.text, limit: query.limit).map { "\(t.terminal.workspace) \($0)" }
        }
        guard !found.isEmpty else { return "No transcript line has \"\(query.text)\"." }
        return Untrusted.wrap(found.suffix(query.limit).joined(separator: "\n\n"))
    }

    /// Pastes `message` into the session, presses Enter, confirms it arrived, then waits for the reply.
    static func send(_ workspace: String, _ message: String, timeout: TimeInterval) -> String {
        if let refusal = Messaging.sendRefusal(message) { return "NOT SENT\n" + refusal }
        let target = target(workspace)
        let names = [workspace] + [target?.terminal.workspace, target?.terminal.workspaceID, target?.terminal.title].compactMap { $0 }
        guard !backedOff(names) else { return "NOT SENT\n\(workspace) is on the operator's back-off list." }
        guard let target else {
            return "NOT SENT\nNo Claude session found in \(workspace). Run --sessions to list them."
        }
        guard let cmux = SessionScanner.cmux else { return "NOT SENT\ncmux isn't installed." }
        let pane = ["--workspace", target.terminal.workspace, "--surface", target.terminal.surface]
        let since = Date()
        let delivery = TerminationHold.pasteAndEnter(cmux, paste: Messaging.pasteArguments(
            workspace: target.terminal.workspace, surface: target.terminal.surface, message: message),
                                                     enter: ["send"] + pane + ["\\r"])
        // The run was ended while pasting: both steps finished, and now this exits too.
        if delivery.interrupted { exit(143) }
        let (rc, out) = delivery.paste
        guard rc == 0 else {
            return "NOT SENT\nThe session is busy: someone is typing there, or a question or permission prompt is open.\n"
                + String(decoding: out, as: UTF8.self)
        }
        // Enter is pressed once. If the transcript doesn't show the message, pressing it again could submit it twice.
        guard waitFor(target.transcript, timeout: 30, until: { Messaging.turn($0, sent: message, since: since).delivered }) else {
            return Messaging.unconfirmed(workspace)
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
        let note = target.note.map { "\nNOTE: " + $0 } ?? ""
        return done ? "REPLY\n" + Untrusted.wrap(reply) + note
            : "STILL WORKING\nNo final reply after \(Int(timeout)) seconds. Latest:\n" + Untrusted.wrap(reply) + "\n"
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
