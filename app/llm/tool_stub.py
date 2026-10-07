"""A stand-in MCP server for the Claude Code adapter (ADR 0032). Run as a script by the CLI.

It lists the household's tools, from the JSON file it is given, so the model can call them as
functions. It runs none of them: the adapter reads the calls from the CLI's output and the loop
runs them. Standard library only, since it starts once per model step."""
import json
import sys


def main(tools_file: str) -> None:
    with open(tools_file) as source:
        tools = json.load(source)
    for line in sys.stdin:
        request = json.loads(line)
        if "id" not in request:
            continue   # a notification
        result: dict[str, object] = {}
        if request["method"] == "initialize":
            result = {"protocolVersion": request["params"]["protocolVersion"], "capabilities": {"tools": {}},
                      "serverInfo": {"name": "household", "version": "1"}}
        elif request["method"] == "tools/list":
            result = {"tools": tools}
        elif request["method"] == "tools/call":
            result = {"content": [{"type": "text", "text": "Requested."}]}   # the run ends here: one turn
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)


if __name__ == "__main__":
    main(sys.argv[1])
