import AppKit
import SessionCore
import SwiftUI

struct PanelView: View {
    @ObservedObject var model: Model

    var body: some View {
        let needs = model.result.items(.needsYou)
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("Your sessions").font(.headline)
                Spacer()
                if model.scanning { ProgressView().controlSize(.small) }
                Button { model.refresh() } label: { Image(systemName: "arrow.clockwise") }
                    .buttonStyle(.borderless)
                    .help("Check again")
            }

            if let next = needs.first {
                Button { model.focus(next.workspace) } label: {
                    Label("Take me to the next one: \(next.workspace.title)", systemImage: "arrow.right.circle.fill")
                        .lineLimit(1)
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
                .keyboardShortcut(.return, modifiers: .command)
            } else if model.lastScan == nil {
                Text("Looking through your sessions…").foregroundStyle(.secondary)
            } else {
                Text("Nothing needs you right now.").foregroundStyle(.secondary)
            }

            if let error = model.result.error {
                Text(error).foregroundStyle(.red)
            }

            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    section("Needs you", needs)
                    section("Done, ready to clean up", model.result.items(.done))
                    section("Quiet", model.result.items(.idle))
                }
            }

            Divider()
            ChatView(model: model, listener: model.listener, speaker: model.speaker)

            Divider()
            HStack {
                Text(footer).font(.caption).foregroundStyle(.secondary)
                Spacer()
                Toggle("Open at login", isOn: Binding(get: { model.opensAtLogin }, set: { model.setOpensAtLogin($0) }))
                    .toggleStyle(.checkbox)
                    .font(.caption)
                Button("Quit") { NSApp.terminate(nil) }.buttonStyle(.borderless).font(.caption)
            }
        }
        .padding(14)
        .frame(width: 440, height: 720)
        .onExitCommand { model.stopTalkMode() }
    }

    var footer: String {
        let working = model.result.items(.working).count
        let untracked = model.result.untracked.count
        return "\(working) working · \(untracked) not tracked · ⌃⌥Space opens · ⌃⌥V talks · ⌃⌥R replays"
    }

    @ViewBuilder
    func section(_ title: String, _ items: [Item]) -> some View {
        if !items.isEmpty {
            VStack(alignment: .leading, spacing: 6) {
                Text("\(title) (\(items.count))").font(.subheadline.weight(.semibold)).foregroundStyle(.secondary)
                ForEach(items) { item in
                    Row(item: item, open: { model.focus(item.workspace) }, ask: { model.about = item })
                }
            }
        }
    }
}

struct Row: View {
    let item: Item
    let open: () -> Void
    let ask: () -> Void
    @State private var hovering = false

    var body: some View {
        HStack(alignment: .top, spacing: 2) {
            main
            Button(action: ask) { Image(systemName: "text.bubble") }
                .buttonStyle(.borderless)
                .padding(.top, 9)
                .help("Ask about this session")
        }
    }

