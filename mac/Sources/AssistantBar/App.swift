import AppKit
import KokoroSwift
import AVFoundation
import Carbon.HIToolbox
import Combine
import ServiceManagement
import SessionCore
import Speech
import SwiftUI
import UserNotifications

@main
enum Main {
    static let delegate = AppDelegate()

    static func main() {
        // A child that exits before reading its input mustn't take the app down; ChildInput also guards each pipe.
        signal(SIGPIPE, SIG_IGN)
        guard let command = AppCommand.parse(CommandLine.arguments) else {
            FileHandle.standardError.write(Data((AppCommand.usage + "\n").utf8))
            exit(2)
        }
        switch command {
        case .app: break
        case .print: return printScan()
        case .sessions: return print(Messenger.list())
        case let .send(ref, message, timeout): return print(Messenger.send(ref, message, timeout: timeout ?? Messaging.callLimit))
        case let .wait(ref, timeout): return print(Messenger.wait(ref, timeout: timeout ?? Messaging.callLimit))
        case let .render(text, file): exit(render(text, to: URL(fileURLWithPath: file)) ? 0 : 1)
        case let .transcribe(file, legacy): exit(transcribe(URL(fileURLWithPath: file), legacy: legacy) ? 0 : 1)
        case let .turnEval(files): exit(turnEval(files.map { URL(fileURLWithPath: $0) }) ? 0 : 1)
        case .search(let json): return print(ChatTools.searchForChat(json))
        case .transcript(let ref): return print(Messenger.conversation(ref))
        case .find(let json): return print(Messenger.find(json))
        case .memory(let json): return print(ChatTools.memory(json))
        case let .ask(question, session, spoken, sayTo):
            exit(ask(question, session: session, spoken: spoken, sayTo: sayTo.map { URL(fileURLWithPath: $0) }) ? 0 : 1)
        case let .compress(session, focus):
            guard let (engine, throwaway) = cliEngine(session: session) else { exit(1) }
            print(engine.compressNow(focus: focus))
            if let throwaway { try? FileManager.default.removeItem(at: throwaway) }
            return
        case .toolGuard:
            let decision = ToolGuard.decide(hookInput: FileHandle.standardInput.readDataToEndOfFile(), tool: tool)
            return print(ToolGuard.hookOutput(decision), terminator: "")
        }
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        app.delegate = delegate
        app.run()
    }

