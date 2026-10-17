"""Plain-language tool descriptions shown to models and MCP hosts.

Descriptions explain what a tool does. They grant nothing: the gateway decides
every call against the assignment, whatever a description says.
"""

TOOL_DESCRIPTIONS = {
    "read_source": "Read one of the documents selected for this assignment (by source_id). The text comes back labeled as outside data.",
    "list_files": "List files in your workspace (optionally under a folder path).",
    "read_file": "Read a UTF-8 text file from your workspace by relative path.",
    "fetch_url": "Fetch a public https:// web page and return its readable text. Only allowed when the assignment permits web reading.",
    "write_report": "Write a text file into your workspace (relative path). Use this to save reports, notes or drafts.",
    "send_report": "Send a file from your workspace to a recipient. recipient_id must be one of the recipients allowed by the assignment.",
    "delete_file": "Delete a file from your workspace. Needs a grant from the user.",
    "make_payment": "Make a payment to an approved payee within the assignment's spending limit. Needs a grant from the user.",
}


def describe(tool_id: str) -> str:
    if tool_id in TOOL_DESCRIPTIONS:
        return TOOL_DESCRIPTIONS[tool_id]
    if tool_id.startswith("mcp__"):
        parts = tool_id.split("__", 2)
        return f"Tool '{parts[-1]}' from connected server '{parts[1]}'. Calls are checked by ANTIBODY before they run."
    return f"Tool {tool_id}."