    var main: some View {
        Button(action: open) {
            VStack(alignment: .leading, spacing: 3) {
                HStack {
                    Text(item.workspace.title).fontWeight(.medium).lineLimit(1)
                    Spacer()
                    Text("\(item.idleText) ago").font(.caption).foregroundStyle(.secondary)
                }
                Text(item.question).font(.callout).lineLimit(2)
                if !item.goal.isEmpty {
                    Text("Goal: \(item.goal)").font(.caption).foregroundStyle(.secondary).lineLimit(1)
                }
                if !item.pullRequests.isEmpty {
                    Text(item.pullRequests.map { "#\($0.number) \($0.state.lowercased())" }.joined(separator: " · "))
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            .padding(8)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 6).fill(hovering ? Color.primary.opacity(0.08) : .clear))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .help("Open this workspace in cmux")
    }
}

struct ChatView: View {
    @ObservedObject var model: Model
    @ObservedObject var listener: Listener
    @ObservedObject var speaker: Speaker
    @FocusState private var typing: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                if let about = model.about {
                    Label("About \(about.workspace.title)", systemImage: "scope")
                        .font(.caption).lineLimit(1)
                    Button { model.about = nil } label: { Image(systemName: "xmark.circle.fill") }
                        .buttonStyle(.borderless).font(.caption).help("Ask about all sessions")
                } else {
                    Text("Ask or tell it to work with a session. ⌃⌥V to talk.").font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                HistoryMenu(model: model)
                if !model.chat.isEmpty {
                    Button("New chat") { model.newChat() }.buttonStyle(.borderless).font(.caption)
                        .disabled(model.asking || model.compressing)
                }
            }
            HStack(spacing: 12) {
                MicrophoneMenu(listener: listener, prefix: "")
                VoiceMenu(speaker: speaker)
                ModelMenu(chat: model)
                Spacer()
            }
            if !model.chat.isEmpty {
                ContextBar(model: model)
            }
            if model.talkMode {
                HStack(spacing: 6) {
                    Image(systemName: "waveform").foregroundStyle(.red)
                    Text("Talk Mode: I listen again after each answer. Talk over an answer to interrupt it.")
                        .font(.caption).foregroundStyle(.secondary)
                    Spacer()
                    Button("Stop") { model.stopTalkMode() }
                        .buttonStyle(.borderless).font(.caption)
                        .keyboardShortcut(.cancelAction)
                }
            }
            if !model.chat.isEmpty || model.asking || listener.listening || listener.finishing {
                ScrollViewReader { proxy in
                    ScrollView {
                        VStack(alignment: .leading, spacing: 8) {
                            ForEach(model.chat) { turn in
                                Bubble(turn: turn, replay: turn.role == .assistant ? { model.play(turn.text) } : nil)
                                    .id(turn.id)
                            }
                            if listener.listening || listener.finishing {
                                ListeningRow(listener: listener).id("listening")
                            }
                            if model.asking {
                                HStack(spacing: 6) {
                                    ProgressView().controlSize(.small)
                                    Text("Working…").font(.caption).foregroundStyle(.secondary)
                                    Button("Stop") { model.stopAsking() }.buttonStyle(.borderless).font(.caption)
                                }.id("asking")
                            }
                            Color.clear.frame(height: 1).id(Self.bottom)
                        }
                    }
                    .defaultScrollAnchor(.bottom)
                    .frame(height: 200)
                    .onAppear { scrollToBottom(proxy, animated: false) }
                    .onChange(of: scrollKey) { _, _ in scrollToBottom(proxy, animated: true) }
                }
            }
            HStack {
                TextField("Tell workspace 25 to use my token, then report back", text: $model.draft, axis: .vertical)
                    .textFieldStyle(.roundedBorder)
                    .lineLimit(1...4)
                    .focused($typing)
                    .onSubmit(send)
                Button { model.listen() } label: {
                    Image(systemName: listener.listening ? "stop.circle.fill" : "mic.circle.fill")
                        .font(.title2)
                        .foregroundStyle(listener.listening ? Color.red : Color.accentColor)
                }
                .buttonStyle(.borderless)
                .disabled(model.asking)
                .help(listener.listening ? "Stop listening" : "Talk (⌃⌥V)")
                Button { model.talkMode ? model.stopTalkMode() : model.toggleTalkMode() } label: {
                    Image(systemName: model.talkMode ? "waveform.circle.fill" : "waveform.circle")
                        .font(.title2)
                        .foregroundStyle(model.talkMode ? Color.red : Color.accentColor)
                }
                .buttonStyle(.borderless)
                .disabled(model.asking && !model.talkMode)
                .help(model.talkMode ? "Stop Talk Mode (Esc)" : "Talk Mode: talk back and forth hands-free (hold ⌃⌥V)")
                .accessibilityLabel(model.talkMode ? "Stop Talk Mode" : "Talk Mode")
                Button { model.replay() } label: {
                    Image(systemName: "arrow.counterclockwise.circle.fill").font(.title2)
                }
                .buttonStyle(.borderless)
                .disabled(Replay.target(model.chat) == nil || listener.listening)
                .help("Play again (⌃⌥R)")
                .accessibilityLabel("Play again")
                if speaker.speaking {
                    Button { speaker.stop() } label: { Image(systemName: "speaker.slash.circle.fill").font(.title2) }
                        .buttonStyle(.borderless)
                        .help("Stop speaking")
                }
                Button(action: send) { Image(systemName: "arrow.up.circle.fill").font(.title2) }
                    .buttonStyle(.borderless)
                    .disabled(model.asking || model.draft.trimmingCharacters(in: .whitespaces).isEmpty)
                    .help("Send")
            }
        }
        .onChange(of: model.about?.id) { _, _ in typing = true }
    }

    static let bottom = "chat-bottom"

    var scrollKey: [String] {
        Self.scrollKey(chat: model.chat, asking: model.asking, listening: listener.listening || listener.finishing,
                       heard: listener.heard, note: listener.note, soundArrived: listener.soundArrived,
                       modelNote: ListeningText.modelNote(listener.model))
    }

    /// Anything that adds or grows content at the bottom of the chat.
    static func scrollKey(chat: [ChatTurn], asking: Bool, listening: Bool, heard: String, note: String?,
                          soundArrived: Bool, modelNote: String?) -> [String] {
        [String(chat.count), chat.last.map { "\($0.id)" } ?? "", String(asking), String(listening), heard,
         note ?? "", String(soundArrived), modelNote ?? ""]
    }

    func scrollToBottom(_ proxy: ScrollViewProxy, animated: Bool) {
        DispatchQueue.main.async {
            if animated {
                withAnimation(.easeOut(duration: 0.2)) { proxy.scrollTo(Self.bottom, anchor: .bottom) }
            } else {
                proxy.scrollTo(Self.bottom, anchor: .bottom)
            }
        }
    }

    func send() {
        model.ask(model.draft)
        model.draft = ""
    }
}