    /// Saves `text` spoken by the current Kokoro voice as a WAV file.
    static func render(_ text: String, to url: URL) -> Bool {
        let engine = KokoroEngine()
        let voice = Kokoro.voices[0].id
        var samples: [Float] = []
        var ok = false
        let done = DispatchSemaphore(value: 0)
        let started = Date()
        engine.speak(text, voiceID: voice, isCurrent: { true }, chunk: { samples += $0 }, done: { ok = $0; done.signal() })
        done.wait()
        guard ok, let format = AVAudioFormat(standardFormatWithSampleRate: Double(KokoroTTS.Constants.samplingRate),
                                             channels: 1),
              let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(samples.count)),
              let file = try? AVAudioFile(forWriting: url, settings: format.settings) else {
            print("Kokoro didn't produce audio. Is the voice downloaded?")
            return false
        }
        buffer.frameLength = buffer.frameCapacity
        samples.withUnsafeBufferPointer { buffer.floatChannelData![0].update(from: $0.baseAddress!, count: samples.count) }
        try? file.write(from: buffer)
        print(String(format: "Wrote %.1f seconds of speech in %.1f seconds to %@",
                     Double(samples.count) / Double(KokoroTTS.Constants.samplingRate), Date().timeIntervalSince(started),
                     url.path))
        return true
    }

    /// Prints what the voice recognizer hears in a sound file: SpeechAnalyzer, or SFSpeechRecognizer with `legacy`.
    static func transcribe(_ url: URL, legacy: Bool) -> Bool {
        if legacy, SFSpeechRecognizer.authorizationStatus() != .authorized {
            var asked = false
            SFSpeechRecognizer.requestAuthorization { _ in asked = true }
            while !asked { RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.05)) }
        }
        let started = Date()
        var heard: String?
        if let problem = Listener.transcribe(url, legacy: legacy, vocabulary: Vocabulary.terms, done: { heard = $0 }) {
            print(problem)
            return false
        }
        while heard == nil { RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.05)) }
        print(heard ?? "")
        FileHandle.standardError.write(Data("(\(legacy ? "SFSpeechRecognizer" : "SpeechAnalyzer"), \(milliseconds(since: started)))\n".utf8))
        return Transcription.succeeded(heard)
    }

    /// For each file, when each rule ends the turn and when speech last ended, as tab-separated seconds: the
    /// 2.5-second timer, the shipped rule, and ending early (not shipped), with the text that ended it early.
    static func turnEval(_ files: [URL]) -> Bool {
        TurnModels.shared.download()
        let started = Date()
        while !TurnModels.installed && Date().timeIntervalSince(started) < 120 {
            RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.1))
        }
        guard let models = TurnModels.shared.loaded() else {
            print("Smart Turn isn't available.")
            return false
        }
        print("file\ttimer\tshipped\theld\tearly\tlast_speech\tearly_text")
        for url in files {
            guard let samples = TurnEval.samples(url) else {
                print("\(url.lastPathComponent)\tunreadable")
                continue
            }
            let r = TurnEval.run(samples, models: models, transcribe: transcribePrefix)
            let f = { (t: TimeInterval?) in t.map { String(format: "%.2f", $0) } ?? "-" }
            print("\(url.lastPathComponent)\t\(f(r.timer))\t\(f(r.shipped))\t\(r.held)\t\(f(r.early))\t\(f(r.lastSpeech))\t\(r.earlyText ?? "")")
        }
        return true
    }

    /// What the recognizer makes of 16 kHz audio so far, the way a live question's text would read at that moment.
    static func transcribePrefix(_ samples: [Float]) -> String {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("prefix-\(UUID().uuidString).wav")
        defer { try? FileManager.default.removeItem(at: url) }
        guard let format = AVAudioFormat(standardFormatWithSampleRate: Double(SmartTurn.sampleRate), channels: 1),
              let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(samples.count)) else { return "" }
        buffer.frameLength = buffer.frameCapacity
        samples.withUnsafeBufferPointer { buffer.floatChannelData![0].update(from: $0.baseAddress!, count: samples.count) }
        do {
            // The file is finished when it goes out of scope, before the recognizer reads it.
            let file = try AVAudioFile(forWriting: url, settings: format.settings)
            try file.write(from: buffer)
        } catch {
            return ""
        }
        var heard: String?
        guard Listener.transcribe(url, legacy: false, vocabulary: Vocabulary.terms, done: { heard = $0 }) == nil else { return "" }
        while heard == nil { RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.05)) }
        return heard ?? ""
    }

    static var tool: String { Bundle.main.executablePath ?? CommandLine.arguments[0] }

    static var memoryFolder: URL {
        ProcessInfo.processInfo.environment["ASSISTANT_MEMORY_DIR"].map { URL(fileURLWithPath: $0, isDirectory: true) }
            ?? MemoryStore.defaultFolder
    }

    /// `ASSISTANT_MODEL` and `ASSISTANT_EFFORT` override the model menu (the evals use them); an empty effort runs the
    /// model's default.
    static func engine(store: ConversationStore, running: ((Process) -> Void)? = nil) -> ChatEngine {
        let env = ProcessInfo.processInfo.environment
        let model = env["ASSISTANT_MODEL"] ?? Asker.model
        let effort: String? = env["ASSISTANT_EFFORT"].map { $0.isEmpty ? nil : $0 } ?? Asker.effort(for: model)
        return ChatEngine(store: store, memoryFolder: memoryFolder, tool: tool, model: model, effort: effort,
                          runner: { invocation, onLine in Asker.run(invocation, tool: tool, onLine: onLine, running: running) },
                          summarizer: ClaudeSummarizer.make())
    }

    /// `--session NAME` continues the chat `cli-NAME` in the default history; without it the chat is thrown away.
    /// `ASSISTANT_DB` points both at another database. Returns the throwaway folder to delete afterwards.
    static func cliEngine(session name: String?) -> (ChatEngine, URL?)? {
        var throwaway: URL?
        let url: URL
        if let path = ProcessInfo.processInfo.environment["ASSISTANT_DB"] {
            url = URL(fileURLWithPath: path)
        } else if name != nil {
            url = ConversationStore.defaultURL
        } else {
            let folder = FileManager.default.temporaryDirectory.appendingPathComponent("assistant-ask-\(UUID().uuidString)")
            throwaway = folder
            url = folder.appendingPathComponent("conversations.db")
        }
        do {
            let engine = engine(store: try ConversationStore(url: url))
            if let name { try engine.use("cli-" + name) }
            return (engine, throwaway)
        } catch {
            print("Couldn't open the chat history: \(error)")
            return nil
        }
    }

    /// `sayTo` renders the spoken answer with Kokoro into a WAV file as it streams, instead of playing it, and prints
    /// when the first audio was ready, then when it would have been ready had Assistant waited for the whole reply.
    static func ask(_ question: String, session: String?, spoken: Bool, sayTo: URL?) -> Bool {
        guard let (engine, throwaway) = cliEngine(session: session) else { return false }
        defer { if let throwaway { try? FileManager.default.removeItem(at: throwaway) } }
        let fleet = SessionScanner().scan()
        let renderer = sayTo.map { _ in SpokenRenderer() }
        let started = Date()
        let outcome = engine.ask(question, fleet: fleet, spoken: spoken, speak: renderer.map { r in { r.add($0, at: started) } },
                                 step: { print("  · " + $0.text) })
        let replied = Date().timeIntervalSince(started)
        print(spoken ? Spoken.plain(outcome.reply) : outcome.reply)
        if let cost = outcome.cost { print(String(format: "($%.2f)", cost)) }
        if let meter = outcome.meter { print("  · " + meter.text) }
        if let renderer, let sayTo {
            renderer.finish()
            let whole = renderer.firstChunkSeconds(of: outcome.reply)
            print(String(format: "  · first sentence after %.2f s, first audio after %.2f s (streamed); whole reply after %.2f s, its first audio after %.2f s (waiting)",
                         renderer.firstSentence ?? .nan, renderer.firstAudio ?? .nan, replied, replied + whole))
            guard renderer.write(to: sayTo) else { return false }
        }
        return !outcome.failed
    }

    static func printScan() {
        let result = SessionScanner().scan()
        if let error = result.error { print(error) }
        for item in result.items where item.attention != .working {
            print("[\(item.attention)] \(item.workspace.title)  (\(item.idleText) idle, \(item.workspace.ref))")
            print("  goal:   \(item.goal)".prefix(120))
            print("  claude: \(item.question)".prefix(120))
            if !item.pullRequests.isEmpty {
                print("  PRs:    " + item.pullRequests.map { "#\($0.number) \($0.state)" }.joined(separator: ", "))
            }
        }
        print("working: \(result.items(.working).count), not tracked: \(result.untracked.count)")
    }
}

