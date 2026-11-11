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
        return Item(workspace: ws, summary: s, attention: attention, idleMinutes: idle, pullRequests: prs)
    }

    @MainActor
    func testPanelPutsTheNextSessionThatNeedsYouFirst() throws {
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
        for (name, view) in [("assistant-panel", AnyView(panel)), ("assistant-list", AnyView(list))] {
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
}
