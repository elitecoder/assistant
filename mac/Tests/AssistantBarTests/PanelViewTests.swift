import AppKit
import SwiftUI
import XCTest
@testable import AssistantBar
@testable import SessionCore

final class PanelViewTests: XCTestCase {
    func item(_ id: String, _ title: String, said: String, attention: Attention, idle: Double,
              prs: [(number: Int, state: String)] = []) -> Item {
        var s = SessionSummary()
        s.goal = "Seeing this error in dashboards:\nfatal coediting state"
        s.lastSaid = said
        let ws = Workspace(id: id, ref: "workspace:\(id)", title: title, directory: nil, submittedAt: nil,
                           submittedMessage: nil)
        return Item(workspace: ws, transcript: "/tmp/\(id).jsonl", summary: s, attention: attention, idleMinutes: idle, pullRequests: prs)
    }

    @MainActor
    func testPanelPutsTheNextSessionThatNeedsYouFirst() throws {
        setenv("ASSISTANT_DB", NSTemporaryDirectory() + "panel-\(UUID().uuidString)/conversations.db", 1)
        defer { unsetenv("ASSISTANT_DB") }
        let model = Model()
        var r = ScanResult()
        r.items = [
            item("1", "Coediting fatal error", said: "Which do you want first?", attention: .needsYou, idle: 660),
            item("2", "Version bump conflicts", said: "Should the Action use your token?", attention: .needsYou,
                 idle: 18_720),
            item("3", "Spanish doc names", said: "Check it in Spanish.", attention: .done, idle: 600,
                 prs: [(21228, "MERGED")]),
            item("4", "CI fix", said: "Run passed.", attention: .idle, idle: 1_140),
        ]
        model.result = r
        model.lastScan = Date()
        let panel = PanelView(model: model)
        let list = VStack(alignment: .leading, spacing: 14) {
            panel.section("Needs you", r.items(.needsYou))
            panel.section("Done, ready to clean up", r.items(.done))
            panel.section("Quiet", r.items(.idle))
        }.padding(14).frame(width: 440)
        let chat = VStack(spacing: 8) {
            Bubble(turn: ChatTurn(role: .you, text: "What do you need from me on the version bump?"))
            Bubble(turn: ChatTurn(role: .assistant, text: "**Version Bump [25]** waits on one choice: `your token` or a bot app.",
                                  cost: 0.24))
        }.padding(14).frame(width: 440)
        for (name, view) in [("assistant-panel", AnyView(panel)), ("assistant-list", AnyView(list)),
                             ("assistant-chat", AnyView(chat))] {
            let renderer = ImageRenderer(content: view)
            renderer.scale = 2
            let image = try XCTUnwrap(renderer.nsImage)
            XCTAssertEqual(image.size.width, 440)
            let png = try XCTUnwrap(NSBitmapImageRep(data: try XCTUnwrap(image.tiffRepresentation))?
                .representation(using: .png, properties: [:]))
            try png.write(to: URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("\(name).png"))
        }
        XCTAssertEqual(r.items(.needsYou).first?.workspace.title, "Coediting fatal error")
    }