final class Model: NSObject, ObservableObject {
    @Published var result = ScanResult()
    @Published var scanning = false
    @Published var lastScan: Date?
    @Published var opensAtLogin = SMAppService.mainApp.status == .enabled
    @Published var chat: [ChatTurn] = []
    @Published var asking = false
    /// The answer running was asked by voice and is read aloud as it streams.
    var answeringSpoken = false
    @Published var about: Item?
    @Published var meter: ContextMeter?
    @Published var history: [ConversationListItem] = []
    @Published var compressing = false
    /// The text in the chat box; a question cut off by a disconnected microphone lands here.
    @Published var draft = ""
    let listener = Listener()
    let speaker = Speaker()
    /// Speaks text aloud; tests replace it so nothing plays.
    lazy var talk: (String) -> Void = { [weak self] in self?.speaker.say($0) }
    /// Speaks text after what's already playing; tests replace it too.
    lazy var talkMore: (String) -> Void = { [weak self] in self?.speaker.append($0) }
    var turns = VoiceTurns()
    /// Talk Mode: listen, answer, speak, and listen again, until Stop or Esc.
    @Published var talkMode = false
    /// A note about the microphone Talk Mode watches for interruptions, when it isn't the one picked or is silent.
    @Published var talkNote: String?
    private var silentWatch: AnyCancellable?
    private var loop = TalkLoop()
    private var speakingWatch: AnyCancellable?
    /// Opens the echo-cancelled microphone that notices the operator talking over an answer; tests replace these.
    lazy var watch: (@escaping () -> Void) -> Bool = { [weak self] in self?.speaker.watchForBargeIn($0) ?? false }
    lazy var unwatch: () -> Void = { [weak self] in self?.speaker.stopWatching() }
    lazy var stopTalking: () -> Void = { [weak self] in self?.speaker.stop() }
    lazy var startListening: () -> Void = { [weak self] in self?.listener.start() }
    /// Ends listening without asking what was heard; tests replace it.
    lazy var cancelListening: () -> Void = { [weak self] in self?.listener.cancel() }
    private var sentAbout: String?
    private var engine: ChatEngine?
    private let chatQueue = DispatchQueue(label: "assistant.chat")
    var onScan: ((ScanResult) -> Void)?
    var onFocus: (() -> Void)?

