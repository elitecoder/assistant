// The Mac chat's conversation loop, after Hermes Agent (MIT, Nous Research): agent/conversation_loop.py,
// agent/turn_preflight.py and agent/turn_context_compaction.py. The app owns the history; every turn is a fresh
// `claude -p` run.
import Foundation

public struct TurnOutcome: Equatable {
    public var reply: String
    public var failed: Bool
    public var cost: Double?
    public var meter: ContextMeter?
    /// Sentences handed to `speak` as the reply was written.
    public var spokenSentences = 0
    /// What a stopped or interrupted run had written before it ended, shown above the note that says why.
    public var partial: String? = nil
}

/// What a chat remembers between processes besides its rows: the fleet baseline and a compression pause.
struct ChatState: Codable, Equatable {
    var fleet: FleetState?
    var compressionPausedUntil: Date?
}

/// One chat at a time: stores each turn in `store`, compresses before a turn when the context nears the window, and
/// assembles each prompt so it extends the last one byte for byte. Not thread-safe: call it from one serial queue.
/// Several engines (the app and terminal commands) may share one database; a per-session file lock keeps their
/// writes to the session and its state in order.
public final class ChatEngine {
    public let store: ConversationStore
    public let memoryFolder: URL
    public let tool: String
    public private(set) var model: String
    public private(set) var effort: String?
    public private(set) var sessionID: String?
    public private(set) var meter: ContextMeter?

    private let runner: ClaudeRunner
    private let compressor: ContextCompressor
    private let windowFor: (String) -> Int
    private let now: () -> Date
    private var parentID: String?
    private var pendingID: String?
    private var anchor: UsageAnchor?

    /// How long automatic compression pauses after a compaction couldn't be saved.
    static let saveFailurePause: TimeInterval = 300

    public init(store: ConversationStore, memoryFolder: URL = MemoryStore.defaultFolder, tool: String, model: String,
                effort: String?, windowFor: @escaping (String) -> Int = { ContextWindow.tokens(for: $0) },
                runner: @escaping ClaudeRunner, summarizer: @escaping Summarizer, now: @escaping () -> Date = Date.init) {
        self.store = store
        self.memoryFolder = memoryFolder
        self.tool = tool
        self.model = model
        self.effort = effort
        self.windowFor = windowFor
        self.runner = runner
        self.now = now
        compressor = ContextCompressor(config: Self.config(model: model, window: windowFor(model)), summarizer: summarizer, now: now)
    }

    static func config(model: String, window: Int) -> CompressorConfig {
        CompressorConfig(model: model, provider: "anthropic", contextLength: window,
                         terminalToolNames: ["terminal", "shell", "bash", "Bash"])
    }

    public var window: Int { compressor.config.contextLength }
    public var thresholdTokens: Int { compressor.thresholdTokens }

    // MARK: Sessions

    /// Reopens the most recently active chat, if there is one.
    public func openLatest() throws {
        if let latest = try store.sessions(limit: 1).first { try open(latest.session.id) }
    }

    public func open(_ id: String) throws {
        guard let session = try store.session(id: id) else { throw ConversationStoreError.sessionNotFound(id) }
        sessionID = id
        pendingID = nil
        parentID = nil
        anchor = UsageAnchor(session: session)
        if let anchor, let active = try? store.resumeMessages(sessionID: id) {
            meter = ContextMeter(tokens: UsageAnchor.contextTokens(active, anchor: anchor), window: window)
        } else {
            meter = nil
        }
        compressor.resetSession()
    }

    /// The next question starts a new session whose parent is the current one.
    public func newChat() {
        parentID = sessionID ?? parentID
        sessionID = nil
        pendingID = nil
        anchor = nil
        meter = nil
        compressor.resetSession()
    }

    /// Continues the session with `id`, or starts it with that id on the next question.
    public func use(_ id: String) throws {
        if try store.session(id: id) != nil { return try open(id) }
        newChat()
        parentID = nil
        pendingID = id
    }

    public func setModel(_ model: String, effort: String?) {
        self.model = model
        self.effort = effort
        compressor.updateModel(Self.config(model: model, window: windowFor(model)))
        meter = meter.map { ContextMeter(tokens: $0.tokens, window: window) }
    }

