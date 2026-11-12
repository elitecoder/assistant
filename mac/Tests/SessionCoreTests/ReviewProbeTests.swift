import Foundation
import XCTest
@testable import SessionCore

/// The fidelity review's probes, as assertions: a context row is never the operator's request, and a summary folded
/// into a kept operator row never strips that row's label.
final class ReviewProbeTests: XCTestCase {
    func testTheTaskSnapshotSkipsAContextRow() {
        let rows = [ConversationMessage(sessionID: "s", seq: 2, role: .user, content: "What needs me?"),
                    ConversationMessage(sessionID: "s", seq: 3, role: .assistant, content: "Alpha."),
                    ConversationMessage(sessionID: "s", seq: 4, role: .user, content: TurnContext.marker + "\n\n# Workspace changes (checked x)\n## Alpha")]
        let snapshot = SummaryPrompt.latestUserTaskSnapshot(rows)
        XCTAssertTrue(snapshot?.contains("'What needs me?'") == true, snapshot ?? "nil")
        XCTAssertFalse(snapshot?.contains("[System:") == true)
    }

    func testAcrossWindowsAndSizesNoSummaryTakesAContextRowOrDropsALabel() throws {
        var contextRowAsTask = 0
        var unlabeledOperator = 0
        var runs = 0
        var compacted = 0
        for window in stride(from: 9_000, through: 30_000, by: 1_500) {
            for size in [50, 200, 600] {
                let folder = FileManager.default.temporaryDirectory.appendingPathComponent("probe-\(UUID().uuidString)")
                defer { try? FileManager.default.removeItem(at: folder) }
                let store = try ConversationStore(url: folder.appendingPathComponent("c.db"))
                let claude = FakeClaude()
                claude.script = { n, inv in
                    let out = String(repeating: "lorem \(n) ", count: size)
                    if n % 2 == 0 {
                        return [Stream.text("Reply \(n) " + out, prompt: FakeClaude.tokens(inv)), Stream.done("Reply \(n) " + out)]
                    }
                    return [Stream.call("c\(n)", "/A/AssistantBar --sessions", prompt: FakeClaude.tokens(inv)), Stream.result("c\(n)", out),
                            Stream.text("Reply \(n)"), Stream.done("Reply \(n)")]
                }
                let chat = ChatEngine(store: store, memoryFolder: folder.appendingPathComponent("m"), tool: "/A/AssistantBar", model: "haiku",
                                      effort: nil, windowFor: { _ in window }, runner: claude.runner, summarizer: { _ in "## Goal\nx" })
                for i in 0..<14 {
                    let a = workspace(1, "Alpha", said: "Said \(i % 3)")
                    _ = chat.ask("Question \(i)", fleet: fleet([a, workspace(2, "Beta", said: "Done.")])) { _ in }
                }
                runs += 1
                let all = try store.messages(sessionID: chat.sessionID!, scope: .all)
                if all.contains(where: \.isSummary) { compacted += 1 }
                if all.contains(where: { $0.isSummary && $0.content.contains("User asked (deterministic, from compacted turns): '[System:") }) {
                    contextRowAsTask += 1
                }
                for invocation in claude.invocations {
                    let blocks = FakeClaude.blocks(invocation)
                    if blocks.contains(where: { $0.contains(CompressionText.summaryEndMarker + "\n\nQuestion")
                        || $0.contains(CompressionText.mergedPriorContextHeader + "\nQuestion") }) {
                        unlabeledOperator += 1
                    }
                }
            }
        }
        XCTAssertGreaterThan(compacted, runs / 5, "the sweep must exercise compaction")
        XCTAssertEqual(contextRowAsTask, 0)
        XCTAssertEqual(unlabeledOperator, 0)
    }
}