    private let scanner = SessionScanner()
    private let queue = DispatchQueue(label: "assistant.scan")

    func refresh() {
        guard !scanning else { return }
        scanning = true
        queue.async {
            let r = self.scanner.scan()
            DispatchQueue.main.async {
                self.result = r
                self.listener.vocabulary = Vocabulary.contextualStrings(titles: r.items.map(\.workspace.title)
                    + r.untracked.map(\.title))
                self.scanning = false
                self.lastScan = Date()
                self.onScan?(r)
            }
        }
    }

    func focus(_ ws: Workspace) {
        onFocus?()
        queue.async { SessionScanner.focus(ws) }
        NSWorkspace.shared.openApplication(at: URL(fileURLWithPath: "/Applications/cmux.app"),
                                           configuration: NSWorkspace.OpenConfiguration())
    }

    override init() {
        super.init()
        listener.onHeard = { [weak self] text in self?.heard(text) }
        listener.onProblem = { [weak self] text in
            self?.chat.append(ChatTurn(role: .error, text: text))
            self?.step { $0.nothingHeard() }
        }
        listener.onInterrupted = { [weak self] text, note in
            guard let self else { return }
            if !text.isEmpty { self.draft = text }
            self.chat.append(ChatTurn(role: .error, text: note))
            self.step { $0.micLost() }
        }
        listener.onEnded = { [weak self] in self?.listeningEnded() }
        speaker.mayTalk = { [weak self] in self?.listener.listening != true }
        silentWatch = speaker.$watchSilent.removeDuplicates().sink { [weak self] silent in
            DispatchQueue.main.async { self?.refreshTalkNote(silent: silent) }
        }
        speakingWatch = speaker.$speaking.removeDuplicates().dropFirst().sink { [weak self] speaking in
            DispatchQueue.main.async { self?.speechChanged(speaking) }
        }
        let url = ProcessInfo.processInfo.environment["ASSISTANT_DB"].map { URL(fileURLWithPath: $0) } ?? ConversationStore.defaultURL
        do {
            engine = Main.engine(store: try ConversationStore(url: url))
        } catch {
            chat.append(ChatTurn(role: .error, text: "Chat history is unavailable: \(error)"))
        }
        reload { try $0.openLatest() }
    }

    /// Runs `change` on the chat queue, then shows the engine's chat, meter, and history.
    private func reload(_ change: @escaping (ChatEngine) throws -> Void) {
        guard let engine else { return }
        chatQueue.async {
            try? change(engine)
            let turns = engine.displayTurns()
            let meter = engine.meter
            let history = engine.sessions()
            DispatchQueue.main.async {
                self.chat = turns
                self.meter = meter
                self.history = history
            }
        }
    }

    /// The microphone button and Control-Option-V. Pressed during a spoken answer, even in a silent gap between its
    /// sentences, it silences the rest of that answer and ends its run.
    func listen() {
        if speakingNow() || answeringSpoken { turns.mute() }
        stopTalking()
        // Interrupting by key works like interrupting by voice: the run ends, so the new question doesn't wait for it.
        if asking && answeringSpoken && !listener.listening { endRun(TalkLoop.interruptedReason, kinds: [.chat]) }
        if !listener.listening { step { $0.listenedByKey() } }
        toggleListening()
    }