    public func sessions(limit: Int = 20) -> [ConversationListItem] {
        (try? store.sessions(limit: limit)) ?? []
    }

    /// What the operator saw in the current chat, compressed turns included.
    public func displayTurns() -> [ChatTurn] {
        guard let sessionID, let rows = try? store.messages(sessionID: sessionID, scope: .display) else { return [] }
        return ChatHistory.turns(rows, tool: tool)
    }

    // MARK: Turns

    /// Runs one turn. `fleet` is the latest scan; the model gets the full digest on a session's first turn and once a
    /// compaction archives it, and only what changed otherwise. The context row and question are stored before the
    /// run, so a crash or a second window never loses them or a session message the run already sent. `step` gets
    /// the session messages Claude sends and compaction notes. The usage anchor is the turn's first model call; the
    /// meter adds a rough count of the rows the turn stored after it.
    /// `speak` gets the reply's sentences as Claude writes them, then whatever is left when it's done; the stored rows
    /// and the reply are the same either way.
    public func ask(_ question: String, fleet: ScanResult, focus: (title: String, exchange: String)? = nil, spoken: Bool = false,
                    speak: ((String) -> Void)? = nil, step: @escaping (ChatStep) -> Void) -> TurnOutcome {
        do {
            let sid = try ensureSession(question)
            let active = try store.resumeMessages(sessionID: sid)
            let entries = Digest.entries(fleet)
            let preview = promptRows(question, entries: entries, focus: focus, state: chatState(sid), at: now())
            let probe = active + preview.rows.enumerated().map { offset, row in
                ConversationMessage(sessionID: sid, seq: (active.last?.seq ?? 0) + offset + 1, role: row.role, content: row.content)
            }
            if pausedFor(sid) == nil && compressor.shouldCompress(messages: probe, anchor: anchor) {
                step(.note("Compressing the chat…"))
                if let note = compact(sid, active, focus: nil, force: false) { step(.note(note)) }
            } else if let note = blockedNote(sid, probe) {
                step(.note(note))
            }
            try withSessionLock(sid) {
                var state = chatState(sid)
                let prompt = promptRows(question, entries: entries, focus: focus, state: state, at: now())
                try store.append(sessionID: sid, prompt.rows)
                if let marks = prompt.marks {
                    state.fleet = FleetState.after(state.fleet, sent: marks)
                    try saveChatState(sid, state)
                }
            }
            return try runTurn(sid, spoken: spoken, speak: speak, step: step, retried: false)
        } catch {
            return TurnOutcome(reply: "The chat couldn't be saved: \(error)", failed: true, cost: nil, meter: meter)
        }
    }

    /// This turn's rows to store before the run: the context row when there is any, then the question. `marks` is
    /// the fleet the context row shows, when it shows one.
    private func promptRows(_ question: String, entries: [FleetEntry], focus: (title: String, exchange: String)?, state: ChatState,
                            at time: Date) -> (rows: [MessageDraft], marks: [FleetMark]?) {
        let update = FleetDelta.update(entries, since: state.fleet?.sent, now: time)
        let rows = [TurnContext.row(fleet: update.text, focus: focus), question].compactMap { text in
            text.map { MessageDraft(role: .user, content: $0, timestamp: time) }
        }
        return (rows, update.text == nil ? nil : update.marks)
    }

