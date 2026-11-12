import Foundation
import XCTest
@testable import SessionCore

// Round 4: archive-instead-of-stub for interrupted calls inside partly answered parallel groups.

private func r4Row(_ seq: Int, _ role: MessageRole, _ text: String, call: String? = nil) -> ConversationMessage {
    ConversationMessage(sessionID: "r4", seq: seq, role: role, content: text, toolName: call == nil ? nil : "shell",
                        toolCallID: call, timestamp: Date(timeIntervalSince1970: 1_800_000_000 + Double(seq)))
}

private func r4Adjacency(_ out: [ConversationMessage]) -> [String] {
    var problems: [String] = []
    for (index, row) in out.enumerated() where row.role == .tool {
        guard let callIndex = out[..<index].lastIndex(where: { $0.isToolCall && $0.toolCallID == row.toolCallID }) else {
            problems.append("orphan result \(row.toolCallID ?? "")")
            continue
        }
        if out[(callIndex + 1)..<index].contains(where: { $0.role != .tool && !$0.isToolCall }) {
            problems.append("text row between call and result \(row.toolCallID ?? "")")
        }
    }
    return problems
}

private func r4Config(_ edit: (inout CompressorConfig) -> Void = { _ in }) -> CompressorConfig { var c = CompressorConfig(model: "claude-opus", provider: "anthropic", contextLength: 20_000); edit(&c); return c }

final class ContextCompressorAdversaryR4Tests: XCTestCase {

    /// Several partly answered groups in a row (interrupted call first, middle and last), then a trailing in-flight
    /// partial group that must stay exactly as it is.
    private func partialGroups() -> [ConversationMessage] {
        var rows = workChat(rounds: 8).messages
        var seq = rows.count + 1
        func add(_ role: MessageRole, _ text: String, _ call: String? = nil) { rows.append(r4Row(seq, role, text, call: call)); seq += 1 }
        add(.user, "Fan out to three groups.")
        add(.assistant, "msg a1", "a1"); add(.assistant, "msg a2", "a2"); add(.tool, "a2 answer", "a2"); add(.assistant, "group a done")
        add(.assistant, "msg b1", "b1"); add(.assistant, "msg b2", "b2"); add(.assistant, "msg b3", "b3")
        add(.tool, "b1 answer", "b1"); add(.tool, "b3 answer", "b3"); add(.assistant, "group b done")
        add(.assistant, "msg c1", "c1"); add(.assistant, "msg c2", "c2"); add(.tool, "c1 answer", "c1"); add(.assistant, "group c done")
        add(.user, "Now the last group.")
        add(.assistant, "msg d1", "d1"); add(.assistant, "msg d2", "d2"); add(.tool, "d2 answer", "d2")
        return rows
    }

    func testMultiplePartialGroupsStayWholeAndInFlightCallsAreUntouched() {
        let rows = partialGroups()
        for firstN in [0, 3] {
            for force in [false, true] {
                let result = ContextCompressor(config: r4Config { $0.protectFirstN = firstN }, summarizer: AdvSummarizer().summarize)
                    .compress(rows, force: force)
                assertPersistable(rows, result, "firstN \(firstN) force \(force)")
                XCTAssertEqual(r4Adjacency(result.messages), [], "firstN \(firstN) force \(force)")
                for id in ["d1", "d2"] {
                    XCTAssertTrue(result.messages.contains { $0.isToolCall && $0.toolCallID == id }, "in-flight call \(id) changed")
                }
                for answer in ["a2 answer", "b1 answer", "b3 answer", "c1 answer", "d2 answer"]
                    where result.archivedSeqs.contains(rows.first { $0.content == answer }!.seq) == false {
                    XCTAssertTrue(result.messages.contains { $0.content == answer }, "\(answer) vanished without being archived")
                }
            }
        }
    }

    func testCompressHereOverPartialGroups() {
        let rows = partialGroups()
        for keep in 1...3 {
            let result = ContextCompressor(config: r4Config(), summarizer: AdvSummarizer().summarize).compressHere(rows, keepLast: keep)
            assertPersistable(rows, result, "keep \(keep)")
            XCTAssertEqual(r4Adjacency(result.messages), [], "keep \(keep)")
        }
    }
}
