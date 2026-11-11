import Foundation
import XCTest
@testable import SessionCore

final class AskTests: XCTestCase {
    func testExchangeKeepsNewestTurnsWithinTheCap() {
        let data = transcript([
            user("first ask", at: "2026-11-01T10:00:00.000Z"),
            assistant("first reply", at: "2026-11-01T10:01:00.000Z"),
            user([["type": "tool_result", "content": "noise"]], at: "2026-11-01T10:01:30.000Z"),
            user("second ask", at: "2026-11-01T10:02:00.000Z"),
            assistant("Should I merge?", at: "2026-11-01T10:03:00.000Z"),
        ])
        XCTAssertEqual(Digest.exchange(data),
                       "OPERATOR: first ask\n\nCLAUDE: first reply\n\nOPERATOR: second ask\n\nCLAUDE: Should I merge?")
        XCTAssertEqual(Digest.exchange(data, cap: 45), "OPERATOR: second ask\n\nCLAUDE: Should I merge?")
    }

    func testFleetDigestNamesEachWorkspaceWithItsTranscriptAndDecision() {
        var s = SessionSummary()
        s.goal = "Stop version fights"
        s.lastSaid = "Should the Action use your token?"
        let ws = Workspace(id: "W", ref: "workspace:25", title: "Version Bump [25]", directory: "/r", submittedAt: nil,
                           submittedMessage: nil)
        var r = ScanResult()
        r.items = [Item(workspace: ws, transcript: "/t/w.jsonl", summary: s, attention: .needsYou, idleMinutes: 120,
                        pullRequests: [(number: 9, state: "MERGED")])]
        r.untracked = [Workspace(id: "U", ref: "workspace:9", title: "Spawned run", directory: "/x", submittedAt: nil,
                                 submittedMessage: nil)]
        let digest = Digest.fleet(r)
        XCTAssertTrue(digest.contains("## Version Bump [25] (workspace:25)"))
        XCTAssertTrue(digest.contains("- status: needs the operator, quiet for 2h"))
        XCTAssertTrue(digest.contains("- Claude's last reply: Should the Action use your token?"))
        XCTAssertTrue(digest.contains("- pull requests: #9 MERGED"))
        XCTAssertTrue(digest.contains("- transcript: /t/w.jsonl"))
        XCTAssertTrue(digest.contains("- Spawned run (workspace:9), folder /x"))
    }

    func testPromptSendsFleetOnlyWhenGivenAndQuestionLast() {
        XCTAssertEqual(Asker.prompt(question: "why?", fleet: nil, focus: nil), "# Question\n\nwhy?")
        let p = Asker.prompt(question: "why?", fleet: "FLEET", focus: (title: "T [1]", exchange: "EX"))
        XCTAssertEqual(p, "FLEET\n\n# Recent conversation in T [1]\n\nEX\n\n# Question\n\nwhy?")
    }

    func testParseReadsTheFinalJSONLine() {
        let ok = Asker.parse(Data("noise\n{\"result\":\"It waits on you.\",\"session_id\":\"s1\",\"total_cost_usd\":0.2,\"is_error\":false}\n".utf8))
        XCTAssertEqual(ok, Reply(text: "It waits on you.", sessionID: "s1", cost: 0.2, failed: false))
        XCTAssertTrue(Asker.parse(Data("command not found".utf8)).failed)
        XCTAssertTrue(Asker.parse(Data("{\"result\":\"limit\",\"is_error\":true}".utf8)).failed)
    }
}

final class AskCommandTests: XCTestCase {
    func arguments(resume: String) throws -> [String] {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/zsh")
        p.arguments = ["-fc", "claude() { printf '%s\\n' \"$@\" }; " + Asker.command]
        p.environment = ["ASSISTANT_INSTRUCTIONS": "Be brief. Use \"quotes\".", "ASSISTANT_RESUME": resume]
        let out = Pipe()
        p.standardOutput = out
        try p.run()
        let data = out.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        return String(decoding: data, as: UTF8.self).split(separator: "\n").map(String.init)
    }

    func testFollowUpPassesResumeAndIdAsSeparateArguments() throws {
        let args = try arguments(resume: "abc-123")
        XCTAssertEqual(Array(args.suffix(2)), ["--resume", "abc-123"])
        XCTAssertTrue(args.contains("Be brief. Use \"quotes\"."))
    }

    func testFirstQuestionHasNoResume() throws {
        XCTAssertFalse(try arguments(resume: "").contains("--resume"))
    }
}

final class AskModelTests: XCTestCase {
    func testModelFlagFollowsTheSetting() throws {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/zsh")
        p.arguments = ["-fc", "claude() { printf '%s\\n' \"$@\" }; " + Asker.command]
        p.environment = ["ASSISTANT_MODEL": "haiku", "ASSISTANT_RESUME": ""]
        let out = Pipe()
        p.standardOutput = out
        try p.run()
        let args = String(decoding: out.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self).split(separator: "\n")
        p.waitUntilExit()
        XCTAssertEqual(Array(args.suffix(2)), ["--model", "haiku"])
        XCTAssertEqual(Asker.defaultModel, "haiku")
    }
}
