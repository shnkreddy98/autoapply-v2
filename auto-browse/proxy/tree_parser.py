import re
from dataclasses import dataclass, field

REF_RE = re.compile(r"\[ref=([^\]]+)\]")
NAME_RE = re.compile(r'"([^"]*)"')
_ROLE_WORD = re.compile(r"^[a-zA-Z][a-zA-Z0-9_\-]*")

LANDMARK_ROLES: frozenset[str] = frozenset(
    {
        "banner",
        "navigation",
        "main",
        "contentinfo",
        "search",
        "form",
        "complementary",
    }
)


@dataclass(eq=False)
class TreeNode:
    role: str
    name: str | None
    ref: str | None
    depth: int
    raw_line: str
    line_index: int
    children: list["TreeNode"] = field(default_factory=list)
    parent: "TreeNode | None" = field(default=None, repr=False)
    landmark: str = "root"
    is_url: bool = False
    is_text: bool = False


def _parse_line(line: str) -> tuple[str, str | None, str | None, bool, bool]:
    """Return (role, name, ref, is_url, is_text). role='' on unrecognized lines."""
    stripped = line.lstrip()
    if not stripped.startswith("- "):
        return "", None, None, False, False

    content = stripped[2:]

    if content.startswith("/url:"):
        return "/url", None, None, True, False

    if content.startswith("text:"):
        return "/text", None, None, False, True

    # YAML-quoted line: 'role "name" [attrs]':
    if content.startswith("'"):
        end = content.rfind("'", 1)
        if end > 0:
            content = content[1:end]
    elif content.endswith(":"):
        content = content[:-1].rstrip()
    elif ": " in content:
        # inline text after colon: strip it for role/ref extraction
        content = content.split(": ", 1)[0].rstrip()

    ref_m = REF_RE.search(content)
    ref = ref_m.group(1) if ref_m else None

    name_m = NAME_RE.search(content)
    name = name_m.group(1) if name_m else None

    role_m = _ROLE_WORD.match(content.lstrip())
    role = role_m.group(0) if role_m else ""

    return role, name, ref, False, False


def _tag_landmarks(node: TreeNode, current: str) -> None:
    if node.role in LANDMARK_ROLES:
        current = node.role
    node.landmark = current
    for child in node.children:
        _tag_landmarks(child, current)


def parse(text: str) -> TreeNode | None:
    """Parse a Playwright MCP snapshot text into a TreeNode tree. Returns root node."""
    lines = text.splitlines()
    root: TreeNode | None = None
    stack: list[TreeNode | None] = []
    counter = 0

    for raw_line in lines:
        if not raw_line.strip():
            continue

        stripped = raw_line.lstrip(" ")
        depth = (len(raw_line) - len(stripped)) // 2

        role, name, ref, is_url, is_text = _parse_line(raw_line)
        if not role:
            continue

        node = TreeNode(
            role=role,
            name=name,
            ref=ref,
            depth=depth,
            raw_line=raw_line,
            line_index=counter,
            is_url=is_url,
            is_text=is_text,
        )
        counter += 1

        if depth == 0:
            root = node
            stack = [node]
        else:
            while len(stack) <= depth:
                stack.append(None)
            parent = stack[depth - 1]
            if parent is not None:
                node.parent = parent
                parent.children.append(node)
            stack[depth] = node

    if root:
        _tag_landmarks(root, "root")

    return root


def collect_all(node: TreeNode) -> list[TreeNode]:
    """DFS collect all nodes in tree order (matches original line order)."""
    result: list[TreeNode] = []

    def _walk(n: TreeNode) -> None:
        result.append(n)
        for child in n.children:
            _walk(child)

    _walk(node)
    return result
