import Foundation

/// Writing to a child process's standard input.
public enum ChildInput {
    /// Writes `data` and closes the pipe. A child that exits before reading it all (Stop, or `claude` quitting at once)
    /// ends the write with an error instead of SIGPIPE, which would kill the app; returns false then.
    @discardableResult
    public static func feed(_ handle: FileHandle, _ data: Data) -> Bool {
        _ = fcntl(handle.fileDescriptor, F_SETNOSIGPIPE, 1)
        defer { try? handle.close() }
        do {
            try handle.write(contentsOf: data)
            return true
        } catch {
            return false
        }
    }
}

/// Which processes descend from which: `parents` maps each pid to its parent's.
public enum ProcessTree {
    /// Every process below `root`, children before their own children.
    public static func descendants(of root: Int32, parents: [Int32: Int32]) -> [Int32] {
        var children: [Int32: [Int32]] = [:]
        for (pid, parent) in parents where pid != parent { children[parent, default: []].append(pid) }
        var out: [Int32] = []
        var queue = children[root] ?? []
        var seen: Set<Int32> = [root]
        while !queue.isEmpty {
            let pid = queue.removeFirst()
            guard seen.insert(pid).inserted else { continue }
            out.append(pid)
            queue += children[pid] ?? []
        }
        return out
    }

    /// Parses `ps -A -o pid=,ppid=`.
    public static func parse(_ ps: String) -> [Int32: Int32] {
        var parents: [Int32: Int32] = [:]
        for line in ps.split(whereSeparator: \.isNewline) {
            let parts = line.split(separator: " ", omittingEmptySubsequences: true)
            if parts.count == 2, let pid = Int32(parts[0]), let parent = Int32(parts[1]) { parents[pid] = parent }
        }
        return parents
    }

    public static func current() -> [Int32: Int32] {
        parse(String(decoding: Shell.run("/bin/ps", ["-A", "-o", "pid=,ppid="], timeout: 5).1, as: UTF8.self))
    }
}

/// The processes the app starts (the chat's `claude`, the summarizer) and whatever they start in turn (`AssistantBar
/// --send` and `--wait` under `claude`'s Bash), so Stop and Quit can end all of them.
public final class ChildProcesses: @unchecked Sendable {
    public static let shared = ChildProcesses()
    private let lock = NSLock()
    private var running: [Int32: Process] = [:]
    private var stopRequested: Set<Int32> = []

    public init() {}

    public func add(_ process: Process) { lock.withLock { running[process.processIdentifier] = process } }

    /// Forgets a finished process; returns whether Stop or Quit ended it.
    @discardableResult
    public func remove(_ process: Process) -> Bool {
        lock.withLock {
            running[process.processIdentifier] = nil
            return stopRequested.remove(process.processIdentifier) != nil
        }
    }

    public var pids: [Int32] { lock.withLock { Array(running.keys) } }

    /// Ends every tracked process and all of their descendants. It first freezes them (SIGSTOP), looking again until
    /// no new process appears below them, so none can start another while they're being ended; then sends SIGTERM and
    /// lets them run to handle it. A last look catches anything started in between.
    public func endAll(tree: () -> [Int32: Int32] = ProcessTree.current) {
        let roots: [Int32] = lock.withLock {
            stopRequested.formUnion(running.keys)
            return Array(running.keys)
        }
        guard !roots.isEmpty else { return }
        func below(_ parents: [Int32: Int32]) -> [Int32] { roots.flatMap { ProcessTree.descendants(of: $0, parents: parents) } }
        var frozen: [Int32] = []
        var next = roots
        for _ in 0..<5 where !next.isEmpty {
            next.forEach { kill($0, SIGSTOP) }
            frozen += next
            next = below(tree()).filter { !frozen.contains($0) }
        }
        frozen.reversed().forEach { kill($0, SIGTERM) }
        frozen.forEach { kill($0, SIGCONT) }
        for pid in below(tree()) where !frozen.contains(pid) {
            kill(pid, SIGTERM)
        }
    }
}