    /// Runs `claude` on the stored transcript and stores what it did. A first call refused as too long forces a
    /// compaction and runs once more, as Hermes does.
    private func runTurn(_ sid: String, spoken: Bool, speak: ((String) -> Void)?, step: @escaping (ChatStep) -> Void,
                         retried: Bool) throws -> TurnOutcome {
        let active = try store.resumeMessages(sessionID: sid)
        var invocation = self.invocation(sid, active: active, tail: spoken ? [TurnContext.marker + "\n\n" + Spoken.style] : [])
        invocation.partialMessages = speak != nil
        var stream = SpokenStream()
        let run = runner(invocation) { [tool] line in
            if let s = Messaging.step(line, sendCommand: tool) { step(s) }
            if let speak { stream.take(line: line).forEach(speak) }
        }
        if let error = run.launchError {
            try store.append(sessionID: sid, [TurnRecord.noReplyNote(error, timestamp: now())])
            return TurnOutcome(reply: error, failed: true, cost: nil, meter: meter)
        }
        var record = StreamTurn.record(run.output, stopped: run.stopped, model: model, now: now())
        if let signal = run.crashSignal, record.failed {
            record.reply = "Claude Code quit unexpectedly (signal \(signal)), so there's no answer. Ask again."
        }
        if run.stopped, let reason = run.stopReason { record.reply = reason }
        if let reported = record.contextWindow, ContextWindow.remember(reported, for: model),
           windowFor(model) != compressor.config.contextLength {
            compressor.updateModel(Self.config(model: model, window: windowFor(model)))
        }
        if record.promptTooLong && !retried {
            step(.note("The chat is too long for the model. Compressing it and trying again…"))
            if let note = compact(sid, active, focus: nil, force: true) { step(.note(note)) }
            return try runTurn(sid, spoken: spoken, speak: speak, step: step, retried: true)
        }
        try store.append(sessionID: sid, record.rowsToStore(timestamp: now()))
        if let tokens = record.promptTokens, tokens > 0, let priced = active.last {
            try store.recordUsage(sessionID: sid, promptTokens: tokens, atSeq: priced.seq)
            compressor.recordUsage(promptTokens: tokens)
            anchor = UsageAnchor.capture(promptTokens: tokens, messages: active)
            meter = ContextMeter(tokens: UsageAnchor.contextTokens(try store.resumeMessages(sessionID: sid), anchor: anchor),
                                 window: window)
        }
        if let speak, !record.failed { stream.finish(reply: record.reply).forEach(speak) }
        return TurnOutcome(reply: record.reply, failed: record.failed, cost: record.cost, meter: meter,
                           spokenSentences: stream.sentences, partial: run.stopped && !record.said.isEmpty ? record.said : nil)
    }

    /// The `claude -p` run for the stored transcript, plus `tail` blocks that are sent this turn only.
    func invocation(_ sid: String, active: [ConversationMessage], tail: [String] = []) -> ClaudeInvocation {
        let frozen = active.first { $0.role == .system }?.content ?? ""
        return ClaudeInvocation(
            systemPrompt: TurnPrompt.systemPrompt(instructions: Asker.instructions(tool: tool) + "\n\n" + Asker.toolGuide(tool: tool),
                                                  frozen: frozen),
            input: TurnPrompt.input(TurnPrompt.blocks(active) + tail), model: model, effort: effort,
            environment: ["ASSISTANT_SESSION_ID": sid, "ASSISTANT_DB": store.url.path, "ASSISTANT_MEMORY_DIR": memoryFolder.path])
    }

    // MARK: Compression

    /// Compresses the current chat now. Returns what happened, for the panel.
    public func compressNow(focus: String?) -> String {
        guard let sid = sessionID, let active = try? store.resumeMessages(sessionID: sid) else { return "Nothing to compress yet." }
        let topic = focus?.trimmingCharacters(in: .whitespacesAndNewlines)
        return compact(sid, active, focus: topic?.isEmpty == false ? topic : nil, force: true) ?? "Nothing to compress yet."
    }

    /// Never throws: a compaction that can't be saved leaves the chat uncompressed and pauses automatic compression.
    private func compact(_ sid: String, _ active: [ConversationMessage], focus: String?, force: Bool) -> String? {
        let frozen = active.first { $0.role == .system }?.content ?? ""
        let result = compressor.compress(active, focusTopic: focus, force: force, memoryContext: frozen)
        if let wait = compressor.activeCooldown { pause(sid, until: now().addingTimeInterval(wait)) }
        switch result.outcome {
        case .compacted:
            var newActive = result.messages
            if newActive.first?.role == .system {
                newActive[0].content = TurnPrompt.frozenAfterCompaction(memory: memorySnapshot())
            }
            do {
                try store.applyCompaction(sessionID: sid, newActive: newActive, watermark: active.map(\.seq).max() ?? 0,
                                          timestamp: now())
                try withSessionLock(sid) {
                    var state = chatState(sid)
                    state.fleet = FleetState.afterCompaction(state.fleet, active: try store.resumeMessages(sessionID: sid))
                    try saveChatState(sid, state)
                }
            } catch {
                pause(sid, until: now().addingTimeInterval(Self.saveFailurePause))
                return "Compression couldn't be saved (\(error)). The chat continues uncompressed."
            }
            anchor = nil
            meter = nil
            var note = "Compressed \(result.stats.summarizedMessages) messages into a summary."
            if result.stats.usedFallbackSummary { note += " The summarizer didn't answer, so it kept a shorter list of what happened." }
            return note
        case .noOp(let reason):
            return force ? "Nothing to compress yet (\(reason))." : nil
        case .aborted:
            return result.warning ?? "Compression failed. The chat continues uncompressed."
        }
    }