    /// Starts or ends a question; tests replace it so no microphone opens.
    lazy var toggleListening: () -> Void = { [weak self] in
        self?.speaker.prepare()
        self?.listener.toggle()
    }

    /// "Stop speaking": silence this answer, including the sentences Claude hasn't written yet.
    func stopSpeaking() {
        turns.mute()
        stopTalking()
    }

    /// Says `text` again with the current voice and speed, from the start. Ignored while listening, so the answer
    /// doesn't talk into the microphone.
    func play(_ text: String) {
        if case .speak(let text) = turns.say(text, listening: listener.listening, isReply: false) { talk(text) }
    }

    func toggleTalkMode() { step { $0.toggle() } }

    /// The Stop button and Esc end Talk Mode and drop the questions waiting for the answer.
    func stopTalkMode() {
        turns.clearQueue()
        guard loop.on else { return }
        step { $0.stop() }
        if listener.listening { cancelListening() }
        stopSpeaking()
    }

    /// Steps Talk Mode's loop and carries out what it says.
    private func step(_ change: (inout TalkLoop) -> [TalkLoop.Action]) {
        for action in change(&loop) {
            switch action {
            case .listen:
                if !listener.listening { startListening() }
            case .interrupt:
                stopSpeaking()
                if asking { endRun(TalkLoop.interruptedReason, kinds: [.chat]) }
                if !listener.listening { startListening() }
            case .watch:
                if !watch({ [weak self] in self?.step { $0.bargedIn() } }) {
                    chat.append(ChatTurn(role: .step, text: "Talking over the answer won't stop it: voice processing isn't available."))
                }
            case .stopWatching:
                unwatch()
            case .exit(let note):
                if let note { chat.append(ChatTurn(role: .step, text: note)) }
            }
        }
        talkMode = loop.on
        refreshTalkNote(silent: speaker.watchSilent)
    }

    func refreshTalkNote(silent: Bool) {
        talkNote = loop.on ? TalkModeNote.text(defaultInput: listener.defaultInput, picked: listener.current, silent: silent) : nil
    }

    /// The answer started or stopped playing. Talk Mode listens again once it stops and the answer is complete.
    func speechChanged(_ speaking: Bool) {
        if speaking { step { $0.startedSpeaking() } } else if !asking { step { $0.finishedSpeaking() } }
    }

    /// A spoken question: asked now, or after the answer that's running.
    func heard(_ question: String) {
        step { $0.heard() }
        switch turns.heard(question, asking: asking) {
        case .ask(let q): askNow(q)
        case .queued(let note):
            chat.append(ChatTurn(role: .you, text: question))
            chat.append(ChatTurn(role: .step, text: note))
        }
    }

    /// Asks a spoken question right away; tests replace it so no `claude` runs.
    lazy var askNow: (String) -> Void = { [weak self] in self?.ask($0, spoken: true) }

    /// Reads a spoken answer, or keeps it until listening ends so it doesn't play into the microphone.
    func answered(_ reply: String) {
        switch turns.say(reply, listening: listener.listening, isReply: true) {
        case .speak(let text): talk(text)
        case .deferred: chat.append(ChatTurn(role: .step, text: VoiceTurns.deferredNote))
        case .skipped: break
        }
    }

    /// One sentence of a spoken answer that's still being written.
    func spokenPart(_ sentence: String) {
        switch turns.part(sentence, listening: listener.listening) {
        case .start(let text): talk(text)
        case .add(let text): talkMore(text)
        case .deferred(let note): if note { chat.append(ChatTurn(role: .step, text: VoiceTurns.deferredNote)) }
        case .dropped: break
        }
    }

    func listeningEnded() {
        guard !listener.listening, let reply = turns.listeningEnded() else { return }
        talk(reply)
    }

    /// Plays the last complete answer again. During a streamed answer that's the previous one, and the rest of the
    /// streaming answer isn't read after it.
    func replay() {
        guard let text = Replay.target(chat) else { return }
        if answeringSpoken { turns.mute() }
        play(text)
    }

