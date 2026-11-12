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

    func testContextRowCarriesFleetAndFocusUnderTheMarker() {
        XCTAssertNil(TurnContext.row(fleet: nil, focus: nil))
        let row = TurnContext.row(fleet: "FLEET", focus: (title: "T [1]", exchange: "EX"))
        XCTAssertEqual(row, TurnContext.marker + "\n\nFLEET\n\n# Recent conversation in T [1]\n\nEX")
    }

    func testParseReadsTheFinalJSONLine() {
        let ok = Asker.parse(Data("noise\n{\"result\":\"It waits on you.\",\"session_id\":\"s1\",\"total_cost_usd\":0.2,\"is_error\":false}\n".utf8))
        XCTAssertEqual(ok, Reply(text: "It waits on you.", sessionID: "s1", cost: 0.2, failed: false))
        XCTAssertTrue(Asker.parse(Data("command not found".utf8)).failed)
        XCTAssertTrue(Asker.parse(Data("{\"result\":\"limit\",\"is_error\":true}".utf8)).failed)
    }
}

/// The chat's and the summarizer's `claude` command lines, pinned exactly: any widening (more tools, a looser
/// permission mode, user settings, a missing guard hook) fails here.
final class AskCommandTests: XCTestCase {
    let turn = ClaudeInvocation(systemPrompt: "Be brief. Use \"quotes\".", input: Data(), model: "haiku", effort: "high")

    func testTheChatRunsSandboxedWithOnlyItsOwnCommands() {
        let guardJSON = #"{"hooks":{"PreToolUse":[{"hooks":[{"command":"'/A/Assistant Bar' --pretooluse-guard","type":"command"}],"matcher":"*"}]}}"#
        XCTAssertEqual(Asker.arguments(turn, tool: "/A/Assistant Bar"), [
            "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
            "--setting-sources", "", "--settings", guardJSON,
            "--tools", "Read,Grep,Glob,Bash",
            "--allowedTools", "Bash(/A/Assistant Bar --sessions)", "Bash(/A/Assistant Bar --send:*)", "Bash(/A/Assistant Bar --wait:*)",
            "Bash(/A/Assistant Bar --search:*)", "Bash(/A/Assistant Bar --memory:*)",
            "--permission-mode", "dontAsk", "--strict-mcp-config", "--disable-slash-commands",
            "--append-system-prompt", "Be brief. Use \"quotes\".", "--model", "haiku", "--effort", "high",
        ])
    }

    func testTheGuardHookQuotesTheToolPathForTheShell() throws {
        let settings = try JSONSerialization.jsonObject(with: Data(ClaudeSandbox.guardSettings(tool: "/A/it's").utf8)) as? [String: Any]
        let hook = ((((settings?["hooks"] as? [String: Any])?["PreToolUse"] as? [[String: Any]])?.first?["hooks"]) as? [[String: Any]])?.first
        XCTAssertEqual(hook?["command"] as? String, #"'/A/it'\''s' --pretooluse-guard"#)
    }

    func testTheSummarizerRunsWithoutToolsOrUserSettings() {
        XCTAssertEqual(ClaudeSummarizer.arguments(model: "haiku"), [
            "-p", "--output-format", "json", "--setting-sources", "", "--tools", "", "--permission-mode", "dontAsk",
            "--strict-mcp-config", "--disable-slash-commands", "--system-prompt", ClaudeSummarizer.system, "--model", "haiku",
        ])
    }

    func testTheBinaryIsResolvedWithoutTheAlias() {
        let exists: (String) -> Bool = { ["/opt/c", "/h/.local/bin/claude", "/w/claude"].contains($0) }
        XCTAssertEqual(ClaudeSandbox.resolve(environment: ["ASSISTANT_CLAUDE": "/opt/c", "HOME": "/h"], whence: { "/w/claude" }, isExecutable: exists), "/opt/c")
        XCTAssertEqual(ClaudeSandbox.resolve(environment: ["HOME": "/h"], whence: { "/w/claude\n" }, isExecutable: exists), "/w/claude")
        XCTAssertEqual(ClaudeSandbox.resolve(environment: ["HOME": "/h"], whence: { "claude: aliased to claude --model x" }, isExecutable: exists),
                       "/h/.local/bin/claude")
        XCTAssertNil(ClaudeSandbox.resolve(environment: ["HOME": "/none"], whence: { nil }, isExecutable: { _ in false }))
    }

    func testEffortIsAlwaysExplicit() {
        XCTAssertEqual(Asker.defaultModel, "haiku")
        for model in ["haiku", "sonnet", "opus"] { XCTAssertEqual(Asker.effort(for: model), "high") }
    }
}