    /// When automatic compression is paused for this session, possibly by another process.
    private func pausedFor(_ sid: String) -> TimeInterval? {
        guard let until = chatState(sid).compressionPausedUntil else { return nil }
        let wait = until.timeIntervalSince(now())
        return wait > 0 ? wait : nil
    }

    private func pause(_ sid: String, until: Date) {
        try? withSessionLock(sid) {
            var state = chatState(sid)
            state.compressionPausedUntil = max(until, state.compressionPausedUntil ?? .distantPast)
            try saveChatState(sid, state)
        }
    }

    private func blockedNote(_ sid: String, _ messages: [ConversationMessage]) -> String? {
        guard UsageAnchor.contextTokens(messages, anchor: anchor) >= thresholdTokens else { return nil }
        if let wait = pausedFor(sid) ?? compressor.activeCooldown {
            return "Compression waits \(Int(wait.rounded())) more seconds after a failed summary."
        }
        return compressor.ineffectiveCompressionCount >= 2 ? "Compression isn't freeing enough room. Start a new chat." : nil
    }

    // MARK: Session setup

    private func ensureSession(_ question: String) throws -> String {
        if let sessionID { return sessionID }
        let id = pendingID ?? UUID().uuidString
        try withSessionLock(id) {
            try store.createSession(title: Self.title(question), parentID: parentID, id: id, createdAt: now())
            if try store.messages(sessionID: id, scope: .all).isEmpty {
                try store.append(sessionID: id, role: .system, content: memorySnapshot(), timestamp: now())
            }
        }
        sessionID = id
        pendingID = nil
        parentID = nil
        return id
    }

    /// The memory blocks as they are now. A chat keeps the snapshot from its start (and its last compaction), so the
    /// system prompt stays byte-stable between compactions.
    private func memorySnapshot() -> String {
        let memory = MemoryStore(folder: memoryFolder)
        memory.loadFromDisk()
        return memory.systemPromptBlocks.joined(separator: "\n\n")
    }

    static func title(_ question: String) -> String {
        let line = question.split(whereSeparator: \.isNewline).first.map(String.init) ?? ""
        let title = String(line.prefix(80)).trimmingCharacters(in: .whitespaces)
        return title.isEmpty ? "Chat" : title
    }

    // MARK: Chat state

    private func stateName(_ sid: String) -> String { String(sid.map { $0.isLetter || $0.isNumber || $0 == "-" ? $0 : "_" }) }

    /// The session's `ChatState` file.
    func fleetStateURL(_ sid: String) -> URL {
        store.url.deletingLastPathComponent().appendingPathComponent("fleet-state/\(stateName(sid)).json")
    }

    private func chatState(_ sid: String) -> ChatState {
        (try? Data(contentsOf: fleetStateURL(sid))).flatMap { try? JSONDecoder().decode(ChatState.self, from: $0) } ?? ChatState()
    }

    private func saveChatState(_ sid: String, _ state: ChatState) throws {
        let url = fleetStateURL(sid)
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try JSONEncoder().encode(state).write(to: url, options: .atomic)
    }

    /// Runs `body` holding an exclusive lock on the session, shared by every engine and process using this database.
    private func withSessionLock<T>(_ sid: String, _ body: () throws -> T) throws -> T {
        let url = store.url.deletingLastPathComponent().appendingPathComponent("fleet-state/\(stateName(sid)).lock")
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        let fd = Darwin.open(url.path, O_CREAT | O_RDWR | O_CLOEXEC, 0o600)
        guard fd >= 0 else { return try body() }
        defer { close(fd) }
        flock(fd, LOCK_EX)
        defer { flock(fd, LOCK_UN) }
        return try body()
    }
}