    func ask(_ question: String, spoken: Bool = false, shown: Bool = false) {
        let q = question.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !q.isEmpty, !asking else { return }
        if !shown { chat.append(ChatTurn(role: .you, text: q)) }
        guard let engine else {
            chat.append(ChatTurn(role: .error, text: "Chat history is unavailable, so Assistant can't answer."))
            return
        }
        asking = true
        answeringSpoken = spoken
        if spoken { turns.newReply() }
        let speak: ((String) -> Void)? = spoken ? { [weak self] sentence in
            DispatchQueue.main.async { self?.spokenPart(sentence) }
        } : nil
        let fleet = result
        let target = about
        let newFocus = target.map(\.id) != sentAbout ? target : nil
        chatQueue.async {
            let focus = newFocus.flatMap { item in
                FileManager.default.contents(atPath: item.transcript)
                    .map { (title: item.workspace.title, exchange: Digest.exchange($0)) }
            }
            let outcome = engine.ask(q, fleet: fleet, focus: focus, spoken: spoken, speak: speak) { step in
                DispatchQueue.main.async { self.chat.append(ChatTurn(role: .step, text: step.text)) }
            }
            let history = engine.sessions()
            DispatchQueue.main.async {
                self.history = history
                if !outcome.failed && newFocus != nil { self.sentAbout = newFocus?.id }
                self.answerCompleted(outcome, spoken: spoken)
            }
        }
    }

    /// An answer is done: show it in full (even if it was stopped while being read), read it if nothing streamed,
    /// then ask the next question waiting. A waiting question is asked before Talk Mode hears that the answer is
    /// over, so it doesn't listen again while that question is being answered.
    func answerCompleted(_ outcome: TurnOutcome, spoken: Bool) {
        asking = false
        answeringSpoken = false
        if let partial = outcome.partial { chat.append(ChatTurn(role: .assistant, text: partial)) }
        chat.append(ChatTurn(role: outcome.failed ? .error : .assistant, text: outcome.reply, cost: outcome.cost))
        meter = outcome.meter
        if spoken && outcome.spokenSentences == 0 { answered(outcome.reply) }
        if let next = turns.answerFinished() {
            askNext(next)
        } else if !speakingNow() {
            speechChanged(false)
        }
    }

    /// Asks a question that waited for the last answer; tests replace it so no `claude` runs.
    lazy var askNext: (String) -> Void = { [weak self] in self?.ask($0, spoken: true, shown: true) }

    /// Whether the answer is still playing; tests replace it.
    lazy var speakingNow: () -> Bool = { [weak self] in self?.speaker.speaking ?? false }

    /// Stop: ends the chat's `claude`, the summarizer, and everything they started, such as `--send` and `--wait`.
    /// Reading the process table can take a moment, so it happens off the main thread.
    func stopAsking() {
        turns.clearQueue()
        endRun(ChildProcesses.stoppedReason, kinds: [.chat, .background])
    }

    /// Ends the running answer's processes of `kinds` off the main thread; the stored reply says `reason`. Talking
    /// over an answer ends only the chat's run; Stop ends the summarizer too.
    private func endRun(_ reason: String, kinds: Set<ChildProcesses.Kind>) {
        cancelPending(reason)
        let end = endChildren
        DispatchQueue.global(qos: .userInitiated).async { end(reason, kinds) }
    }

    /// Keeps an ask that hasn't started its `claude` yet (compacting, or about to launch) from starting it.
    lazy var cancelPending: (String) -> Void = { [weak self] in self?.engine?.cancelPending($0) }

    /// Ends the processes the app started, recording why; tests replace it.
    lazy var endChildren: (String, Set<ChildProcesses.Kind>) -> Void = { ChildProcesses.shared.endAll(reason: $0, kinds: $1) }

    /// Ignored while an answer is running, so its reply can't land in the next chat.
    func newChat() {
        guard !asking, !compressing else { return }
        chat = []
        sentAbout = nil
        about = nil
        meter = nil
        reload { $0.newChat() }
    }

    func openChat(_ id: String) {
        guard !asking, !compressing else { return }
        sentAbout = nil
        about = nil
        reload { try $0.open(id) }
    }

    func useModel(_ id: String) {
        Asker.model = id
        guard let engine else { return }
        chatQueue.async {
            engine.setModel(id, effort: Asker.effort(for: id))
            let meter = engine.meter
            DispatchQueue.main.async { self.meter = meter }
        }
    }

