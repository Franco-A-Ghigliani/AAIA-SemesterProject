class Tool:
    def __init__(self, name, description, args):
        self.name = name
        self.description = description
        self.args = args

    def to_json(self):
        return {
            "name": self.name,
            "description": self.description,
            "args": self.args
        }


tools = [
    Tool(name="list_files",
         description="List up to 200 non-hidden files below the workspace root.",
         args=[]),
    Tool(name="read_file",
         description="Read one UTF-8 text file, truncated after 12000 characters.",
         args=["path"]),
    Tool(name="search_files",
         description="Literal text search across non-hidden workspace files.",
         args=["query"]),
    Tool(name="write_file",
         description="Create a new UTF-8 file; fails if the path already exists.",
         args=["path", "content"]),
    Tool(name="edit_file",
         description="Replace one unique non-empty text occurrence in a file.",
         args=["path", "old", "new"]),
    Tool(name="delete_file",
         description="Delete a file.",
         args=["path"]),
    Tool(name="bash",
         description="Run one explicitly approved Bash command in a disposable networkless Docker container. Push, merge and deployment are disabled.",
         args=["command"]),
    Tool(name="fetch_url",
         description="GET HTTPS from docs.python.org pages only",
         args=["url"]),
]


def describe_tools():
    return [
        tool.to_json()
        for tool in tools
    ]

def get_tool(name):
    for tool in tools:
        if tool.name == name:
            return tool.to_json()
    return None