extension ConversationStore {
    /// True for a Mac chat context row: app-written workspace text that search never offers as a match.
    func isAppContextRow(messageID: Int64) throws -> Bool {
        try sync {
            try conn.one("SELECT 1 AS hit FROM messages WHERE id = ? AND role = 'user' AND substr(content, 1, ?) = ?",
                         [messageID, TurnContext.marker.count, TurnContext.marker]) != nil
        }
    }
}

/// The two commands the chat's Claude runs through Assistant: `--search` and `--memory`.
public enum ChatTools {
    public static func search(_ arguments: String, environment: [String: String] = ProcessInfo.processInfo.environment) -> String {
        let url = environment["ASSISTANT_DB"].map { URL(fileURLWithPath: $0) } ?? ConversationStore.defaultURL
        do {
            let store = try ConversationStore(url: url)
            return SessionSearchTool.run(json: Data(arguments.utf8), store: store,
                                         currentSessionID: environment["ASSISTANT_SESSION_ID"]).text
        } catch {
            return SessionSearchResult(.error("session_search could not open the chat history: \(error)")).text
        }
    }

    /// `--search` as the chat's Claude sees it: earlier chats hold other sessions' text, so the results are marked
    /// untrusted.
    public static func searchForChat(_ arguments: String, environment: [String: String] = ProcessInfo.processInfo.environment) -> String {
        Untrusted.wrap(search(arguments, environment: environment))
    }

    public static func memory(_ arguments: String, environment: [String: String] = ProcessInfo.processInfo.environment) -> String {
        let folder = environment["ASSISTANT_MEMORY_DIR"].map { URL(fileURLWithPath: $0, isDirectory: true) } ?? MemoryStore.defaultFolder
        let store = MemoryStore(folder: folder)
        store.loadFromDisk()
        return MemoryTool(store: store).call(argumentsJSON: arguments)
    }
}

/// A summary failure in Claude's own words; the compressor classifies it by this text.
struct ClaudeSummaryFailure: Error, CustomStringConvertible {
    let description: String
}

/// The compressor's summarizer: Haiku through the `claude` binary, with no tools and no user settings.
public enum ClaudeSummarizer {
    static let system = "You write context-compaction handoff summaries. Follow the instructions in the message exactly."

    public static func arguments(model: String) -> [String] {
        ["-p", "--output-format", "json", "--setting-sources", "", "--tools", "", "--permission-mode", "dontAsk",
         "--strict-mcp-config", "--disable-slash-commands", "--system-prompt", system, "--model", model]
    }

    public static func make(model: String = "haiku", timeout: TimeInterval = 300) -> Summarizer {
        { request in try summarize(request.prompt, model: model, timeout: timeout) }
    }

    static func summarize(_ prompt: String, model: String, timeout: TimeInterval, binary: String? = ClaudeSandbox.binary,
                          children: ChildProcesses = .shared) throws -> String {
        guard let claude = binary else { throw SummaryError.noProvider }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: claude)
        p.arguments = arguments(model: model)
        p.currentDirectoryURL = URL(fileURLWithPath: NSTemporaryDirectory())
        let input = Pipe()
        let output = Pipe()
        p.standardInput = input
        p.standardOutput = output
        p.standardError = FileHandle.nullDevice
        do { try p.run() } catch { throw SummaryError.noProvider }
        children.add(p, kind: .background)
        defer { children.remove(p) }
        var timedOut = false
        let killer = DispatchWorkItem { timedOut = true; p.terminate() }
        DispatchQueue.global().asyncAfter(deadline: .now() + timeout, execute: killer)
        DispatchQueue.global().async { ChildInput.feed(input.fileHandleForWriting, Data(prompt.utf8)) }
        let data = output.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        killer.cancel()
        if timedOut { throw SummaryError.timeout("no summary after \(Int(timeout)) seconds") }
        return try reply(data)
    }

    /// The summary text of a `claude -p --output-format json` run.
    static func reply(_ data: Data) throws -> String {
        guard let result = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
            throw SummaryError.invalidResponse(String(String(decoding: data, as: UTF8.self).prefix(200)))
        }
        let text = (result["result"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        if result["is_error"] as? Bool == true { throw ClaudeSummaryFailure(description: text.isEmpty ? "claude failed" : text) }
        if text.isEmpty { throw SummaryError.emptyContent }
        return text
    }
}