/// The waveform and live text while listening. Shows "Starting…" from the press until the microphone's first sound.
struct ListeningRow: View {
    @ObservedObject var listener: Listener

    var body: some View {
        VStack(alignment: .trailing, spacing: 4) {
            Waveform(levels: listener.levels)
            Text(ListeningText.label(heard: listener.heard, soundArrived: listener.soundArrived)).font(.callout)
            MicrophoneMenu(listener: listener, prefix: "Using ")
            if let note = listener.note {
                Text(note).font(.caption2).foregroundStyle(.orange)
            }
            if let note = ListeningText.modelNote(listener.model) {
                Text(note).font(.caption2).foregroundStyle(.secondary)
            }
        }.frame(maxWidth: .infinity, alignment: .trailing)
    }
}

struct Bubble: View {
    let turn: ChatTurn
    var replay: (() -> Void)?

    var body: some View {
        VStack(alignment: turn.role == .you ? .trailing : .leading, spacing: 2) {
            Text(rendered)
                .font(turn.role == .step ? .caption : .callout)
                .textSelection(.enabled)
                .padding(8)
                .background(RoundedRectangle(cornerRadius: 8).fill(fill))
                .foregroundStyle(turn.role == .error ? Color.red : Color.primary)
            if turn.cost != nil || replay != nil {
                HStack(spacing: 6) {
                    if let replay {
                        Button(action: replay) { Image(systemName: "play.circle") }
                            .buttonStyle(.borderless)
                            .font(.caption)
                            .help("Play this answer")
                            .accessibilityLabel("Play this answer")
                    }
                    if let cost = turn.cost {
                        Text(String(format: "$%.2f", cost)).font(.caption2).foregroundStyle(.secondary)
                    }
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: turn.role == .you ? .trailing : .leading)
    }

    var fill: Color {
        switch turn.role {
        case .you: Color.accentColor.opacity(0.18)
        case .step: Color.orange.opacity(0.10)
        default: Color.primary.opacity(0.06)
        }
    }

    var rendered: AttributedString {
        (try? AttributedString(markdown: turn.text,
                               options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)))
            ?? AttributedString(turn.text)
    }
}

struct Waveform: View {
    let levels: [Float]

    var body: some View {
        HStack(alignment: .center, spacing: 2) {
            ForEach(levels.indices, id: \.self) { i in
                Capsule()
                    .fill(Color.red.opacity(0.35 + 0.65 * Double(levels[i])))
                    .frame(width: 4, height: 3 + 29 * CGFloat(levels[i]))
            }
        }
        .frame(height: 32)
        .animation(.linear(duration: 0.08), value: levels)
        .accessibilityLabel("Microphone level")
    }
}

struct MicrophoneMenu: View {
    @ObservedObject var listener: Listener
    let prefix: String

    var body: some View {
        Menu {
            ForEach(listener.choices, id: \.self) { name in
                Button { listener.use(name) } label: {
                    if name == listener.current { Label(name, systemImage: "checkmark") } else { Text(name) }
                }
            }
        } label: {
            Label(prefix + (listener.current ?? "No microphone"), systemImage: "mic").font(.caption)
        }
        .menuStyle(.borderlessButton)
        .fixedSize()
        .help("Pick the microphone. Assistant remembers your pick.")
    }
}

struct VoiceMenu: View {
    @ObservedObject var speaker: Speaker