    /// Compresses the chat now, keeping `focus` in more detail when given.
    func compress(focus: String?) {
        guard let engine, !asking, !compressing else { return }
        compressing = true
        chatQueue.async {
            let note = engine.compressNow(focus: focus)
            let meter = engine.meter
            DispatchQueue.main.async {
                self.compressing = false
                self.chat.append(ChatTurn(role: .step, text: note))
                self.meter = meter
            }
        }
    }

    func setOpensAtLogin(_ on: Bool) {
        do {
            if on { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
        } catch {
            NSLog("Assistant: login item change failed: \(error)")
        }
        opensAtLogin = SMAppService.mainApp.status == .enabled
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    let model = Model()
    var statusItem: NSStatusItem!
    let popover = NSPopover()
    var notified = Set<String>()
    var timer: Timer?
    var hotKey: EventHotKeyRef?
    var talkKey: EventHotKeyRef?
    var replayKey: EventHotKeyRef?

    func applicationWillTerminate(_ note: Notification) {
        ChildProcesses.shared.endAll()
    }

    func applicationDidFinishLaunching(_ note: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.action = #selector(toggle)
        statusItem.button?.target = self
        updateIcon()

        popover.behavior = .transient
        popover.contentViewController = NSHostingController(rootView: PanelView(model: model))

        model.onScan = { [weak self] r in
            self?.updateIcon()
            self?.notifyNew(r)
        }
        model.onFocus = { [weak self] in self?.popover.performClose(nil) }
        model.refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 120, repeats: true) { [weak self] _ in self?.model.refresh() }

        if Bundle.main.bundleIdentifier != nil {
            UNUserNotificationCenter.current().delegate = self
            UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
        }
        registerHotKey()
        model.listener.prepare()
        if CommandLine.arguments.contains("--show") {
            DispatchQueue.main.asyncAfter(deadline: .now() + 1) { self.show() }
        }
        if CommandLine.arguments.contains("--listen") {
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { self.talk() }
        } else if CommandLine.arguments.contains("--talk") {
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
                if !self.popover.isShown { self.show() }
                self.model.toggleTalkMode()
            }
        }
    }

    @objc func toggle() {
        if popover.isShown {
            popover.performClose(nil)
        } else {
            show()
        }
    }

    func show() {
        guard let button = statusItem.button else { return }
        model.refresh()
        model.listener.prepare()
        NSApp.activate(ignoringOtherApps: true)
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        popover.contentViewController?.view.window?.makeKey()
    }

    func updateIcon() {
        let count = model.result.items(.needsYou).count
        let symbol = count > 0 ? "person.crop.circle.badge.exclamationmark" : "person.crop.circle.badge.checkmark"
        let image = NSImage(systemSymbolName: symbol, accessibilityDescription: "Assistant")
        image?.isTemplate = true
        statusItem.button?.image = image
        statusItem.button?.imagePosition = .imageLeading
        statusItem.button?.title = count > 0 ? " \(count)" : ""
        statusItem.button?.toolTip = count > 0 ? "\(count) sessions need you (⌃⌥Space)" : "Nothing needs you (⌃⌥Space)"
    }

