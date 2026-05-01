import Foundation

public final class CodexService: CLIAgentService, @unchecked Sendable {
    public init(executable: String?, model: String? = nil) {
        super.init(agentName: "Codex", executable: executable, model: model)
    }

    public override func buildArguments(
        prompt: String,
        sessionID: String?,
        systemPrompt: String?
    ) -> [String] {
        let effectivePrompt = systemPrompt.map { "\($0)\n\n\(prompt)" } ?? prompt
        var args = ["--dangerously-bypass-approvals-and-sandbox"]
        if let sessionID {
            args += ["exec", "resume", sessionID]
        } else {
            args += ["exec"]
        }
        if let model {
            args += ["--model", model]
        }
        args += ["--json", "--skip-git-repo-check", effectivePrompt]
        return args
    }

    public override func parseLine(_ data: [String: Any]) -> [StreamEvent] {
        let eventType = data["type"] as? String ?? ""
        var events: [StreamEvent] = []

        if eventType == "thread.started", let threadID = data["thread_id"] as? String {
            events.append(StreamEvent(kind: .sessionStarted, sessionID: threadID))
        } else if eventType == "item.started",
                  let item = data["item"] as? [String: Any] {
            let itemType = item["type"] as? String ?? ""
            switch itemType {
            case "command_execution":
                events.append(StreamEvent(
                    kind: .toolUseStarted,
                    toolID: item["id"] as? String,
                    toolName: item["command"] as? String
                ))
            case "file_change":
                let changes = item["changes"] as? [[String: Any]] ?? []
                let summary = changes.map { change in
                    "\(change["kind"] ?? "?") \(change["path"] ?? "?")"
                }.joined(separator: ", ")
                events.append(StreamEvent(
                    kind: .toolUseStarted,
                    toolID: item["id"] as? String,
                    toolName: summary.isEmpty ? "file changes" : summary
                ))
            case "mcp_tool_call":
                let server = item["server"] as? String ?? ""
                let tool = item["tool"] as? String ?? ""
                events.append(StreamEvent(
                    kind: .toolUseStarted,
                    toolID: item["id"] as? String,
                    toolName: server.isEmpty ? tool : "\(server):\(tool)"
                ))
            case "web_search":
                let query = item["query"] as? String ?? ""
                events.append(StreamEvent(
                    kind: .toolUseStarted,
                    toolID: item["id"] as? String,
                    toolName: query.isEmpty ? "web_search" : "web_search: \(query)"
                ))
            default:
                break
            }
        } else if eventType == "item.completed",
                  let item = data["item"] as? [String: Any] {
            let itemType = item["type"] as? String ?? ""
            if itemType == "agent_message", let text = item["text"] as? String, !text.isEmpty {
                events.append(StreamEvent(kind: .textComplete, text: text))
            } else if itemType == "reasoning", let text = item["text"] as? String, !text.isEmpty {
                events.append(StreamEvent(kind: .thinking, text: text))
            } else if itemType == "web_search", let query = item["query"] as? String, !query.isEmpty {
                events.append(StreamEvent(kind: .thinking, text: "> Searched: \(query)\n"))
            } else if itemType == "todo_list",
                      let todos = item["items"] as? [[String: Any]],
                      !todos.isEmpty {
                let text = todos.map { todo in
                    let completed = (todo["completed"] as? Bool) == true ? "x" : " "
                    return "  [\(completed)] \(todo["text"] as? String ?? "")"
                }.joined(separator: "\n")
                events.append(StreamEvent(kind: .thinking, text: "\(text)\n"))
            }
        } else if eventType == "turn.completed", let usage = data["usage"] {
            events.append(StreamEvent(kind: .usage, usageDescription: String(describing: usage)))
        } else if eventType == "turn.failed" {
            let error = data["error"] as? [String: Any]
            let message = error?["message"] as? String ?? data["message"] as? String ?? "Turn failed"
            events.append(StreamEvent(kind: .error, error: message))
        } else if eventType == "error" {
            let message = data["message"] as? String ?? "Unknown error"
            events.append(StreamEvent(kind: .error, error: message))
        }

        return events
    }
}