    var downloadNote: String {
        guard speaker.current.map({ Kokoro.npzKey($0.id) != nil }) == true else { return "" }
        switch speaker.kokoroStatus {
        case .downloading(let f): return " (downloading \(Int(f * 100))%)"
        case .failed: return " (download failed)"
        case .notDownloaded: return " (downloads on first use)"
        case .ready: return ""
        }
    }

    var body: some View {
        Menu {
            ForEach(speaker.voices) { voice in
                Button { speaker.use(voice) } label: {
                    if voice.id == speaker.current?.id { Label(voice.label, systemImage: "checkmark") } else { Text(voice.label) }
                }
            }
            Divider()
            Menu("Speed: \(SpeechSpeed.label(speaker.speed))") {
                ForEach(SpeechSpeed.choices, id: \.self) { value in
                    Button { speaker.setSpeed(value) } label: {
                        if value == speaker.speed {
                            Label(SpeechSpeed.label(value), systemImage: "checkmark")
                        } else {
                            Text(SpeechSpeed.label(value))
                        }
                    }
                }
            }
            Button("Get more natural voices…") {
                let settings = URL(string: "x-apple.systempreferences:com.apple.Accessibility-Settings.extension")!
                if !NSWorkspace.shared.open(settings) {
                    NSWorkspace.shared.open(URL(fileURLWithPath: "/System/Applications/System Settings.app"))
                }
            }
        } label: {
            Label((speaker.current?.label ?? "Voice") + downloadNote, systemImage: "speaker.wave.2").font(.caption)
        }
        .menuStyle(.borderlessButton)
        .fixedSize()
        .help("Pick the voice Assistant answers with")
    }
}

struct ModelMenu: View {
    let chat: Model
    @State private var model = Asker.model

    var body: some View {
        Menu {
            ForEach(Asker.models, id: \.0) { id, name in
                Button {
                    chat.useModel(id)
                    model = id
                } label: {
                    if id == model { Label(name, systemImage: "checkmark") } else { Text(name) }
                }
            }
        } label: {
            Label(Asker.models.first { $0.0 == model }?.1 ?? model, systemImage: "cpu").font(.caption)
        }
        .menuStyle(.borderlessButton)
        .fixedSize()
        .help("Model for answers. Haiku is fast and costs about a cent per question.")
    }
}

/// The chats Assistant has stored, most recent first. Pick one to continue it.
struct HistoryMenu: View {
    @ObservedObject var model: Model

    var body: some View {
        if !model.history.isEmpty {
            Menu {
                ForEach(model.history, id: \.session.id) { item in
                    Button(item.session.title) { model.openChat(item.session.id) }
                }
            } label: {
                Label("History", systemImage: "clock.arrow.circlepath").font(.caption)
            }
            .menuStyle(.borderlessButton)
            .fixedSize()
            .disabled(model.asking)
            .help("Open an earlier chat")
        }
    }
}

/// How full the chat's context is, from the model's last reported prompt size, and the Compress controls.
struct ContextBar: View {
    @ObservedObject var model: Model
    @State private var focusing = false
    @State private var focus = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) {
                if let meter = model.meter {
                    ProgressView(value: min(1, Double(meter.tokens) / Double(max(1, meter.window))))
                        .frame(width: 60)
                    Text(meter.text).font(.caption2).foregroundStyle(.secondary)
                } else {
                    Text("Context: shown after the next answer").font(.caption2).foregroundStyle(.secondary)
                }
                Spacer()
                if model.compressing { ProgressView().controlSize(.mini) }
                Menu {
                    Button("Compress") { model.compress(focus: nil) }
                    Button("Compress with focus…") { focusing = true }
                } label: {
                    Label("Compress", systemImage: "arrow.down.right.and.arrow.up.left").font(.caption)
                }
                .menuStyle(.borderlessButton)
                .fixedSize()
                .disabled(model.asking || model.compressing)
                .help("Summarize older turns to free up context")
            }
            if focusing {
                HStack {
                    TextField("What to keep in detail, for example the deploy token", text: $focus)
                        .textFieldStyle(.roundedBorder).font(.caption)
                        .onSubmit(run)
                    Button("Compress", action: run).font(.caption).disabled(focus.trimmingCharacters(in: .whitespaces).isEmpty)
                    Button("Cancel") { focusing = false }.buttonStyle(.borderless).font(.caption)
                }
            }
        }
    }

    func run() {
        model.compress(focus: focus)
        focus = ""
        focusing = false
    }
}
