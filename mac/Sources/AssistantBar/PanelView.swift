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
                .keyboardShortcut(.defaultAction)
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
        .frame(width: 440, height: 560)
    }

    var footer: String {
        let working = model.result.items(.working).count
        let untracked = model.result.untracked.count
        return "\(working) working · \(untracked) not tracked · ⌃⌥Space"
    }

    @ViewBuilder
    func section(_ title: String, _ items: [Item]) -> some View {
        if !items.isEmpty {
            VStack(alignment: .leading, spacing: 6) {
                Text("\(title) (\(items.count))").font(.subheadline.weight(.semibold)).foregroundStyle(.secondary)
                ForEach(items) { item in
                    Row(item: item) { model.focus(item.workspace) }
                }
            }
        }
    }
}

struct Row: View {
    let item: Item
    let open: () -> Void
    @State private var hovering = false

    var body: some View {
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
