import Foundation

public struct Workspace: Equatable, Identifiable {
    public let id: String
    public let ref: String
    public let title: String
    public let directory: String?
    public let submittedAt: String?
    public let submittedMessage: String?

    public static func parseList(_ data: Data) -> [Workspace] {
        guard let root = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let list = root["workspaces"] as? [[String: Any]] else { return [] }
        return list.compactMap { w in
            guard let id = w["id"] as? String, let ref = w["ref"] as? String else { return nil }
            let title = (w["custom_title"] as? String) ?? (w["title"] as? String) ?? ref
            return Workspace(id: id, ref: ref, title: title, directory: w["current_directory"] as? String,
                             submittedAt: w["latest_submitted_at"] as? String,
                             submittedMessage: w["latest_submitted_message"] as? String)
        }
    }
}

public struct Item: Identifiable {
    public var id: String { workspace.id }
    public let workspace: Workspace
    public let transcript: String
    public let summary: SessionSummary
    public let attention: Attention
    public let idleMinutes: Double
    public let pullRequests: [(number: Int, state: String)]

    public var question: String { Classifier.askLine(summary.lastSaid) }
    public var goal: String { (summary.goal ?? "").split(whereSeparator: \.isWhitespace).joined(separator: " ") }

    public var idleText: String {
        let h = idleMinutes / 60
        if h < 1 { return "\(Int(idleMinutes))m" }
        return h < 48 ? "\(Int(h))h" : "\(Int(h / 24))d"
    }
}

public struct ScanResult {
    public var items: [Item] = []
    public var untracked: [Workspace] = []
    public var error: String?

    public init() {}

    public func items(_ a: Attention) -> [Item] { items.filter { $0.attention == a } }
}

/// Finds the Claude transcript behind each cmux workspace. A workspace with no transcript yet is searched in full
/// once; after that, an FSEvents stream on `projects` names the transcripts that changed, and a later lookup reads
/// only those.
public final class TranscriptLocator {
    let projects: URL
    let home: String
    let read: (String) -> Data?
    private var paths: [String: String] = [:]
    private var summaries: [String: (Date, SessionSummary)] = [:]
    private struct Miss {
        let submitted: String
        let folders: Set<String>
        /// Changed transcript file names, by project folder.
        var changed: [String: Set<String>] = [:]
    }
    /// Workspaces whose transcript wasn't found, by workspace ID. The stream's queue adds to them under `lock`.
    private var misses: [String: Miss] = [:]
    private let lock = NSLock()
    private let events = DispatchQueue(label: "assistant.transcripts")
    private var stream: FSEventStreamRef?

    public init(projects: URL, home: String = NSHomeDirectory(),
                read: @escaping (String) -> Data? = { FileManager.default.contents(atPath: $0) }) {
        self.projects = projects
        self.home = home
        self.read = read
        watch()
    }

    deinit {
        guard let stream else { return }
        FSEventStreamStop(stream)
        FSEventStreamInvalidate(stream)
        FSEventStreamRelease(stream)
        events.sync {}
    }

    private func watch() {
        var context = FSEventStreamContext(version: 0, info: Unmanaged.passUnretained(self).toOpaque(),
                                           retain: nil, release: nil, copyDescription: nil)
        let callback: FSEventStreamCallback = { _, info, count, paths, flags, _ in
            guard let info, let changed = unsafeBitCast(paths, to: NSArray.self) as? [String] else { return }
            let dropped = FSEventStreamEventFlags(kFSEventStreamEventFlagMustScanSubDirs)
            let rescan = (0..<count).contains { flags[$0] & dropped != 0 }
            Unmanaged<TranscriptLocator>.fromOpaque(info).takeUnretainedValue().noticed(changed, rescan: rescan)
        }
        guard let s = FSEventStreamCreate(nil, callback, &context, [projects.path] as CFArray,
                                          FSEventStreamEventId(kFSEventStreamEventIdSinceNow), 1,
                                          FSEventStreamCreateFlags(kFSEventStreamCreateFlagFileEvents
                                                                   | kFSEventStreamCreateFlagUseCFTypes))
        else { return }
        FSEventStreamSetDispatchQueue(s, events)
        guard FSEventStreamStart(s) else {
            FSEventStreamInvalidate(s)
            FSEventStreamRelease(s)
            return
        }
        stream = s
    }