    /// A restarted app reopens the latest stored chat with its context meter and history, and the panel shows them.
    @MainActor
    func testTheAppReopensTheLatestChatWithItsMeter() throws {
        let url = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("reopen-\(UUID().uuidString)/conversations.db")
        let store = try ConversationStore(url: url)
        let session = try store.createSession(title: "What needs me?")
        try store.append(sessionID: session.id, role: .system, content: "")
        try store.append(sessionID: session.id, role: .user, content: TurnContext.row(fleet: "# Workspaces (checked x)", focus: nil)!)
        let question = try store.append(sessionID: session.id, role: .user, content: "What needs me?")
        try store.append(sessionID: session.id, role: .assistant, content: "**Alpha [1]** waits on a merge call.")
        let live = try store.messages(sessionID: session.id, scope: .active)
        let summary = ConversationMessage(sessionID: session.id, seq: 0, role: .user,
                                          content: CompressionText.summaryPrefix + " Earlier turns.\n\n" + CompressionText.summaryEndMarker,
                                          isSummary: true)
        let applied = try store.applyCompaction(sessionID: session.id, newActive: live + [summary],
                                      watermark: live.map(\.seq).max() ?? 0)
        try store.append(sessionID: session.id, role: .assistant, content: "Compressed earlier turns.")
        try store.recordUsage(sessionID: session.id, promptTokens: 31_852,
                              atSeq: try XCTUnwrap(applied.oldToNew[question.seq]))
        setenv("ASSISTANT_DB", url.path, 1)
        defer { unsetenv("ASSISTANT_DB") }

        let model = Model()
        let loaded = expectation(for: NSPredicate { _, _ in !model.chat.isEmpty && model.meter != nil }, evaluatedWith: nil)
        wait(for: [loaded], timeout: 10)
        XCTAssertEqual(model.chat.map(\.text), ["What needs me?", "**Alpha [1]** waits on a merge call.", ChatHistory.compactedNote,
                                                "Compressed earlier turns."])
        XCTAssertEqual(model.meter?.window, ContextWindow.tokens(for: Asker.model))
        XCTAssertGreaterThan(model.meter?.tokens ?? 0, 31_852)
        XCTAssertEqual(model.history.map(\.session.title), ["What needs me?"])

        model.asking = true
        model.newChat()
        model.openChat("other")
        XCTAssertEqual(model.chat.count, 4, "New chat and History wait for the running answer")
        XCTAssertNotNil(model.meter)
        model.asking = false

        let panel = AnyView(ChatView(model: model, listener: model.listener, speaker: model.speaker).padding(14).frame(width: 440))
        let bubbles = AnyView(VStack(spacing: 8) { ForEach(model.chat) { Bubble(turn: $0) } }.padding(14).frame(width: 440))
        for (name, view) in [("assistant-chat-reopened", panel), ("assistant-chat-reopened-bubbles", bubbles)] {
            let renderer = ImageRenderer(content: view)
            renderer.scale = 2
            let image = try XCTUnwrap(renderer.nsImage)
            XCTAssertEqual(image.size.width, 440)
            let png = try XCTUnwrap(NSBitmapImageRep(data: try XCTUnwrap(image.tiffRepresentation))?.representation(using: .png, properties: [:]))
            try png.write(to: URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("\(name).png"))
        }
    }

    /// The listening row says "Starting…" from the press until sound arrives, and only answers get a play button.
    @MainActor
    func testTheListeningRowAndReplayButtonsRender() throws {
        setenv("ASSISTANT_DB", NSTemporaryDirectory() + "listen-\(UUID().uuidString)/conversations.db", 1)
        defer { unsetenv("ASSISTANT_DB") }
        let model = Model()
        model.chat = [ChatTurn(role: .you, text: "What needs me?"),
                      ChatTurn(role: .assistant, text: "**Alpha [1]** waits on a merge call.", cost: 0.01)]
        model.listener.listening = true
        let view = AnyView(VStack(spacing: 8) {
            ForEach(model.chat) { turn in
                Bubble(turn: turn, replay: turn.role == .assistant ? { model.play(turn.text) } : nil)
            }
            ListeningRow(listener: model.listener)
        }.padding(14).frame(width: 440))
        let renderer = ImageRenderer(content: view)
        renderer.scale = 2
        let image = try XCTUnwrap(renderer.nsImage)
        XCTAssertEqual(image.size.width, 440)
        let png = try XCTUnwrap(NSBitmapImageRep(data: try XCTUnwrap(image.tiffRepresentation))?.representation(using: .png, properties: [:]))
        try png.write(to: URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("assistant-listening.png"))
        XCTAssertEqual(ListeningText.label(heard: model.listener.heard, soundArrived: model.listener.soundArrived), "Starting…")
        XCTAssertEqual(Replay.target(model.chat), "**Alpha [1]** waits on a merge call.")
    }
}
