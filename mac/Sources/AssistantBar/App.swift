import AppKit
import KokoroSwift
import AVFoundation
import Carbon.HIToolbox
import ServiceManagement
import SessionCore
import SwiftUI
import UserNotifications

@main
enum Main {
    static let delegate = AppDelegate()

    static func main() {
        guard let command = AppCommand.parse(CommandLine.arguments) else {
            FileHandle.standardError.write(Data((AppCommand.usage + "\n").utf8))
            exit(2)
        }
        switch command {
        case .app: break
        case .print: return printScan()
        case .sessions: return print(Messenger.list())
        case let .send(ref, message, timeout): return print(Messenger.send(ref, message, timeout: timeout ?? 900))
        case let .wait(ref, timeout): return print(Messenger.wait(ref, timeout: timeout ?? 900))
        case let .render(text, file): exit(render(text, to: URL(fileURLWithPath: file)) ? 0 : 1)
        case .search(let json): return print(ChatTools.search(json))
        case .memory(let json): return print(ChatTools.memory(json))
        case let .ask(question, session, spoken): exit(ask(question, session: session, spoken: spoken) ? 0 : 1)
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

    static func ask(_ question: String, session: String?, spoken: Bool) -> Bool {
        guard let (engine, throwaway) = cliEngine(session: session) else { return false }
        defer { if let throwaway { try? FileManager.default.removeItem(at: throwaway) } }
        let outcome = engine.ask(question, fleet: SessionScanner().scan(), spoken: spoken, step: { print("  · " + $0.text) })
        print(spoken ? Spoken.plain(outcome.reply) : outcome.reply)
        if let cost = outcome.cost { print(String(format: "($%.2f)", cost)) }
        if let meter = outcome.meter { print("  · " + meter.text) }
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
    @Published var about: Item?
    @Published var meter: ContextMeter?
    @Published var history: [ConversationListItem] = []
    @Published var compressing = false
    let listener = Listener()
    let speaker = Speaker()
    private var running: Process?
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
        listener.onHeard = { [weak self] text in self?.ask(text, spoken: true) }
        listener.onProblem = { [weak self] text in self?.chat.append(ChatTurn(role: .error, text: text)) }
        let url = ProcessInfo.processInfo.environment["ASSISTANT_DB"].map { URL(fileURLWithPath: $0) } ?? ConversationStore.defaultURL
        do {
            engine = Main.engine(store: try ConversationStore(url: url),
                                 running: { [weak self] p in DispatchQueue.main.async { self?.running = p } })
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

    func listen() {
        speaker.stop()
        speaker.prepare()
        listener.toggle()
    }

    func ask(_ question: String, spoken: Bool = false) {
        let q = question.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !q.isEmpty, !asking else { return }
        chat.append(ChatTurn(role: .you, text: q))
        guard let engine else {
            chat.append(ChatTurn(role: .error, text: "Chat history is unavailable, so Assistant can't answer."))
            return
        }
        asking = true
        let fleet = result
        let target = about
        let newFocus = target.map(\.id) != sentAbout ? target : nil
        chatQueue.async {
            let focus = newFocus.flatMap { item in
                FileManager.default.contents(atPath: item.transcript)
                    .map { (title: item.workspace.title, exchange: Digest.exchange($0)) }
            }
            let outcome = engine.ask(q, fleet: fleet, focus: focus, spoken: spoken) { step in
                DispatchQueue.main.async { self.chat.append(ChatTurn(role: .step, text: step.text)) }
            }
            let history = engine.sessions()
            DispatchQueue.main.async {
                self.asking = false
                self.running = nil
                self.chat.append(ChatTurn(role: outcome.failed ? .error : .assistant, text: outcome.reply, cost: outcome.cost))
                self.meter = outcome.meter
                self.history = history
                if spoken { self.speaker.say(outcome.reply) }
                if !outcome.failed && newFocus != nil { self.sentAbout = newFocus?.id }
            }
        }
    }

    func stopAsking() {
        running?.terminate()
    }

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
        if CommandLine.arguments.contains("--show") {
            DispatchQueue.main.asyncAfter(deadline: .now() + 1) { self.show() }
        }
        if CommandLine.arguments.contains("--listen") {
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { self.talk() }
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

    func talk() {
        if !popover.isShown { show() }
        model.listen()
    }

    func registerHotKey() {
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, event, _ in
            var id = EventHotKeyID()
            GetEventParameter(event, EventParamName(kEventParamDirectObject), EventParamType(typeEventHotKeyID), nil,
                              MemoryLayout<EventHotKeyID>.size, nil, &id)
            DispatchQueue.main.async { id.id == 2 ? Main.delegate.talk() : Main.delegate.toggle() }
            return noErr
        }, 1, &spec, nil, nil)
        let signature = OSType(0x4153_5354)
        RegisterEventHotKey(UInt32(kVK_Space), UInt32(controlKey | optionKey), EventHotKeyID(signature: signature, id: 1),
                            GetApplicationEventTarget(), 0, &hotKey)
        RegisterEventHotKey(UInt32(kVK_ANSI_V), UInt32(controlKey | optionKey), EventHotKeyID(signature: signature, id: 2),
                            GetApplicationEventTarget(), 0, &talkKey)
    }
}