    /// Records changed transcripts against each workspace that searches their folder. When FSEvents drops events, every
    /// workspace gets a full search again.
    func noticed(_ changed: [String], rescan: Bool) {
        lock.lock()
        defer { lock.unlock() }
        if rescan {
            misses = [:]
            return
        }
        for path in changed where path.hasSuffix(".jsonl") {
            let parts = path.split(separator: "/")
            guard parts.count >= 2 else { continue }
            let folder = String(parts[parts.count - 2])
            for (id, miss) in misses where miss.folders.contains(folder) {
                misses[id]?.changed[folder, default: []].insert(String(parts[parts.count - 1]))
            }
        }
    }

    /// Drops what's remembered about workspaces that are gone.
    public func forget(keeping ids: Set<String>) {
        lock.lock()
        misses = misses.filter { ids.contains($0.key) }
        lock.unlock()
    }

    static func folderName(_ path: String) -> String {
        path.replacingOccurrences(of: "/", with: "-").replacingOccurrences(of: ".", with: "-")
    }

    public func transcript(for ws: Workspace) -> String? {
        guard let submitted = ws.submittedAt, let sent = Timestamps.parse(submitted) else { return nil }
        let key = ws.id + "|" + submitted
        if let hit = paths[key] { return hit }
        let folders = [ws.directory.map(Self.folderName), Self.folderName(home)].compactMap { $0 }
            .reduce(into: [String]()) { if !$0.contains($1) { $0.append($1) } }
        var changed: [String: Set<String>]?
        lock.lock()
        if stream != nil, let miss = misses[ws.id], miss.submitted == submitted {
            changed = miss.changed
            misses[ws.id]?.changed = [:]
        } else if stream != nil {
            misses[ws.id] = Miss(submitted: submitted, folders: Set(folders))
        }
        lock.unlock()
        for folder in folders {
            let dir = projects.appendingPathComponent(folder)
            let urls = changed.map { ($0[folder] ?? []).map { dir.appendingPathComponent($0) } }
                ?? ((try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: [.contentModificationDateKey]))
                    ?? []).filter { $0.pathExtension == "jsonl" }
            let files = urls.compactMap { url -> (String, Date)? in
                guard let m = try? url.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate,
                      m >= sent.addingTimeInterval(-5) else { return nil }
                return (url.path, m)
            }.sorted { $0.1 > $1.1 }
            for (path, _) in files {
                guard let data = read(path),
                      Transcript.holdsMessage(ws.submittedMessage, submittedAt: submitted, in: data) else { continue }
                paths[key] = path
                lock.lock()
                misses[ws.id] = nil
                lock.unlock()
                return path
            }
        }
        return nil
    }

    public func summary(at path: String) -> SessionSummary? {
        guard let m = (try? FileManager.default.attributesOfItem(atPath: path))?[.modificationDate] as? Date
        else { return nil }
        if let (seen, s) = summaries[path], seen == m { return s }
        guard let data = FileManager.default.contents(atPath: path) else { return nil }
        let s = Transcript.summarize(data)
        summaries[path] = (m, s)
        return s
    }
}