    func notifyNew(_ r: ScanResult) {
        let needs = r.items(.needsYou)
        let fresh = needs.filter { !notified.contains($0.id) }
        notified = Set(needs.map(\.id))
        guard !fresh.isEmpty, Bundle.main.bundleIdentifier != nil else { return }
        let content = UNMutableNotificationContent()
        content.title = fresh.count == 1 ? "A session needs you" : "\(fresh.count) sessions need you"
        content.body = fresh.prefix(4).map { "\($0.workspace.title): \($0.question)" }.joined(separator: "\n")
        content.sound = .default
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: UUID().uuidString,
                                                                     content: content, trigger: nil))
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler done: @escaping () -> Void) {
        DispatchQueue.main.async { self.show() }
        done()
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler done: @escaping (UNNotificationPresentationOptions) -> Void) {
        done([.banner, .sound])
    }

    private var talkPressedAt: Date?

    /// Control-Option-V: a press talks; holding it for `TalkLoop.longPress` also turns on Talk Mode.
    func talkPressed() {
        talkPressedAt = Date()
        talk()
    }

    func talkReleased() {
        guard let pressed = talkPressedAt else { return }
        talkPressedAt = nil
        if TalkLoop.isLongPress(held: Date().timeIntervalSince(pressed)), !model.talkMode, model.listener.listening {
            model.toggleTalkMode()
        }
    }

    func talk() {
        if !popover.isShown { show() }
        model.listen()
    }

    func registerHotKey() {
        var specs = [EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed)),
                     EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyReleased))]
        InstallEventHandler(GetApplicationEventTarget(), { _, event, _ in
            var id = EventHotKeyID()
            GetEventParameter(event, EventParamName(kEventParamDirectObject), EventParamType(typeEventHotKeyID), nil,
                              MemoryLayout<EventHotKeyID>.size, nil, &id)
            let released = GetEventKind(event) == UInt32(kEventHotKeyReleased)
            DispatchQueue.main.async {
                switch (id.id, released) {
                case (2, false): Main.delegate.talkPressed()
                case (2, true): Main.delegate.talkReleased()
                case (3, false): Main.delegate.model.replay()
                case (1, false): Main.delegate.toggle()
                default: break
                }
            }
            return noErr
        }, 2, &specs, nil, nil)
        let signature = OSType(0x4153_5354)
        RegisterEventHotKey(UInt32(kVK_Space), UInt32(controlKey | optionKey), EventHotKeyID(signature: signature, id: 1),
                            GetApplicationEventTarget(), 0, &hotKey)
        RegisterEventHotKey(UInt32(kVK_ANSI_V), UInt32(controlKey | optionKey), EventHotKeyID(signature: signature, id: 2),
                            GetApplicationEventTarget(), 0, &talkKey)
        RegisterEventHotKey(UInt32(kVK_ANSI_R), UInt32(controlKey | optionKey), EventHotKeyID(signature: signature, id: 3),
                            GetApplicationEventTarget(), 0, &replayKey)
    }
}

/// Renders streamed sentences with Kokoro into memory, for `--ask --say-to`, timing the first sentence and audio.
final class SpokenRenderer {
    private let engine = KokoroEngine()
    private let voice: String
    private let lock = NSLock()
    private let group = DispatchGroup()
    private var samples: [Float] = []
    private(set) var firstSentence: TimeInterval?
    private(set) var firstAudio: TimeInterval?

    init() {
        voice = UserDefaults.standard.string(forKey: Speaker.preferenceKey).flatMap { Kokoro.npzKey($0) != nil ? $0 : nil }
            ?? Kokoro.voices[0].id
        _ = firstChunkSeconds(of: "Getting ready.")
    }

    func add(_ sentence: String, at start: Date) {
        lock.withLock { if firstSentence == nil { firstSentence = Date().timeIntervalSince(start) } }
        group.enter()
        engine.speak(Spoken.plain(sentence), voiceID: voice, isCurrent: { true }, chunk: { [self] chunk in
            lock.withLock {
                if firstAudio == nil { firstAudio = Date().timeIntervalSince(start) }
                samples += chunk
            }
        }, done: { [group] _ in group.leave() })
    }

    func finish() { group.wait() }

    /// How long Kokoro takes to produce the first audio of `text`, with the model already loaded.
    func firstChunkSeconds(of text: String) -> TimeInterval {
        let started = Date()
        var first: TimeInterval?
        let done = DispatchSemaphore(value: 0)
        engine.speak(Spoken.plain(text), voiceID: voice, isCurrent: { first == nil },
                     chunk: { _ in if first == nil { first = Date().timeIntervalSince(started) } }, done: { _ in done.signal() })
        done.wait()
        return first ?? .nan
    }

    func write(to url: URL) -> Bool {
        let all = lock.withLock { samples }
        guard let format = AVAudioFormat(standardFormatWithSampleRate: Double(KokoroTTS.Constants.samplingRate), channels: 1),
              let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(max(1, all.count))),
              let file = try? AVAudioFile(forWriting: url, settings: format.settings) else { return false }
        buffer.frameLength = AVAudioFrameCount(all.count)
        all.withUnsafeBufferPointer { buffer.floatChannelData![0].update(from: $0.baseAddress!, count: all.count) }
        return (try? file.write(from: buffer)) != nil
    }
}