public enum Shell {
    /// Runs `tool`, ending it after `timeout` seconds so a hung `cmux`, `ps`, or `lsof` can't block a scan or a send
    /// forever; a timed-out run reports status -1.
    public static func run(_ tool: String, _ args: [String], timeout: TimeInterval = 30) -> (Int32, Data) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: tool)
        p.arguments = args
        var env = ProcessInfo.processInfo.environment
        env["CMUX_QUIET"] = "1"
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
        p.environment = env
        let out = Pipe()
        p.standardOutput = out
        p.standardError = FileHandle.nullDevice
        do { try p.run() } catch { return (-1, Data()) }
        let killer = DispatchWorkItem { p.terminate() }
        DispatchQueue.global().asyncAfter(deadline: .now() + timeout, execute: killer)
        let data = out.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        killer.cancel()
        return (p.terminationReason == .uncaughtSignal ? -1 : p.terminationStatus, data)
    }

    public static func find(_ candidates: [String]) -> String? {
        candidates.first { FileManager.default.isExecutableFile(atPath: $0) }
    }
}

public final class SessionScanner {
    public static let cmux = Shell.find(["/Applications/cmux.app/Contents/Resources/bin/cmux",
                                         "/opt/homebrew/bin/cmux", "/usr/local/bin/cmux"])
    static let gh = Shell.find(["/opt/homebrew/bin/gh", "/usr/local/bin/gh"])

    let locator: TranscriptLocator
    let threshold: Double
    private var merged = Set<String>()

    public init(projects: URL = URL(fileURLWithPath: NSHomeDirectory() + "/.claude/projects"),
                idleThresholdMinutes: Double = 60) {
        locator = TranscriptLocator(projects: projects)
        threshold = idleThresholdMinutes
    }

    func prStates(_ urls: [String]) -> [String: String] {
        var states = Dictionary(uniqueKeysWithValues: urls.filter(merged.contains).map { ($0, "MERGED") })
        let pending = urls.filter { !merged.contains($0) }
        guard let gh = Self.gh else { return states }
        var fetched = [String?](repeating: nil, count: pending.count)
        let lock = NSLock()
        DispatchQueue.concurrentPerform(iterations: pending.count) { i in
            let (rc, data) = Shell.run(gh, ["pr", "view", pending[i], "--json", "state,mergeStateStatus"])
            guard rc == 0, let d = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
                  let state = d["state"] as? String else { return }
            let merge = (d["mergeStateStatus"] as? String ?? "").lowercased()
            lock.lock()
            fetched[i] = state == "OPEN" ? "open, \(merge)" : state
            lock.unlock()
        }
        for (url, state) in zip(pending, fetched) {
            guard let state else { continue }
            states[url] = state
            if state == "MERGED" { merged.insert(url) }
        }
        return states
    }

    public func scan(now: Date = Date()) -> ScanResult {
        var result = ScanResult()
        guard let cmux = Self.cmux else {
            result.error = "cmux isn't installed."
            return result
        }
        let (rc, data) = Shell.run(cmux, ["workspace", "list", "--json"])
        guard rc == 0 else {
            result.error = "cmux isn't running."
            return result
        }
        var found: [(Workspace, String, SessionSummary)] = []
        let workspaces = Workspace.parseList(data)
        locator.forget(keeping: Set(workspaces.map(\.id)))
        for ws in workspaces {
            guard let path = locator.transcript(for: ws), let s = locator.summary(at: path), s.goal != nil,
                  s.lastActivity != nil else {
                result.untracked.append(ws)
                continue
            }
            found.append((ws, path, s))
        }
        let states = prStates(Array(Set(found.flatMap { $0.2.pullRequests.keys })).sorted())
        result.items = found.map { ws, path, s in
            let idle = now.timeIntervalSince(s.lastActivity!) / 60
            let prs = s.pullRequests.sorted { $0.value < $1.value }.map { (number: $0.value, state: states[$0.key] ?? "unknown") }
            return Item(workspace: ws, transcript: path, summary: s,
                        attention: Classifier.status(s, prStates: states, idleMinutes: idle, threshold: threshold),
                        idleMinutes: idle, pullRequests: prs)
        }.sorted { ($0.attention, $0.idleMinutes) < ($1.attention, $1.idleMinutes) }
        return result
    }

    public static func focus(_ ws: Workspace) {
        guard let cmux else { return }
        _ = Shell.run(cmux, ["workspace", "select", ws.ref])
    }
}
